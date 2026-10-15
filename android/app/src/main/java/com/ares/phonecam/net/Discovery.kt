package com.ares.phonecam.net

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.util.Log
import com.ares.phonecam.BuildConfig
import com.ares.phonecam.TAG
import com.ares.phonecam.protocol.Protocol

/** Announces the control port as `_phonecam._tcp` (mDNS/DNS-SD) so the PC can list this phone. */
class Discovery(context: Context) {
    private val nsd = context.applicationContext.getSystemService(NsdManager::class.java)
    private var listener: NsdManager.RegistrationListener? = null

    @Synchronized
    fun register(pairingRequired: Boolean) {
        if (listener != null) return
        val info = NsdServiceInfo().apply {
            serviceName = "PhoneCam ${Build.MODEL}"
            serviceType = SERVICE_TYPE
            port = Protocol.CONTROL_PORT
            setAttribute("model", Build.MODEL)
            setAttribute("video", Protocol.VIDEO_PORT.toString())
            setAttribute("version", Protocol.VERSION.toString())
            setAttribute("app", BuildConfig.VERSION_NAME)
            setAttribute("pairing", if (pairingRequired) "1" else "0")
        }
        val l = object : NsdManager.RegistrationListener {
            override fun onServiceRegistered(s: NsdServiceInfo) {
                Log.i(TAG, "NSD registered as '${s.serviceName}'")
            }
            override fun onRegistrationFailed(s: NsdServiceInfo, code: Int) {
                Log.w(TAG, "NSD registration failed: $code")
            }
            override fun onServiceUnregistered(s: NsdServiceInfo) {
                Log.i(TAG, "NSD unregistered")
            }
            override fun onUnregistrationFailed(s: NsdServiceInfo, code: Int) {
                Log.w(TAG, "NSD unregistration failed: $code")
            }
        }
        listener = l
        try {
            nsd.registerService(info, NsdManager.PROTOCOL_DNS_SD, l)
        } catch (e: Exception) {
            Log.w(TAG, "NSD register failed", e)
            listener = null
        }
    }

    @Synchronized
    fun unregister() {
        val l = listener ?: return
        listener = null
        runCatching { nsd.unregisterService(l) }
    }

    companion object {
        const val SERVICE_TYPE = "_phonecam._tcp"
    }
}
