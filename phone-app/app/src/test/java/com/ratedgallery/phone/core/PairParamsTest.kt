package com.ratedgallery.phone.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PairParamsTest {
    @Test fun parsesValidLink() {
        assertEquals(
            PairParams("192.168.1.10", 8765, "123456"),
            PairParams.parseLink("ratedgallery://pair?host=192.168.1.10&port=8765&code=123456"),
        )
    }

    @Test fun parameterOrderAndCaseOfScheme() {
        assertEquals(
            PairParams("10.0.0.2", 80, "000001"),
            PairParams.parseLink("RatedGallery://PAIR?code=000001&port=80&host=10.0.0.2"),
        )
    }

    @Test fun decodesPercentEncodingAndTrailingSlash() {
        assertEquals(
            PairParams("my-pc.local", 8765, "654321"),
            PairParams.parseLink("ratedgallery://pair/?host=my%2Dpc.local&port=8765&code=654321#x"),
        )
    }

    @Test fun ipv6Host() {
        assertEquals("fe80::1", PairParams.parseLink("ratedgallery://pair?host=%5Bfe80::1%5D&port=1&code=111111")?.host)
    }

    @Test fun rejectsBadLinks() {
        val bad = listOf(
            null, "", "http://pair?host=1.2.3.4&port=1&code=123456",
            "ratedgallery://other?host=1.2.3.4&port=1&code=123456",
            "ratedgallery://pair", "ratedgallery://pair?host=1.2.3.4&port=1",
            "ratedgallery://pair?host=1.2.3.4&port=0&code=123456",
            "ratedgallery://pair?host=1.2.3.4&port=70000&code=123456",
            "ratedgallery://pair?host=1.2.3.4&port=abc&code=123456",
            "ratedgallery://pair?host=1.2.3.4&port=80&code=12345",
            "ratedgallery://pair?host=1.2.3.4&port=80&code=12345a",
            "ratedgallery://pair?host=&port=80&code=123456",
            "ratedgallery://pair?host=evil.com/x&port=80&code=123456",
            "ratedgallery://pair?host=a%20b&port=80&code=123456",
            "ratedgallery://pair?host=1.2.3.4&port=80&code=%ZZ",
        )
        for (b in bad) assertNull("should reject: $b", PairParams.parseLink(b))
    }

    @Test fun validatesManualInput() {
        assertEquals(PairParams("192.168.0.5", 8765, "123456"), PairParams.validate(" 192.168.0.5 ", "8765 ", "123 456"))
        assertNull(PairParams.validate("192.168.0.5", "", "123456"))
        assertNull(PairParams.validate("http://192.168.0.5", "8765", "123456"))
    }
}
