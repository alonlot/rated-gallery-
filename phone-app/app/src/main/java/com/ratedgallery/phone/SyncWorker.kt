package com.ratedgallery.phone

import android.app.NotificationManager
import android.content.Context
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.os.SystemClock
import android.util.Log
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.work.WorkerParameters
import com.ratedgallery.phone.core.IndexStore
import com.ratedgallery.phone.core.SyncEngine
import com.ratedgallery.phone.core.SyncListener
import com.ratedgallery.phone.core.SyncResult
import com.ratedgallery.phone.core.SyncStatus
import com.ratedgallery.phone.core.search.SearchSync
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.withContext

class SyncWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val ctx = applicationContext
        val prefs = Prefs(ctx)
        val api = prefs.api() ?: return@withContext Result.success()
        // Only one sync at a time (periodic and one-time work may overlap).
        if (!LOCK.tryLock()) return@withContext Result.success()
        try {
            val mode = runCatching { SyncEngine.Mode.valueOf(inputData.getString(KEY_MODE) ?: "") }
                .getOrDefault(SyncEngine.Mode.FULL)
            if (mode == SyncEngine.Mode.FULL) StatusRepo.undeliverable.clear()
            StatusRepo.update { it.copy(syncing = true, message = "", done = 0, total = 0) }
            val engine = SyncEngine(
                api = api,
                media = MediaStoreSource(ctx),
                index = indexStore(ctx),
                isForeground = { StatusRepo.foreground.get() },
                listener = Listener(prefs),
                search = SearchSync(api, Search.files(ctx), onProgress = ::onModelProgress, isStopped = { isStopped }),
                isUnmetered = {
                    val cm = ctx.getSystemService(ConnectivityManager::class.java)
                    cm != null && !cm.isActiveNetworkMetered
                },
            )
            val result = engine.run(mode)
            Log.i(TAG, "sync $mode: $result")
            when (result) {
                is SyncResult.Done -> {
                    val now = System.currentTimeMillis() / 1000
                    prefs.lastSync = now
                    var msg = if (result.failed > 0) "Uploaded ${result.uploaded}, could not read ${result.failed}" else
                        "Uploaded ${result.uploaded}"
                    if (result.searchError != null) msg += ". ${result.searchError}"
                    StatusRepo.update { it.copy(lastSync = now, message = msg) }
                }
                SyncResult.Unreachable -> StatusRepo.update { it.copy(connected = false, message = "PC not reachable") }
                SyncResult.Unauthorized -> {
                    prefs.markNeedsRepair()
                    SyncScheduler.cancelAll(ctx)
                    StatusRepo.refreshFromPrefs(ctx)
                    StatusRepo.update { it.copy(connected = false, message = "Pair again") }
                }
                SyncResult.NoPermission -> StatusRepo.update { it.copy(message = "No photo access: grant it in Settings") }
                is SyncResult.Failed -> StatusRepo.update { it.copy(message = "Sync stopped: ${result.message}") }
            }
            Result.success()
        } finally {
            StatusRepo.update { it.copy(syncing = false, phase = SyncStatus.Phase.IDLE) }
            LOCK.unlock()
        }
    }

    private var lastModelReport = 0L

    /** Model download progress, in MB (phase "search"). */
    private fun onModelProgress(name: String, done: Long, total: Long) {
        val t = SystemClock.elapsedRealtime()
        if (t - lastModelReport < 500 && done != total) return
        lastModelReport = t
        val mb = 1024 * 1024
        val d = (done / mb).toInt()
        val tot = if (total > 0) ((total + mb - 1) / mb).toInt() else 0
        StatusRepo.update {
            it.copy(phase = SyncStatus.Phase.SEARCH, done = d, total = tot, message = "Downloading search model ($name)")
        }
    }

    override suspend fun getForegroundInfo(): ForegroundInfo =
        foregroundInfo(Notifications.syncProgress(applicationContext, 0, 0, "Syncing"))

    private fun foregroundInfo(n: android.app.Notification) =
        ForegroundInfo(Notifications.ID_SYNC, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)

    private inner class Listener(private val prefs: Prefs) : SyncListener {
        private var foregroundStarted = false
        private var lastNotify = 0L

        override suspend fun onConnected(connected: Boolean, serverName: String?) {
            if (!serverName.isNullOrEmpty() && serverName != prefs.serverName) prefs.saveServerName(serverName)
            StatusRepo.update { it.copy(connected = connected) }
        }

        override suspend fun onProgress(phase: String, done: Int, total: Int, message: String) {
            StatusRepo.update { it.copy(phase = phase, done = done, total = total, message = message) }
            // Show the foreground notification only when there is something to upload.
            if (phase == SyncStatus.Phase.UPLOAD && total > 0) {
                val text = applicationContext.getString(R.string.notif_upload_progress, done, total)
                val n = Notifications.syncProgress(applicationContext, done, total, text)
                if (!foregroundStarted) {
                    foregroundStarted = true
                    try {
                        setForeground(foregroundInfo(n))
                    } catch (e: kotlinx.coroutines.CancellationException) {
                        throw e
                    } catch (e: Exception) {
                        // Android 12+ may refuse a foreground service start from the background.
                        // The sync still runs as normal background work.
                        Log.w(TAG, "foreground not allowed: $e")
                    }
                } else {
                    val t = SystemClock.elapsedRealtime()
                    if (t - lastNotify > 700 || done == total) {
                        lastNotify = t
                        try {
                            if (Permissions.hasNotifications(applicationContext)) {
                                applicationContext.getSystemService(NotificationManager::class.java)
                                    .notify(Notifications.ID_SYNC, n)
                            }
                        } catch (e: SecurityException) {
                            Log.w(TAG, "notify: $e")
                        }
                    }
                }
            }
        }

        override suspend fun onUndeliverable(mid: String) {
            StatusRepo.undeliverable.add(mid)
        }

        override suspend fun onIndexSaved() {
            StatusRepo.indexUpdated()
        }

        override suspend fun onSearchUpdated() {
            Search.engine(applicationContext).invalidateIndex()
        }
    }

    companion object {
        const val KEY_MODE = "mode"
        private const val TAG = "RGSync"
        private val LOCK = Mutex()
        val isRunning: Boolean get() = LOCK.isLocked

        fun indexStore(ctx: Context) = IndexStore(ctx.filesDir)
    }
}
