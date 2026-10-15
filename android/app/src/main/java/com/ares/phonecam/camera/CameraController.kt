package com.ares.phonecam.camera

import android.annotation.SuppressLint
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.hardware.camera2.params.OutputConfiguration
import android.hardware.camera2.params.SessionConfiguration
import android.os.Build
import android.os.Handler
import android.util.Log
import android.util.Range
import android.view.Surface
import com.ares.phonecam.TAG
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import java.io.IOException
import java.util.concurrent.Executor
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/** What the repeating request should do. Recomputed by the engine whenever settings change. */
data class CaptureParams(
    val fpsRange: Range<Int>,
    val afMode: Int,
    val torch: Boolean = false,
    val aeCompensation: Int = 0,
    val recording: Boolean = true,
)

/**
 * Thin coroutine wrapper over Camera2. Every method must be called on the thread of [handler]
 * (the engine's camera thread); Camera2 callbacks are delivered there too.
 */
class CameraController(
    private val manager: CameraManager,
    private val handler: Handler,
    /** Called (on the camera thread) when an open device is lost or fails. */
    private val onDeviceLost: (cameraId: String, reason: String) -> Unit,
) {
    private val executor = Executor { handler.post(it) }

    var device: CameraDevice? = null
        private set
    private var session: CameraCaptureSession? = null
    private var sessionClosed: CompletableDeferred<Unit>? = null
    var sessionSurfaces: List<Surface> = emptyList()
        private set

    val cameraId: String? get() = device?.id

    @SuppressLint("MissingPermission") // checked by the UI before streaming can start
    suspend fun open(id: String): CameraDevice = suspendCancellableCoroutine { cont ->
        manager.openCamera(id, object : CameraDevice.StateCallback() {
            override fun onOpened(camera: CameraDevice) {
                Log.i(TAG, "camera $id opened")
                if (cont.isActive) {
                    device = camera
                    cont.resume(camera)
                } else {
                    camera.close()
                }
            }

            override fun onDisconnected(camera: CameraDevice) = fail(camera, "disconnected (in use by another app?)")

            override fun onError(camera: CameraDevice, error: Int) = fail(camera, "error $error")

            private fun fail(camera: CameraDevice, reason: String) {
                Log.w(TAG, "camera $id $reason")
                camera.close()
                if (cont.isActive) {
                    cont.resumeWithException(IOException("camera $id $reason"))
                } else if (device === camera) {
                    device = null
                    session = null
                    sessionSurfaces = emptyList()
                    onDeviceLost(id, reason)
                }
            }
        }, handler)
    }

    suspend fun startSession(surfaces: List<Surface>, params: CaptureParams) {
        val dev = device ?: throw IllegalStateException("camera not open")
        closeSession()
        val closed = CompletableDeferred<Unit>()
        val s = suspendCancellableCoroutine { cont ->
            val cb = object : CameraCaptureSession.StateCallback() {
                override fun onClosed(s: CameraCaptureSession) {
                    closed.complete(Unit)
                }

                override fun onConfigured(s: CameraCaptureSession) {
                    if (cont.isActive) cont.resume(s) else s.close()
                }

                override fun onConfigureFailed(s: CameraCaptureSession) {
                    if (cont.isActive) cont.resumeWithException(IOException("capture session configuration failed"))
                }
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                val outputs = surfaces.map { OutputConfiguration(it) }
                dev.createCaptureSession(SessionConfiguration(SessionConfiguration.SESSION_REGULAR, outputs, executor, cb))
            } else {
                @Suppress("DEPRECATION")
                dev.createCaptureSession(surfaces, cb, handler)
            }
        }
        session = s
        sessionClosed = closed
        sessionSurfaces = surfaces
        applyParams(params)
        Log.i(TAG, "capture session started with ${surfaces.size} surface(s), $params")
    }

    fun applyParams(params: CaptureParams) {
        val dev = device ?: return
        val s = session ?: return
        val template = if (params.recording) CameraDevice.TEMPLATE_RECORD else CameraDevice.TEMPLATE_PREVIEW
        val req = dev.createCaptureRequest(template).apply {
            sessionSurfaces.forEach { addTarget(it) }
            set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO)
            set(CaptureRequest.CONTROL_AE_MODE, CaptureRequest.CONTROL_AE_MODE_ON)
            set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, params.fpsRange)
            set(CaptureRequest.CONTROL_AE_EXPOSURE_COMPENSATION, params.aeCompensation)
            set(CaptureRequest.CONTROL_AF_MODE, params.afMode)
            set(CaptureRequest.FLASH_MODE, if (params.torch) CaptureRequest.FLASH_MODE_TORCH else CaptureRequest.FLASH_MODE_OFF)
        }.build()
        try {
            s.setRepeatingRequest(req, null, handler)
        } catch (e: Exception) {
            Log.w(TAG, "setRepeatingRequest failed", e)
        }
    }

    /** Close the session and wait (bounded) until the camera stops using its Surfaces. */
    suspend fun closeSession() {
        val s = session ?: return
        try {
            s.stopRepeating()
        } catch (_: Exception) {
        }
        s.close()
        val closed = sessionClosed
        session = null
        sessionClosed = null
        sessionSurfaces = emptyList()
        if (closed != null && withTimeoutOrNull(1000) { closed.await() } == null) Log.w(TAG, "session close timed out")
    }

    suspend fun close() {
        closeSession()
        device?.close()
        device?.let { Log.i(TAG, "camera ${it.id} closed") }
        device = null
    }
}
