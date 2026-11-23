package com.ratedgallery.phone.core

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class RatingBridgeTest {
    @get:Rule val tmp = TemporaryFolder()

    private var clock = 1714738930123L
    private var changes = 0
    private val queue by lazy { RatingQueue(tmp.root, now = { clock }) }
    private val bridge by lazy { RatingBridge(queue) { changes++ } }

    private fun ok(json: String) = JSONObject(json).getBoolean("ok")

    @Test fun rateReturnsOkAndShowsInPending() {
        assertEquals("""{"ok":true}""", bridge.rate("123", "8"))
        val o = JSONObject(bridge.pendingRatings())
        assertEquals(setOf("123"), o.keys().asSequence().toSet())
        assertEquals(8, o.getJSONObject("123").getInt("rating"))
        assertEquals(1714738930123L, o.getJSONObject("123").getLong("created"))
        assertEquals(1, changes)
    }

    @Test fun numbersThatArriveAsDoubleTextAreAccepted() {
        // Some WebView versions pass a JS number to a String parameter as "123.0".
        assertTrue(ok(bridge.rate("123.0", "8.0")))
        assertTrue(ok(bridge.rate(" 124 ", "10")))
        assertTrue(ok(bridge.rate("1.25e2", "1e0")))
        assertTrue(ok(bridge.rate("9007199254740993", "3"))) // beyond a double: exact long text still works
        val o = JSONObject(bridge.pendingRatings())
        assertEquals(setOf("123", "124", "125", "9007199254740993"), o.keys().asSequence().toSet())
        assertEquals(8, o.getJSONObject("123").getInt("rating"))
        assertEquals(10, o.getJSONObject("124").getInt("rating"))
        assertEquals(1, o.getJSONObject("125").getInt("rating"))
    }

    @Test fun replaceAndRemoveThroughBridge() {
        bridge.rate("5", "4")
        clock += 10
        bridge.rate("5", "9")
        assertEquals(9, JSONObject(bridge.pendingRatings()).getJSONObject("5").getInt("rating"))
        assertEquals("""{"ok":true}""", bridge.rate("5", "0"))
        assertEquals("{}", bridge.pendingRatings())
        assertEquals("""{"ok":true}""", bridge.rate("5", "0")) // removing nothing is fine
    }

    @Test fun badRatingsAreRefused() {
        bridge.rate("1", "6")
        changes = 0
        for (r in listOf("11", "-1", "8.5", "abc", "", null, "NaN", "Infinity", "1e400", "0x8", "8d", "100")) {
            val o = JSONObject(bridge.rate("1", r))
            assertFalse("accepted rating $r", o.getBoolean("ok"))
            assertEquals("rating must be 0..10", o.getString("reason"))
        }
        assertEquals(0, changes)
        assertEquals(6, JSONObject(bridge.pendingRatings()).getJSONObject("1").getInt("rating"))
    }

    @Test fun badPhotoIdsAreRefused() {
        for (id in listOf("0", "-3", "1.5", "", null, "x", "NaN", "1e400", "99999999999999999999")) {
            val o = JSONObject(bridge.rate(id, "5"))
            assertFalse("accepted photo id $id", o.getBoolean("ok"))
            assertEquals("bad photo id", o.getString("reason"))
        }
        assertEquals("{}", bridge.pendingRatings())
        assertFalse(File(tmp.root, RatingQueue.FILE_NAME).exists())
    }

    @Test fun typedOverloadChecksRangeToo() {
        assertTrue(ok(bridge.rate(3L, 10)))
        val bad = JSONObject(bridge.rate(3L, 11))
        assertFalse(bad.getBoolean("ok"))
        assertEquals("rating must be 0..10", bad.getString("reason"))
        assertFalse(ok(bridge.rate(0L, 5)))
    }

    @Test fun saveFailureIsReportedNotThrown() {
        val blocker = File(tmp.root, RatingQueue.FILE_NAME + ".tmp")
        blocker.mkdir()
        File(blocker, "x").writeText("x")
        val o = JSONObject(bridge.rate("1", "5"))
        assertFalse(o.getBoolean("ok"))
        assertTrue(o.getString("reason"), o.getString("reason").startsWith("cannot save"))
        assertEquals(0, changes)
    }

    @Test fun parseWhole() {
        assertEquals(8L, RatingBridge.parseWhole("8"))
        assertEquals(8L, RatingBridge.parseWhole("8.0"))
        assertEquals(-2L, RatingBridge.parseWhole("-2"))
        assertNull(RatingBridge.parseWhole("8.1"))
        assertNull(RatingBridge.parseWhole("-Infinity"))
        assertNull(RatingBridge.parseWhole(null))
    }
}
