package com.rtcview.app

import android.content.Intent
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/** Entry point (launcher activity). Shows a one-field "server address"
 * screen the first time, or whenever the user asks to change server from
 * MainActivity's overflow menu — otherwise it silently forwards straight
 * to MainActivity so returning users never see this screen. */
class SetupActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val saved = Prefs.getServerUrl(this)
        val forceSetup = intent?.getBooleanExtra(EXTRA_FORCE_SETUP, false) == true
        if (saved != null && !forceSetup) {
            openMain()
            return
        }

        setContentView(R.layout.activity_setup)

        val input = findViewById<EditText>(R.id.serverUrlInput)
        val testButton = findViewById<Button>(R.id.testButton)
        val saveButton = findViewById<Button>(R.id.saveButton)
        val statusText = findViewById<TextView>(R.id.statusText)

        if (saved != null) input.setText(saved)

        testButton.setOnClickListener {
            val url = NetUtils.normalizeServerUrl(input.text.toString())
            if (url == null) {
                statusText.text = getString(R.string.setup_invalid_url)
                return@setOnClickListener
            }
            statusText.text = getString(R.string.setup_testing)
            testButton.isEnabled = false
            Thread {
                val ok = NetUtils.pingServer(url)
                runOnUiThread {
                    testButton.isEnabled = true
                    statusText.text = getString(
                        if (ok) R.string.setup_test_ok else R.string.setup_test_fail
                    )
                }
            }.start()
        }

        saveButton.setOnClickListener {
            val url = NetUtils.normalizeServerUrl(input.text.toString())
            if (url == null) {
                statusText.text = getString(R.string.setup_invalid_url)
                return@setOnClickListener
            }
            Prefs.setServerUrl(this, url)
            NotificationScheduler.schedule(this)
            openMain()
        }
    }

    private fun openMain() {
        startActivity(Intent(this, MainActivity::class.java))
        finish()
    }

    companion object {
        const val EXTRA_FORCE_SETUP = "force_setup"
    }
}
