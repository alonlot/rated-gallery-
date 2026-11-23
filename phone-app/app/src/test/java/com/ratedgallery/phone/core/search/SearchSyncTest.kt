package com.ratedgallery.phone.core.search

import com.ratedgallery.phone.core.FakeMedia
import com.ratedgallery.phone.core.FakePc
import com.ratedgallery.phone.core.IndexStore
import com.ratedgallery.phone.core.OkHttpDeviceApi
import com.ratedgallery.phone.core.SearchInfo
import com.ratedgallery.phone.core.SyncEngine
import com.ratedgallery.phone.core.SyncListener
import com.ratedgallery.phone.core.SyncResult
import com.ratedgallery.phone.core.item
import com.ratedgallery.phone.core.sha256
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okio.Buffer
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.IOException
import kotlin.random.Random

class SearchSyncTest {
    @get:Rule val tmp = TemporaryFolder()

    private val files by lazy { SearchFiles(File(tmp.root, "search")) }
    private val text = Random(1).nextBytes(300_000)
    private val dense = Random(2).nextBytes(1000)
    private val vocab = "[PAD]\nsun\n".toByteArray()
    private val index = SearchIndex.encode(longArrayOf(1, 2), 512, FloatArray(1024) { 0.01f })

    private fun pc() = FakePc().apply {
        models["text"] = text; models["dense"] = dense; models["vocab"] = vocab
        search = SearchInfo(SearchFiles.VERSION, models.mapValues { sha256(it.value) })
        searchIndex = index
    }

    private fun dirFiles() = files.dir.list()!!.toSortedSet()

    @Test fun downloadsAllAndKeepsMatchingSha() {
        val pc = pc()
        val s = SearchSync(pc, files)
        s.syncIndex()
        assertEquals(listOf("text", "dense", "vocab"), s.syncModels(pc.search!!))
        assertArrayEquals(text, files.model("text").readBytes())
        assertArrayEquals(dense, files.model("dense").readBytes())
        assertEquals(sha256(text), files.storedSha("text"))
        assertTrue(files.modelsPresent())
        assertArrayEquals(index, files.index.readBytes())
        assertEquals(sortedSetOf("mclip_text_q8.onnx", "mclip_dense.f32", "mclip_vocab.txt", "models.properties", "search_index.bin"), dirFiles())
    }

    @Test fun unchangedShaIsNotDownloadedAgain() {
        val pc = pc()
        SearchSync(pc, files).syncModels(pc.search!!)
        pc.calls.clear()
        assertEquals(emptyList<String>(), SearchSync(pc, files).syncModels(pc.search!!))
        assertTrue(pc.calls.none { it.startsWith("model:") })
        // Only the changed file is fetched.
        val newDense = Random(3).nextBytes(1000)
        pc.models["dense"] = newDense
        pc.search = pc.search!!.copy(files = pc.search!!.files + ("dense" to sha256(newDense)))
        assertEquals(listOf("dense"), SearchSync(pc, files).syncModels(pc.search!!))
        assertEquals(listOf("model:dense"), pc.calls)
        assertArrayEquals(newDense, files.model("dense").readBytes())
        // A file deleted on the phone is fetched again even with the same sha.
        files.model("vocab").delete()
        assertEquals(listOf("vocab"), SearchSync(pc, files).syncModels(pc.search!!))
    }

    @Test fun shaMismatchWithPingLeavesNoFile() {
        val pc = pc()
        pc.search = pc.search!!.copy(files = pc.search!!.files + ("text" to "0".repeat(64)))
        try { SearchSync(pc, files).syncModels(pc.search!!); fail() } catch (e: ShaMismatchException) { /* expected */ }
        assertFalse(files.model("text").exists())
        assertNull(files.storedSha("text"))
        assertTrue(dirFiles().none { it.endsWith(".part") })
        assertFalse(files.modelsPresent())
    }

    @Test fun headerMismatchKeepsOldFile() {
        val pc = pc()
        SearchSync(pc, files).syncModels(pc.search!!)
        val newText = Random(9).nextBytes(5000)
        pc.models["text"] = newText
        pc.search = pc.search!!.copy(files = pc.search!!.files + ("text" to sha256(newText)))
        pc.modelHeaderSha["text"] = "f".repeat(64)
        try { SearchSync(pc, files).syncModels(pc.search!!); fail() } catch (e: ShaMismatchException) { /* expected */ }
        assertArrayEquals(text, files.model("text").readBytes()) // old model still there and usable
        assertEquals(sha256(text), files.storedSha("text"))
        pc.modelHeaderSha["text"] = null // missing header is not accepted either
        try { SearchSync(pc, files).syncModels(pc.search!!); fail() } catch (e: ShaMismatchException) { /* expected */ }
        assertTrue(dirFiles().none { it.endsWith(".part") })
    }

    @Test fun brokenDownloadLeavesNoFile() {
        val pc = pc().apply { modelBreakAfter = 100_000 }
        try { SearchSync(pc, files).syncModels(pc.search!!); fail() } catch (e: IOException) { /* expected */ }
        assertFalse(files.model("text").exists())
        assertTrue(dirFiles().none { it.endsWith(".part") })
        // A leftover temp file of a killed download is removed by the next run.
        File(files.dir, "mclip_text_q8.onnx.part").writeBytes(ByteArray(10))
        pc.modelBreakAfter = null
        SearchSync(pc, files).syncModels(pc.search!!)
        assertTrue(dirFiles().none { it.endsWith(".part") })
        assertArrayEquals(text, files.model("text").readBytes())
    }

    @Test fun stopAbandonsDownload() {
        val pc = pc()
        var stop = false
        val s = SearchSync(pc, files, onProgress = { _, _, _ -> }, isStopped = { stop })
        stop = true
        try { s.syncModels(pc.search!!); fail() } catch (e: IOException) { /* expected */ }
        assertFalse(files.model("text").exists())
    }

    @Test fun brokenSearchIndexKeepsOld() {
        val pc = pc()
        SearchSync(pc, files).syncIndex()
        pc.searchIndex = index.copyOf(index.size - 3)
        try { SearchSync(pc, files).syncIndex(); fail() } catch (e: IOException) { /* expected */ }
        assertArrayEquals(index, files.index.readBytes())
    }

    @Test fun unknownVersionIsRefused() {
        val pc = pc()
        try { SearchSync(pc, files).syncModels(pc.search!!.copy(version = "other-v9")); fail() } catch (e: IOException) { /* expected */ }
        assertFalse(files.model("text").exists())
    }

    // ---------------------------------------------------------------- through the sync engine

    private fun engine(pc: FakePc, unmetered: Boolean, events: MutableList<String> = ArrayList()) = SyncEngine(
        api = pc, media = FakeMedia(listOf(item("1"))), index = IndexStore(tmp.root),
        isForeground = { false },
        listener = object : SyncListener {
            override suspend fun onSearchUpdated() { events += "search-updated" }
        },
        search = SearchSync(pc, files),
        isUnmetered = { unmetered },
    )

    @Test fun syncEngineDownloadsAfterIndex() = runBlocking {
        val pc = pc()
        val events = ArrayList<String>()
        val r = engine(pc, unmetered = true, events).run(SyncEngine.Mode.FULL)
        assertEquals(SyncResult.Done(1, 0, true, null), r)
        val i = pc.calls.indexOf("index")
        assertEquals(listOf("index", "search-index", "model:text", "model:dense", "model:vocab"), pc.calls.drop(i))
        assertTrue(files.modelsPresent())
        assertEquals(listOf("search-updated"), events)
        // Next sync: search index again, models not.
        pc.calls.clear()
        engine(pc, unmetered = true).run(SyncEngine.Mode.FULL)
        assertEquals(listOf("index", "search-index"), pc.calls.takeLast(2))
    }

    @Test fun meteredNetworkSkipsModels() = runBlocking {
        val pc = pc()
        val r = engine(pc, unmetered = false).run(SyncEngine.Mode.FULL) as SyncResult.Done
        assertEquals("Search model waits for Wi-Fi", r.searchError)
        assertTrue("search-index" in pc.calls)
        assertTrue(pc.calls.none { it.startsWith("model:") })
        assertTrue(files.index.isFile)
        assertFalse(files.modelsPresent())
    }

    @Test fun noSearchOnPcSkipsEverything() = runBlocking {
        val pc = pc().apply { search = null }
        assertEquals(SyncResult.Done(1, 0, true, null), engine(pc, unmetered = true).run(SyncEngine.Mode.FULL))
        assertTrue(pc.calls.none { it.startsWith("model:") || it == "search-index" })
    }

    @Test fun searchFailureDoesNotFailTheSync() = runBlocking {
        val pc = pc()
        pc.search = pc.search!!.copy(files = pc.search!!.files + ("vocab" to "0".repeat(64)))
        val r = engine(pc, unmetered = true).run(SyncEngine.Mode.FULL) as SyncResult.Done
        assertTrue(r.indexSaved)
        assertTrue(r.searchError!!, r.searchError!!.startsWith("Search update failed"))
        assertTrue(files.model("text").isFile) // the files before the bad one are kept
        assertFalse(files.model("vocab").exists())
    }

    // ---------------------------------------------------------------- real HTTP (MockWebServer)

    @Test fun overHttp() {
        val server = MockWebServer()
        val token = "b".repeat(64)
        val shas = mapOf("text" to sha256(text), "dense" to sha256(dense), "vocab" to sha256(vocab))
        val bodies = mapOf("text" to text, "dense" to dense, "vocab" to vocab)
        val modelHits = ArrayList<String>()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                if (request.getHeader("Authorization") != "Bearer $token") return MockResponse().setResponseCode(401)
                val path = request.path!!
                return when {
                    path == "/api/device/ping" -> MockResponse().setBody(
                        """{"ok":true,"server_version":"1.0","server_name":"PC","device_id":3,
                           "search":{"version":"mclip-b32-v1","files":{"text":"${shas["text"]}","dense":"${shas["dense"]}","vocab":"${shas["vocab"]}"}}}""",
                    )
                    path == "/api/device/search-index" -> MockResponse().setBody(Buffer().write(index))
                    path.startsWith("/api/device/model/") -> {
                        val name = path.substringAfterLast('/')
                        modelHits += name
                        val sha = if (name == "dense") "1".repeat(64) else shas.getValue(name) // dense: wrong header
                        MockResponse().setHeader("X-Sha256", sha).setBody(Buffer().write(bodies.getValue(name)))
                    }
                    else -> MockResponse().setResponseCode(404)
                }
            }
        }
        server.start()
        try {
            val api = OkHttpDeviceApi(server.hostName, server.port, token)
            val ping = api.ping()!!
            assertEquals(SearchInfo("mclip-b32-v1", shas), ping.search)
            val s = SearchSync(api, files)
            s.syncIndex()
            assertArrayEquals(index, files.index.readBytes())
            try { s.syncModels(ping.search!!); fail() } catch (e: ShaMismatchException) { /* dense header wrong */ }
            assertArrayEquals(text, files.model("text").readBytes())
            assertFalse(files.model("dense").exists())
            assertEquals(listOf("text", "dense"), modelHits)
            assertTrue(dirFiles().none { it.endsWith(".part") })
        } finally {
            server.shutdown()
        }
    }
}
