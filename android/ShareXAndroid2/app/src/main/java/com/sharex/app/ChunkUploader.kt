package com.sharex.app

import android.content.ContentResolver
import android.net.Uri
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.InputStream
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * SHAREX v2 chunk protocol client (Android sender side).
 *
 * Mirrors shared/protocol.md:
 *   POST /session/start               -> session_id, chunks_needed
 *   POST /session/<id>/chunk/<idx>    -> raw 4 MiB body
 *   POST /session/<id>/finish         -> assembled file
 *   POST /session/<id>/cancel         -> cleanup on abort
 *
 * Pause / cancel are driven by AtomicBoolean flags. Checks happen between chunks.
 * Because a 4 MiB chunk at hotspot speed takes ~0.5-1 s, pause/cancel latency is
 * up to one chunk (acceptable; matches the desktop sender behavior).
 */
class ChunkUploader(
    private val resolver: ContentResolver,
    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .writeTimeout(5, TimeUnit.MINUTES)
        .readTimeout(2, TimeUnit.MINUTES)
        .build(),
) {

    companion object {
        const val CHUNK_SIZE = 4 * 1024 * 1024  // 4 MiB
        private const val CHUNK_RETRY_ATTEMPTS = 3
        private const val CHUNK_RETRY_BASE_DELAY_MS = 1000L
    }

    data class UploadResult(
        val sessionId: String,
        val sha256: String,
        val sizeBytes: Long,
        val storedFilename: String,
    )

    interface Callbacks {
        fun onHashing() {}
        fun onSessionStarted(sessionId: String, totalChunks: Int, neededChunks: Int) {}
        fun onChunkDone(index: Int, done: Int, total: Int) {}
    }

    class UploadPaused : Exception("paused")
    class UploadCancelled : Exception("cancelled")

    /**
     * Upload `uri` to `serverIp:8000` using the v2 chunk protocol.
     *
     * @param resumeId  session_id from a previous paused transfer (null for fresh)
     * @param cancel    if set true mid-transfer, throws UploadCancelled
     * @param pause     if set true mid-transfer, throws UploadPaused
     *
     * Throws UploadCancelled, UploadPaused, or RuntimeException on failure.
     */
    fun uploadFile(
        uri: Uri,
        filename: String,
        totalSize: Long,
        serverIp: String,
        token: String,
        resumeId: String? = null,
        cancel: AtomicBoolean,
        pause: AtomicBoolean,
        callbacks: Callbacks = object : Callbacks {},
    ): UploadResult {
        checkFlags(cancel, pause)

        // --- Step 1: hash the whole file ---
        callbacks.onHashing()
        val sha256 = computeSha256(uri, cancel, pause)
        checkFlags(cancel, pause)

        // --- Step 2: start (or resume) session ---
        val startResp = postSessionStart(
            serverIp = serverIp,
            token = token,
            filename = filename,
            totalSize = totalSize,
            sha256 = sha256,
            resumeId = resumeId,
        )
        val sessionId = startResp.getString("session_id")
        val totalChunks = ((totalSize + CHUNK_SIZE - 1) / CHUNK_SIZE).toInt()
        val neededArr = startResp.optJSONArray("chunks_needed")
        val chunksNeeded = mutableListOf<Int>()
        if (neededArr != null) {
            for (i in 0 until neededArr.length()) chunksNeeded.add(neededArr.getInt(i))
        }
        if (chunksNeeded.isEmpty()) {
            // Server reported nothing missing — everything is already there; jump to finish.
            for (i in 0 until totalChunks) chunksNeeded.add(i)
        }

        callbacks.onSessionStarted(sessionId, totalChunks, chunksNeeded.size)

        // --- Step 3: upload each needed chunk ---
        val input = resolver.openInputStream(uri)
            ?: throw RuntimeException("Cannot open file")
        var chunksDone = 0
        input.use { stream ->
            for (idx in chunksNeeded) {
                checkFlags(cancel, pause)

                val offset = idx.toLong() * CHUNK_SIZE
                val remaining = (totalSize - offset).coerceAtMost(CHUNK_SIZE.toLong()).toInt()
                if (remaining <= 0) throw RuntimeException("file truncated at chunk $idx")

                // Seek by skipping (InputStream may not seek, so we read/discard)
                // For sequential-in-order this is fine; for out-of-order we'd
                // need RandomAccessFile — but resume also starts fresh streams.
                // To be safe, we open a new stream per chunk.
                val chunkBytes = readChunk(uri, offset, remaining)

                postChunkWithRetry(
                    serverIp = serverIp,
                    token = token,
                    sessionId = sessionId,
                    index = idx,
                    chunkBytes = chunkBytes,
                    cancel = cancel,
                    pause = pause,
                )

                chunksDone += 1
                callbacks.onChunkDone(idx, chunksDone, chunksNeeded.size)
            }
        }

        // --- Step 4: finish ---
        checkFlags(cancel, pause)
        val finishResp = postSessionFinish(
            serverIp = serverIp,
            token = token,
            sessionId = sessionId,
            sha256 = sha256,
        )
        return UploadResult(
            sessionId = sessionId,
            sha256 = finishResp.optString("sha256", sha256),
            sizeBytes = finishResp.optLong("size_bytes", totalSize),
            storedFilename = finishResp.optString("stored_filename", filename),
        )
    }

    fun cancelSession(serverIp: String, token: String, sessionId: String) {
        try {
            val req = Request.Builder()
                .url("http://$serverIp:8000/session/$sessionId/cancel")
                .addHeader("X-Pairing-Token", token)
                .post(ByteArray(0).toRequestBody("application/json".toMediaType()))
                .build()
            client.newCall(req).execute().close()
        } catch (_: Exception) {
        }
    }

    // ---------------------------------------------------------------------
    // Internals
    // ---------------------------------------------------------------------

    private fun checkFlags(cancel: AtomicBoolean, pause: AtomicBoolean) {
        if (cancel.get()) throw UploadCancelled()
        if (pause.get()) throw UploadPaused()
    }

    private fun computeSha256(uri: Uri, cancel: AtomicBoolean, pause: AtomicBoolean): String {
        val md = MessageDigest.getInstance("SHA-256")
        resolver.openInputStream(uri)?.use { input ->
            val buf = ByteArray(1 shl 20)  // 1 MiB
            while (true) {
                checkFlags(cancel, pause)
                val n = input.read(buf)
                if (n <= 0) break
                md.update(buf, 0, n)
            }
        } ?: throw RuntimeException("Cannot open file for hashing")
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    private fun readChunk(uri: Uri, offset: Long, length: Int): ByteArray {
        val buf = ByteArray(length)
        resolver.openInputStream(uri)?.use { input ->
            var skipped = 0L
            while (skipped < offset) {
                val s = input.skip(offset - skipped)
                if (s <= 0) throw RuntimeException("cannot seek to offset $offset")
                skipped += s
            }
            var read = 0
            while (read < length) {
                val n = input.read(buf, read, length - read)
                if (n <= 0) break
                read += n
            }
            if (read != length) throw RuntimeException("short read at offset $offset")
        } ?: throw RuntimeException("Cannot open file")
        return buf
    }

    private fun postSessionStart(
        serverIp: String, token: String, filename: String,
        totalSize: Long, sha256: String, resumeId: String?,
    ): JSONObject {
        val body = JSONObject().apply {
            put("filename", filename)
            put("total_size", totalSize)
            put("sha256", sha256)
            put("chunk_size", CHUNK_SIZE)
            if (resumeId != null) put("resume_id", resumeId) else put("resume_id", JSONObject.NULL)
        }
        val req = Request.Builder()
            .url("http://$serverIp:8000/session/start")
            .addHeader("X-Pairing-Token", token)
            .post(body.toString().toRequestBody("application/json".toMediaType()))
            .build()
        client.newCall(req).execute().use { resp ->
            val text = resp.body?.string() ?: ""
            if (!resp.isSuccessful) {
                throw RuntimeException("/session/start failed: HTTP ${resp.code}: ${text.take(200)}")
            }
            return JSONObject(text)
        }
    }

    private fun postChunkWithRetry(
        serverIp: String, token: String, sessionId: String,
        index: Int, chunkBytes: ByteArray,
        cancel: AtomicBoolean, pause: AtomicBoolean,
    ) {
        var lastErr: String? = null
        for (attempt in 1..CHUNK_RETRY_ATTEMPTS) {
            checkFlags(cancel, pause)
            try {
                val req = Request.Builder()
                    .url("http://$serverIp:8000/session/$sessionId/chunk/$index")
                    .addHeader("X-Pairing-Token", token)
                    .post(chunkBytes.toRequestBody("application/octet-stream".toMediaType()))
                    .build()
                client.newCall(req).execute().use { resp ->
                    if (resp.isSuccessful) return
                    val text = resp.body?.string() ?: ""
                    if (resp.code in 400..499) {
                        // Non-retryable
                        throw RuntimeException("chunk $index: HTTP ${resp.code}: ${text.take(200)}")
                    }
                    lastErr = "HTTP ${resp.code}: ${text.take(200)}"
                }
            } catch (e: RuntimeException) {
                throw e
            } catch (e: Exception) {
                lastErr = "${e.javaClass.simpleName}: ${e.message}"
            }
            if (attempt < CHUNK_RETRY_ATTEMPTS) {
                val delayMs = CHUNK_RETRY_BASE_DELAY_MS * (1L shl (attempt - 1))
                val stepMs = 100L
                var slept = 0L
                while (slept < delayMs) {
                    checkFlags(cancel, pause)
                    Thread.sleep(stepMs)
                    slept += stepMs
                }
            }
        }
        throw RuntimeException("chunk $index failed after $CHUNK_RETRY_ATTEMPTS attempts: $lastErr")
    }

    private fun postSessionFinish(
        serverIp: String, token: String, sessionId: String, sha256: String,
    ): JSONObject {
        val body = JSONObject().apply { put("sha256", sha256) }
        val req = Request.Builder()
            .url("http://$serverIp:8000/session/$sessionId/finish")
            .addHeader("X-Pairing-Token", token)
            .post(body.toString().toRequestBody("application/json".toMediaType()))
            .build()
        client.newCall(req).execute().use { resp ->
            val text = resp.body?.string() ?: ""
            if (resp.code == 422) {
                throw RuntimeException("hash mismatch on receiver: ${text.take(300)}")
            }
            if (!resp.isSuccessful) {
                throw RuntimeException("/session/finish failed: HTTP ${resp.code}: ${text.take(200)}")
            }
            return JSONObject(text)
        }
    }
}