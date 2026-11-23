package com.ratedgallery.phone.core

import kotlinx.coroutines.delay
import java.io.IOException

sealed class SyncResult {
    data class Done(val uploaded: Int, val failed: Int, val indexSaved: Boolean) : SyncResult()
    object Unreachable : SyncResult()
    object Unauthorized : SyncResult()
    object NoPermission : SyncResult()
    data class Failed(val message: String) : SyncResult()
}

interface SyncListener {
    suspend fun onConnected(connected: Boolean, serverName: String?) {}
    suspend fun onProgress(phase: String, done: Int, total: Int, message: String) {}
    suspend fun onIndexSaved() {}
    /** [mid] could not be delivered in this run (unreadable on the phone or rejected by the PC). */
    suspend fun onUndeliverable(mid: String) {}
}

/**
 * The sync state machine (pure, testable):
 * ping -> manifest (FULL only) -> upload loop -> PC processing wait (foreground only) -> index.
 * It is resumable by design: the PC tracks what it has, so an interrupted run just continues next time.
 */
class SyncEngine(
    private val api: DeviceApi,
    private val media: MediaSource,
    private val index: IndexStore,
    private val isForeground: () -> Boolean,
    private val listener: SyncListener = object : SyncListener {},
    private val sleep: suspend (Long) -> Unit = { delay(it) },
    private val now: () -> Long = System::currentTimeMillis,
) {
    enum class Mode {
        /** Manifest + uploads + index. Normal sync. */
        FULL,
        /** Uploads + index, no manifest (unless the PC answers 404). Used when the PC asks for photos. */
        QUICK,
    }

    suspend fun run(mode: Mode): SyncResult {
        try {
            listener.onProgress(SyncStatus.Phase.CONNECT, 0, 0, "Connecting")
            val ping = api.ping()
            if (ping == null) {
                listener.onConnected(false, null)
                return SyncResult.Unreachable
            }
            listener.onConnected(true, ping.serverName)

            val run = Run()
            if (mode == Mode.FULL) {
                if (!media.canRead()) return SyncResult.NoPermission
                run.sendManifest()
            }

            run.uploadLoop()

            // Let the PC process what we sent. Wait only while the app is visible.
            // While waiting, keep serving photos the PC asks for (e.g. one the user opened on the PC).
            var st = api.status()
            listener.onProgress(SyncStatus.Phase.PROCESSING, st.done, st.total, st.task)
            if (st.pending > 0 && isForeground()) {
                val deadline = now() + MAX_WAIT_MS
                while (st.pending > 0 && isForeground() && now() < deadline) {
                    sleep(POLL_MS)
                    if (run.uploadLoop()) {
                        st = api.status()
                        continue
                    }
                    st = api.status()
                    listener.onProgress(SyncStatus.Phase.PROCESSING, st.done, st.total, st.task)
                }
            }

            listener.onProgress(SyncStatus.Phase.INDEX, 0, 0, "Downloading index")
            index.save { out -> api.downloadIndex(out) }
            listener.onIndexSaved()
            return SyncResult.Done(run.uploaded, run.failed.size, true)
        } catch (e: UnauthorizedException) {
            return SyncResult.Unauthorized
        } catch (e: IOException) {
            return SyncResult.Failed(e.message ?: e.javaClass.simpleName)
        }
    }

    /** State of one sync run. */
    private inner class Run {
        var manifestSent = false
        val attempted = HashSet<String>()
        val failed = LinkedHashSet<String>()
        var uploaded = 0

        suspend fun sendManifest() {
            listener.onProgress(SyncStatus.Phase.MANIFEST, 0, 0, "Listing photos")
            val items = media.listImages()
            val res = api.postManifest(ManifestJson.build(items, full = true))
            manifestSent = true
            listener.onProgress(
                SyncStatus.Phase.MANIFEST, items.size, items.size,
                "${items.size} photos (new ${res.new}, changed ${res.changed}, missing ${res.missing})",
            )
        }

        /**
         * Asks the PC what it wants, uploads it, asks again until nothing new is left.
         * Returns true if anything was attempted.
         */
        suspend fun uploadLoop(): Boolean {
            var any = false
            val startDone = uploaded + failed.size
            while (true) {
                val wanted = api.wanted(BATCH)
                // Stop when the PC only asks again for items already tried in this run.
                val fresh = wanted.items.filter { it !in attempted }
                if (fresh.isEmpty()) break
                if (!any) listener.onProgress(SyncStatus.Phase.UPLOAD, 0, 0, "Uploading")
                any = true
                // "remaining" counts what is left after the returned items (PC server behavior).
                var total = uploaded + failed.size - startDone + wanted.items.size + wanted.remaining
                for (mid in fresh) {
                    attempted.add(mid)
                    val ok = try {
                        uploadOne(mid)
                    } catch (e: NotFoundException) {
                        if (!manifestSent && media.canRead()) {
                            sendManifest()
                            try { uploadOne(mid) } catch (e2: NotFoundException) { false }
                        } else {
                            false
                        }
                    } catch (e: UnauthorizedException) {
                        throw e
                    } catch (e: HttpStatusException) {
                        false // the PC rejected this one file; continue with the others
                    }
                    if (ok) {
                        uploaded++
                    } else {
                        failed.add(mid)
                        listener.onUndeliverable(mid)
                    }
                    val done = uploaded + failed.size - startDone
                    total = maxOf(total, done)
                    listener.onProgress(SyncStatus.Phase.UPLOAD, done, total, "Uploading")
                }
            }
            return any
        }

        /** Returns false if the photo cannot be read on the phone. */
        private fun uploadOne(mid: String): Boolean {
            val m = media.open(mid) ?: return false
            api.upload(mid, m)
            return true
        }
    }

    companion object {
        const val BATCH = 20
        const val POLL_MS = 5_000L
        const val MAX_WAIT_MS = 10 * 60_000L
    }
}
