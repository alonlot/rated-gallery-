package com.ratedgallery.phone

import android.webkit.JavascriptInterface
import com.ratedgallery.phone.core.RatingBridge
import com.ratedgallery.phone.core.SyncEngine
import java.lang.ref.WeakReference

/** The `RGBridge` JavaScript interface of API.md section 5. Only added to the local gallery WebView. */
class GalleryBridge(activity: MainActivity) {
    private val ref = WeakReference(activity)
    private val app = activity.applicationContext
    private val ratingQueue = SyncWorker.ratingQueue(app)
    private val ratings = RatingBridge(ratingQueue) { StatusRepo.refreshPendingRatings(app) }

    @JavascriptInterface
    fun getIndex(): String = SyncWorker.indexStore(app).readOrEmpty()

    @JavascriptInterface
    fun getStatus(): String = StatusRepo.status.copy(pendingRatings = ratingQueue.size()).toJson()

    /**
     * Queues a rating made on the phone (API.md section 7); 0 removes the queued one.
     * The parameters are Strings on purpose: a JS number arrives as its text ("123" or "123.0",
     * depending on the WebView version), a JS string as is; a `long`/`int` parameter would silently
     * turn a string or a fraction into 0. Returns `{"ok":true}` or `{"ok":false,"reason":"..."}`.
     */
    @JavascriptInterface
    fun rate(photoId: String?, rating: String?): String = ratings.rate(photoId, rating)

    /** `{"<photoId>": {"rating": 8, "created": <epoch ms>}, ...}` */
    @JavascriptInterface
    fun getPendingRatings(): String = ratings.pendingRatings()

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
