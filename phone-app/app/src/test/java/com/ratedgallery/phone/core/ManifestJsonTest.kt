package com.ratedgallery.phone.core

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ManifestJsonTest {
    @Test fun buildsContractShape() {
        val json = ManifestJson.build(
            listOf(
                MediaItem("1234", "IMG_20240503_142210.jpg", 3456789, 1714738930, 1714738930123, "image/jpeg", "DCIM/Camera/"),
                MediaItem("1240", "x\"y.heic", 10, 5, null, "image/heic", null),
            ),
            full = true,
        )
        val o = JSONObject(json)
        assertEquals(true, o.getBoolean("full"))
        val items = o.getJSONArray("items")
        assertEquals(2, items.length())
        val a = items.getJSONObject(0)
        assertEquals("1234", a.get("mid")) // a string, not a number
        assertEquals("IMG_20240503_142210.jpg", a.getString("name"))
        assertEquals(3456789L, a.getLong("size"))
        assertEquals(1714738930L, a.getLong("mtime"))
        assertEquals(1714738930123L, a.getLong("taken"))
        assertEquals("image/jpeg", a.getString("mime"))
        assertEquals("DCIM/Camera/", a.getString("path"))
        val b = items.getJSONObject(1)
        assertEquals("x\"y.heic", b.getString("name"))
        assertTrue("taken must be present as null", b.has("taken") && b.isNull("taken"))
        assertTrue(b.has("path") && b.isNull("path"))
    }

    @Test fun emptyManifest() {
        val o = JSONObject(ManifestJson.build(emptyList()))
        assertEquals(0, o.getJSONArray("items").length())
        assertEquals(true, o.getBoolean("full"))
    }
}
