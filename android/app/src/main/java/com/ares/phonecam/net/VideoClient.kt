package com.ares.phonecam.net

import android.util.Log
import com.ares.phonecam.TAG
import com.ares.phonecam.engine.FrameSink
import com.ares.phonecam.protocol.PacketType
import com.ares.phonecam.protocol.PacketWriter
import com.ares.phonecam.protocol.Protocol
import java.io.BufferedOutputStream
import java.io.IOException
import java.net.Socket
import java.util.concurrent.atomic.AtomicLong

/**
 * One PC on the video port. Frames arrive on the encoder thread and go through a [FrameQueue] to
 * a dedicated sender thread, so a slow socket never blocks the encoder.
 */
class VideoClient(
    private val socket: Socket,
    private val requestKeyframe: () -> Unit,
    private val onClosed: (VideoClient) -> Unit,
) : FrameSink {
    private val queue = FrameQueue(MAX_FRAMES)
    @Volatile private var closed = false

    val remote: String = socket.inetAddress?.hostAddress ?: "?"
    val sent = AtomicLong()
    val dropped: Long get() = queue.dropped
    val queueDepth: Int get() = queue.depth

    // Buffered so the 20-byte header and the payload leave in one write (TCP_NODELAY is on).
    private val writer = PacketWriter(BufferedOutputStream(socket.getOutputStream(), 256 * 1024))
    private val thread = Thread(::sendLoop, "video-send").apply { isDaemon = true }

    fun start() = thread.start()

    override fun onConfig(csd: ByteArray) = queue.offerConfig(csd)

    override fun onFrame(data: ByteArray, ptsUs: Long, keyframe: Boolean, extraFlags: Int) {
        if (queue.offerFrame(data, ptsUs, keyframe, extraFlags)) {
            Log.d(TAG, "video queue full for $remote; dropping until next keyframe")
            requestKeyframe()
        }
    }

    private fun sendLoop() {
        try {
            while (true) {
                when (val item = queue.take() ?: break) {
                    is FrameQueue.Config -> writer.write(PacketType.CONFIG, item.csd)
                    is FrameQueue.Frame -> {
                        val flags = (if (item.key) Protocol.FLAG_KEYFRAME else 0) or item.extraFlags
                        writer.write(PacketType.VIDEO, item.data, flags = flags, ptsUs = item.ptsUs)
                        sent.incrementAndGet()
                    }
                }
            }
        } catch (e: IOException) {
            if (!closed) Log.i(TAG, "video client $remote disconnected: ${e.message}")
        } catch (_: InterruptedException) {
        }
        if (!closed) onClosed(this)
    }

    override fun close() {
        if (closed) return
        closed = true
        queue.close()
        try {
            socket.close()
        } catch (_: IOException) {
        }
        Log.i(TAG, "video client $remote closed (sent=${sent.get()} dropped=$dropped)")
    }

    companion object {
        const val MAX_FRAMES = 2
    }
}
