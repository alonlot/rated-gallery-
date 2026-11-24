package com.ratedgallery.phone.core

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class SyncEngineTest {
    @get:Rule val tmp = TemporaryFolder()

    private var clock = 0L
    private var foreground = false
    private val sleeps = ArrayList<Long>()
    private val phases = ArrayList<String>()
    private var indexEvents = 0
    private val uploadTotals = ArrayList<Int>()

    private fun engine(pc: DeviceApi, media: MediaSource) = SyncEngine(
        api = pc, media = media, index = IndexStore(tmp.root),
        isForeground = { foreground },
        listener = object : SyncListener {
            override suspend fun onProgress(phase: String, done: Int, total: Int, message: String) {
                if (phases.lastOrNull() != phase) phases += phase
                if (phase == "upload") uploadTotals += total
            }
            override suspend fun onIndexSaved() { indexEvents++ }
        },
        sleep = { sleeps += it; clock += it },
        now = { clock },
    )

    @Test fun unreachableStopsQuietly() = runBlocking {
        val pc = FakePc().apply { reachable = false }
        val r = engine(pc, FakeMedia(listOf(item("1")))).run(SyncEngine.Mode.FULL)
        assertEquals(SyncResult.Unreachable, r)
        assertEquals(listOf("ping"), pc.calls)
    }

    @Test fun fullSyncUploadsEverythingInBatches() = runBlocking {
        val items = (1..45).map { item(it.toString()) }
        val pc = FakePc().apply { index = """{"version":1,"persons":[],"photos":[{"mid":"1"}]}""" }
        val r = engine(pc, FakeMedia(items)).run(SyncEngine.Mode.FULL)
        assertEquals(SyncResult.Done(45, 0, true), r)
        assertEquals(items.map { it.mid }, pc.uploads)
        assertEquals(listOf("ping", "manifest", "wanted"), pc.calls.take(3))
        // 3 batches of up to 20 + one final empty answer
        assertEquals(4, pc.calls.count { it == "wanted" })
        assertEquals(listOf("status", "index"), pc.calls.takeLast(2))
        assertEquals(pc.index, IndexStore(tmp.root).readOrEmpty())
        assertEquals(1, indexEvents)
        assertEquals(listOf("connect", "manifest", "upload", "processing", "index"), phases)
        assertEquals(45, uploadTotals.max())
        assertEquals(setOf(0, 45), uploadTotals.toSet())
    }

    @Test fun longUploadRefreshesTheIndexOnTheWay() = runBlocking {
        // 45 uploads, each takes 1 minute: the index comes every 3 minutes, and once more at the end
        val items = (1..45).map { item(it.toString()) }
        val pc = FakePc().apply { index = """{"version":1,"persons":[],"photos":[{"mid":"1"}]}""" }
        var saves = 0
        val e = SyncEngine(
            api = pc, media = FakeMedia(items), index = IndexStore(tmp.root), isForeground = { false },
            listener = object : SyncListener {
                override suspend fun onProgress(phase: String, done: Int, total: Int, message: String) {
                    if (phase == "upload" && done > 0) clock += 60_000
                }
                override suspend fun onIndexSaved() { saves++ }
            },
            sleep = { clock += it }, now = { clock },
        )
        assertEquals(SyncResult.Done(45, 0, true), e.run(SyncEngine.Mode.FULL))
        assertEquals(45, pc.uploads.size)
        assertEquals(15 + 1, pc.calls.count { it == "index" })
        assertEquals(16, saves)
        assertEquals("index", pc.calls.last())
    }

    @Test fun nothingToUpload() = runBlocking {
        val pc = FakePc().apply { known.addAll(listOf("1", "2")) }
        val r = engine(pc, FakeMedia(listOf(item("1"), item("2")))).run(SyncEngine.Mode.FULL)
        assertEquals(SyncResult.Done(0, 0, true), r)
        assertTrue(pc.uploads.isEmpty())
    }

    @Test fun unauthorizedAnywhere() = runBlocking {
        val pc = FakePc().apply { unauthorized = true }
        assertEquals(SyncResult.Unauthorized, engine(pc, FakeMedia(listOf(item("1")))).run(SyncEngine.Mode.FULL))
    }

    @Test fun noPermissionSendsNoManifest() = runBlocking {
        val pc = FakePc()
        val r = engine(pc, FakeMedia(listOf(item("1")), readable = false)).run(SyncEngine.Mode.FULL)
        assertEquals(SyncResult.NoPermission, r)
        assertFalse("manifest" in pc.calls)
    }

    @Test fun unreadableAndRejectedFilesDoNotLoopForever() = runBlocking {
        val pc = FakePc().apply { failUploadOf = "3"; neverForget = "4" }
        val media = FakeMedia((1..5).map { item(it.toString()) }).apply { unreadable += "2" }
        val r = engine(pc, media).run(SyncEngine.Mode.FULL)
        // 2 unreadable, 3 rejected by PC; 4 is uploaded once even though the PC keeps asking.
        assertEquals(SyncResult.Done(3, 2, true), r)
        assertEquals(listOf("1", "4", "5"), pc.uploads)
    }

    @Test fun waitsForPcProcessingOnlyInForeground() = runBlocking {
        foreground = true
        val pc = FakePc().apply { pendingSequence = ArrayDeque(listOf(3, 2, 0)) }
        engine(pc, FakeMedia(listOf(item("1")))).run(SyncEngine.Mode.FULL)
        assertEquals(3, pc.calls.count { it == "status" })
        assertEquals(listOf(5000L, 5000L), sleeps)

        foreground = false
        sleeps.clear()
        val pc2 = FakePc().apply { pendingSequence = ArrayDeque(listOf(3, 2, 0)) }
        engine(pc2, FakeMedia(listOf(item("1")))).run(SyncEngine.Mode.FULL)
        assertEquals(1, pc2.calls.count { it == "status" })
        assertTrue(sleeps.isEmpty())
        assertEquals("index", pc2.calls.last())
    }

    @Test fun servesPcRequestsWhileWaitingForProcessing() = runBlocking {
        foreground = true
        val media = FakeMedia(listOf(item("1"), item("2")))
        val pc = FakePc().apply {
            known.addAll(listOf("1", "2")); want.add("1")
            pendingSequence = ArrayDeque(listOf(1, 1, 1, 0))
            // The user opens photo 2 on the PC while the phone waits.
            onStatus = { n -> if (n == 2) want.add("2") }
        }
        val r = engine(pc, media).run(SyncEngine.Mode.FULL)
        assertEquals(SyncResult.Done(2, 0, true), r)
        assertEquals(listOf("1", "2"), pc.uploads)
        assertEquals("index", pc.calls.last())
    }

    @Test fun reportsUndeliverable() = runBlocking {
        val bad = ArrayList<String>()
        val pc = FakePc()
        val media = FakeMedia(listOf(item("1"), item("2"))).apply { unreadable += "2" }
        val e = SyncEngine(pc, media, IndexStore(tmp.root), { false }, object : SyncListener {
            override suspend fun onUndeliverable(mid: String) { bad += mid }
        })
        e.run(SyncEngine.Mode.FULL)
        assertEquals(listOf("2"), bad)
    }

    @Test fun processingWaitIsCappedAtTenMinutes() = runBlocking {
        foreground = true
        val pc = FakePc().apply { pendingSequence = ArrayDeque(listOf(5)) } // never finishes
        val r = engine(pc, FakeMedia(listOf(item("1")))).run(SyncEngine.Mode.FULL)
        assertTrue(r is SyncResult.Done)
        assertEquals(10 * 60_000L, sleeps.sum())
        assertEquals("index", pc.calls.last())
    }

    @Test fun quickModeSkipsManifestUntil404() = runBlocking {
        val pc = FakePc().apply { known.add("1"); want.add("1") }
        val media = FakeMedia(listOf(item("1"), item("2")))
        val r = engine(pc, media).run(SyncEngine.Mode.QUICK)
        assertEquals(SyncResult.Done(1, 0, true), r)
        assertFalse("manifest" in pc.calls)

        // The PC asks for a mid it does not know yet -> the engine sends the manifest once and retries.
        val pc2 = FakePc().apply { want.add("2"); autoWantNew = false }
        val r2 = engine(pc2, media).run(SyncEngine.Mode.QUICK)
        assertEquals(SyncResult.Done(1, 0, true), r2)
        assertEquals(listOf("upload:2", "manifest", "upload:2"), pc2.calls.filter { it == "manifest" || it.startsWith("upload") })
    }

    @Test fun interruptedSyncResumes() = runBlocking {
        val items = (1..10).map { item(it.toString()) }
        val pc = FakePc()
        val flaky = object : DeviceApi by pc {
            var n = 0
            override fun upload(mid: String, media: OpenedMedia) {
                if (++n == 4) throw java.io.IOException("Wi-Fi lost")
                pc.upload(mid, media)
            }
        }
        val r1 = engine(flaky, FakeMedia(items)).run(SyncEngine.Mode.FULL)
        assertTrue(r1 is SyncResult.Failed)
        assertEquals(listOf("1", "2", "3"), pc.uploads)
        val r2 = engine(pc, FakeMedia(items)).run(SyncEngine.Mode.FULL)
        assertEquals(SyncResult.Done(7, 0, true), r2)
        assertEquals(items.map { it.mid }, pc.uploads)
    }

    @Test fun badIndexKeepsOldOne() = runBlocking {
        IndexStore(tmp.root).saveString("""{"version":1,"persons":[],"photos":[{"mid":"9"}]}""")
        val pc = FakePc().apply { index = "not json" }
        val r = engine(pc, FakeMedia(emptyList())).run(SyncEngine.Mode.FULL)
        assertTrue(r is SyncResult.Failed)
        assertTrue(IndexStore(tmp.root).readOrEmpty().contains("\"9\""))
        assertEquals(0, indexEvents)
    }
}
