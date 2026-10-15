package com.ares.phonecam.encoder

import android.media.MediaCodec
import android.media.MediaCodecInfo.CodecCapabilities
import android.media.MediaCodecInfo.CodecProfileLevel
import android.media.MediaCodecInfo.EncoderCapabilities
import android.media.MediaFormat
import android.os.Build
import android.os.Bundle
import android.os.HandlerThread
import android.os.Handler
import android.os.SystemClock
import android.util.Log
import android.view.Surface
import com.ares.phonecam.TAG
import java.io.IOException
import kotlin.math.abs

data class EncoderConfig(
    val width: Int,
    val height: Int,
    val fps: Int,
    val bitrate: Int,
    val iFrameIntervalSec: Int = 1,
    val profile: String = "auto",
    val operatingRate: Int = 0,
    val vendor: Map<String, Int> = emptyMap(),
)

/**
 * H.264 encoder with Surface input in async mode. Callbacks run on a dedicated HandlerThread; the
 * listener must not block (it should copy/enqueue and return).
 */
class VideoEncoder private constructor(
    val config: EncoderConfig,
    val name: String,
    val hardware: Boolean,
    private val codec: MediaCodec,
    private val thread: HandlerThread,
    private val listener: Listener,
) {
    interface Listener {
        /** SPS+PPS, Annex-B. */
        fun onConfig(csd: ByteArray)

        /** One access unit, Annex-B, owned by the receiver. */
        fun onFrame(data: ByteArray, ptsUs: Long, keyframe: Boolean)

        fun onError(e: Exception)
    }

    lateinit var inputSurface: Surface
        private set

    @Volatile private var released = false
    private var configSent = false
    private var ptsOffsetUs: Long? = null

    /**
     * Map encoder timestamps to the protocol clock (elapsedRealtime / CLOCK_BOOTTIME). The camera
     * framework re-bases timestamps for video-encoder Surfaces to CLOCK_MONOTONIC on many devices
     * (for A/V sync), regardless of SENSOR_INFO_TIMESTAMP_SOURCE, so detect the base from the
     * first frame: whichever clock it is closest to. The two clocks differ by total deep-sleep time,
     * which is constant while we hold a wake lock.
     */
    private fun toProtocolClock(ptsUs: Long): Long {
        val offset = ptsOffsetUs ?: run {
            val boot = SystemClock.elapsedRealtimeNanos() / 1000
            val mono = System.nanoTime() / 1000
            val o = if (abs(mono - ptsUs) < abs(boot - ptsUs)) boot - mono else 0L
            Log.i(TAG, "encoder timestamps are ${if (o == 0L) "BOOTTIME" else "MONOTONIC (offset ${o / 1000} ms)"}")
            ptsOffsetUs = o
            o
        }
        return ptsUs + offset
    }

    private val callback = object : MediaCodec.Callback() {
        override fun onInputBufferAvailable(codec: MediaCodec, index: Int) = Unit // Surface input

        override fun onOutputBufferAvailable(codec: MediaCodec, index: Int, info: MediaCodec.BufferInfo) {
            if (released) return
            try {
                val buf = codec.getOutputBuffer(index)
                if (buf != null && info.size > 0) {
                    buf.position(info.offset).limit(info.offset + info.size)
                    val data = ByteArray(info.size).also { buf.get(it) }
                    if (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0) {
                        configSent = true
                        listener.onConfig(data)
                    } else {
                        val key = info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME != 0
                        listener.onFrame(data, toProtocolClock(info.presentationTimeUs), key)
                    }
                }
                codec.releaseOutputBuffer(index, false)
            } catch (e: IllegalStateException) {
                if (!released) listener.onError(e)
            }
        }

        override fun onOutputFormatChanged(codec: MediaCodec, format: MediaFormat) {
            Log.i(TAG, "encoder output format: $format")
            // Some codecs deliver SPS/PPS only via csd-0/csd-1 instead of a CODEC_CONFIG buffer.
            if (configSent) return
            val sps = format.getByteBuffer("csd-0") ?: return
            val pps = format.getByteBuffer("csd-1")
            val out = ByteArray(sps.remaining() + (pps?.remaining() ?: 0))
            sps.get(out, 0, sps.remaining())
            pps?.get(out, out.size - pps.remaining(), pps.remaining())
            configSent = true
            listener.onConfig(out)
        }

        override fun onError(codec: MediaCodec, e: MediaCodec.CodecException) {
            Log.e(TAG, "encoder error (transient=${e.isTransient} recoverable=${e.isRecoverable})", e)
            if (!released && !e.isTransient) listener.onError(e)
        }
    }

    private fun startCodec(format: MediaFormat) {
        codec.setCallback(callback, Handler(thread.looper))
        codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        inputSurface = codec.createInputSurface()
        codec.start()
    }

    fun requestKeyframe() = setParam { putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0) }

    fun setBitrate(bps: Int) = setParam { putInt(MediaCodec.PARAMETER_KEY_VIDEO_BITRATE, bps) }

    private inline fun setParam(fill: Bundle.() -> Unit) {
        if (released) return
        try {
            codec.setParameters(Bundle().apply(fill))
        } catch (e: IllegalStateException) {
            Log.w(TAG, "setParameters failed", e)
        }
    }

    /** Stop and free everything. The camera must no longer render into [inputSurface]. */
    fun release() {
        if (released) return
        released = true
        try {
            codec.stop()
        } catch (e: Exception) {
            Log.w(TAG, "codec.stop", e)
        }
        codec.release()
        if (::inputSurface.isInitialized) inputSurface.release()
        thread.quitSafely()
        Log.i(TAG, "encoder $name released")
    }

    companion object {
        /** Create and start an encoder, trying hardware candidates first. */
        fun create(cfg: EncoderConfig, listener: Listener): VideoEncoder {
            val candidates = EncoderSelector.candidatesFor(cfg.width, cfg.height, cfg.fps)
            if (candidates.isEmpty()) throw IOException("no H.264 encoder supports ${cfg.width}x${cfg.height}@${cfg.fps}")
            var last: Exception? = null
            for (c in candidates) {
                // With profile/level first, then without (some codecs reject explicit levels).
                for (withProfile in listOf(true, false)) {
                    val thread = HandlerThread("encoder").apply { start() }
                    val codec = try {
                        MediaCodec.createByCodecName(c.name)
                    } catch (e: Exception) {
                        thread.quitSafely(); last = e; break
                    }
                    val enc = VideoEncoder(cfg, c.name, c.hardware, codec, thread, listener)
                    try {
                        val format = buildFormat(cfg, c.caps, withProfile)
                        enc.startCodec(format)
                        Log.i(TAG, "encoder ${c.name} started: $format")
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                            runCatching { Log.i(TAG, "encoder vendor parameters: ${codec.supportedVendorParameters}") }
                        }
                        return enc
                    } catch (e: Exception) {
                        Log.w(TAG, "encoder ${c.name} (profile=$withProfile) failed to start", e)
                        last = e
                        enc.release()
                    }
                }
            }
            throw IOException("could not start any H.264 encoder", last)
        }

        private fun buildFormat(cfg: EncoderConfig, caps: CodecCapabilities, withProfile: Boolean): MediaFormat =
            MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, cfg.width, cfg.height).apply {
                setInteger(MediaFormat.KEY_COLOR_FORMAT, CodecCapabilities.COLOR_FormatSurface)
                setInteger(MediaFormat.KEY_BIT_RATE, cfg.bitrate)
                setInteger(MediaFormat.KEY_FRAME_RATE, cfg.fps)
                setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, cfg.iFrameIntervalSec)
                if (caps.encoderCapabilities?.isBitrateModeSupported(EncoderCapabilities.BITRATE_MODE_CBR) == true) {
                    setInteger(MediaFormat.KEY_BITRATE_MODE, EncoderCapabilities.BITRATE_MODE_CBR)
                } else {
                    setInteger(MediaFormat.KEY_BITRATE_MODE, EncoderCapabilities.BITRATE_MODE_VBR)
                }
                if (withProfile) {
                    val profiles = caps.profileLevels.map { it.profile }.toSet()
                    val profile = when (cfg.profile) {
                        "main" -> CodecProfileLevel.AVCProfileMain
                        "high" -> CodecProfileLevel.AVCProfileHigh
                        "baseline" -> CodecProfileLevel.AVCProfileBaseline
                        else -> if (Build.VERSION.SDK_INT >= 27 && CodecProfileLevel.AVCProfileConstrainedBaseline in profiles) {
                            CodecProfileLevel.AVCProfileConstrainedBaseline
                        } else {
                            CodecProfileLevel.AVCProfileBaseline
                        }
                    }
                    setInteger(MediaFormat.KEY_PROFILE, profile)
                    setInteger(MediaFormat.KEY_LEVEL, avcLevelFor(cfg.width, cfg.height, cfg.fps))
                }
                setInteger("max-bframes", 0)
                setInteger(MediaFormat.KEY_PRIORITY, 0) // realtime
                // If the camera stalls (e.g. during reconfiguration), keep emitting frames.
                setLong(MediaFormat.KEY_REPEAT_PREVIOUS_FRAME_AFTER, 100_000L)
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) setInteger(MediaFormat.KEY_LATENCY, 1)
                when {
                    cfg.operatingRate < 0 -> setInteger(MediaFormat.KEY_OPERATING_RATE, Short.MAX_VALUE.toInt())
                    cfg.operatingRate > 0 -> setInteger(MediaFormat.KEY_OPERATING_RATE, cfg.operatingRate)
                }
                cfg.vendor.forEach { (k, v) -> setInteger(k, v) }
            }

        /** Smallest H.264 level whose MaxFS and MaxMBPS fit the stream. */
        fun avcLevelFor(width: Int, height: Int, fps: Int): Int {
            val fs = ((width + 15) / 16) * ((height + 15) / 16)
            val mbps = fs * fps
            val table = listOf( // level, MaxFS, MaxMBPS
                Triple(CodecProfileLevel.AVCLevel3, 1620, 40500),
                Triple(CodecProfileLevel.AVCLevel31, 3600, 108000),
                Triple(CodecProfileLevel.AVCLevel32, 5120, 216000),
                Triple(CodecProfileLevel.AVCLevel4, 8192, 245760),
                Triple(CodecProfileLevel.AVCLevel42, 8704, 522240),
                Triple(CodecProfileLevel.AVCLevel5, 22080, 589824),
                Triple(CodecProfileLevel.AVCLevel51, 36864, 983040),
            )
            return table.firstOrNull { fs <= it.second && mbps <= it.third }?.first ?: CodecProfileLevel.AVCLevel51
        }
    }
}
