package com.ratedgallery.phone

import android.Manifest
import android.content.ContentUris
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.ParcelFileDescriptor
import android.provider.MediaStore
import androidx.core.content.ContextCompat
import com.ratedgallery.phone.core.MediaItem
import com.ratedgallery.phone.core.MediaSource
import com.ratedgallery.phone.core.OpenedMedia
import java.io.FileNotFoundException

/** Read-only MediaStore access. Never writes or deletes anything. */
class MediaStoreSource(context: Context) : MediaSource {
    private val ctx = context.applicationContext
    private val cr = ctx.contentResolver

    /** MediaStore.VOLUME_EXTERNAL covers all external volumes (internal storage and SD cards). */
    private val collection: Uri = MediaStore.Images.Media.getContentUri(MediaStore.VOLUME_EXTERNAL)

    override fun canRead(): Boolean = Permissions.photoAccess(ctx) != Permissions.PhotoAccess.NONE

    override fun listImages(): List<MediaItem> {
        val projection = arrayOf(
            MediaStore.Images.Media._ID,
            MediaStore.Images.Media.DISPLAY_NAME,
            MediaStore.Images.Media.SIZE,
            MediaStore.Images.Media.DATE_MODIFIED,
            MediaStore.Images.Media.DATE_TAKEN,
            MediaStore.Images.Media.MIME_TYPE,
            MediaStore.Images.Media.RELATIVE_PATH,
        )
        val out = ArrayList<MediaItem>()
        val seen = HashSet<Long>()
        cr.query(collection, projection, null, null, "${MediaStore.Images.Media._ID} ASC")?.use { c ->
            val iId = c.getColumnIndexOrThrow(MediaStore.Images.Media._ID)
            val iName = c.getColumnIndexOrThrow(MediaStore.Images.Media.DISPLAY_NAME)
            val iSize = c.getColumnIndexOrThrow(MediaStore.Images.Media.SIZE)
            val iMod = c.getColumnIndexOrThrow(MediaStore.Images.Media.DATE_MODIFIED)
            val iTaken = c.getColumnIndexOrThrow(MediaStore.Images.Media.DATE_TAKEN)
            val iMime = c.getColumnIndexOrThrow(MediaStore.Images.Media.MIME_TYPE)
            val iPath = c.getColumnIndexOrThrow(MediaStore.Images.Media.RELATIVE_PATH)
            while (c.moveToNext()) {
                val id = c.getLong(iId)
                if (!seen.add(id)) continue
                val taken = if (c.isNull(iTaken)) null else c.getLong(iTaken).takeIf { it > 0 }
                out.add(
                    MediaItem(
                        mid = id.toString(),
                        name = c.getString(iName),
                        size = if (c.isNull(iSize)) 0 else c.getLong(iSize),
                        mtime = if (c.isNull(iMod)) 0 else c.getLong(iMod),
                        taken = taken,
                        mime = c.getString(iMime),
                        path = c.getString(iPath),
                    ),
                )
            }
        }
        return out
    }

    fun uriFor(mid: String): Uri? = mid.toLongOrNull()?.let { ContentUris.withAppendedId(collection, it) }

    fun mimeOf(mid: String): String? {
        val uri = uriFor(mid) ?: return null
        return try {
            cr.query(uri, arrayOf(MediaStore.Images.Media.MIME_TYPE), null, null, null)?.use { c ->
                if (c.moveToFirst()) c.getString(0) else null
            }
        } catch (e: Exception) {
            null
        }
    }

    /** URI for reading the original bytes, with GPS EXIF kept when ACCESS_MEDIA_LOCATION is granted. */
    private fun originalUri(uri: Uri): Uri =
        if (Permissions.hasMediaLocation(ctx)) MediaStore.setRequireOriginal(uri) else uri

    override fun open(mid: String): OpenedMedia? {
        val uri = uriFor(mid) ?: return null
        val mime = mimeOf(mid) ?: return null
        val src = originalUri(uri)
        val length = try {
            cr.openFileDescriptor(src, "r")?.use { it.statSize } ?: return null
        } catch (e: FileNotFoundException) {
            return null
        } catch (e: SecurityException) {
            return null
        } catch (e: UnsupportedOperationException) {
            return null
        }
        return OpenedMedia(mime, length) {
            val pfd = cr.openFileDescriptor(src, "r") ?: throw FileNotFoundException(mid)
            ParcelFileDescriptor.AutoCloseInputStream(pfd)
        }
    }
}

object Permissions {
    enum class PhotoAccess { FULL, PARTIAL, NONE }

    private fun granted(ctx: Context, p: String) =
        ContextCompat.checkSelfPermission(ctx, p) == PackageManager.PERMISSION_GRANTED

    fun photoAccess(ctx: Context): PhotoAccess = when {
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU && granted(ctx, Manifest.permission.READ_MEDIA_IMAGES) -> PhotoAccess.FULL
        Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU && granted(ctx, Manifest.permission.READ_EXTERNAL_STORAGE) -> PhotoAccess.FULL
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE &&
            granted(ctx, Manifest.permission.READ_MEDIA_VISUAL_USER_SELECTED) -> PhotoAccess.PARTIAL
        else -> PhotoAccess.NONE
    }

    fun hasMediaLocation(ctx: Context) = granted(ctx, Manifest.permission.ACCESS_MEDIA_LOCATION)

    fun hasNotifications(ctx: Context) =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU || granted(ctx, Manifest.permission.POST_NOTIFICATIONS)

    /** Everything we ask for at runtime, in one request. */
    fun toRequest(): Array<String> {
        val list = ArrayList<String>()
        when {
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE -> {
                list += Manifest.permission.READ_MEDIA_IMAGES
                list += Manifest.permission.READ_MEDIA_VISUAL_USER_SELECTED
            }
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU -> list += Manifest.permission.READ_MEDIA_IMAGES
            else -> list += Manifest.permission.READ_EXTERNAL_STORAGE
        }
        list += Manifest.permission.ACCESS_MEDIA_LOCATION
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) list += Manifest.permission.POST_NOTIFICATIONS
        return list.toTypedArray()
    }
}
