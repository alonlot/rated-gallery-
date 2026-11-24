package com.ratedgallery.phone.core

import com.ratedgallery.phone.core.search.SearchSync
import kotlinx.coroutines.delay
import java.io.IOException

sealed class SyncResult {
    data class Done(
        val uploaded: Int,
        val failed: Int,
        val indexSaved: Boolean,
        /** Why the search files could not be updated in this run (null: fine or nothing to do). */
        val searchError: String? = null,
        /** Phone ratings the PC accepted or rejected in this run (removed from the queue). */
        val ratingsSent: Int = 0,
        /** Why the queued ratings could not be sent in this run (null: fine or nothing to send). */
        val ratingsError: String? = null,
    ) : SyncResult()
    object Unreachable : SyncResult()
    object Unauthorized : SyncResult()
    object NoPermission : SyncResult()
    data class Failed(val message: String) : SyncResult()
}

interface SyncListener {
    suspend fun onConnected(connected: Boolean, serverName: String?) {}
    suspend fun onProgress(phase: String, done: Int, total: Int, message: String) {}
    suspend fun onIndexSaved() {}
    /** A new search index or search model file was saved. */
    suspend fun onSearchUpdated() {}
    /** [mid] could not be delivered in this run (unreadable on the phone or rejected by the PC). */
    suspend fun onUndeliverable(mid: String) {}
    /** The PC answered the rating upload; [removed] items left the queue. */
    suspend fun onRatingsSent(removed: Int) {}
}

/**
 * The sync state machine (pure, testable):
 * ping -> manifest (FULL only) -> upload loop -> PC processing wait (foreground only) -> phone ratings -> index.
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
    /** Text search files (API.md section 6); null disables search sync. */
    private val search: SearchSync? = null,
    /** Model files (about 137 MB) are downloaded only on unmetered networks. */
    private val isUnmetered: () -> Boolean = { true },
    /** Ratings made on the phone (API.md section 7); null disables the rating upload. */
    private val ratings: RatingQueue? = null,
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

            val (ratingsSent, ratingsError) = sendRatings()

            listener.onProgress(SyncStatus.Phase.INDEX, 0, 0, "Downloading index")
            index.save { out -> api.downloadIndex(out) }
            listener.onIndexSaved()
            val searchError = syncSearch(ping)
            return SyncResult.Done(run.uploaded, run.failed.size, true, searchError, ratingsSent, ratingsError)
        } catch (e: UnauthorizedException) {
            return SyncResult.Unauthorized
        } catch (e: IOException) {
            return SyncResult.Failed(e.message ?: e.javaClass.simpleName)
        }
    }

    /**
     * Before the index: POST the queued phone ratings. Accepted and rejected items leave the queue,
     * unless the user rated the photo again while the request ran (newer `created`). Any error but
     * 401 keeps the queue and does not stop the sync (404: a PC version without the endpoint).
     * Returns the number removed and the error message (null if none).
     */
    private suspend fun sendRatings(): Pair<Int, String?> {
        val q = ratings ?: return 0 to null
        val items = q.all()
        if (items.isEmpty()) return 0 to null
        listener.onProgress(SyncStatus.Phase.RATINGS, 0, items.size, "Sending ${items.size} ratings")
        val res = try {
            api.postRatings(items)
        } catch (e: UnauthorizedException) {
            throw e
        } catch (e: NotFoundException) {
            return 0 to "Ratings wait: the PC version cannot take phone ratings yet"
        } catch (e: IOException) {
            return 0 to "Ratings not sent: ${e.message ?: e.javaClass.simpleName}"
        }
        val done = HashSet<Long>()
        done.addAll(res.accepted)
        res.rejected.forEach { done.add(it.photoId) }
        val removed = try {
            q.removeSent(items, done)
        } catch (e: IOException) {
            // The PC has them; sending the same item again does not make a second rating.
            return 0 to "Ratings sent, queue not updated: ${e.message ?: e.javaClass.simpleName}"
        }
        listener.onProgress(SyncStatus.Phase.RATINGS, items.size, items.size, "Sent ${items.size} ratings")
        listener.onRatingsSent(removed)
        return removed to null
    }

    /**
     * After the index: the search index (every sync) and the model files (when their sha256 changed,
     * unmetered network only). A failure here does not fail the sync; it is returned as the message.
     */
    private suspend fun syncSearch(ping: PingResult): String? {
        val s = search ?: return null
        val info = ping.search ?: return null
        var changed = false
        try {
            listener.onProgress(SyncStatus.Phase.SEARCH, 0, 0, "Downloading search index")
            s.syncIndex()
            changed = true
            if (s.outdated(info).isNotEmpty()) {
                if (!isUnmetered()) return "Search model waits for Wi-Fi"
                listener.onProgress(SyncStatus.Phase.SEARCH, 0, 0, "Downloading search model")
                if (s.syncModels(info).isNotEmpty()) changed = true
            }
            return null
        } catch (e: UnauthorizedException) {
            throw e
        } catch (e: IOException) {
            return "Search update failed: ${e.message ?: e.javaClass.simpleName}"
        } finally {
            if (changed) listener.onSearchUpdated()
        }
    }

    /** State of one sync run. */
    private inner class Run {
        var manifestSent = false
        val attempted = HashSet<String>()
        val failed = LinkedHashSet<String>()
        var uploaded = 0
        var lastIndexAt = now()

        /**
         * A first sync can upload for an hour or more, and the index used to come only at the end. Meanwhile
         * the gallery showed old scores (for example from before the first model). Refresh it now and then.
         * A failure here does not stop the uploads; the index comes again at the end of the sync.
         */
        suspend fun maybeRefreshIndex() {
            if (now() - lastIndexAt < INDEX_REFRESH_MS) return
            lastIndexAt = now()
            try {
                index.save { out -> api.downloadIndex(out) }
                listener.onIndexSaved()
            } catch (e: UnauthorizedException) {
                throw e
            } catch (e: IOException) {
                // try again at the next refresh or at the end
            }
        }

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
                    maybeRefreshIndex()
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
        /** During long uploads, download the index this often so the gallery shows current scores. */
        const val INDEX_REFRESH_MS = 3 * 60_000L
    }
}
