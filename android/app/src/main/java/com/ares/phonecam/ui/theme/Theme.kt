package com.ares.phonecam.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val Colors = darkColorScheme(
    primary = Color(0xFF2F6FED),
    onPrimary = Color.White,
    secondary = Color(0xFF9AA4B2),
    background = Color(0xFF101215),
    onBackground = Color(0xFFE6E6E6),
    surface = Color(0xFF16181C),
    surfaceVariant = Color(0xFF1F2227),
    onSurface = Color(0xFFE6E6E6),
    onSurfaceVariant = Color(0xFF9AA4B2),
    error = Color(0xFFEF5350),
)

/** Dark-only by design: the app is mostly a camera viewfinder. */
@Composable
fun PhoneCamTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = Colors) {
        // Surface provides the default content (text/icon) color for everything below it.
        Surface(color = Colors.background, contentColor = Colors.onBackground, content = content)
    }
}
