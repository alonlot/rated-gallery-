package com.ratedgallery.phone.core

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.IOException

/** The rating upload of a sync (API.md section 7). */
class SyncRatingsTest {
    @get:Rule val tmp = TemporaryFolder()

    private var clock = 1_000L
    private val indexDir by lazy { File(tmp.root, "index") }
    private val queue by lazy { RatingQueue(File(tmp.root, "ratings"), now = { clock }) }
    private var indexEvents = 0
    private val sentEvents = ArrayList<Int>()

    private fun engine(pc: DeviceApi, ratings: RatingQueue? = queue) = SyncEngine(
        api = pc, media = FakeMedia(listOf(item("1"), item("2"))), index = IndexStore(indexDir),
        isForeground = { false },
        listener = object : SyncListener {
            override suspend fun onIndexSaved() { indexEvents++ }
            override suspend fun onRatingsSent(removed: Int) { sentEvents += removed }
        },
        sleep = { clock += it }, now = { clock },
        ratings = ratings,
    )

    private val newIndex = """{"version":1,"persons":[],"photos":[{"id":5,"mid":"1","r":8}]}"""

    @Test fun ratingsAreSentBeforeTheIndexAndAcceptedOnesLeave() = runBlocking {
        queue.set(5, 8)
        clock = 2_000
        queue.set(6, 3)
        val pc = FakePc().apply { index = newIndex }
        val r = engine(pc).run(SyncEngine.Mode.FULL)
        assertEquals(SyncResult.Done(2, 0, true, ratingsSent = 2), r)
        // After the photo uploads, right before the index.
        val i = pc.calls.indexOf("ratings")
        assertEquals("index", pc.calls[i + 1])
        assertTrue(pc.calls.indexOf("upload:2") < i)
        assertEquals(1, pc.calls.count { it == "ratings" })
        assertEquals(listOf(listOf(PendingRating(5, 8, 1_000), PendingRating(6, 3, 2_000))), pc.ratingPosts)
        assertTrue(queue.isEmpty())
        assertEquals(newIndex, IndexStore(indexDir).readOrEmpty())
        assertEquals(1, indexEvents)
        assertEquals(listOf(2), sentEvents)
    }

    @Test fun emptyQueueSendsNothing() = runBlocking {
        val pc = FakePc()
        assertEquals(SyncResult.Done(2, 0, true), engine(pc).run(SyncEngine.Mode.FULL))
        assertFalse("ratings" in pc.calls)
        assertTrue(sentEvents.isEmpty())
    }

    @Test fun noQueueConfiguredSendsNothing() = runBlocking {
        queue.set(5, 8)
        val pc = FakePc()
        assertEquals(SyncResult.Done(2, 0, true), engine(pc, ratings = null).run(SyncEngine.Mode.FULL))
        assertFalse("ratings" in pc.calls)
    }

    @Test fun acceptedAndRejectedLeaveUnansweredStay() = runBlocking {
        queue.set(1, 5); queue.set(2, 6); queue.set(3, 7)
        val pc = FakePc().apply {
            ratingsAnswer = { RatingsResult(listOf(1L), listOf(RejectedRating(2, "not a photo of me"))) }
        }
        val r = engine(pc).run(SyncEngine.Mode.QUICK) as SyncResult.Done
        assertEquals(2, r.ratingsSent)
        assertNull(r.ratingsError)
        assertEquals(listOf(PendingRating(3, 7, 1_000)), queue.all())

        // Next sync sends only what is left.
        pc.ratingsAnswer = null
        engine(pc).run(SyncEngine.Mode.QUICK)
        assertEquals(listOf(PendingRating(3, 7, 1_000)), pc.ratingPosts[1])
        assertTrue(queue.isEmpty())
    }

    @Test fun ratingChangedDuringTheSyncStaysQueued() = runBlocking {
        queue.set(1, 5); queue.set(2, 6)
        val pc = FakePc()
        pc.ratingsAnswer = { items ->
            // The user rates photo 1 again on the WebView thread while the POST runs.
            clock = 5_000
            queue.set(1, 9)
            queue.set(4, 2)
            RatingsResult(items.map { it.photoId }, emptyList())
        }
        val r = engine(pc).run(SyncEngine.Mode.FULL) as SyncResult.Done
        assertEquals(1, r.ratingsSent)
        assertEquals(setOf(PendingRating(1, 9, 5_000), PendingRating(4, 2, 5_000)), queue.all().toSet())
    }

    @Test fun networkErrorKeepsQueueAndSyncFinishes() = runBlocking {
        queue.set(1, 5)
        val pc = FakePc().apply { ratingsError = IOException("connection reset"); index = newIndex }
        val r = engine(pc).run(SyncEngine.Mode.FULL) as SyncResult.Done
        assertEquals(2, r.uploaded)
        assertTrue(r.indexSaved)
        assertEquals(0, r.ratingsSent)
        assertNotNull(r.ratingsError)
        assertTrue(r.ratingsError!!, r.ratingsError!!.contains("connection reset"))
        assertEquals(listOf(PendingRating(1, 5, 1_000)), queue.all())
        assertEquals(newIndex, IndexStore(indexDir).readOrEmpty())
        assertEquals(1, indexEvents)
        assertTrue(sentEvents.isEmpty())
    }

    @Test fun serverErrorKeepsQueueAndSyncFinishes() = runBlocking {
        queue.set(1, 5)
        val pc = FakePc().apply { ratingsError = HttpStatusException(500, "boom") }
        val r = engine(pc).run(SyncEngine.Mode.FULL) as SyncResult.Done
        assertTrue(r.indexSaved)
        assertEquals(1, queue.size())
    }

    @Test fun oldPcWithoutEndpointKeepsQueueAndSyncFinishes() = runBlocking {
        queue.set(1, 5)
        val pc = FakePc().apply { ratingsError = NotFoundException("Not Found"); index = newIndex }
        val r = engine(pc).run(SyncEngine.Mode.FULL) as SyncResult.Done
        assertEquals(2, r.uploaded)
        assertTrue(r.indexSaved)
        assertTrue(r.ratingsError!!, r.ratingsError!!.contains("PC version"))
        assertEquals(listOf(PendingRating(1, 5, 1_000)), queue.all())
        assertEquals(listOf("ratings", "index"), pc.calls.takeLast(2))
        assertEquals(newIndex, IndexStore(indexDir).readOrEmpty())
    }

    @Test fun unauthorizedStopsAndKeepsQueue() = runBlocking {
        queue.set(1, 5)
        val pc = FakePc()
        val api = object : DeviceApi by pc {
            override fun postRatings(items: List<PendingRating>): RatingsResult = throw UnauthorizedException("bad token")
        }
        assertEquals(SyncResult.Unauthorized, engine(api).run(SyncEngine.Mode.FULL))
        assertEquals(1, queue.size())
        assertFalse("index" in pc.calls)
    }

    @Test fun unreachablePcSendsNothing() = runBlocking {
        queue.set(1, 5)
        val pc = FakePc().apply { reachable = false }
        assertEquals(SyncResult.Unreachable, engine(pc).run(SyncEngine.Mode.FULL))
        assertEquals(1, queue.size())
    }
}
