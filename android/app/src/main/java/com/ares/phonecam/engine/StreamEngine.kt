package com.ares.phonecam.engine

import android.content.Context
import android.graphics.SurfaceTexture
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CameraMetadata
import android.hardware.camera2.params.MeteringRectangle
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Log
import android.util.Size
import android.view.OrientationEventListener
import android.view.Surface
import com.ares.phonecam.TAG
import com.ares.phonecam.camera.CameraCatalog
import com.ares.phonecam.camera.CameraController
import com.ares.phonecam.camera.CameraInfo
import com.ares.phonecam.camera.CaptureParams
import com.ares.phonecam.camera.Facing
import com.ares.phonecam.camera.Geometry
import com.ares.phonecam.camera.afModeValue
import com.ares.phonecam.encoder.EncoderConfig
import com.ares.phonecam.encoder.EncoderSelector
import com.ares.phonecam.encoder.VideoEncoder
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineExceptionHandler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.FlowPreview
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.android.asCoroutineDispatcher
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.debounce
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.flow.filterNotNull
import kotlinx.coroutines.flow.map
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

enum class Orientation(val wire: String) {
    LANDSCAPE("landscape"), PORTRAIT("portrait"), AUTO("auto");

    companion object {
        fun fromWire(s: String?) = entries.firstOrNull { it.wire == s }
    }
}

/** Live camera controls (no encoder restart needed). */
data class Controls(
    val torch: Boolean = false,
    /** Protocol AF mode name; "locked" = AUTO mode after a one-shot scan. */
    val afMode: String = "continuous-video",
    val aeCompIndex: Int = 0,
    val orientation: Orientation = Orientation.LANDSCAPE,
    /** Normalized point in the sensor-oriented frame for AF/AE metering; null = whole frame. */
    val focusPoint: Pair<Float, Float>? = null,
    /** Incremented to request a new one-shot AF scan. */
    val focusSeq: Int = 0,
)

const val MIN_BITRATE = 250_000
const val MAX_BITRATE = 40_000_000

data class EngineStats(
    val fps: Float = 0f,
    val bitrateBps: Long = 0,
    val frames: Long = 0,
    /** Sensor timestamp → encoded frame available (exposure end/readout + ISP + encode). */
    val pipelineMs: Float = 0f,
    val pipelineMaxMs: Float = 0f,
    /** Sensor timestamp → capture result delivered (exposure + readout + ISP). */
    val captureMs: Float = 0f,
    val exposureMs: Float = 0f,
    val pipelineDepth: Int = 0,
)

data class EngineState(
    val cameras: List<CameraInfo> = emptyList(),
    val config: StreamConfig? = null,
    val controls: Controls = Controls(),
    /** Clockwise rotation the receiver should apply (0/90/180/270), see [Geometry.streamRotation]. */
    val rotation: Int = 0,
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
@OptIn(FlowPreview::class)
class StreamEngine(context: Context, private val settings: SettingsRepository) {
    private val appContext = context.applicationContext
    private val cameraManager = appContext.getSystemService(CameraManager::class.java)
    private val thread = HandlerThread("camera").apply { start() }
    private val handler = Handler(thread.looper)
    private val scope = CoroutineScope(
        SupervisorJob() + handler.asCoroutineDispatcher("camera") +
            CoroutineExceptionHandler { _, e -> Log.e(TAG, "engine coroutine failed", e) },
    )
    private val mutex = Mutex()
    private val captureSumUs = AtomicLong()
    private val captureN = AtomicLong()
    private val exposureSumUs = AtomicLong()
    @Volatile private var lastDepth = 0
    private val camera = CameraController(
        cameraManager, handler,
        onDeviceLost = { id, reason -> scope.launch { mutex.withLock { handleFailure(RuntimeException("camera $id $reason")) } } },
        onCaptureMetrics = { latencyNs, exposureNs, depth ->
            if (_state.value.camera?.timestampRealtime == true) {
                captureSumUs.addAndGet(latencyNs / 1000)
                captureN.incrementAndGet()
            }
            exposureSumUs.addAndGet(exposureNs / 1000)
            lastDepth = depth
        },
    )

    private data class Desired(
        val streaming: Boolean = false,
        val record: Boolean = false,
        val preview: SurfaceTexture? = null,
        val config: StreamConfig? = null,
        val controls: Controls = Controls(),
        val tuning: Tuning = Tuning(),
        /** Bumped to force a re-apply after a failure. */
        val epoch: Int = 0,
    )

    private val desired = MutableStateFlow(Desired())
    private val _state = MutableStateFlow(EngineState())
    val state: StateFlow<EngineState> = _state.asStateFlow()

    // ---- camera-thread-only state ----
    @Volatile private var encoder: VideoEncoder? = null // read from other threads for keyframe requests
    private var encoderBitrate = 0
    private var previewTexture: SurfaceTexture? = null
    private var previewSurface: Surface? = null
    private var previewSize: Size? = null
    private val releaseCallbacks = mutableMapOf<SurfaceTexture, () -> Unit>()
    private var fileSink: FileSink? = null
    private var failures = 0
    private var appliedFocusSeq = 0

    /** Rotation hint in protocol flag bits 1-2 (quarter turns clockwise), read by the encoder thread. */
    @Volatile private var rotationFlags = 0
    @Volatile private var autoDeviceDeg = 270
    private val orientationListener = object : OrientationEventListener(appContext) {
        override fun onOrientationChanged(deg: Int) {
            if (deg == ORIENTATION_UNKNOWN) return
            val snapped = Geometry.snapOrientation(deg, autoDeviceDeg)
            if (snapped != autoDeviceDeg) {
                autoDeviceDeg = snapped
                updateRotation()
            }
        }
    }

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
        scope.launch {
            // Persist the last-used stream settings (debounced; skips the initial value).
            desired.map { d -> d.config?.let { it to d.controls.orientation.wire } }
                .filterNotNull().distinctUntilChanged().drop(1).debounce(500)
                .collect { (cfg, orient) -> settings.saveStream(cfg, orient) }
        }
    }

    // ------------------------------------------------------------------ public API (any thread)

    fun startStreaming(record: Boolean) = desired.update { it.copy(streaming = true, record = record) }

    fun stopStreaming() = desired.update { it.copy(streaming = false, record = false) }

    fun requestKeyframe() = encoder?.requestKeyframe()

    /** Experimental latency knobs (see [Tuning]); throws IllegalArgumentException on bad values. */
    fun setTuning(changes: org.json.JSONObject) {
        val t = desired.value.tuning.merge(changes)
        desired.update { it.copy(tuning = t) }
        Log.i(TAG, "tuning: ${t.toJson()}")
    }

    val tuning: Tuning get() = desired.value.tuning

    // ---- controls: validate synchronously (IllegalArgumentException = rejected), apply async.

    private fun currentCamera(): CameraInfo =
        _state.value.camera ?: throw IllegalArgumentException("camera not ready")

    /** Update desired state and publish the requested settings immediately (STATE sync). */
    private fun mutate(f: (Desired) -> Desired) {
        desired.update(f)
        val d = desired.value
        _state.update { it.copy(config = d.config, controls = d.controls) }
        updateRotation()
    }

    fun setCamera(id: String) {
        val cam = _state.value.cameras.firstOrNull { it.id == id } ?: throw IllegalArgumentException("unknown camera $id")
        mutate { d ->
            val cfg = d.config ?: return@mutate d
            val size = closestSize(cam, cfg.width, cfg.height)
            val fps = cam.supportedFps.minBy { abs(it - cfg.fps) }
            // Keep the AF mode if the new camera supports it, except "off" (often forced by a
            // fixed-focus camera) which would otherwise stick when switching back.
            val af = d.controls.afMode.takeIf { name ->
                name != "off" && afModeValue(name)?.let { it in cam.afModes } == true
            } ?: defaultAfMode(cam)
            d.copy(
                config = cfg.copy(cameraId = id, width = size.width, height = size.height, fps = fps),
                controls = d.controls.copy(
                    torch = d.controls.torch && cam.hasFlash,
                    afMode = af,
                    aeCompIndex = d.controls.aeCompIndex.coerceIn(cam.aeCompRange.lower, cam.aeCompRange.upper),
                    focusPoint = null,
                ),
            )
        }
    }

    fun setFacing(facing: Facing) {
        val cam = _state.value.cameras.firstOrNull { it.facing == facing }
            ?: throw IllegalArgumentException("no ${facing.wire} camera")
        setCamera(cam.id)
    }

    /** Next camera in the list (the phone UI's switch button). */
    fun switchCamera() {
        val cams = _state.value.cameras
        val i = cams.indexOfFirst { it.id == desired.value.config?.cameraId }
        if (cams.size > 1) setCamera(cams[(i + 1) % cams.size].id)
    }

    fun setResolution(width: Int, height: Int) {
        val cam = currentCamera()
        require(cam.sizes.any { it.width == width && it.height == height }) { "unsupported size ${width}x$height" }
        mutate { d -> d.copy(config = d.config?.copy(width = width, height = height)) }
    }

    fun setFps(fps: Int) {
        val cam = currentCamera()
        require(fps in cam.supportedFps) { "unsupported fps $fps (supported: ${cam.supportedFps})" }
        mutate { d -> d.copy(config = d.config?.copy(fps = fps)) }
    }

    fun setBitrate(bps: Int) {
        require(bps in MIN_BITRATE..MAX_BITRATE) { "bitrate must be $MIN_BITRATE..$MAX_BITRATE" }
        mutate { d -> d.copy(config = d.config?.copy(bitrate = bps)) }
    }

    fun setTorch(on: Boolean) {
        require(!on || currentCamera().hasFlash) { "this camera has no flash" }
        mutate { d -> d.copy(controls = d.controls.copy(torch = on)) }
    }

    fun setAfMode(name: String) {
        val cam = currentCamera()
        val value = afModeValue(name) ?: throw IllegalArgumentException("unknown AF mode $name")
        require(value in cam.afModes) { "AF mode $name not supported (supported: ${cam.afModeNames})" }
        mutate { d ->
            val c = d.controls
            d.copy(controls = if (name == "locked") c.copy(afMode = name, focusSeq = c.focusSeq + 1)
            else c.copy(afMode = name, focusPoint = if (name.startsWith("continuous")) null else c.focusPoint))
        }
    }

    /** Tap-to-focus at a normalized point of the frame as streamed (rotated). */
    fun focusAt(x: Float, y: Float) {
        require(x in 0f..1f && y in 0f..1f) { "x and y must be in 0..1" }
        val (sx, sy) = Geometry.toSensor(x, y, _state.value.rotation)
        focusAtSensor(sx, sy)
    }

    /** Tap-to-focus at a normalized point of the sensor-oriented frame. */
    fun focusAtSensor(sx: Float, sy: Float) {
        val cam = currentCamera()
        mutate { d ->
            val locked = CameraMetadata.CONTROL_AF_MODE_AUTO in cam.afModes
            d.copy(controls = d.controls.copy(
                focusPoint = sx.coerceIn(0f, 1f) to sy.coerceIn(0f, 1f),
                afMode = if (locked) "locked" else d.controls.afMode,
                focusSeq = d.controls.focusSeq + 1,
            ))
        }
    }

    fun setExposureEv(ev: Float) {
        val cam = currentCamera()
        val index = cam.indexForEv(ev)
        mutate { d -> d.copy(controls = d.controls.copy(aeCompIndex = index)) }
    }

    fun setOrientation(o: Orientation) {
        mutate { d -> d.copy(controls = d.controls.copy(orientation = o)) }
        if (o == Orientation.AUTO) orientationListener.enable() else orientationListener.disable()
    }

    private fun updateRotation() {
        val cam = _state.value.camera ?: return
        val dev = when (desired.value.controls.orientation) {
            Orientation.LANDSCAPE -> 270
            Orientation.PORTRAIT -> 0
            Orientation.AUTO -> autoDeviceDeg
        }
        val r = Geometry.streamRotation(cam.sensorOrientation, cam.facing == Facing.FRONT, dev)
        rotationFlags = (r / 90) shl 1
        if (_state.value.rotation != r) _state.update { it.copy(rotation = r) }
    }

    private fun closestSize(cam: CameraInfo, w: Int, h: Int): Size =
        cam.sizes.firstOrNull { it.width == w && it.height == h }
            ?: cam.sizes.minBy { abs(it.width.toFloat() / it.height - w.toFloat() / h) * 1e7 + abs(it.width * it.height - w * h) }

    private fun defaultAfMode(cam: CameraInfo): String = when {
        CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_VIDEO in cam.afModes -> "continuous-video"
        CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_PICTURE in cam.afModes -> "continuous-picture"
        CameraMetadata.CONTROL_AF_MODE_AUTO in cam.afModes -> "auto"
        else -> "off"
    }

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

    private suspend fun loadCameras() {
        val encoders = EncoderSelector.all()
        val cams = CameraCatalog.query(cameraManager).map { cam ->
            // Only offer sizes that some H.264 encoder can also handle at the camera's fps.
            cam.copy(sizes = cam.sizes.filter { s -> encoders.any { it.supports(s.width, s.height, min(30, cam.supportedFps.max())) } })
        }.filter { it.sizes.isNotEmpty() }
        val saved = runCatching { settings.load() }.getOrDefault(SavedSettings())
        val cam = cams.firstOrNull { it.id == saved.cameraId }
            ?: cams.firstOrNull { it.facing == Facing.BACK } ?: cams.firstOrNull()
        if (cam == null) {
            _state.update { it.copy(error = "No usable camera found") }
            return
        }
        val size = cam.sizes.firstOrNull { it.width == saved.width && it.height == saved.height }
            ?: cam.sizes.firstOrNull { it.width == 1280 && it.height == 720 }
            ?: cam.sizes.minBy { abs(it.width * it.height - 1280 * 720) }
        val fps = saved.fps?.takeIf { it in cam.supportedFps }
            ?: cam.supportedFps.filter { it <= 30 }.maxOrNull() ?: cam.supportedFps.first()
        val bitrate = saved.bitrate?.takeIf { it in MIN_BITRATE..MAX_BITRATE } ?: 6_000_000
        val cfg = StreamConfig(cam.id, size.width, size.height, fps, bitrate)
        val controls = Controls(
            afMode = defaultAfMode(cam),
            orientation = Orientation.fromWire(saved.orientation) ?: Orientation.LANDSCAPE,
        )
        _state.update { it.copy(cameras = cams, config = cfg, controls = controls) }
        desired.update { it.copy(config = it.config ?: cfg, controls = controls) }
        updateRotation()
        if (controls.orientation == Orientation.AUTO) orientationListener.enable()
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

        val t = d.tuning
        val encCfg = EncoderConfig(cfg.width, cfg.height, cfg.fps, cfg.bitrate, profile = t.profile, operatingRate = t.operatingRate, vendor = t.vendor)
        val curEnc = encoder
        val encoderChange = d.streaming != (curEnc != null) ||
            (curEnc != null && curEnc.config.copy(bitrate = 0) != encCfg.copy(bitrate = 0))
        val wantPreviewSize = d.preview?.let { choosePreviewSize(info, cfg) }
        val previewChange = d.preview !== previewTexture || wantPreviewSize != previewSize
        val cameraChange = camera.cameraId != null && camera.cameraId != cfg.cameraId
        val params = captureParams(info, cfg, d.controls, d.streaming).copy(tuning = d.tuning)

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
            if (d.controls.afMode == "locked") appliedFocusSeq = -1 // re-run the scan on the new session
        } else {
            camera.applyParams(params)
        }
        if (d.controls.focusSeq != appliedFocusSeq && afModeValue(d.controls.afMode) == CameraMetadata.CONTROL_AF_MODE_AUTO) {
            camera.triggerAutofocus()
        }
        appliedFocusSeq = d.controls.focusSeq
        if (encoder != null && encoderBitrate != cfg.bitrate) {
            encoder?.setBitrate(cfg.bitrate)
            encoderBitrate = cfg.bitrate
        }
        if (d.streaming && d.record && fileSink == null) startRecording(cfg)
        if (!(d.streaming && d.record) && fileSink != null) stopRecording()
    }

    private fun captureParams(info: CameraInfo, cfg: StreamConfig, c: Controls, recording: Boolean): CaptureParams {
        val af = afModeValue(c.afMode)?.takeIf { it in info.afModes }
            ?: afModeValue(defaultAfMode(info)) ?: CameraMetadata.CONTROL_AF_MODE_OFF
        val region = c.focusPoint?.let { (sx, sy) ->
            val a = info.activeArray
            val (l, t, r, b) = Geometry.meteringRegion(sx, sy, cfg.width, cfg.height, a.width(), a.height()).toList()
            MeteringRectangle(a.left + l, a.top + t, r - l, b - t, MeteringRectangle.METERING_WEIGHT_MAX - 1)
        }
        return CaptureParams(
            fpsRange = info.fpsRangeFor(cfg.fps),
            afMode = af,
            torch = c.torch && info.hasFlash,
            aeCompensation = c.aeCompIndex.coerceIn(info.aeCompRange.lower, info.aeCompRange.upper),
            recording = recording,
            region = region,
            maxAfRegions = info.maxAfRegions,
            maxAeRegions = info.maxAeRegions,
        )
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
                controls = d.controls,
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
            val flags = rotationFlags
            sinks.forEach { it.onFrame(data, ptsUs, keyframe, flags) }
        }

        override fun onError(e: Exception) {
            scope.launch { mutex.withLock { handleFailure(e) } }
        }
    }

    private suspend fun statsLoop() {
        var lastFrames = 0L
        var lastBytes = 0L
        var lastSum = 0L
        var lastCapSum = 0L
        var lastCapN = 0L
        var lastExpSum = 0L
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
                captureMs = (captureN.get() - lastCapN).let { n -> if (n > 0) (captureSumUs.get() - lastCapSum) / n / 1000f else 0f },
                exposureMs = (captureN.get() - lastCapN).let { n -> if (n > 0) (exposureSumUs.get() - lastExpSum) / n / 1000f else 0f },
                pipelineDepth = lastDepth,
            )
            lastCapSum = captureSumUs.get(); lastCapN = captureN.get(); lastExpSum = exposureSumUs.get()
            if (df > 0) Log.d(TAG, "stats $stats")
            _state.update { it.copy(stats = stats) }
            lastFrames = f; lastBytes = b; lastSum = s; lastT = now
        }
    }
}
