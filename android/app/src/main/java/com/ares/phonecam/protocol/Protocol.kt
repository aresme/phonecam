package com.ares.phonecam.protocol

import java.io.EOFException
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

/** PhoneCam wire protocol v1. docs/protocol.md is the source of truth. */
object Protocol {
    const val MAGIC = 0x5043414D // "PCAM"
    const val VERSION = 1
    const val HEADER_SIZE = 20
    const val MAX_PAYLOAD = 4 * 1024 * 1024
    const val VIDEO_PORT = 47100
    const val CONTROL_PORT = 47101
    const val FLAG_KEYFRAME = 0x01
}

/** Packet type codes. Plain ints, because unknown types must survive decoding (and be skipped). */
object PacketType {
    const val HELLO = 0x01
    const val CONFIG = 0x02
    const val VIDEO = 0x03
    const val CONTROL_REQ = 0x10
    const val CONTROL_RES = 0x11
    const val STATE = 0x12
    const val PING = 0x20
    const val PONG = 0x21
    const val STATS = 0x30
    const val AUDIO = 0x40
    const val AUDIO_CONFIG = 0x41

    private val known = setOf(HELLO, CONFIG, VIDEO, CONTROL_REQ, CONTROL_RES, STATE, PING, PONG, STATS, AUDIO, AUDIO_CONFIG)
    fun isKnown(type: Int) = type in known
}

/** Fatal framing error: the connection must be closed. [kind] matches the shared test vectors. */
class ProtocolException(val kind: Kind, message: String) : IOException(message) {
    enum class Kind(val wire: String) { BAD_MAGIC("bad_magic"), BAD_VERSION("bad_version"), TOO_LARGE("too_large"), BAD_PTS("bad_pts"), TRUNCATED("truncated") }
}

class Packet(
    val type: Int,
    val payload: ByteArray = EMPTY,
    val flags: Int = 0,
    /** Phone clock (elapsedRealtimeNanos / 1000). Kept below 2^63 so it fits a Long. */
    val ptsUs: Long = 0,
) {
    init {
        require(type in 0..0xFF && flags in 0..0xFF) { "type/flags must fit in a byte" }
        require(payload.size <= Protocol.MAX_PAYLOAD) { "payload too large: ${payload.size}" }
        require(ptsUs >= 0) { "ptsUs must be non-negative" }
    }

    val isKeyframe get() = flags and Protocol.FLAG_KEYFRAME != 0

    fun encode(): ByteArray =
        ByteArray(Protocol.HEADER_SIZE + payload.size).also { out ->
            encodeHeader(type, payload.size, flags, ptsUs).copyInto(out)
            payload.copyInto(out, Protocol.HEADER_SIZE)
        }

    override fun equals(other: Any?) = other is Packet && type == other.type && flags == other.flags &&
        ptsUs == other.ptsUs && payload.contentEquals(other.payload)

    override fun hashCode() = ((type * 31 + flags) * 31 + ptsUs.hashCode()) * 31 + payload.contentHashCode()

    override fun toString() = "Packet(type=0x%02x, flags=0x%02x, ptsUs=%d, len=%d)".format(type, flags, ptsUs, payload.size)

    companion object {
        val EMPTY = ByteArray(0)

        fun encodeHeader(type: Int, length: Int, flags: Int = 0, ptsUs: Long = 0): ByteArray {
            require(length in 0..Protocol.MAX_PAYLOAD) { "length out of range: $length" }
            require(ptsUs >= 0) { "ptsUs must be non-negative" }
            return ByteBuffer.allocate(Protocol.HEADER_SIZE).order(ByteOrder.BIG_ENDIAN)
                .putInt(Protocol.MAGIC)
                .put(Protocol.VERSION.toByte())
                .put(type.toByte())
                .put(flags.toByte())
                .put(0)
                .putLong(ptsUs)
                .putInt(length)
                .array()
        }

        fun json(type: Int, json: String, ptsUs: Long = 0) = Packet(type, json.toByteArray(Charsets.UTF_8), 0, ptsUs)

        fun ping(tPcSendUs: Long) = Packet(PacketType.PING, ByteBuffer.allocate(8).putLong(tPcSendUs).array())

        fun pong(tPcSendUs: Long, tPhoneUs: Long) =
            Packet(PacketType.PONG, ByteBuffer.allocate(16).putLong(tPcSendUs).putLong(tPhoneUs).array(), 0, tPhoneUs)

        /** PING payload → t_pc_send_us. */
        fun parsePing(p: Packet): Long = ByteBuffer.wrap(p.payload).long
    }
}

/** Blocking packet reader. Not thread-safe; use one reader per socket on its own thread. */
class PacketReader(private val input: InputStream) {
    private val header = ByteArray(Protocol.HEADER_SIZE)

    /** Next packet, or null on a clean end of stream at a packet boundary. */
    fun read(): Packet? {
        val got = readFully(header, 0, header.size)
        if (got == 0) return null
        if (got < header.size) throw truncated()
        val buf = ByteBuffer.wrap(header).order(ByteOrder.BIG_ENDIAN)
        val magic = buf.int
        if (magic != Protocol.MAGIC) throw ProtocolException(ProtocolException.Kind.BAD_MAGIC, "bad magic 0x%08x".format(magic))
        val version = buf.get().toInt() and 0xFF
        if (version != Protocol.VERSION) throw ProtocolException(ProtocolException.Kind.BAD_VERSION, "unsupported version $version")
        val type = buf.get().toInt() and 0xFF
        val flags = buf.get().toInt() and 0xFF
        buf.get() // reserved, ignored
        val pts = buf.long
        val length = buf.int.toLong() and 0xFFFFFFFFL
        if (length > Protocol.MAX_PAYLOAD) {
            throw ProtocolException(ProtocolException.Kind.TOO_LARGE, "payload length $length exceeds ${Protocol.MAX_PAYLOAD}")
        }
        if (pts < 0) throw ProtocolException(ProtocolException.Kind.BAD_PTS, "pts_us exceeds 2^63-1")
        val payload = if (length == 0L) Packet.EMPTY else ByteArray(length.toInt())
        if (readFully(payload, 0, payload.size) < payload.size) throw truncated()
        return Packet(type, payload, flags, pts)
    }

    private fun readFully(b: ByteArray, off: Int, len: Int): Int {
        var n = 0
        while (n < len) {
            val r = input.read(b, off + n, len - n)
            if (r < 0) break
            n += r
        }
        return n
    }

    private fun truncated() = ProtocolException(ProtocolException.Kind.TRUNCATED, "stream ended mid-packet")
}

/**
 * Packet writer. [write] is synchronized so several producers (video, control, pong) can share
 * one socket. Header and payload go out without concatenating into a new array.
 */
class PacketWriter(private val output: OutputStream) {
    @Synchronized
    fun write(type: Int, payload: ByteArray, offset: Int = 0, length: Int = payload.size, flags: Int = 0, ptsUs: Long = 0) {
        output.write(Packet.encodeHeader(type, length, flags, ptsUs))
        if (length > 0) output.write(payload, offset, length)
        output.flush()
    }

    fun write(p: Packet) = write(p.type, p.payload, 0, p.payload.size, p.flags, p.ptsUs)
}

/** Thrown by callers that expect a packet; kept here so EOF semantics are documented in one place. */
fun PacketReader.readOrThrow(): Packet = read() ?: throw EOFException("connection closed")
