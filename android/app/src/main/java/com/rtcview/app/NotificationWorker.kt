package com.rtcview.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.work.Worker
import androidx.work.WorkerParameters
import org.json.JSONArray

/** Runs on a WorkManager-managed background thread (Worker.doWork() is
 * already off the main thread — no coroutines/extra dependency needed for
 * that). Polls GET /api/notifications?unread_only=1, and for every row
 * newer than the highest id already seen, posts a native notification
 * that deep-links back into MainActivity at that camera/moment. */
class NotificationWorker(appContext: Context, params: WorkerParameters) :
    Worker(appContext, params) {

    override fun doWork(): Result {
        val baseUrl = Prefs.getServerUrl(applicationContext) ?: return Result.success()
        val lastId = Prefs.getLastNotifId(applicationContext)

        val json = NetUtils.getJson("$baseUrl/api/notifications?unread_only=1&limit=50")
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
        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                context.getString(R.string.notif_channel_name),
                NotificationManager.IMPORTANCE_DEFAULT
            ).apply { description = context.getString(R.string.notif_channel_desc) }
            nm.createNotificationChannel(channel)
        }

        val kindLabel = context.getString(
            if (kind == "person") R.string.notif_kind_person else R.string.notif_kind_motion
        )

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
            .setContentTitle(camName)
            .setContentText(kindLabel)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setAutoCancel(true)
            .setContentIntent(pendingIntent)
            .build()

        nm.notify(notifId.toInt(), notification)
    }

    companion object {
        private const val CHANNEL_ID = "rtcview_notifications"
    }
}
