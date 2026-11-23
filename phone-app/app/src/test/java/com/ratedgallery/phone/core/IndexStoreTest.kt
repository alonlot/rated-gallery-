package com.ratedgallery.phone.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.IOException

class IndexStoreTest {
    @get:Rule val tmp = TemporaryFolder()

    private val good = """{"version":1,"persons":[],"photos":[{"id":1,"mid":"5"}]}"""

    @Test fun emptyWhenMissing() {
        assertEquals(IndexStore.EMPTY, IndexStore(tmp.root).readOrEmpty())
    }

    @Test fun savesAndReplaces() {
        val s = IndexStore(tmp.root)
        s.saveString(good)
        assertEquals(good, s.readOrEmpty())
        val newer = """{"version":1,"persons":[],"photos":[]}"""
        s.saveString(newer)
        assertEquals(newer, s.readOrEmpty())
        assertEquals(listOf("index.json"), tmp.root.list()!!.toList())
    }

    @Test fun failedDownloadKeepsOldIndex() {
        val s = IndexStore(tmp.root)
        s.saveString(good)
        try {
            s.save { out ->
                out.write("{\"version\":1,\"pho".toByteArray())
                throw IOException("connection reset")
            }
            fail("expected IOException")
        } catch (e: IOException) {
            assertEquals("connection reset", e.message)
        }
        assertEquals(good, s.readOrEmpty())
        assertFalse(File(tmp.root, "index.json.tmp").exists())
    }

    @Test fun invalidJsonIsRejected() {
        val s = IndexStore(tmp.root)
        s.saveString(good)
        try {
            s.saveString("<html>error</html>")
            fail("expected IOException")
        } catch (e: IOException) { /* ok */ }
        assertEquals(good, s.readOrEmpty())
        assertFalse(File(tmp.root, "index.json.tmp").exists())
    }

    @Test fun createsFolder() {
        val dir = File(tmp.root, "a/b")
        IndexStore(dir).saveString(good)
        assertTrue(File(dir, "index.json").isFile)
    }
}
