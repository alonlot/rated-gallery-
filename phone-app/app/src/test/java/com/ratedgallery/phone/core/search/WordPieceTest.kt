package com.ratedgallery.phone.core.search

import org.json.JSONArray
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.BeforeClass
import org.junit.Test
import java.io.File

class WordPieceTest {
    companion object {
        private var tok: WordPiece? = null

        @BeforeClass @JvmStatic fun load() {
            val f = File(TestData.models, "mclip_vocab.txt")
            if (f.isFile) tok = WordPiece.load(f)
        }
    }

    private fun tok(): WordPiece {
        TestData.require(File(TestData.models, "mclip_vocab.txt"))
        return tok!!
    }

    @Test fun vocabSize() {
        assertEquals(119547, tok().size)
    }

    @Test fun referenceCases() {
        val casesFile = File(TestData.data, "mclip_tokens.json")
        TestData.require(casesFile)
        val t = tok()
        val cases = JSONArray(casesFile.readText())
        assertTrue(cases.length() > 0)
        val failures = ArrayList<String>()
        for (i in 0 until cases.length()) {
            val c = cases.getJSONObject(i)
            val q = c.getString("q")
            val ids = c.getJSONArray("ids")
            val want = LongArray(ids.length()) { ids.getLong(it) }
            val got = t.encode(q)
            if (!want.contentEquals(got)) failures += "${JSONArray(listOf(q))}: want ${want.toList()} got ${got.toList()}"
        }
        assertEquals(failures.joinToString("\n"), 0, failures.size)
        println("tokenizer: ${cases.length()} reference cases match")
    }

    // Small synthetic vocab: checks the rules without the real vocab file.
    private fun mini(vararg tokens: String): WordPiece {
        val list = ArrayList<String>()
        repeat(100) { list.add("[unused$it]") }
        list.add("[UNK]"); list.add("[CLS]"); list.add("[SEP]")
        list.addAll(tokens)
        return WordPiece(list)
    }

    @Test fun vocabWithWindowsLineEndsLoadsLikeUnix() {
        // a PC installed on Windows wrote mclip_vocab.txt with CR LF line ends; then every token became [UNK]
        val tokens = ArrayList<String>()
        repeat(100) { tokens.add("[unused$it]") }
        tokens.addAll(listOf("[UNK]", "[CLS]", "[SEP]", "river", "sun", "##set"))
        val lf = "\n"
        val crlf = "\r\n"
        val unix = File.createTempFile("vocab", ".txt").apply { deleteOnExit(); writeText(tokens.joinToString(lf) + lf) }
        val win = File.createTempFile("vocab", ".txt").apply { deleteOnExit(); writeText(tokens.joinToString(crlf) + crlf) }
        val a = WordPiece.load(unix)
        val b = WordPiece.load(win)
        assertEquals(a.size, b.size)
        assertArrayEquals(longArrayOf(101, 103, 104, 105, 102), b.encode("river sunset"))
        assertArrayEquals(a.encode("river sunset"), b.encode("river sunset"))
    }

    @Test fun greedyLongestMatchAndUnk() {
        val t = mini("un", "##aff", "##able", "aff", "!", "a")
        // 103=un 104=##aff 105=##able 106=aff 107=! 108=a
        assertArrayEquals(longArrayOf(101, 103, 104, 105, 102), t.encode("unaffable"))
        assertArrayEquals(longArrayOf(101, 103, 104, 105, 107, 102), t.encode("unaffable!"))
        assertArrayEquals(longArrayOf(101, 100, 102), t.encode("unknown"))  // one part has no match -> whole word [UNK]
        assertArrayEquals(longArrayOf(101, 108, 107, 108, 102), t.encode("a!a"))
        assertArrayEquals(longArrayOf(101, 100, 102), t.encode("a".repeat(101)))
    }

    @Test fun cutTo128KeepsSepLast() {
        val t = mini("a")
        val ids = t.encode(List(300) { "a" }.joinToString(" "))
        assertEquals(128, ids.size)
        assertEquals(101L, ids[0])
        assertEquals(102L, ids[127])
        assertEquals(103L, ids[126])
    }

    @Test fun basicTokensCleaningAndCodePoints() {
        assertEquals(listOf("a", "b"), WordPiece.basicTokens("a\u0000​� b"))  // NUL, Cf, U+FFFD removed
        assertEquals(listOf("ab", "c"), WordPiece.basicTokens("a‍b\tc"))
        assertEquals(listOf("x", "中", "文", "y"), WordPiece.basicTokens("x中文y"))
        // CJK extension B (outside the BMP) is one code point, split out.
        assertEquals(listOf("a", "𠀀", "b"), WordPiece.basicTokens("a𠀀b"))
        // Emoji (So, outside the BMP) stays inside the word.
        assertEquals(listOf("hi😀"), WordPiece.basicTokens("hi😀"))
        // Unicode punctuation (Hebrew maqaf U+05BE is Pd) and ASCII symbols split.
        assertEquals(listOf("בית", "־", "ספר"), WordPiece.basicTokens("בית־ספר"))
        assertEquals(listOf("a", "$", "b", "¿"), WordPiece.basicTokens("a\$b¿"))
        // Lone surrogate (Cs) removed; no-break space and line separator are spaces.
        assertEquals(listOf("a", "b", "c"), WordPiece.basicTokens("a\uD800 b c"))
    }
}
