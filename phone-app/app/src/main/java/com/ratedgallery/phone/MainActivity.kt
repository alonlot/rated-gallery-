package com.ratedgallery.phone

import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.ClipData
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.View
import android.webkit.CookieManager
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.lifecycle.lifecycleScope
import androidx.webkit.WebViewAssetLoader
import com.ratedgallery.phone.core.HttpStatusException
import com.ratedgallery.phone.core.OkHttpDeviceApi
import com.ratedgallery.phone.core.PairParams
import com.ratedgallery.phone.core.SyncEngine
import com.ratedgallery.phone.core.SyncStatus
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.IOException
import java.text.DateFormat
import java.util.Date

class MainActivity : AppCompatActivity(), StatusRepo.Listener {

    enum class Tab { GALLERY, PC, SETTINGS }

    private lateinit var prefs: Prefs
    private lateinit var galleryWeb: WebView
    private lateinit var pcWeb: WebView
    private lateinit var pcContainer: View
    private lateinit var pcOverlay: LinearLayout
    private lateinit var pcMessage: TextView
    private lateinit var settingsView: View
    private lateinit var tabs: Map<Tab, Button>
    private lateinit var tabTextColors: android.content.res.ColorStateList

    private var current = Tab.GALLERY
    /** The session URL currently loaded in the PC tab (null = needs loading). */
    private var pcLoadedUrl: String? = null
    private var pcMainFrameError = false

    private val permLauncher = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        updatePermissionUi()
        if (Permissions.photoAccess(this) != Permissions.PhotoAccess.NONE) SyncScheduler.syncNow(this)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        prefs = Prefs(this)

        val root = findViewById<View>(R.id.root)
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val b = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime())
            v.setPadding(b.left, b.top, b.right, b.bottom)
            WindowInsetsCompat.CONSUMED
        }

        galleryWeb = findViewById(R.id.gallery_web)
        pcWeb = findViewById(R.id.pc_web)
        pcContainer = findViewById(R.id.pc_container)
        pcOverlay = findViewById(R.id.pc_overlay)
        pcMessage = findViewById(R.id.pc_message)
        settingsView = findViewById(R.id.settings_view)
        tabs = mapOf(
            Tab.GALLERY to findViewById(R.id.tab_gallery),
            Tab.PC to findViewById(R.id.tab_pc),
            Tab.SETTINGS to findViewById(R.id.tab_settings),
        )
        tabTextColors = tabs.getValue(Tab.GALLERY).textColors
        tabs.forEach { (tab, button) -> button.setOnClickListener { showTab(tab) } }

        setupGallery()
        setupPcWeb()
        setupSettings()

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                when {
                    current == Tab.PC && pcWeb.canGoBack() -> pcWeb.goBack()
                    current == Tab.GALLERY && galleryWeb.canGoBack() -> galleryWeb.goBack()
                    current != Tab.GALLERY -> showTab(Tab.GALLERY)
                    else -> {
                        isEnabled = false
                        onBackPressedDispatcher.onBackPressed()
                        isEnabled = true
                    }
                }
            }
        })

        val tab = savedInstanceState?.getString(STATE_TAB)?.let { runCatching { Tab.valueOf(it) }.getOrNull() }
        showTab(tab ?: if (prefs.paired) Tab.GALLERY else Tab.SETTINGS)

        if (savedInstanceState == null) {
            if (!handlePairIntent(intent)) {
                if (Permissions.photoAccess(this) == Permissions.PhotoAccess.NONE) askPermissions()
            }
            // Sync on app start.
            SyncScheduler.syncNow(this, SyncEngine.Mode.FULL)
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handlePairIntent(intent)
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString(STATE_TAB, current.name)
    }

    override fun onStart() {
        super.onStart()
        StatusRepo.addListener(this)
        StatusRepo.refreshFromPrefs(this)
        onStatus(StatusRepo.status)
    }

    override fun onResume() {
        super.onResume()
        updatePermissionUi()
    }

    override fun onStop() {
        StatusRepo.removeListener(this)
        super.onStop()
    }

    override fun onDestroy() {
        galleryWeb.destroy()
        pcWeb.destroy()
        super.onDestroy()
    }

    // ---- Tabs ----

    fun showTab(tab: Tab) {
        current = tab
        galleryWeb.visibility = if (tab == Tab.GALLERY) View.VISIBLE else View.GONE
        pcContainer.visibility = if (tab == Tab.PC) View.VISIBLE else View.GONE
        settingsView.visibility = if (tab == Tab.SETTINGS) View.VISIBLE else View.GONE
        val accent = ContextCompat.getColor(this, R.color.accent)
        tabs.forEach { (t, b) ->
            b.isSelected = t == tab
            if (t == tab) b.setTextColor(accent) else b.setTextColor(tabTextColors)
        }
        if (tab == Tab.PC) ensurePcLoaded()
    }


    // ---- Offline gallery ----

    @SuppressLint("SetJavaScriptEnabled", "JavascriptInterface")
    private fun setupGallery() {
        val loader = WebViewAssetLoader.Builder()
            .addPathHandler("/assets/", WebViewAssetLoader.AssetsPathHandler(this))
            .addPathHandler("/media/", MediaPathHandler(this))
            .build()
        with(galleryWeb.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            cacheMode = WebSettings.LOAD_DEFAULT
            mediaPlaybackRequiresUserGesture = true
        }
        galleryWeb.webViewClient = object : WebViewClient() {
            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
                val url = request.url
                if (url.scheme == "https" && url.host == ASSET_HOST) {
                    return loader.shouldInterceptRequest(url) ?: MediaPathHandler.notFound()
                }
                // Map tiles: only requested after the user turns the map on in the gallery (opt-in, as on the PC).
                if (url.scheme == "https" && url.host == MAP_TILE_HOST && request.method == "GET") {
                    return null
                }
                // Everything else is blocked: the offline gallery makes no other network requests.
                return MediaPathHandler.notFound()
            }

            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url
                return !(url.scheme == "https" && url.host == ASSET_HOST)
            }

            override fun onPageFinished(view: WebView, url: String) {
                pushStatus(StatusRepo.status)
            }
        }
        galleryWeb.addJavascriptInterface(GalleryBridge(this), "RGBridge")
        galleryWeb.loadUrl(galleryUrl())
    }

    private fun galleryUrl(): String {
        val hasWeb = try {
            assets.list("web")?.contains("gallery.html") == true
        } catch (e: IOException) {
            false
        }
        val dir = if (hasWeb) "web" else "web-fallback"
        return "https://$ASSET_HOST/assets/$dir/gallery.html"
    }

    private fun pushStatus(s: SyncStatus) {
        galleryWeb.evaluateJavascript("window.RG_onStatus && window.RG_onStatus(${s.toJson()})", null)
    }

    override fun onStatus(status: SyncStatus) {
        pushStatus(status)
        updateStatusUi(status)
    }

    override fun onIndexUpdated() {
        galleryWeb.evaluateJavascript("window.RG_onIndexUpdated && window.RG_onIndexUpdated()", null)
    }

    fun shareMedia(mid: String) {
        val src = MediaStoreSource(this)
        val uri = src.uriFor(mid) ?: return
        val mime = src.mimeOf(mid) ?: "image/*"
        val send = Intent(Intent.ACTION_SEND)
            .setType(mime)
            .putExtra(Intent.EXTRA_STREAM, uri)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        send.clipData = ClipData.newRawUri("", uri)
        try {
            startActivity(Intent.createChooser(send, getString(R.string.share)))
        } catch (e: ActivityNotFoundException) {
            Toast.makeText(this, e.message, Toast.LENGTH_SHORT).show()
        }
    }

    // ---- PC tab (full PC web UI) ----

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupPcWeb() {
        with(pcWeb.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = false
        }
        CookieManager.getInstance().setAcceptCookie(true)
        pcWeb.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url
                if (url.host == prefs.host) return false
                // Links to other sites open outside the app.
                try {
                    startActivity(Intent(Intent.ACTION_VIEW, url))
                } catch (e: ActivityNotFoundException) {
                    // ignore
                }
                return true
            }

            override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
                pcMainFrameError = false
            }

            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) {
                    pcMainFrameError = true
                    pcLoadedUrl = null
                    showPcOverlay(getString(R.string.pc_unreachable), retry = true)
                }
            }

            override fun onPageFinished(view: WebView, url: String) {
                if (!pcMainFrameError) pcOverlay.visibility = View.GONE
            }
        }
        findViewById<Button>(R.id.pc_retry).setOnClickListener {
            if (prefs.paired) {
                pcLoadedUrl = null
                ensurePcLoaded()
            } else {
                showTab(Tab.SETTINGS)
            }
        }
    }

    private fun showPcOverlay(msg: String, retry: Boolean) {
        pcMessage.text = msg
        findViewById<Button>(R.id.pc_retry).text = getString(if (retry) R.string.retry else R.string.tab_settings)
        pcOverlay.visibility = View.VISIBLE
    }

    private fun ensurePcLoaded() {
        val api = prefs.api()
        if (api == null) {
            showPcOverlay(getString(if (prefs.needsRepair) R.string.state_needs_repair else R.string.pc_not_paired), retry = false)
            return
        }
        val url = api.sessionUrl()
        if (pcLoadedUrl == url) return
        pcLoadedUrl = url
        pcOverlay.visibility = View.GONE
        pcWeb.loadUrl(url)
    }

    // ---- Settings ----

    private lateinit var hostEdit: EditText
    private lateinit var portEdit: EditText
    private lateinit var codeEdit: EditText

    private fun setupSettings() {
        hostEdit = findViewById(R.id.host)
        portEdit = findViewById(R.id.port)
        codeEdit = findViewById(R.id.code)
        hostEdit.setText(prefs.host)
        portEdit.setText(prefs.port.toString())

        findViewById<Button>(R.id.pair).setOnClickListener {
            val p = PairParams.validate(hostEdit.text.toString(), portEdit.text.toString(), codeEdit.text.toString())
            if (p == null) toast(getString(R.string.pair_invalid)) else pair(p)
        }
        findViewById<Button>(R.id.unpair).setOnClickListener {
            AlertDialog.Builder(this)
                .setMessage(R.string.unpair_confirm)
                .setPositiveButton(R.string.unpair) { _, _ -> unpair() }
                .setNegativeButton(R.string.cancel, null)
                .show()
        }
        findViewById<Button>(R.id.sync_now).setOnClickListener {
            if (prefs.paired) {
                SyncScheduler.syncNow(this, SyncEngine.Mode.FULL)
                toast(getString(R.string.sync_started))
            } else {
                toast(getString(R.string.pc_not_paired))
            }
        }
        findViewById<Button>(R.id.grant).setOnClickListener { askPermissions() }
        findViewById<Button>(R.id.app_settings).setOnClickListener {
            startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", packageName, null)))
        }
    }

    private fun updateStatusUi(s: SyncStatus) {
        if (!::hostEdit.isInitialized) return
        val pairState = findViewById<TextView>(R.id.pair_state)
        val warn = ContextCompat.getColor(this, R.color.warn)
        when {
            s.needsRepair -> {
                pairState.text = getString(R.string.state_needs_repair)
                pairState.setTextColor(warn)
            }
            s.paired -> {
                val name = prefs.serverName.ifEmpty { s.host }
                pairState.text = getString(R.string.state_paired, name, "${s.host}:${s.port}")
                pairState.setTextColor(findViewById<TextView>(R.id.last_sync).currentTextColor)
            }
            else -> {
                pairState.text = getString(R.string.state_not_paired)
                pairState.setTextColor(findViewById<TextView>(R.id.last_sync).currentTextColor)
            }
        }
        findViewById<Button>(R.id.pair).text = getString(if (s.needsRepair) R.string.pair_again else R.string.pair)
        findViewById<Button>(R.id.unpair).isEnabled = s.paired
        findViewById<Button>(R.id.sync_now).isEnabled = s.paired
        findViewById<TextView>(R.id.conn_state).text =
            if (!s.paired) "" else getString(if (s.connected) R.string.state_connected else R.string.state_not_connected)
        val sync = buildString {
            if (s.syncing) {
                append("Syncing: ").append(s.phase)
                if (s.total > 0) append(" ${s.done}/${s.total}")
            }
            if (s.message.isNotEmpty()) {
                if (isNotEmpty()) append(" · ")
                append(s.message)
            }
        }
        findViewById<TextView>(R.id.sync_state).text = sync
        val last = if (s.lastSync > 0) DateFormat.getDateTimeInstance().format(Date(s.lastSync * 1000)) else getString(R.string.never)
        findViewById<TextView>(R.id.last_sync).text = getString(R.string.last_sync, last)
    }

    private fun updatePermissionUi() {
        val warn = ContextCompat.getColor(this, R.color.warn)
        val normal = findViewById<TextView>(R.id.last_sync).currentTextColor
        val photos = findViewById<TextView>(R.id.perm_photos)
        when (Permissions.photoAccess(this)) {
            Permissions.PhotoAccess.FULL -> { photos.setText(R.string.perm_photos_full); photos.setTextColor(normal) }
            Permissions.PhotoAccess.PARTIAL -> { photos.setText(R.string.perm_photos_partial); photos.setTextColor(warn) }
            Permissions.PhotoAccess.NONE -> { photos.setText(R.string.perm_photos_none); photos.setTextColor(warn) }
        }
        val loc = findViewById<TextView>(R.id.perm_location)
        if (Permissions.hasMediaLocation(this)) { loc.setText(R.string.perm_location_ok); loc.setTextColor(normal) }
        else { loc.setText(R.string.perm_location_missing); loc.setTextColor(warn) }
        val notif = findViewById<TextView>(R.id.perm_notif)
        notif.setText(if (Permissions.hasNotifications(this)) R.string.perm_notif_ok else R.string.perm_notif_missing)
    }

    private fun askPermissions() {
        AlertDialog.Builder(this)
            .setTitle(R.string.perm_dialog_title)
            .setMessage(R.string.perm_dialog_text)
            .setPositiveButton(R.string.continue_) { _, _ -> permLauncher.launch(Permissions.toRequest()) }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    // ---- Pairing ----

    /** Returns true if the intent was a pairing link. */
    private fun handlePairIntent(intent: Intent?): Boolean {
        if (intent?.action != Intent.ACTION_VIEW) return false
        val data = intent.dataString ?: return false
        intent.data = null // handle once
        val p = PairParams.parseLink(data)
        showTab(Tab.SETTINGS)
        if (p == null) {
            toast(getString(R.string.bad_link))
            return true
        }
        hostEdit.setText(p.host)
        portEdit.setText(p.port.toString())
        codeEdit.setText(p.code)
        // Always confirm: a link from anywhere could point to a stranger's PC.
        AlertDialog.Builder(this)
            .setTitle(R.string.pair_confirm_title)
            .setMessage(getString(R.string.pair_confirm_text, p.host, p.port))
            .setPositiveButton(R.string.pair) { _, _ -> pair(p) }
            .setNegativeButton(R.string.cancel, null)
            .show()
        return true
    }

    private fun pair(p: PairParams) {
        toast(getString(R.string.pairing))
        findViewById<Button>(R.id.pair).isEnabled = false
        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) {
                runCatching { OkHttpDeviceApi(p.host, p.port, null).pair(p.code, deviceName()) }
            }
            findViewById<Button>(R.id.pair).isEnabled = true
            result.onSuccess { r ->
                prefs.savePairing(p.host, p.port, r.token, r.deviceId, r.serverName)
                codeEdit.setText("")
                pcLoadedUrl = null
                CookieManager.getInstance().removeAllCookies(null)
                StatusRepo.refreshFromPrefs(this@MainActivity)
                StatusRepo.update { it.copy(message = "") }
                SyncScheduler.schedulePeriodic(this@MainActivity)
                SyncScheduler.syncNow(this@MainActivity, SyncEngine.Mode.FULL)
                toast(getString(R.string.pair_ok, r.serverName.ifEmpty { p.host }))
                if (Permissions.photoAccess(this@MainActivity) == Permissions.PhotoAccess.NONE) askPermissions()
            }.onFailure { e ->
                val msg = when {
                    e is HttpStatusException && e.code == 403 -> getString(R.string.pair_bad_code)
                    e is HttpStatusException -> getString(R.string.pair_failed, e.detail.ifEmpty { "HTTP ${e.code}" })
                    e is IOException -> getString(R.string.pair_unreachable, p.host, p.port)
                    else -> getString(R.string.pair_failed, e.toString())
                }
                toast(msg)
            }
        }
    }

    private fun unpair() {
        prefs.unpair()
        SyncScheduler.cancelAll(this)
        CookieManager.getInstance().removeAllCookies(null)
        pcLoadedUrl = null
        pcWeb.loadUrl("about:blank")
        StatusRepo.refreshFromPrefs(this)
        StatusRepo.update { it.copy(connected = false, message = "") }
    }

    private fun deviceName(): String {
        val n = try {
            Settings.Global.getString(contentResolver, Settings.Global.DEVICE_NAME)
        } catch (e: Exception) {
            null
        }
        return n?.takeIf { it.isNotBlank() } ?: "${Build.MANUFACTURER} ${Build.MODEL}"
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_LONG).show()

    companion object {
        const val ASSET_HOST = "appassets.androidplatform.net"
        const val MAP_TILE_HOST = "tile.openstreetmap.org"
        private const val STATE_TAB = "tab"
    }
}
