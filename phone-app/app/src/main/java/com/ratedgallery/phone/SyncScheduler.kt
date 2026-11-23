package com.ratedgallery.phone

import android.content.Context
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.workDataOf
import com.ratedgallery.phone.core.SyncEngine
import java.util.concurrent.TimeUnit

object SyncScheduler {
    private const val PERIODIC = "rg-periodic-sync"
    private const val NOW = "rg-sync-now"

    /** Background sync every 15 minutes on unmetered networks (Wi-Fi). */
    fun schedulePeriodic(ctx: Context) {
        val req = PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES)
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.UNMETERED).build())
            .setInputData(workDataOf(SyncWorker.KEY_MODE to SyncEngine.Mode.FULL.name))
            .build()
        WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.KEEP, req)
    }

    /** One sync as soon as there is a network. Ignored if one is already queued or running. */
    fun syncNow(ctx: Context, mode: SyncEngine.Mode = SyncEngine.Mode.FULL) {
        if (!Prefs(ctx).paired) return
        val req = OneTimeWorkRequestBuilder<SyncWorker>()
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .setInputData(workDataOf(SyncWorker.KEY_MODE to mode.name))
            .build()
        WorkManager.getInstance(ctx).enqueueUniqueWork(NOW, ExistingWorkPolicy.KEEP, req)
    }

    fun cancelAll(ctx: Context) {
        val wm = WorkManager.getInstance(ctx)
        wm.cancelUniqueWork(PERIODIC)
        wm.cancelUniqueWork(NOW)
    }
}
