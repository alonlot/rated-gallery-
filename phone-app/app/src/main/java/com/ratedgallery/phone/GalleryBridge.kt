package com.ratedgallery.phone

import android.webkit.JavascriptInterface
import com.ratedgallery.phone.core.SyncEngine
import java.lang.ref.WeakReference

/** The `RGBridge` JavaScript interface of API.md section 5. Only added to the local gallery WebView. */
class GalleryBridge(activity: MainActivity) {
    private val ref = WeakReference(activity)
    private val app = activity.applicationContext

    @JavascriptInterface
    fun getIndex(): String = SyncWorker.indexStore(app).readOrEmpty()

    @JavascriptInterface
    fun getStatus(): String = StatusRepo.status.toJson()

    /**
     * Text search (API.md section 6). JavaScript bridge methods run on a WebView background thread,
     * not the UI thread; the first call loads the model (a few seconds), later calls are fast.
     */
    @JavascriptInterface
    fun search(query: String?): String = Search.engine(app).searchJson(query ?: "")

    @JavascriptInterface
    fun syncNow() {
        SyncScheduler.syncNow(app, SyncEngine.Mode.FULL)
    }

    @JavascriptInterface
    fun share(mid: String) {
        val a = ref.get() ?: return
        a.runOnUiThread { a.shareMedia(mid) }
    }

    @JavascriptInterface
    fun openPC() {
        val a = ref.get() ?: return
        a.runOnUiThread { a.showTab(MainActivity.Tab.PC) }
    }
}
