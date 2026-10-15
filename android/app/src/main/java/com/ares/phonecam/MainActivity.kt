package com.ares.phonecam

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import com.ares.phonecam.ui.MainScreen
import com.ares.phonecam.ui.PermissionScreen
import com.ares.phonecam.ui.hasCameraPermission
import com.ares.phonecam.ui.theme.PhoneCamTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val engine = (application as PhoneCamApp).engine
        setContent {
            PhoneCamTheme {
                val context = LocalContext.current
                var granted by remember { mutableStateOf(hasCameraPermission(context)) }
                if (granted) MainScreen(engine) else PermissionScreen(onGranted = { granted = true })
            }
        }
    }
}
