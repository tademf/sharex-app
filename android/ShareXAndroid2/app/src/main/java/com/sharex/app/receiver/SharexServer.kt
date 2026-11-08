package com.sharex.app.receiver

import android.content.Context
import android.os.Environment
import fi.iki.elonen.NanoHTTPD
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.security.MessageDigest
import java.util.UUID

class SharexServer(
    private val context: Context,
    port: Int = 8080,
    private val pairingToken: String = "sharex-phase2-test",
    private val onFileReceived: (name: String, size: Long, sha256: String) -> Unit,
) : NanoHTTPD(port) {

    private val downloadsDir: File by lazy {
        val base = context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)
            ?: File(context.filesDir, "downloads")
        File(base, "SHAREX").also { it.mkdirs() }
    }

    override fun serve(session: IHTTPSession): Response {
        return when (session.uri) {
            "/health" -> newFixedLengthResponse(
                Response.Status.OK, "application/json",
                """{"status":"alive","service":"SHAREX Android Receiver"}"""
            )
            "/upload" -> handleUpload(session)
            else -> newFixedLengthResponse(
                Response.Status.NOT_FOUND, "text/plain", "not found"
            )
        }
    }

    private fun handleUpload(session: IHTTPSession): Response {
        val token = session.headers["x-pairing-token"]
        if (token != pairingToken) {
            return newFixedLengthResponse(
                Response.Status.FORBIDDEN, "application/json",
                """{"detail":"Invalid pairing token"}"""
            )
        }

        if (session.method != Method.POST) {
            return newFixedLengthResponse(
                Response.Status.METHOD_NOT_ALLOWED, "text/plain", "POST required"
            )
        }

        val contentType = session.headers["content-type"]
            ?: return newFixedLengthResponse(
                Response.Status.BAD_REQUEST, "text/plain", "missing content-type"
            )
        val boundary = extractBoundary(contentType)
            ?: return newFixedLengthResponse(
                Response.Status.BAD_REQUEST, "text/plain", "missing boundary"
            )

        val originalName = session.headers["x-filename"] ?: "unknown.bin"
        val safeName = sanitize(originalName)
        val finalName = "${UUID.randomUUID().toString().replace("-", "")}_$safeName"
        val finalFile = File(downloadsDir, finalName)

        val sha256 = MessageDigest.getInstance("SHA-256")
        var total = 0L
        val ok: Boolean
        try {
            FileOutputStream(finalFile).use { out ->
                ok = streamMultipartFileBody(
                    input = session.inputStream,
                    boundary = boundary,
                    output = out,
                    sha256 = sha256,
                    onBytes = { n -> total += n },
                )
            }
        } catch (e: Exception) {
            finalFile.delete()
            return newFixedLengthResponse(
                Response.Status.INTERNAL_ERROR, "application/json",
                """{"detail":"stream failed: ${escape(e.message ?: "unknown")}"}"""
            )
        }

        if (!ok) {
            finalFile.delete()
            return newFixedLengthResponse(
                Response.Status.BAD_REQUEST, "text/plain", "multipart parse failed"
            )
        }

        val hashHex = sha256.digest().joinToString("") { "%02x".format(it) }

        onFileReceived(originalName, total, hashHex)

        val body = """{"status":"success","original_filename":"${escape(originalName)}","stored_filename":"${escape(finalName)}","stored_path":"${escape(finalFile.absolutePath)}","size_bytes":$total,"sha256":"$hashHex"}"""

        return newFixedLengthResponse(
            Response.Status.OK, "application/json", body
        )
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

    /**
     * Streams a multipart body from the raw InputStream.
     * Locates the FIRST part's body and copies it to `output` while updating `sha256`.
     * Detects the closing boundary of that part to end the stream.
     */
    private fun streamMultipartFileBody(
        input: InputStream,
        boundary: String,
        output: OutputStream,
        sha256: MessageDigest,
        onBytes: (Int) -> Unit,
    ): Boolean {
        val boundaryBytes = "--$boundary".toByteArray(Charsets.ISO_8859_1)
        // \r\n--<boundary>
        val marker = ByteArray(2 + boundaryBytes.size)
        marker[0] = 13
        marker[1] = 10
        System.arraycopy(boundaryBytes, 0, marker, 2, boundaryBytes.size)

        // Phase 1: skip preamble until we see the boundary
        val phaseBuf = ByteArray(8192)
        var phaseFilled = 0
        var foundBoundary = false
        while (!foundBoundary) {
            val idx = indexOf(phaseBuf, phaseFilled, boundaryBytes, 0)
            if (idx >= 0) {
                // Skip the boundary line — read to end of line (\r\n)
                foundBoundary = true
                // Discard any bytes after the boundary in phaseBuf; also read to next LF
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
                    // Need to read more to find the LF
                    while (true) {
                        val n = input.read()
                        if (n < 0) return false
                        if (n == 10) break
                    }
                }
                // Copy any leftover bytes after the LF back into phaseBuf for header parsing
                val leftoverStart = if (foundLf) after else phaseFilled
                val leftoverLen = phaseFilled - leftoverStart
                if (leftoverLen > 0) {
                    System.arraycopy(phaseBuf, leftoverStart, phaseBuf, 0, leftoverLen)
                }
                phaseFilled = leftoverLen
                break
            }
            if (phaseFilled >= phaseBuf.size) {
                // No boundary in 8 KB — malformed
                return false
            }
            val n = input.read(phaseBuf, phaseFilled, phaseBuf.size - phaseFilled)
            if (n <= 0) return false
            phaseFilled += n
        }

        // Phase 2: read part headers until \r\n\r\n
        val doubleCrlf = byteArrayOf(13, 10, 13, 10)
        var foundHeaderEnd = false
        var bodyAlreadyInBuf: ByteArray = ByteArray(0)
        while (!foundHeaderEnd) {
            val idx = indexOf(phaseBuf, phaseFilled, doubleCrlf, 0)
            if (idx >= 0) {
                val bodyStart = idx + 4
                if (bodyStart < phaseFilled) {
                    bodyAlreadyInBuf = phaseBuf.copyOfRange(bodyStart, phaseFilled)
                }
                foundHeaderEnd = true
                break
            }
            if (phaseFilled >= phaseBuf.size) return false
            val n = input.read(phaseBuf, phaseFilled, phaseBuf.size - phaseFilled)
            if (n <= 0) return false
            phaseFilled += n
        }

        // Phase 3: stream file content until we see marker (with trailing -- or \r\n)
        // Match NanoHTTPD's internal 8 KB buffer size for smooth read cadence.
        val win = 8192
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
                // Flush bytes before marker
                if (idx > 0) {
                    output.write(buf, 0, idx)
                    sha256.update(buf, 0, idx)
                    onBytes(idx)
                }
                // Consume "--" or "\r\n" after boundary
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
                    if (n <= 0) return true   // EOF right after marker — treat as end
                    tailFilled += n
                }
                val isClosing = (tail[0] == 45.toByte() && tail[1] == 45.toByte())
                if (isClosing) {
                    // Drain trailing \r\n (best-effort)
                    input.read(ByteArray(2))
                }
                return true
            }

            // Slide window: keep marker.size - 1 bytes so a straddling marker is not lost
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

    private fun indexOf(hay: ByteArray, hayLen: Int, needle: ByteArray, from: Int): Int {
        if (needle.isEmpty()) return from
        val limit = hayLen - needle.size
        var i = from
        outer@ while (i <= limit) {
            var j = 0
            while (j < needle.size) {
                if (hay[i + j] != needle[j]) {
                    i++
                    continue@outer
                }
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