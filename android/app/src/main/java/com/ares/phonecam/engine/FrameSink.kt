package com.ares.phonecam.engine

/**
 * Consumer of the encoded stream (file recorder, network client, ...). Called on the encoder
 * thread: implementations must not block for long.
 */
interface FrameSink {
    /** SPS+PPS (Annex-B). Always delivered before the first frame, and again after encoder restarts. */
    fun onConfig(csd: ByteArray)

    fun onFrame(data: ByteArray, ptsUs: Long, keyframe: Boolean)

    /** Detached from the engine; release resources. */
    fun close()
}
