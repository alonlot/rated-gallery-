package com.ratedgallery.phone.core

import java.io.ByteArrayInputStream
import java.io.IOException
import java.io.OutputStream

class FakeMedia(var items: List<MediaItem>, var readable: Boolean = true) : MediaSource {
    val unreadable = HashSet<String>()
    var listCalls = 0
    override fun canRead() = readable
    override fun listImages(): List<MediaItem> { listCalls++; return items }
    override fun open(mid: String): OpenedMedia? {
        if (mid in unreadable) return null
        val item = items.firstOrNull { it.mid == mid } ?: return null
        val bytes = "bytes-of-$mid".toByteArray()
        return OpenedMedia(item.mime ?: "image/jpeg", bytes.size.toLong()) { ByteArrayInputStream(bytes) }
    }
}

fun item(mid: String, mime: String = "image/jpeg") = MediaItem(mid, "IMG_$mid.jpg", 100, 1000, 2000, mime, "DCIM/Camera/")

/** A PC simulator implementing [DeviceApi] in memory. */
class FakePc : DeviceApi {
    var reachable = true
    var unauthorized = false
    var known = LinkedHashSet<String>()
    /** Mids the PC wants, most urgent first. */
    val want = ArrayList<String>()
    val uploads = ArrayList<String>()
    val calls = ArrayList<String>()
    var pendingSequence = ArrayDeque(listOf(0))
    var index = """{"version":1,"persons":[],"photos":[]}"""
    var failUploadOf: String? = null
    var neverForget: String? = null
    var autoWantNew = true

    private fun auth() { if (unauthorized) throw UnauthorizedException("bad token") }

    override fun ping(): PingResult? {
        calls += "ping"
        auth()
        return if (reachable) PingResult("1.0", "PC", 3) else null
    }

    override fun postManifest(json: String): ManifestResult {
        calls += "manifest"; auth()
        val o = org.json.JSONObject(json)
        val arr = o.getJSONArray("items")
        val mids = (0 until arr.length()).map { arr.getJSONObject(it).getString("mid") }
        val new = mids.filter { it !in known }
        known.addAll(mids)
        if (autoWantNew) want.addAll(new.filter { it !in want })
        return ManifestResult(known.size, new.size, 0, 0)
    }

    override fun wanted(limit: Int): Wanted {
        calls += "wanted"; auth()
        val items = want.take(limit)
        return Wanted(items, want.size - items.size) // like the PC: what is left after these items
    }

    override fun upload(mid: String, media: OpenedMedia) {
        calls += "upload:$mid"; auth()
        if (mid !in known) throw NotFoundException("unknown mid")
        if (mid == failUploadOf) throw HttpStatusException(500, "boom")
        val text = media.open().use { String(it.readBytes()) }
        if (text != "bytes-of-$mid") throw IOException("bad body")
        uploads += mid
        if (mid != neverForget) want.remove(mid)
    }

    var onStatus: ((Int) -> Unit)? = null
    private var statusCalls = 0

    override fun status(): PcStatus {
        calls += "status"; auth()
        onStatus?.invoke(++statusCalls)
        val p = if (pendingSequence.size > 1) pendingSequence.removeFirst() else pendingSequence.first()
        return PcStatus(p, p > 0, "Scanning", 0, 0)
    }

    override fun downloadIndex(out: OutputStream) {
        calls += "index"; auth()
        out.write(index.toByteArray())
    }
}
