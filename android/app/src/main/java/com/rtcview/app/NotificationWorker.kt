package com.rtcview.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.ActivityCompat
import androidx.core.app.NotificationCompat
import androidx.work.Worker
import androidx.work.WorkerParameters
import org.json.JSONArray
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Runs on a WorkManager-managed background thread (Worker.doWork() is
 * already off the main thread — no coroutines/extra dependency needed for
 * that). Polls GET /api/notifications, and for every row newer than the
 * highest id already seen, posts a native notification that deep-links
 * back into MainActivity at that camera/moment.
 *
 * Deliberately NOT filtering by unread_only=1: "read" is a shared flag
 * with the web UI (opening the bell there marks everything read). If this
 * poller depended on that flag, checking notifications in a browser first
 * would make the Android app see an empty list and never catch up — the
 * lastId cursor below is what actually tracks "already shown on this
 * device", independent of what any other client has viewed. */
class NotificationWorker(appContext: Context, params: WorkerParameters) :
    Worker(appContext, params) {

    override fun doWork(): Result {
        val baseUrl = Prefs.getServerUrl(applicationContext) ?: return Result.success()
        val lastId = Prefs.getLastNotifId(applicationContext)

        val json = NetUtils.getJson("$baseUrl/api/notifications?limit=50")
            ?: return Result.retry()

        val notifications = try {
            JSONArray(json)
        } catch (e: Exception) {
            return Result.success()
        }

        var maxId = lastId
        var cameras: JSONArray? = null

        for (i in 0 until notifications.length()) {
            val n = notifications.getJSONObject(i)
            val id = n.optLong("id", 0L)
            if (id <= lastId) continue
            if (id > maxId) maxId = id

            if (cameras == null) {
                cameras = NetUtils.getJson("$baseUrl/api/cameras")?.let {
                    try { JSONArray(it) } catch (e: Exception) { null }
                }
            }
            val camId = n.optString("cam_id", "")
            val camName = findCameraName(cameras, camId) ?: camId
            val kind = n.optString("kind", "motion")
            val eventTs = n.optDouble("event_ts", 0.0)

            postNotification(id, camId, camName, kind, eventTs)
        }

        if (maxId > lastId) Prefs.setLastNotifId(applicationContext, maxId)
        return Result.success()
    }

    private fun findCameraName(cameras: JSONArray?, camId: String): String? {
        if (cameras == null) return null
        for (i in 0 until cameras.length()) {
            val c = cameras.getJSONObject(i)
            if (c.optString("id") == camId) return c.optString("name", camId)
        }
        return null
    }

    private fun postNotification(
        notifId: Long, camId: String, camName: String, kind: String, eventTs: Double
    ) {
        val context = applicationContext

        // API 33+ requires POST_NOTIFICATIONS at runtime; if it was never
        // granted (denied, or the request dialog got missed), notify()
        // would either no-op or throw depending on OEM — check explicitly
        // and skip rather than crash doWork() for the whole batch.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ActivityCompat.checkSelfPermission(context, android.Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED
        ) return

        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                context.getString(R.string.notif_channel_name),
                NotificationManager.IMPORTANCE_HIGH
            ).apply { description = context.getString(R.string.notif_channel_desc) }
            nm.createNotificationChannel(channel)
        }

        val kindLabel = context.getString(
            if (kind == "person") R.string.notif_kind_person else R.string.notif_kind_motion
        )
        val timeStr = if (eventTs > 0) {
            SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date((eventTs * 1000).toLong()))
        } else ""
        val title = context.getString(R.string.notif_title_fmt, timeStr, camName)

        val intent = Intent(context, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
            putExtra(MainActivity.EXTRA_CAM_ID, camId)
            putExtra(MainActivity.EXTRA_EVENT_TS, eventTs)
        }
        val pendingIntent = PendingIntent.getActivity(
            context, notifId.toInt(), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(title)
            .setContentText(kindLabel)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_ALARM)
            .setAutoCancel(true)
            .setContentIntent(pendingIntent)
            .build()

        try {
            nm.notify(notifId.toInt(), notification)
        } catch (e: SecurityException) { /* permission revoked between the check above and here */ }
    }

    companion object {
        private const val CHANNEL_ID = "rtcview_notifications"
    }
}
