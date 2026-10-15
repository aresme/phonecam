package com.ares.phonecam.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExposedDropdownMenuBox
import androidx.compose.material3.ExposedDropdownMenuDefaults
import androidx.compose.material3.ExposedDropdownMenuAnchorType
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.ares.phonecam.engine.EngineState
import com.ares.phonecam.engine.MAX_BITRATE
import com.ares.phonecam.engine.Orientation
import com.ares.phonecam.engine.StreamEngine

/** Bitrate slider range (the protocol accepts a wider range from the PC). */
private const val UI_MIN_MBPS = 0.5f
private const val UI_MAX_MBPS = 20f

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun SettingsSheet(
    engine: StreamEngine,
    state: EngineState,
    statsOn: Boolean,
    onStatsChange: (Boolean) -> Unit,
    keepScreenOn: Boolean,
    onKeepScreenOnChange: (Boolean) -> Unit,
    record: Boolean,
    onRecordChange: (Boolean) -> Unit,
    requirePairing: Boolean,
    onRequirePairingChange: (Boolean) -> Unit,
    pairingCode: String?,
    onNewPairingCode: () -> Unit,
    onDismiss: () -> Unit,
) {
    val cfg = state.config ?: return
    val cam = state.camera ?: return
    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(
            Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(horizontal = 20.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            Text("Stream settings", style = MaterialTheme.typography.titleLarge)

            // Resolution
            var expanded by remember { mutableStateOf(false) }
            ExposedDropdownMenuBox(expanded = expanded, onExpandedChange = { expanded = it }) {
                OutlinedTextField(
                    value = "${cfg.width}×${cfg.height}",
                    onValueChange = {},
                    readOnly = true,
                    label = { Text("Resolution") },
                    trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded) },
                    modifier = Modifier.fillMaxWidth().menuAnchor(ExposedDropdownMenuAnchorType.PrimaryNotEditable),
                )
                ExposedDropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
                    cam.sizes.forEach { s ->
                        DropdownMenuItem(
                            text = { Text("${s.width}×${s.height}") },
                            onClick = {
                                expanded = false
                                runCatching { engine.setResolution(s.width, s.height) }
                            },
                        )
                    }
                }
            }

            Label("Frame rate")
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                cam.supportedFps.forEach { fps ->
                    FilterChip(selected = fps == cfg.fps, onClick = { runCatching { engine.setFps(fps) } }, label = { Text("$fps fps") })
                }
            }

            var mbps by remember(cfg.bitrate) { mutableFloatStateOf(cfg.bitrate / 1e6f) }
            Label("Bitrate: %.1f Mbit/s".format(mbps))
            Slider(
                value = mbps.coerceIn(UI_MIN_MBPS, UI_MAX_MBPS),
                onValueChange = { mbps = it },
                onValueChangeFinished = { runCatching { engine.setBitrate((mbps * 1e6f).toInt().coerceAtMost(MAX_BITRATE)) } },
                valueRange = UI_MIN_MBPS..UI_MAX_MBPS,
            )

            Label("Orientation of the streamed video")
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Orientation.entries.forEach { o ->
                    FilterChip(
                        selected = state.controls.orientation == o,
                        onClick = { engine.setOrientation(o) },
                        label = { Text(o.wire.replaceFirstChar { it.uppercase() }) },
                    )
                }
            }
            Text(
                "Landscape assumes the phone is held with its top to the left. Auto follows how the phone is held.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            Label("Microphone (when enabled)")
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                FilterChip(selected = state.controls.micVoice, onClick = { engine.setMicVoice(true) }, label = { Text("Voice (noise suppression)") })
                FilterChip(selected = !state.controls.micVoice, onClick = { engine.setMicVoice(false) }, label = { Text("Raw") })
            }

            HorizontalDivider()
            SwitchRow("Show stats overlay", statsOn, onStatsChange)
            SwitchRow("Keep screen on while streaming", keepScreenOn, onKeepScreenOnChange)
            SwitchRow("Also record to file", record, onRecordChange, enabled = !state.streaming)
            HorizontalDivider()
            SwitchRow("Require pairing code on Wi-Fi", requirePairing, onRequirePairingChange)
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    if (requirePairing) "Code: ${pairingCode ?: "…"}" else "Wi-Fi connections are not protected",
                    Modifier.weight(1f),
                    style = MaterialTheme.typography.bodyMedium,
                )
                TextButton(onClick = onNewPairingCode, enabled = requirePairing) { Text("New code") }
            }
            Text(
                "USB connections never need the code. The video itself is not encrypted: use trusted networks.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            HorizontalDivider()
            Text(
                "Encoder: ${state.encoderName ?: "starts with the stream"}${if (state.encoderName != null) if (state.encoderHardware) " (hardware)" else " (software)" else ""}",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Text(
                "Camera ${cam.id} (${cam.facing.wire}) · sensor orientation ${cam.sensorOrientation}°",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            state.lastRecording?.let {
                Text("Last recording: ${it.absolutePath}", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

@Composable
private fun Label(text: String) = Text(text, style = MaterialTheme.typography.labelLarge)

@Composable
private fun SwitchRow(label: String, checked: Boolean, onChange: (Boolean) -> Unit, enabled: Boolean = true) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(label, Modifier.weight(1f), color = if (enabled) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.onSurfaceVariant)
        Switch(checked = checked, onCheckedChange = onChange, enabled = enabled)
    }
}
