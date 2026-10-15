package com.ares.phonecam

import android.app.Application
import com.ares.phonecam.engine.StreamEngine
import com.ares.phonecam.net.StreamServer

class PhoneCamApp : Application() {
    /** Process-wide pipeline. The UI and the foreground service both talk to this instance. */
    val engine: StreamEngine by lazy { StreamEngine(this) }

    /** Started/stopped by the streaming service. */
    val server: StreamServer by lazy { StreamServer(this, engine) }
}
