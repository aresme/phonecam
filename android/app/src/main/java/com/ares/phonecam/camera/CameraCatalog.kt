package com.ares.phonecam.camera

import android.graphics.SurfaceTexture
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CameraMetadata
import android.media.MediaCodec
import android.util.Log
import android.util.Range
import android.util.Rational
import android.util.Size
import com.ares.phonecam.TAG

enum class Facing(val wire: String) { BACK("back"), FRONT("front"), EXTERNAL("external") }

data class CameraInfo(
    val id: String,
    val facing: Facing,
    val sensorOrientation: Int,
    /** Sizes usable for encoding (camera MediaCodec output), landscape, largest first, capped at 1080p. */
    val sizes: List<Size>,
    val previewSizes: List<Size>,
    val fpsRanges: List<Range<Int>>,
    val hasFlash: Boolean,
    val afModes: List<Int>,
    val aeCompRange: Range<Int>,
    val aeCompStep: Rational,
    /** SENSOR_INFO_TIMESTAMP_SOURCE == REALTIME: sensor timestamps are elapsedRealtimeNanos. */
    val timestampRealtime: Boolean,
    val hardwareLevel: Int,
    val activeArray: android.graphics.Rect,
    val maxAfRegions: Int,
    val maxAeRegions: Int,
) {
    val afModeNames: List<String> get() = afModes.mapNotNull(::afModeName)

    /** Exposure compensation in EV for an index, and the nearest index for an EV value. */
    fun evForIndex(index: Int): Float = index * aeCompStep.toFloat()
    fun indexForEv(ev: Float): Int =
        Math.round(ev / aeCompStep.toFloat()).coerceIn(aeCompRange.lower, aeCompRange.upper)

    /** Best AE fps range for a target fps: fixed [fps,fps] if possible, else highest floor with that ceiling. */
    fun fpsRangeFor(fps: Int): Range<Int> =
        fpsRanges.firstOrNull { it.lower == fps && it.upper == fps }
            ?: fpsRanges.filter { it.upper == fps }.maxByOrNull { it.lower }
            ?: fpsRanges.minBy { kotlin.math.abs(it.upper - fps) * 100 - it.lower }

    val supportedFps: List<Int> get() = fpsRanges.map { it.upper }.distinct().sorted()
}

/** Protocol names for CONTROL_AF_MODE values. */
fun afModeName(mode: Int): String? = when (mode) {
    CameraMetadata.CONTROL_AF_MODE_OFF -> "off"
    CameraMetadata.CONTROL_AF_MODE_AUTO -> "auto"
    CameraMetadata.CONTROL_AF_MODE_MACRO -> "macro"
    CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_VIDEO -> "continuous-video"
    CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_PICTURE -> "continuous-picture"
    else -> null
}

fun afModeValue(name: String): Int? = when (name) {
    "off" -> CameraMetadata.CONTROL_AF_MODE_OFF
    "auto", "locked" -> CameraMetadata.CONTROL_AF_MODE_AUTO
    "macro" -> CameraMetadata.CONTROL_AF_MODE_MACRO
    "continuous-video" -> CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_VIDEO
    "continuous-picture" -> CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_PICTURE
    else -> null
}

object CameraCatalog {
    private const val MAX_W = 1920
    private const val MAX_H = 1080

    fun query(manager: CameraManager): List<CameraInfo> = manager.cameraIdList.mapNotNull { id ->
        try {
            describe(id, manager.getCameraCharacteristics(id))
        } catch (e: Exception) {
            Log.w(TAG, "skipping camera $id", e)
            null
        }
    }

    private fun describe(id: String, c: CameraCharacteristics): CameraInfo? {
        val caps = c.get(CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES) ?: intArrayOf()
        if (CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_BACKWARD_COMPATIBLE !in caps) return null
        val map = c.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP) ?: return null
        fun usable(sizes: Array<Size>?) = sizes.orEmpty()
            .filter { it.width in 1..MAX_W && it.height in 1..MAX_H && it.width >= it.height && it.width % 2 == 0 && it.height % 2 == 0 }
            .sortedByDescending { it.width * it.height }
        val facing = when (c.get(CameraCharacteristics.LENS_FACING)) {
            CameraMetadata.LENS_FACING_FRONT -> Facing.FRONT
            CameraMetadata.LENS_FACING_BACK -> Facing.BACK
            else -> Facing.EXTERNAL
        }
        val info = CameraInfo(
            id = id,
            facing = facing,
            sensorOrientation = c.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 0,
            sizes = usable(map.getOutputSizes(MediaCodec::class.java)),
            previewSizes = usable(map.getOutputSizes(SurfaceTexture::class.java)),
            fpsRanges = c.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES).orEmpty().toList(),
            hasFlash = c.get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true,
            afModes = c.get(CameraCharacteristics.CONTROL_AF_AVAILABLE_MODES)?.toList().orEmpty(),
            aeCompRange = c.get(CameraCharacteristics.CONTROL_AE_COMPENSATION_RANGE) ?: Range(0, 0),
            aeCompStep = c.get(CameraCharacteristics.CONTROL_AE_COMPENSATION_STEP) ?: Rational(1, 1),
            timestampRealtime = c.get(CameraCharacteristics.SENSOR_INFO_TIMESTAMP_SOURCE) ==
                CameraMetadata.SENSOR_INFO_TIMESTAMP_SOURCE_REALTIME,
            hardwareLevel = c.get(CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL) ?: -1,
            activeArray = c.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE) ?: android.graphics.Rect(0, 0, 1, 1),
            maxAfRegions = c.get(CameraCharacteristics.CONTROL_MAX_REGIONS_AF) ?: 0,
            maxAeRegions = c.get(CameraCharacteristics.CONTROL_MAX_REGIONS_AE) ?: 0,
        )
        Log.i(TAG, "camera $id ${info.facing} orient=${info.sensorOrientation} level=${info.hardwareLevel} " +
            "fps=${info.fpsRanges} realtimeTs=${info.timestampRealtime} sizes=${info.sizes.take(8)}")
        return info.takeIf { it.sizes.isNotEmpty() && it.fpsRanges.isNotEmpty() }
    }
}
