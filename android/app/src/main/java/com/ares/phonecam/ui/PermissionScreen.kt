package com.ares.phonecam.ui

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

val requiredPermissions: Array<String> =
    if (Build.VERSION.SDK_INT >= 33) arrayOf(Manifest.permission.CAMERA, Manifest.permission.POST_NOTIFICATIONS)
    else arrayOf(Manifest.permission.CAMERA)

fun hasCameraPermission(context: Context) =
    ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED

/** Explains why the camera (and notifications) are needed before asking. */
@Composable
fun PermissionScreen(onGranted: () -> Unit) {
    val context = LocalContext.current
    var askedOnce by remember { mutableStateOf(false) }
    var permanentlyDenied by remember { mutableStateOf(false) }
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { result ->
        askedOnce = true
        if (result[Manifest.permission.CAMERA] == true) {
            onGranted()
        } else {
            val activity = context as? Activity
            permanentlyDenied = activity != null &&
                !ActivityCompat.shouldShowRequestPermissionRationale(activity, Manifest.permission.CAMERA)
        }
    }
    Column(
        Modifier.fillMaxSize().safeDrawingPadding().padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp, androidx.compose.ui.Alignment.CenterVertically),
    ) {
        Text("Camera access", style = MaterialTheme.typography.headlineMedium)
        Text(
            "PhoneCam turns this phone into a webcam for your computer. It needs the camera to " +
                "capture video, which is sent only to the computer you connect over USB or your " +
                "local Wi-Fi.",
            style = MaterialTheme.typography.bodyLarge,
        )
        if (Build.VERSION.SDK_INT >= 33) {
            Text(
                "Notifications are used for the \"streaming\" notification, which also lets you " +
                    "stop the stream when the app is in the background. They're optional.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        if (askedOnce && !permanentlyDenied) {
            Text("Camera permission was denied. PhoneCam can't work without it.", color = MaterialTheme.colorScheme.error)
        }
        if (permanentlyDenied) {
            Text("Camera permission is blocked. Allow it in the app settings.", color = MaterialTheme.colorScheme.error)
            OutlinedButton(
                onClick = {
                    context.startActivity(
                        Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", context.packageName, null)),
                    )
                },
                modifier = Modifier.fillMaxWidth(),
            ) { Text("Open app settings") }
        } else {
            Button(onClick = { launcher.launch(requiredPermissions) }, modifier = Modifier.fillMaxWidth()) {
                Text("Allow camera")
            }
        }
    }
}
