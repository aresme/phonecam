package com.ares.phonecam.engine

import android.util.Log
import com.ares.phonecam.TAG
import java.io.BufferedOutputStream
import java.io.File
import java.io.FileOutputStream
import java.io.IOException

/** Writes a raw Annex-B .h264 elementary stream (playable with `ffplay -f h264`). */
class FileSink(val file: File) : FrameSink {
    private val out = BufferedOutputStream(FileOutputStream(file), 256 * 1024)
    private var haveConfig = false
    private var started = false
    private var closed = false
    var bytes = 0L
        private set

    @Synchronized
    override fun onConfig(csd: ByteArray) {
        if (closed) return
        write(csd)
        haveConfig = true
    }

    @Synchronized
    override fun onFrame(data: ByteArray, ptsUs: Long, keyframe: Boolean, extraFlags: Int) {
        if (closed || !haveConfig) return
        if (!started && !keyframe) return // a decodable file starts at an IDR
        started = true
        write(data)
    }

    private fun write(b: ByteArray) {
        try {
            out.write(b)
            bytes += b.size
        } catch (e: IOException) {
            Log.e(TAG, "recording write failed; stopping recording", e)
            closeQuietly()
        }
    }

    @Synchronized
    override fun close() = closeQuietly()

    private fun closeQuietly() {
        if (closed) return
        closed = true
        try {
            out.close()
        } catch (e: IOException) {
            Log.w(TAG, "closing ${file.name}", e)
        }
        Log.i(TAG, "recording closed: $file ($bytes bytes)")
    }
}
