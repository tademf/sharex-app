package com.sharex.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.MediaStore
import android.provider.OpenableColumns
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.ImageButton
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.GravityCompat
import androidx.drawerlayout.widget.DrawerLayout
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.GridLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.tabs.TabLayout
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import com.sharex.app.receiver.ReceiverService
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicBoolean

class MainActivity : AppCompatActivity() {

    private lateinit var drawerLayout: DrawerLayout
    private lateinit var menuButton: ImageButton
    private lateinit var tabLayout: TabLayout
    private lateinit var photoRecyclerView: RecyclerView

    private lateinit var serverIpInput: EditText
    private lateinit var tokenInput: EditText
    private lateinit var selectFileButton: Button
    private lateinit var sendButton: Button
    private lateinit var cancelButton: Button
    private lateinit var statusText: TextView
    private lateinit var progressBar: ProgressBar
    private lateinit var startReceiverButton: Button
    private lateinit var receiverStatusText: TextView
    private lateinit var scanQrButton: ImageButton

    private var selectedFileUri: Uri? = null
    private var selectedFileName: String? = null
    private var selectedFileSize: Long = 0L

    private val cancelFlag = AtomicBoolean(false)
    private val pauseFlag = AtomicBoolean(false)
    private var lastSessionId: String? = null
    private var lastServerIp: String? = null
    private var lastToken: String? = null
    private var isPaused = false

    private lateinit var photoAdapter: PhotoAdapter
    private val photoList = mutableListOf<String>()

    private val uploader by lazy { ChunkUploader(contentResolver) }

    private val requestStoragePermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { isGranted ->
        if (isGranted) loadPhotos()
        else Toast.makeText(this, "Permission denied", Toast.LENGTH_SHORT).show()
    }

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

        drawerLayout = findViewById(R.id.drawer_layout)
        menuButton = findViewById(R.id.menuButton)
        tabLayout = findViewById(R.id.tabLayout)
        photoRecyclerView = findViewById(R.id.photoRecyclerView)

        serverIpInput = findViewById(R.id.serverIpInput)
        tokenInput = findViewById(R.id.tokenInput)
        selectFileButton = findViewById(R.id.selectFileButton)
        sendButton = findViewById(R.id.sendButton)
        statusText = findViewById(R.id.statusText)
        progressBar = findViewById(R.id.progressBar)
        startReceiverButton = findViewById(R.id.startReceiverButton)
        receiverStatusText = findViewById(R.id.receiverStatusText)
        scanQrButton = findViewById(R.id.scanQrButton)
        cancelButton = findViewById(R.id.cancelButton)

        serverIpInput.setText("192.168.137.1")
        tokenInput.setText("")
        sendButton.isEnabled = false
        cancelButton.isEnabled = false
        cancelButton.text = "Pause"
        receiverStatusText.text = "Receiver: stopped"

        // Setup Photo Grid
        photoRecyclerView.layoutManager = GridLayoutManager(this, 3)
        photoAdapter = PhotoAdapter(photoList)
        photoRecyclerView.adapter = photoAdapter

        menuButton.setOnClickListener {
            drawerLayout.openDrawer(GravityCompat.START)
        }

        val tabs = arrayOf("History", "Apps", "Photos", "Videos", "Music", "Files", "Contacts")
        for (tab in tabs) {
            tabLayout.addTab(tabLayout.newTab().setText(tab))
        }

        tabLayout.addOnTabSelectedListener(object : TabLayout.OnTabSelectedListener {
            override fun onTabSelected(tab: TabLayout.Tab?) {
                val tabName = tab?.text?.toString() ?: ""

                if (tabName == "Photos") {
                    photoRecyclerView.visibility = View.VISIBLE
                    hideTransferForm()
                    checkAndLoadPhotos()
                } else if (tabName == "History") {
                    photoRecyclerView.visibility = View.GONE
                    showTransferForm()
                } else {
                    photoRecyclerView.visibility = View.GONE
                    showTransferForm()
                    Toast.makeText(this@MainActivity, "$tabName coming soon!", Toast.LENGTH_SHORT).show()
                }
            }
            override fun onTabUnselected(tab: TabLayout.Tab?) {}
            override fun onTabReselected(tab: TabLayout.Tab?) {}
        })

        scanQrButton.setOnClickListener {
            val hasCamera = ContextCompat.checkSelfPermission(
                this, Manifest.permission.CAMERA
            ) == PackageManager.PERMISSION_GRANTED
            if (hasCamera) launchScanner()
            else cameraPermissionLauncher.launch(Manifest.permission.CAMERA)
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
            startUpload(uri, ip, token)
        }

        cancelButton.setOnClickListener {
            handleCancelOrPause()
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

    private fun hideTransferForm() {
        serverIpInput.visibility = View.GONE
        tokenInput.visibility = View.GONE
        selectFileButton.visibility = View.GONE
        sendButton.visibility = View.GONE
        progressBar.visibility = View.GONE
        statusText.visibility = View.GONE
    }

    private fun showTransferForm() {
        serverIpInput.visibility = View.VISIBLE
        tokenInput.visibility = View.VISIBLE
        selectFileButton.visibility = View.VISIBLE
        sendButton.visibility = View.VISIBLE
        progressBar.visibility = View.VISIBLE
        statusText.visibility = View.VISIBLE
    }

    private fun checkAndLoadPhotos() {
        val permission = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            Manifest.permission.READ_MEDIA_IMAGES
        } else {
            Manifest.permission.READ_EXTERNAL_STORAGE
        }

        if (ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED) {
            loadPhotos()
        } else {
            requestStoragePermission.launch(permission)
        }
    }

    private fun loadPhotos() {
        photoList.clear()
        val projection = arrayOf(MediaStore.Images.Media._ID, MediaStore.Images.Media.DATA)
        val cursor = contentResolver.query(
            MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
            projection, null, null, "${MediaStore.Images.Media.DATE_ADDED} DESC"
        )

        cursor?.use {
            val dataColumn = it.getColumnIndexOrThrow(MediaStore.Images.Media.DATA)
            while (it.moveToNext()) {
                photoList.add(it.getString(dataColumn))
            }
        }
        photoAdapter.notifyDataSetChanged()

        if (photoList.isEmpty()) {
            Toast.makeText(this, "No photos found", Toast.LENGTH_SHORT).show()
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

    private fun handleCancelOrPause() {
        if (!isPaused) {
            pauseFlag.set(true)
            cancelButton.text = "Resume"
            statusText.text = "Pausing…"
            isPaused = true
        } else {
            pauseFlag.set(false)
            cancelButton.text = "Pause"
            isPaused = false
            val uri = selectedFileUri ?: return
            val ip = lastServerIp ?: serverIpInput.text.toString().trim()
            val token = lastToken ?: tokenInput.text.toString().trim()
            if (ip.isEmpty() || token.isEmpty()) return
            startUpload(uri, ip, token, resumeId = lastSessionId)
        }
    }

    private fun startUpload(uri: Uri, serverIp: String, token: String, resumeId: String? = null) {
        cancelFlag.set(false)
        pauseFlag.set(false)
        isPaused = false
        cancelButton.text = "Pause"
        cancelButton.isEnabled = true

        lastServerIp = serverIp
        lastToken = token
        lastSessionId = resumeId

        val fileName = selectedFileName ?: "unknown_file"
        val fileSize = selectedFileSize

        lifecycleScope.launch(Dispatchers.IO) {
            withContext(Dispatchers.Main) {
                statusText.text = if (resumeId != null) "Resuming…" else "Preparing…"
                progressBar.progress = 0
                progressBar.visibility = ProgressBar.VISIBLE
                sendButton.isEnabled = false
                selectFileButton.isEnabled = false
            }

            try {
                val result = uploader.uploadFile(
                    uri = uri,
                    filename = fileName,
                    totalSize = fileSize,
                    serverIp = serverIp,
                    token = token,
                    resumeId = resumeId,
                    cancel = cancelFlag,
                    pause = pauseFlag,
                    callbacks = object : ChunkUploader.Callbacks {
                        override fun onHashing() {
                            lifecycleScope.launch(Dispatchers.Main) {
                                statusText.text = "Hashing…"
                            }
                        }

                        override fun onSessionStarted(sessionId: String, totalChunks: Int, neededChunks: Int) {
                            lastSessionId = sessionId
                            lifecycleScope.launch(Dispatchers.Main) {
                                val label = if (neededChunks < totalChunks) {
                                    "Resuming (${totalChunks - neededChunks}/$totalChunks done)"
                                } else {
                                    "Uploading 0/$neededChunks chunks"
                                }
                                statusText.text = label
                            }
                        }

                        override fun onChunkDone(index: Int, done: Int, total: Int) {
                            lifecycleScope.launch(Dispatchers.Main) {
                                statusText.text = "Uploading $done/$total chunks"
                                val pct = if (total > 0) (done * 100 / total) else 0
                                progressBar.progress = pct.coerceIn(0, 100)
                            }
                        }
                    },
                )

                withContext(Dispatchers.Main) {
                    statusText.text = "Success!\n${result.storedFilename}\n${formatBytes(result.sizeBytes)}\nSHA: ${result.sha256.take(16)}…"
                    progressBar.progress = 100
                    sendButton.isEnabled = true
                    selectFileButton.isEnabled = true
                    cancelButton.isEnabled = false
                    cancelButton.text = "Pause"
                }
            } catch (e: ChunkUploader.UploadCancelled) {
                lastSessionId?.let { sid ->
                    uploader.cancelSession(serverIp, token, sid)
                }
                withContext(Dispatchers.Main) {
                    statusText.text = "Cancelled."
                    progressBar.visibility = ProgressBar.GONE
                    sendButton.isEnabled = true
                    selectFileButton.isEnabled = true
                    cancelButton.isEnabled = false
                    cancelButton.text = "Pause"
                }
            } catch (e: ChunkUploader.UploadPaused) {
                withContext(Dispatchers.Main) {
                    statusText.text = "Paused. Tap Resume to continue."
                    cancelButton.text = "Resume"
                    cancelButton.isEnabled = true
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    statusText.text = "Error: ${e.message?.take(200)}"
                    progressBar.visibility = ProgressBar.GONE
                    sendButton.isEnabled = true
                    selectFileButton.isEnabled = true
                    cancelButton.isEnabled = false
                    cancelButton.text = "Pause"
                }
            }
        }
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
}