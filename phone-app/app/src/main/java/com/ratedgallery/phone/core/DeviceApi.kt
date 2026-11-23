package com.ratedgallery.phone.core

import java.io.IOException
import java.io.OutputStream

data class PairResult(val deviceId: Long, val token: String, val serverName: String)
data class PingResult(val serverVersion: String, val serverName: String, val deviceId: Long)
data class ManifestResult(val known: Int, val new: Int, val changed: Int, val missing: Int)
data class Wanted(val items: List<String>, val remaining: Int)
data class PcStatus(val pending: Int, val busy: Boolean, val task: String, val done: Int, val total: Int)

/** HTTP status error from the PC. */
open class HttpStatusException(val code: Int, val detail: String) : IOException("HTTP $code: $detail")

/** 401: the device token is not valid any more. The app must show "Pair again". */
class UnauthorizedException(detail: String) : HttpStatusException(401, detail)

/** 404 (for example an upload for a mid the PC does not know). */
class NotFoundException(detail: String) : HttpStatusException(404, detail)

/** The device endpoints of API.md section 3. All calls block; call them off the main thread. */
interface DeviceApi {
    /** Returns null if the PC is not reachable (3 s timeout). Throws [UnauthorizedException] on 401. */
    fun ping(): PingResult?
    fun postManifest(json: String): ManifestResult
    fun wanted(limit: Int): Wanted
    fun upload(mid: String, media: OpenedMedia)
    fun status(): PcStatus
    /** Streams the device index into [out]. */
    fun downloadIndex(out: OutputStream)
}
