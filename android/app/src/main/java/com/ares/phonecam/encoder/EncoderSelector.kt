package com.ares.phonecam.encoder

import android.media.MediaCodecInfo
import android.media.MediaCodecList
import android.media.MediaFormat
import android.os.Build
import android.util.Log
import com.ares.phonecam.TAG

data class EncoderCandidate(val info: MediaCodecInfo, val hardware: Boolean) {
    val name: String get() = info.name
    val caps: MediaCodecInfo.CodecCapabilities get() = info.getCapabilitiesForType(MediaFormat.MIMETYPE_VIDEO_AVC)

    fun supports(width: Int, height: Int, fps: Int): Boolean = try {
        caps.videoCapabilities?.areSizeAndRateSupported(width, height, fps.toDouble()) == true
    } catch (_: Exception) {
        false
    }
}

object EncoderSelector {
    /** All H.264 encoders, hardware first. Software ones are kept only as a last resort. */
    fun all(): List<EncoderCandidate> {
        val list = MediaCodecList(MediaCodecList.REGULAR_CODECS).codecInfos
            .filter { it.isEncoder && it.supportedTypes.any { t -> t.equals(MediaFormat.MIMETYPE_VIDEO_AVC, ignoreCase = true) } }
            .map { EncoderCandidate(it, isHardware(it)) }
            .sortedByDescending { it.hardware }
        list.forEach { Log.i(TAG, "H.264 encoder candidate: ${it.name} hardware=${it.hardware}") }
        return list
    }

    fun candidatesFor(width: Int, height: Int, fps: Int): List<EncoderCandidate> =
        all().filter { it.supports(width, height, fps) }

    private fun isHardware(info: MediaCodecInfo): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) return info.isHardwareAccelerated && !info.isSoftwareOnly
        val n = info.name.lowercase()
        return !(n.startsWith("omx.google.") || n.startsWith("c2.android.") || n.contains(".sw.") || n.endsWith(".sw"))
    }
}
