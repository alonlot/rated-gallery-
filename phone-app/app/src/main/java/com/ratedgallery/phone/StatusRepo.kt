package com.ratedgallery.phone

import android.content.Context
import android.os.Handler
import android.os.Looper
import com.ratedgallery.phone.core.SyncStatus
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicBoolean

/** Process-wide sync status. Listeners are called on the main thread. */
object StatusRepo {
    interface Listener {
        fun onStatus(status: SyncStatus)
        fun onIndexUpdated() {}
    }

    @Volatile
    var status: SyncStatus = SyncStatus()
        private set

    /** True while the app has a visible activity (ProcessLifecycleOwner). */
    val foreground = AtomicBoolean(false)

    /** Mids the last syncs could not deliver; the foreground poll does not restart a sync for these alone. */
    val undeliverable: MutableSet<String> = java.util.Collections.newSetFromMap(java.util.concurrent.ConcurrentHashMap())

    private val main = Handler(Looper.getMainLooper())
    private val listeners = CopyOnWriteArrayList<Listener>()

    fun addListener(l: Listener) { listeners.add(l) }
    fun removeListener(l: Listener) { listeners.remove(l) }

    @Synchronized
    fun update(transform: (SyncStatus) -> SyncStatus) {
        val next = transform(status)
        if (next == status) return
        status = next
        main.post { listeners.forEach { it.onStatus(next) } }
    }

    /** Copies pairing fields from prefs into the status. */
    fun refreshFromPrefs(context: Context) {
        val p = Prefs(context)
        update {
            it.copy(
                paired = p.paired, host = p.host, port = if (p.host.isEmpty()) 0 else p.port,
                lastSync = p.lastSync, needsRepair = p.needsRepair,
                connected = if (p.paired) it.connected else false,
                pendingRatings = SyncWorker.ratingQueue(context).size(),
            )
        }
    }

    /** Re-reads the number of queued phone ratings (after a rating or a sync). */
    fun refreshPendingRatings(context: Context) {
        val n = SyncWorker.ratingQueue(context).size()
        update { it.copy(pendingRatings = n) }
    }

    fun indexUpdated() {
        main.post { listeners.forEach { it.onIndexUpdated() } }
    }
}
