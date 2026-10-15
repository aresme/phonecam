package com.ares.phonecam.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.AssistChip
import androidx.compose.material3.AssistChipDefaults
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.FilledIconToggleButton
import androidx.compose.material3.FilledTonalIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.ares.phonecam.R
import com.ares.phonecam.camera.Facing
import com.ares.phonecam.camera.Geometry
import com.ares.phonecam.engine.EngineState
import com.ares.phonecam.engine.SettingsRepository
import com.ares.phonecam.engine.StreamEngine
import com.ares.phonecam.net.LocalAddress
import com.ares.phonecam.net.NetworkAddresses
import com.ares.phonecam.net.ServerState
import com.ares.phonecam.net.StreamServer
import com.ares.phonecam.service.StreamingService
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

@Composable
fun MainScreen(engine: StreamEngine, server: StreamServer, settings: SettingsRepository) {
    val state by engine.state.collectAsStateWithLifecycle()
    val net by server.state.collectAsStateWithLifecycle()
    val saved by settings.settings.collectAsStateWithLifecycle(initialValue = null)
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var previewOn by rememberSaveable { mutableStateOf(true) }
    var statsOn by rememberSaveable { mutableStateOf(false) }
    var record by rememberSaveable { mutableStateOf(false) }
    var showSettings by rememberSaveable { mutableStateOf(false) }
    val cfg = state.config
    val cam = state.camera
    val keepScreenOn = saved?.keepScreenOn == true

    // Keep the screen on while streaming, if enabled.
    LocalView.current.keepScreenOn = keepScreenOn && state.streaming

    Column(
        Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background).safeDrawingPadding().padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("PhoneCam", style = MaterialTheme.typography.headlineSmall)
            Spacer(Modifier.weight(1f))
            StatusChip(state, net)
            IconButton(onClick = { showSettings = true }, enabled = cfg != null) {
                Icon(Icons.Default.Settings, contentDescription = "Settings")
            }
        }

        // Portrait-locked UI: the sensor image shows rotated upright, so aspect = h / w.
        val aspect = if (cfg != null) cfg.height.toFloat() / cfg.width else 9f / 16f
        Box(Modifier.fillMaxWidth().weight(1f), contentAlignment = Alignment.Center) {
            BoxWithConstraints(
                Modifier.aspectRatio(aspect, matchHeightConstraintsFirst = true)
                    .clip(RoundedCornerShape(12.dp)).background(Color.Black),
                contentAlignment = Alignment.Center,
            ) {
                val ring = remember { Animatable(0f) }
                var ringAt by remember { mutableStateOf<Offset?>(null) }
                if (previewOn && cfg != null && cam != null) {
                    val front = cam.facing == Facing.FRONT
                    CameraPreview(engine, mirror = front, modifier = Modifier.fillMaxSize())
                    // Tap to focus: view point -> sensor-oriented frame (the preview shows the
                    // sensor image rotated by sensorOrientation, mirrored for front cameras).
                    Box(
                        Modifier.fillMaxSize().pointerInput(cam.id) {
                            detectTapGestures { pos ->
                                val (sx, sy) = Geometry.toSensor(
                                    pos.x / size.width, pos.y / size.height, cam.sensorOrientation, mirrored = front,
                                )
                                runCatching { engine.focusAtSensor(sx, sy) }
                                ringAt = pos
                                scope.launch {
                                    ring.snapTo(1f)
                                    ring.animateTo(0f, tween(900))
                                }
                            }
                        },
                    )
                    ringAt?.let { at ->
                        Canvas(Modifier.fillMaxSize()) {
                            if (ring.value > 0f) {
                                drawCircle(
                                    Color.White.copy(alpha = ring.value), radius = 36.dp.toPx() * (1.3f - 0.3f * ring.value),
                                    center = at, style = Stroke(2.dp.toPx()),
                                )
                            }
                        }
                    }
                } else {
                    Text("Preview off", color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                if (statsOn) StatsOverlay(state, net, Modifier.align(Alignment.TopStart).padding(8.dp))
            }
        }

        val micPermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) {
                runCatching { engine.setAudio(true) }
                if (state.streaming) StreamingService.refresh(context)
            }
        }
        if (cam != null && cfg != null) {
            QuickControls(engine, state, previewOn, onPreview = { previewOn = it }, onMic = { on ->
                if (on && !engine.hasMicPermission()) micPermission.launch(android.Manifest.permission.RECORD_AUDIO)
                else runCatching { engine.setAudio(on) }
            })
        }

        if (cfg != null) {
            val enc = state.encoderName?.let { " · $it${if (state.encoderHardware) " (HW)" else " (SW)"}" } ?: ""
            Text(
                "${cfg.width}×${cfg.height} · ${cfg.fps} fps · %.1f Mbit/s · %s%s".format(
                    cfg.bitrate / 1e6, state.controls.orientation.wire, enc,
                ),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        val pairing = if (saved?.requirePairing != false) saved?.pairingCode else null
        WifiInfo(addresses = rememberAddresses(), pairing = pairing, requirePairing = saved?.requirePairing != false, net = net)
        LaunchedEffect(saved?.pairingCode == null && saved != null) {
            if (saved != null && saved?.pairingCode == null) settings.pairingCode() // create one
        }

        state.recordingFile?.let {
            Text("Recording: ${it.name}", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        state.error?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
        state.audioError?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }

        Button(
            onClick = {
                if (state.streaming) StreamingService.stop(context) else StreamingService.start(context, record = record)
            },
            enabled = cfg != null,
            colors = if (state.streaming) ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.error)
            else ButtonDefaults.buttonColors(),
            modifier = Modifier.fillMaxWidth().height(56.dp),
        ) {
            Text(if (state.streaming) "Stop" else "Start", style = MaterialTheme.typography.titleMedium)
        }
    }

    if (showSettings) {
        SettingsSheet(
            engine = engine,
            state = state,
            statsOn = statsOn,
            onStatsChange = { statsOn = it },
            keepScreenOn = keepScreenOn,
            onKeepScreenOnChange = { on -> scope.launch { settings.setKeepScreenOn(on) } },
            record = record,
            onRecordChange = { record = it },
            requirePairing = saved?.requirePairing != false,
            onRequirePairingChange = { on -> scope.launch { settings.setRequirePairing(on) } },
            pairingCode = saved?.pairingCode,
            onNewPairingCode = { scope.launch { settings.newPairingCode() } },
            onDismiss = { showSettings = false },
        )
    }
}

/** Local addresses, refreshed every few seconds (Wi-Fi changes, hotspot on/off). */
@Composable
private fun rememberAddresses(): List<LocalAddress> {
    var list by remember { mutableStateOf(emptyList<LocalAddress>()) }
    LaunchedEffect(Unit) {
        while (true) {
            list = withContext(Dispatchers.IO) { NetworkAddresses.list() }
            delay(3000)
        }
    }
    return list
}

@Composable
private fun WifiInfo(addresses: List<LocalAddress>, pairing: String?, requirePairing: Boolean, net: ServerState) {
    Column(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp)).background(MaterialTheme.colorScheme.surfaceVariant).padding(10.dp),
    ) {
        if (addresses.isEmpty()) {
            Text("Wi-Fi: not connected (USB works without it)", style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        } else {
            addresses.take(2).forEach { a ->
                Text("${a.kind}: ${a.ip}", style = MaterialTheme.typography.titleMedium, fontFamily = FontFamily.Monospace)
            }
            Text(
                if (requirePairing) "Pairing code: ${pairing ?: "…"}   ·   ports 47100/47101"
                else "No pairing code (anyone on this network can connect)   ·   ports 47100/47101",
                style = MaterialTheme.typography.bodySmall,
                color = if (requirePairing) MaterialTheme.colorScheme.onSurfaceVariant else MaterialTheme.colorScheme.error,
            )
        }
        net.rejected?.let {
            Text("Rejected a connection from $it (wrong or missing code)", style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error)
        }
    }
}

@Composable
private fun QuickControls(
    engine: StreamEngine,
    state: EngineState,
    previewOn: Boolean,
    onPreview: (Boolean) -> Unit,
    onMic: (Boolean) -> Unit,
) {
    val cam = state.camera ?: return
    val c = state.controls
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        FilledTonalIconButton(onClick = { engine.switchCamera() }, enabled = state.cameras.size > 1) {
            Icon(Icons.Default.Refresh, contentDescription = "Switch camera")
        }
        FilledIconToggleButton(
            checked = c.torch,
            onCheckedChange = { runCatching { engine.setTorch(it) } },
            enabled = cam.hasFlash,
        ) {
            Icon(painterResource(R.drawable.ic_flash), contentDescription = "Torch")
        }
        FilledIconToggleButton(checked = c.audio, onCheckedChange = onMic) {
            Icon(painterResource(R.drawable.ic_mic), contentDescription = "Microphone")
        }
        if ("auto" in cam.afModeNames) {
            val locked = c.afMode == "locked"
            AssistChip(
                onClick = { runCatching { engine.setAfMode(if (locked) "continuous-video" else "locked") } },
                label = { Text(if (locked) "AF locked" else "AF auto") },
                leadingIcon = if (locked) ({ Icon(Icons.Default.Lock, null, Modifier.width(16.dp)) }) else null,
            )
        }
        Spacer(Modifier.weight(1f))
        Text("Preview", style = MaterialTheme.typography.bodySmall)
        Switch(checked = previewOn, onCheckedChange = onPreview)
    }
    val range = cam.aeCompRange
    if (range.upper > range.lower) {
        var idx by remember(c.aeCompIndex, cam.id) { mutableFloatStateOf(c.aeCompIndex.toFloat()) }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("EV %+.1f".format(cam.evForIndex(idx.toInt())), Modifier.width(64.dp), style = MaterialTheme.typography.bodySmall)
            Slider(
                value = idx,
                onValueChange = { idx = it },
                onValueChangeFinished = { runCatching { engine.setExposureEv(cam.evForIndex(idx.toInt())) } },
                valueRange = range.lower.toFloat()..range.upper.toFloat(),
                modifier = Modifier.weight(1f),
            )
        }
    }
}

@Composable
private fun StatusChip(state: EngineState, net: ServerState) {
    val (label, color) = when {
        state.error != null -> "Error" to MaterialTheme.colorScheme.error
        state.streaming && net.connected -> "Connected via ${net.transport?.label}" to Color(0xFF4CAF50)
        state.streaming -> "Waiting for PC" to Color(0xFFFFB300)
        else -> "Idle" to MaterialTheme.colorScheme.onSurfaceVariant
    }
    AssistChip(onClick = {}, label = { Text(label) }, colors = AssistChipDefaults.assistChipColors(labelColor = color))
}

@Composable
private fun StatsOverlay(state: EngineState, net: ServerState, modifier: Modifier) {
    val s = state.stats
    val text = if (state.streaming) {
        "%.1f fps\n%.2f Mbit/s\nframes %d\npipeline %.0f ms (max %.0f)\nrotation %d°\nPC %s".format(
            s.fps, s.bitrateBps / 1e6, s.frames, s.pipelineMs, s.pipelineMaxMs, state.rotation, net.videoClient ?: "—",
        )
    } else {
        "not streaming"
    }
    Text(
        text,
        modifier.clip(RoundedCornerShape(6.dp)).background(Color(0x99000000)).padding(6.dp),
        color = Color.White,
        fontFamily = FontFamily.Monospace,
        fontSize = 11.sp,
        lineHeight = 14.sp,
    )
}
