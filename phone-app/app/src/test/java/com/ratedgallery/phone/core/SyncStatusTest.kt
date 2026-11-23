package com.ratedgallery.phone.core

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class SyncStatusTest {
    @Test fun bridgeJsonHasContractKeys() {
        val s = SyncStatus(true, "192.168.1.10", 8765, true, false, 1714738930, "upload", 5, 20, "", needsRepair = true)
        val o = JSONObject(s.toJson())
        assertEquals(
            setOf("paired", "host", "port", "connected", "syncing", "lastSync", "phase", "done", "total", "message"),
            o.keys().asSequence().toSet(),
        )
        assertEquals(8765, o.getInt("port"))
        assertEquals(1714738930L, o.getLong("lastSync"))
        assertEquals("upload", o.getString("phase"))
        assertFalse(o.getBoolean("syncing"))
    }
}
