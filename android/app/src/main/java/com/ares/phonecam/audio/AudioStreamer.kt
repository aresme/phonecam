package com.ares.phonecam.audio

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTimestamp
import android.media.MediaCodec
import android.media.MediaCodecList
import android.media.MediaFormat
import android.media.MediaRecorder
import android.os.Build
import android.os.SystemClock
import android.util.Base64
import android.util.Log
import com.ares.phonecam.TAG
import org.json.JSONObject

/**
 * Microphone → 20 ms frames → Opus (MediaCodec, API 29+) or raw PCM, with capture timestamps on
 * the protocol clock (CLOCK_BOOTTIME µs) for A/V sync on the PC. Runs on its own thread.
 */
class AudioStreamer(private val listener: Listener, private val voice: Boolean) {
    interface Listener {
        fun onAudioConfig(json: String)
        fun onAudio(data: ByteArray, ptsUs: Long)
        fun onAudioError(message: String)
    }

    @Volatile private var running = false
    private var thread: Thread? = null

    var codecName: String = ""
        private set

    fun start() {
        if (running) return
        running = true
        thread = Thread(::run, "audio").apply { start() }
    }

    fun stop() {
        running = false
        thread?.join(1000)
        thread = null
    }

    @SuppressLint("MissingPermission") // checked by the engine before starting
    private fun run() {
        val source = if (voice) MediaRecorder.AudioSource.VOICE_COMMUNICATION else MediaRecorder.AudioSource.CAMCORDER
        val minBuf = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val rec = try {
            AudioRecord(source, RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, FRAME * 2 * 4))
        } catch (e: Exception) {
            listener.onAudioError("microphone unavailable: ${e.message}")
            running = false
            return
        }
        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            rec.release()
            listener.onAudioError("microphone could not be initialized")
            running = false
            return
        }
        val encoder = createOpusEncoder()
        codecName = if (encoder != null) "opus" else "pcm_s16le"
        if (encoder == null) {
            listener.onAudioConfig(config("pcm_s16le", null))
        }
        val pcm = ByteArray(FRAME * 2)
        val ts = AudioTimestamp()
        var framesRead = 0L
        val info = MediaCodec.BufferInfo()
        var configSent = encoder == null
        try {
            rec.startRecording()
            Log.i(TAG, "audio capture started (${if (voice) "voice" else "raw"} source, $codecName)")
            while (running) {
                var off = 0
                while (off < pcm.size && running) {
                    val n = rec.read(pcm, off, pcm.size - off)
                    if (n < 0) throw IllegalStateException("AudioRecord.read error $n")
                    off += n
                }
                if (!running) break
                val ptsUs = framePtsUs(rec, ts, framesRead)
                framesRead += FRAME
                if (encoder == null) {
                    listener.onAudio(pcm.copyOf(), ptsUs)
                    continue
                }
                val inIdx = encoder.dequeueInputBuffer(20_000)
                if (inIdx >= 0) {
                    encoder.getInputBuffer(inIdx)!!.apply { clear(); put(pcm) }
                    encoder.queueInputBuffer(inIdx, 0, pcm.size, ptsUs, 0)
                }
                // Wait briefly for this frame's packet so it leaves now, not one frame (20 ms) later.
                var waitUs = 10_000L
                while (true) {
                    val outIdx = encoder.dequeueOutputBuffer(info, waitUs)
                    waitUs = 0
                    if (outIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                        val head = encoder.outputFormat.getByteBuffer("csd-0")
                        val bytes = head?.let { ByteArray(it.remaining()).also { b -> it.get(b) } }
                        listener.onAudioConfig(config("opus", bytes?.let(::opusHead)))
                        configSent = true
                        continue
                    }
                    if (outIdx < 0) break
                    val buf = encoder.getOutputBuffer(outIdx)
                    if (buf != null && info.size > 0 && info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0 && configSent) {
                        buf.position(info.offset).limit(info.offset + info.size)
                        listener.onAudio(ByteArray(info.size).also { buf.get(it) }, info.presentationTimeUs)
                    }
                    encoder.releaseOutputBuffer(outIdx, false)
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "audio failed", e)
            listener.onAudioError("audio failed: ${e.message}")
        } finally {
            runCatching { rec.stop() }
            rec.release()
            encoder?.let { runCatching { it.stop() }; it.release() }
            running = false
            Log.i(TAG, "audio capture stopped")
        }
    }

    /** Device frame index of our stream's first sample (see [framePtsUs]); null = uncalibrated. */
    private var deviceBase: Long? = null

    /**
     * Capture time (CLOCK_BOOTTIME µs) of the first sample of the frame that starts at our stream
     * position [framePos], called right after that frame was read.
     *
     * `AudioRecord.getTimestamp` maps a *device* frame index to its capture time, but on some
     * devices (MediaTek) that index counts from when the microphone device opened, not from our
     * recording start. So once, at the first frame, we estimate which device index our stream
     * starts at (the frame just read ends roughly "now"), and then derive exact 20 ms-spaced
     * timestamps from the latest device timestamp. We recalibrate if it drifts by > 0.5 s.
     */
    private fun framePtsUs(rec: AudioRecord, ts: AudioTimestamp, framePos: Long): Long {
        val nowNs = SystemClock.elapsedRealtimeNanos()
        val fallbackUs = nowNs / 1000 - FRAME * 1_000_000L / RATE
        if (rec.getTimestamp(ts, AudioTimestamp.TIMEBASE_BOOTTIME) != AudioRecord.SUCCESS) return fallbackUs
        val deviceNow = ts.framePosition + (nowNs - ts.nanoTime) * RATE / 1_000_000_000L
        var base = deviceBase ?: (deviceNow - (framePos + FRAME)).also { deviceBase = it }
        var ptsUs = (ts.nanoTime + (framePos + base - ts.framePosition) * 1_000_000_000L / RATE) / 1000
        if (kotlin.math.abs(ptsUs - fallbackUs) > 500_000) {
            Log.w(TAG, "audio timestamp jumped by ${(ptsUs - fallbackUs) / 1000} ms; recalibrating")
            base = deviceNow - (framePos + FRAME)
            deviceBase = base
            ptsUs = (ts.nanoTime + (framePos + base - ts.framePosition) * 1_000_000_000L / RATE) / 1000
        }
        return ptsUs
    }

    private fun createOpusEncoder(): MediaCodec? {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return null
        val format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, RATE, 1).apply {
            setInteger(MediaFormat.KEY_BIT_RATE, 48_000)
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, FRAME * 2)
        }
        val name = MediaCodecList(MediaCodecList.REGULAR_CODECS).findEncoderForFormat(format) ?: return null
        return try {
            MediaCodec.createByCodecName(name).apply {
                configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
                start()
            }.also { Log.i(TAG, "audio encoder $name") }
        } catch (e: Exception) {
            Log.w(TAG, "Opus encoder unavailable, sending PCM", e)
            null
        }
    }

    /**
     * Android's Opus encoder wraps the standard 19-byte `OpusHead` in its own container
     * ("AOPUSHDR" + length + OpusHead + "AOPUSDLY"... ). Return just the OpusHead (RFC 7845).
     */
    private fun opusHead(csd: ByteArray): ByteArray {
        val sig = "OpusHead".toByteArray()
        for (i in 0..csd.size - 19) {
            if ((0 until 8).all { csd[i + it] == sig[it] }) return csd.copyOfRange(i, i + 19)
        }
        return csd
    }

    private fun config(codec: String, csd: ByteArray?): String = JSONObject()
        .put("codec", codec).put("sample_rate", RATE).put("channels", 1).put("frame_samples", FRAME)
        .put("source", if (voice) "voice" else "raw")
        .apply { csd?.let { put("csd", Base64.encodeToString(it, Base64.NO_WRAP)) } }
        .toString()

    companion object {
        const val RATE = 48_000
        const val FRAME = 960 // 20 ms
    }
}
