package com.rtcview.app

import android.content.Context

/** Thin SharedPreferences wrapper — the only local state this app keeps. */
object Prefs {
    private const val FILE = "rtcview_prefs"
    private const val KEY_SERVER_URL = "server_url"
    private const val KEY_LAST_NOTIF_ID = "last_notif_id"
    private const val KEY_BATTERY_PROMPT_SHOWN = "battery_prompt_shown"

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun getServerUrl(ctx: Context): String? = prefs(ctx).getString(KEY_SERVER_URL, null)

    fun setServerUrl(ctx: Context, url: String) {
        prefs(ctx).edit().putString(KEY_SERVER_URL, url).apply()
    }

    fun clearServerUrl(ctx: Context) {
        prefs(ctx).edit().remove(KEY_SERVER_URL).apply()
    }

    /** Highest notification `id` (from /api/notifications) already shown,
     * so the periodic poll never re-notifies for the same event twice. */
    fun getLastNotifId(ctx: Context): Long = prefs(ctx).getLong(KEY_LAST_NOTIF_ID, 0L)

    fun setLastNotifId(ctx: Context, id: Long) {
        prefs(ctx).edit().putLong(KEY_LAST_NOTIF_ID, id).apply()
    }

    /** Whether we've already asked the user (once) to exempt the app from
     * battery optimization — OEM battery managers are the other common
     * reason a 15-minute WorkManager poll silently stops firing. */
    fun getBatteryPromptShown(ctx: Context): Boolean = prefs(ctx).getBoolean(KEY_BATTERY_PROMPT_SHOWN, false)

    fun setBatteryPromptShown(ctx: Context, shown: Boolean) {
        prefs(ctx).edit().putBoolean(KEY_BATTERY_PROMPT_SHOWN, shown).apply()
    }
}
