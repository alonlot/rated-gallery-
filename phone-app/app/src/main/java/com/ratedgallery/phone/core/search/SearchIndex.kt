package com.ratedgallery.phone.core.search

import java.io.File
import java.io.IOException
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * The search index of API.md section 6 (`GET /api/device/search-index`), little-endian:
 * "RGV1", uint32 n, uint32 dim, n int64 photo ids, n*dim float16 vectors (row per photo).
 * The vectors are converted to float32 once at load.
 */
class SearchIndex(val ids: LongArray, val dim: Int, val vectors: FloatArray) {
    val size: Int get() = ids.size

    /** Dot product of every photo vector with [q] (length [dim]). */
    fun similarities(q: FloatArray): FloatArray {
        require(q.size == dim) { "query has ${q.size} dims, index has $dim" }
        val n = ids.size
        val out = FloatArray(n)
        val v = vectors
        var off = 0
        for (i in 0 until n) {
            var s = 0f
            for (j in 0 until dim) s += v[off + j] * q[j]
            out[i] = s
            off += dim
        }
        return out
    }

    companion object {
        private val MAGIC = byteArrayOf('R'.code.toByte(), 'G'.code.toByte(), 'V'.code.toByte(), '1'.code.toByte())
        const val HEADER = 12

        @Throws(IOException::class)
        fun parse(bytes: ByteArray): SearchIndex {
            if (bytes.size < HEADER || !bytes.copyOfRange(0, 4).contentEquals(MAGIC)) {
                throw IOException("not a search index (bad magic)")
            }
            val b = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN)
            val n = b.getInt(4).toLong() and 0xFFFFFFFFL
            val dim = b.getInt(8).toLong() and 0xFFFFFFFFL
            if (dim <= 0 || dim > 4096) throw IOException("bad search index dim $dim")
            val expected = HEADER + n * 8 + n * dim * 2
            if (expected != bytes.size.toLong()) {
                throw IOException("search index size ${bytes.size}, expected $expected")
            }
            val ni = n.toInt()
            val d = dim.toInt()
            b.position(HEADER)
            val ids = LongArray(ni)
            b.asLongBuffer().get(ids)
            b.position(HEADER + ni * 8)
            val halfs = b.asShortBuffer()
            val vec = FloatArray(ni * d)
            val table = Float16.table
            for (i in vec.indices) vec[i] = table[halfs.get(i).toInt() and 0xFFFF]
            return SearchIndex(ids, d, vec)
        }

        @Throws(IOException::class)
        fun load(file: File): SearchIndex = parse(file.readBytes())

        /** Checks the header and the file size (used before a downloaded index replaces the old one). */
        fun looksValid(file: File): Boolean = try {
            RandomAccessFile(file, "r").use { f ->
                val head = ByteArray(HEADER)
                f.readFully(head)
                if (!head.copyOfRange(0, 4).contentEquals(MAGIC)) return false
                val b = ByteBuffer.wrap(head).order(ByteOrder.LITTLE_ENDIAN)
                val n = b.getInt(4).toLong() and 0xFFFFFFFFL
                val dim = b.getInt(8).toLong() and 0xFFFFFFFFL
                dim in 1..4096 && f.length() == HEADER + n * 8 + n * dim * 2
            }
        } catch (e: IOException) {
            false
        }

        /** Builds the binary format (tests and tools). */
        fun encode(ids: LongArray, dim: Int, vectors: FloatArray): ByteArray {
            require(vectors.size == ids.size * dim)
            val b = ByteBuffer.allocate(HEADER + ids.size * 8 + vectors.size * 2).order(ByteOrder.LITTLE_ENDIAN)
            b.put(MAGIC).putInt(ids.size).putInt(dim)
            ids.forEach { b.putLong(it) }
            vectors.forEach { b.putShort(Float16.fromFloat(it)) }
            return b.array()
        }
    }
}

/** IEEE 754 half precision conversion (android.util.Half is not on the JVM, Float.float16ToFloat is Java 20+). */
object Float16 {
    /** All 65536 float16 values as float32 (256 KB, built once). */
    val table: FloatArray by lazy { FloatArray(65536) { toFloat(it) } }

    fun toFloat(h: Int): Float {
        val sign = (h ushr 15) and 1
        val exp = (h ushr 10) and 0x1F
        val mant = h and 0x3FF
        val bits = when {
            exp == 0 && mant == 0 -> sign shl 31
            exp == 0 -> { // subnormal: normalize
                var m = mant
                var e = -14
                while (m and 0x400 == 0) {
                    m = m shl 1
                    e--
                }
                (sign shl 31) or ((e + 127) shl 23) or ((m and 0x3FF) shl 13)
            }
            exp == 31 -> (sign shl 31) or (0xFF shl 23) or (mant shl 13)
            else -> (sign shl 31) or ((exp - 15 + 127) shl 23) or (mant shl 13)
        }
        return java.lang.Float.intBitsToFloat(bits)
    }

    /** Round-to-nearest-even float32 -> float16 (only used to build test data). */
    fun fromFloat(f: Float): Short {
        val bits = java.lang.Float.floatToIntBits(f)
        val sign = (bits ushr 16) and 0x8000
        val exp = (bits ushr 23) and 0xFF
        var mant = bits and 0x7FFFFF
        if (exp == 0xFF) return (sign or 0x7C00 or (if (mant != 0) 0x200 else 0)).toShort()
        var e = exp - 127 + 15
        if (e >= 31) return (sign or 0x7C00).toShort()
        if (e <= 0) {
            if (e < -10) return sign.toShort()
            mant = mant or 0x800000
            val shift = 14 - e
            var h = mant ushr shift
            val rem = mant and ((1 shl shift) - 1)
            val half = 1 shl (shift - 1)
            if (rem > half || (rem == half && (h and 1) == 1)) h++
            return (sign or h).toShort()
        }
        var h = (e shl 10) or (mant ushr 13)
        val rem = mant and 0x1FFF
        if (rem > 0x1000 || (rem == 0x1000 && (h and 1) == 1)) h++
        return (sign or h).toShort()
    }
}

/** Step 4 of API.md section 6: best first, at most [limit], only sim >= best - [margin]. */
object Ranking {
    const val LIMIT = 300
    const val MARGIN = 0.10f

    fun rank(sims: FloatArray, ids: LongArray, limit: Int = LIMIT, margin: Float = MARGIN): List<Pair<Long, Float>> {
        if (sims.isEmpty()) return emptyList()
        var best = Float.NEGATIVE_INFINITY
        for (s in sims) if (s > best) best = s
        val cut = best - margin
        // Results within the margin are a prefix of the sorted order: sort only those.
        var count = 0
        for (s in sims) if (s >= cut) count++
        val idx = IntArray(count)
        var k = 0
        for (i in sims.indices) if (sims[i] >= cut) idx[k++] = i
        val sorted = idx.sortedWith { a, b ->
            val c = sims[b].compareTo(sims[a])
            if (c != 0) c else a.compareTo(b)
        }
        return sorted.take(limit).map { ids[it] to sims[it] }
    }
}
