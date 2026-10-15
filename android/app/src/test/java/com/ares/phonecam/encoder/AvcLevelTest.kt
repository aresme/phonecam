package com.ares.phonecam.encoder

import android.media.MediaCodecInfo.CodecProfileLevel
import org.junit.Assert.assertEquals
import org.junit.Test

class AvcLevelTest {
    @Test
    fun levelsMatchH264TableA1() {
        assertEquals(CodecProfileLevel.AVCLevel3, VideoEncoder.avcLevelFor(640, 480, 30))
        assertEquals(CodecProfileLevel.AVCLevel31, VideoEncoder.avcLevelFor(1280, 720, 30))
        assertEquals(CodecProfileLevel.AVCLevel32, VideoEncoder.avcLevelFor(1280, 720, 60))
        assertEquals(CodecProfileLevel.AVCLevel4, VideoEncoder.avcLevelFor(1920, 1080, 30))
        assertEquals(CodecProfileLevel.AVCLevel42, VideoEncoder.avcLevelFor(1920, 1080, 60))
    }
}
