package com.ratedgallery.phone.core.search

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.IOException
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.sqrt

class SearchEngineTest {
    @get:Rule val tmp = TemporaryFolder()

    /** Fake text model: every token becomes the one-hot row at (id % 768). */
    private class OneHotModel : TextModel {
        var closed = false
        override fun lastHiddenState(ids: LongArray): FloatArray {
            val h = FloatArray(ids.size * 768)
            ids.forEachIndexed { t, id -> h[t * 768 + (id % 768).toInt()] = 1f }
            return h
        }
        override fun close() { closed = true }
    }

    private val loaded = ArrayList<OneHotModel>()
    private lateinit var files: SearchFiles

    private fun setUpModels() {
        files = SearchFiles(File(tmp.root, "search"))
        files.ensureDir()
        // vocab: [unused0..99], [UNK], [CLS], [SEP], then "sun" (103), "sea" (104)
        val vocab = (List(100) { "[unused$it]" } + listOf("[UNK]", "[CLS]", "[SEP]", "sun", "sea")).joinToString("\n") + "\n"
        files.model("vocab").writeText(vocab)
        // dense: identity on the first 512 inputs
        val b = ByteBuffer.allocate(512 * 768 * 4).order(ByteOrder.LITTLE_ENDIAN)
        for (i in 0 until 512) for (j in 0 until 768) b.putFloat(if (i == j) 1f else 0f)
        files.model("dense").writeBytes(b.array())
        files.model("text").writeBytes(byteArrayOf(1, 2, 3))
        for (n in SearchFiles.MODEL_NAMES) files.setStoredSha(n, "sha-$n")
    }

    private fun unit(vararg idx: Int): FloatArray {
        val v = FloatArray(512)
        idx.forEach { v[it] = 1f }
        val n = sqrt(idx.size.toFloat())
        return FloatArray(512) { v[it] / n }
    }

    private fun writeIndex(ids: LongArray, vecs: List<FloatArray>) {
        val flat = FloatArray(vecs.size * 512)
        vecs.forEachIndexed { i, v -> v.copyInto(flat, i * 512) }
        files.index.writeBytes(SearchIndex.encode(ids, 512, flat))
    }

    private fun engine(fail: Boolean = false) = SearchEngine(files, modelFactory = {
        if (fail) throw IOException("bad model")
        OneHotModel().also { loaded += it }
    })

    @Test fun notDownloadedYet() {
        files = SearchFiles(File(tmp.root, "search"))
        val o = JSONObject(engine().searchJson("sun"))
        assertFalse(o.getBoolean("ok"))
        assertEquals("Search model not downloaded yet. Connect to the PC on Wi-Fi and sync.", o.getString("reason"))
        assertTrue(loaded.isEmpty())
    }

    @Test fun modelButNoIndex() {
        setUpModels()
        val o = JSONObject(engine().searchJson("sun"))
        assertFalse(o.getBoolean("ok"))
        assertEquals(SearchEngine.MSG_NO_INDEX, o.getString("reason"))
    }

    @Test fun modelLoadFailureIsReported() {
        setUpModels()
        writeIndex(longArrayOf(1), listOf(unit(1)))
        val o = JSONObject(engine(fail = true).searchJson("sun"))
        assertFalse(o.getBoolean("ok"))
        assertTrue(o.getString("reason"), o.getString("reason").contains("bad model"))
    }

    @Test fun okResultsBestFirstWithCut() {
        setUpModels()
        // "sun" -> ids [101, 103, 102] -> v = unit(101, 102, 103)
        writeIndex(
            longArrayOf(5, 6, 7, 8),
            listOf(unit(103), unit(101, 102, 103), unit(7), unit(101, 102, 103, 104)),
        )
        val e = engine()
        val o = JSONObject(e.searchJson("  sun "))
        assertTrue(o.getBoolean("ok"))
        val r = o.getJSONArray("results")
        // sims: 6 -> 1.0, 8 -> 0.866, 5 -> 0.577, 7 -> 0; cut = 0.9
        assertEquals(1, r.length())
        assertEquals(6L, r.getJSONArray(0).getLong(0))
        assertEquals(1.0, r.getJSONArray(0).getDouble(1), 1e-3)
        assertEquals(1, loaded.size)

        // A new index file is picked up; the model stays loaded.
        val near = FloatArray(512).also { v -> listOf(101, 102, 103).forEach { v[it] = 1f }; v[104] = 0.3f }
        val nn = sqrt(near.sumOf { (it * it).toDouble() }).toFloat()
        writeIndex(longArrayOf(9, 10, 11), listOf(FloatArray(512) { near[it] / nn }, unit(101, 102, 103), unit(103)))
        e.invalidateIndex()
        val r2 = JSONObject(e.searchJson("sun")).getJSONArray("results")
        assertEquals(2, r2.length())
        assertEquals(10L, r2.getJSONArray(0).getLong(0))
        assertEquals(9L, r2.getJSONArray(1).getLong(0))
        assertEquals(0.985, r2.getJSONArray(1).getDouble(1), 1e-3) // 3 / (sqrt(3) * sqrt(3.09))
        assertEquals(1, loaded.size)

        // A new model file (new sha) reloads the model and closes the old session.
        files.setStoredSha("text", "sha-new")
        assertTrue(JSONObject(e.searchJson("sun")).getBoolean("ok"))
        assertEquals(2, loaded.size)
        assertTrue(loaded[0].closed)
    }

    @Test fun emptyQueryIsOkAndEmpty() {
        setUpModels()
        writeIndex(longArrayOf(1), listOf(unit(1)))
        assertEquals("""{"ok":true,"results":[]}""", engine().searchJson("   "))
    }

    @Test fun jsonFormat() {
        assertEquals(
            """{"ok":true,"results":[[12,0.3123],[3,-0.0001]]}""",
            SearchEngine.toJson(SearchEngine.Result.Ok(listOf(12L to 0.31234f, 3L to -0.00009f))),
        )
        val o = JSONObject(SearchEngine.toJson(SearchEngine.Result.NotReady("x \"y\"")))
        assertEquals(setOf("ok", "reason"), o.keys().asSequence().toSet())
        assertEquals("x \"y\"", o.getString("reason"))
    }
}
