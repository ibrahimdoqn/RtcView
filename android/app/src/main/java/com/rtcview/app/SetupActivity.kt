package com.rtcview.app

import android.content.Intent
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsControllerCompat

/** Entry point (launcher activity). Shows a one-field "server address"
 * screen the first time, whenever the user asks to change server from
 * MainActivity's overflow menu, or whenever MainActivity couldn't reach
 * the saved address (EXTRA_UNREACHABLE) — otherwise it silently forwards
 * straight to MainActivity so returning users never see this screen. */
class SetupActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val saved = Prefs.getServerUrl(this)
        val forceSetup = intent?.getBooleanExtra(EXTRA_FORCE_SETUP, false) == true
        if (saved != null && !forceSetup) {
            openMain()
            return
        }

        WindowCompat.setDecorFitsSystemWindows(window, false)
        WindowInsetsControllerCompat(window, window.decorView).apply {
            isAppearanceLightStatusBars = false
            isAppearanceLightNavigationBars = false
        }

        setContentView(R.layout.activity_setup)

        val input = findViewById<EditText>(R.id.serverUrlInput)
        val testButton = findViewById<Button>(R.id.testButton)
        val saveButton = findViewById<Button>(R.id.saveButton)
        val statusText = findViewById<TextView>(R.id.statusText)

        if (saved != null) input.setText(saved)

        val unreachable = intent?.getBooleanExtra(EXTRA_UNREACHABLE, false) == true
        if (unreachable) {
            statusText.text = getString(R.string.setup_unreachable_hint)
            statusText.setTextColor(ContextCompat.getColor(this, R.color.rtcview_danger))
        }

        testButton.setOnClickListener {
            val url = NetUtils.normalizeServerUrl(input.text.toString())
            if (url == null) {
                setStatus(statusText, getString(R.string.setup_invalid_url), R.color.rtcview_danger)
                return@setOnClickListener
            }
            setStatus(statusText, getString(R.string.setup_testing), R.color.rtcview_muted)
            testButton.isEnabled = false
            Thread {
                val ok = NetUtils.pingServer(url)
                runOnUiThread {
                    testButton.isEnabled = true
                    setStatus(
                        statusText,
                        getString(if (ok) R.string.setup_test_ok else R.string.setup_test_fail),
                        if (ok) R.color.rtcview_success else R.color.rtcview_danger
                    )
                }
            }.start()
        }

        saveButton.setOnClickListener {
            val url = NetUtils.normalizeServerUrl(input.text.toString())
            if (url == null) {
                setStatus(statusText, getString(R.string.setup_invalid_url), R.color.rtcview_danger)
                return@setOnClickListener
            }
            Prefs.setServerUrl(this, url)
            NotificationScheduler.schedule(this)
            openMain()
        }
    }

    private fun setStatus(view: TextView, text: String, colorRes: Int) {
        view.text = text
        view.setTextColor(ContextCompat.getColor(this, colorRes))
    }

    private fun openMain() {
        startActivity(Intent(this, MainActivity::class.java))
        finish()
    }

    companion object {
        const val EXTRA_FORCE_SETUP = "force_setup"
        const val EXTRA_UNREACHABLE = "unreachable"
    }
}
