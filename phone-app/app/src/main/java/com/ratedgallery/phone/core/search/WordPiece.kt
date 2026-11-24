package com.ratedgallery.phone.core.search

import java.io.File

/**
 * BERT WordPiece tokenizer (cased, no lowercasing, no accent removal), exactly as API.md section 6
 * and ratedgallery/search.py. Works on Unicode code points, not UTF-16 chars.
 */
class WordPiece(tokens: List<String>) {
    private val vocab = HashMap<String, Int>(tokens.size * 2)

    init {
        // Like the Python dict comprehension: a repeated token keeps its last id.
        tokens.forEachIndexed { i, t -> vocab[t] = i }
    }

    val size: Int get() = vocab.size

    /** WordPiece ids of one basic token (greedy longest match from the start). */
    fun wordIds(word: String): List<Int> {
        val cps = word.codePoints().toArray()
        if (cps.size > MAX_WORD_CHARS) return listOf(UNK)
        val ids = ArrayList<Int>()
        var start = 0
        while (start < cps.size) {
            var end = cps.size
            var found = -1
            while (start < end) {
                val sub = String(cps, start, end - start)
                val piece = if (start == 0) sub else "##$sub"
                val id = vocab[piece]
                if (id != null) {
                    found = id
                    break
                }
                end--
            }
            if (found < 0) return listOf(UNK)
            ids.add(found)
            start = end
        }
        return ids
    }

    /** Token ids of a query: [CLS] + pieces + [SEP], at most [MAX_LEN] tokens ([SEP] stays last). */
    fun encode(text: String): LongArray {
        val ids = ArrayList<Int>()
        ids.add(CLS)
        for (w in basicTokens(text)) ids.addAll(wordIds(w))
        val n = minOf(ids.size, MAX_LEN - 1)
        val out = LongArray(n + 1)
        for (i in 0 until n) out[i] = ids[i].toLong()
        out[n] = SEP.toLong()
        return out
    }

    companion object {
        const val CLS = 101
        const val SEP = 102
        const val UNK = 100
        const val MAX_LEN = 128
        const val MAX_WORD_CHARS = 100

        /** Reads the vocab file: UTF-8, one token per line, the line number (from 0) is the id. */
        fun load(file: File): WordPiece {
            // Line ends like the Python reference (text mode): "\r\n" and "\r" count as "\n". A PC installed on
            // Windows wrote the file with "\r\n"; split on '\n' alone, every token kept a '\r' and became [UNK].
            // A final empty line is not a token.
            val text = file.readText(Charsets.UTF_8).replace("\r\n", "\n").replace('\r', '\n')
            val lines = text.split('\n').toMutableList()
            if (lines.isNotEmpty() && lines.last().isEmpty()) lines.removeAt(lines.size - 1)
            return WordPiece(lines)
        }

        private val CJK = arrayOf(
            0x4E00..0x9FFF, 0x3400..0x4DBF, 0x20000..0x2A6DF, 0x2A700..0x2B73F, 0x2B740..0x2B81F,
            0x2B820..0x2CEAF, 0xF900..0xFAFF, 0x2F800..0x2FA1F,
        )

        fun isCjk(cp: Int): Boolean = CJK.any { cp in it }

        /** Categories Cc, Cf, Cs, Co, Cn (tab, newline and carriage return are not control here). */
        fun isControl(cp: Int): Boolean {
            if (cp == '\t'.code || cp == '\n'.code || cp == '\r'.code) return false
            return when (Character.getType(cp).toByte()) {
                Character.CONTROL, Character.FORMAT, Character.SURROGATE, Character.PRIVATE_USE,
                Character.UNASSIGNED -> true
                else -> false
            }
        }

        /** Python str.isspace() for the characters left after cleaning (Zs, Zl, Zp, \t \n \r). */
        fun isWhitespace(cp: Int): Boolean {
            if (cp == ' '.code || cp == '\t'.code || cp == '\n'.code || cp == '\r'.code) return true
            if (cp in 0x1C..0x1F || cp == 0x0B || cp == 0x0C || cp == 0x85) return true
            return when (Character.getType(cp).toByte()) {
                Character.SPACE_SEPARATOR, Character.LINE_SEPARATOR, Character.PARAGRAPH_SEPARATOR -> true
                else -> false
            }
        }

        /** ASCII 33-47, 58-64, 91-96, 123-126 or Unicode category P*. */
        fun isPunct(cp: Int): Boolean {
            if (cp in 33..47 || cp in 58..64 || cp in 91..96 || cp in 123..126) return true
            return when (Character.getType(cp).toByte()) {
                Character.CONNECTOR_PUNCTUATION, Character.DASH_PUNCTUATION, Character.START_PUNCTUATION,
                Character.END_PUNCTUATION, Character.INITIAL_QUOTE_PUNCTUATION,
                Character.FINAL_QUOTE_PUNCTUATION, Character.OTHER_PUNCTUATION -> true
                else -> false
            }
        }

        /** Clean, split on whitespace, split punctuation into its own words. */
        fun basicTokens(text: String): List<String> {
            val sb = StringBuilder(text.length + 8)
            text.codePoints().forEach { cp ->
                when {
                    cp == 0 || cp == 0xFFFD || isControl(cp) -> Unit
                    isWhitespace(cp) -> sb.append(' ')
                    isCjk(cp) -> sb.append(' ').appendCodePoint(cp).append(' ')
                    else -> sb.appendCodePoint(cp)
                }
            }
            val words = ArrayList<String>()
            for (w in sb.split(' ')) {
                if (w.isEmpty()) continue
                val cur = StringBuilder()
                w.codePoints().forEach { cp ->
                    if (isPunct(cp)) {
                        if (cur.isNotEmpty()) {
                            words.add(cur.toString())
                            cur.setLength(0)
                        }
                        words.add(String(Character.toChars(cp)))
                    } else {
                        cur.appendCodePoint(cp)
                    }
                }
                if (cur.isNotEmpty()) words.add(cur.toString())
            }
            return words
        }
    }
}
