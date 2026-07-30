package com.rtcview.app

import java.net.HttpURLConnection
import java.net.URI
import java.net.URL

/** Deliberately no HTTP library dependency (OkHttp/Retrofit) — the app's
 * networking needs are this small: a GET here and there against a
 * self-hosted server, well served by the platform's own HttpURLConnection. */
object NetUtils {

    /** Accepts "host:port", "host", or a full "http(s)://host:port" and
     * returns a clean "scheme://host[:port]" with no trailing slash, or
     * null if it doesn't look like a usable address at all. */
    fun normalizeServerUrl(input: String): String? {
        var s = input.trim()
        if (s.isEmpty()) return null
        if (!s.startsWith("http://") && !s.startsWith("https://")) {
            s = "http://$s"
        }
        while (s.length > "http://".length && s.endsWith("/")) {
            s = s.substring(0, s.length - 1)
        }
        return try {
            val uri = URI(s)
            if (uri.host.isNullOrEmpty()) null else s
        } catch (e: Exception) {
            null
        }
    }

    /** Simple reachability probe against /api/status. Runs network I/O —
     * callers must invoke this off the main thread. */
    fun pingServer(baseUrl: String): Boolean {
        return try {
            val conn = URL("$baseUrl/api/status").openConnection() as HttpURLConnection
            conn.connectTimeout = 4000
            conn.readTimeout = 4000
            conn.requestMethod = "GET"
            val code = conn.responseCode
            conn.disconnect()
            code in 200..299
        } catch (e: Exception) {
            false
        }
    }
}
