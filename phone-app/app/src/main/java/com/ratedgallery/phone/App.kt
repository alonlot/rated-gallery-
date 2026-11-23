package com.ratedgallery.phone

import android.app.Application
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.ProcessLifecycleOwner

class App : Application() {
    override fun onCreate() {
        super.onCreate()
        Notifications.createChannel(this)
        StatusRepo.refreshFromPrefs(this)
        if (Prefs(this).paired) SyncScheduler.schedulePeriodic(this)

        val poller = ForegroundPoller(this)
        ProcessLifecycleOwner.get().lifecycle.addObserver(object : DefaultLifecycleObserver {
            override fun onStart(owner: LifecycleOwner) {
                StatusRepo.foreground.set(true)
                poller.start()
            }

            override fun onStop(owner: LifecycleOwner) {
                StatusRepo.foreground.set(false)
                poller.stop()
            }
        })
    }
}
