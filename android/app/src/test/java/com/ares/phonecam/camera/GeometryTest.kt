package com.ares.phonecam.camera

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test

class GeometryTest {
    @Test
    fun rotationForTypicalPhone() {
        // Back sensor 90°, front 270°. Landscape with top to the left (deviceDeg 270): upright.
        assertEquals(0, Geometry.streamRotation(90, false, 270))
        assertEquals(0, Geometry.streamRotation(270, true, 270))
        // Portrait.
        assertEquals(90, Geometry.streamRotation(90, false, 0))
        assertEquals(270, Geometry.streamRotation(270, true, 0))
        // Landscape, top to the right.
        assertEquals(180, Geometry.streamRotation(90, false, 90))
    }

    private fun assertPoint(ex: Float, ey: Float, p: Pair<Float, Float>) {
        assertEquals(ex, p.first, 1e-6f)
        assertEquals(ey, p.second, 1e-6f)
    }

    @Test
    fun toSensorInvertsClockwiseRotation() {
        // Sensor top-left corner (0,0). After a 90° clockwise rotation it is at the top-right.
        assertPoint(0f, 0f, Geometry.toSensor(1f, 0f, 90))
        assertPoint(0f, 0f, Geometry.toSensor(1f, 1f, 180))
        assertPoint(0f, 0f, Geometry.toSensor(0f, 1f, 270))
        assertPoint(0.25f, 0.75f, Geometry.toSensor(0.25f, 0.75f, 0))
        assertPoint(0.75f, 0.75f, Geometry.toSensor(0.25f, 0.75f, 0, mirrored = true))
    }

    @Test
    fun meteringRegionMapsCropOfActiveArray() {
        // 16:9 stream from a 4:3 array 4000x3000: visible band is 4000x2250 starting at y=375.
        assertArrayEquals(intArrayOf(1850, 1350, 2150, 1650), Geometry.meteringRegion(0.5f, 0.5f, 1280, 720, 4000, 3000))
        val corner = Geometry.meteringRegion(0f, 0f, 1280, 720, 4000, 3000)
        assertEquals(0, corner[0])
        assertEquals(375 - 150, corner[1])
    }

    @Test
    fun snapHasHysteresis() {
        assertEquals(270, Geometry.snapOrientation(265, 0))
        assertEquals(270, Geometry.snapOrientation(300, 0)) // exactly 30° from 270: snaps
        assertEquals(0, Geometry.snapOrientation(310, 0)) // 40° from 270, 50° from 0: stay
        assertEquals(0, Geometry.snapOrientation(355, 270))
    }
}
