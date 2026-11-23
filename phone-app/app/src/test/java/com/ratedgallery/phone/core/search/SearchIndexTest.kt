package com.ratedgallery.phone.core.search

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.IOException
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.random.Random

class SearchIndexTest {
    @get:Rule val tmp = TemporaryFolder()

    @Test fun float16Conversion() {
        assertEquals(0f, Float16.toFloat(0x0000))
        assertEquals(-0f, Float16.toFloat(0x8000))
        assertEquals(1f, Float16.toFloat(0x3C00))
        assertEquals(-2f, Float16.toFloat(0xC000))
        assertEquals(65504f, Float16.toFloat(0x7BFF))
        assertEquals(0.333251953125f, Float16.toFloat(0x3555))
        assertEquals(5.9604645e-8f, Float16.toFloat(0x0001))      // smallest subnormal
        assertEquals(6.097555e-5f, Float16.toFloat(0x03FF))       // largest subnormal
        assertEquals(Float.POSITIVE_INFINITY, Float16.toFloat(0x7C00))
        assertTrue(Float16.toFloat(0x7E00).isNaN())
        // Round trip of every finite half value.
        for (h in 0 until 65536) {
            val f = Float16.toFloat(h)
            if (f.isNaN()) continue
            assertEquals("h=$h", h, Float16.fromFloat(f).toInt() and 0xFFFF)
        }
    }

    @Test fun parseHandBuiltFile() {
        // Built by hand like ratedgallery/search.py device_blob: b"RGV1" + <II + <i8 ids + <f2 vectors.
        val b = ByteBuffer.allocate(12 + 2 * 8 + 2 * 3 * 2).order(ByteOrder.LITTLE_ENDIAN)
        b.put("RGV1".toByteArray()).putInt(2).putInt(3)
        b.putLong(7).putLong(1L shl 40)
        listOf(0x3C00, 0x0000, 0xBC00, 0x3800, 0x3800, 0x3800).forEach { b.putShort(it.toShort()) }
        val idx = SearchIndex.parse(b.array())
        assertEquals(2, idx.size)
        assertEquals(3, idx.dim)
        assertArrayEquals(longArrayOf(7, 1L shl 40), idx.ids)
        assertArrayEquals(floatArrayOf(1f, 0f, -1f, 0.5f, 0.5f, 0.5f), idx.vectors, 0f)
        assertArrayEquals(floatArrayOf(-1f, 2.5f), idx.similarities(floatArrayOf(1f, 2f, 2f)), 1e-6f)
    }

    @Test fun rejectsBrokenFiles() {
        val good = SearchIndex.encode(longArrayOf(1, 2), 4, FloatArray(8) { it / 8f })
        val f = File(tmp.root, "i.bin")
        f.writeBytes(good)
        assertTrue(SearchIndex.looksValid(f))
        for (bad in listOf(good.copyOf(good.size - 1), good.copyOf(good.size + 2), byteArrayOf(), "XXXX".toByteArray() + good.copyOfRange(4, good.size))) {
            f.writeBytes(bad)
            assertFalse(SearchIndex.looksValid(f))
            try { SearchIndex.parse(bad); fail() } catch (e: IOException) { /* expected */ }
        }
        val empty = SearchIndex.encode(LongArray(0), 512, FloatArray(0))
        assertEquals(0, SearchIndex.parse(empty).size)
    }

    @Test fun rankingCutAndLimit() {
        val ids = longArrayOf(10, 11, 12, 13, 14, 15)
        val sims = floatArrayOf(0.20f, 0.31f, 0.25f, 0.30f, 0.205f, 0.31f)
        // best 0.31 -> cut 0.21; ties keep index order
        assertEquals(listOf(11L, 15L, 13L, 12L), Ranking.rank(sims, ids).map { it.first })
        assertEquals(listOf(11L, 15L), Ranking.rank(sims, ids, limit = 2).map { it.first })
        assertEquals(emptyList<Pair<Long, Float>>(), Ranking.rank(FloatArray(0), LongArray(0)))
        // max 300
        val n = 1000
        val r = Ranking.rank(FloatArray(n) { 0.5f + it * 1e-5f }, LongArray(n) { it.toLong() })
        assertEquals(300, r.size)
        assertEquals(999L, r[0].first)
        assertTrue(r.zipWithNext().all { (a, b) -> a.second >= b.second })
    }

    @Test fun tenThousandPhotosAreFast() {
        val n = 10_000
        val dim = 512
        val rnd = Random(1)
        val vec = FloatArray(n * dim) { rnd.nextFloat() - 0.5f }
        val bytes = SearchIndex.encode(LongArray(n) { it.toLong() }, dim, vec)
        var t0 = System.nanoTime()
        val idx = SearchIndex.parse(bytes)
        val parseMs = (System.nanoTime() - t0) / 1_000_000
        val q = FloatArray(dim) { rnd.nextFloat() - 0.5f }
        repeat(5) { Ranking.rank(idx.similarities(q), idx.ids) } // JIT warm-up
        t0 = System.nanoTime()
        val r = Ranking.rank(idx.similarities(q), idx.ids)
        val ms = (System.nanoTime() - t0) / 1_000_000.0
        println("search index: parse $parseMs ms, score+rank of $n x $dim: $ms ms")
        assertTrue(r.isNotEmpty())
        assertTrue("took $ms ms", ms < 100)
    }
}
