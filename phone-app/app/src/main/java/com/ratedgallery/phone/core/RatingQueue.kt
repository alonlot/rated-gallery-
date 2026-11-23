package com.ratedgallery.phone.core

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.concurrent.ConcurrentHashMap

/** One rating made on the phone that waits for the next sync (API.md section 7). */
data class PendingRating(val photoId: Long, val rating: Int, val created: Long)

/**
 * The queue of ratings made on the phone, kept as a small JSON file in app-private storage:
 * `{"items":[{"photo_id":123,"rating":8,"created":1714738930123}]}`.
 *
 * Thread-safe: the WebView bridge thread and the sync worker use it at the same time. Every
 * operation reads and writes the file under one process-wide lock per file, so several instances
 * for the same file are safe too. Writes are atomic (temp file + fsync + rename, like [IndexStore]):
 * a failed write keeps the previous queue.
 */
class RatingQueue(
    private val dir: File,
    private val name: String = FILE_NAME,
    private val now: () -> Long = System::currentTimeMillis,
) {
    val file: File get() = File(dir, name)

    private val lock: Any = LOCKS.computeIfAbsent(file.absoluteFile.normalize().path) { Any() }

    /**
     * Queues [rating] (1..10) for [photoId], replacing a queued one; 0 removes the queued rating.
     * `created` always grows for the same photo, so a rating made during a sync is never taken for
     * the one that was sent. Throws [IllegalArgumentException] for a bad value, [IOException] if
     * the file cannot be written (the queue is then unchanged).
     */
    @Throws(IOException::class)
    fun set(photoId: Long, rating: Int) {
        require(photoId > 0) { "bad photo id" }
        require(rating in 0..MAX_RATING) { "rating must be 0..$MAX_RATING" }
        synchronized(lock) {
            val items = readLocked()
            if (rating == 0) {
                if (items.remove(photoId) == null) return
            } else {
                val old = items[photoId]
                val t = now()
                val created = if (old != null && t <= old.created) old.created + 1 else t
                items[photoId] = PendingRating(photoId, rating, created)
            }
            writeLocked(items.values)
        }
    }

    /** The queued ratings, oldest first. */
    fun all(): List<PendingRating> = synchronized(lock) { readLocked().values.sortedBy { it.created } }

    fun size(): Int = synchronized(lock) { readLocked().size }

    fun isEmpty(): Boolean = size() == 0

    /**
     * Removes the items in [sent] whose photo is in [done] (accepted or rejected by the PC), but only
     * if the queued item still has the `created` that was sent. Returns the number removed.
     */
    @Throws(IOException::class)
    fun removeSent(sent: Collection<PendingRating>, done: Set<Long>): Int = synchronized(lock) {
        val items = readLocked()
        var removed = 0
        for (s in sent) {
            if (s.photoId !in done) continue
            val cur = items[s.photoId] ?: continue
            if (cur.created == s.created) {
                items.remove(s.photoId)
                removed++
            }
        }
        if (removed > 0) writeLocked(items.values)
        removed
    }

    /** `{"<photoId>": {"rating": 8, "created": <epoch ms>}, ...}` for RGBridge.getPendingRatings(). */
    fun toBridgeJson(): String {
        val o = JSONObject()
        for (r in all()) o.put(r.photoId.toString(), JSONObject().put("rating", r.rating).put("created", r.created))
        return o.toString()
    }

    private fun readLocked(): LinkedHashMap<Long, PendingRating> {
        val map = LinkedHashMap<Long, PendingRating>()
        val f = file
        if (!f.isFile) return map
        // An unreadable or damaged file counts as an empty queue (it is only ever replaced atomically).
        val arr = try {
            JSONObject(f.readText(Charsets.UTF_8)).optJSONArray("items")
        } catch (e: Exception) {
            null
        } ?: return map
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            val id = o.optLong("photo_id", 0)
            val rating = o.optInt("rating", 0)
            if (id <= 0 || rating !in 1..MAX_RATING) continue
            map[id] = PendingRating(id, rating, o.optLong("created", 0))
        }
        return map
    }

    private fun writeLocked(items: Collection<PendingRating>) {
        if (!dir.isDirectory && !dir.mkdirs()) throw IOException("cannot create $dir")
        val bytes = JSONObject().put("items", itemsJson(items)).toString().toByteArray(Charsets.UTF_8)
        val tmp = File(dir, "$name.tmp")
        try {
            FileOutputStream(tmp).use { fos ->
                fos.write(bytes)
                fos.flush()
                fos.fd.sync()
            }
            try {
                Files.move(tmp.toPath(), file.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
            } catch (e: AtomicMoveNotSupportedException) {
                if (!tmp.renameTo(file)) throw IOException("rename failed")
            }
        } finally {
            if (tmp.exists()) tmp.delete()
        }
    }

    companion object {
        const val FILE_NAME = "pending_ratings.json"
        const val MAX_RATING = 10
        private val LOCKS = ConcurrentHashMap<String, Any>()

        /** `[{"photo_id":..,"rating":..,"created":..}]`, the file format and the POST body items. */
        fun itemsJson(items: Collection<PendingRating>): JSONArray {
            val arr = JSONArray()
            for (r in items) arr.put(JSONObject().put("photo_id", r.photoId).put("rating", r.rating).put("created", r.created))
            return arr
        }
    }
}

/**
 * The logic of `RGBridge.rate` and `RGBridge.getPendingRatings` (API.md section 7), apart from
 * Android so that it can be tested on the JVM.
 *
 * Arguments come from JavaScript as strings: a JS number passed to a String parameter of a
 * `@JavascriptInterface` method arrives as its text (`"123"`, or `"123.0"` on some WebView
 * versions), a JS string arrives as is. Both are accepted; anything that is not a whole number is not.
 */
class RatingBridge(private val queue: RatingQueue, private val onChanged: () -> Unit = {}) {

    fun rate(photoId: String?, rating: String?): String {
        val id = parseWhole(photoId)
        if (id == null || id <= 0) return fail("bad photo id")
        val r = parseWhole(rating)
        if (r == null || r !in 0..RatingQueue.MAX_RATING.toLong()) return fail("rating must be 0..${RatingQueue.MAX_RATING}")
        return rate(id, r.toInt())
    }

    fun rate(photoId: Long, rating: Int): String {
        if (photoId <= 0) return fail("bad photo id")
        if (rating !in 0..RatingQueue.MAX_RATING) return fail("rating must be 0..${RatingQueue.MAX_RATING}")
        try {
            queue.set(photoId, rating)
        } catch (e: IOException) {
            return fail("cannot save: ${e.message ?: e.javaClass.simpleName}")
        }
        onChanged()
        return JSONObject().put("ok", true).toString()
    }

    fun pendingRatings(): String = queue.toBridgeJson()

    companion object {
        private val NUMBER = Regex("""[+-]?\d+(\.\d*)?([eE][+-]?\d+)?""")
        /** Largest integer a JS number holds exactly. */
        private const val MAX_SAFE = 9_007_199_254_740_991.0

        /** `"8"`, `"8.0"`, `" 8 "`, `"8e0"` -> 8; null for anything else (fractions, NaN, text, null). */
        fun parseWhole(s: String?): Long? {
            val t = s?.trim() ?: return null
            t.toLongOrNull()?.let { return it }
            if (!NUMBER.matches(t)) return null
            val d = t.toDoubleOrNull() ?: return null
            if (!d.isFinite() || d != Math.floor(d) || Math.abs(d) > MAX_SAFE) return null
            return d.toLong()
        }

        private fun fail(reason: String) = JSONObject().put("ok", false).put("reason", reason).toString()
    }
}
