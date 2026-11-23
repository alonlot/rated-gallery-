package com.ratedgallery.phone.core

import org.json.JSONArray
import org.json.JSONObject
import java.io.InputStream

/** One row of MediaStore.Images, as sent in the manifest (API.md section 3). */
data class MediaItem(
    val mid: String,
    val name: String?,
    val size: Long,
    /** DATE_MODIFIED, seconds. */
    val mtime: Long,
    /** DATE_TAKEN, milliseconds, or null. */
    val taken: Long?,
    val mime: String?,
    val path: String?,
)

/** An opened original image ready for upload. [open] may be called more than once (retries). */
class OpenedMedia(val mime: String, val length: Long, val open: () -> InputStream)

/** Read-only access to the photos on the phone. */
interface MediaSource {
    /** True if the app may read photos at all. If false, no manifest is sent (it would mark everything missing). */
    fun canRead(): Boolean
    fun listImages(): List<MediaItem>
    /** Opens the original bytes of [mid], or returns null if the photo is gone or unreadable. */
    fun open(mid: String): OpenedMedia?
}

object ManifestJson {
    fun build(items: List<MediaItem>, full: Boolean = true): String {
        val arr = JSONArray()
        for (it in items) {
            val o = JSONObject()
            o.put("mid", it.mid)
            o.put("name", it.name ?: JSONObject.NULL)
            o.put("size", it.size)
            o.put("mtime", it.mtime)
            o.put("taken", it.taken ?: JSONObject.NULL)
            o.put("mime", it.mime ?: JSONObject.NULL)
            o.put("path", it.path ?: JSONObject.NULL)
            arr.put(o)
        }
        val root = JSONObject()
        root.put("full", full)
        root.put("items", arr)
        return root.toString()
    }
}
