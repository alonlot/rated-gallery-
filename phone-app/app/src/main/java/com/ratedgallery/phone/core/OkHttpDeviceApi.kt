package com.ratedgallery.phone.core

import okhttp3.HttpUrl
import okhttp3.MediaType
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okio.BufferedSink
import okio.source
import org.json.JSONException
import org.json.JSONObject
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.util.concurrent.TimeUnit

/** [DeviceApi] over plain HTTP on the LAN. Talks only to the paired PC. */
class OkHttpDeviceApi(
    private val host: String,
    private val port: Int,
    private val token: String?,
    private val client: OkHttpClient = defaultClient,
    private val scheme: String = "http",
) : DeviceApi {

    private val pingClient: OkHttpClient by lazy {
        client.newBuilder()
            .connectTimeout(PING_TIMEOUT_MS, TimeUnit.MILLISECONDS)
            .readTimeout(PING_TIMEOUT_MS, TimeUnit.MILLISECONDS)
            .callTimeout(PING_TIMEOUT_MS, TimeUnit.MILLISECONDS)
            .build()
    }

    fun url(path: String, query: Map<String, String> = emptyMap()): HttpUrl {
        val b = HttpUrl.Builder().scheme(scheme).host(host).port(port)
        path.trim('/').split('/').forEach { b.addPathSegment(it) }
        query.forEach { (k, v) -> b.addQueryParameter(k, v) }
        return b.build()
    }

    /** URL that opens the full PC web UI with a session cookie (API.md: GET /api/device/session). */
    fun sessionUrl(): String = url("api/device/session", mapOf("token" to (token ?: ""))).toString()

    private fun request(path: String, query: Map<String, String> = emptyMap()): Request.Builder {
        val b = Request.Builder().url(url(path, query))
        if (token != null) b.header("Authorization", "Bearer $token")
        return b
    }

    /** POST /api/device/pair. Throws [HttpStatusException] (403 = invalid or expired code). */
    fun pair(code: String, name: String): PairResult {
        val body = JSONObject().put("code", code).put("name", name).toString()
        val req = Request.Builder().url(url("api/device/pair")).post(body.toRequestBody(JSON)).build()
        val o = callJson(client, req)
        val token = o.optString("token", "")
        if (token.isEmpty()) throw IOException("pairing response has no token")
        return PairResult(o.optLong("device_id"), token, o.optString("server_name", ""))
    }

    override fun ping(): PingResult? {
        val req = request("api/device/ping").get().build()
        return try {
            val o = callJson(pingClient, req)
            PingResult(
                o.optString("server_version", ""), o.optString("server_name", ""), o.optLong("device_id"),
                parseSearch(o.optJSONObject("search")),
            )
        } catch (e: UnauthorizedException) {
            throw e
        } catch (e: IOException) {
            null
        }
    }

    override fun postManifest(json: String): ManifestResult {
        val req = request("api/device/manifest").post(json.toRequestBody(JSON)).build()
        val o = callJson(client, req)
        return ManifestResult(o.optInt("known"), o.optInt("new"), o.optInt("changed"), o.optInt("missing"))
    }

    override fun wanted(limit: Int): Wanted = wantedWith(client, limit)

    /** Same as [wanted] but with the short ping timeout (for the foreground poll). */
    fun wantedQuick(limit: Int): Wanted = wantedWith(pingClient, limit)

    private fun wantedWith(c: OkHttpClient, limit: Int): Wanted {
        val req = request("api/device/wanted", mapOf("limit" to limit.toString())).get().build()
        val o = callJson(c, req)
        val arr = o.optJSONArray("items")
        val items = ArrayList<String>()
        if (arr != null) for (i in 0 until arr.length()) items.add(arr.get(i).toString())
        return Wanted(items, o.optInt("remaining", 0))
    }

    override fun upload(mid: String, media: OpenedMedia) {
        val type = media.mime.toMediaTypeOrNull() ?: "application/octet-stream".toMediaType()
        val body = object : RequestBody() {
            override fun contentType(): MediaType = type
            override fun contentLength(): Long = media.length
            override fun writeTo(sink: BufferedSink) {
                media.open().source().use { sink.writeAll(it) }
            }
        }
        val req = request("api/device/file/$mid").put(body).build()
        val o = callJson(client, req)
        if (o.has("ok") && !o.optBoolean("ok")) throw HttpStatusException(200, "PC did not store $mid")
    }

    override fun status(): PcStatus {
        val o = callJson(client, request("api/device/status").get().build())
        return PcStatus(
            o.optInt("pending"), o.optBoolean("busy"), o.optString("task", ""),
            o.optInt("done"), o.optInt("total"),
        )
    }

    override fun postRatings(items: List<PendingRating>): RatingsResult {
        val body = JSONObject().put("items", RatingQueue.itemsJson(items)).toString()
        val o = callJson(client, request("api/device/ratings").post(body.toRequestBody(JSON)).build())
        val accepted = ArrayList<Long>()
        o.optJSONArray("accepted")?.let { a ->
            for (i in 0 until a.length()) a.optLong(i, 0).takeIf { it > 0 }?.let(accepted::add)
        }
        val rejected = ArrayList<RejectedRating>()
        o.optJSONArray("rejected")?.let { a ->
            for (i in 0 until a.length()) {
                val r = a.optJSONObject(i) ?: continue
                val id = r.optLong("photo_id", 0)
                if (id > 0) rejected.add(RejectedRating(id, r.optString("reason", "")))
            }
        }
        return RatingsResult(accepted, rejected)
    }

    override fun downloadIndex(out: OutputStream) {
        // OkHttp adds Accept-Encoding: gzip and decompresses transparently.
        client.newCall(request("api/device/index").get().build()).execute().use { resp ->
            checkStatus(resp)
            val body = resp.body ?: throw IOException("empty index")
            body.byteStream().use { it.copyTo(out) }
        }
    }

    override fun downloadSearchIndex(out: OutputStream) {
        client.newCall(request("api/device/search-index").get().build()).execute().use { resp ->
            checkStatus(resp)
            val body = resp.body ?: throw IOException("empty search index")
            body.byteStream().use { it.copyTo(out) }
        }
    }

    override fun downloadModel(name: String, read: (DownloadInfo, InputStream) -> Unit) {
        // Identity encoding: the length is known and the bytes are hashed exactly as sent.
        val req = request("api/device/model/$name").header("Accept-Encoding", "identity").get().build()
        client.newCall(req).execute().use { resp ->
            checkStatus(resp)
            val body = resp.body ?: throw IOException("empty model file")
            val info = DownloadInfo(resp.header("X-Sha256")?.trim()?.lowercase(), body.contentLength())
            body.byteStream().use { read(info, it) }
        }
    }

    companion object {
        const val PING_TIMEOUT_MS = 3000L

        fun parseSearch(o: JSONObject?): SearchInfo? {
            if (o == null) return null
            val files = o.optJSONObject("files") ?: return null
            val map = LinkedHashMap<String, String>()
            files.keys().forEach { k -> map[k] = files.optString(k, "").trim().lowercase() }
            return SearchInfo(o.optString("version", ""), map)
        }
        private val JSON = "application/json".toMediaType()

        val defaultClient: OkHttpClient by lazy {
            OkHttpClient.Builder()
                .connectTimeout(5, TimeUnit.SECONDS)
                .readTimeout(120, TimeUnit.SECONDS)
                .writeTimeout(120, TimeUnit.SECONDS)
                .followRedirects(false)
                .build()
        }

        private fun callJson(c: OkHttpClient, req: Request): JSONObject {
            c.newCall(req).execute().use { resp ->
                checkStatus(resp)
                val text = resp.body?.string().orEmpty()
                if (text.isBlank()) return JSONObject()
                return try {
                    JSONObject(text)
                } catch (e: JSONException) {
                    throw IOException("bad JSON from PC", e)
                }
            }
        }

        private fun checkStatus(resp: Response) {
            if (resp.isSuccessful) return
            val raw = try { resp.body?.string().orEmpty() } catch (e: IOException) { "" }
            val detail = try { JSONObject(raw).optString("detail", raw) } catch (e: JSONException) { raw }
            when (resp.code) {
                401 -> throw UnauthorizedException(detail)
                404 -> throw NotFoundException(detail)
                else -> throw HttpStatusException(resp.code, detail)
            }
        }
    }
}
