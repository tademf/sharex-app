package com.sharex.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.OpenableColumns
import android.widget.Button
import android.widget.EditText
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import com.sharex.app.receiver.ReceiverService
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okio.BufferedSink
import org.json.JSONObject
import java.io.InputStream
import java.util.concurrent.TimeUnit

class MainActivity : AppCompatActivity() {

    private lateinit var serverIpInput: EditText
    private lateinit var tokenInput: EditText
    private lateinit var selectFileButton: Button
    private lateinit var sendButton: Button
    private lateinit var statusText: TextView
    private lateinit var progressBar: ProgressBar
    private lateinit var startReceiverButton: Button
    private lateinit var receiverStatusText: TextView
    private lateinit var scanQrButton: Button

    private var selectedFileUri: Uri? = null
    private var selectedFileName: String? = null
    private var selectedFileSize: Long = 0L

    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .writeTimeout(10, TimeUnit.MINUTES)
        .readTimeout(10, TimeUnit.MINUTES)
        .build()

    // QR scanner — ZXing launches its own camera activity
    private val scanQrLauncher = registerForActivityResult(ScanContract()) { result ->
        if (result.contents == null) {
            statusText.text = "QR scan cancelled"
            return@registerForActivityResult
        }
        val raw = result.contents
        try {
            val obj = JSONObject(raw)
            val ip = obj.optString("ip", "")
            val port = obj.optInt("port", 8000)
            val token = obj.optString("token", "")
            val device = obj.optString("device", "Unknown device")

            if (ip.isEmpty() || token.isEmpty()) {
                statusText.text = "QR missing ip or token"
                return@registerForActivityResult
            }

            serverIpInput.setText(ip)
            tokenInput.setText(token)
            statusText.text = "Paired with $device\nIP: $ip  Port: $port\nSelect a file to send."
        } catch (e: Exception) {
            statusText.text = "Invalid QR: not a SHAREX code\n${e.message}"
        }
    }

    private val cameraPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) launchScanner() else statusText.text = "Camera permission denied"
    }

    private val pickFileLauncher = registerForActivityResult(
        ActivityResultContracts.GetContent()
    ) { uri: Uri? ->
        if (uri != null) {
            selectedFileUri = uri
            selectedFileName = queryFileName(uri)
            selectedFileSize = queryFileSize(uri)
            val sizeStr = formatBytes(selectedFileSize)
            statusText.text = "Selected: ${selectedFileName ?: "unknown"} ($sizeStr)"
            sendButton.isEnabled = true
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        serverIpInput = findViewById(R.id.serverIpInput)
        tokenInput = findViewById(R.id.tokenInput)
        selectFileButton = findViewById(R.id.selectFileButton)
        sendButton = findViewById(R.id.sendButton)
        statusText = findViewById(R.id.statusText)
        progressBar = findViewById(R.id.progressBar)
        startReceiverButton = findViewById(R.id.startReceiverButton)
        receiverStatusText = findViewById(R.id.receiverStatusText)
        scanQrButton = findViewById(R.id.scanQrButton)

        serverIpInput.setText("192.168.137.1")
        tokenInput.setText("")
        sendButton.isEnabled = false
        receiverStatusText.text = "Receiver: stopped"

        scanQrButton.setOnClickListener {
            val hasCamera = ContextCompat.checkSelfPermission(
                this, Manifest.permission.CAMERA
            ) == PackageManager.PERMISSION_GRANTED
            if (hasCamera) {
                launchScanner()
            } else {
                cameraPermissionLauncher.launch(Manifest.permission.CAMERA)
            }
        }

        selectFileButton.setOnClickListener {
            pickFileLauncher.launch("*/*")
        }

        sendButton.setOnClickListener {
            val uri = selectedFileUri ?: return@setOnClickListener
            val ip = serverIpInput.text.toString().trim()
            val token = tokenInput.text.toString().trim()
            if (ip.isEmpty() || token.isEmpty()) {
                Toast.makeText(this, "Enter IP and token (or scan QR)", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            uploadFile(uri, ip, token)
        }

        startReceiverButton.setOnClickListener {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                if (ContextCompat.checkSelfPermission(
                        this, Manifest.permission.POST_NOTIFICATIONS
                    ) != PackageManager.PERMISSION_GRANTED
                ) {
                    requestPermissions(
                        arrayOf(Manifest.permission.POST_NOTIFICATIONS), 200
                    )
                    return@setOnClickListener
                }
            }
            startReceiver()
        }
    }

    private fun launchScanner() {
        val options = ScanOptions().apply {
            setDesiredBarcodeFormats(ScanOptions.QR_CODE)
            setPrompt("Point at the SHAREX QR on your laptop")
            setBeepEnabled(false)
            setOrientationLocked(false)
            setBarcodeImageEnabled(false)
        }
        scanQrLauncher.launch(options)
    }

    private fun startReceiver() {
        val intent = Intent(this, ReceiverService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }
        receiverStatusText.text =
            "Receiver: listening on port 8080\nSend a file from Windows to this phone's IP."
    }

    private fun queryFileName(uri: Uri): String? {
        var name: String? = null
        contentResolver.query(uri, null, null, null, null)?.use { cursor ->
            if (cursor.moveToFirst()) {
                val index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (index >= 0) name = cursor.getString(index)
            }
        }
        return name
    }

    private fun queryFileSize(uri: Uri): Long {
        var size = 0L
        contentResolver.query(uri, null, null, null, null)?.use { cursor ->
            if (cursor.moveToFirst()) {
                val index = cursor.getColumnIndex(OpenableColumns.SIZE)
                if (index >= 0 && !cursor.isNull(index)) size = cursor.getLong(index)
            }
        }
        return size
    }

    private fun formatBytes(bytes: Long): String {
        if (bytes < 1024) return "$bytes B"
        val kb = bytes / 1024.0
        if (kb < 1024) return String.format("%.1f KB", kb)
        val mb = kb / 1024.0
        if (mb < 1024) return String.format("%.1f MB", mb)
        val gb = mb / 1024.0
        return String.format("%.2f GB", gb)
    }

    private fun uploadFile(uri: Uri, serverIp: String, token: String) {
        lifecycleScope.launch(Dispatchers.IO) {
            try {
                withContext(Dispatchers.Main) {
                    statusText.text = "Preparing upload..."
                    progressBar.progress = 0
                    progressBar.visibility = ProgressBar.VISIBLE
                    sendButton.isEnabled = false
                    selectFileButton.isEnabled = false
                }

                val fileName = selectedFileName ?: "unknown_file"
                val fileSize = selectedFileSize

                val requestBody = object : RequestBody() {
                    override fun contentType() = "application/octet-stream".toMediaType()
                    override fun contentLength() = fileSize

                    override fun writeTo(sink: BufferedSink) {
                        val inputStream: InputStream = contentResolver.openInputStream(uri)
                            ?: throw IllegalStateException("Cannot open file")
                        inputStream.use { input ->
                            val buffer = ByteArray(8192)
                            var totalRead = 0L
                            while (true) {
                                val read = input.read(buffer)
                                if (read == -1) break
                                sink.write(buffer, 0, read)
                                totalRead += read
                                val pct = if (fileSize > 0) ((totalRead * 100) / fileSize).toInt() else 0
                                lifecycleScope.launch(Dispatchers.Main) {
                                    progressBar.progress = pct.coerceIn(0, 100)
                                }
                            }
                        }
                    }
                }

                val multipartBody = MultipartBody.Builder()
                    .setType(MultipartBody.FORM)
                    .addFormDataPart("file", fileName, requestBody)
                    .build()

                val request = Request.Builder()
                    .url("http://$serverIp:8000/upload")
                    .addHeader("X-Pairing-Token", token)
                    .post(multipartBody)
                    .build()

                withContext(Dispatchers.Main) {
                    statusText.text = "Uploading..."
                }

                val response = client.newCall(request).execute()
                val responseBody = response.body?.string() ?: ""

                withContext(Dispatchers.Main) {
                    if (response.isSuccessful) {
                        statusText.text = "Upload successful!\n$responseBody"
                        progressBar.progress = 100
                    } else {
                        statusText.text = "Server error ${response.code}:\n$responseBody"
                    }
                    sendButton.isEnabled = true
                    selectFileButton.isEnabled = true
                }

            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    statusText.text = "Error: ${e.message}"
                    progressBar.visibility = ProgressBar.GONE
                    sendButton.isEnabled = true
                    selectFileButton.isEnabled = true
                }
            }
        }
    }
}