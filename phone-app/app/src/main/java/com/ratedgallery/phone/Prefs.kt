package com.ratedgallery.phone

import android.content.Context
import androidx.core.content.edit
import com.ratedgallery.phone.core.OkHttpDeviceApi

/** Pairing data in app-private SharedPreferences. */
class Prefs(context: Context) {
    private val sp = context.applicationContext.getSharedPreferences("pairing", Context.MODE_PRIVATE)

    val host: String get() = sp.getString(KEY_HOST, "") ?: ""
    val port: Int get() = sp.getInt(KEY_PORT, DEFAULT_PORT)
    val token: String? get() = sp.getString(KEY_TOKEN, null)
    val serverName: String get() = sp.getString(KEY_SERVER, "") ?: ""
    val deviceId: Long get() = sp.getLong(KEY_DEVICE, 0)
    val needsRepair: Boolean get() = sp.getBoolean(KEY_REPAIR, false)
    var lastSync: Long
        get() = sp.getLong(KEY_LAST_SYNC, 0)
        set(v) = sp.edit { putLong(KEY_LAST_SYNC, v) }

    val paired: Boolean get() = !token.isNullOrEmpty() && host.isNotEmpty() && !needsRepair

    fun savePairing(host: String, port: Int, token: String, deviceId: Long, serverName: String) = sp.edit {
        putString(KEY_HOST, host)
        putInt(KEY_PORT, port)
        putString(KEY_TOKEN, token)
        putLong(KEY_DEVICE, deviceId)
        putString(KEY_SERVER, serverName)
        putBoolean(KEY_REPAIR, false)
    }

    fun saveServerName(name: String) = sp.edit { putString(KEY_SERVER, name) }

    /** After a 401: forget the token, keep host/port to prefill the form, show "Pair again". */
    fun markNeedsRepair() = sp.edit {
        remove(KEY_TOKEN)
        putBoolean(KEY_REPAIR, true)
    }

    fun unpair() = sp.edit {
        remove(KEY_TOKEN)
        remove(KEY_DEVICE)
        remove(KEY_SERVER)
        putBoolean(KEY_REPAIR, false)
    }

    /** API client for the paired PC, or null if not paired. */
    fun api(): OkHttpDeviceApi? {
        val t = token
        if (t.isNullOrEmpty() || host.isEmpty() || needsRepair) return null
        return OkHttpDeviceApi(host, port, t)
    }

    companion object {
        const val DEFAULT_PORT = 8765
        private const val KEY_HOST = "host"
        private const val KEY_PORT = "port"
        private const val KEY_TOKEN = "token"
        private const val KEY_SERVER = "server_name"
        private const val KEY_DEVICE = "device_id"
        private const val KEY_REPAIR = "needs_repair"
        private const val KEY_LAST_SYNC = "last_sync"
    }
}
