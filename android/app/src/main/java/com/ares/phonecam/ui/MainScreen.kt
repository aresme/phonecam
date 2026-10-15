package com.ares.phonecam.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
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
import androidx.compose.material3.AssistChip
import androidx.compose.material3.AssistChipDefaults
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.ares.phonecam.camera.Facing
import com.ares.phonecam.engine.EngineState
import com.ares.phonecam.engine.StreamEngine
import com.ares.phonecam.net.ServerState
import com.ares.phonecam.net.StreamServer
import com.ares.phonecam.service.StreamingService

@Composable
fun MainScreen(engine: StreamEngine, server: StreamServer) {
    val state by engine.state.collectAsStateWithLifecycle()
    val net by server.state.collectAsStateWithLifecycle()
    var record by rememberSaveable { mutableStateOf(false) }
    val context = LocalContext.current
    var previewOn by rememberSaveable { mutableStateOf(true) }
    var statsOn by rememberSaveable { mutableStateOf(true) }
    val cfg = state.config

    Column(
        Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background).safeDrawingPadding().padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("PhoneCam", style = MaterialTheme.typography.headlineSmall)
            Spacer(Modifier.weight(1f))
            StatusChip(state, net)
        }

        // Portrait-locked UI: the landscape stream shows rotated upright, so aspect = h / w.
        val aspect = if (cfg != null) cfg.height.toFloat() / cfg.width else 9f / 16f
        Box(
            Modifier.fillMaxWidth().weight(1f),
            contentAlignment = Alignment.Center,
        ) {
            Box(
                Modifier.aspectRatio(aspect, matchHeightConstraintsFirst = true)
                    .clip(RoundedCornerShape(12.dp)).background(Color.Black),
                contentAlignment = Alignment.Center,
            ) {
                if (previewOn && cfg != null) {
                    CameraPreview(engine, mirror = state.camera?.facing == Facing.FRONT, modifier = Modifier.fillMaxSize())
                } else {
                    Text("Preview off", color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                if (statsOn) StatsOverlay(state, net, Modifier.align(Alignment.TopStart).padding(8.dp))
            }
        }

        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Preview", Modifier.weight(1f))
            Switch(checked = previewOn, onCheckedChange = { previewOn = it })
            Spacer(Modifier.width(16.dp))
            Text("Stats")
            Spacer(Modifier.width(8.dp))
            Switch(checked = statsOn, onCheckedChange = { statsOn = it })
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Also record to file", Modifier.weight(1f))
            Switch(checked = record, enabled = !state.streaming, onCheckedChange = { record = it })
        }

        if (cfg != null) {
            val enc = state.encoderName?.let { " · $it${if (state.encoderHardware) " (HW)" else " (SW)"}" } ?: ""
            Text(
                "${cfg.width}×${cfg.height} · ${cfg.fps} fps · ${cfg.bitrate / 1_000_000.0} Mbit/s$enc",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        (state.recordingFile ?: state.lastRecording)?.let {
            Text(
                (if (state.recordingFile != null) "Recording: " else "Last recording: ") + it.absolutePath,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        state.error?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }

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
        "%.1f fps\n%.2f Mbit/s\nframes %d\npipeline %.0f ms (max %.0f)\nPC %s".format(
            s.fps, s.bitrateBps / 1e6, s.frames, s.pipelineMs, s.pipelineMaxMs, net.videoClient ?: "—",
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
