package com.ratedgallery.phone

import android.content.Context
import android.util.Log
import com.ratedgallery.phone.core.search.SearchEngine
import com.ratedgallery.phone.core.search.SearchFiles
import java.io.File

/** Process-wide text search: one [SearchEngine] (one ONNX Runtime session) while the app runs. */
object Search {
    private const val TAG = "RGSearch"

    @Volatile
    private var engine: SearchEngine? = null

    fun files(ctx: Context) = SearchFiles(File(ctx.applicationContext.filesDir, "search"))

    fun engine(ctx: Context): SearchEngine {
        engine?.let { return it }
        synchronized(this) {
            engine?.let { return it }
            return SearchEngine(files(ctx), log = { Log.i(TAG, it) }).also { engine = it }
        }
    }
}
