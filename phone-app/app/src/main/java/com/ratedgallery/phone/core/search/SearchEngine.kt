package com.ratedgallery.phone.core.search

import org.json.JSONObject
import java.io.File

/**
 * Offline text search (API.md section 6). The model is loaded lazily on the first search and kept
 * open; it is reloaded when a sync replaces a model file. The search index is reloaded when the file changes.
 * Thread-safe (calls are serialized).
 */
class SearchEngine(
    private val files: SearchFiles,
    private val modelFactory: (File) -> TextModel = { OrtTextModel(it) },
    private val log: (String) -> Unit = {},
    private val clock: () -> Long = System::nanoTime,
) {
    private var encoder: QueryEncoder? = null
    private var model: TextModel? = null
    private var modelStamp: String? = null
    private var index: SearchIndex? = null
    private var indexStamp: Pair<Long, Long>? = null

    sealed class Result {
        data class Ok(val results: List<Pair<Long, Float>>) : Result()
        data class NotReady(val reason: String) : Result()
    }

    @Synchronized
    fun search(query: String, limit: Int = Ranking.LIMIT): Result {
        val q = query.trim()
        if (!files.modelsPresent()) return Result.NotReady(MSG_NO_MODEL)
        if (!files.index.isFile) return Result.NotReady(MSG_NO_INDEX)
        if (q.isEmpty()) return Result.Ok(emptyList())
        val enc = try {
            encoder()
        } catch (e: Exception) {
            log("search model load failed: $e")
            return Result.NotReady("Search model could not be loaded (${e.message ?: e.javaClass.simpleName}). Sync again on Wi-Fi.")
        }
        val idx = try {
            index()
        } catch (e: Exception) {
            log("search index load failed: $e")
            return Result.NotReady(MSG_NO_INDEX)
        }
        if (idx.dim != enc.outDim) return Result.NotReady(MSG_NO_INDEX)
        val t0 = clock()
        val v = try {
            enc.embed(q)
        } catch (e: Exception) {
            log("search query failed: $e")
            return Result.NotReady("Search failed (${e.message ?: e.javaClass.simpleName}).")
        }
        val t1 = clock()
        val res = Ranking.rank(idx.similarities(v), idx.ids, limit)
        val t2 = clock()
        log("search: model ${(t1 - t0) / 1_000_000} ms, rank ${idx.size} photos ${(t2 - t1) / 1_000_000} ms, ${res.size} results")
        return Result.Ok(res)
    }

    /** The JSON string of RGBridge.search (API.md section 6). */
    fun searchJson(query: String): String = toJson(search(query))

    /** Loads the model now if it is not loaded (for example in the background). Never throws. */
    @Synchronized
    fun warmUp() {
        if (!files.modelsPresent()) return
        try {
            encoder()
        } catch (e: Exception) {
            log("search warm-up failed: $e")
        }
    }

    @Synchronized
    fun close() {
        model?.close()
        model = null
        encoder = null
        modelStamp = null
        index = null
        indexStamp = null
    }

    private fun encoder(): QueryEncoder {
        val stamp = files.modelStamp()
        encoder?.let { if (stamp == modelStamp) return it }
        model?.close()
        model = null
        encoder = null
        val t0 = clock()
        val tok = WordPiece.load(files.model("vocab"))
        val dense = QueryEncoder.loadDense(files.model("dense"))
        val m = modelFactory(files.model("text"))
        val enc = try {
            QueryEncoder(tok, m, dense)
        } catch (e: Exception) {
            m.close()
            throw e
        }
        model = m
        encoder = enc
        modelStamp = stamp
        log("search model loaded in ${(clock() - t0) / 1_000_000} ms (vocab ${tok.size} tokens)")
        return enc
    }

    private fun index(): SearchIndex {
        val f = files.index
        val stamp = f.lastModified() to f.length()
        index?.let { if (stamp == indexStamp) return it }
        index = null
        val t0 = clock()
        val idx = SearchIndex.load(f)
        index = idx
        indexStamp = stamp
        log("search index loaded: ${idx.size} photos in ${(clock() - t0) / 1_000_000} ms")
        return idx
    }

    /** Forget the loaded search index (call after a new one was saved). */
    @Synchronized
    fun invalidateIndex() {
        index = null
        indexStamp = null
    }

    companion object {
        const val MSG_NO_MODEL = "Search model not downloaded yet. Connect to the PC on Wi-Fi and sync."
        const val MSG_NO_INDEX = "Search index not downloaded yet. Connect to the PC on Wi-Fi and sync."

        fun toJson(r: Result): String = when (r) {
            is Result.NotReady -> JSONObject().put("ok", false).put("reason", r.reason).toString()
            is Result.Ok -> {
                val sb = StringBuilder(32 + r.results.size * 24)
                sb.append("{\"ok\":true,\"results\":[")
                r.results.forEachIndexed { i, (id, sim) ->
                    if (i > 0) sb.append(',')
                    // Rounded to 4 decimals, like the PC.
                    val rounded = if (sim.isFinite()) Math.round(sim * 10000.0) / 10000.0 else 0.0
                    sb.append('[').append(id).append(',').append(rounded).append(']')
                }
                sb.append("]}")
                sb.toString()
            }
        }
    }
}
