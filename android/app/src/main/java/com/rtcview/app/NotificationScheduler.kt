package com.rtcview.app

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.util.concurrent.TimeUnit

/** Schedules the background notification poll. 15 minutes is the shortest
 * interval WorkManager's PeriodicWorkRequest allows — there's no HTTPS on
 * this server, so real push (Web Push/FCM) isn't an option, and a
 * foreground service holding an always-on connection was traded away
 * deliberately in favor of no persistent notification-bar icon. */
object NotificationScheduler {
    private const val UNIQUE_WORK_NAME = "rtcview_notification_poll"

    fun schedule(context: Context) {
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()
        val request = PeriodicWorkRequestBuilder<NotificationWorker>(15, TimeUnit.MINUTES)
            .setConstraints(constraints)
            .setBackoffCriteria(BackoffPolicy.LINEAR, 1, TimeUnit.MINUTES)
            .build()
        // KEEP: re-saving the same server address (e.g. reopening Setup
        // without changing anything) shouldn't reset the poll schedule.
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            UNIQUE_WORK_NAME, ExistingPeriodicWorkPolicy.KEEP, request
        )
    }

    fun cancel(context: Context) {
        WorkManager.getInstance(context).cancelUniqueWork(UNIQUE_WORK_NAME)
    }
}
