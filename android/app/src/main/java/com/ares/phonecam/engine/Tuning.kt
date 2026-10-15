package com.ares.phonecam.engine

import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraMetadata
import org.json.JSONObject

/**
 * Low-level latency knobs. Defaults are the tuned values (docs/latency.md); the experimental
 * `set_tuning` control command changes them at runtime for A/B measurements.
 */
data class Tuning(
    /** Camera request template: "record" or "preview". */
    val template: String = "preview",
    /** NOISE_REDUCTION_MODE: "default" (template's choice), "off", "fast", "hq", "minimal". */
    val noiseReduction: String = "fast",
    /** EDGE_MODE: "default", "off", "fast". */
    val edge: String = "fast",
    /** Force CONTROL_VIDEO_STABILIZATION_MODE off (EIS adds buffering). */
    val stabilizationOff: Boolean = true,
    /**
     * H.264 profile: "auto" (constrained baseline if offered, else baseline), "baseline", "main",
     * "high". Main (no B-frames) measured best: smallest stream, fastest software decode on the PC,
     * and VAAPI-decodable (plain Baseline is not).
     */
    val profile: String = "main",
    /** KEY_OPERATING_RATE: 0 = unset, -1 = Short.MAX_VALUE ("as fast as possible"), else fps. -1 saved ~3-4 ms of encode time. */
    val operatingRate: Int = -1,
    /** Extra integer MediaFormat keys (vendor parameters), e.g. {"vendor.mtk.ext.venc.lowlatency": 1}. */
    val vendor: Map<String, Int> = emptyMap(),
) {
    val templateValue get() = if (template == "record") CameraDevice.TEMPLATE_RECORD else CameraDevice.TEMPLATE_PREVIEW

    val noiseReductionValue: Int? get() = when (noiseReduction) {
        "off" -> CameraMetadata.NOISE_REDUCTION_MODE_OFF
        "fast" -> CameraMetadata.NOISE_REDUCTION_MODE_FAST
        "hq" -> CameraMetadata.NOISE_REDUCTION_MODE_HIGH_QUALITY
        "minimal" -> CameraMetadata.NOISE_REDUCTION_MODE_MINIMAL
        else -> null
    }

    val edgeValue: Int? get() = when (edge) {
        "off" -> CameraMetadata.EDGE_MODE_OFF
        "fast" -> CameraMetadata.EDGE_MODE_FAST
        else -> null
    }

    fun toJson(): JSONObject = JSONObject()
        .put("template", template).put("noise_reduction", noiseReduction).put("edge", edge)
        .put("stabilization_off", stabilizationOff).put("profile", profile)
        .put("operating_rate", operatingRate).put("vendor", JSONObject(vendor as Map<*, *>))

    /** Returns a copy with the keys present in [o] changed. */
    fun merge(o: JSONObject): Tuning {
        val t = copy(
            template = o.optString("template", template),
            noiseReduction = o.optString("noise_reduction", noiseReduction),
            edge = o.optString("edge", edge),
            stabilizationOff = o.optBoolean("stabilization_off", stabilizationOff),
            profile = o.optString("profile", profile),
            operatingRate = o.optInt("operating_rate", operatingRate),
            vendor = o.optJSONObject("vendor")?.let { v -> v.keys().asSequence().associateWith { v.getInt(it) } } ?: vendor,
        )
        require(t.template in listOf("record", "preview")) { "template must be record or preview" }
        require(t.noiseReduction in listOf("default", "off", "fast", "hq", "minimal")) { "bad noise_reduction" }
        require(t.edge in listOf("default", "off", "fast")) { "bad edge" }
        require(t.profile in listOf("auto", "baseline", "main", "high")) { "bad profile" }
        return t
    }
}
