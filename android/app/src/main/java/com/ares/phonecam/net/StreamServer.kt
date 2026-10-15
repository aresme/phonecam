package com.ares.phonecam.net

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.Build
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import com.ares.phonecam.BuildConfig
import com.ares.phonecam.TAG
import com.ares.phonecam.camera.CameraInfo
import com.ares.phonecam.camera.Facing
import com.ares.phonecam.engine.Orientation
import com.ares.phonecam.engine.EngineState
import com.ares.phonecam.engine.StreamEngine
import com.ares.phonecam.protocol.Packet
import com.ares.phonecam.protocol.PacketReader
import com.ares.phonecam.protocol.PacketType
import com.ares.phonecam.protocol.PacketWriter
import com.ares.phonecam.protocol.Protocol
import kotlinx.coroutines.CoroutineExceptionHandler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.IOException
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketException

enum class Transport(val label: String) { USB("USB"), WIFI("Wi-Fi") }

data class ServerState(
    val running: Boolean = false,
    val videoClient: String? = null,
    val controlClient: String? = null,
    val error: String? = null,
    /** Required from non-USB clients in HELLO; null = pairing disabled. */
    val pairingCode: String? = null,
    val rejected: String? = null,
) {
    val connected get() = videoClient != null
    /** adb forward connections arrive from adbd on the phone's loopback interface. */
    val transport: Transport? get() = videoClient?.let { if (it == "127.0.0.1" || it == "::1") Transport.USB else Transport.WIFI }
}

/**
 * TCP server on the video (47100) and control (47101) ports. One client per port: a new
 * connection replaces the previous one. Runs while the streaming service runs.
 */
class StreamServer(private val context: Context, private val engine: StreamEngine) {
    private val _state = MutableStateFlow(ServerState())
    val state: StateFlow<ServerState> = _state.asStateFlow()

    @Volatile private var scope: CoroutineScope? = null
    private val serverSockets = mutableListOf<ServerSocket>()
    @Volatile private var video: VideoClient? = null
    @Volatile private var control: Socket? = null
    @Volatile private var pairingCode: String? = null
    /** Remote IPs with a paired control connection (value: expiry, ms since boot; MAX = connected). */
    private val authorized = java.util.concurrent.ConcurrentHashMap<String, Long>()

    private fun isLoopback(ip: String) = ip == "127.0.0.1" || ip == "::1" || ip.startsWith("127.")

    private fun isAuthorized(ip: String): Boolean {
        if (pairingCode == null || isLoopback(ip)) return true
        val until = authorized[ip] ?: return false
        return until == Long.MAX_VALUE || SystemClock.elapsedRealtime() < until
    }

    @Synchronized
    fun start(pairing: String? = null) {
        pairingCode = pairing
        _state.update { it.copy(pairingCode = pairing) }
        if (scope != null) return
        val s = CoroutineScope(
            SupervisorJob() + Dispatchers.IO + CoroutineExceptionHandler { _, e -> Log.e(TAG, "server coroutine failed", e) },
        )
        scope = s
        _state.value = ServerState(running = true, pairingCode = pairing)
        s.launch { acceptLoop(Protocol.VIDEO_PORT, ::onVideoClient) }
        s.launch { acceptLoop(Protocol.CONTROL_PORT) { sock -> s.launch { serveControl(sock) } } }
        Log.i(TAG, "stream server started on ${Protocol.VIDEO_PORT}/${Protocol.CONTROL_PORT}")
    }

    @Synchronized
    fun stop() {
        val s = scope ?: return
        scope = null
        serverSockets.forEach { runCatching { it.close() } }
        serverSockets.clear()
        video?.let { engine.removeSink(it) }
        video = null
        control?.let { runCatching { it.close() } }
        control = null
        s.cancel()
        _state.value = ServerState(running = false)
        Log.i(TAG, "stream server stopped")
    }

    private suspend fun acceptLoop(port: Int, onClient: (Socket) -> Unit) {
        while (currentScopeActive()) {
            try {
                ServerSocket().use { ss ->
                    ss.reuseAddress = true
                    ss.bind(InetSocketAddress(port))
                    synchronized(this) { serverSockets += ss }
                    _state.update { it.copy(error = null) }
                    while (true) {
                        val sock = ss.accept()
                        sock.tcpNoDelay = true
                        sock.keepAlive = true
                        Log.i(TAG, "client ${sock.inetAddress?.hostAddress} connected on port $port")
                        onClient(sock)
                    }
                }
            } catch (e: IOException) {
                if (!currentScopeActive()) return
                Log.w(TAG, "server on port $port failed; retrying", e)
                _state.update { it.copy(error = "Port $port: ${e.message}") }
                delay(1000)
            }
        }
    }

    private fun currentScopeActive() = scope?.isActive == true

    private fun onVideoClient(sock: Socket) {
        val ip = sock.inetAddress?.hostAddress ?: ""
        if (!isAuthorized(ip)) {
            Log.w(TAG, "video connection from $ip rejected: not paired")
            _state.update { it.copy(rejected = ip) }
            runCatching { sock.close() }
            return
        }
        // Keep the kernel send buffer small so congestion shows up in our drop queue instead of
        // as hidden latency in the socket.
        runCatching { sock.sendBufferSize = 128 * 1024 }
        val client = VideoClient(sock, requestKeyframe = { engine.requestKeyframe() }) { closed ->
            engine.removeSink(closed)
            if (video === closed) {
                video = null
                _state.update { it.copy(videoClient = null) }
            }
        }
        video?.let { engine.removeSink(it) } // replace the previous PC
        video = client
        _state.update { it.copy(videoClient = client.remote) }
        client.start()
        engine.addSink(client) // sends CONFIG and requests a keyframe
    }

    // ------------------------------------------------------------------ control channel

    private suspend fun serveControl(sock: Socket) {
        val remote = sock.inetAddress?.hostAddress ?: "?"
        val writer = PacketWriter(BufferedOutputStream(sock.getOutputStream(), 64 * 1024))
        var statsJob: Job? = null
        var stateJob: Job? = null
        var paired = false
        try {
            sock.soTimeout = 10_000 // an unpaired client must send HELLO promptly
            val reader = PacketReader(BufferedInputStream(sock.getInputStream()))
            while (true) {
                val p = reader.read() ?: break
                if (!paired && p.type != PacketType.HELLO) break // HELLO comes first
                when (p.type) {
                    PacketType.PING -> writer.write(Packet.pong(Packet.parsePing(p), nowUs()))
                    PacketType.HELLO -> {
                        if (paired) continue
                        Log.i(TAG, "HELLO from $remote")
                        val hello = runCatching { JSONObject(p.payload.decodeToString()) }.getOrDefault(JSONObject())
                        val code = pairingCode
                        if (code != null && !isLoopback(remote) && hello.optString("pairing") != code) {
                            val err = if (hello.has("pairing")) "pairing_failed" else "pairing_required"
                            Log.w(TAG, "HELLO from $remote rejected: $err")
                            _state.update { it.copy(rejected = remote) }
                            writer.write(Packet.json(PacketType.HELLO, JSONObject()
                                .put("server", "phonecam-android/${BuildConfig.VERSION_NAME}")
                                .put("version", Protocol.VERSION).put("error", err).toString()))
                            break
                        }
                        // Paired: this connection replaces the previous PC (only now, so an
                        // unpaired device can't kick the current one off).
                        paired = true
                        sock.soTimeout = 0
                        authorized[remote] = Long.MAX_VALUE
                        control?.let { runCatching { it.close() } }
                        control = sock
                        _state.update { it.copy(controlClient = remote, rejected = null) }
                        writer.write(Packet.json(PacketType.HELLO, helloJson().toString()))
                        writer.write(Packet.json(PacketType.STATE, stateJson(engine.state.value).toString()))
                        statsJob = scope?.launch {
                            try {
                                while (isActive) {
                                    delay(1000)
                                    writer.write(Packet.json(PacketType.STATS, statsJson(remote).toString()))
                                }
                            } catch (_: IOException) {
                                // socket gone; the read loop notices and cleans up
                            }
                        }
                        stateJob = scope?.launch {
                            // Push STATE whenever the published settings change (PC or phone UI).
                            engine.state.map { stateJson(it).toString() }.distinctUntilChanged().drop(1).collect { json ->
                                try {
                                    writer.write(Packet.json(PacketType.STATE, json))
                                } catch (_: IOException) {
                                }
                            }
                        }
                    }
                    PacketType.CONTROL_REQ -> writer.write(Packet.json(PacketType.CONTROL_RES, handleRequest(p).toString()))
                    else -> Unit // unknown or not for us: skipped
                }
            }
        } catch (e: SocketException) {
            Log.i(TAG, "control client $remote: ${e.message}")
        } catch (e: java.net.SocketTimeoutException) {
            Log.w(TAG, "control client $remote sent no HELLO")
        } catch (e: IOException) {
            Log.w(TAG, "control client $remote failed", e)
        } finally {
            statsJob?.cancel()
            stateJob?.cancel()
            runCatching { sock.close() }
            // Keep the IP authorized briefly so a video reconnect right after a control drop works.
            if (paired) authorized[remote] = SystemClock.elapsedRealtime() + 10_000
            if (control === sock) {
                control = null
                _state.update { it.copy(controlClient = null) }
            }
        }
    }

    /** CONTROL_REQ → CONTROL_RES. Validation errors are reported with ok=false. */
    private fun handleRequest(p: Packet): JSONObject {
        val req = JSONObject(p.payload.decodeToString())
        val args = req.optJSONObject("args") ?: JSONObject()
        val cmd = req.optString("cmd")
        val res = JSONObject().put("id", req.opt("id"))
        try {
            when (cmd) {
                "set_camera" -> if (args.has("id")) engine.setCamera(args.getString("id"))
                else engine.setFacing(Facing.entries.firstOrNull { it.wire == args.optString("facing") }
                    ?: throw IllegalArgumentException("set_camera needs id or facing"))
                "set_resolution" -> engine.setResolution(args.getInt("width"), args.getInt("height"))
                "set_fps" -> engine.setFps(args.getInt("fps"))
                "set_bitrate" -> engine.setBitrate(args.getInt("bps"))
                "set_torch" -> engine.setTorch(args.getBoolean("on"))
                "focus_at" -> engine.focusAt(args.getDouble("x").toFloat(), args.getDouble("y").toFloat())
                "set_af" -> engine.setAfMode(args.getString("mode"))
                "set_exposure_comp" -> engine.setExposureEv(args.getDouble("ev").toFloat())
                "set_orientation" -> engine.setOrientation(Orientation.fromWire(args.optString("orientation"))
                    ?: throw IllegalArgumentException("orientation must be landscape, portrait or auto"))
                "request_keyframe" -> engine.requestKeyframe()
                "set_tuning" -> engine.setTuning(args) // experimental, for latency measurements
                "set_audio" -> {
                    if (args.has("mic")) engine.setMicVoice(args.getString("mic") != "raw")
                    if (args.has("enabled")) engine.setAudio(args.getBoolean("enabled"))
                }
                "get_status" -> Unit
                else -> throw IllegalArgumentException("unknown command: $cmd")
            }
            res.put("ok", true)
        } catch (e: IllegalArgumentException) {
            res.put("ok", false).put("error", e.message)
        } catch (e: org.json.JSONException) {
            res.put("ok", false).put("error", "bad arguments for $cmd: ${e.message}")
        }
        Log.i(TAG, "control $cmd $args -> ${res.optBoolean("ok")} ${res.optString("error")}")
        return res.put("state", stateJson(engine.state.value))
    }

    private fun helloJson(): JSONObject {
        val st = engine.state.value
        return JSONObject()
            .put("server", "phonecam-android/${BuildConfig.VERSION_NAME}")
            .put("version", Protocol.VERSION)
            .put("device", JSONObject().put("model", Build.MODEL).put("android", Build.VERSION.RELEASE).put("sdk", Build.VERSION.SDK_INT))
            .put("cameras", JSONArray(st.cameras.map(::cameraJson)))
            .put("encoders", JSONArray(listOfNotNull(st.encoderName?.let { JSONObject().put("name", it).put("hardware", st.encoderHardware) })))
            .put("state", stateJson(st))
    }

    private fun cameraJson(c: CameraInfo) = JSONObject()
        .put("id", c.id)
        .put("facing", c.facing.wire)
        .put("sizes", JSONArray(c.sizes.map { JSONArray(listOf(it.width, it.height)) }))
        .put("fps_ranges", JSONArray(c.fpsRanges.map { JSONArray(listOf(it.lower, it.upper)) }))
        .put("has_flash", c.hasFlash)
        .put("af_modes", JSONArray(c.afModeNames + if ("auto" in c.afModeNames) listOf("locked") else emptyList()))
        .put("exposure_comp", JSONObject().put("min", c.aeCompRange.lower).put("max", c.aeCompRange.upper).put("step", c.aeCompStep.toDouble()))

    private fun stateJson(st: EngineState): JSONObject {
        val cfg = st.config
        return JSONObject()
            .put("streaming", st.streaming)
            .put("camera_id", cfg?.cameraId)
            .put("facing", st.camera?.facing?.wire)
            .put("width", cfg?.width).put("height", cfg?.height).put("fps", cfg?.fps).put("bitrate", cfg?.bitrate)
            .put("orientation", st.controls.orientation.wire)
            .put("rotation", st.rotation)
            .put("torch", st.controls.torch)
            .put("af_mode", st.controls.afMode)
            .put("exposure_comp", st.camera?.evForIndex(st.controls.aeCompIndex)?.toDouble() ?: 0.0)
            .put("encoder", st.encoderName)
            .put("audio", st.controls.audio)
            .put("mic", if (st.controls.micVoice) "voice" else "raw")
            .put("audio_codec", st.audioCodec)
            .put("audio_error", st.audioError)
            .put("audio_permission", engine.hasMicPermission())
            .put("bitrate_range", JSONArray(listOf(com.ares.phonecam.engine.MIN_BITRATE, com.ares.phonecam.engine.MAX_BITRATE)))
    }

    private fun statsJson(remote: String): JSONObject {
        val s = engine.state.value.stats
        val v = video
        val battery = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        return JSONObject()
            .put("fps", s.fps.toDouble())
            .put("bitrate", s.bitrateBps)
            .put("frames", s.frames)
            .put("pipeline_ms", s.pipelineMs.toDouble())
            .put("pipeline_max_ms", s.pipelineMaxMs.toDouble())
            .put("capture_ms", s.captureMs.toDouble())
            .put("exposure_ms", s.exposureMs.toDouble())
            .put("pipeline_depth", s.pipelineDepth)
            .put("encode_ms", (s.pipelineMs - s.captureMs).toDouble().coerceAtLeast(0.0))
            .put("tuning", engine.tuning.toJson())
            .put("dropped_total", v?.dropped ?: 0)
            .put("queue", v?.queueDepth ?: 0)
            .put("battery_pct", battery?.let { it.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) * 100 / it.getIntExtra(BatteryManager.EXTRA_SCALE, 100).coerceAtLeast(1) })
            .put("battery_temp_c", battery?.let { it.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, 0) / 10.0 })
            .put("thermal_status", if (Build.VERSION.SDK_INT >= 29) context.getSystemService(PowerManager::class.java).currentThermalStatus else JSONObject.NULL)
            .put("client", remote)
    }

    private fun nowUs() = SystemClock.elapsedRealtimeNanos() / 1000
}
