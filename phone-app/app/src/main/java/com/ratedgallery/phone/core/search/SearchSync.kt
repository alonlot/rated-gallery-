package com.ratedgallery.phone.core.search

import com.ratedgallery.phone.core.DeviceApi
import com.ratedgallery.phone.core.IndexStore
import com.ratedgallery.phone.core.SearchInfo
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.security.MessageDigest

/** A downloaded file did not have the expected sha256. Nothing was kept. */
class ShaMismatchException(message: String) : IOException(message)

/**
 * Downloads the search files of API.md section 6.
 * - The search index: with every index sync, saved atomically (old index kept if the new one is broken).
 * - The model files: only when the stored sha256 differs from the one in the ping answer. Each file is
 *   streamed to a temp file in the same folder, hashed while writing, checked against the ping value and
 *   the X-Sha256 header, synced to disk, then renamed over the old file. A failed download leaves no file.
 */
class SearchSync(
    private val api: DeviceApi,
    private val files: SearchFiles,
    /** Download progress of a model file (bytes; total -1 if unknown). Called often, from the sync thread. */
    private val onProgress: (name: String, done: Long, total: Long) -> Unit = { _, _, _ -> },
    /** True when the sync must stop (the download is abandoned and the temp file deleted). */
    private val isStopped: () -> Boolean = { false },
) {
    @Throws(IOException::class)
    fun syncIndex() {
        files.ensureDir()
        IndexStore(files.dir, files.index.name, SearchIndex::looksValid).save { out -> api.downloadSearchIndex(out) }
    }

    /** Model files whose sha256 differs from [info] (or that are missing). */
    fun outdated(info: SearchInfo): List<String> {
        val stored = files.storedShas()
        return SearchFiles.MODEL_NAMES.filter { name ->
            val want = info.files[name]
            !want.isNullOrEmpty() && (stored[name] != want || !files.model(name).isFile)
        }
    }

    /** Downloads the outdated model files. Returns the names downloaded. */
    @Throws(IOException::class)
    fun syncModels(info: SearchInfo): List<String> {
        if (info.version != SearchFiles.VERSION) {
            throw IOException("search model ${info.version} is not supported by this app (needs ${SearchFiles.VERSION})")
        }
        files.ensureDir()
        cleanTemp()
        val done = ArrayList<String>()
        for (name in outdated(info)) {
            downloadModel(name, info.files.getValue(name))
            done.add(name)
        }
        return done
    }

    @Throws(IOException::class)
    private fun downloadModel(name: String, want: String) {
        val target = files.model(name)
        val tmp = File(files.dir, target.name + TEMP_SUFFIX)
        try {
            var header: String? = null
            var got: String? = null
            api.downloadModel(name) { info, input ->
                header = info.sha256
                if (info.length > 0 && files.dir.usableSpace in 0 until info.length + SPACE_MARGIN) {
                    throw IOException("not enough free space for the search model (${info.length / MB} MB)")
                }
                val md = MessageDigest.getInstance("SHA-256")
                val buf = ByteArray(1 shl 16)
                var total = 0L
                var lastReport = -1L
                FileOutputStream(tmp).use { fos ->
                    while (true) {
                        if (isStopped()) throw IOException("download stopped")
                        val n = input.read(buf)
                        if (n < 0) break
                        md.update(buf, 0, n)
                        fos.write(buf, 0, n)
                        total += n
                        if (total - lastReport >= REPORT_STEP) {
                            lastReport = total
                            onProgress(name, total, info.length)
                        }
                    }
                    fos.fd.sync()
                }
                if (info.length >= 0 && total != info.length) {
                    throw IOException("model file $name: got $total of ${info.length} bytes")
                }
                onProgress(name, total, info.length)
                got = hex(md.digest())
            }
            val actual = got ?: throw IOException("model file $name: no data")
            if (actual != want || header != actual) {
                throw ShaMismatchException("model file $name: sha256 $actual, ping $want, header ${header ?: "missing"}")
            }
            // Forget the old sha first: if the app dies between the rename and the write, the file is fetched again.
            files.setStoredSha(name, null)
            SearchFiles.atomicReplace(tmp, target)
            files.setStoredSha(name, actual)
        } finally {
            if (tmp.exists()) tmp.delete()
        }
    }

    /** Removes temp files left by a download that was killed. */
    private fun cleanTemp() {
        files.dir.listFiles()?.forEach { if (it.name.endsWith(TEMP_SUFFIX)) it.delete() }
    }

    companion object {
        const val TEMP_SUFFIX = ".part"
        private const val MB = 1024L * 1024L
        private const val REPORT_STEP = 2 * MB
        private const val SPACE_MARGIN = 32 * MB

        fun hex(b: ByteArray): String {
            val sb = StringBuilder(b.size * 2)
            for (x in b) sb.append(String.format("%02x", x.toInt() and 0xFF))
            return sb.toString()
        }
    }
}
