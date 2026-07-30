package com.rtcview.app

import android.content.Context

/** Thin SharedPreferences wrapper — the only local state this app keeps. */
object Prefs {
    private const val FILE = "rtcview_prefs"
    private const val KEY_SERVER_URL = "server_url"

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun getServerUrl(ctx: Context): String? = prefs(ctx).getString(KEY_SERVER_URL, null)

    fun setServerUrl(ctx: Context, url: String) {
        prefs(ctx).edit().putString(KEY_SERVER_URL, url).apply()
    }

    fun clearServerUrl(ctx: Context) {
        prefs(ctx).edit().remove(KEY_SERVER_URL).apply()
    }
}
