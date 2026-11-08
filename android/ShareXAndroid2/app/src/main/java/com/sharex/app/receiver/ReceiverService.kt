package com.sharex.app.receiver

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat

class ReceiverService : Service() {

    private var server: SimpleHttpServer? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForegroundWithNotification()
        if (server == null) {
           server = SimpleHttpServer(
                context = applicationContext,
                onFileReceived = { name, size, sha ->
                    Log.i("SHAREX-RECEIVER", "received $name ($size bytes) sha256=$sha")
                    updateNotification("Received: $name", "$size bytes")
                },
            ).also {
                try {
              it.startServer()
                    Log.i("SHAREX-RECEIVER", "listening on port 8080")
                } catch (e: Exception) {
                    Log.e("SHAREX-RECEIVER", "start failed: ${e.message}")
                }
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
      server?.stopServer()
        server = null
        super.onDestroy()
    }

    // ---------- Notifications ----------

    private fun channelId() = "sharex-receiver"

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val mgr = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            if (mgr.getNotificationChannel(channelId()) == null) {
                mgr.createNotificationChannel(
                    NotificationChannel(
                        channelId(), "SHAREX Receiver",
                        NotificationManager.IMPORTANCE_LOW
                    ).apply { description = "SHAREX receiver status" }
                )
            }
        }
    }

    private fun buildNotification(title: String, text: String): Notification {
        ensureChannel()
        return NotificationCompat.Builder(this, channelId())
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentTitle(title)
            .setContentText(text)
            .setOngoing(true)
            .build()
    }

    private fun startForegroundWithNotification() {
        val notif = buildNotification("SHAREX Receiver", "Listening on port 8080")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                1, notif,
                android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
            )
        } else {
            startForeground(1, notif)
        }
    }

    private fun updateNotification(title: String, text: String) {
        val notif = buildNotification(title, text)
        val mgr = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        mgr.notify(1, notif)
    }
}