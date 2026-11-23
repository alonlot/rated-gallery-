package com.ratedgallery.phone.core.search

import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.Properties

/**
 * Where the search files live (app-private storage, one folder):
 * the three model files of API.md section 6, the search index, and the sha256 of each model file
 * as downloaded (models.properties).
 */
class SearchFiles(val dir: File) {
    fun model(name: String): File = File(dir, FILE_NAMES[name] ?: throw IllegalArgumentException("unknown model file $name"))

    val index: File get() = File(dir, "search_index.bin")
    private val shaFile: File get() = File(dir, "models.properties")

    /** All three model files are present. */
    fun modelsPresent(): Boolean = MODEL_NAMES.all { model(it).isFile && storedSha(it) != null }

    @Synchronized
    fun storedShas(): Map<String, String> {
        val p = Properties()
        try {
            if (shaFile.isFile) shaFile.inputStream().use { p.load(it) }
        } catch (e: IOException) {
            return emptyMap()
        }
        return p.stringPropertyNames().associateWith { p.getProperty(it) }
    }

    fun storedSha(name: String): String? = storedShas()[name]

    /** Records [sha] for [name], or removes the entry if [sha] is null. Atomic. */
    @Synchronized
    @Throws(IOException::class)
    fun setStoredSha(name: String, sha: String?) {
        val p = Properties()
        storedShas().forEach { (k, v) -> p.setProperty(k, v) }
        if (sha == null) p.remove(name) else p.setProperty(name, sha)
        atomicWrite(shaFile) { out -> p.store(out, null) }
    }

    /** Stamp that changes whenever a model file is replaced. */
    fun modelStamp(): String = MODEL_NAMES.joinToString(",") { storedSha(it) ?: "-" }

    fun ensureDir() {
        if (!dir.isDirectory && !dir.mkdirs()) throw IOException("cannot create $dir")
    }

    companion object {
        val MODEL_NAMES = listOf("text", "dense", "vocab")
        val FILE_NAMES = mapOf("text" to "mclip_text_q8.onnx", "dense" to "mclip_dense.f32", "vocab" to "mclip_vocab.txt")
        const val VERSION = "mclip-b32-v1"

        /** Moves [from] over [to] atomically (same folder). */
        @Throws(IOException::class)
        fun atomicReplace(from: File, to: File) {
            try {
                Files.move(from.toPath(), to.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
            } catch (e: AtomicMoveNotSupportedException) {
                if (!from.renameTo(to)) throw IOException("rename failed")
            }
        }

        @Throws(IOException::class)
        fun atomicWrite(file: File, writer: (java.io.OutputStream) -> Unit) {
            val tmp = File(file.parentFile, file.name + ".tmp")
            try {
                FileOutputStream(tmp).use { fos ->
                    val b = fos.buffered()
                    writer(b)
                    b.flush()
                    fos.fd.sync()
                }
                atomicReplace(tmp, file)
            } finally {
                if (tmp.exists()) tmp.delete()
            }
        }
    }
}
