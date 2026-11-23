package com.ratedgallery.phone.core.search

import org.junit.Assume.assumeTrue
import java.io.File

/** Reference data of the PC side (paths set in app/build.gradle.kts, relative to phone-app/). */
object TestData {
    private fun dir(prop: String, fallback: String): File {
        System.getProperty(prop)?.let { return File(it) }
        // Run from an IDE: the working directory is phone-app/app or phone-app.
        return listOf(File("../$fallback"), File("../../$fallback")).firstOrNull { it.isDirectory } ?: File("../$fallback")
    }

    val data: File get() = dir("rg.testData", "tests/data")
    val models: File get() = dir("rg.models", "data/models")

    /** Skips the test (with a clear message) if one of [files] is missing. */
    fun require(vararg files: File) {
        for (f in files) {
            assumeTrue("SKIPPED: ${f.absolutePath} is missing (download the search model on the PC first)", f.isFile)
        }
    }
}
