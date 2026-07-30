package com.rtcview.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.webkit.CookieManager
import android.webkit.PermissionRequest
import android.webkit.URLUtil
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.Toolbar
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.core.content.getSystemService

/** Full-screen WebView wrapper around the existing RtcView web app — the
 * whole point is to reuse that UI as-is (grid, live view, playback,
 * settings) rather than re-implement it natively. This activity's own job
 * is just: point the WebView at the configured server, make WebRTC/
 * autoplay/fullscreen/downloads work correctly inside a WebView (none of
 * which are on by default), and turn a notification tap into a deep-link
 * URL the web app already knows how to open (see handleDeepLink() in
 * app.js). */
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var rootLayout: FrameLayout
    private var customView: View? = null
    private var customViewCallback: WebChromeClient.CustomViewCallback? = null
    private var baseUrl: String = ""

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val savedUrl = Prefs.getServerUrl(this)
        if (savedUrl == null) {
            startActivity(Intent(this, SetupActivity::class.java))
            finish()
            return
        }
        baseUrl = savedUrl

        setContentView(R.layout.activity_main)
        setSupportActionBar(findViewById<Toolbar>(R.id.toolbar))
        supportActionBar?.title = getString(R.string.app_name)

        rootLayout = findViewById(R.id.rootLayout)
        webView = findViewById(R.id.webView)
        setUpWebView()

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                when {
                    customView != null -> webView.webChromeClient?.onHideCustomView()
                    webView.canGoBack() -> webView.goBack()
                    else -> {
                        isEnabled = false
                        onBackPressedDispatcher.onBackPressed()
                    }
                }
            }
        })

        webView.loadUrl(urlForIntent(intent))

        NotificationScheduler.schedule(this)
        requestNotificationPermissionIfNeeded()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        webView.loadUrl(urlForIntent(intent))
    }

    /** A notification tap arrives as extras on the launch intent — turn
     * those into the ?open_cam=&open_at= query params app.js already
     * looks for (see handleDeepLink() there). No extras -> plain root URL. */
    private fun urlForIntent(intent: Intent?): String {
        val camId = intent?.getStringExtra(EXTRA_CAM_ID)
        val eventTs = intent?.getDoubleExtra(EXTRA_EVENT_TS, -1.0) ?: -1.0
        return if (camId != null && eventTs >= 0) {
            "$baseUrl/?open_cam=${Uri.encode(camId)}&open_at=$eventTs"
        } else {
            baseUrl
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setUpWebView() {
        val settings = webView.settings
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        settings.databaseEnabled = true
        // go2rtc's live video must autoplay the instant a tile mounts —
        // WebView defaults to requiring a user gesture, which would leave
        // every tile stuck on its first frame.
        settings.mediaPlaybackRequiresUserGesture = false
        settings.mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
        settings.cacheMode = WebSettings.LOAD_DEFAULT
        settings.setSupportZoom(false)
        settings.builtInZoomControls = false

        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true)

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, url: String): Boolean {
                if (url.startsWith(baseUrl)) return false
                // Anything pointing off-server (e.g. a "view source" style
                // link) opens in a real browser instead of navigating the
                // app away from RtcView.
                return try {
                    startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
                    true
                } catch (e: Exception) {
                    false
                }
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            // HTML5 fullscreen (the web app's own "F" fullscreen toggle)
            // needs these two to actually fill the screen inside a WebView.
            override fun onShowCustomView(view: View, callback: CustomViewCallback) {
                if (customView != null) {
                    callback.onCustomViewHidden()
                    return
                }
                customView = view
                customViewCallback = callback
                rootLayout.addView(
                    view,
                    FrameLayout.LayoutParams(
                        FrameLayout.LayoutParams.MATCH_PARENT,
                        FrameLayout.LayoutParams.MATCH_PARENT
                    )
                )
                webView.visibility = View.GONE
                @Suppress("DEPRECATION")
                window.decorView.systemUiVisibility = IMMERSIVE_FLAGS
            }

            override fun onHideCustomView() {
                val view = customView ?: return
                rootLayout.removeView(view)
                customView = null
                customViewCallback?.onCustomViewHidden()
                customViewCallback = null
                webView.visibility = View.VISIBLE
                @Suppress("DEPRECATION")
                window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_VISIBLE
            }

            // This app only ever RECEIVES camera streams (go2rtc handles
            // the actual capture server-side) — it never needs this
            // device's own camera/mic, so deny rather than silently grant.
            override fun onPermissionRequest(request: PermissionRequest) {
                request.deny()
            }
        }

        webView.setDownloadListener { url, _, contentDisposition, mimeType, _ ->
            try {
                val request = DownloadManager.Request(Uri.parse(url))
                val fileName = URLUtil.guessFileName(url, contentDisposition, mimeType)
                request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, fileName)
                getSystemService<DownloadManager>()?.enqueue(request)
            } catch (e: Exception) { /* best-effort — snapshot/segment download isn't critical path */ }
        }
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            ActivityCompat.requestPermissions(
                this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQ_NOTIF_PERMISSION
            )
        }
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.main_menu, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            R.id.action_reload -> {
                webView.reload()
                true
            }
            R.id.action_change_server -> {
                val i = Intent(this, SetupActivity::class.java)
                i.putExtra(SetupActivity.EXTRA_FORCE_SETUP, true)
                startActivity(i)
                true
            }
            else -> super.onOptionsItemSelected(item)
        }
    }

    companion object {
        const val EXTRA_CAM_ID = "cam_id"
        const val EXTRA_EVENT_TS = "event_ts"
        private const val REQ_NOTIF_PERMISSION = 1001

        @Suppress("DEPRECATION")
        private const val IMMERSIVE_FLAGS = (
            View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_FULLSCREEN
                or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            )
    }
}
