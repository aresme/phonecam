package com.ares.phonecam.protocol

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.InputStream

class ProtocolTest {
    private val dir = File(System.getProperty("phonecam.vectors") ?: error("phonecam.vectors not set"))
    private val manifest = JSONArray(File(dir, "manifest.json").readText())
    private val entries = (0 until manifest.length()).map { manifest.getJSONObject(it) }

    private fun loadHex(name: String): ByteArray =
        File(dir, name).readLines()
            .joinToString(" ") { it.substringBefore('#') }
            .split(Regex("\\s+")).filter { it.isNotEmpty() }
            .map { it.toInt(16).toByte() }.toByteArray()

    private fun String.hexToBytes() = chunked(2).map { it.toInt(16).toByte() }.toByteArray()

    private fun expected(entry: JSONObject): List<Packet> {
        val arr = entry.getJSONArray("packets")
        return (0 until arr.length()).map {
            val p = arr.getJSONObject(it)
            Packet(p.getInt("type"), p.getString("payload").hexToBytes(), p.getInt("flags"), p.getString("pts_us").toLong())
        }
    }

    /** Returns at most [chunk] bytes per read() call, to exercise partial reads. */
    private class TrickleInputStream(private val data: ByteArray, private val chunk: Int) : InputStream() {
        private var pos = 0
        override fun read(): Int = if (pos < data.size) data[pos++].toInt() and 0xFF else -1
        override fun read(b: ByteArray, off: Int, len: Int): Int {
            if (pos >= data.size) return -1
            val n = minOf(len, chunk, data.size - pos)
            data.copyInto(b, off, pos, pos + n)
            pos += n
            return n
        }
    }

    private fun readAll(input: InputStream): List<Packet> {
        val reader = PacketReader(input)
        return generateSequence { reader.read() }.toList()
    }

    @Test
    fun allVectorsDecodeAsSpecified() {
        assertTrue("expected vectors", entries.size >= 10)
        for (entry in entries) {
            val name = entry.getString("file")
            val data = loadHex(name)
            for (chunk in listOf(1, 7, Int.MAX_VALUE)) {
                val input = TrickleInputStream(data, chunk)
                if (entry.has("error")) {
                    try {
                        readAll(input)
                        fail("$name (chunk $chunk): expected ${entry.getString("error")}")
                    } catch (e: ProtocolException) {
                        assertEquals(name, entry.getString("error"), e.kind.wire)
                    }
                } else {
                    assertEquals("$name (chunk $chunk)", expected(entry), readAll(input))
                }
            }
        }
    }

    @Test
    fun validVectorsReencodeByteForByte() {
        for (entry in entries.filter { it.has("packets") }) {
            val name = entry.getString("file")
            val viaEncode = ByteArrayOutputStream().apply { expected(entry).forEach { write(it.encode()) } }.toByteArray()
            assertArrayEquals(name, loadHex(name), viaEncode)
            val viaWriter = ByteArrayOutputStream().also { out -> expected(entry).forEach { PacketWriter(out).write(it) } }
            assertArrayEquals(name, loadHex(name), viaWriter.toByteArray())
        }
    }

    @Test
    fun writerWithOffsetAndLength() {
        val buf = "xxHELLOyy".toByteArray()
        val out = ByteArrayOutputStream()
        PacketWriter(out).write(PacketType.VIDEO, buf, offset = 2, length = 5, flags = 1, ptsUs = 99)
        assertEquals(Packet(PacketType.VIDEO, "HELLO".toByteArray(), 1, 99), PacketReader(ByteArrayInputStream(out.toByteArray())).read())
    }

    @Test
    fun cleanEofReturnsNull() {
        assertNull(PacketReader(ByteArrayInputStream(ByteArray(0))).read())
    }

    @Test
    fun maxPayloadBoundary() {
        assertEquals(Protocol.MAX_PAYLOAD, Packet(PacketType.VIDEO, ByteArray(Protocol.MAX_PAYLOAD)).payload.size)
        val big = Packet(PacketType.VIDEO, ByteArray(Protocol.MAX_PAYLOAD), 0, 1).encode()
        assertEquals(Protocol.MAX_PAYLOAD, PacketReader(ByteArrayInputStream(big)).read()!!.payload.size)
        try {
            Packet.encodeHeader(PacketType.VIDEO, Protocol.MAX_PAYLOAD + 1)
            fail("expected IllegalArgumentException")
        } catch (_: IllegalArgumentException) {
        }
    }

    @Test
    fun reservedByteIgnored() {
        val raw = Packet(PacketType.STATE, "{}".toByteArray()).encode()
        raw[7] = 0xAA.toByte()
        assertEquals(Packet(PacketType.STATE, "{}".toByteArray()), PacketReader(ByteArrayInputStream(raw)).read())
    }

    @Test
    fun pingPongHelpers() {
        val ping = Packet.ping(123456789)
        assertEquals(8, ping.payload.size)
        assertEquals(123456789L, Packet.parsePing(ping))
        val pong = Packet.pong(123456789, 987654321012)
        // Matches the shared vector byte-for-byte.
        assertArrayEquals(loadHex("07-pong.hex"), pong.encode())
    }

    @Test
    fun keyframeFlagAndKnownTypes() {
        assertTrue(Packet(PacketType.VIDEO, flags = 1).isKeyframe)
        assertFalse(Packet(PacketType.VIDEO, flags = 0x80).isKeyframe)
        assertTrue(PacketType.isKnown(PacketType.STATS))
        assertFalse(PacketType.isKnown(0x7F))
    }
}
