package com.ratedgallery.phone.core

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.IOException
import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

class RatingQueueTest {
    @get:Rule val tmp = TemporaryFolder()

    private var clock = 1_000L
    private fun queue(dir: File = tmp.root) = RatingQueue(dir, now = { clock })

    @Test fun newRatingReplacesQueuedOne() {
        val q = queue()
        q.set(7, 5)
        clock = 2_000
        q.set(7, 8)
        assertEquals(listOf(PendingRating(7, 8, 2_000)), q.all())
        assertEquals(1, q.size())
    }

    @Test fun createdGrowsForSamePhotoEvenInSameMillisecond() {
        val q = queue()
        q.set(7, 5)
        q.set(7, 6)
        q.set(7, 6)
        assertEquals(listOf(PendingRating(7, 6, 1_002)), q.all())
    }

    @Test fun zeroRemovesQueuedRating() {
        val q = queue()
        q.set(1, 5)
        q.set(2, 9)
        q.set(1, 0)
        assertEquals(listOf(PendingRating(2, 9, 1_000)), q.all())
        q.set(2, 0)
        assertTrue(q.isEmpty())
        assertEquals("{}", q.toBridgeJson())
    }

    @Test fun zeroForUnknownPhotoWritesNothing() {
        queue().set(5, 0)
        assertFalse(File(tmp.root, RatingQueue.FILE_NAME).exists())
    }

    @Test fun badValuesThrowAndChangeNothing() {
        val q = queue()
        q.set(1, 4)
        for ((id, r) in listOf(1L to 11, 1L to -1, 0L to 5, -3L to 5)) {
            try {
                q.set(id, r); fail("accepted $id/$r")
            } catch (e: IllegalArgumentException) { /* ok */ }
        }
        assertEquals(listOf(PendingRating(1, 4, 1_000)), q.all())
    }

    @Test fun persistsAcrossInstances() {
        queue().set(10, 3)
        clock = 5_000
        queue().set(11, 10)
        val again = RatingQueue(tmp.root)
        assertEquals(listOf(PendingRating(10, 3, 1_000), PendingRating(11, 10, 5_000)), again.all())
        val o = JSONObject(File(tmp.root, RatingQueue.FILE_NAME).readText())
        assertEquals(2, o.getJSONArray("items").length())
        assertEquals(11, o.getJSONArray("items").getJSONObject(1).getLong("photo_id"))
    }

    @Test fun writeIsAtomicAndFailedWriteKeepsOldQueue() {
        val q = queue()
        q.set(1, 5)
        // Only the queue file: the temp file was renamed over it.
        assertEquals(listOf(RatingQueue.FILE_NAME), tmp.root.list()!!.toList())
        val before = File(tmp.root, RatingQueue.FILE_NAME).readText()

        // Make the temp file impossible to create: the write fails before the rename.
        val blocker = File(tmp.root, RatingQueue.FILE_NAME + ".tmp")
        assertTrue(blocker.mkdir())
        File(blocker, "x").writeText("x") // not empty, so the cleanup cannot remove it
        try {
            q.set(2, 7); fail("expected IOException")
        } catch (e: IOException) { /* ok */ }
        assertEquals(before, File(tmp.root, RatingQueue.FILE_NAME).readText())
        assertEquals(listOf(PendingRating(1, 5, 1_000)), q.all())
        blocker.deleteRecursively()

        q.set(2, 7)
        assertEquals(2, queue().size())
        assertEquals(listOf(RatingQueue.FILE_NAME), tmp.root.list()!!.toList())
    }

    @Test fun damagedFileCountsAsEmpty() {
        File(tmp.root, RatingQueue.FILE_NAME).writeText("{\"items\":[{\"photo_id\":1,\"rat")
        val q = queue()
        assertTrue(q.isEmpty())
        q.set(3, 2)
        assertEquals(listOf(PendingRating(3, 2, 1_000)), queue().all())
    }

    @Test fun createsFolder() {
        val dir = File(tmp.root, "a/b")
        queue(dir).set(1, 1)
        assertEquals(1, queue(dir).size())
    }

    @Test fun removeSentOnlyRemovesUnchangedItems() {
        val q = queue()
        q.set(1, 5); q.set(2, 6); q.set(3, 7)
        val sent = q.all()
        clock = 9_000
        q.set(2, 9) // rated again while the request ran
        val removed = q.removeSent(sent, setOf(1L, 2L, 99L))
        assertEquals(1, removed)
        assertEquals(listOf(PendingRating(3, 7, 1_000), PendingRating(2, 9, 9_000)), q.all())
    }

    @Test fun concurrentAccessLosesNothing() {
        val threads = 8
        val perThread = 40
        val pool = Executors.newFixedThreadPool(threads + 2)
        val start = CountDownLatch(1)
        val errors = Collections.synchronizedList(ArrayList<Throwable>())
        val tick = AtomicLong(0)
        val futures = (0 until threads).map { t ->
            pool.submit {
                start.await()
                // A separate instance per thread, like the bridge thread and the sync worker.
                val q = RatingQueue(tmp.root, now = { tick.incrementAndGet() })
                try {
                    for (i in 1..perThread) {
                        val id = (t * 1000 + i).toLong()
                        q.set(id, 1 + i % 10)
                        q.set(id, 1 + (i + 1) % 10) // replace
                        q.set(42, 1 + i % 10) // all threads fight over one photo too
                    }
                } catch (e: Throwable) { errors += e }
            }
        }
        // Readers and a "sync" removing photo 42 while the writers run.
        val readers = (0 until 2).map {
            pool.submit {
                start.await()
                val q = RatingQueue(tmp.root)
                try {
                    repeat(100) {
                        JSONObject(q.toBridgeJson())
                        val sent = q.all().filter { it.photoId == 42L }
                        q.removeSent(sent, setOf(42L))
                    }
                } catch (e: Throwable) { errors += e }
            }
        }
        start.countDown()
        (futures + readers).forEach { it.get(60, TimeUnit.SECONDS) }
        pool.shutdown()
        assertEquals(emptyList<Throwable>(), errors)

        val all = RatingQueue(tmp.root).all().filter { it.photoId != 42L }
        assertEquals(threads * perThread, all.size)
        for (r in all) {
            val i = (r.photoId % 1000).toInt()
            assertEquals(1 + (i + 1) % 10, r.rating)
        }
        assertEquals(listOf(RatingQueue.FILE_NAME), tmp.root.list()!!.toList())
    }
}
