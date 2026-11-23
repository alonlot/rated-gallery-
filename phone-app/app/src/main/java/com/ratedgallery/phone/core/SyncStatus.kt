package com.ratedgallery.phone.core

import org.json.JSONObject

/** The status object returned by RGBridge.getStatus() (API.md section 5). */
data class SyncStatus(
    val paired: Boolean = false,
    val host: String = "",
    val port: Int = 0,
    val connected: Boolean = false,
    val syncing: Boolean = false,
    /** Unix epoch seconds of the last complete sync, 0 if never. */
    val lastSync: Long = 0,
    val phase: String = Phase.IDLE,
    val done: Int = 0,
    val total: Int = 0,
    val message: String = "",
    /** Not in the bridge JSON: true after a 401, the UI shows "Pair again". */
    val needsRepair: Boolean = false,
) {
    fun toJson(): String {
        val o = JSONObject()
        o.put("paired", paired)
        o.put("host", host)
        o.put("port", port)
        o.put("connected", connected)
        o.put("syncing", syncing)
        o.put("lastSync", lastSync)
        o.put("phase", phase)
        o.put("done", done)
        o.put("total", total)
        o.put("message", message)
        return o.toString()
    }

    object Phase {
        const val IDLE = "idle"
        const val CONNECT = "connect"
        const val MANIFEST = "manifest"
        const val UPLOAD = "upload"
        const val PROCESSING = "processing"
        const val INDEX = "index"
        /** Search index and search model download (done/total in MB for the model). */
        const val SEARCH = "search"
    }
}
