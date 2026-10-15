package com.ares.phonecam.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.concurrent.thread

class FrameQueueTest {
    private fun b(v: Int) = byteArrayOf(v.toByte())
    private fun describe(i: FrameQueue.Item) = when (i) {
        is FrameQueue.Config -> "C"
        is FrameQueue.Frame -> (if (i.key) "K" else "P") + i.data[0]
        is FrameQueue.AudioConfig -> "AC"
        is FrameQueue.Audio -> "A" + i.data[0]
    }

    @Test
    fun framesBeforeFirstKeyframeAreDropped() {
        val q = FrameQueue(2)
        q.offerConfig(b(0))
        assertFalse(q.offerFrame(b(1), 1, false))
        q.offerFrame(b(2), 2, true)
        assertEquals("C", describe(q.take()!!))
        assertEquals("K2", describe(q.take()!!))
        assertEquals(1, q.dropped)
    }

    @Test
    fun fullQueueDropsUntilNextKeyframeAndAsksForOne() {
        val q = FrameQueue(2)
        q.offerConfig(b(0))
        q.offerFrame(b(1), 1, true)
        q.offerFrame(b(2), 2, false)
        assertTrue("full queue must request a keyframe", q.offerFrame(b(3), 3, false))
        assertFalse("already waiting: no repeated request", q.offerFrame(b(4), 4, false))
        assertEquals(2, q.dropped)
        q.offerFrame(b(5), 5, true) // flushes K1, P2
        assertEquals(4, q.dropped)
        assertEquals("C", describe(q.take()!!))
        assertEquals("K5", describe(q.take()!!))
        assertEquals(0, q.depth)
        q.offerFrame(b(6), 6, false)
        assertEquals("P6", describe(q.take()!!))
    }

    @Test
    fun configResetsQueue() {
        val q = FrameQueue(2)
        q.offerConfig(b(0))
        q.offerFrame(b(1), 1, true)
        q.offerConfig(b(9))
        q.offerFrame(b(2), 2, false) // must wait for a key after the new config
        assertEquals("C", describe(q.take()!!))
        assertEquals(0, q.depth)
    }

    @Test
    fun closeUnblocksTake() {
        val q = FrameQueue(2)
        var result: Any? = "unset"
        val t = thread { result = q.take() }
        Thread.sleep(50)
        q.close()
        t.join(1000)
        assertNull(result)
    }

    @Test
    fun audioGoesFirstAndSurvivesVideoDrops() {
        val q = FrameQueue(2)
        q.offerConfig(b(0))
        q.offerFrame(b(1), 1, true)
        q.offerFrame(b(2), 2, false)
        q.offerFrame(b(3), 3, false) // video full -> dropped
        q.offerAudioConfig("{}")
        q.offerAudio(b(7), 7)
        assertEquals("AC", describe(q.take()!!))
        assertEquals("A7", describe(q.take()!!))
        assertEquals("C", describe(q.take()!!))
    }

    @Test
    fun audioLaneIsBounded() {
        val q = FrameQueue(2)
        repeat(FrameQueue.MAX_AUDIO + 5) { q.offerAudio(b(it), it.toLong()) }
        assertEquals(5, q.audioDropped)
        assertEquals("A5", describe(q.take()!!)) // the oldest 5 were dropped
    }
}
