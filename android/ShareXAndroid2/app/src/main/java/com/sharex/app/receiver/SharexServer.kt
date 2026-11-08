package com.sharex.app.receiver

import android.content.Context
import android.os.Environment
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

/**
 * SHAREX Android receiver.
 *
 * v1 single-POST `/upload` (backward compat) + v2 chunked-session protocol
 * (`/session/start`, `/session/<id>/chunk/<i>`, `/session/<id>/finish`,
 * `/session/<id>/cancel`).
 *
 * Sessions are stored on disk under cache/sessions/<session_id>/.
 */
class SharexServer(
    private val context: Context,
    private val port: Int = 8080,
    private val pairingToken: String = "sharex-phase2-test",
    private val onFileReceived: (name: String, size: Long, sha256: String) -> Unit,
) {

    private var serverSocket: ServerSocket? = null
    private var acceptThread: Thread? = null

    @Volatile
    private var running = false

    private val downloadsDir: File by lazy {
        val base = context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)
            ?: File(context.filesDir, "downloads")
        File(base, "SHAREX").also { it.mkdirs() }
    }

    private val sessionsRoot: File by lazy {
        File(context.cacheDir, "sessions").also { it.mkdirs() }
    }

    private val sessions = ConcurrentHashMap<String, SessionState>()

    data class SessionState(
        val sessionId: String,
        val filename: String,
        val totalSize: Long,
        val chunkSize: Long,
        val totalChunks: Int,
        val expectedSha256: String,
        val createdAt: Long,
        var lastActivity: Long,
        val chunksReceived: MutableSet<Int> = ConcurrentHashMap.newKeySet(),
        var cancelled: Boolean = false,
    )

    fun startServer() {
        if (running) return
        running = true
        val ss = ServerSocket()
        ss.reuseAddress = true
        ss.bind(InetSocketAddress(port))
        serverSocket = ss
        Log.i("SHAREX-RECEIVER", "listening on port $port (protocol v2)")

        acceptThread = Thread {
            while (running) {
                try {
                    val client = ss.accept()
                    Thread { handleClient(client) }
                        .apply { isDaemon = true }
                        .start()
                } catch (e: Exception) {
                    if (running) Log.e("SHAREX-RECEIVER", "accept failed: ${e.message}")
                }
            }
        }.apply {
            name = "sharex-accept"
            isDaemon = true
            start()
        }

        Thread {
            while (running) {
                try {
                    Thread.sleep(5 * 60 * 1000L)
                    cleanupExpiredSessions()
                } catch (_: InterruptedException) {
                    break
                } catch (e: Exception) {
                    Log.e("SHAREX-RECEIVER", "cleanup error: ${e.message}")
                }
            }
        }.apply {
            name = "sharex-cleanup"
            isDaemon = true
            start()
        }
    }

    fun stopServer() {
        running = false
        try { serverSocket?.close() } catch (_: Exception) {}
        serverSocket = null
        try { acceptThread?.join(2000) } catch (_: Exception) {}
        acceptThread = null
    }

    private fun handleClient(socket: Socket) {
        socket.use { sock ->
            sock.soTimeout = 30000
            try {
                val input = sock.getInputStream().buffered()
                val output = sock.getOutputStream()

                val requestLine = readLine(input) ?: return
                val parts = requestLine.split(" ")
                if (parts.size < 3) {
                    writeError(output, 400, "bad request line")
                    return
                }
                val method = parts[0]
                val path = parts[1].substringBefore('?')

                val headers = HashMap<String, String>(16)
                while (true) {
                    val line = readLine(input) ?: break
                    if (line.isEmpty()) break
                    val colon = line.indexOf(':')
                    if (colon > 0) {
                        headers[line.substring(0, colon).trim().lowercase()] =
                            line.substring(colon + 1).trim()
                    }
                }

                val contentLength = headers["content-length"]?.toLongOrNull() ?: 0L

                when {
                    method == "GET" && path == "/health" -> {
                        val body = """{"status":"alive","service":"SHAREX Android Receiver","protocol":"v2"}"""
                        writeResponse(output, 200, "application/json", body.toByteArray())
                    }

                    method == "POST" && path == "/upload" -> {
                        handleLegacyUpload(input, output, headers, contentLength)
                    }

                    method == "POST" && path == "/session/start" -> {
                        handleSessionStart(input, output, headers, contentLength)
                    }

                    method == "POST" && path.startsWith("/session/") && path.endsWith("/finish") -> {
                        val sessionId = path.substringAfter("/session/").substringBefore("/finish")
                        handleSessionFinish(sessionId, input, output, headers, contentLength)
                    }

                    method == "POST" && path.startsWith("/session/") && path.contains("/chunk/") -> {
                        val sessionId = path.substringAfter("/session/").substringBefore("/chunk/")
                        val index = path.substringAfter("/chunk/").toIntOrNull() ?: -1
                        if (index < 0) {
                            writeError(output, 400, "bad chunk index")
                        } else {
                            handleSessionChunk(sessionId, index, input, output, headers, contentLength)
                        }
                    }

                    method == "POST" && path.startsWith("/session/") && path.endsWith("/cancel") -> {
                        val sessionId = path.substringAfter("/session/").substringBefore("/cancel")
                        handleSessionCancel(sessionId, output, headers)
                    }

                    else -> writeError(output, 404, "not found")
                }
            } catch (e: Exception) {
                Log.e("SHAREX-RECEIVER", "handler failed: ${e.message}")
            }
        }
    }

    private fun handleSessionStart(
        input: InputStream,
        output: OutputStream,
        headers: Map<String, String>,
        contentLength: Long,
    ) {
        if (headers["x-pairing-token"] != pairingToken) {
            writeError(output, 403, "Invalid pairing token")
            return
        }
        val body = readExactly(input, contentLength.toInt())
        val json = try {
            JSONObject(String(body, Charsets.UTF_8))
        } catch (_: Exception) {
            writeError(output, 400, "invalid JSON")
            return
        }

        val filename = json.optString("filename", "unknown.bin")
        val totalSize = json.optLong("total_size", 0)
        val expectedSha256 = json.optString("sha256", "")
        val chunkSize = json.optLong("chunk_size", 4L * 1024 * 1024)
        val resumeId = json.optString("resume_id", "")

        if (totalSize <= 0 || chunkSize <= 0) {
            writeError(output, 400, "invalid total_size or chunk_size")
            return
        }
        val totalChunks = ((totalSize + chunkSize - 1) / chunkSize).toInt()

        if (resumeId.isNotEmpty()) {
            val existing = sessions[resumeId]
            if (existing != null && !existing.cancelled && !isExpired(existing)) {
                existing.lastActivity = System.currentTimeMillis()
                val missing = (0 until existing.totalChunks)
                    .filter { it !in existing.chunksReceived }
                val received = existing.chunksReceived.toList().sorted()
                val resp = JSONObject().apply {
                    put("session_id", resumeId)
                    put("chunks_needed", JSONArray(missing))
                    put("chunks_received", JSONArray(received))
                    put("resume_token", resumeId)
                    put("expires_at", (existing.lastActivity + SESSION_TTL_MS) / 1000)
                }
                writeResponse(output, 200, "application/json", resp.toString().toByteArray())
                return
            }
        }

        val sessionId = UUID.randomUUID().toString().replace("-", "")
        val state = SessionState(
            sessionId = sessionId,
            filename = filename,
            totalSize = totalSize,
            chunkSize = chunkSize,
            totalChunks = totalChunks,
            expectedSha256 = expectedSha256,
            createdAt = System.currentTimeMillis(),
            lastActivity = System.currentTimeMillis(),
        )
        sessions[sessionId] = state
        val dir = File(sessionsRoot, sessionId).also { it.mkdirs() }
        writeMeta(dir, state)

        val resp = JSONObject().apply {
            put("session_id", sessionId)
            put("chunks_needed", JSONArray((0 until totalChunks).toList()))
            put("resume_token", sessionId)
            put("expires_at", (state.lastActivity + SESSION_TTL_MS) / 1000)
        }
        writeResponse(output, 200, "application/json", resp.toString().toByteArray())
    }

    private fun handleSessionChunk(
        sessionId: String,
        index: Int,
        input: InputStream,
        output: OutputStream,
        headers: Map<String, String>,
        contentLength: Long,
    ) {
        if (headers["x-pairing-token"] != pairingToken) {
            writeError(output, 403, "Invalid pairing token")
            return
        }
        val state = sessions[sessionId] ?: run {
            writeError(output, 404, "session not found")
            return
        }
        if (state.cancelled) {
            writeError(output, 409, "session cancelled")
            return
        }
        if (index !in 0 until state.totalChunks) {
            writeError(output, 400, "chunk index out of range")
            return
        }
        if (contentLength > state.chunkSize) {
            writeError(output, 400, "chunk too large")
            return
        }

        val dir = File(sessionsRoot, sessionId).also { it.mkdirs() }
        val chunkFile = File(dir, "chunk_%05d".format(index))

        try {
            FileOutputStream(chunkFile).use { out ->
                var left = contentLength
                val buf = ByteArray(65536)
                while (left > 0) {
                    val n = input.read(buf, 0, minOf(buf.size.toLong(), left).toInt())
                    if (n <= 0) break
                    out.write(buf, 0, n)
                    left -= n
                }
            }
        } catch (e: Exception) {
            writeError(output, 500, "chunk write failed: ${e.message}")
            return
        }

        state.chunksReceived.add(index)
        state.lastActivity = System.currentTimeMillis()

        val resp = JSONObject().apply {
            put("index", index)
            put("bytes_written", chunkFile.length())
            put("chunks_received", state.chunksReceived.size)
            put("chunks_total", state.totalChunks)
        }
        writeResponse(output, 200, "application/json", resp.toString().toByteArray())
    }

    private fun handleSessionFinish(
        sessionId: String,
        input: InputStream,
        output: OutputStream,
        headers: Map<String, String>,
        contentLength: Long,
    ) {
        if (headers["x-pairing-token"] != pairingToken) {
            writeError(output, 403, "Invalid pairing token")
            return
        }
        val state = sessions[sessionId] ?: run {
            writeError(output, 404, "session not found")
            return
        }
        if (state.cancelled) {
            writeError(output, 409, "session cancelled")
            return
        }
        val allChunks = (0 until state.totalChunks).all { it in state.chunksReceived }
        if (!allChunks) {
            writeError(output, 400, "missing chunks")
            return
        }

        val body = readExactly(input, contentLength.toInt())
        val clientSha = try {
            JSONObject(String(body, Charsets.UTF_8)).optString("sha256", "")
        } catch (_: Exception) { "" }

        val dir = File(sessionsRoot, sessionId)
        val safeName = sanitize(state.filename)
        val finalName = "${UUID.randomUUID().toString().replace("-", "")}_$safeName"
        val finalFile = File(downloadsDir, finalName)

        val sha256 = MessageDigest.getInstance("SHA-256")
        var total = 0L
        try {
            FileOutputStream(finalFile).use { out ->
                for (i in 0 until state.totalChunks) {
                    val chunk = File(dir, "chunk_%05d".format(i))
                    if (!chunk.exists()) {
                        throw IllegalStateException("missing on-disk chunk $i")
                    }
                    chunk.inputStream().use { inp ->
                        val buf = ByteArray(65536)
                        while (true) {
                            val n = inp.read(buf)
                            if (n <= 0) break
                            sha256.update(buf, 0, n)
                            out.write(buf, 0, n)
                            total += n
                        }
                    }
                }
            }
        } catch (e: Exception) {
            finalFile.delete()
            writeError(output, 500, "assembly failed: ${e.message}")
            return
        }

        val hashHex = sha256.digest().joinToString("") { "%02x".format(it) }
        val expected = state.expectedSha256.ifEmpty { clientSha }
        if (expected.isNotEmpty() && !expected.equals(hashHex, ignoreCase = true)) {
            finalFile.delete()
            dir.deleteRecursively()
            sessions.remove(sessionId)
            writeResponse(
                output, 422, "application/json",
                """{"status":"error","detail":"hash mismatch","expected":"$expected","actual":"$hashHex"}""".toByteArray()
            )
            return
        }

        onFileReceived(state.filename, total, hashHex)

        try {
            val signalFile = File(downloadsDir.parentFile, "last_received.json")
            signalFile.writeText(
                """{"filename":"${escape(state.filename)}","stored_filename":"${escape(finalName)}","size_bytes":$total,"sha256":"$hashHex","received_at":${System.currentTimeMillis() / 1000}}""",
                Charsets.UTF_8
            )
        } catch (_: Exception) {}

        dir.deleteRecursively()
        sessions.remove(sessionId)

        val resp = JSONObject().apply {
            put("status", "success")
            put("stored_filename", finalName)
            put("size_bytes", total)
            put("sha256", hashHex)
        }
        writeResponse(output, 200, "application/json", resp.toString().toByteArray())
    }

    private fun handleSessionCancel(
        sessionId: String,
        output: OutputStream,
        headers: Map<String, String>,
    ) {
        if (headers["x-pairing-token"] != pairingToken) {
            writeError(output, 403, "Invalid pairing token")
            return
        }
        val state = sessions[sessionId]
        if (state != null) {
            state.cancelled = true
            File(sessionsRoot, sessionId).deleteRecursively()
            sessions.remove(sessionId)
        }
        writeResponse(
            output, 200, "application/json",
            """{"status":"cancelled"}""".toByteArray()
        )
    }

    private fun handleLegacyUpload(
        input: InputStream,
        output: OutputStream,
        headers: Map<String, String>,
        contentLength: Long,
    ) {
        if (headers["x-pairing-token"] != pairingToken) {
            writeError(output, 403, "Invalid pairing token")
            return
        }
        val contentType = headers["content-type"] ?: run {
            writeError(output, 400, "missing content-type")
            return
        }
        val boundary = extractBoundary(contentType) ?: run {
            writeError(output, 400, "missing boundary")
            return
        }
        val originalName = headers["x-filename"] ?: "unknown.bin"
        val safeName = sanitize(originalName)
        val finalName = "${UUID.randomUUID().toString().replace("-", "")}_$safeName"
        val finalFile = File(downloadsDir, finalName)

        val sha256 = MessageDigest.getInstance("SHA-256")
        var total = 0L
        val ok: Boolean
        try {
            FileOutputStream(finalFile).use { out ->
                ok = streamMultipartFileBody(
                    input = input,
                    boundary = boundary,
                    output = out,
                    sha256 = sha256,
                    onBytes = { n -> total += n },
                )
            }
        } catch (e: Exception) {
            finalFile.delete()
            writeError(output, 500, "stream failed: ${e.message}")
            return
        }
        if (!ok) {
            finalFile.delete()
            writeError(output, 400, "multipart parse failed")
            return
        }
        val hashHex = sha256.digest().joinToString("") { "%02x".format(it) }
        onFileReceived(originalName, total, hashHex)

        val body = """{"status":"success","original_filename":"${escape(originalName)}","stored_filename":"${escape(finalName)}","stored_path":"${escape(finalFile.absolutePath)}","size_bytes":$total,"sha256":"$hashHex"}"""
        writeResponse(output, 200, "application/json", body.toByteArray())
    }

    private fun readLine(input: InputStream): String? {
        val buf = ByteArrayOutputStream(256)
        while (true) {
            val b = input.read()
            if (b < 0) return if (buf.size() == 0) null else buf.toString("ISO-8859-1")
            if (b == 10) {
                val s = buf.toString("ISO-8859-1")
                return if (s.endsWith("\r")) s.dropLast(1) else s
            }
            buf.write(b)
        }
    }

    private fun readExactly(input: InputStream, n: Int): ByteArray {
        val buf = ByteArray(n)
        var read = 0
        while (read < n) {
            val r = input.read(buf, read, n - read)
            if (r <= 0) break
            read += r
        }
        return buf
    }

    private fun writeResponse(out: OutputStream, status: Int, contentType: String, body: ByteArray) {
        val reason = when (status) {
            200 -> "OK"; 400 -> "Bad Request"; 403 -> "Forbidden"
            404 -> "Not Found"; 409 -> "Conflict"; 422 -> "Unprocessable Entity"
            500 -> "Internal Server Error"; else -> "Unknown"
        }
        val header =
            "HTTP/1.1 $status $reason\r\n" +
            "Content-Type: $contentType\r\n" +
            "Content-Length: ${body.size}\r\n" +
            "Connection: close\r\n" +
            "\r\n"
        out.write(header.toByteArray(Charsets.ISO_8859_1))
        out.write(body)
        out.flush()
    }

    private fun writeError(out: OutputStream, status: Int, detail: String) {
        val body = """{"detail":"${escape(detail)}"}""".toByteArray()
        writeResponse(out, status, "application/json", body)
    }

    private fun writeMeta(dir: File, s: SessionState) {
        try {
            File(dir, "meta.json").writeText(
                """{"session_id":"${s.sessionId}","filename":"${escape(s.filename)}","total_size":${s.totalSize},"chunk_size":${s.chunkSize},"total_chunks":${s.totalChunks},"sha256":"${s.expectedSha256}","created_at":${s.createdAt / 1000}}""",
                Charsets.UTF_8
            )
        } catch (_: Exception) {}
    }

    private fun cleanupExpiredSessions() {
        val now = System.currentTimeMillis()
        val stale = sessions.values.filter { isExpired(it, now) }
        for (s in stale) {
            sessions.remove(s.sessionId)
            File(sessionsRoot, s.sessionId).deleteRecursively()
        }
        sessionsRoot.listFiles()?.forEach { dir ->
            if (!sessions.containsKey(dir.name)) {
                val age = now - dir.lastModified()
                if (age > SESSION_TTL_MS) dir.deleteRecursively()
            }
        }
    }

    private fun isExpired(s: SessionState, now: Long = System.currentTimeMillis()): Boolean =
        (now - s.lastActivity) > SESSION_TTL_MS

    private fun extractBoundary(contentType: String): String? {
        val idx = contentType.indexOf("boundary=", ignoreCase = true)
        if (idx < 0) return null
        var b = contentType.substring(idx + "boundary=".length).trim()
        if (b.startsWith("\"")) b = b.trim('"')
        val semi = b.indexOf(';')
        if (semi >= 0) b = b.substring(0, semi).trim()
        return b.ifEmpty { null }
    }

    private fun sanitize(name: String): String {
        val base = File(name).name
        return base.replace(Regex("[\\\\/:*?\"<>|]"), "_").take(120)
    }

    private fun escape(s: String): String =
        s.replace("\\", "\\\\").replace("\"", "\\\"")

    private fun streamMultipartFileBody(
        input: InputStream,
        boundary: String,
        output: OutputStream,
        sha256: MessageDigest,
        onBytes: (Int) -> Unit,
    ): Boolean {
        val boundaryBytes = "--$boundary".toByteArray(Charsets.ISO_8859_1)
        val marker = ByteArray(2 + boundaryBytes.size)
        marker[0] = 13; marker[1] = 10
        System.arraycopy(boundaryBytes, 0, marker, 2, boundaryBytes.size)

        val phaseBuf = ByteArray(16384)
        var phaseFilled = 0
        var foundBoundary = false
        while (!foundBoundary) {
            val idx = indexOf(phaseBuf, phaseFilled, boundaryBytes, 0)
            if (idx >= 0) {
                foundBoundary = true
                var after = idx + boundaryBytes.size
                var foundLf = false
                while (after < phaseFilled) {
                    if (phaseBuf[after] == 10.toByte()) { after++; foundLf = true; break }
                    after++
                }
                if (!foundLf) {
                    while (true) {
                        val n = input.read()
                        if (n < 0) return false
                        if (n == 10) break
                    }
                }
                val leftoverStart = if (foundLf) after else phaseFilled
                val leftoverLen = phaseFilled - leftoverStart
                if (leftoverLen > 0) System.arraycopy(phaseBuf, leftoverStart, phaseBuf, 0, leftoverLen)
                phaseFilled = leftoverLen
                break
            }
            if (phaseFilled >= phaseBuf.size) return false
            val n = input.read(phaseBuf, phaseFilled, phaseBuf.size - phaseFilled)
            if (n <= 0) return false
            phaseFilled += n
        }

        val doubleCrlf = byteArrayOf(13, 10, 13, 10)
        var bodyAlreadyInBuf = ByteArray(0)
        while (true) {
            val idx = indexOf(phaseBuf, phaseFilled, doubleCrlf, 0)
            if (idx >= 0) {
                val bodyStart = idx + 4
                if (bodyStart < phaseFilled) bodyAlreadyInBuf = phaseBuf.copyOfRange(bodyStart, phaseFilled)
                break
            }
            if (phaseFilled >= phaseBuf.size) return false
            val n = input.read(phaseBuf, phaseFilled, phaseBuf.size - phaseFilled)
            if (n <= 0) return false
            phaseFilled += n
        }

        val win = 16384
        val buf = ByteArray(win)
        var filled = 0
        if (bodyAlreadyInBuf.isNotEmpty()) {
            val n = minOf(bodyAlreadyInBuf.size, win)
            System.arraycopy(bodyAlreadyInBuf, 0, buf, 0, n)
            filled = n
            if (bodyAlreadyInBuf.size > n) {
                val extra = bodyAlreadyInBuf.copyOfRange(n, bodyAlreadyInBuf.size)
                output.write(extra); sha256.update(extra); onBytes(extra.size)
            }
        }
        while (true) {
            val idx = indexOf(buf, filled, marker, 0)
            if (idx >= 0) {
                if (idx > 0) { output.write(buf, 0, idx); sha256.update(buf, 0, idx); onBytes(idx) }
                val after = idx + marker.size
                val tail = ByteArray(2); var tailFilled = 0
                if (after < filled) {
                    val take = minOf(filled - after, 2)
                    System.arraycopy(buf, after, tail, 0, take)
                    tailFilled = take
                }
                while (tailFilled < 2) {
                    val n = input.read(tail, tailFilled, 2 - tailFilled)
                    if (n <= 0) return true
                    tailFilled += n
                }
                val isClosing = (tail[0] == 45.toByte() && tail[1] == 45.toByte())
                if (isClosing) { try { input.read(ByteArray(2)) } catch (_: Exception) {} }
                return true
            }
            val keep = marker.size - 1
            if (filled >= win) {
                val flush = filled - keep
                if (flush > 0) {
                    output.write(buf, 0, flush); sha256.update(buf, 0, flush); onBytes(flush)
                    System.arraycopy(buf, flush, buf, 0, keep); filled = keep
                }
            }
            val n = input.read(buf, filled, win - filled)
            if (n <= 0) return false
            filled += n
        }
    }

    private fun indexOf(hay: ByteArray, hayLen: Int, needle: ByteArray, from: Int): Int {
        if (needle.isEmpty()) return from
        val limit = hayLen - needle.size
        var i = from
        outer@ while (i <= limit) {
            var j = 0
            while (j < needle.size) {
                if (hay[i + j] != needle[j]) { i++; continue@outer }
                j++
            }
            return i
        }
        return -1
    }

    companion object {
        private const val SESSION_TTL_MS = 60 * 60 * 1000L
    }
}