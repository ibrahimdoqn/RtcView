package com.rtcview.app

import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.view.View
import android.webkit.CookieManager
import android.webkit.PermissionRequest
import android.webkit.URLUtil
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.ImageButton
import android.widget.PopupMenu
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.getSystemService
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.core.view.updateLayoutParams
import androidx.core.view.updateMargins

/** Full-screen WebView wrapper around the existing RtcView web app — the
 * whole point is to reuse that UI as-is (grid, live view, playback,
 * settings) rather than re-implement it natively. This is a viewing-only
 * client (no background notification polling — WorkManager's 15-minute
 * minimum interval was too slow to be useful, see git history). This
 * activity's own job is just: draw truly edge-to-edge (no native Toolbar
 * — the web app has its own hamburger menu already), verify the server is
 * reachable before ever showing a WebView error page, and make WebRTC/
 * autoplay/fullscreen/downloads work correctly inside a WebView (none of
 * which are on by default). */
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var rootLayout: FrameLayout
    private lateinit var loadingOverlay: View
    private lateinit var btnMenu: ImageButton
    private var customView: View? = null
    private var customViewCallback: WebChromeClient.CustomViewCallback? = null
    private var baseUrl: String = ""
    private var unreachableHandled = false

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
        setUpEdgeToEdge()

        rootLayout = findViewById(R.id.rootLayout)
        webView = findViewById(R.id.webView)
        loadingOverlay = findViewById(R.id.loadingOverlay)
        btnMenu = findViewById(R.id.btnMenu)
        setUpWebView()
        applyInsetMargins()

        btnMenu.setOnClickListener { showMoreMenu(it) }

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

        checkConnectionThenLoad()
    }

    /** Draw the WebView behind the status/navigation bars instead of
     * squeezing it into the space below a native Toolbar — the old bar
     * duplicated the web app's own hamburger menu and just wasted screen. */
    private fun setUpEdgeToEdge() {
        WindowCompat.setDecorFitsSystemWindows(window, false)
        val controller = WindowInsetsControllerCompat(window, window.decorView)
        // Dark background throughout -> light (white) system icons.
        controller.isAppearanceLightStatusBars = false
        controller.isAppearanceLightNavigationBars = false
    }

    /** The glass overlay button and the "connecting…" copy must stay clear
     * of the status bar / camera cutout instead of sliding underneath it —
     * everything else (WebView content) is allowed to draw full-bleed
     * since the web app already accounts for env(safe-area-inset-*). */
    private fun applyInsetMargins() {
        val baseMarginPx = (14 * resources.displayMetrics.density).toInt()
        ViewCompat.setOnApplyWindowInsetsListener(rootLayout) { _, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            btnMenu.updateLayoutParams<FrameLayout.LayoutParams> {
                updateMargins(top = bars.top + baseMarginPx, right = bars.right + baseMarginPx)
            }
            insets
        }
        ViewCompat.requestApplyInsets(rootLayout)
    }

    /** Ping the server before ever pointing the WebView at it — that way a
     * wrong/stale/offline address lands the user back on the address entry
     * screen with a clear reason, instead of WebView's own bare error page. */
    private fun checkConnectionThenLoad() {
        loadingOverlay.visibility = View.VISIBLE
        Thread {
            val ok = NetUtils.pingServer(baseUrl)
            runOnUiThread {
                if (isFinishing) return@runOnUiThread
                if (ok) {
                    loadingOverlay.visibility = View.GONE
                    webView.loadUrl(baseUrl)
                } else {
                    goToSetupUnreachable()
                }
            }
        }.start()
    }

    private fun goToSetupUnreachable() {
        if (unreachableHandled) return
        unreachableHandled = true
        val i = Intent(this, SetupActivity::class.java)
        i.putExtra(SetupActivity.EXTRA_FORCE_SETUP, true)
        i.putExtra(SetupActivity.EXTRA_UNREACHABLE, true)
        startActivity(i)
        finish()
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

            // Belt-and-braces: the ping in checkConnectionThenLoad() covers
            // the common "wrong/offline address" case, but if the main
            // document load itself still fails (e.g. the server dropped
            // between the ping and this request), fall back to Setup
            // instead of leaving WebView's bare error page on screen.
            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) goToSetupUnreachable()
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
                btnMenu.visibility = View.GONE
            }

            override fun onHideCustomView() {
                val view = customView ?: return
                rootLayout.removeView(view)
                customView = null
                customViewCallback?.onCustomViewHidden()
                customViewCallback = null
                webView.visibility = View.VISIBLE
                btnMenu.visibility = View.VISIBLE
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

    private fun showMoreMenu(anchor: View) {
        PopupMenu(this, anchor).apply {
            menuInflater.inflate(R.menu.main_menu, menu)
            setOnMenuItemClickListener { item ->
                when (item.itemId) {
                    R.id.action_reload -> { webView.reload(); true }
                    R.id.action_change_server -> {
                        val i = Intent(this@MainActivity, SetupActivity::class.java)
                        i.putExtra(SetupActivity.EXTRA_FORCE_SETUP, true)
                        startActivity(i)
                        true
                    }
                    else -> false
                }
            }
        }.show()
    }
}
