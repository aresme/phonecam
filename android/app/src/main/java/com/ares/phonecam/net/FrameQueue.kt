package com.ares.phonecam.net

/**
 * Bounded hand-off between the encoder thread and a socket sender thread.
 *
 * Drop policy (docs/protocol.md §6): at most [maxFrames] access units wait. If a non-key frame
 * arrives while full, it is dropped, all following non-key frames are dropped too (they would
 * reference the missing one), and the caller is told to request a sync frame. A keyframe flushes
 * any queued frames and is always enqueued. A CONFIG resets the queue and waits for a keyframe.
 */
class FrameQueue(private val maxFrames: Int = 2) {
    sealed interface Item
    class Config(val csd: ByteArray) : Item
    class Frame(val data: ByteArray, val ptsUs: Long, val key: Boolean, val extraFlags: Int = 0) : Item

    private val lock = Object()
    private val queue = ArrayDeque<Item>()
    private var frames = 0
    private var waitingForKey = true
    private var closed = false

    var dropped = 0L
        private set

    val depth: Int get() = synchronized(lock) { frames }

    fun offerConfig(csd: ByteArray) = synchronized(lock) {
        if (closed) return
        queue.clear()
        frames = 0
        queue.addLast(Config(csd))
        waitingForKey = true
        lock.notifyAll()
    }

    /** @return true if the caller should request a keyframe from the encoder. */
    fun offerFrame(data: ByteArray, ptsUs: Long, key: Boolean, extraFlags: Int = 0): Boolean = synchronized(lock) {
        if (closed) return false
        if (key) {
            if (queue.removeAll { it is Frame }) dropped += frames
            frames = 0
            waitingForKey = false
        } else if (waitingForKey) {
            dropped++
            return false
        } else if (frames >= maxFrames) {
            dropped++
            waitingForKey = true
            return true
        }
        queue.addLast(Frame(data, ptsUs, key, extraFlags))
        frames++
        lock.notifyAll()
        return false
    }

    /** Blocks until an item is available; null once closed. */
    fun take(): Item? = synchronized(lock) {
        while (queue.isEmpty() && !closed) lock.wait()
        if (closed) return null
        queue.removeFirst().also { if (it is Frame) frames-- }
    }

    fun close() = synchronized(lock) {
        closed = true
        queue.clear()
        lock.notifyAll()
    }
}
