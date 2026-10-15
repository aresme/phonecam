package com.ares.phonecam.camera

import kotlin.math.abs
import kotlin.math.roundToInt

/**
 * Orientation and coordinate math. Pure functions (no Android types) so they are unit-tested.
 *
 * Frames are encoded in sensor orientation. The receiver rotates them clockwise by
 * [streamRotation] degrees (sent as a hint in VIDEO flags, docs/protocol.md) to make them upright.
 */
object Geometry {
    /**
     * Clockwise rotation that makes a sensor-oriented frame upright.
     * @param deviceDeg OrientationEventListener convention: 0 = natural portrait, 270 = landscape
     *   with the top of the phone to the left.
     */
    fun streamRotation(sensorOrientation: Int, front: Boolean, deviceDeg: Int): Int =
        if (front) (sensorOrientation - deviceDeg + 360) % 360 else (sensorOrientation + deviceDeg) % 360

    /** Normalized point in the rotated (as-streamed/as-displayed) frame → sensor-oriented frame. */
    fun toSensor(x: Float, y: Float, rotation: Int, mirrored: Boolean = false): Pair<Float, Float> {
        val mx = if (mirrored) 1f - x else x
        return when ((rotation % 360 + 360) % 360) {
            90 -> y to 1f - mx
            180 -> 1f - mx to 1f - y
            270 -> 1f - y to mx
            else -> mx to y
        }
    }

    /**
     * Square metering region (left, top, right, bottom) in active-array pixels around a normalized
     * sensor-frame point. The stream is a centered crop of the active array with the stream's
     * aspect ratio (the default crop region).
     */
    fun meteringRegion(
        sx: Float, sy: Float, streamW: Int, streamH: Int, arrayW: Int, arrayH: Int, sizeFraction: Float = 0.1f,
    ): IntArray {
        val streamAspect = streamW.toFloat() / streamH
        val arrayAspect = arrayW.toFloat() / arrayH
        val (visW, visH) = if (streamAspect > arrayAspect) arrayW.toFloat() to arrayW / streamAspect
        else arrayH * streamAspect to arrayH.toFloat()
        val cx = (arrayW - visW) / 2 + sx.coerceIn(0f, 1f) * visW
        val cy = (arrayH - visH) / 2 + sy.coerceIn(0f, 1f) * visH
        val half = (minOf(arrayW, arrayH) * sizeFraction / 2).coerceAtLeast(1f)
        val l = (cx - half).roundToInt().coerceIn(0, arrayW - 1)
        val t = (cy - half).roundToInt().coerceIn(0, arrayH - 1)
        val r = (cx + half).roundToInt().coerceIn(l + 1, arrayW)
        val b = (cy + half).roundToInt().coerceIn(t + 1, arrayH)
        return intArrayOf(l, t, r, b)
    }

    /** Snap a raw OrientationEventListener angle to a quadrant, with ±30° stickiness. */
    fun snapOrientation(deg: Int, current: Int): Int {
        val nearest = ((deg + 45) / 90 % 4) * 90
        val dist = abs(((deg - nearest) % 360 + 540) % 360 - 180)
        return if (nearest != current && dist <= 30) nearest else current
    }
}
