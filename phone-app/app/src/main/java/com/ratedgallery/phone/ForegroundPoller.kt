package com.ratedgallery.phone

import android.content.Context
import com.ratedgallery.phone.core.SyncEngine
import com.ratedgallery.phone.core.UnauthorizedException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.io.IOException

/**
 * While the app is visible, asks the PC every 5 s whether it wants photos
 * (for example a photo the user opened on the PC). If yes, starts a quick sync.
 */
class ForegroundPoller(context: Context) {
    private val ctx = context.applicationContext
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var job: Job? = null

    fun start() {
        if (job?.isActive == true) return
        job = scope.launch {
            while (isActive) {
                delay(INTERVAL_MS)
                pollOnce()
            }
        }
    }

    fun stop() {
        job?.cancel()
        job = null
    }

    private fun pollOnce() {
        if (SyncWorker.isRunning) return
        val prefs = Prefs(ctx)
        val api = prefs.api() ?: return
        try {
            val w = api.wantedQuick(SyncEngine.BATCH)
            StatusRepo.update { it.copy(connected = true) }
            if (w.items.any { it !in StatusRepo.undeliverable }) SyncScheduler.syncNow(ctx, SyncEngine.Mode.QUICK)
        } catch (e: UnauthorizedException) {
            prefs.markNeedsRepair()
            SyncScheduler.cancelAll(ctx)
            StatusRepo.refreshFromPrefs(ctx)
            StatusRepo.update { it.copy(connected = false, message = "Pair again") }
        } catch (e: IOException) {
            StatusRepo.update { it.copy(connected = false) }
        }
    }

    companion object {
        const val INTERVAL_MS = 5_000L
    }
}
