package com.ratedgallery.phone.core.search

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import java.io.Closeable
import java.io.File
import java.io.IOException
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.LongBuffer

/** The multilingual text model: token ids [1, L] -> last_hidden_state [1, L, hidden]. */
interface TextModel : Closeable {
    /** Returns last_hidden_state as L rows of hidden floats, row-major (length L * hidden). */
    fun lastHiddenState(ids: LongArray): FloatArray
}

/** [TextModel] on ONNX Runtime (CPU). One session, kept open; [lastHiddenState] may be called from any thread. */
class OrtTextModel(modelFile: File, threads: Int = 4) : TextModel {
    private val env: OrtEnvironment = OrtEnvironment.getEnvironment()
    private val session: OrtSession

    init {
        // No telemetry (the app is offline by design; the Android telemetry provider is also removed in the manifest).
        try {
            env.setTelemetry(false)
        } catch (e: Exception) {
            // not supported on this platform
        }
        val opts = OrtSession.SessionOptions()
        opts.setIntraOpNumThreads(threads)
        opts.setSessionLogLevel(ai.onnxruntime.OrtLoggingLevel.ORT_LOGGING_LEVEL_ERROR)
        session = try {
            env.createSession(modelFile.absolutePath, opts)
        } finally {
            opts.close()
        }
    }

    override fun lastHiddenState(ids: LongArray): FloatArray {
        val shape = longArrayOf(1, ids.size.toLong())
        val mask = LongArray(ids.size) { 1L }
        OnnxTensor.createTensor(env, LongBuffer.wrap(ids), shape).use { idT ->
            OnnxTensor.createTensor(env, LongBuffer.wrap(mask), shape).use { maskT ->
                session.run(mapOf("input_ids" to idT, "attention_mask" to maskT)).use { res ->
                    val out = res.get(0) as OnnxTensor
                    val fb = out.floatBuffer
                    val arr = FloatArray(fb.remaining())
                    fb.get(arr)
                    return arr
                }
            }
        }
    }

    override fun close() {
        session.close()
    }
}

/**
 * Steps 1-3 of API.md section 6: tokenize, run the model, mean over all tokens, v = W . mean, L2 normalize.
 * [dense] is W, [outDim] x [hidden], row-major.
 */
class QueryEncoder(
    private val tokenizer: WordPiece,
    private val model: TextModel,
    private val dense: FloatArray,
    val outDim: Int = OUT_DIM,
    val hidden: Int = HIDDEN,
) {
    init {
        require(dense.size == outDim * hidden) { "dense has ${dense.size} floats, expected ${outDim * hidden}" }
    }

    fun tokenize(text: String): LongArray = tokenizer.encode(text)

    fun embed(text: String): FloatArray = embedIds(tokenizer.encode(text))

    fun embedIds(ids: LongArray): FloatArray {
        val h = model.lastHiddenState(ids)
        val len = ids.size
        if (h.size != len * hidden) throw IOException("text model returned ${h.size} floats for $len tokens")
        // Mean in double: the order of the sum does not matter then.
        val meanD = DoubleArray(hidden)
        var off = 0
        for (t in 0 until len) {
            for (j in 0 until hidden) meanD[j] += h[off + j].toDouble()
            off += hidden
        }
        val mean = FloatArray(hidden) { (meanD[it] / len).toFloat() }
        val v = FloatArray(outDim)
        var norm = 0.0
        for (i in 0 until outDim) {
            var s = 0.0
            val row = i * hidden
            for (j in 0 until hidden) s += dense[row + j] * mean[j]
            v[i] = s.toFloat()
            norm += s * s
        }
        val n = maxOf(Math.sqrt(norm), 1e-9).toFloat()
        for (i in v.indices) v[i] /= n
        return v
    }

    companion object {
        const val OUT_DIM = 512
        const val HIDDEN = 768

        /** Reads the dense file: [outDim] x [hidden] float32 little-endian, row-major. */
        @Throws(IOException::class)
        fun loadDense(file: File, outDim: Int = OUT_DIM, hidden: Int = HIDDEN): FloatArray {
            val bytes = file.readBytes()
            if (bytes.size != outDim * hidden * 4) {
                throw IOException("dense file has ${bytes.size} bytes, expected ${outDim * hidden * 4}")
            }
            val out = FloatArray(outDim * hidden)
            ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN).asFloatBuffer().get(out)
            return out
        }
    }
}
