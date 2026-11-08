package com.sharex.app.receiver

import android.content.Context
import android.os.Environment
import android.util.Log
import java.io.BufferedInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Minimal HTTP/1.1 server for receiving SHAREX uploads.
 * Uses raw ServerSocket instead of NanoHTTPD to avoid its internal
 * buffering limits on large multipart bodies over high-latency networks.
 *
 * Endpoints:
 *   GET  /health  → {"status":"alive","service":"SHAREX Android Receiver"}
 *   POST /upload  → multipart/form-data with token header, returns JSON summary
 */
class SimpleHttpServer(
    private val context: Context,
    private val port: Int = 8080,
    private val pairingToken: String = "sharex-phase2-test",
    private val onFileReceived: (name: String, size: Long, sha256: String) -> Unit,
) {
    private val downloadsDir: File by lazy {
        val base = context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)
            ?: File(context.filesDir, "downloads")
        File(base, "SHAREX").also { it.mkdirs() }
    }

    private val running = AtomicBoolean(false)
    private var serverSocket: ServerSocket? = null
    private var acceptThread: Thread? = null

    fun startServer() {
        if (!running.compareAndSet(false, true)) return
        val ss = ServerSocket()
        ss.reuseAddress = true
        ss.bind(InetSocketAddress(port))
        serverSocket = ss
        Log.i("SHAREX-RECEIVER", "listening on port $port")

        acceptThread = Thread {
            while (running.get()) {
                try {
                    val client = ss.accept()
                    Thread { handleClient(client) }
                        .apply { isDaemon = true }
                        .start()
                } catch (e: Exception) {
                    if (running.get()) {
                        Log.e("SHAREX-RECEIVER", "accept failed: ${e.message}")
                    }
                }
            }
        }.apply {
            name = "sharex-accept"
            isDaemon = true
            start()
        }
    }

    fun stopServer() {
        running.set(false)
        try { serverSocket?.close() } catch (_: Exception) {}
        serverSocket = null
        try { acceptThread?.join(2000) } catch (_: Exception) {}
        acceptThread = null
    }

    // ---------------------------------------------------------------------------
    // Per-client handler
    // ---------------------------------------------------------------------------

    private fun handleClient(socket: Socket) {
        socket.use { sock ->
            sock.soTimeout = 30000  // 30 s idle timeout for reads
            try {
                val input = BufferedInputStream(sock.getInputStream())
                val output = sock.getOutputStream()

                // 1. Read request line: "METHOD /path HTTP/1.1\r\n"
                val requestLine = readLine(input) ?: return
                val parts = requestLine.split(" ")
                if (parts.size < 3) {
                    writeError(output, 400, "bad request line")
                    return
                }
                val method = parts[0]
                val path = parts[1].substringBefore('?')

                // 2. Read headers until empty line
                val headers = HashMap<String, String>(16)
                while (true) {
                    val line = readLine(input) ?: break
                    if (line.isEmpty()) break
                    val colon = line.indexOf(':')
                    if (colon > 0) {
                        val name = line.substring(0, colon).trim().lowercase()
                        val value = line.substring(colon + 1).trim()
                        headers[name] = value
                    }
                }

                when {
                    method == "GET" && path == "/health" -> {
                        val body = """{"status":"alive","service":"SHAREX Android Receiver"}"""
                        writeResponse(output, 200, "application/json", body.toByteArray())
                    }
                    method == "POST" && path == "/upload" -> {
                        handleUpload(input, output, headers)
                    }
                    else -> writeError(output, 404, "not found")
                }
            } catch (e: SocketTimeoutException) {
                Log.w("SHAREX-RECEIVER", "client timeout: ${e.message}")
            } catch (e: Exception) {
                Log.e("SHAREX-RECEIVER", "handler failed: ${e.message}")
            }
        }
    }

    // ---------------------------------------------------------------------------
    // Upload handling
    // ---------------------------------------------------------------------------

    private fun handleUpload(
        input: InputStream,
        output: OutputStream,
        headers: Map<String, String>,
    ) {
        val token = headers["x-pairing-token"]
        if (token != pairingToken) {
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

    // ---------------------------------------------------------------------------
    // Multipart streaming
    // ---------------------------------------------------------------------------

    private fun streamMultipartFileBody(
        input: InputStream,
        boundary: String,
        output: OutputStream,
        sha256: MessageDigest,
        onBytes: (Int) -> Unit,
    ): Boolean {
        val boundaryBytes = "--$boundary".toByteArray(Charsets.ISO_8859_1)
        val marker = ByteArray(2 + boundaryBytes.size)
        marker[0] = 13
        marker[1] = 10
        System.arraycopy(boundaryBytes, 0, marker, 2, boundaryBytes.size)

        // Phase 1: skip preamble until first boundary
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
                    if (phaseBuf[after] == 10.toByte()) {
                        after++
                        foundLf = true
                        break
                    }
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
                if (leftoverLen > 0) {
                    System.arraycopy(phaseBuf, leftoverStart, phaseBuf, 0, leftoverLen)
                }
                phaseFilled = leftoverLen
                break
            }
            if (phaseFilled >= phaseBuf.size) return false
            val n = input.read(phaseBuf, phaseFilled, phaseBuf.size - phaseFilled)
            if (n <= 0) return false
            phaseFilled += n
        }

        // Phase 2: read part headers until \r\n\r\n
        val doubleCrlf = byteArrayOf(13, 10, 13, 10)
        var bodyAlreadyInBuf = ByteArray(0)
        while (true) {
            val idx = indexOf(phaseBuf, phaseFilled, doubleCrlf, 0)
            if (idx >= 0) {
                val bodyStart = idx + 4
                if (bodyStart < phaseFilled) {
                    bodyAlreadyInBuf = phaseBuf.copyOfRange(bodyStart, phaseFilled)
                }
                break
            }
            if (phaseFilled >= phaseBuf.size) return false
            val n = input.read(phaseBuf, phaseFilled, phaseBuf.size - phaseFilled)
            if (n <= 0) return false
            phaseFilled += n
        }

        // Phase 3: stream file content until closing boundary
        val win = 16384
        val buf = ByteArray(win)
        var filled = 0
        if (bodyAlreadyInBuf.isNotEmpty()) {
            val n = minOf(bodyAlreadyInBuf.size, win)
            System.arraycopy(bodyAlreadyInBuf, 0, buf, 0, n)
            filled = n
            if (bodyAlreadyInBuf.size > n) {
                val extra = bodyAlreadyInBuf.copyOfRange(n, bodyAlreadyInBuf.size)
                output.write(extra)
                sha256.update(extra)
                onBytes(extra.size)
            }
        }

        while (true) {
            val idx = indexOf(buf, filled, marker, 0)
            if (idx >= 0) {
                if (idx > 0) {
                    output.write(buf, 0, idx)
                    sha256.update(buf, 0, idx)
                    onBytes(idx)
                }
                val after = idx + marker.size
                val tail = ByteArray(2)
                var tailFilled = 0
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
                if (isClosing) {
                    try { input.read(ByteArray(2)) } catch (_: Exception) {}
                }
                return true
            }

            val keep = marker.size - 1
            if (filled >= win) {
                val flush = filled - keep
                if (flush > 0) {
                    output.write(buf, 0, flush)
                    sha256.update(buf, 0, flush)
                    onBytes(flush)
                    System.arraycopy(buf, flush, buf, 0, keep)
                    filled = keep
                }
            }
            val n = input.read(buf, filled, win - filled)
            if (n <= 0) return false
            filled += n
        }
    }

    // ---------------------------------------------------------------------------
    // HTTP utilities
    // ---------------------------------------------------------------------------

    /** Reads a CRLF-terminated ASCII line from the stream. Returns null on EOF. */
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

    private fun writeResponse(output: OutputStream, status: Int, contentType: String, body: ByteArray) {
        val reason = when (status) {
            200 -> "OK"
            400 -> "Bad Request"
            403 -> "Forbidden"
            404 -> "Not Found"
            500 -> "Internal Server Error"
            else -> "Unknown"
        }
        val header =
            "HTTP/1.1 $status $reason\r\n" +
            "Content-Type: $contentType\r\n" +
            "Content-Length: ${body.size}\r\n" +
            "Connection: close\r\n" +
            "\r\n"
        output.write(header.toByteArray(Charsets.ISO_8859_1))
        output.write(body)
        output.flush()
    }

    private fun writeError(output: OutputStream, status: Int, detail: String) {
        val body = """{"detail":"${escape(detail)}"}""".toByteArray()
        writeResponse(output, status, "application/json", body)
    }

    private fun extractBoundary(contentType: String): String? {
        val idx = contentType.indexOf("boundary=", ignoreCase = true)
        if (idx < 0) return null
        var b = contentType.substring(idx + "boundary=".length).trim()
        if (b.startsWith("\"")) b = b.trim('"')
        val semi = b.indexOf(';')
        if (semi >= 0) b = b.substring(0, semi).trim()
        return b.ifEmpty { null }
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

    private fun sanitize(name: String): String {
        val base = File(name).name
        return base.replace(Regex("[\\\\/:*?\"<>|]"), "_").take(120)
    }

    private fun escape(s: String): String =
        s.replace("\\", "\\\\").replace("\"", "\\\"")
}