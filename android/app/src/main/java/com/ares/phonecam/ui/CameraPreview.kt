package com.ares.phonecam.ui

import android.graphics.SurfaceTexture
import android.view.TextureView
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.viewinterop.AndroidView
import com.ares.phonecam.engine.StreamEngine

/**
 * TextureView fed by the engine. In a portrait-locked activity the camera's SurfaceTexture
 * transform already rotates the sensor image upright, so the view only needs the right aspect
 * ratio (set by the caller) and a horizontal flip for front cameras.
 */
@Composable
fun CameraPreview(engine: StreamEngine, mirror: Boolean, modifier: Modifier = Modifier) {
    AndroidView(
        modifier = modifier,
        factory = { ctx ->
            TextureView(ctx).apply {
                surfaceTextureListener = object : TextureView.SurfaceTextureListener {
                    override fun onSurfaceTextureAvailable(st: SurfaceTexture, w: Int, h: Int) = engine.attachPreview(st)
                    override fun onSurfaceTextureSizeChanged(st: SurfaceTexture, w: Int, h: Int) = Unit
                    override fun onSurfaceTextureUpdated(st: SurfaceTexture) = Unit
                    override fun onSurfaceTextureDestroyed(st: SurfaceTexture): Boolean {
                        // Keep the texture alive until the camera has stopped drawing into it.
                        engine.detachPreview(st) { st.release() }
                        return false
                    }
                }
            }
        },
        update = { it.scaleX = if (mirror) -1f else 1f },
    )
}
