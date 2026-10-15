package com.ares.phonecam.engine

import android.content.Context
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

private val Context.dataStore by preferencesDataStore(name = "settings")

/** Last-used stream settings, restored at startup (values are validated by the engine). */
data class SavedSettings(
    val cameraId: String? = null,
    val width: Int? = null,
    val height: Int? = null,
    val fps: Int? = null,
    val bitrate: Int? = null,
    val orientation: String? = null,
    val keepScreenOn: Boolean = false,
    val transport: String = "usb",
    val requirePairing: Boolean = true,
    val pairingCode: String? = null,
)

class SettingsRepository(context: Context) {
    private val store = context.applicationContext.dataStore

    private object Keys {
        val camera = stringPreferencesKey("camera_id")
        val width = intPreferencesKey("width")
        val height = intPreferencesKey("height")
        val fps = intPreferencesKey("fps")
        val bitrate = intPreferencesKey("bitrate")
        val orientation = stringPreferencesKey("orientation")
        val keepScreenOn = booleanPreferencesKey("keep_screen_on")
        val transport = stringPreferencesKey("transport")
        val requirePairing = booleanPreferencesKey("require_pairing")
        val pairingCode = stringPreferencesKey("pairing_code")
    }

    val settings: Flow<SavedSettings> = store.data.map { it.toSettings() }

    suspend fun load(): SavedSettings = settings.first()

    suspend fun saveStream(config: StreamConfig, orientation: String) {
        store.edit {
            it[Keys.camera] = config.cameraId
            it[Keys.width] = config.width
            it[Keys.height] = config.height
            it[Keys.fps] = config.fps
            it[Keys.bitrate] = config.bitrate
            it[Keys.orientation] = orientation
        }
    }

    suspend fun setKeepScreenOn(on: Boolean) {
        store.edit { it[Keys.keepScreenOn] = on }
    }

    suspend fun setTransport(transport: String) {
        store.edit { it[Keys.transport] = transport }
    }

    suspend fun setRequirePairing(on: Boolean) {
        store.edit { it[Keys.requirePairing] = on }
    }

    /** The Wi-Fi pairing code, created on first use. */
    suspend fun pairingCode(): String {
        load().pairingCode?.let { return it }
        return newPairingCode()
    }

    suspend fun newPairingCode(): String {
        val code = "%04d".format(java.security.SecureRandom().nextInt(10_000))
        store.edit { it[Keys.pairingCode] = code }
        return code
    }

    private fun Preferences.toSettings() = SavedSettings(
        cameraId = this[Keys.camera],
        width = this[Keys.width],
        height = this[Keys.height],
        fps = this[Keys.fps],
        bitrate = this[Keys.bitrate],
        orientation = this[Keys.orientation],
        keepScreenOn = this[Keys.keepScreenOn] ?: false,
        transport = this[Keys.transport] ?: "usb",
        requirePairing = this[Keys.requirePairing] ?: true,
        pairingCode = this[Keys.pairingCode],
    )
}
