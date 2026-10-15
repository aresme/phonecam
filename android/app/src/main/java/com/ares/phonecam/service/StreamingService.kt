package com.ares.phonecam.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.ares.phonecam.MainActivity
import com.ares.phonecam.PhoneCamApp
import com.ares.phonecam.R
import com.ares.phonecam.TAG
import com.ares.phonecam.net.Discovery
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch

/**
 * Foreground service (type camera) that keeps the process and camera alive while streaming,
 * including with the screen off or the app in the background. The pipeline itself lives in
 * [com.ares.phonecam.engine.StreamEngine]; this service only owns lifecycle, notification and
 * wake lock.
 */
class StreamingService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var watcher: Job? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var pairingJob: Job? = null
    private val discovery by lazy { Discovery(this) }
    private val settings get() = (application as PhoneCamApp).settings
    private val engine get() = (application as PhoneCamApp).engine
    private val server get() = (application as PhoneCamApp).server

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_REFRESH) {
            // Microphone permission was just granted: add the microphone FGS type.
            if (wakeLock != null) ServiceCompat.startForeground(this, NOTIFICATION_ID, buildNotification(), foregroundType())
            return START_NOT_STICKY
        }
        if (intent?.action == ACTION_STOP) {
            Log.i(TAG, "stop requested")
            engine.stopStreaming()
            stopSelf()
            return START_NOT_STICKY
        }
        ServiceCompat.startForeground(this, NOTIFICATION_ID, buildNotification(), foregroundType())
        if (wakeLock == null) {
            wakeLock = getSystemService(PowerManager::class.java)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "PhoneCam:streaming")
                .apply { acquire() }
        }
        if (wifiLock == null) {
            // Keep Wi-Fi out of power-save while streaming (power save adds 100+ ms bursts).
            @Suppress("DEPRECATION")
            val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) WifiManager.WIFI_MODE_FULL_LOW_LATENCY
            else WifiManager.WIFI_MODE_FULL_HIGH_PERF
            wifiLock = applicationContext.getSystemService(WifiManager::class.java)
                .createWifiLock(mode, "PhoneCam:streaming").apply { setReferenceCounted(false); acquire() }
        }
        engine.startStreaming(record = intent?.getBooleanExtra(EXTRA_RECORD, false) ?: false)
        if (pairingJob == null) {
            // (Re)start the server whenever the pairing setting or code changes.
            pairingJob = scope.launch {
                settings.settings.map { it.requirePairing to it.pairingCode }.distinctUntilChanged().collect { (required, _) ->
                    val code = if (required) settings.pairingCode() else null
                    server.start(code)
                    discovery.unregister()
                    discovery.register(pairingRequired = code != null)
                }
            }
        }
        if (watcher == null) {
            // Stop ourselves when streaming ends for any reason (UI, error, give-up).
            watcher = scope.launch {
                engine.state.first { it.streaming }
                engine.state.first { !it.streaming }
                Log.i(TAG, "streaming ended; stopping service")
                stopSelf()
            }
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        scope.cancel()
        discovery.unregister()
        server.stop()
        wifiLock?.let { if (it.isHeld) it.release() }
        wifiLock = null
        engine.stopStreaming()
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
        super.onDestroy()
    }

    private fun foregroundType(): Int {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) return 0
        var type = ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
        if (engine.hasMicPermission()) type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
        return type
    }

    private fun buildNotification(): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "Streaming", NotificationManager.IMPORTANCE_LOW)
                    .apply { description = "Shown while the camera is streaming" },
            )
        }
        val pendingFlags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), pendingFlags)
        val stop = PendingIntent.getService(this, 1, Intent(this, StreamingService::class.java).setAction(ACTION_STOP), pendingFlags)
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_phonecam)
            .setContentTitle("PhoneCam is streaming")
            .setContentText("Camera is streaming to your computer")
            .setContentIntent(open)
            .addAction(0, "Stop", stop)
            .setOngoing(true)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    companion object {
        private const val CHANNEL_ID = "streaming"
        private const val NOTIFICATION_ID = 1
        private const val ACTION_STOP = "com.ares.phonecam.STOP"
        private const val ACTION_REFRESH = "com.ares.phonecam.REFRESH"
        private const val EXTRA_RECORD = "record"

        fun start(context: Context, record: Boolean) {
            ContextCompat.startForegroundService(
                context, Intent(context, StreamingService::class.java).putExtra(EXTRA_RECORD, record),
            )
        }

        /** Re-declare the foreground types (e.g. after the microphone permission was granted). */
        fun refresh(context: Context) {
            context.startService(Intent(context, StreamingService::class.java).setAction(ACTION_REFRESH))
        }

        fun stop(context: Context) {
            context.startService(Intent(context, StreamingService::class.java).setAction(ACTION_STOP))
        }
    }
}
