package com.ares.phonecam

import android.app.Application
import com.ares.phonecam.engine.StreamEngine

class PhoneCamApp : Application() {
    /** Process-wide pipeline. The UI and the foreground service both talk to this instance. */
    val engine: StreamEngine by lazy { StreamEngine(this) }
}
