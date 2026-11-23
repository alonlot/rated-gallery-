package com.ratedgallery.phone

import android.content.Context
import android.graphics.Bitmap
import android.graphics.ImageDecoder
import android.util.Log
import android.util.Size
import android.webkit.WebResourceResponse
import androidx.webkit.WebViewAssetLoader
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.util.concurrent.Semaphore
import kotlin.math.max
import kotlin.math.roundToInt

/**
 * Serves phone photos to the gallery WebView on the asset loader origin:
 *  /media/thumb/<mid>  JPEG, about 384 px (ContentResolver.loadThumbnail)
 *  /media/full/<mid>   JPEG q90, longest side up to 2560 px (ImageDecoder, also decodes HEIC)
 * Read only: photos are never modified.
 */
class MediaPathHandler(context: Context) : WebViewAssetLoader.PathHandler {
    private val ctx = context.applicationContext
    private val media = MediaStoreSource(ctx)

    override fun handle(path: String): WebResourceResponse {
        val parts = path.trim('/').split('/')
        if (parts.size != 2 || parts[1].isEmpty() || !parts[1].all { it.isDigit() }) return notFound()
        val uri = media.uriFor(parts[1]) ?: return notFound()
        return try {
            when (parts[0]) {
                "thumb" -> {
                    val bmp = ctx.contentResolver.loadThumbnail(uri, Size(THUMB_PX, THUMB_PX), null)
                    jpeg(bmp, 85)
                }
                "full" -> {
                    FULL_SLOTS.acquire()
                    try {
                        jpeg(decodeFull(uri), 90)
                    } finally {
                        FULL_SLOTS.release()
                    }
                }
                else -> notFound()
            }
        } catch (e: Exception) {
            Log.w(TAG, "media $path: $e")
            notFound()
        } catch (e: OutOfMemoryError) {
            Log.w(TAG, "media $path: out of memory")
            notFound()
        }
    }

    private fun decodeFull(uri: android.net.Uri): Bitmap {
        val src = ImageDecoder.createSource(ctx.contentResolver, uri)
        return ImageDecoder.decodeBitmap(src) { decoder, info, _ ->
            val w = info.size.width
            val h = info.size.height
            val longest = max(w, h)
            if (longest > FULL_PX) {
                val s = FULL_PX.toDouble() / longest
                decoder.setTargetSize(max(1, (w * s).roundToInt()), max(1, (h * s).roundToInt()))
            }
            decoder.allocator = ImageDecoder.ALLOCATOR_SOFTWARE
        }
    }

    private fun jpeg(bmp: Bitmap, quality: Int): WebResourceResponse {
        val bos = ByteArrayOutputStream(64 * 1024)
        try {
            bmp.compress(Bitmap.CompressFormat.JPEG, quality, bos)
        } finally {
            bmp.recycle()
        }
        val headers = mapOf("Cache-Control" to "max-age=86400", "Content-Length" to bos.size().toString())
        return WebResourceResponse("image/jpeg", null, 200, "OK", headers, ByteArrayInputStream(bos.toByteArray()))
    }

    companion object {
        private const val TAG = "RGMedia"
        const val THUMB_PX = 384
        const val FULL_PX = 2560
        private val FULL_SLOTS = Semaphore(2)

        fun notFound() = WebResourceResponse(
            "text/plain", "utf-8", 404, "Not Found", emptyMap(), ByteArrayInputStream(ByteArray(0)),
        )
    }
}
