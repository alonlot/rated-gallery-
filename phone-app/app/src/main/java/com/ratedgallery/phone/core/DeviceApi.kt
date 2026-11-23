package com.ratedgallery.phone.core

import java.io.IOException
import java.io.InputStream
import java.io.OutputStream

data class PairResult(val deviceId: Long, val token: String, val serverName: String)
data class PingResult(
    val serverVersion: String,
    val serverName: String,
    val deviceId: Long,
    /** Text search info (API.md section 6), null if the PC offers no search. */
    val search: SearchInfo? = null,
)

/** `search` of the ping answer: model version and the sha256 of each model file (`text`, `dense`, `vocab`). */
data class SearchInfo(val version: String, val files: Map<String, String>)

/** Headers of a model download: `X-Sha256` (null if missing) and the length (-1 if unknown). */
data class DownloadInfo(val sha256: String?, val length: Long)
data class ManifestResult(val known: Int, val new: Int, val changed: Int, val missing: Int)
data class Wanted(val items: List<String>, val remaining: Int)
/** Answer of POST /api/device/ratings: photo ids the PC stored, and the ones it will never accept. */
data class RatingsResult(val accepted: List<Long>, val rejected: List<RejectedRating>)
data class RejectedRating(val photoId: Long, val reason: String)
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
    /** POST /api/device/ratings (API.md section 7). Throws [NotFoundException] on a PC without the endpoint. */
    fun postRatings(items: List<PendingRating>): RatingsResult
    /** Streams the device index into [out]. */
    fun downloadIndex(out: OutputStream)
    /** Streams the search index (binary, API.md section 6) into [out]. */
    fun downloadSearchIndex(out: OutputStream)
    /** GET /api/device/model/{name}: calls [read] with the headers and the body stream (never buffered in memory). */
    fun downloadModel(name: String, read: (DownloadInfo, InputStream) -> Unit)
}
