package com.ratedgallery.phone.core

import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.io.OutputStream
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.StandardCopyOption

/**
 * Keeps the last synced device index as a file. Saving is atomic: the new index is written to a
 * temp file in the same folder, synced to disk, checked, and then renamed over the old file.
 * A failed or interrupted download never damages the previous index.
 */
class IndexStore(
    private val dir: File,
    private val name: String = "index.json",
    private val validate: (File) -> Boolean = ::looksLikeIndex,
) {
    val file: File get() = File(dir, name)

    @Throws(IOException::class)
    fun save(writer: (OutputStream) -> Unit) {
        if (!dir.isDirectory && !dir.mkdirs()) throw IOException("cannot create $dir")
        val tmp = File(dir, "$name.tmp")
        try {
            FileOutputStream(tmp).use { fos ->
                val buffered = fos.buffered()
                writer(buffered)
                buffered.flush()
                fos.fd.sync()
            }
            if (!validate(tmp)) throw IOException("downloaded index is not valid")
            try {
                Files.move(tmp.toPath(), file.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
            } catch (e: AtomicMoveNotSupportedException) {
                if (!tmp.renameTo(file)) throw IOException("rename failed")
            }
        } finally {
            if (tmp.exists()) tmp.delete()
        }
    }

    fun saveString(json: String) = save { it.write(json.toByteArray(Charsets.UTF_8)) }

    /** The saved index JSON, or [EMPTY] if there is none. */
    fun readOrEmpty(): String = try {
        val f = file
        if (f.isFile) f.readText(Charsets.UTF_8) else EMPTY
    } catch (e: IOException) {
        EMPTY
    }

    companion object {
        const val EMPTY = "{\"version\":1,\"persons\":[],\"photos\":[]}"

        fun looksLikeIndex(f: File): Boolean = try {
            val o = JSONObject(f.readText(Charsets.UTF_8))
            o.optJSONArray("photos") != null
        } catch (e: Exception) {
            false
        }
    }
}
