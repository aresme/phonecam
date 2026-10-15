package com.ares.phonecam.engine

/**
 * Consumer of the encoded stream (file recorder, network client, ...). Called on the encoder
 * thread: implementations must not block for long.
 */
interface FrameSink {
    /** SPS+PPS (Annex-B). Always delivered before the first frame, and again after encoder restarts. */
    fun onConfig(csd: ByteArray)

    /** @param extraFlags protocol VIDEO flag bits besides KEYFRAME (rotation hint, bits 1-2). */
    fun onFrame(data: ByteArray, ptsUs: Long, keyframe: Boolean, extraFlags: Int = 0)

    /** Audio stream description (JSON, see docs/protocol.md AUDIO_CONFIG). */
    fun onAudioConfig(json: String) = Unit

    /** One encoded audio frame (20 ms), pts on the protocol clock. */
    fun onAudio(data: ByteArray, ptsUs: Long) = Unit

    /** Detached from the engine; release resources. */
    fun close()
}
