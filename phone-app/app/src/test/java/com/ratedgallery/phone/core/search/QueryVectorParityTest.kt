package com.ratedgallery.phone.core.search

import org.json.JSONArray
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/** The full query vector through the production code path (WordPiece + OrtTextModel + QueryEncoder) vs Python. */
class QueryVectorParityTest {
    @Test fun matchesPythonVectors() {
        val cases = File(TestData.data, "mclip_query_vectors.json")
        val model = File(TestData.models, "mclip_text_q8.onnx")
        val dense = File(TestData.models, "mclip_dense.f32")
        val vocab = File(TestData.models, "mclip_vocab.txt")
        TestData.require(cases, model, dense, vocab)

        val t0 = System.nanoTime()
        OrtTextModel(model).use { m ->
            val enc = QueryEncoder(WordPiece.load(vocab), m, QueryEncoder.loadDense(dense))
            println("parity: model loaded in ${(System.nanoTime() - t0) / 1_000_000} ms")
            val arr = JSONArray(cases.readText())
            assertTrue(arr.length() > 0)
            for (i in 0 until arr.length()) {
                val c = arr.getJSONObject(i)
                val q = c.getString("q")
                val ids = c.getJSONArray("ids").let { a -> LongArray(a.length()) { a.getLong(it) } }
                val want = c.getJSONArray("vec").let { a -> FloatArray(a.length()) { a.getDouble(it).toFloat() } }
                assertArrayEquals("ids of $q", ids, enc.tokenize(q))
                val t1 = System.nanoTime()
                val got = enc.embed(q)
                val ms = (System.nanoTime() - t1) / 1_000_000
                assertEquals(512, got.size)
                var dot = 0.0; var na = 0.0; var nb = 0.0
                for (j in got.indices) { dot += got[j] * want[j]; na += got[j] * got[j]; nb += want[j] * want[j] }
                val cos = dot / Math.sqrt(na * nb)
                println("parity: cosine %.6f, %d ms: %s".format(cos, ms, q))
                assertTrue("cosine $cos for $q", cos >= 0.999)
                assertEquals(1.0, Math.sqrt(na), 1e-4)
            }
        }
    }
}
