package com.ares.phonecam.engine

import android.content.Context
import android.graphics.SurfaceTexture
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CameraMetadata
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Log
import android.util.Size
import android.view.Surface
import com.ares.phonecam.TAG
import com.ares.phonecam.camera.CameraCatalog
import com.ares.phonecam.camera.CameraController
import com.ares.phonecam.camera.CameraInfo
import com.ares.phonecam.camera.CaptureParams
import com.ares.phonecam.camera.Facing
import com.ares.phonecam.encoder.EncoderConfig
import com.ares.phonecam.encoder.EncoderSelector
import com.ares.phonecam.encoder.VideoEncoder
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineExceptionHandler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.android.asCoroutineDispatcher
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.abs
import kotlin.math.min

data class StreamConfig(val cameraId: String, val width: Int, val height: Int, val fps: Int, val bitrate: Int)

data class EngineStats(
    val fps: Float = 0f,
    val bitrateBps: Long = 0,
    val frames: Long = 0,
    /** Sensor timestamp → encoded frame available (exposure end/readout + ISP + encode). */
    val pipelineMs: Float = 0f,
    val pipelineMaxMs: Float = 0f,
)

data class EngineState(
    val cameras: List<CameraInfo> = emptyList(),
    val config: StreamConfig? = null,
    val streaming: Boolean = false,
    val cameraActive: Boolean = false,
    val encoderName: String? = null,
    val encoderHardware: Boolean = false,
    val recordingFile: File? = null,
    val lastRecording: File? = null,
    val error: String? = null,
    val stats: EngineStats = EngineStats(),
) {
    val camera: CameraInfo? get() = cameras.firstOrNull { it.id == config?.cameraId }
}

/**
 * Owns camera, encoder and sinks. Callers express what they *want* (streaming on/off, a preview
 * texture, a config); a single coroutine on the camera thread reconciles the hardware with it.
 * Lives as long as the process (held by [com.ares.phonecam.PhoneCamApp]).
 */
class StreamEngine(context: Context) {
    private val appContext = context.applicationContext
    private val cameraManager = appContext.getSystemService(CameraManager::class.java)
    private val thread = HandlerThread("camera").apply { start() }
    private val handler = Handler(thread.looper)
    private val scope = CoroutineScope(
        SupervisorJob() + handler.asCoroutineDispatcher("camera") +
            CoroutineExceptionHandler { _, e -> Log.e(TAG, "engine coroutine failed", e) },
    )
    private val mutex = Mutex()
    private val camera = CameraController(cameraManager, handler) { id, reason ->
        scope.launch { mutex.withLock { handleFailure(RuntimeException("camera $id $reason")) } }
    }

    private data class Desired(
        val streaming: Boolean = false,
        val record: Boolean = false,
        val preview: SurfaceTexture? = null,
        val config: StreamConfig? = null,
        /** Bumped to force a re-apply after a failure. */
        val epoch: Int = 0,
    )

    private val desired = MutableStateFlow(Desired())
    private val _state = MutableStateFlow(EngineState())
    val state: StateFlow<EngineState> = _state.asStateFlow()

    // ---- camera-thread-only state ----
    private var encoder: VideoEncoder? = null
    private var encoderBitrate = 0
    private var previewTexture: SurfaceTexture? = null
    private var previewSurface: Surface? = null
    private var previewSize: Size? = null
    private val releaseCallbacks = mutableMapOf<SurfaceTexture, () -> Unit>()
    private var fileSink: FileSink? = null
    private var failures = 0

    // ---- shared with the encoder thread ----
    private val sinks = CopyOnWriteArrayList<FrameSink>()
    @Volatile private var lastCsd: ByteArray? = null
    private val frames = AtomicLong()
    private val bytes = AtomicLong()
    private val latencySumUs = AtomicLong()
    private val latencyMaxUs = AtomicLong()

    init {
        scope.launch { loadCameras() }
        scope.launch { desired.collect { d -> mutex.withLock { applySafely(d) } } }
        scope.launch { statsLoop() }
    }

    // ------------------------------------------------------------------ public API (any thread)

    fun startStreaming(record: Boolean) = desired.update { it.copy(streaming = true, record = record) }

    fun stopStreaming() = desired.update { it.copy(streaming = false, record = false) }

    fun updateConfig(transform: (StreamConfig) -> StreamConfig) =
        desired.update { d -> d.config?.let { d.copy(config = transform(it)) } ?: d }

    fun requestKeyframe() = encoder?.requestKeyframe()

    fun attachPreview(texture: SurfaceTexture) = desired.update { it.copy(preview = texture) }

    /** The UI's texture is going away. [onReleased] runs once the camera no longer draws into it. */
    fun detachPreview(texture: SurfaceTexture, onReleased: () -> Unit) {
        scope.launch {
            mutex.withLock {
                if (previewTexture === texture || desired.value.preview === texture) {
                    releaseCallbacks[texture] = onReleased
                    desired.update { if (it.preview === texture) it.copy(preview = null) else it }
                } else {
                    onReleased()
                }
            }
        }
    }

    fun addSink(sink: FrameSink) {
        sinks += sink
        lastCsd?.let { sink.onConfig(it) }
        encoder?.requestKeyframe()
    }

    fun removeSink(sink: FrameSink) {
        if (sinks.remove(sink)) sink.close()
    }

    fun shutdown() {
        scope.launch { mutex.withLock { teardown() } }.invokeOnCompletion {
            scope.cancel()
            thread.quitSafely()
        }
    }

    // ------------------------------------------------------------------ reconciliation

    private fun loadCameras() {
        val encoders = EncoderSelector.all()
        val cams = CameraCatalog.query(cameraManager).map { cam ->
            // Only offer sizes that some H.264 encoder can also handle at the camera's fps.
            cam.copy(sizes = cam.sizes.filter { s -> encoders.any { it.supports(s.width, s.height, min(30, cam.supportedFps.max())) } })
        }.filter { it.sizes.isNotEmpty() }
        val cam = cams.firstOrNull { it.facing == Facing.BACK } ?: cams.firstOrNull()
        if (cam == null) {
            _state.update { it.copy(error = "No usable camera found") }
            return
        }
        val size = cam.sizes.firstOrNull { it.width == 1280 && it.height == 720 }
            ?: cam.sizes.minBy { abs(it.width * it.height - 1280 * 720) }
        val fps = cam.supportedFps.filter { it <= 30 }.maxOrNull() ?: cam.supportedFps.first()
        val cfg = StreamConfig(cam.id, size.width, size.height, fps, 6_000_000)
        _state.update { it.copy(cameras = cams, config = cfg) }
        desired.update { it.copy(config = it.config ?: cfg) }
    }

    private suspend fun applySafely(d: Desired) {
        try {
            apply(d)
            if (failures > 0 || _state.value.error != null) {
                failures = 0
                _state.update { it.copy(error = null) }
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            handleFailure(e)
        }
        publishState()
    }

    private suspend fun apply(d: Desired) {
        val cfg = d.config ?: return
        val info = _state.value.cameras.firstOrNull { it.id == cfg.cameraId } ?: return
        val wantCamera = d.streaming || d.preview != null
        if (!wantCamera) {
            teardown()
            return
        }

        val encCfg = EncoderConfig(cfg.width, cfg.height, cfg.fps, cfg.bitrate)
        val curEnc = encoder
        val encoderChange = d.streaming != (curEnc != null) ||
            (curEnc != null && curEnc.config.copy(bitrate = 0) != encCfg.copy(bitrate = 0))
        val wantPreviewSize = d.preview?.let { choosePreviewSize(info, cfg) }
        val previewChange = d.preview !== previewTexture || wantPreviewSize != previewSize
        val cameraChange = camera.cameraId != null && camera.cameraId != cfg.cameraId
        val params = captureParams(info, cfg, d.streaming)

        if (encoderChange || previewChange || cameraChange || camera.device == null) {
            camera.closeSession()
            if (cameraChange) camera.close()
            if (encoderChange) {
                stopRecording()
                stopEncoder()
                if (d.streaming) startEncoder(encCfg)
            }
            if (previewChange) swapPreview(d.preview, wantPreviewSize)
            if (camera.device == null) camera.open(cfg.cameraId)
            camera.startSession(listOfNotNull(encoder?.inputSurface, previewSurface), params)
        } else {
            camera.applyParams(params)
        }
        if (encoder != null && encoderBitrate != cfg.bitrate) {
            encoder?.setBitrate(cfg.bitrate)
            encoderBitrate = cfg.bitrate
        }
        if (d.streaming && d.record && fileSink == null) startRecording(cfg)
        if (!(d.streaming && d.record) && fileSink != null) stopRecording()
    }

    private fun captureParams(info: CameraInfo, cfg: StreamConfig, recording: Boolean): CaptureParams {
        val af = listOf(
            CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_VIDEO,
            CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_PICTURE,
            CameraMetadata.CONTROL_AF_MODE_AUTO,
        ).firstOrNull { it in info.afModes } ?: CameraMetadata.CONTROL_AF_MODE_OFF
        return CaptureParams(fpsRange = info.fpsRangeFor(cfg.fps), afMode = af, recording = recording)
    }

    /** Preview at the stream size if possible, else the closest size with the same aspect ratio. */
    private fun choosePreviewSize(info: CameraInfo, cfg: StreamConfig): Size {
        val sizes = info.previewSizes
        sizes.firstOrNull { it.width == cfg.width && it.height == cfg.height }?.let { return it }
        val aspect = cfg.width.toFloat() / cfg.height
        return sizes.filter { abs(it.width.toFloat() / it.height - aspect) < 0.01f }
            .minByOrNull { abs(it.width * it.height - cfg.width * cfg.height) }
            ?: sizes.firstOrNull() ?: Size(cfg.width, cfg.height)
    }

    private fun startEncoder(cfg: EncoderConfig) {
        lastCsd = null
        val enc = VideoEncoder.create(cfg, encoderListener)
        encoder = enc
        encoderBitrate = cfg.bitrate
        _state.update { it.copy(encoderName = enc.name, encoderHardware = enc.hardware) }
    }

    private fun stopEncoder() {
        encoder?.release()
        encoder = null
        lastCsd = null
    }

    private fun swapPreview(texture: SurfaceTexture?, size: Size?) {
        val old = previewTexture
        previewSurface?.release()
        previewSurface = null
        previewTexture = null
        previewSize = null
        if (old != null && old !== texture) releaseCallbacks.remove(old)?.invoke()
        if (texture != null && size != null) {
            texture.setDefaultBufferSize(size.width, size.height)
            previewTexture = texture
            previewSize = size
            previewSurface = Surface(texture)
        }
    }

    private fun startRecording(cfg: StreamConfig) {
        val dir = appContext.getExternalFilesDir("recordings") ?: File(appContext.filesDir, "recordings")
        dir.mkdirs()
        val stamp = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
        val sink = FileSink(File(dir, "phonecam-$stamp-${cfg.width}x${cfg.height}-${cfg.fps}.h264"))
        fileSink = sink
        addSink(sink)
        _state.update { it.copy(recordingFile = sink.file) }
        Log.i(TAG, "recording to ${sink.file}")
    }

    private fun stopRecording() {
        val sink = fileSink ?: return
        fileSink = null
        removeSink(sink)
        _state.update { it.copy(recordingFile = null, lastRecording = sink.file) }
    }

    private suspend fun teardown() {
        camera.close()
        stopRecording()
        stopEncoder()
        // Drops our Surface; a texture the UI already detached gets its release callback here.
        swapPreview(null, null)
    }

    private suspend fun handleFailure(e: Exception) {
        Log.e(TAG, "pipeline failure", e)
        failures++
        teardown()
        _state.update { it.copy(error = e.message ?: e.javaClass.simpleName) }
        val d = desired.value
        if (!d.streaming && d.preview == null) return
        if (failures > 5) {
            Log.e(TAG, "giving up after $failures failures")
            desired.update { it.copy(streaming = false, record = false) }
            return
        }
        val backoff = min(500L shl (failures - 1), 5000L)
        scope.launch {
            delay(backoff)
            desired.update { it.copy(epoch = it.epoch + 1) }
        }
    }

    private fun publishState() {
        val d = desired.value
        _state.update {
            it.copy(
                config = d.config,
                streaming = d.streaming,
                cameraActive = camera.device != null,
                encoderName = encoder?.name ?: it.encoderName,
            )
        }
    }

    // ------------------------------------------------------------------ encoder output

    private val encoderListener = object : VideoEncoder.Listener {
        override fun onConfig(csd: ByteArray) {
            if (lastCsd?.contentEquals(csd) == true) return
            Log.i(TAG, "encoder config (SPS/PPS) ${csd.size} bytes")
            lastCsd = csd
            sinks.forEach { it.onConfig(csd) }
        }

        override fun onFrame(data: ByteArray, ptsUs: Long, keyframe: Boolean) {
            val latency = SystemClock.elapsedRealtimeNanos() / 1000 - ptsUs
            frames.incrementAndGet()
            bytes.addAndGet(data.size.toLong())
            latencySumUs.addAndGet(latency)
            latencyMaxUs.accumulateAndGet(latency) { a, b -> maxOf(a, b) }
            sinks.forEach { it.onFrame(data, ptsUs, keyframe) }
        }

        override fun onError(e: Exception) {
            scope.launch { mutex.withLock { handleFailure(e) } }
        }
    }

    private suspend fun statsLoop() {
        var lastFrames = 0L
        var lastBytes = 0L
        var lastSum = 0L
        var lastT = SystemClock.elapsedRealtime()
        while (true) {
            delay(1000)
            val now = SystemClock.elapsedRealtime()
            val f = frames.get()
            val b = bytes.get()
            val s = latencySumUs.get()
            val dt = (now - lastT).coerceAtLeast(1) / 1000f
            val df = f - lastFrames
            val stats = EngineStats(
                fps = df / dt,
                bitrateBps = ((b - lastBytes) * 8 / dt).toLong(),
                frames = f,
                pipelineMs = if (df > 0) (s - lastSum) / df / 1000f else 0f,
                pipelineMaxMs = latencyMaxUs.getAndSet(0) / 1000f,
            )
            if (df > 0) Log.d(TAG, "stats $stats")
            _state.update { it.copy(stats = stats) }
            lastFrames = f; lastBytes = b; lastSum = s; lastT = now
        }
    }
}
