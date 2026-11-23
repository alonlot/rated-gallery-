package com.ratedgallery.phone.core

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okio.Buffer
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.util.concurrent.TimeUnit
import java.util.zip.GZIPOutputStream

class OkHttpDeviceApiTest {
    @get:Rule val tmp = TemporaryFolder()
    private lateinit var server: MockWebServer
    private val token = "a".repeat(64)

    @Before fun start() { server = MockWebServer(); server.start() }
    @After fun stop() { server.shutdown() }

    private fun api(t: String? = token) = OkHttpDeviceApi(server.hostName, server.port, t)

    @Test fun pairSendsCodeAndName() {
        server.enqueue(MockResponse().setBody("""{"device_id": 3, "token": "$token", "server_name": "DESKTOP-ABC"}"""))
        val r = api(null).pair("123456", "Poco F6")
        assertEquals(PairResult(3, token, "DESKTOP-ABC"), r)
        val req = server.takeRequest()
        assertEquals("POST", req.method)
        assertEquals("/api/device/pair", req.path)
        assertNull(req.getHeader("Authorization"))
        assertTrue(req.getHeader("Content-Type")!!.startsWith("application/json"))
        val body = JSONObject(req.body.readUtf8())
        assertEquals("123456", body.getString("code"))
        assertEquals("Poco F6", body.getString("name"))
    }

    @Test fun pairRejected() {
        server.enqueue(MockResponse().setResponseCode(403).setBody("""{"detail": "invalid or expired code"}"""))
        try {
            api(null).pair("000000", "x"); fail()
        } catch (e: HttpStatusException) {
            assertEquals(403, e.code)
            assertEquals("invalid or expired code", e.detail)
        }
    }

    @Test fun pingSendsBearerAndParses() {
        server.enqueue(MockResponse().setBody("""{"ok": true, "server_version": "1.0", "server_name": "PC", "device_id": 3}"""))
        assertEquals(PingResult("1.0", "PC", 3), api().ping())
        assertEquals("Bearer $token", server.takeRequest().getHeader("Authorization"))
    }

    @Test fun pingTimeoutMeansUnreachable() {
        server.enqueue(MockResponse().setBody("{}").setHeadersDelay(5, TimeUnit.SECONDS))
        val t0 = System.nanoTime()
        assertNull(api().ping())
        val ms = (System.nanoTime() - t0) / 1_000_000
        assertTrue("took $ms ms", ms < 4500)
    }

    @Test fun pingConnectionRefusedMeansUnreachable() {
        val port = server.port
        server.shutdown()
        assertNull(OkHttpDeviceApi("127.0.0.1", port, token).ping())
    }

    @Test fun unauthorizedIsMapped() {
        server.enqueue(MockResponse().setResponseCode(401).setBody("""{"detail": "bad token"}"""))
        try { api().ping(); fail() } catch (e: UnauthorizedException) { assertEquals("bad token", e.detail) }
        server.enqueue(MockResponse().setResponseCode(401))
        try { api().wanted(20); fail() } catch (e: UnauthorizedException) { /* ok */ }
    }

    @Test fun wantedAndManifest() {
        server.enqueue(MockResponse().setBody("""{"known": 9000, "new": 120, "changed": 2, "missing": 5}"""))
        server.enqueue(MockResponse().setBody("""{"items": ["1234", "1240"], "remaining": 118}"""))
        assertEquals(ManifestResult(9000, 120, 2, 5), api().postManifest(ManifestJson.build(listOf(item("1")))))
        assertEquals(Wanted(listOf("1234", "1240"), 118), api().wanted(20))
        val m = server.takeRequest()
        assertEquals("/api/device/manifest", m.path)
        assertEquals(true, JSONObject(m.body.readUtf8()).getBoolean("full"))
        assertEquals("/api/device/wanted?limit=20", server.takeRequest().path)
    }

    @Test fun uploadStreamsRawBytesWithMime() {
        server.enqueue(MockResponse().setBody("""{"ok": true}"""))
        val bytes = ByteArray(300_000) { (it % 251).toByte() }
        api().upload("1234", OpenedMedia("image/heic", bytes.size.toLong()) { ByteArrayInputStream(bytes) })
        val req = server.takeRequest()
        assertEquals("PUT", req.method)
        assertEquals("/api/device/file/1234", req.path)
        assertEquals("image/heic", req.getHeader("Content-Type"))
        assertEquals(bytes.size.toString(), req.getHeader("Content-Length"))
        assertTrue(bytes.contentEquals(req.body.readByteArray()))
    }

    @Test fun uploadNotStored() {
        server.enqueue(MockResponse().setBody("""{"ok": false}"""))
        try {
            api().upload("9", OpenedMedia("image/jpeg", 1) { ByteArrayInputStream(byteArrayOf(1)) }); fail()
        } catch (e: HttpStatusException) { assertEquals(200, e.code) }
    }

    @Test fun upload404() {
        server.enqueue(MockResponse().setResponseCode(404).setBody("""{"detail": "unknown"}"""))
        try {
            api().upload("9", OpenedMedia("image/jpeg", 1) { ByteArrayInputStream(byteArrayOf(1)) }); fail()
        } catch (e: NotFoundException) { /* ok */ }
    }

    @Test fun statusAndGzipIndex() {
        server.enqueue(MockResponse().setBody("""{"pending": 37, "busy": true, "task": "Scanning", "done": 120, "total": 157}"""))
        assertEquals(PcStatus(37, true, "Scanning", 120, 157), api().status())

        val index = """{"version":1,"persons":[],"photos":[{"id":1,"mid":"1234"}]}"""
        val gz = Buffer().apply { GZIPOutputStream(outputStream()).use { it.write(index.toByteArray()) } }
        server.enqueue(MockResponse().setHeader("Content-Encoding", "gzip").setBody(gz))
        val out = ByteArrayOutputStream()
        api().downloadIndex(out)
        assertEquals(index, out.toString("UTF-8"))
    }

    @Test fun sessionUrl() {
        assertEquals(
            "http://192.168.1.10:8765/api/device/session?token=$token",
            OkHttpDeviceApi("192.168.1.10", 8765, token).sessionUrl(),
        )
    }

    /** End to end: the sync engine against an HTTP PC simulator. */
    @Test fun syncEngineOverHttp() = runBlocking {
        val media = FakeMedia((1..25).map { item(it.toString()) })
        val want = ArrayList<String>()
        val received = HashMap<String, String>()
        var manifests = 0
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                if (request.getHeader("Authorization") != "Bearer $token") return MockResponse().setResponseCode(401)
                val path = request.path!!
                return when {
                    path == "/api/device/ping" -> MockResponse().setBody("""{"ok":true,"server_version":"1.0","server_name":"PC","device_id":3}""")
                    path == "/api/device/manifest" -> {
                        manifests++
                        val items = JSONObject(request.body.readUtf8()).getJSONArray("items")
                        synchronized(want) { for (i in 0 until items.length()) want.add(items.getJSONObject(i).getString("mid")) }
                        MockResponse().setBody("""{"known":0,"new":${items.length()},"changed":0,"missing":0}""")
                    }
                    path.startsWith("/api/device/wanted?limit=20") -> synchronized(want) {
                        val items = want.take(20).joinToString(",") { "\"$it\"" }
                        MockResponse().setBody("""{"items":[$items],"remaining":${maxOf(0, want.size - 20)}}""")
                    }
                    path.startsWith("/api/device/file/") -> {
                        val mid = path.removePrefix("/api/device/file/")
                        received[mid] = request.body.readUtf8()
                        synchronized(want) { want.remove(mid) }
                        MockResponse().setBody("""{"ok":true}""")
                    }
                    path == "/api/device/status" -> MockResponse().setBody("""{"pending":0,"busy":false,"task":"","done":25,"total":25}""")
                    path == "/api/device/index" -> MockResponse().setBody("""{"version":1,"persons":[],"photos":[]}""")
                    else -> MockResponse().setResponseCode(404)
                }
            }
        }
        val engine = SyncEngine(api(), media, IndexStore(tmp.root), isForeground = { true })
        assertEquals(SyncResult.Done(25, 0, true), engine.run(SyncEngine.Mode.FULL))
        assertEquals(1, manifests)
        assertEquals(25, received.size)
        assertEquals("bytes-of-7", received["7"])
        assertTrue(IndexStore(tmp.root).file.isFile)
    }
}
