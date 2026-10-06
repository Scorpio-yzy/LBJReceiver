/*
 * LBJ Receiver —— Android 端 铁路列车接近预警接收机
 * Copyright (C) 2026 Scorpio-yzy
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * 本文件是 LBJ Receiver 的一部分，以 GPL-3.0-or-later 发布；详见 LICENSE 与 THIRD_PARTY.md。
 */

package com.railfan.lbj

import android.app.PendingIntent
import android.content.Context
import android.content.BroadcastReceiver
import android.content.Intent
import android.content.IntentFilter
import android.content.SharedPreferences
import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.media.AudioManager
import android.media.ToneGenerator
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.VibrationEffect
import android.os.Vibrator
import android.speech.tts.TextToSpeech
import android.text.InputFilter
import android.text.TextUtils
import android.text.InputType
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import android.view.WindowManager
import android.widget.BaseAdapter
import android.widget.Button
import android.widget.CheckBox
import android.widget.ListView
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.SeekBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.railfan.lbj.mirisdr.MiriSdrDevice
import com.sdrtouch.rtlsdr.BuiltinDriver
import com.sdrtouch.tools.StrRes
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.InetSocketAddress
import java.net.Socket
import java.text.SimpleDateFormat
import java.time.LocalDate
import java.util.Date
import java.util.Locale

class MainActivity : AppCompatActivity() {

    companion object {
        private const val FREQ_MHZ = 821.2375
        private const val DC_OFFSET_HZ = 50000L
        private const val SAMPLE_RATE = 960000
        private const val TCP_PORT = 1234
// 拉起驱动后最多等多久（毫秒）。驱动要先弹 USB 授权框、再建服务，
// 手机上实测通常 2~6 秒；给到 20 秒是给"用户还要在系统弹窗上点一下允许"留时间。
private const val DRIVER_WAIT_MS = 20000L
// 停止后保持连接多久（毫秒）。期间再点【开始接收】可以秒开；
// 超过就彻底断开，免得驱动一直空转耗电。
private const val FULL_STOP_DELAY_MS = 120000L
        private const val PREFS = "lbj"
        private const val DRIVER_PKG = "marto.rtl_tcp_andro"
        private const val DRIVER_FDROID = "https://f-droid.org/packages/marto.rtl_tcp_andro/"
        private const val DRIVER_PLAY = "https://play.google.com/store/apps/details?id=marto.rtl_tcp_andro"
    }

    private val main = Handler(Looper.getMainLooper())
    private var module: PyObject? = null
    private var engine: PyObject? = null
    private var sink: StateSink? = null
    private var busy = false
    private var running = false
    // 后台线程（启动/GPS）用来判断 Activity 是不是已经没了。
    // 首次初始化 Python 要 10~30 秒，用户完全可能中途退出。
    @Volatile private var destroyed = false
    private lateinit var prefs: SharedPreferences

    /**
     * 当前这个引擎是照着哪套【数据源设置】连上的：地址 + 是否走内置驱动。
     *
     * ★ 停止→开始 的"直接恢复"只在设置没变时成立：resume 只是重跑 DSP，
     *   **不会重新建立 TCP 连接**。设置变了还恢复，就会拿着旧连接继续跑 ——
     *   实测现象："关掉台架模式后停止再开始，仍然连着台架服务器"。
     */
    private var engineSource: String? = null

    private lateinit var tvHeader: TextView
    private lateinit var tvWarning: TextView
    private lateinit var tvTrain: TextView
    private lateinit var tvCategory: TextView
    private lateinit var infoGrid: LinearLayout
    private val gridValues = arrayOfNulls<TextView>(16)
    // 标签也要能改：乘车模式下第 3 格（本站）要改成"位置"
    private val gridLabelViews = arrayOfNulls<TextView>(16)
    private lateinit var tvS1: TextView
    private lateinit var tvS2: TextView
    private lateinit var tvS3: TextView
    private lateinit var tvS4: TextView
    // "最近列车"表格：表头 + 若干数据行，每行 6 格。
    // 列名直接写在这里，与信息表格 gridLabels 的做法保持一致。
    private lateinit var logTable: LinearLayout
    private val logRows = ArrayList<Array<TextView>>()
    private val logCols = arrayOf("车次", "方向", "速度", "公里标", "机车", "时间")
    private val logWeights = floatArrayOf(0.85f, 0.62f, 0.62f, 0.95f, 1.55f, 0.95f)

    /**
     * 表格显示几行，按屏幕高度决定（用户要求"依据屏幕大小而定"）。
     * 高屏 16 行，普通屏 10 行 —— 再多的行也会被下面的滚动挤走，反而不如少而清楚。
     */
    private val maxTrainRows: Int by lazy {
        val dp = resources.displayMetrics.heightPixels / resources.displayMetrics.density
        if (dp >= 780f) 16 else 10
    }
    private lateinit var spectrum: SpectrumView
    private lateinit var btnStart: Button
    private lateinit var btnStop: Button
    private lateinit var btnSettings: Button
    private lateinit var btnKeyword: Button
    private lateinit var btnClear: Button
    private lateinit var btnGps: Button
    private lateinit var btnHistory: Button
    private lateinit var chkVoice: CheckBox

    // ---- 列车接收历史 ----
    private var histDays: List<String> = emptyList()
    private var histIdx = 0
    private var histTrips: JSONArray = JSONArray()
    private var histDialog: AlertDialog? = null
    private var histStatus: TextView? = null
    private var histAdapter: HistAdapter? = null
    private var pendingHistFmt = "csv"
    private var pendingHistScope = "all"

    // ------------------------------------------------------------ 收音机
    //
    // 与预警器共用同一根电视棒和同一条 TCP 连接，只是换一套 DSP。
    // 切模式时【只停解码线程、不关连接】（就是预警器那套暂停/恢复机制），
    // 所以来回切不会把驱动搞成"连得上却不推数据"的僵尸状态。
    private lateinit var mainRoot: View
    private lateinit var radioRoot: View
    private lateinit var tvFreq: TextView
    private lateinit var tvMhzUnit: TextView
    private lateinit var freqRow: LinearLayout
    private lateinit var keypad: View
    private lateinit var tvKeypadTip: TextView
    // 自制数字键盘的输入缓冲。输入内容直接显示在频率大字上，所见即所得。
    @Volatile private var freqEditing = false
    private var freqBuf = ""
    private lateinit var tvRadioStatus: TextView
    private lateinit var tvSquelchVal: TextView
    private lateinit var seekSquelch: SeekBar
    private lateinit var seekVolume: SeekBar
    private lateinit var chkSquelch: CheckBox
    private lateinit var spectrumRadio: SpectrumView
    private lateinit var tvChannel: TextView
    private lateinit var tvTopMode: TextView

    // 100 个信道，全部由用户自己命名和修改（不切波段模式）
    private var channels: MutableList<RadioChannel> = mutableListOf()
    private var curChannel = 0
    // 信道模式：调谐跟着信道走，改动会写回信道。
    // 频率模式：随便调，不绑任何信道；从信道模式切过来时频率原样保留。
    @Volatile private var radioVfo = false
    private var radioCtcss = 0.0

    private var radioEngine: PyObject? = null
    private var radioModule: PyObject? = null
    private var audioSink: AudioSink? = null

    @Volatile private var inRadio = false
    @Volatile private var pendingRadio = false
    @Volatile private var radioPolling = false
    private var fgOn = false
    private var radioScan = false
    // 扫描（找频）：面板 + 结果表
    private var scanDialog: AlertDialog? = null
    private var scanStatus: TextView? = null
    private var scanAdapter: ScanAdapter? = null
    private var scanResults: JSONArray = JSONArray()
    private var scanActive = false
    private var scanFine = true
    private var scanMargin = 7.0        // 门限余量（dB，相对底噪）；扫描中也能改
    private var scanModeBtn: Button? = null
    // PPM 校准
    private var calibActive = false
    private var calibDialog: AlertDialog? = null
    private var calibMsg: TextView? = null
    private var autocalActive = false
    // 从扫描结果存信道时，真正的写入值放这里（复用 writeChannel 的覆盖确认流程）
    private var pendingWrite: RadioChannel? = null
    private val radioSteps = doubleArrayOf(100e3, 25e3, 12.5e3, 5e3, 1e3)
    private var radioStepIdx = 2
    private var radioFreqHz = 457_000_000.0
    private var radioMode = "NFM"
    private var lastRssi = -140.0
    private var lastSquelchDb = 12.0       // 高于底噪多少 dB
    private var lastThreshold = -95.0      // 实际生效的门限（底噪 + 上面的值）
    private var lastFloor = -140.0
    private var lastSqlOn = true

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        scanMargin = prefs.getFloat("scan_margin", 7f).toDouble()
        // 内置驱动出错时 SdrException 会去 StrRes 取文案；不设就全是空串（原驱动在
        // RtlSdrApplication.onCreate 里设，本 App 的 Application 是 Chaquopy 的，设不了）。
        StrRes.res = resources

        tvHeader = findViewById(R.id.tvHeader)
        tvWarning = findViewById(R.id.tvWarning)
        tvTrain = findViewById(R.id.tvTrain)
        tvCategory = findViewById(R.id.tvCategory)
        infoGrid = findViewById(R.id.infoGrid)
        buildInfoGrid(infoGrid)
        setCell(15, "0")            // 未启动时"样本"也不要显示成 ---
        tvS1 = findViewById(R.id.tvS1)
        tvS2 = findViewById(R.id.tvS2)
        tvS3 = findViewById(R.id.tvS3)
        tvS4 = findViewById(R.id.tvS4)
        logTable = findViewById(R.id.logTable)
        buildLogTable(logTable)
        spectrum = findViewById(R.id.spectrum)
        btnStart = findViewById(R.id.btnStart)
        btnStop = findViewById(R.id.btnStop)
        btnSettings = findViewById(R.id.btnSettings)
        btnKeyword = findViewById(R.id.btnKeyword)
        btnClear = findViewById(R.id.btnClear)
        btnGps = findViewById(R.id.btnGps)
        btnHistory = findViewById(R.id.btnHistory)
        chkVoice = findViewById(R.id.chkVoice)
        chkVoice.isChecked = prefs.getBoolean("voice", false)
        chkVoice.setOnCheckedChangeListener { _, on -> setVoiceEnabled(on) }
        if (chkVoice.isChecked) setVoiceEnabled(true)      // 上次开着就把它唤起

        mainRoot = findViewById(R.id.mainRoot)
        radioRoot = findViewById(R.id.radioRoot)
        tvFreq = findViewById(R.id.tvFreq)
        tvMhzUnit = findViewById(R.id.tvMhzUnit)
        freqRow = findViewById(R.id.freqRow)
        keypad = findViewById(R.id.keypad)
        tvKeypadTip = findViewById(R.id.tvKeypadTip)
        setupKeypad()
        tvRadioStatus = findViewById(R.id.tvRadioStatus)
        tvSquelchVal = findViewById(R.id.tvSquelchVal)
        seekSquelch = findViewById(R.id.seekSquelch)
        seekVolume = findViewById(R.id.seekVolume)
        chkSquelch = findViewById(R.id.chkSquelch)
        spectrumRadio = findViewById(R.id.spectrumRadio)
        tvChannel = findViewById(R.id.tvChannel)
        tvTopMode = findViewById(R.id.tvTopMode)

        channels = RadioChannel.load(prefs)
        curChannel = prefs.getInt("rch", 0).coerceIn(0, RadioChannel.COUNT - 1)

        findViewById<Button>(R.id.btnRadio).setOnClickListener { enterRadio() }
        findViewById<Button>(R.id.btnBackToLbj).setOnClickListener { leaveRadio() }
        findViewById<Button>(R.id.btnDown).setOnClickListener { radioNudge(-1.0) }
        findViewById<Button>(R.id.btnUp).setOnClickListener { radioNudge(1.0) }
        findViewById<Button>(R.id.btnMode).setOnClickListener { radioCycleMode() }
        findViewById<Button>(R.id.btnCtcss).setOnClickListener { showCtcssDialog() }
        findViewById<Button>(R.id.btnStep).setOnClickListener { radioCycleStep() }
        findViewById<Button>(R.id.btnScan).setOnClickListener { radioToggleScan() }
        findViewById<Button>(R.id.btnVfo).setOnClickListener { toggleVfo() }
        findViewById<Button>(R.id.btnChPrev).setOnClickListener { channelStep(-1) }
        findViewById<Button>(R.id.btnChNext).setOnClickListener { channelStep(1) }
        findViewById<Button>(R.id.btnChList).setOnClickListener { showChannelList() }
        // 频率模式下这颗按钮不是"编辑"（频率模式不允许改信道），
        // 而是"把当前频率写进某个信道"。
        findViewById<Button>(R.id.btnChEdit).setOnClickListener {
            if (radioVfo) saveToChannel() else showChannelEdit(curChannel)
        }


        chkSquelch.setOnCheckedChangeListener { _, b ->
            prefs.edit().putBoolean("rsqon", b).apply()
            lastSqlOn = b
            radioCall2("set_squelch_on", b)
        }
        seekSquelch.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(sb: SeekBar?, p: Int, fromUser: Boolean) {
                val db = squelchFromProgress(p)
                // 格式必须跟别处一致：拖动一下变成光秃秃一个"14"就很难看懂（真机上出现过）
                tvSquelchVal.text = String.format(Locale.US, "+%.0f dB", db)
                lastSquelchDb = db
                if (fromUser) {
                    prefs.edit().putFloat("rsq_margin", db.toFloat()).apply()
                    radioCall2("set_squelch", db)
                }
            }
            override fun onStartTrackingTouch(sb: SeekBar?) { }
            override fun onStopTrackingTouch(sb: SeekBar?) { }
        })
        seekVolume.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(sb: SeekBar?, p: Int, fromUser: Boolean) {
                val v = p / 100.0
                audioSink?.setVolume(v.toFloat())
                if (fromUser) {
                    prefs.edit().putFloat("rvol", v.toFloat()).apply()
                    radioCall2("set_volume", v)
                }
            }
            override fun onStartTrackingTouch(sb: SeekBar?) { }
            override fun onStopTrackingTouch(sb: SeekBar?) { }
        })

        btnStart.setOnClickListener { startEngine() }
        btnStop.setOnClickListener { stopEngine() }
        btnHistory.setOnClickListener { showHistory() }
        btnSettings.setOnClickListener { showSettings() }
        btnKeyword.setOnClickListener { showKeywordDialog() }
        btnClear.setOnClickListener { callAsync("clear_dashboard") { toast("已清屏") } }
        btnGps.setOnClickListener {
            // 乘车模式下"本站"没有意义：我一直在移动，去定位某个固定车站没用。
            // 位置已经跟随本车公里标实时更新了，所以这里直接不做（用户明确要求）。
            if (isRideMode()) {
                toast("乘车模式下不用定位本站：你的位置已跟随本车公里标实时更新")
            } else {
                locateByGps()
            }
        }

        applyLaunchExtras(intent)

        tvHeader.text = "未启动 · 正在检查驱动状态…"
        // 一眼就能看出"驱动到底行不行"，不用点了开始再猜
        refreshDriverStatus()
    }

    /**
     * 处理 adb 注入的启动参数（台架模式 / 服务器地址 / 本站公里标 / 告警距离）。
     *
     * ⚠ 必须同时被 onCreate 和 onNewIntent 调用。
     * manifest 里 Activity 是 singleTop：App 已经在前台时再 am start，
     * 走的是 onNewIntent，onCreate 根本不会执行。之前只写在 onCreate 里，
     * 结果是"App 开着的时候用 adb 注入参数毫无反应，也不报错"，非常难查。
     *
     * ⚠ bench 必须支持显式设成 false。
     * 测试脚本收尾要能把台架模式关回去；只允许设 true 的话，跑完测试
     * 普通用户一打开 App 就会去连测试用的电脑然后"连接被拒"。
     */
    private fun applyLaunchExtras(intent: Intent?) {
        if (intent == null) return
        val before = effectiveHost()
        if (intent.hasExtra("bench")) {
            val on = intent.getBooleanExtra("bench", false)
            prefs.edit().putBoolean("bench", on).apply()
            toast(if (on) "已开启台架模式" else "已关闭台架模式")
        }
        intent.getStringExtra("host")?.let {
            if (it.isNotBlank()) prefs.edit().putString("host", it.trim()).apply()
        }
        // ★ 正在接收时改数据源地址是【不会】重连的（TCP 在 setup() 时就建立了）。
        // 不提示的话，用户以为已经切过去了，实际还在连旧地址 —— 与设置对话框里
        // 那个"运行中改地址却提示已应用"是同一个坑，这里也必须说清楚。
        if (running && effectiveHost() != before) {
            toast("地址已改为 " + effectiveHost() + "，需要【停止】后重新【开始接收】才生效")
        }
        // ★ 运行中通过 adb 改本站公里标，必须【立刻】应用到引擎。
        // 只写 prefs 的话，用户（或测试脚本）以为改了，引擎其实还用着旧值 ——
        // 和"设置对话框在停止态下谎报已应用"是同一类问题。
        // （alarmkm / beep / alarm 不用管：maybeAlarm 每次都实时读 prefs。）
        if (running) {
            val eng = engine
            if (eng != null) {
                Thread {
                    try {
                        val km = prefs.getFloat("mykm", -1f)
                        if (km >= 0f) eng.callAttr("set_my_km", km.toDouble())
                        else eng.callAttr("set_my_km", null)
                    } catch (_: Throwable) { }
                }.start()
            }
        }
        if (intent.hasExtra("mykm")) {
            prefs.edit().putFloat("mykm", intent.getFloatExtra("mykm", -1f)).apply()
        }
        if (intent.hasExtra("alarmkm")) {
            prefs.edit().putFloat("alarmkm", intent.getFloatExtra("alarmkm", 5f)).apply()
        }
        // ★ 插上电视棒时 UsbDelegate 会把我们拉起来并带上这个标记 —— 直接开始接收。
        //   只有用户启用了「使用内置驱动」才自动开始：没开的话数据源是外部驱动 App
        //   （iqsrc:// 那条路），我们不该抢它的活。
        // ★ 台架模式要排除：那条路径连的是电脑上的模拟服务器，插不插电视棒都与它无关，
        //   插棒时去 startEngine 只会白跑一趟（而且用户会以为"插棒把它切走了"）。
        if (intent.getBooleanExtra("autostart", false)
            && prefs.getBoolean("builtin", true)
            && !prefs.getBoolean("bench", false)) {
            main.postDelayed({
                if (!destroyed && !running && !busy) startEngine()
            }, 1000L)
        }
        // ★ 用完就把这些 extra 清掉。
        //   Activity 因内存回收被重建时，系统会拿【原来的 Intent】再走一遍 onCreate；
        //   不清的话，用户在设置里手动关掉的"台架模式"会被那个残留的 --ez bench true
        //   再一次打开 —— 现象就是"我明明关了台架，它自己又变回去了"。
        intent.removeExtra("bench")
        intent.removeExtra("host")
        intent.removeExtra("mykm")
        intent.removeExtra("alarmkm")
        intent.removeExtra("autostart")
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        applyLaunchExtras(intent)
    }

    override fun onResume() {
        super.onResume()
        // 用户可能刚从驱动界面回来，或者刚插好电视棒 ——
        // 这时自动重新检查一次，标题栏上就能立刻看到状态变化。
        refreshDriverStatus()
        // 上次自检如果在 native 里崩了（App 直接闪退），这里把崩溃点告诉用户
        checkMiriProbeTrace()
    }

    override fun onDestroy() {
        super.onDestroy()
        destroyed = true
        // 主线程上绝不做阻塞收尾：Python 的 stop() 内部会 _src.close() → join(2.0)，
        // 再加 DSP 线程 join(1.5)，最长能把主线程卡住约 3.5 秒（足以触发 ANR）。
        // 所以只置标志 + 丢到后台线程。
        main.removeCallbacksAndMessages(null)
        // USB 授权回调跟着 Activity 一起撤，别让它活过界面（否则会抱着旧 Activity 泄漏）
        try { usbPermRx?.let { unregisterReceiver(it) } } catch (_: Throwable) { }
        usbPermRx = null
        // 收音机也要收干净：先停轮询与音频，再把数据源还回去
        stopRadioPoll()
        radioScan = false
        try { audioSink?.stop() } catch (_: Throwable) { }
        audioSink = null
        val re = radioEngine
        radioEngine = null
        val eng0 = engine
        if (re != null && eng0 != null) {
            try { eng0.callAttr("give_source", re.callAttr("yield_source")) } catch (_: Throwable) { }
        } else if (re != null) {
            try { re.callAttr("release_source") } catch (_: Throwable) { }
        }
        val eng = engine
        engine = null
        sink = null
        if (eng != null) {
            Thread {
                // 先解绑推送目标，再停 —— 否则被停掉的引擎还会往已销毁的 Activity 推状态
                try { eng.callAttr("set_push", null) } catch (_: Throwable) { }
                try { eng.callAttr("stop") } catch (_: Throwable) { }
            }.start()
        }
        try { tone?.release(); tone = null } catch (_: Throwable) { }
        // 语音引擎也要关掉，不然它会一直占着（下次进 App 还会接着念没念完的）
        try { tts?.stop() } catch (_: Throwable) { }
        try { tts?.shutdown(); tts = null } catch (_: Throwable) { }
        EngineService.stop(this)      // 引擎都停了，前台服务也要撤掉
        // 内置驱动要【彻底停掉】而不只是解绑：只解绑的话服务照样活着、占着 USB，
        // 退出 App 后电视棒就一直打不开，而通知栏的通知却已经消失。
        BuiltinDriver.stop(this)
    }

    /**
     * 语音播报开关：解出一趟列车就把【车次 / 线路 / 上下行 / 速度 / 公里标 / 距离】念出来。
     * 没解出来的项直接跳过（基础帧里往往还没有机车/线路，硬念"未知"很难听）。
     */
    private fun setVoiceEnabled(on: Boolean) {
        prefs.edit().putBoolean("voice", on).apply()
        if (!on) {
            try { tts?.stop() } catch (_: Throwable) { }
            return
        }
        // 语音引擎要一两秒才就绪，所以第一次打开时才初始化
        if (tts == null) {
            tts = TextToSpeech(this) { st ->
                ttsOk = (st == TextToSpeech.SUCCESS)
                var lang = -1
                if (ttsOk) {
                    // 中文语言包不一定装在这台机器上：按 简体中文 → 中文 → 系统默认 依次试
                    for (loc in arrayOf(Locale.SIMPLIFIED_CHINESE, Locale.CHINESE, Locale.getDefault())) {
                        try {
                            val r = tts?.setLanguage(loc)
                            if (r != TextToSpeech.LANG_MISSING_DATA && r != TextToSpeech.LANG_NOT_SUPPORTED) {
                                lang = 0
                                break
                            }
                            lang = (r ?: -1)
                        } catch (_: Throwable) { }
                    }
                    if (lang != 0) ttsOk = false
                }
                android.util.Log.i("LBJVOICE", "TTS init status=" + st + " lang=" + lang)
                main.post {
                    if (ttsOk) {
                        toast("语音播报已开启")
                    } else {
                        // 引擎在、但缺中文语音包（或语音服务被禁用）：说清楚原因并给一条出路
                        chkVoice.isChecked = false
                        prefs.edit().putBoolean("voice", false).apply()
                        AlertDialog.Builder(this)
                            .setTitle("语音播报打不开")
                            .setMessage("这台手机的语音引擎没有可用的中文语音包。\n\n" +
                                "到系统的「文字转语音」设置里把语音数据下载/启用后，再回来打开这个开关。")
                            .setPositiveButton("去设置") { _, _ ->
                                try {
                                    startActivity(Intent("com.android.settings.TTS_SETTINGS"))
                                } catch (_: Throwable) {
                                    try { startActivity(Intent(android.provider.Settings.ACTION_SETTINGS)) } catch (_: Throwable) { }
                                }
                            }
                            .setNegativeButton("知道了", null)
                            .show()
                    }
                }
            }
            return
        }
        if (ttsOk) toast("语音播报已开启")
    }

    /** 念一句（走媒体通道，和提示音/告警音一致）。 */
    private fun speak(text: String) {
        val t = tts ?: return
        if (!ttsOk) return
        try {
            // ★ 正在念上一条时用 QUEUE_FLUSH：宁可把上一条掐掉，也不要排队积压 ——
            //   车流密的时候（实测模拟源每秒一趟）排队会越念越旧，报的就不是当前这趟车了。
            val mode = if (t.isSpeaking) TextToSpeech.QUEUE_FLUSH else TextToSpeech.QUEUE_ADD
            android.util.Log.i("LBJVOICE", "speak: " + text)
            t.speak(text, mode, null, "lbj" + System.currentTimeMillis())
        } catch (_: Throwable) { }
    }

    /** 这趟车最近是不是已经念过了（同一趟车的两种写法算一趟）。 */
    private fun alreadySaid(tkey: String, train: String, pure: Boolean): Boolean {
        val now = System.currentTimeMillis()
        val it = voiceSaid.iterator()
        while (it.hasNext()) {
            val e = it.next()
            if (now - e.third > VOICE_REPEAT_MS) { it.remove(); continue }
            if (e.first != tkey) continue
            val ePure = e.second.isNotEmpty() && e.second.all { c -> c.isDigit() }
            // 与引擎 _same_train 同规则：字面相同，或数字相同且有一方是纯数字
            if (e.second == train || pure || ePure) return true
        }
        return false
    }

    /** 决定要不要播报，并【延后一点】再念。
     *
     * 为什么要延后：基础帧先到（只有车次/速度/公里标），扩展帧大约 200ms 后到，
     * 补上线路/机车/端位。立刻念的话会念一版缺线路的，然后扩展帧那版又被去重挡掉。
     */
    private fun voiceTrigger(t: JSONObject, train: String) {
        if (!prefs.getBoolean("voice", false)) return
        val tkey = t.optString("tkey", "").ifEmpty { train }
        val pure = t.optBoolean("tpure", train.all { c -> c.isDigit() })
        if (alreadySaid(tkey, train, pure)) return
        voiceSaid.add(Triple(tkey, train, System.currentTimeMillis()))
        main.postDelayed({ voiceSay(tkey) }, 700)
    }

    /** 从最新一帧里取这趟车信息最全的那条，念出来。 */
    private fun voiceSay(tkey: String) {
        if (!prefs.getBoolean("voice", false)) return
        val o = lastSnapshot ?: return
        val trains = o.optJSONArray("trains") ?: return
        var best: JSONObject? = null
        var bestScore = -1
        for (i in 0 until trains.length()) {
            val x = trains.optJSONObject(i) ?: continue
            if (x.optString("tkey", x.optString("train")) != tkey) continue
            var sc = 0
            if (x.optString("route", "").let { it.isNotEmpty() && it != "----" }) sc += 2
            if (x.optString("loco", "").let { it.isNotEmpty() && it != "----" }) sc += 1
            if (sc > bestScore) { best = x; bestScore = sc }
        }
        val t = best ?: return
        announce(t, o)
    }

    /** 组装一句话；缺哪项就少念哪项。 */
    private fun announce(t: JSONObject, o: JSONObject) {
        val train = t.optString("train", "").trim()
        if (train.isEmpty() || train == "----") return
        val parts = ArrayList<String>()
        parts.add(train)
        val route = t.optString("route", "").trim()
        if (route.isNotEmpty() && route != "----") parts.add(route)
        val dir = t.optString("direction", "").trim()
        if (dir.isNotEmpty() && dir != "未知") parts.add(dir)
        val spd = t.optString("speed", "").trim()
        if (spd.isNotEmpty() && spd != "---") parts.add("速度 " + spd + " 公里每小时")
        val km = t.optString("position", "").trim()
        if (km.isNotEmpty() && km != "---.-") parts.add("公里标 " + km)
        if (!o.isNull("eta_distance_km")) {
            val d = o.optDouble("eta_distance_km", -1.0)
            if (d >= 0.0) parts.add("距离 " + String.format(Locale.US, "%.1f", d) + " 公里")
        }
        speak(parts.joinToString("，"))
    }
    // ---------------------------------------------------------- 告警音 / 语音播报
    private var tone: ToneGenerator? = null
    private var lastTrainTs = 0.0
    private var lastRateWarn = ""
    // RSP1 类设备（Mirics MSi2500/MSi001）：本机自带的 rtl_tcp 服务由它提供
    private var miriDevice: MiriSdrDevice? = null
    private var miriConn: UsbDeviceConnection? = null
    // 自检时用到：pendingRsp1Dev = 刚点过的那台（可能是识别表外的新型号，授权后接着用它）
    private var pendingRsp1Dev: UsbDevice? = null
    private var usbPermRx: BroadcastReceiver? = null
    private var miriTraceChecked = false
    private var tts: TextToSpeech? = null
    private var ttsOk = false
    // 同一趟车不要反复念：LBJ 每隔几秒就重发一次，车次 -> 上次播报时刻
    // 已播报过的：(归并键, 车次原串, 时刻)。用归并键是为了别把同一趟车念两遍
    // —— 基础帧只有数字（'57'），扩展帧才带字母（'Z57'），实测就是念了两遍。
    private val voiceSaid = ArrayList<Triple<String, String, Long>>()
    private val VOICE_REPEAT_MS = 10 * 60 * 1000L
    private var lastSnapshot: JSONObject? = null

    // 用【媒体通道】而不是【闹钟通道】：
    // 闹钟通道在很多机型上音量下限不为 0、还与系统闹钟绑定，用户根本关不掉；
    // 媒体通道可以把音量拉到 0，配合下面的两个开关就能做到彻底静音。
    private fun beep(urgent: Boolean) {
        try {
            if (tone == null) tone = ToneGenerator(AudioManager.STREAM_MUSIC, 90)
            tone?.startTone(
                if (urgent) ToneGenerator.TONE_CDMA_ALERT_CALL_GUARD else ToneGenerator.TONE_PROP_BEEP,
                if (urgent) 900 else 120
            )
        } catch (_: Throwable) { }
    }

    private fun stopSound() {
        try { tone?.stopTone() } catch (_: Throwable) { }
    }

    private fun vibrate(ms: Long) {
        try {
            val v = getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                v.vibrate(VibrationEffect.createOneShot(ms, VibrationEffect.DEFAULT_AMPLITUDE))
            } else {
                @Suppress("DEPRECATION") v.vibrate(ms)
            }
        } catch (_: Throwable) { }
    }

    // ------------------------------------------------------ 信息表格
    // 固定列宽：每行都是"标签|数值|标签|数值"四格，权重固定，
    // 所以标签和数值的横向位置在每一行都一致，不会因为数字位数变化而左右跳动。
    private val gridLabels = arrayOf(
        "到达:", "时刻:", "本站:", "距离:", "状态:", "方向:",
        "速度:", "公里:", "机车:", "代号:", "线路:", "类别:",
        "端位:", "经度:", "纬度:", "样本:"
    )

    // 每行显示哪两个标签（存的是 gridLabels 的下标）。
    //
    // ★ 不再"顺序两两配对"：经度和纬度是一组坐标，原来被拆到
    //   「端位|经度」和「纬度|样本」两行，一个在第 2 列一个在第 1 列，
    //   既不同行也不同列，看着很别扭（用户直接指出）。
    //   现在把经度/纬度固定放同一行，端位/样本配一行。
    private val gridRows = intArrayOf(
        0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15, 13, 14
    )

    private fun buildInfoGrid(container: LinearLayout) {
        // 明确按"每次前进 2 个、且保证还能取到下一个"来遍历。
        // 之前写成 while (r < size) + gridRows[r + k] 是错的：
        // size 为奇数（或 r 恰好等于 size）时 r + k 会越过数组末尾，
        // 实测直接 ArrayIndexOutOfBoundsException 崩在 onCreate —— App 一启动就挂。
        var r = 0
        while (r + 1 < gridRows.size) {
            val row = LinearLayout(this)
            row.orientation = LinearLayout.HORIZONTAL
            for (k in 0 until 2) {
                val i = gridRows[r + k]
                val lab = TextView(this).apply {
                    text = gridLabels[i]
                    typeface = Typeface.MONOSPACE
                    textSize = 13f
                    maxLines = 1
                    setTextColor(0xFF8B949E.toInt())
                    layoutParams = LinearLayout.LayoutParams(
                        0, LinearLayout.LayoutParams.WRAP_CONTENT, 0.95f)
                }
                val val_ = TextView(this).apply {
                    text = "---"
                    typeface = Typeface.MONOSPACE
                    textSize = 13f
                    maxLines = 1
                    ellipsize = TextUtils.TruncateAt.END
                    setTextColor(0xFFE6EDF3.toInt())
                    layoutParams = LinearLayout.LayoutParams(
                        0, LinearLayout.LayoutParams.WRAP_CONTENT, 1.55f)
                }
                gridValues[i] = val_
                gridLabelViews[i] = lab
                row.addView(lab)
                row.addView(val_)
            }
            r += 2
            container.addView(row)
        }
    }

    private fun setCell(i: Int, v: String) {
        if (i in gridValues.indices) gridValues[i]?.text = v
    }

    /**
     * 建"最近列车"表格：一行表头 + maxTrainRows 行数据。
     *
     * 视图只建这一次，之后每帧只改文字、不重建 —— 每秒推送三次，
     * 每次都重建上百个 TextView 会明显卡顿。
     */
    private fun buildLogTable(container: LinearLayout) {
        val head = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        for (i in logCols.indices) {
            head.addView(TextView(this).apply {
                text = logCols[i]
                textSize = 11f
                maxLines = 1
                setTextColor(0xFF8B949E.toInt())        // 表头用暗色，和数据行区分开
                layoutParams = LinearLayout.LayoutParams(
                    0, LinearLayout.LayoutParams.WRAP_CONTENT, logWeights[i])
            })
        }
        container.addView(head)

        for (r in 0 until maxTrainRows) {
            val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
            val cells = ArrayList<TextView>(logCols.size)
            for (i in logCols.indices) {
                val cell = TextView(this).apply {
                    text = ""
                    typeface = Typeface.MONOSPACE     // 数字等宽，看着更整齐
                    textSize = 11f
                    maxLines = 1
                    ellipsize = TextUtils.TruncateAt.END
                    setTextColor(0xFFE6EDF3.toInt())
                    layoutParams = LinearLayout.LayoutParams(
                        0, LinearLayout.LayoutParams.WRAP_CONTENT, logWeights[i])
                }
                cells.add(cell)
                row.addView(cell)
            }
            logRows.add(cells.toTypedArray())
            container.addView(row)
        }
    }

    /** 把引擎推来的车次列表填进表格；多余的行清空 */
    private fun renderTrains(trains: JSONArray?) {
        val n = minOf(trains?.length() ?: 0, logRows.size)
        for (r in logRows.indices) {
            val cells = logRows[r]
            val t = if (r < n) trains!!.optJSONObject(r) else null
            if (t == null) {
                for (c in cells) c.text = ""
                continue
            }
            cells[0].text = t.optString("train")
            cells[1].text = t.optString("direction")
            cells[2].text = t.optString("speed")
            cells[3].text = t.optString("position")
            cells[4].text = t.optString("loco")
            cells[5].text = t.optString("time")
            // 乘车模式：自己坐的那趟用蓝色标出来，一眼能和路过的车分开
            val color = if (t.optBoolean("muted", false)) 0xFF58A6FF.toInt()
                        else 0xFFE6EDF3.toInt()
            for (c in cells) c.setTextColor(color)
        }
    }

    // ------------------------------------------------------ GPS 定位本站
    private fun locateByGps() {
        // ★ 精确或大致位置【任一】授权即可。
        // Android 12+ 用户可以只给"大致位置"（COARSE，FINE 被拒）。
        // 只检查 FINE 的话，这类用户每次点都会再 requestPermissions，
        // 而系统已经记过答案、不再弹框 —— 于是永久静默失败，用户只看到"没反应"。
        // locate_by_gps_json 本来就容忍 5 km 误差，COARSE 完全够用。
        val fineOk = ContextCompat.checkSelfPermission(
            this, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
        val coarseOk = ContextCompat.checkSelfPermission(
            this, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED
        if (!fineOk && !coarseOk) {
            ActivityCompat.requestPermissions(this,
                arrayOf(Manifest.permission.ACCESS_FINE_LOCATION,
                        Manifest.permission.ACCESS_COARSE_LOCATION), 1001)
            return
        }
        if (!running) {
            toast("请先开始接收；需要先收到列车报文才能建立线路里程样本")
            return
        }
        val eng = engine ?: run { toast("请先开始接收"); return }
        val lm = getSystemService(Context.LOCATION_SERVICE) as LocationManager
        toast("正在获取当前定位…（最多 20 秒）")
        // ★ 一律【重新请求】当前位置，绝不读系统缓存。
        //   getLastKnownLocation 给的是缓存 —— 实测拿到过 3.7 小时前的坐标，
        //   用它算出来的"离线 X 公里"完全是假的（用户正是被这个坑过）。
        //   宁可多等几秒、或者明确失败，也不用一个过期位置糊弄过去。
        requestFreshLocation(lm, 20000L) { loc, _ ->
            if (loc == null) {
                // 用对话框而不是 toast：等了 20 秒只闪一下提示，用户很可能根本没看见。
                if (isFinishing || isDestroyed) return@requestFreshLocation
                val locOn = try {
                    lm.isProviderEnabled(LocationManager.GPS_PROVIDER) ||
                        lm.isProviderEnabled(LocationManager.NETWORK_PROVIDER)
                } catch (_: Throwable) { false }
                AlertDialog.Builder(this).setTitle("没能拿到当前定位")
                    .setMessage("App 已经请求了一次实时定位，但 20 秒内没有拿到。\n\n" +
                        "当前系统定位开关：" + (if (locOn) "已开启" else "已关闭") + "\n\n" +
                        "请检查：\n" +
                        "① 是否在室外或靠窗 —— GPS 需要能看到天空\n" +
                        "② 系统的「位置信息」开关是否打开\n" +
                        "③ 本应用的位置权限是否允许\n\n" +
                        "刚打开定位的话，稍等十几秒再点一次通常会更快。")
                    .setPositiveButton("好", null).show()
            } else {
                runLocate(eng, loc)
            }
        }
    }

    /**
     * 请求一次【当前】定位。超时或 provider 都不可用就返回 null —— **不回退缓存**。
     * @param done 回调 (位置, 是否为刚获取的当前位置)；位置为 null 表示这次没拿到。
     */
    private fun requestFreshLocation(
        lm: LocationManager, timeoutMs: Long, done: (Location?, Boolean) -> Unit
    ) {
        val called = java.util.concurrent.atomic.AtomicBoolean(false)
        val ui = Handler(Looper.getMainLooper())
        var listener: LocationListener? = null

        fun finish(loc: Location?, fresh: Boolean) {
            if (!called.compareAndSet(false, true)) return
            listener?.let { l -> try { lm.removeUpdates(l) } catch (_: Throwable) { } }
            ui.removeCallbacksAndMessages(null)
            done(loc, fresh)
        }

        // 超时 -> 直接失败。不回退缓存：旧坐标算出来的结论是误导。
        ui.postDelayed({ finish(null, false) }, timeoutMs)

        listener = object : LocationListener {
            override fun onLocationChanged(location: Location) { finish(location, true) }
            override fun onProviderEnabled(provider: String) { }
            override fun onProviderDisabled(provider: String) { }
            @Deprecated("Deprecated in Java")
            override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) { }
        }

        // ★ 关键：必须把【所有】可用的 provider 都请一遍，谁先回来用谁。
        //   以前这里是 for + break —— 只请了第一个不抛异常的 provider（通常是 GPS）。
        //   于是室内收不到卫星就一直等到超时；而高德能定位，正是因为它同时用了网络定位。
        val providers = listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)
            .filter { p -> try { lm.isProviderEnabled(p) } catch (_: Throwable) { false } }

        if (providers.isEmpty()) {
            ui.removeCallbacksAndMessages(null)
            finish(null, false)
            return
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            // API 30+ 用 getCurrentLocation：官方推荐的"要一次当前位置"，
            // 比已废弃的 requestSingleUpdate 可靠（老 API 在新系统上偶发不回调）。
            val exec = ContextCompat.getMainExecutor(this)
            for (p in providers) {
                try {
                    lm.getCurrentLocation(p, null, exec) { loc ->
                        if (loc != null) finish(loc, true)
                    }
                } catch (_: Throwable) { }
            }
        } else {
            listener = object : LocationListener {
                override fun onLocationChanged(location: Location) { finish(location, true) }
                override fun onProviderEnabled(provider: String) { }
                override fun onProviderDisabled(provider: String) { }
                @Deprecated("Deprecated in Java")
                override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) { }
            }
            for (p in providers) {
                try {
                    @Suppress("DEPRECATION")
                    lm.requestSingleUpdate(p, listener, Looper.getMainLooper())
                } catch (_: Throwable) { }
            }
        }
    }

    /** 拿到位置后反推公里标，并把"这次到底用了哪个位置"一并摆给用户看 */
    private fun runLocate(eng: PyObject, loc: Location) {
        val lat = loc.latitude
        val lon = loc.longitude
        val accM = loc.accuracy
        // ★ 这段自检信息是关键：用户以前只看到"离线路 28.2 公里"，
        //   根本不知道那是不是一个过期坐标。现在来源/时间/精度都摆出来，
        //   结果可不可信一眼就能判断。
        val where = String.format(Locale.US,
            "本次用的是【刚获取的实时定位】：\n坐标：%.5f, %.5f\n定位时间：%s\n定位精度：约 %.0f 米\n",
            lat, lon, SimpleDateFormat("HH:mm:ss", Locale.US).format(Date(loc.time)), accM)
        Thread {
            try {
                val r = eng.callAttr("locate_by_gps_json", lat, lon).toString()
                val o = JSONObject(r)
                main.post {
                    // 弹窗需要有效的 window token。Activity 已销毁还 show() 会抛
                    // BadTokenException，表现为"点了 GPS 再退出就闪退"。
                    if (isFinishing || isDestroyed) return@post
                    if (!o.optBoolean("ok", false)) {
                        val sm = o.optJSONObject("samples")
                        val cnt = if (sm == null) 0 else {
                            var n = 0
                            for (k in sm.keys()) n += sm.optInt(k, 0)
                            n
                        }
                        AlertDialog.Builder(this).setTitle("还定位不了")
                            .setMessage(o.optString("reason") +
                                "\n\n" + where +
                                "\n当前线路样本数：" + cnt +
                                "\n\n请让 App 多收几趟车（每趟车都会带来一组" +
                                "公里标+经纬度" + "），样本够了再点一次。")
                            .setPositiveButton("好", null).show()
                    } else {
                        val km = o.optDouble("km", -1.0)
                        prefs.edit().putFloat("mykm", km.toFloat()).apply()
                        Thread { try { eng.callAttr("set_my_km", km) } catch (_: Throwable) { } }.start()
                        AlertDialog.Builder(this).setTitle("已按 GPS 定位本站")
                            .setMessage(String.format(Locale.US,
                                "线路：%s\n本站公里标：%.1f km\n（离最近的线路折线约 %d 米，用了 %d 个样本）\n\n%s\n已自动写入【全局本站公里标】",
                                o.optString("route"), km, o.optInt("dist_m", 0), o.optInt("samples", 0), where))
                            .setPositiveButton("好", null).show()
                    }
                }
            } catch (t: Throwable) {
                main.post { toast("定位失败：" + (t.message ?: "")) }
            }
        }.start()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != 1001) return
        val granted = grantResults.isNotEmpty() &&
                      grantResults.any { it == PackageManager.PERMISSION_GRANTED }
        if (granted) {
            locateByGps()
        } else {
            // 之前被拒绝时什么都不做，用户点完只看到"没反应"，完全不知道为什么。
            toast("没有位置权限，无法定位本站。可在系统设置里为本应用开启位置权限后重试")
        }
    }

    /** 每解出一趟新车次响一次；正在接近且距离够近则用急促告警音 */
    private fun maybeAlarm(o: JSONObject) {
        val trains = o.optJSONArray("trains") ?: return
        if (trains.length() == 0) return
        // ★ 乘车模式：自己坐的那趟车会一直在列表里更新，必须跳过它。
        //   不跳的话两件事都会出问题：
        //     ① 本车每收一次就响一声提示音（几秒一次，根本没法用）；
        //     ② 本车常年占着 trains[0]，别的车来了也会被它顶下去，
        //        结果"路过的车"反而不响 —— 与乘车模式的初衷正好相反。
        var t: JSONObject? = null
        for (i in 0 until trains.length()) {
            val x = trains.optJSONObject(i) ?: continue
            if (!x.optBoolean("muted", false)) { t = x; break }
        }
        t ?: return                                  // 列表里全是本车 → 不响
        val ts = t.optDouble("ts", 0.0)
        if (ts <= lastTrainTs) return          // 同一条不重复响
        lastTrainTs = ts

        val status = o.optString("eta_status", "")
        val dist = if (o.isNull("eta_distance_km")) Double.MAX_VALUE
                   else o.optDouble("eta_distance_km", Double.MAX_VALUE)
        val limit = prefs.getFloat("alarmkm", 5f).toDouble()
        val near = (status == "即将到达") || (status == "接近" && dist <= limit)

        // 语音播报：与提示音同一个触发点（每解出一趟新车念一次；本车已在上面跳过）
        voiceTrigger(t, t.optString("train", ""))

        if (near) {
            // 接近告警：独立开关，关掉就完全静音
            if (prefs.getBoolean("alarm", true)) {
                beep(true)
                vibrate(600)
            }
        } else {
            // 普通提示音：另一个独立开关
            if (prefs.getBoolean("beep", true)) beep(false)
        }
    }

    // -------------------------------------------------------------- 驱动
    private fun findDriver(): String? {
        try {
            val probe = Intent(Intent.ACTION_VIEW, Uri.parse("iqsrc://"))
            val list = packageManager.queryIntentActivities(probe, 0)
            if (list.isNotEmpty()) return list[0].activityInfo.packageName
        } catch (_: Throwable) { }
        return try {
            packageManager.getPackageInfo(DRIVER_PKG, 0)
            DRIVER_PKG
        } catch (_: Throwable) { null }
    }

    // ---------------------------------------------------- 驱动可用性检查
    //
    // 背景：原来 launchDriver() 只要 startActivity 不抛异常就返回 true，
    // 于是"驱动界面亮了但服务器根本没起来"（没插电视棒、没点 USB 授权、
    // 驱动自己报错）这三种情况一律被当成成功，用户之后只看到一句
    // "连接被拒"，完全不知道问题出在哪。下面这几个函数用来把话说清楚。

    // ---------------------------------------------------------------
    //  关于"怎么知道驱动在不在跑"——这里踩过两个坑，都写下来免得再犯：
    //
    //  坑 1：用"连一下再断开"探测端口。
    //        rtl_tcp_andro 是【一次只服务一个客户端，客户端一走就把整个服务停掉】的设计，
    //        我们的探测一连上就关闭，驱动立刻报 Broken pipe 然后 "TCP server shutting down"
    //        自我了断 —— 想确认它就绪，反而把它弄死。（见 docs/33）
    //
    //  坑 2：改成读 /proc/net/tcp 的监听表。
    //        本地测试通过，但 Android 10+ 对这个文件有访问限制，
    //        App 看不到【别的 UID】的 socket —— 实测：adb 能看到 1234 在 LISTEN，
    //        而 App 里这个函数永远返回 false。（见 docs/35）
    //
    //  坑 3：用 UsbManager.deviceList 判断"电视棒插没插"。
    //        它只返回【本 App 已获授权】的设备。USB 授权在驱动 App 手里，
    //        我们从来没申请过，所以永远拿到 0 个 —— 于是误判"没插棒"而拒绝启动。
    //
    //  ★ 结论：这几种"先探测再决定"的写法全都不可靠。
    //    正确做法是【直接去连】：连得上 = 驱动本来就在跑，直接复用；
    //    连不上（连接被拒，在驱动侧不留任何痕迹） = 才去拉起驱动。
    //    判断"连没连上"由 Python 侧的数据源负责（见 lbj_engine.connect()）。
    // ---------------------------------------------------------------

    /** 把本 App 拉回前台。驱动被拉起后用户会停在驱动界面，看不到接收情况。 */
    private fun bringSelfToFront() {
        try {
            startActivity(Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT))
        } catch (_: Throwable) { }
    }

    /** 未启动时，在标题栏里直接显示"驱动到底行不行"，不用等点了开始再猜 */
    private fun refreshDriverStatus() {
        Thread {
            val bench = prefs.getBoolean("bench", false)
            val builtin = prefs.getBoolean("builtin", true)
            // 内置驱动模式没必要（也不该）去看外部驱动 App 装没装
            val installed = if (bench || builtin) false else findDriver() != null
            main.post {
                if (destroyed || running || busy) return@post
                // 只显示【可靠检测得到】的信息。
                // "USB 插没插"和"驱动在不在跑"都检测不了（原因见上面那段注释），
                // 硬显示只会误导 —— 之前就因此误报"未检测到设备"而拒绝启动。
                val sb = StringBuilder()
                when {
                    bench -> sb.append("台架模式 → " + effectiveHost() + ":1234")
                    builtin -> sb.append("内置驱动模式（本机 127.0.0.1:1234）")
                    else -> sb.append("本机驱动模式")
                }
                if (!bench) {
                    if (builtin) {
                        sb.append("\n使用 App 内置的 rtl_tcp，无需外部驱动 App")
                    } else {
                        sb.append("\n驱动App:").append(if (installed) "已安装✓" else "未安装❌")
                    }
                }
                sb.append("\n点【开始接收】：驱动已在跑就直接用，没跑会自动拉起")
                tvHeader.text = sb.toString()
            }
        }.start()
    }

    private fun showDriverFailedDialog() {
        AlertDialog.Builder(this)
            .setTitle("驱动没有就绪")
            .setMessage("已经尝试打开 RTL-SDR 驱动 App，但过了 " + (DRIVER_WAIT_MS / 1000) +
                " 秒手机上的 1234 端口仍然没有开始监听。\n\n" +
                "请依次检查：\n" +
                "① 电视棒是否插到底（OTG 转接头最容易接触不良，建议先直插试试）\n" +
                "② OTG 转接头是否支持数据传输（有些只供电）\n" +
                "③ 首次插上时系统会问是否允许访问 USB 设备 —— 必须点【允许】\n" +
                "④ 打开 RTL-SDR 驱动 App 看看它的提示：\n" +
                "   若显示「found 0 device opening options」，就是电视棒没插好或没被识别；\n" +
                "   若显示「权限被拒绝」，则是 USB 授权的问题。\n\n" +
                "确认后回到本 App 再点一次【开始接收】。")
            .setPositiveButton("知道了", null)
            .setNeutralButton("重试") { _, _ -> startEngine() }
            .show()
    }

    /** 内置驱动没起来时的提示：措辞与外部驱动那条区分开，对症检查 */
    private fun showBuiltinFailedDialog() {
        val why = BuiltinDriver.lastError
        AlertDialog.Builder(this)
            .setTitle("内置驱动没有就绪")
            .setMessage(
                (if (why != null) "错误信息：" + why + "\n\n" else "") +
                "已经在本 App 内启动了内置 rtl_tcp 驱动，但过了 " + (DRIVER_WAIT_MS / 1000) +
                " 秒手机上的 1234 端口仍然没有开始监听。\n\n" +
                "请依次检查：\n" +
                "① 电视棒是否插到底（OTG 转接头最容易接触不良，建议先直插试试）\n" +
                "② 首次插上时系统会问是否允许访问 USB 设备 —— 必须点【允许】\n" +
                "③ 电视棒有没有被【外部驱动 App】占着：内置和外部驱动抢同一个 USB 设备，\n" +
                "   请先关掉 RTL-SDR 驱动 App 再试\n" +
                "④ 若错误信息是「加载 librtlSdrAndroid.so 失败」，说明这台手机的\n" +
                "   CPU 架构不受支持\n\n" +
                "确认后回到本 App 再点一次【开始接收】。")
            .setPositiveButton("知道了", null)
            .setNeutralButton("重试") { _, _ -> startEngine() }
            .show()
    }

    /**
     * 硬件频率 = 用户兆赫 × 1e6 − DC 避让 50 kHz。
     *
     * 必须用【用户设置的】频率。用编译期常量的话，用户改过频率后
     * 驱动仍被按默认频率拉起，引擎随后再改也来不及（而且改完重启又丢）。
     * 内置驱动和外部 iqsrc:// 两条路都用它，保证频率口径只有一处。
     */
    private fun hwFreqHz(): Long {
        val userMhz = prefs.getFloat("freq", FREQ_MHZ.toFloat()).toDouble()
        return (userMhz * 1_000_000.0).toLong() - DC_OFFSET_HZ
    }

    /**
     * 内置驱动：本 App 自己起 rtl_tcp 服务并绑上，不用装外部驱动 App。
     * 真正成没成，仍以随后对 127.0.0.1:1234 的那次 connect 为准。
     */
    private fun startBuiltinDriver(hwFreq: Long): Boolean {
        return BuiltinDriver.start(this, hwFreq, TCP_PORT, SAMPLE_RATE)
    }

    /** @return true 表示成功把驱动拉起来了 */
    private fun launchDriver(): Boolean {
        val hwFreq = hwFreqHz()
        val uri = "iqsrc://-a 127.0.0.1 -p $TCP_PORT -s $SAMPLE_RATE -f $hwFreq -T 0"
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse(uri))
        findDriver()?.let { intent.setPackage(it) }
        // ★ 必须带 CLEAR_TASK。
        //
        // 驱动的界面如果还留在任务栈里，只发 NEW_TASK 会走 onNewIntent —— 实测那条路
        // 不能把 USB 流恢复正常：TCP 连得上、数据只推几百 KB 就彻底沉默，
        // 界面于是显示"连不上本机驱动"，而驱动进程其实活得好好的。
        // 带上 CLEAR_TASK 会重建 Activity，走的是 onNewIntent 之外的完整初始化。
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
        return try { startActivity(intent); true } catch (_: Throwable) { false }
    }

    private fun showDriverDialog() {
        AlertDialog.Builder(this)
            .setTitle(R.string.need_driver_title)
            .setMessage(R.string.need_driver_msg)
            .setPositiveButton(R.string.go_fdroid) { _, _ -> openUrl(DRIVER_FDROID) }
            .setNeutralButton(R.string.go_play) { _, _ -> openUrl(DRIVER_PLAY) }
            .setNegativeButton(R.string.later, null)
            .show()
    }

    // ============================================================ 收音机
    //
    // 静噪滑条是 0~100，映射成"比底噪高多少 dB"（0 ~ 25 dB）。
    // 不用绝对门限：噪声底和真信号只差十来 dB，绝对门限分不开，
    // 结果就是纯噪声也一直"有声"，喇叭嘶嘶响个不停。
    private fun squelchFromProgress(p: Int): Double = p * 0.4
    private fun progressFromSquelch(db: Double): Int = (db / 0.4).toInt().coerceIn(0, 100)

    /** 后台安全调用收音机引擎的方法（绝不在主线程调 Python） */
    private fun radioCall2(name: String, arg: Any) {
        val re = radioEngine ?: return
        Thread {
            try { re.callAttr(name, arg) } catch (_: Throwable) { }
        }.start()
    }

    /**
     * 进入收音机。
     *
     * 没有现成连接时，先借用预警器那套【已验证】的启动流程把驱动连起来，
     * 连上之后再切过去（见 startEngine 末尾对 pendingRadio 的处理）。
     * 这样驱动拉起/重试/报错的逻辑只有一份，不用再写一遍。
     */
    private fun enterRadio() {
        if (inRadio || busy) return
        ensureNotiPermission()
        val eng = engine
        val connected = if (eng == null) false
                        else try { eng.callAttr("is_connected").toBoolean() } catch (_: Throwable) { false }
        if (eng == null || !connected) {
            pendingRadio = true
            startEngine()
            return
        }
        switchToRadio(eng)
    }

    private fun leaveRadio() {
        if (!inRadio) return
        switchToLbj()
    }

    private fun switchToRadio(eng: PyObject) {
        busy = true
        updateButtons()
        tvHeader.text = getString(R.string.radio_starting)
        Thread {
            var src: PyObject? = null
            var sink: AudioSink? = null
            val err: String?
            try {
                val s = AudioSink(this)
                if (!s.start()) throw RuntimeException("音频设备打不开（可能被别的应用占着）")
                sink = s
                val mod = radioModule ?: Python.getInstance().getModule("lbj_radio").also { radioModule = it }
                val re = mod.callAttr("RadioEngine", s)
                src = eng.callAttr("take_source")
                if (src == null) throw RuntimeException("没有可用的数据源")
                re.callAttr("adopt", src)
                // 参数来自【当前信道】（信道里存着名称/频率/制式/亚音/步进）；
                // 如果上次停在频率模式，则频率用频率模式自己记的那个。
                channels = RadioChannel.load(prefs)
                curChannel = prefs.getInt("rch", 0).coerceIn(0, RadioChannel.COUNT - 1)
                radioVfo = prefs.getBoolean("rvfo", false)
                val ch0 = channels[curChannel]
                if (radioVfo) {
                    loadVfoState(ch0)
                } else {
                    radioMode = ch0.mode
                    radioStepIdx = nearestStepIndex(ch0.stepHz)
                    radioCtcss = ch0.ctcss
                    radioFreqHz = ch0.freqHz
                }
                // 注意 key 带 _margin：旧版本的 rsq 是"绝对 dB 门限"，语义完全变了，
                // 直接沿用会把 -95 读成"比底噪低 95dB"，静噪就永远关不上了。
                lastSquelchDb = prefs.getFloat("rsq_margin", 12f).toDouble()
                lastSqlOn = prefs.getBoolean("rsqon", true)
                re.callAttr("set_mode", radioMode)
                re.callAttr("set_frequency", radioFreqHz)
                re.callAttr("set_step", radioSteps[radioStepIdx])
                re.callAttr("set_ctcss", radioCtcss)
                re.callAttr("set_volume", prefs.getFloat("rvol", 0.8f).toDouble())
                re.callAttr("set_squelch", lastSquelchDb)
                re.callAttr("set_squelch_on", lastSqlOn)
                re.callAttr("set_gain", prefs.getFloat("gain", 19.7f).toDouble())
                re.callAttr("set_ppm", prefs.getInt("ppm", 0))
                if (!re.callAttr("start").toBoolean()) {
                    throw RuntimeException(re.callAttr("error").toString())
                }
                main.post {
                    radioEngine = re
                    audioSink = s
                    inRadio = true
                    busy = false
                    mainRoot.visibility = View.GONE
                    radioRoot.visibility = View.VISIBLE
                    updateButtons()
                    // 信道栏按当前模式初始化（布局里那句只是占位文字）
                    updateChannelBar()
                    applyRadioUi()
                    startRadioPoll()
                    tvHeader.text = "收音机接收中（没有重连驱动）"
                }
                return@Thread
            } catch (t: Throwable) {
                err = t.message ?: t.toString()
            }
            // 失败：一定要把数据源还回预警器，否则连接就丢在这儿了
            try { if (src != null) eng.callAttr("give_source", src) } catch (_: Throwable) { }
            main.post {
                sink?.stop()
                busy = false
                updateButtons()
                radioRoot.visibility = View.GONE
                mainRoot.visibility = View.VISIBLE
                running = true
                tvHeader.text = "收音机启动失败：" + err
            }
        }.start()
    }

    /** 切回预警器：收音机把连接还回来，预警器接着解码 */
    private fun switchToLbj() {
        busy = true
        updateButtons()
        radioScan = false
        val re = radioEngine
        val eng = engine
        // 记下收音机的设置，下次进来还是这个台
        if (radioVfo) saveVfoState()
        syncChannelFromCurrent()
        prefs.edit()
            .putFloat("rsq_margin", lastSquelchDb.toFloat())
            .putBoolean("rsqon", lastSqlOn)
            .putBoolean("rvfo", radioVfo)
            .putFloat("rfreq_vfo", (radioFreqHz / 1e6).toFloat())
            .apply()
        Thread {
            var src: PyObject? = null
            try { src = re?.callAttr("yield_source") } catch (_: Throwable) { }
            main.post {
                stopRadioPoll()
                audioSink?.stop()
                audioSink = null
                radioEngine = null
                inRadio = false
                busy = false
                radioRoot.visibility = View.GONE
                mainRoot.visibility = View.VISIBLE
                updateButtons()
                tvHeader.text = "已切回接近器"
            }
            if (eng != null && src != null) {
                val ok = try { eng.callAttr("give_source", src).toBoolean() } catch (_: Throwable) { false }
                main.post {
                    running = ok
                    updateButtons()
                    if (!ok) tvHeader.text = "连接已失效，请重新点【开始接收】"
                }
            }
        }.start()
    }

    // ---------------------------------------------------------- 轮询刷新
    private fun startRadioPoll() {
        if (radioPolling) return
        radioPolling = true
        Thread {
            while (radioPolling) {
                val re = radioEngine
                if (re != null) {
                    try {
                        val o = JSONObject(re.callAttr("snapshot_json").toString())
                        main.post { renderRadio(o) }
                    } catch (_: Throwable) { }
                }
                try { Thread.sleep(250) } catch (_: InterruptedException) { break }
            }
        }.start()
    }

    private fun stopRadioPoll() { radioPolling = false }

    private fun applyRadioUi() {
        val o = try {
            JSONObject(radioEngine?.callAttr("snapshot_json")?.toString() ?: "{}")
        } catch (_: Throwable) { JSONObject() }
        lastSquelchDb = o.optDouble("squelch_db", lastSquelchDb)
        lastSqlOn = o.optBoolean("squelch_on", lastSqlOn)
        lastRssi = o.optDouble("rssi", -140.0)
        seekSquelch.progress = progressFromSquelch(lastSquelchDb)
        tvSquelchVal.text = String.format(Locale.US, "+%.0f dB", lastSquelchDb)
        chkSquelch.isChecked = lastSqlOn
        seekVolume.progress = (prefs.getFloat("rvol", 0.8f) * 100).toInt().coerceIn(0, 100)
        audioSink?.setVolume(prefs.getFloat("rvol", 0.8f))
        updateRadioButtons()
    }

    /** 扫描面板：进度 + 结果表（结果一到就刷新） */
    private fun renderScan(sc: JSONObject?) {
        if (sc == null) return
        val res = sc.optJSONArray("results") ?: JSONArray()
        val act = sc.optBoolean("active", false)
        if (act) {
            val ph = when (sc.optString("phase", "")) {
                "verify" -> "复核信号"
                "window", "discard", "measure" -> "扫描中"
                else -> "准备"
            }
            val fTxt = if (sc.isNull("floor")) "—" else String.format(Locale.US, "%.0f", sc.optDouble("floor"))
            val tTxt = if (sc.isNull("thr")) "—" else String.format(Locale.US, "%.0f", sc.optDouble("thr"))
            val mTxt = if (sc.isNull("margin")) ""
                        else String.format(Locale.US, "（底噪+%.0f）", sc.optDouble("margin"))
            scanStatus?.text = String.format(
                Locale.US, "%s（第 %d 趟）   %s MHz\n%s\n底噪 %s dB   门限 %s dB%s   已找到 %d 个",
                ph, sc.optInt("pass", 0) + 1, fmtMhz(sc.optDouble("cur_hz", radioFreqHz)),
                scanRangeText(sc), fTxt, tTxt, mTxt, res.length()
            )
        } else if (scanActive) {
            // 引擎那边刚结束
            scanActive = false
            radioScan = false
            updateRadioButtons()
            val pb = scanDialog?.getButton(AlertDialog.BUTTON_POSITIVE)
            if (pb != null) {
                pb.text = "再扫一趟"
                pb.setOnClickListener { scanDialog?.dismiss(); showScanOptionsDialog() }
            }
            scanStatus?.text = String.format(
                Locale.US, "扫描已停止：共找到 %d 个信号（已停在最强那个上）\n%s\n点下面任意一行可存成信道。",
                res.length(), scanRangeText(sc)
            )
            if (res.length() == 0) toast("这片范围里没扫到信号，把门限调小点再试")
        } else {
            // 看的是"上次结果"（扫描没在跑）：别留着"准备中…"
            scanStatus?.text = String.format(
                Locale.US, "上次扫描结果：%d 个信号\n%s\n点一行 = 设为当前频率，长按 = 存信道",
                res.length(), scanRangeText(sc)
            )
        }
        // 按内容比，不按长度比：扫描中调门限会【删掉】几行，长度可能碰巧不变
        if (res.toString() != scanResults.toString()) {
            scanResults = res
            scanAdapter?.notifyDataSetChanged()
            updateRadioButtons()          // 主界面上那颗按钮要显示"扫描结果(N)"
        }
    }

    private fun updateRadioButtons() {
        findViewById<Button>(R.id.btnVfo).text = getString(R.string.switch_mode)
        // 信道模式：频率/制式/亚音/步进都跟着信道走，这些一律不许在这里改
        // （用户明确要求：只能通过下面的"编辑"或进信道列表改）
        val tune = radioVfo
        findViewById<Button>(R.id.btnDown).isEnabled = tune
        findViewById<Button>(R.id.btnUp).isEnabled = tune
        findViewById<Button>(R.id.btnStep).isEnabled = tune
        findViewById<Button>(R.id.btnMode).isEnabled = tune
        findViewById<Button>(R.id.btnCtcss).isEnabled = tune
        findViewById<Button>(R.id.btnScan).isEnabled = tune
        // 频率模式下信道相关按钮不生效（避免误改信道预设）
        findViewById<Button>(R.id.btnChPrev).isEnabled = !radioVfo
        findViewById<Button>(R.id.btnChNext).isEnabled = !radioVfo
        findViewById<Button>(R.id.btnChList).isEnabled = !radioVfo
        findViewById<Button>(R.id.btnCtcss).text = "亚音: " + RadioChannel.ctcssLabel(radioCtcss)
        findViewById<Button>(R.id.btnMode).text = "制式: $radioMode"
        // 频率模式：这颗按钮换成"存到信道"（编辑信道只在信道模式做）
        findViewById<Button>(R.id.btnChEdit).text =
            getString(if (radioVfo) R.string.ch_write else R.string.ch_edit)
        // 只写数值，不写"步进"两个字：长了会折行，把这一排撑高导致错位
        findViewById<Button>(R.id.btnStep).text =
            String.format(Locale.US, "%.4g kHz", radioSteps[radioStepIdx] / 1000.0)
        // ★ 扫描那颗按钮是【回到结果列表】的入口，不是停止键：
        //   停止在列表窗里点（用户反馈：关掉列表窗后回不去了）。
        findViewById<Button>(R.id.btnScan).text = when {
            radioScan -> String.format(Locale.US, "扫描结果(%d)", scanResults.length())
            scanResults.length() > 0 -> String.format(Locale.US, "上次结果(%d)", scanResults.length())
            else -> "扫描"
        }
        // 扫描期间禁掉会跟扫描抢调谐的那几颗按钮
        for (id in arrayOf(R.id.btnDown, R.id.btnUp, R.id.btnStep, R.id.btnMode, R.id.btnCtcss)) {
            findViewById<Button>(id).isEnabled = tune && !radioScan
        }
        syncKeypadVisibility()
        syncForeground()
    }

    private fun renderRadio(o: JSONObject) {
        if (!inRadio) return
        val hz = o.optDouble("freq", radioFreqHz)
        if (Math.abs(hz - radioFreqHz) > 1.0) {
            // ★ 引擎那边的频率变了就一定要把信道栏跟上。
            //   不然会出现"频率显示 438.5150，信道栏还写着 457.0000"这种自相矛盾的画面
            //   （真机上就是这么暴露的：进收音机时没初始化信道栏，留的是布局里的默认文字）。
            radioFreqHz = hz
            // ★ 扫描期间引擎会不停改频（复核命中点），这不是用户在调谐 ——
            //   不挡住的话会把当前信道的频率悄悄改掉。
            if (channels.isNotEmpty() && !radioVfo && !radioScan) {
                val c = channels[curChannel]
                if (Math.abs(c.freqHz - hz) > 1.0) {
                    c.freqHz = hz
                    RadioChannel.save(prefs, channels)
                }
            }
            updateChannelBar()
        }
        radioMode = o.optString("mode", radioMode)
        lastRssi = o.optDouble("rssi", -140.0)
        lastSquelchDb = o.optDouble("squelch_db", lastSquelchDb)
        lastThreshold = o.optDouble("threshold", lastThreshold)
        lastFloor = o.optDouble("floor", lastFloor)
        lastSqlOn = o.optBoolean("squelch_on", lastSqlOn)
        setFreqText(radioFreqHz)
        val st = when {
            !lastSqlOn -> "静噪已关"
            o.optBoolean("open", false) -> "有声"
            else -> "静噪中"
        }
        tvRadioStatus.text = String.format(
            Locale.US, "%s   RSSI %.0f   门限 %.0f   %s",
            radioMode, lastRssi, lastThreshold, st
        )
        val arr = o.optJSONArray("spectrum")
        if (arr != null && arr.length() > 0) {
            val v = FloatArray(arr.length())
            for (i in 0 until arr.length()) v[i] = arr.optDouble(i, -120.0).toFloat()
            spectrumRadio.update(v, -1f, 0.5f)
        }

        // ★ 扫描面板必须在这里刷新：轮询调的是 renderRadio()，
        //   applyRadioUi() 只在进收音机时调一次 —— 放错地方的后果就是
        //   面板一直卡在"准备中…"、结果表空白（真机上就是这么暴露的）。
        renderScan(o.optJSONObject("scan"))
        renderCalib(o.optJSONObject("calib"))
        renderAutocal(o.optJSONObject("autocal"))
    }

    // ---------------------------------------------------------- 调谐操作
    private fun radioNudge(dir: Double) {
        val step = radioSteps[radioStepIdx]
        var f = radioFreqHz + dir * step
        if (f < 1e6) f = 1e6
        if (f > 1e9) f = 1e9
        radioFreqHz = f
        setFreqText(f)
        radioCall2("set_frequency", f)
        syncChannelFromCurrent()
    }

    private fun radioCycleMode() {
        val order = listOf("NFM", "AM", "WFM")
        val i = order.indexOf(radioMode)
        radioMode = order[(i + 1) % order.size]
        if (radioMode == "WFM") radioStepIdx = 0
        else if (radioMode == "AM") radioStepIdx = 1
        else if (radioSteps[radioStepIdx] > 25e3) radioStepIdx = 2
        updateRadioButtons()
        radioCall2("set_mode", radioMode)
        radioCall2("set_step", radioSteps[radioStepIdx])
        radioCall2("set_frequency", radioFreqHz)
        setFreqText(radioFreqHz)
        syncChannelFromCurrent()
    }

    private fun radioCycleStep() {
        radioStepIdx = (radioStepIdx + 1) % radioSteps.size
        updateRadioButtons()
        radioCall2("set_step", radioSteps[radioStepIdx])
        syncChannelFromCurrent()
        saveVfoState()
    }

    // ---------------------------------------------------------- 信道
    //
    // 不做"航空/铁路/对讲"这类模式切换 —— 每个信道自己带着
    // 名称 / 频率 / 制式 / 亚音 / 步进，用户想怎么命名、怎么改都行。
    // 调频率、改制式都会同步回当前信道，听到什么信道里就是什么。

    private fun nearestStepIndex(hz: Double): Int {
        var best = 0
        var bd = Double.MAX_VALUE
        for (i in radioSteps.indices) {
            val d = Math.abs(radioSteps[i] - hz)
            if (d < bd) { bd = d; best = i }
        }
        return best
    }

    private fun channelStep(d: Int) {
        if (radioVfo) return
        applyChannel((curChannel + d + RadioChannel.COUNT) % RadioChannel.COUNT)
    }

    private fun updateChannelBar() {
        if (channels.isEmpty()) { tvChannel.text = ""; return }
        if (radioVfo) {
            // 频率模式用黄色标出来，一眼就能看出现在不受信道约束
            tvTopMode.text = getString(R.string.mode_freq)
            tvTopMode.setTextColor(getColor(R.color.warn))
            tvChannel.text = String.format(
                Locale.US, "%s   %s%s",
                getString(R.string.vfo_free), radioMode,
                if (radioCtcss > 0) "   亚音 " + RadioChannel.ctcssLabel(radioCtcss) else ""
            )
        } else {
            tvTopMode.text = getString(R.string.mode_channel) + " · " +
                RadioChannel.label(channels[curChannel], curChannel)
            tvTopMode.setTextColor(getColor(R.color.accent))
            tvChannel.text = RadioChannel.summary(channels[curChannel], curChannel)
        }
    }

    /**
     * 信道模式 <-> 频率模式。
     *
     * 切到频率模式时【频率保留】—— 就是信道模式下已经调到的那个频率继续用，
     * 只是从此不再写回任何信道。
     */
    private fun toggleVfo() {
        if (channels.isEmpty()) channels = RadioChannel.load(prefs)
        // 离开频率模式前先把它这一套设置记下来（下次切回来要用）
        if (radioVfo) saveVfoState()
        radioVfo = !radioVfo
        prefs.edit().putBoolean("rvfo", radioVfo).apply()
        if (radioVfo) {
            // ★ 回到频率模式：用它【自己】上次的频率/制式/亚音/步进，
            //   不沿用刚才信道模式下的那套（用户明确要求）
            loadVfoState(channels[curChannel])
            pushTuning()
        } else {
            // 回信道模式：套用当前信道的频率/制式/亚音/步进
            applyChannel(curChannel)
        }
        updateChannelBar()
        updateRadioButtons()
    }

    /** 切到某个信道：刷新界面，并把设置推给引擎 */
    private fun applyChannel(idx: Int) {
        if (channels.isEmpty()) channels = RadioChannel.load(prefs)
        curChannel = idx.coerceIn(0, RadioChannel.COUNT - 1)
        val c = channels[curChannel]
        radioFreqHz = c.freqHz
        radioMode = c.mode
        radioCtcss = c.ctcss
        radioStepIdx = nearestStepIndex(c.stepHz)
        prefs.edit().putInt("rch", curChannel).apply()
        setFreqText(radioFreqHz)
        updateChannelBar()
        updateRadioButtons()
        if (radioEngine != null) {
            radioCall2("set_mode", c.mode)
            radioCall2("set_step", c.stepHz)
            radioCall2("set_frequency", c.freqHz)
            radioCall2("set_ctcss", c.ctcss)
        }
    }

    /** 把当前听到的频率/制式/步进写回当前信道 */
    private fun syncChannelFromCurrent() {
        if (channels.isEmpty()) return
        if (radioVfo) {
            // 频率模式下随便改：只记到频率模式自己那份，绝不写回信道
            // —— 否则会把用户的信道预设改乱
            saveVfoState()
            updateChannelBar()
            return
        }
        val c = channels[curChannel]
        c.freqHz = radioFreqHz
        c.mode = radioMode
        c.stepHz = radioSteps[radioStepIdx]
        RadioChannel.save(prefs, channels)
        updateChannelBar()
    }

    private var chDialog: AlertDialog? = null

    /**
     * 信道列表。
     *
     * ★ 信道名/频率/制式/亚音一律【在这里改】：
     *   点一下 = 切到这个信道，长按 = 编辑这个信道。
     *   主界面不再直接改信道内容，避免两处都能改、谁也不清楚改了哪一份。
     */
    private fun showChannelList() {
        if (channels.isEmpty()) channels = RadioChannel.load(prefs)
        val list = ListView(this)
        list.adapter = ChAdapter()
        list.setOnItemClickListener { _, _, pos, _ ->
            applyChannel(pos)
            chDialog?.dismiss()
        }
        list.setOnItemLongClickListener { _, _, pos, _ ->
            chDialog?.dismiss()
            showChannelEdit(pos)
            true
        }
        chDialog = AlertDialog.Builder(this)
            .setTitle("信道列表（共 100 个 · 点=切换，长按=编辑）")
            .setView(list)
            .setPositiveButton("编辑当前信道") { _, _ -> showChannelEdit(curChannel) }
            .setNegativeButton("关闭", null)
            .create()
        chDialog?.show()
    }

    /** 信道列表的适配器：编号 + 名称 + 频率/制式/亚音 */
    private inner class ChAdapter : BaseAdapter() {
        override fun getCount(): Int = RadioChannel.COUNT
        override fun getItem(position: Int): Any = position
        override fun getItemId(position: Int): Long = position.toLong()

        override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
            val v = convertView ?: layoutInflater.inflate(R.layout.view_ch_row, parent, false)
            val c = channels[position]
            val active = (!radioVfo && position == curChannel)
            v.findViewById<TextView>(R.id.chIndex).text =
                String.format(Locale.US, "%02d", position + 1)
            v.findViewById<TextView>(R.id.chIndex)
                .setTextColor(getColor(if (active) R.color.accent else R.color.dim))
            v.findViewById<TextView>(R.id.chName).text = RadioChannel.label(c, position)
            v.findViewById<TextView>(R.id.chName)
                .setTextColor(getColor(if (active) R.color.accent else R.color.text))
            v.findViewById<TextView>(R.id.chInfo).text = String.format(
                Locale.US, "%.4f MHz   %s%s", c.freqHz / 1e6, c.mode,
                if (c.ctcss > 0) "   亚音 " + RadioChannel.ctcssLabel(c.ctcss) else ""
            )
            return v
        }
    }

    /**
     * 频率模式专用：把当前频率/制式/亚音/步进写进指定的信道。
     *
     * ★ 直接给下拉列表让用户挑，不用手打信道号 ——
     *   列表里带着信道名和频率，挑的时候一眼就知道哪个是哪个。
     *
     * 空信道直接写；已经有名字的信道先确认再覆盖，免得手滑把辛苦命名的频道冲掉。
     */
    private fun saveToChannel() {
        pendingWrite = null
        if (channels.isEmpty()) channels = RadioChannel.load(prefs)
        AlertDialog.Builder(this)
            .setTitle(
                String.format(
                    Locale.US, "存到信道 · 当前 %.4f MHz  %s%s",
                    radioFreqHz / 1e6, radioMode,
                    if (radioCtcss > 0) "  亚音 " + RadioChannel.ctcssLabel(radioCtcss) else ""
                )
            )
            .setAdapter(ChAdapter()) { _, which -> writeChannel(which) }
            .setNegativeButton(R.string.ch_cancel, null)
            .show()
    }

    private fun writeChannel(idx: Int) {
        val c = channels[idx]
        if (c.name.isBlank()) {
            doWriteChannel(idx)          // 空信道：直接写
            return
        }
        AlertDialog.Builder(this)
            .setTitle("覆盖信道？")
            .setMessage(
                String.format(
                    Locale.US, "CH%02d「%s」已有内容：\n%.4f MHz   %s\n\n确定用当前频率覆盖吗？",
                    idx + 1, c.name, c.freqHz / 1e6, c.mode
                )
            )
            .setPositiveButton("覆盖") { _, _ -> doWriteChannel(idx) }
            .setNegativeButton(R.string.ch_cancel, null)
            .show()
    }

    private fun doWriteChannel(idx: Int) {
        val old = channels[idx]
        val src = pendingWrite            // 来自扫描结果时用它，否则用当前收听值
        pendingWrite = null
        channels[idx] = RadioChannel(
            name = old.name,
            freqHz = src?.freqHz ?: radioFreqHz,
            mode = src?.mode ?: radioMode,
            stepHz = src?.stepHz ?: radioSteps[radioStepIdx],
            ctcss = src?.ctcss ?: radioCtcss
        )
        RadioChannel.save(prefs, channels)
        toast(
            String.format(
                Locale.US, "已写入 CH%02d   %.4f MHz",
                idx + 1, radioFreqHz / 1e6
            )
        )
    }

    private fun showChannelEdit(idx: Int) {
        val src = channels[idx]
        val c = RadioChannel(src.name, src.freqHz, src.mode, src.stepHz, src.ctcss)
        val pad = (resources.displayMetrics.density * 16).toInt()
        val col = LinearLayout(this)
        col.orientation = LinearLayout.VERTICAL
        col.setPadding(pad, pad / 2, pad, 0)

        fun mkLabel(t: String): TextView {
            val v = TextView(this)
            v.text = t
            v.setTextColor(getColor(R.color.dim))
            v.textSize = 12f
            return v
        }

        val edName = EditText(this)
        edName.setText(c.name)
        edName.hint = "例如：调机 1（留空则显示 CH 编号）"
        val edFreq = EditText(this)
        edFreq.inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
        edFreq.setText(String.format(Locale.US, "%.4f", c.freqHz / 1e6))

        val btnMode = Button(this)
        val btnCtcss = Button(this)

        for (b in arrayOf(btnMode, btnCtcss)) b.isAllCaps = false
        // 不提供"步进"了：信道模式下加减按钮是禁用的，信道里存步进已经没有意义，
        // 步进统一由主界面上加减中间那颗按钮管。
        fun refresh() {
            btnMode.text = "制式：" + c.mode
            btnCtcss.text = "亚音：" + RadioChannel.ctcssLabel(c.ctcss)
        }
        btnMode.setOnClickListener {
            val order = listOf("NFM", "AM", "WFM")
            c.mode = order[(order.indexOf(c.mode) + 1) % order.size]
            refresh()
        }
        btnCtcss.setOnClickListener {
            val items = Array(RadioChannel.CTCSS_TONES.size) {
                RadioChannel.ctcssLabel(RadioChannel.CTCSS_TONES[it])
            }
            AlertDialog.Builder(this)
                .setTitle("模拟亚音 CTCSS（Hz）")
                .setItems(items) { _, w -> c.ctcss = RadioChannel.CTCSS_TONES[w]; refresh() }
                .show()
        }
        refresh()

        col.addView(mkLabel(getString(R.string.ch_name)))
        col.addView(edName)
        col.addView(mkLabel(getString(R.string.ch_freq)))
        col.addView(edFreq)
        col.addView(mkLabel("制式 / 亚音"))
        col.addView(btnMode)
        col.addView(btnCtcss)

        AlertDialog.Builder(this)
            .setTitle(
                getString(R.string.ch_title) + "  CH" +
                    String.format(Locale.US, "%02d", idx + 1)
            )
            .setView(col)
            .setPositiveButton(R.string.ch_ok) { _, _ ->
                val mhz = edFreq.text.toString().trim().toDoubleOrNull()
                if (mhz == null || mhz < 1.0 || mhz > 1700.0) {
                    toast("频率要在 1 ~ 1700 MHz 之间")
                } else {
                    channels[idx] = RadioChannel(
                        edName.text.toString().trim(), mhz * 1e6, c.mode, c.stepHz, c.ctcss
                    )
                    RadioChannel.save(prefs, channels)
                    if (idx == curChannel) applyChannel(idx)
                    toast("已保存到 " + RadioChannel.label(channels[idx], idx))
                }
            }
            .setNegativeButton(R.string.ch_cancel, null)
            .show()
    }

    private var radioScanRunnable: Runnable? = null

    /**
     * 扫描（找频）。
     *
     * 用途：不停扫这个范围，把有信号的频点都记进一张表，点一行就能存成信道。
     * ★ 它【不自己停】—— 只有用户点【停止扫描】才结束（一趟扫完接着下一趟）。
     * 本机是纯接收，不参与通话。
     *
     * ★ 扫描在 Python 引擎里跑（FFT 一次算一个窗口的 64 个格子），
     *   界面只负责显示进度和结果表 —— 绝不能像旧版那样每 700ms 调一次 set_frequency，
     *   那样一格就要 300ms 静音，10 MHz 得扫十几分钟。
     */
    private fun radioToggleScan() {
        // 扫描中、或者手上有上一轮结果 -> 先把列表调出来（不重扫、不停止）
        if (scanActive || scanResults.length() > 0) {
            showScanDialog()
            return
        }
        showScanOptionsDialog()
    }

    /** 扫描选项（范围/门限/自动微调）—— 只有从"开始"这条路进来。 */
    private fun showScanOptionsDialog() {
        val pad = (resources.displayMetrics.density * 20).toInt()
        val col = LinearLayout(this)
        col.orientation = LinearLayout.VERTICAL
        col.setPadding(pad, pad / 2, pad, 0)
        val cFine = check(col, "用峰值自动微调频率", prefs.getBoolean("scan_fine", true))
        // 门限：判定"这里有没有信号"的分界 = 底噪 + 这个余量。
        // 调大只留强台（滤掉弱信号），调小一个不漏；扫描中还能再改（点结果窗里的「门限」）。
        val eMargin = numField(
            col, "门限：高出底噪多少 dB 才算信号（调大滤掉弱信号）",
            String.format(Locale.US, "%.0f", prefs.getFloat("scan_margin", 7f))
        )
        val tip = TextView(this)
        tip.text = String.format(
            Locale.US,
            "范围：以当前 %s MHz 为中心，上下各 %d MHz（共 %d MHz）\n" +
                "扫描：一直扫，扫到的都往表里加，点【停止扫描】才结束。\n" +
                "制式：按外面设的制式复核（窗口里那颗制式键可以直接换）。\n" +
                "门限：默认 7 dB。底噪 -55 就是 -48 dB 以上才算信号；嫌杂音多就填 12、15。",
            fmtMhz(radioFreqHz), 10, 20
        )
        tip.setTextColor(getColor(R.color.dim))
        tip.setPadding(0, pad / 2, 0, 0)
        col.addView(tip)
        AlertDialog.Builder(this)
            .setTitle("扫描（找本范围内有信号的频点）")
            .setView(col)
            .setPositiveButton("开始") { _, _ ->
                // 空着或写乱了就退回默认 7 dB，不让一个笔误把扫描卡住
                val mg = eMargin.text.toString().trim().toFloatOrNull() ?: 7f
                prefs.edit().putBoolean("scan_fine", cFine.isChecked)
                    .putFloat("scan_margin", mg).apply()
                startScan(cFine.isChecked, mg.toDouble())
            }
            // PPM 校准单独一个入口：它不扫描，只是"停下来量一下载波偏了多少"
            .setNeutralButton("PPM 校准") { _, _ -> showCalibChooser() }
            .setNegativeButton(R.string.ch_cancel, null)
            .show()
    }

    /**
     * PPM 校准：量当前这个台的**载波频偏**。
     *
     * 用鉴频器的直流，不用频谱峰值 —— FM 广播的 19kHz 导频/38kHz 副载波会把
     * 峰值和质心整体拉高约 +2kHz（95.9 读成 95.9023 就是这么来的），
     * 而音频没有直流分量，所以鉴频直流只反映载波偏了多少。实测精度 ±15Hz。
     */
    /** PPM 校准的两个入口：自动（自己找广播台）/ 手动（用当前正在听的台） */
    private fun showCalibChooser() {
        AlertDialog.Builder(this)
            .setTitle("PPM 校准")
            .setItems(arrayOf(
                "自动：自己找本地 FM 广播台",
                "手动：用当前正在听的台"
            )) { _, w ->
                if (w == 0) startAutocal() else startCalib()
            }
            .setNegativeButton(R.string.ch_cancel, null)
            .show()
    }

    /**
     * 自动 PPM 校准：扫 FM 段找最强的台 → 吸附到 100kHz 栅格 → 量载波频偏
     * → 应用 → 复测。全程自己走完，只看结果。
     *
     * FC0013 这类便宜棒晶振普遍偏得多，所以这个功能是"新装就该点一下"的东西。
     */
    private fun startAutocal() {
        val re = radioEngine ?: return
        Thread {
            try {
                re.callAttr("start_autocalib")
            } catch (t: Throwable) {
                main.post { toast("启动失败：" + (t.message ?: "")) }
                return@Thread
            }
            main.post {
                autocalActive = true
                val tv = TextView(this)
                val pad = (resources.displayMetrics.density * 20).toInt()
                tv.setPadding(pad, pad, pad, pad)
                tv.text = "正在自动校准…\n先扫 FM 段找广播台"
                calibMsg = tv
                calibDialog?.dismiss()
                calibDialog = AlertDialog.Builder(this)
                    .setTitle("自动 PPM 校准")
                    .setView(tv)
                    .setNegativeButton("取消") { _, _ ->
                        Thread { try { re.callAttr("stop_autocalib") } catch (_: Throwable) { } }.start()
                        autocalActive = false
                    }
                    .create()
                calibDialog?.show()
            }
        }.start()
    }

    private fun renderAutocal(c: JSONObject?) {
        if (c == null || !autocalActive) return
        if (!c.optBoolean("done", false)) {
            calibMsg?.text = String.format(
                Locale.US, "%s\n%.4f MHz   完成 %.0f%%",
                c.optString("msg", "校准中…"),
                (if (c.optDouble("scan_hz", 0.0) > 0) c.optDouble("scan_hz") else c.optDouble("freq", radioFreqHz)) / 1e6,
                c.optDouble("progress", 0.0) * 100
            )
            return
        }
        autocalActive = false
        calibDialog?.dismiss()
        if (c.isNull("ppm_suggest")) {
            AlertDialog.Builder(this)
                .setTitle("自动 PPM 校准")
                .setMessage(c.optString("msg", "没成功"))
                .setPositiveButton("知道了", null).show()
            return
        }
        val sug = c.optInt("ppm_suggest", 0)
        prefs.edit().putInt("ppm", sug).apply()      // 引擎那边已经应用，这里落盘
        AlertDialog.Builder(this)
            .setTitle("自动 PPM 校准完成")
            .setMessage(String.format(
                Locale.US,
                "校准台：%.4f MHz（FM 广播，锁 GPS）\n\n" +
                    "应用前载波偏 %+.0f Hz\n已应用 ppm = %d\n复测残差 %+.0f Hz\n\n" +
                    "残差接近 0 就说明准了；这个值只跟这根棒有关，换位置/换电脑都一样。",
                c.optDouble("freq", radioFreqHz) / 1e6,
                c.optDouble("meas_hz", 0.0), sug, c.optDouble("resid_hz", 0.0)
            ))
            .setPositiveButton("好", null)
            .show()
    }

    private fun startCalib() {
        val re = radioEngine ?: return
        if (radioMode == "AM") {
            toast("AM 没有鉴频器，切到 NFM / WFM 再测")
            return
        }
        Thread {
            try {
                re.callAttr("start_calib", 4.0)
            } catch (t: Throwable) {
                main.post { toast("启动失败：" + (t.message ?: "")) }
                return@Thread
            }
            main.post { calibActive = true; showCalibProgress() }
        }.start()
    }

    private fun showCalibProgress() {
        val tv = TextView(this)
        val pad = (resources.displayMetrics.density * 20).toInt()
        tv.setPadding(pad, pad, pad, pad)
        tv.text = String.format(Locale.US, "正在测载波频偏…\n%.4f MHz   %s", radioFreqHz / 1e6, radioMode)
        calibMsg = tv
        calibDialog = AlertDialog.Builder(this)
            .setTitle("PPM 校准")
            .setView(tv)
            .setNegativeButton("取消", null)
            .create()
        calibDialog?.show()
    }

    private fun renderCalib(c: JSONObject?) {
        if (c == null || !calibActive) return
        if (!c.optBoolean("done", false)) {
            calibMsg?.text = String.format(
                Locale.US, "正在测载波频偏…\n%.4f MHz   %s\n完成 %.0f%%",
                c.optDouble("freq", radioFreqHz) / 1e6, radioMode, c.optDouble("progress", 0.0) * 100
            )
            return
        }
        calibActive = false
        calibDialog?.dismiss()
        val off = c.optDouble("offset_hz", 0.0)
        val sug = c.optInt("ppm_suggest", 0)
        val now = c.optInt("ppm_now", 0)
        val rssi = c.optDouble("rssi", -140.0)
        AlertDialog.Builder(this)
            .setTitle(String.format(Locale.US, "PPM 校准 · %.4f MHz", c.optDouble("freq", radioFreqHz) / 1e6))
            .setMessage(String.format(
                Locale.US,
                "载波偏 %+.0f Hz（信号 %.0f dB，当前 ppm=%d）\n\n建议填 %d\n\n" +
                    "前提：这个台正好在它标称的频率上（FM 广播一般锁 GPS，是准的）。\n" +
                    "应用后再测一次，应该接近 0 Hz。",
                off, rssi, now, sug
            ))
            .setPositiveButton("应用") { _, _ ->
                prefs.edit().putInt("ppm", sug).apply()
                radioCall2("set_ppm", sug)
                toast(String.format(Locale.US, "PPM 已设为 %d", sug))
            }
            .setNegativeButton("关闭", null)
            .show()
    }

    private fun startScan(fine: Boolean, marginDb: Double = 7.0) {
        val re = radioEngine ?: return
        scanFine = fine
        scanMargin = marginDb
        scanResults = JSONArray()
        scanActive = true
        radioScan = true
        updateRadioButtons()
        showScanDialog()
        Thread {
            try {
                // 第 4 个参数是扫描速率（null = 引擎自己挑最快的）
                re.callAttr("start_scan", 20e6, fine, marginDb, null)
            } catch (t: Throwable) {
                main.post { toast("扫描启动失败：" + (t.message ?: "")) }
            }
        }.start()
    }


    private fun stopScan(targetHz: Double? = null) {
        val re = radioEngine
        Thread {
            // 带频率 = 停下时直接停在它上面（引擎默认会停在最强信号上，会盖掉用户的选择）
            try {
                if (targetHz != null) re?.callAttr("stop_scan", targetHz)
                else re?.callAttr("stop_scan")
            } catch (_: Throwable) { }
        }.start()
        scanActive = false
        radioScan = false
        radioScanRunnable?.let { main.removeCallbacks(it) }
        updateRadioButtons()
    }

    /**
     * 扫描中改门限。
     *
     * 引擎会顺手把已经扫到的结果按新门限过一遍 —— 调高门限就是想滤掉弱信号，
     * 让它们留在表里没有意义。所以这里只传一个数，不用重扫。
     */
    private fun showScanMarginDialog() {
        val pad = (resources.displayMetrics.density * 16).toInt()
        val col = LinearLayout(this)
        col.orientation = LinearLayout.VERTICAL
        col.setPadding(pad, pad / 2, pad, 0)
        val e = numField(
            col, "门限：高出底噪多少 dB 才算信号（调大滤掉弱信号）",
            String.format(Locale.US, "%.0f", scanMargin)
        )
        AlertDialog.Builder(this)
            .setTitle("扫描门限")
            .setView(col)
            .setPositiveButton("应用") { _, _ ->
                val v = e.text.toString().trim().toFloatOrNull() ?: 7f
                setScanMargin(v.toDouble())
            }
            .setNegativeButton(R.string.ch_cancel, null)
            .show()
    }

    private fun setScanMargin(v: Double) {
        val re = radioEngine ?: return
        scanMargin = v
        prefs.edit().putFloat("scan_margin", v.toFloat()).apply()
        Thread {
            try { re.callAttr("set_scan_margin", v) } catch (_: Throwable) { }
        }.start()
    }

    private fun showScanDialog() {
        val pad = (resources.displayMetrics.density * 16).toInt()
        val col = LinearLayout(this)
        col.orientation = LinearLayout.VERTICAL
        col.setPadding(pad, pad / 2, pad, 0)
        val tv = TextView(this)
        tv.text = "准备中…"
        col.addView(tv)
        scanStatus = tv
        // 制式 + 清空 一行。制式：复核用的就是外面这个制式，扫的过程中换了立刻按新制式听。
        val bar = LinearLayout(this)
        bar.orientation = LinearLayout.HORIZONTAL
        val bm = Button(this)
        bm.isAllCaps = false
        bm.text = "制式：" + radioMode
        bm.setOnClickListener {
            radioCycleMode()
            bm.text = "制式：" + radioMode
        }
        bar.addView(bm, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 2f))
        val bc = Button(this)
        bc.isAllCaps = false
        bc.text = "清空"
        bc.setOnClickListener { confirmClearScan() }
        bar.addView(bc, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        col.addView(bar)
        scanModeBtn = bm
        val lv = ListView(this)
        scanAdapter = ScanAdapter()
        lv.adapter = scanAdapter
        // 点一下 = 问要不要把这个频率设为当前收听频率；长按 = 存成信道
        lv.setOnItemClickListener { _, _, i, _ -> askSetScanFreq(i) }
        lv.setOnItemLongClickListener { _, _, i, _ -> saveScanResult(i); true }
        col.addView(lv, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            (resources.displayMetrics.density * 260).toInt()
        ))
        scanDialog = AlertDialog.Builder(this)
            .setTitle("扫描结果（点=设为当前频率，长按=存信道）")
            .setView(col)
            .setPositiveButton(if (scanActive) "停止扫描" else "再扫一趟") { _, _ ->
                if (scanActive) stopScan() else showScanOptionsDialog()
            }
            // ★ 「门限」必须是 null 监听 + 自己换监听：
            //   neutral 的默认行为会在回调之后【自动关掉这个窗】，而扫描还在跑，
            //   用户就再也回不到列表了（真机踩过）。换成自己的监听就不关窗。
            .setNeutralButton("门限", null)
            .setNegativeButton("关闭", null)
            .create()
        scanDialog?.setOnShowListener {
            scanDialog?.getButton(AlertDialog.BUTTON_NEUTRAL)?.setOnClickListener {
                showScanMarginDialog()
            }
        }
        scanDialog?.show()
    }

    /** 结果表的一行：序号 + 频率 + 制式/强度（复用信道行的布局） */
    private inner class ScanAdapter : BaseAdapter() {
        override fun getCount(): Int = scanResults.length()
        override fun getItem(position: Int): Any = position
        override fun getItemId(position: Int): Long = position.toLong()
        override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
            val v = convertView ?: layoutInflater.inflate(R.layout.view_ch_row, parent, false)
            val r = scanResults.optJSONObject(position) ?: JSONObject()
            val f = r.optDouble("freq", 0.0)
            val cur = r.optBoolean("cur", false)
            v.findViewById<TextView>(R.id.chIndex).text = String.format(Locale.US, "%02d", position + 1)
            v.findViewById<TextView>(R.id.chName).text =
                fmtMhz(f) + " MHz" + (if (cur) "  （当前收听）" else "")
            val ovl = r.optBoolean("ovl", false)
            val nobs = r.optInt("n", 1)
            // 每条结果自带制式（扫描复核时定下来的），不是当前收听的那个
            val md = r.optString("mode", radioMode)
            val bw = r.optDouble("bw", 0.0)
            // 显示"复扫了几次"：次数越多，频率是多次平均出来的，越可信
            v.findViewById<TextView>(R.id.chInfo).text = String.format(
                Locale.US, "%s   %.0f dB%s%s%s", md,
                r.optDouble("db", 0.0),
                if (bw > 0.0) String.format(Locale.US, "   占用 %.0f kHz", bw / 1e3) else "",
                if (nobs > 1) String.format(Locale.US, "   已复扫 %d 次", nobs) else "",
                if (ovl) "   ⚠过载" else ""
            )
            v.findViewById<TextView>(R.id.chInfo)
                .setTextColor(getColor(if (ovl) R.color.warn else R.color.dim))
            return v
        }
    }

    /** 清空结果表：主界面上那颗"上次结果(N)"也就跟着没了。 */
    private fun confirmClearScan() {
        AlertDialog.Builder(this)
            .setTitle("清空扫描结果？")
            .setPositiveButton("清空") { _, _ -> clearScanResults() }
            .setNegativeButton(R.string.ch_cancel, null)
            .show()
    }

    private fun clearScanResults() {
        scanResults = JSONArray()
        scanAdapter?.notifyDataSetChanged()
        scanStatus?.text = "结果已清空"
        Thread {
            try { radioEngine?.callAttr("clear_scan") } catch (_: Throwable) { }
        }.start()
        updateRadioButtons()
    }

    /** 点结果表某一行 -> 问一句要不要把这个频率设为当前收听频率。 */
    private fun askSetScanFreq(i: Int) {
        val r = scanResults.optJSONObject(i) ?: return
        val f = r.optDouble("freq", 0.0)
        if (f <= 0.0) return
        AlertDialog.Builder(this)
            .setTitle("把 " + fmtMhz(f) + " MHz 设为当前频率？")
            .setPositiveButton("是") { _, _ -> setScanFreq(f, r.optString("mode", radioMode)) }
            .setNegativeButton("否", null)
            .show()
    }

    /** 定为当前收听频率：先停扫描，再切到那个频率（顺便切到它复核时用的制式）。 */
    private fun setScanFreq(f: Double, mode: String) {
        if (mode.isNotEmpty() && mode != radioMode) radioMode = mode
        if (scanActive) {
            stopScan(f)          // 引擎收尾时会把频率定在这个上（不再停最强）
        } else {
            radioCall2("set_mode", radioMode)
            radioCall2("set_frequency", f)
        }
        radioFreqHz = f
        setFreqText(radioFreqHz)
        updateRadioButtons()
        saveVfoState()
        scanDialog?.dismiss()
        toast("已设为 " + fmtMhz(f) + " MHz  " + radioMode)
    }

    /** 长按结果表某一行 -> 存到信道（走和「存到信道」按钮一样的选信道+覆盖确认流程） */
    private fun saveScanResult(i: Int) {
        val r = scanResults.optJSONObject(i) ?: return
        val f = r.optDouble("freq", 0.0)
        if (f <= 0.0) return
        // 亚音未知（扫描不再读亚音），先存 0，要开亚音在信道编辑里填
        // 用这条结果自己判出来的制式，不是当前收听的那个
        pendingWrite = RadioChannel("", f, r.optString("mode", radioMode), radioSteps[radioStepIdx], 0.0)
        if (channels.isEmpty()) channels = RadioChannel.load(prefs)
        AlertDialog.Builder(this)
            .setTitle("存到信道 · " + fmtMhz(f) + " MHz")
            .setAdapter(ChAdapter()) { _, which -> writeChannel(which) }
            .setNegativeButton(R.string.ch_cancel, null)
            .show()
    }

    /**
     * 频率输入框。
     *
     * ★ 不再弹对话框：点一下频率数字【直接】拉起数字键盘就地改，
     *   免得"弹窗 -> 再点一下输入框 -> 才能打字"这么绕。
     *
     * 输入限制：只允许数字和一个小数点，整数最多 4 位（到 1700MHz）、小数最多 4 位（1Hz）。
     */
    // ==================================================== 自制数字键盘
    //
    // 为什么不用系统输入法：频率是个 40sp 的大字，系统输入法的光标看不见，
    // 用户根本不知道改到哪一位了。自己做一个小键盘（0-9 / 小数点 / 退格 / 清空），
    // 输入的内容直接显示在那行大字上 —— 所见即所得，也不用管输入法弹不弹。
    private fun setupKeypad() {
        val digits = mapOf(
            R.id.kp0 to "0", R.id.kp1 to "1", R.id.kp2 to "2", R.id.kp3 to "3", R.id.kp4 to "4",
            R.id.kp5 to "5", R.id.kp6 to "6", R.id.kp7 to "7", R.id.kp8 to "8", R.id.kp9 to "9",
            R.id.kpDot to "."
        )
        for ((id, ch) in digits) {
            findViewById<Button>(id).setOnClickListener { keypadType(ch) }
        }
        findViewById<Button>(R.id.kpDel).setOnClickListener {
            if (freqBuf.isNotEmpty()) freqBuf = freqBuf.substring(0, freqBuf.length - 1)
            renderFreqBuf()
        }
        findViewById<Button>(R.id.kpClear).setOnClickListener { freqBuf = ""; renderFreqBuf() }
        findViewById<Button>(R.id.kpCancel).setOnClickListener { closeKeypad() }
        findViewById<Button>(R.id.kpOk).setOnClickListener { applyKeypad() }
        // 点频率大字就把键盘调出来（只在频率模式）
        tvFreq.setOnClickListener { if (radioVfo) openKeypad() }
    }

    private fun openKeypad() {
        if (!radioVfo) return
        freqEditing = true
        freqBuf = fmtMhzInput(radioFreqHz)
        keypad.visibility = View.VISIBLE
        renderFreqBuf()
    }

    private fun closeKeypad() {
        freqEditing = false
        keypad.visibility = View.GONE
        showFreq(fmtMhz(radioFreqHz))
    }

    private fun renderFreqBuf() {
        showFreq(if (freqBuf.isEmpty()) "—" else freqBuf)
    }

    /** 只收数字和一个小数点；整数最多 4 位（到 1700MHz），小数最多 4 位（1Hz） */
    private fun keypadType(ch: String) {
        if (!freqEditing) return
        if (ch == ".") {
            if (freqBuf.contains(".")) return
            if (freqBuf.isEmpty()) freqBuf = "0"
            freqBuf += "."
        } else {
            val dot = freqBuf.indexOf('.')
            if (dot < 0) {
                if (freqBuf.length >= 4) return
            } else {
                if (freqBuf.length - dot - 1 >= 4) return
            }
            if (freqBuf == "0") freqBuf = ""
            freqBuf += ch
        }
        renderFreqBuf()
    }

    private fun applyKeypad() {
        val mhz = freqBuf.toDoubleOrNull()
        if (mhz == null || mhz < 1.0 || mhz > 1700.0) {
            toast("频率要在 1 ~ 1700 MHz 之间")
            return
        }
        radioFreqHz = mhz * 1e6
        radioCall2("set_frequency", radioFreqHz)
        freqEditing = false
        keypad.visibility = View.GONE
        showFreq(fmtMhzText(mhz))
        updateChannelBar()
        saveVfoState()
    }

    /** 信道模式下键盘一律收起来 */
    private fun syncKeypadVisibility() {
        if (!radioVfo && (freqEditing || keypad.visibility == View.VISIBLE)) closeKeypad()
        if (!radioVfo) keypad.visibility = View.GONE
    }

    private fun showCtcssDialog() {
        if (!radioVfo) return
        val items = Array(RadioChannel.CTCSS_TONES.size) {
            RadioChannel.ctcssLabel(RadioChannel.CTCSS_TONES[it])
        }
        AlertDialog.Builder(this)
            .setTitle("模拟亚音 CTCSS（Hz）")
            .setItems(items) { _, w ->
                radioCtcss = RadioChannel.CTCSS_TONES[w]
                radioCall2("set_ctcss", radioCtcss)
                updateRadioButtons()
                updateChannelBar()
                saveVfoState()
            }
            .show()
    }

    // ---------------- 频率模式自己的一套设置（与信道互不影响） ----------------
    private fun saveVfoState() {
        prefs.edit()
            .putFloat("rfreq_vfo", (radioFreqHz / 1e6).toFloat())
            .putString("rmode_vfo", radioMode)
            .putFloat("rctcss_vfo", radioCtcss.toFloat())
            .putInt("rstep_vfo", radioStepIdx)
            .apply()
    }

    /** 回到频率模式时，用它【自己】上次的频率/制式/亚音/步进，不沿用信道的那套 */
    private fun loadVfoState(ch: RadioChannel) {
        radioFreqHz = prefs.getFloat("rfreq_vfo", (ch.freqHz / 1e6).toFloat()).toDouble() * 1e6
        radioMode = prefs.getString("rmode_vfo", ch.mode) ?: ch.mode
        radioCtcss = prefs.getFloat("rctcss_vfo", ch.ctcss.toFloat()).toDouble()
        radioStepIdx = prefs.getInt("rstep_vfo", nearestStepIndex(ch.stepHz))
            .coerceIn(0, radioSteps.size - 1)
    }

    private fun pushTuning() {
        setFreqText(radioFreqHz)
        radioCall2("set_mode", radioMode)
        radioCall2("set_step", radioSteps[radioStepIdx])
        radioCall2("set_frequency", radioFreqHz)
        radioCall2("set_ctcss", radioCtcss)
    }

    /**
     * 频率文本：最多 4 位小数，去掉没意义的尾零。
     * 95.9000 → 95.9，438.5000 → 438.5，457.8250 → 457.825（到 1Hz 的精度一个不丢）。
     */
    private fun fmtMhzText(mhz: Double): String {
        val s = String.format(Locale.US, "%.3f", mhz).trimEnd('0').trimEnd('.')
        return s.ifEmpty { "0" }
    }

    private fun fmtMhz(hz: Double): String = fmtMhzText(hz / 1e6)

    /** 键盘输入框里的初值：最多 4 位小数、去尾零。

    去尾零（95.9000 -> 95.9）是用户要的；但必须留到 4 位 —— 821.2375 这种
    铁路频率是 25kHz 栅格上的精确值，截成 3 位会在"直接点确定"时偏 500Hz。 */
    private fun fmtMhzInput(hz: Double): String {
        val s = String.format(Locale.US, "%.4f", hz / 1e6).trimEnd('0').trimEnd('.')
        return s.ifEmpty { "0" }
    }

    /**
     * 频率数字要正对屏幕中心。
     *
     * 这一行是【数字 + MHz】整体居中，右边挂着的 MHz 会把数字往左顶半个标签宽。
     * 给这行补一个等量的左内边距就把偏移抵消掉，数字回到正中、MHz 紧贴其右。
     * 偏移量必须按 MHz 的实测宽度算 —— 写死 dp 在换字体/换语言后会差几像素。
     */
    private fun showFreq(text: String) {
        tvFreq.text = text
        tvFreq.post { centerFreqRow() }
    }

    /** 扫描范围一行字：以哪个频率为中心、上下各多少 MHz（用户要求把范围写在界面上）。 */
    private fun scanRangeText(sc: JSONObject): String {
        val lo = sc.optDouble("start_hz", 0.0)
        val hi = sc.optDouble("end_hz", 0.0)
        if (hi <= lo) return ""
        return String.format(
            Locale.US, "范围 %s ~ %s MHz（上下各 %.0f MHz）   制式 %s",
            fmtMhz(lo), fmtMhz(hi), (hi - lo) / 2e6, radioMode
        )
    }

    private fun centerFreqRow() {
        val u = tvMhzUnit.width
        if (u <= 0) return
        val pad = u + (resources.displayMetrics.density * 6).toInt()
        if (freqRow.paddingStart != pad) freqRow.setPadding(pad, 0, 0, 0)
    }

    /** 刷新频率显示。正在键盘输入时不要覆盖用户打的内容。 */
    private fun setFreqText(hz: Double) {
        if (freqEditing) return
        showFreq(fmtMhz(hz))
    }

    private fun openUrl(url: String) {
        try { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url))) }
        catch (_: Throwable) { toast("无法打开浏览器") }
    }

    /**
     * 丢弃一个引擎之前必须先把它 stop() 掉。
     *
     * ★ 不这么做会泄漏 TCP 连接：实测在一次"启动失败 → 再启动"之后，
     *    /proc/net/tcp 里 1234 的条目从 3 涨到 7 —— 被丢弃的引擎没人关，
     *    它的 socket 一直挂在驱动那边，驱动会以为还有客户端连着。
     */
    private fun releaseEngine(eng: PyObject?) {
        if (eng == null) return
        // 引擎被丢弃，"建立连接时用的设置"这个记录也跟着失效
        engineSource = null
        Thread {
            try { eng.callAttr("set_push", null) } catch (_: Throwable) { }
            try { eng.callAttr("stop") } catch (_: Throwable) { }
        }.start()
    }

    // -------------------------------------------------------------- 启停
    private fun startEngine() {
        if (busy || running) return
        busy = true
        updateButtons()
        val bench = prefs.getBoolean("bench", false)
        // 取消"延时断开"：用户又要开始接收了
        main.removeCallbacks(fullStopTask)

        // ★ 先看看能不能【直接恢复】：上次停止只是暂停，连接还留着。
        //   这条路完全不碰驱动，所以是瞬时的，也不会有重连竞态。
        val existing = engine
        // ★ 还要确认【数据源设置没变过】。resume 只重跑 DSP，不会换 TCP 连接 ——
        //   所以关掉/打开台架、换服务器地址之后，恢复出来的还是旧连接。
        //   注意这里必须用 Kotlin 侧记的 engineSource，不能问引擎的 tcp_host：
        //   保存设置时 set_host() 已经把引擎里的 host 改成新值了，但 socket 并没有重连。
        val srcSame = (existing != null) && (engineSource == sourceKey())
        if (existing != null && !bench && srcSame) {
            main.post { tvHeader.text = "正在恢复上次的连接…" }
            Thread {
                val ok = try { existing.callAttr("resume").toBoolean() } catch (_: Throwable) { false }
                main.post {
                    if (ok) {
                        busy = false; running = true; updateButtons()
                        tvHeader.text = "已恢复接收"
                        // ★ 兜底复查：resume() 只是重启本地 DSP 线程，它不保证那条
                        //   TCP 连接还活着（拔掉电视棒后连接可能已经死了）。
                        //   引擎侧已改成"连接坏了就不让 resume"，这里再补一道：
                        //   3 秒后回查真实状态，坏了就丢弃引擎、走完整重建 ——
                        //   绝不能让界面停在"已恢复接收"而频谱一动不动（用户报过）。
                        main.postDelayed({
                            if (destroyed || !running || engine !== existing) return@postDelayed
                            val alive = try {
                                existing.callAttr("is_connected").toBoolean()
                            } catch (_: Throwable) { false }
                            if (!alive) {
                                engine = null
                                releaseEngine(existing)
                                running = false
                                busy = false
                                startEngine()
                            }
                        }, 3000L)
                    } else {
                        // 连接已经坏了，走完整流程。
                        // 注意：丢弃前一定要 stop()，否则这个引擎的 socket 会一直挂着。
                        engine = null
                        releaseEngine(existing)
                        busy = false
                        startEngine()
                    }
                }
            }.start()
            return
        }

        // 注意：连接数据源属于网络操作，不能放主线程，所以整体挪进线程里。
        Thread {
            try {
                main.post { tvHeader.text = "正在初始化 Python 运行时…" }
                val mod = Python.getInstance().getModule("lbj_engine")
                module = mod
                val s = sink ?: StateSink(main) { json -> render(json) }.also { sink = it }
                // 走到这里说明旧引擎已经不用了（多半是上一次失败留下的），
                // 先把它停干净，否则它的连接会一直占着驱动。
                val stale = engine
                engine = null
                releaseEngine(stale)
                val eng = mod.callAttr("LbjEngine", s)
                engine = eng
                applyPrefs(eng)
                main.post { tvHeader.text = "正在装配 DSP 链路…" }
                // 首次初始化 Python 要 10~30 秒，用户完全可能在这期间退出。
                // 不检查的话引擎会在 Activity 销毁【之后】才 start()，此后再没人 stop 它，
                // 于是后台一直跑 DSP、占着端口和驱动连接，持续耗电发热。
                if (destroyed) {
                    try { eng.callAttr("stop") } catch (_: Throwable) { }
                    main.post { busy = false; updateButtons() }
                    return@Thread
                }
                // 先只做装配，不连接（连接时机见下）
                eng.callAttr("start")

                // ★ 连接数据源。这里【不做任何端口探测】：
                //   · 探测端口若"连上就断"，会被驱动当成客户端掉线，它直接自杀（docs/33）；
                //   · 若改读 /proc/net/tcp，Android 10+ 又不让 App 看别的 UID 的 socket，
                //     本地测试能过、真机永远返回"未监听"（docs/35）。
                //   直接去连最可靠：连接被拒时【在驱动侧不留任何痕迹】，重试完全安全。
                if (bench) {
                    // ★ 台架模式连的是电脑上的模拟服务器，必须先把【内置驱动】停掉。
                    //   否则它仍在后台占着 USB、继续监听 127.0.0.1:1234、通知栏也挂着，
                    //   看起来就像"切了台架却还在从 SDR 读数据"（用户实际反馈过）。
                    BuiltinDriver.stop(this@MainActivity)
                    main.post { tvHeader.text = "台架模式：连接 " + effectiveHost() + ":1234 …" }
                    val ok = try {
                        eng.callAttr("connect", 25.0, 8.0).toBoolean()
                    } catch (_: Throwable) { false }
                    if (!ok) {
                        main.post {
                            busy = false; running = false; updateButtons()
                            tvHeader.text = "连不上台架服务器 " + effectiveHost() + ":1234"
                            clearDashboardUi()
                        }
                        return@Thread
                    }
                } else {
                    // 先【短试】一次：连得上说明驱动本来就在跑 —— 直接复用，绝不再去拉起它。
                    main.post { tvHeader.text = "正在检查驱动是否已在运行…" }
                    // 短试：8 秒总时长、6 秒静默判定、端口没人监听就立即返回。
                    //
                    // 三个判据一次覆盖三种情况：
                    //   · 驱动没在跑   -> 连接被拒 -> fail_fast -> 立刻返回 False，马上去拉起驱动
                    //   · 驱动正常在跑 -> 1~2 秒内收到数据 -> 成功，直接复用（不打扰驱动）
                    //   · 驱动卡住了   -> 连得上但收不到数据 -> 约 5 秒后 socket 超时报错
                    //                    -> 返回 False -> 拉起驱动（这一步会把它重置）-> 再连
                    //   最后那种是实测遇到过的：驱动挂着一个已死客户端，新连接收不到数据。
                    val already = try {
                        eng.callAttr("connect", 8.0, 6.0, true).toBoolean()
                    } catch (_: Throwable) { false }
                    if (already) {
                        main.post { tvHeader.text = "驱动已在运行，直接使用（没有打扰它）" }
                    } else {
                        // ★ 最多试 3 轮「拉起驱动 -> 连接」。
                        //
                        // 为什么要多轮：快速点开/停止时，驱动上一次的服务器可能还在收尾
                        // （它是"客户端一走就停服"的设计）。这时再发 Intent 拉起，
                        // 它未必能立刻准备好 —— 实测出现过卡在"已装配，等待连接"再无下文，
                        // 用户看到的就是"连不上本机驱动"。多试两轮基本都能自愈。
                        //
                        // 走内置还是外部，只看设置里的【使用内置驱动】开关：
                        //   · 勾上 -> BuiltinDriver 在本进程起 rtl_tcp，不装外部驱动 App
                        //   · 没勾 -> 原来的 iqsrc:// 拉起外部驱动 App（回退路径，必须保留）
                        val builtin = prefs.getBoolean("builtin", true)
                        val hwFreq = hwFreqHz()
                        var ok = false
                        for (round in 1..3) {
                            main.post {
                                tvHeader.text = if (builtin) {
                                    if (round == 1) "正在启动内置驱动…（首次会请求 USB 授权）"
                                    else "正在重试内置驱动…（第 " + round + " 次）"
                                } else {
                                    if (round == 1) "正在拉起 RTL-SDR 驱动…"
                                    else "正在重试拉起驱动…（第 " + round + " 次）"
                                }
                            }
                            // 外部路径注意：Android 11+ 有包可见性限制，queryIntentActivities 可能
                            // 误报“未安装”，所以 launchDriver() 不依赖检测结果，直接尝试发 Intent，
                            // 真的解析不到才提示去装。
                            val started = if (builtin) startBuiltinDriver(hwFreq) else launchDriver()
                            if (!started) {
                                main.post {
                                    busy = false; updateButtons()
                                    tvHeader.text = if (builtin) "内置驱动启动失败"
                                                     else "没有找到 RTL-SDR 驱动 App"
                                    // 内置驱动起不来只可能是设备/原生库/服务的问题，
                                    // 不存在"去装个驱动App"这条路，所以弹另一套提示。
                                    if (builtin) showBuiltinFailedDialog() else showDriverDialog()
                                }
                                return@Thread
                            }
                            ok = try {
                                eng.callAttr("connect", DRIVER_WAIT_MS / 1000.0, 8.0).toBoolean()
                            } catch (_: Throwable) { false }
                            if (ok) break
                            // 给驱动一点时间把上一个会话收拾干净，再试下一轮
                            try { Thread.sleep(1500) } catch (_: InterruptedException) { break }
                        }
                        if (!ok) {
                            main.post {
                                busy = false; running = false; updateButtons()
                                tvHeader.text = "驱动没有就绪"
                                // ★ 失败时必须把仪表盘清掉。
                                //   否则上一轮的频谱/读数会留在屏幕上，看起来"在收但不动"，
                                //   用户会以为软件卡死了（实测被这么误解过）。
                                clearDashboardUi()
                                if (builtin) {
                                    // 内置驱动全程在本 App 里，直接把话说清楚就行
                                    showBuiltinFailedDialog()
                                } else {
                                    // ★ 还要先把本 App 拉回前台，否则提示对话框会被驱动界面挡住。
                                    bringSelfToFront()
                                    showDriverFailedDialog()
                                }
                            }
                            return@Thread
                        }
                        // 把本 App 拉回前台：否则用户被留在驱动界面，
                        // 看不到接收情况，也找不到【停止】按钮。
                        // （内置驱动不离开本 App，不需要拉。）
                        if (!builtin) main.post { bringSelfToFront() }
                    }
                }

                // 清掉上一场会话残留在模块全局量里的仪表盘数据，
                // 否则第一帧就会把上一趟车原样显示出来（车次/公里标/ETA 全在）。
                try { eng.callAttr("clear_dashboard") } catch (_: Throwable) { }
                // running 不乐观置真：问一次引擎的真实状态。
                // 乐观置真的话，引擎"启动后立刻死掉"（典型：驱动还没监听 1234，
                // 立刻报【连接被拒】）时这一帧会把按钮锁死在"正在接收"。
                val nowRunning = try {
                    JSONObject(eng.callAttr("snapshot_json").toString()).optBoolean("running", false)
                } catch (_: Throwable) { false }
                // 记下这次是按哪套数据源设置连上的，供下次"停止→开始"判断能否直接恢复
                engineSource = sourceKey()
                // 用户点的是【收音机】但当时还没有连接：连接刚建好，这就切过去
                val wantRadio = pendingRadio
                pendingRadio = false
                main.post {
                    busy = false; running = nowRunning; updateButtons()
                    if (wantRadio && nowRunning) switchToRadio(eng)
                }
            } catch (t: Throwable) {
                main.post {
                    busy = false; running = false; updateButtons()
                    tvHeader.text = "启动失败：\n" + (t.message ?: t.toString())
                }
            }
        }.start()
    }

    /**
     * 停止接收。
     *
     * ★ 这里【只暂停，不断开连接】。
     *
     * 原因：rtl_tcp_andro 是"客户端一走就把整个服务器停掉"的设计。
     * 如果停止时断开，那么"停止→开始"就必须重新拉起驱动，而快速反复启停
     * 会让它来不及收拾上一个会话 —— 实测会出现"TCP 连上了、驱动却不推数据"，
     * 8 个数据块之后 read() 超时（err='等待数据流超时，硬件可能未授权'），
     * 界面标题就显示"连不上本机驱动"，而频谱是上一帧的残留、看着像卡死。
     *
     * 保持连接后，"开始"只是重启本地解码线程，完全不碰驱动，瞬时且无竞态。
     *
     * 代价：驱动会继续推流。所以下面挂了一个延时兜底 —— 真正不用了就彻底断开。
     */
    private fun stopEngine() {
        if (busy) return
        busy = true
        updateButtons()
        Thread {
            var ok = true
            val eng = engine
            // 还有活连接 -> 暂停（保留连接）；否则才真正断开
            if (eng != null) {
                try {
                    val stillConnected = eng.callAttr("is_connected").toBoolean()
                    if (stillConnected) eng.callAttr("pause") else eng.callAttr("stop")
                } catch (_: Throwable) { ok = false }
            }
            main.post {
                busy = false; running = false; updateButtons()
                stopSound()
                tvHeader.text = if (ok) "已停止"
                                else "已停止（但引擎报告了错误，建议重新开始接收）"
                clearDashboardUi()
                // 真正不用了就彻底断开：延时兜底，避免驱动一直空转耗电
                scheduleFullStop()
            }
        }.start()
    }

    /** 延时兜底：停止后一段时间内没有重新开始，就真正断开连接 */
    private fun scheduleFullStop() {
        main.removeCallbacks(fullStopTask)
        main.postDelayed(fullStopTask, FULL_STOP_DELAY_MS)
    }

    private val fullStopTask = Runnable {
        if (running || busy) return@Runnable
        val eng = engine
        engine = null
        // 引擎彻底丢弃了，数据源记录也失效（否则下次可能拿它误判成"可以直接恢复"）
        engineSource = null
        if (eng != null) {
            Thread {
                try { eng.callAttr("stop") } catch (_: Throwable) { }
            }.start()
            tvHeader.text = "已停止（连接已断开）"
        }
    }

    /**
     * 清掉界面上所有"上一趟车"的残留。
     *
     * 停止之后不会再有任何状态推送，因此不清的话，15 个信息格、频谱、
     * "最近列车"会一直停在最后一帧 —— 红色干扰预警还会永远亮着。
     * 用户和旁边的人无法判断到底还在不在接收。
     */
    private fun clearDashboardUi() {
        tvTrain.text = "----"
        tvCategory.text = "等待信号..."
        for (i in 0 until gridLabels.size) setCell(i, "---")
        tvWarning.visibility = View.GONE
        tvS1.text = ""; tvS2.text = ""; tvS3.text = ""; tvS4.text = ""
        renderTrains(null)
        spectrum.update(FloatArray(0), -1f, 0.5f)
    }

    private fun updateButtons() {
        btnStart.isEnabled = !busy && !running && !inRadio
        btnStop.isEnabled = !busy && running && !inRadio
        btnSettings.isEnabled = true
        btnKeyword.isEnabled = true
        btnClear.isEnabled = true
        btnHistory.isEnabled = true
        syncForeground()
    }

    /**
     * 引擎在跑就挂前台服务保活；两个都停了才撤掉。
     *
     * ★ 没有它，锁屏后还能出声只是"碰巧"（Activity 进后台后进程和 AudioTrack
     *   还活着而已），MIUI 的省电策略随时会冻结或杀掉进程。前台服务是唯一可靠的做法。
     */
    private fun syncForeground() {
        val want = inRadio || running
        if (want == fgOn) return
        fgOn = want
        if (want) {
            EngineService.start(
                this,
                if (inRadio) "收音机接收中 · 后台继续" else "列车预警接收中 · 后台继续"
            )
        } else {
            EngineService.stop(this)
        }
    }

    /** Android 13+ 要通知权限，否则前台服务的常驻通知不会显示 */
    private fun ensureNotiPermission() {
        if (Build.VERSION.SDK_INT < 33) return
        try {
            val p = "android.permission.POST_NOTIFICATIONS"
            if (ContextCompat.checkSelfPermission(this, p) != PackageManager.PERMISSION_GRANTED) {
                ActivityCompat.requestPermissions(this, arrayOf(p), 9021)
            }
        } catch (_: Throwable) {
        }
    }

    /** 在后台线程调用一个无返回值的引擎方法 */
    private fun callAsync(method: String, done: (() -> Unit)? = null) {
        // 守卫用 running 而不是 engine == null：停止之后 engine 已经解绑，
        // 而"运行中"才是这些操作真正需要的前提。
        if (!running) { toast("请先开始接收"); return }
        val eng = engine ?: run { toast("请先开始接收"); return }
        Thread {
            try {
                eng.callAttr(method)
                done?.let { main.post(it) }
            } catch (t: Throwable) {
                main.post { toast("操作失败：" + (t.message ?: "")) }
            }
        }.start()
    }

    // -------------------------------------------------------------- 设置
    /**
     * 引擎实际该连的地址。
     * 【关键安全约束】只有台架模式开启时才允许指向外部地址；
     * 台架模式关闭时一律强制 127.0.0.1（手机本机的驱动 App）。
     * 这样即使 pref 里残留着电脑 IP，正常使用也绝不会去连它。
     */
    private fun effectiveHost(): String {
        if (!prefs.getBoolean("bench", false)) return "127.0.0.1"
        val h = (prefs.getString("host", "127.0.0.1") ?: "").trim()
        return h.ifEmpty { "127.0.0.1" }
    }

    /** 是否处于乘车模式（「关注车次」里填了自己坐的车次）。 */
    private fun isRideMode(): Boolean =
        (prefs.getString("ridetrain", "") ?: "").trim().isNotEmpty()

    /**
     * 数据源标识：连哪里 + 走不走内置驱动。
     * 只要它变了，"停止→开始"就必须重新建连，不能走 resume 快路。
     */
    private fun sourceKey(): String =
        effectiveHost() + "|" + prefs.getBoolean("builtin", true)

    private fun applyPrefs(eng: PyObject) {
        fun t(block: () -> Unit) { try { block() } catch (_: Throwable) { } }
        // ★ 调谐器型号要排在 set_gain 之前 —— 它决定用哪张增益档位表
        t {
            eng.callAttr("set_tuner", prefs.getString("tuner",
                if (prefs.getBoolean("fc0013", true)) "FC0013" else "R820T"))
        }
        t { eng.callAttr("set_host", effectiveHost()) }
        // ★ 频率必须在这里下发。
        // 之前只在设置对话框的"保存并应用"里调用 set_frequency，于是
        // 【停止→开始】或重启 App 之后，频率会悄悄退回引擎默认的 821.2375，
        // 而设置框里仍然显示用户填的值 —— 设置与实际永久不一致，
        // 用户只会觉得"这 App 保存不了设置"。
        t { eng.callAttr("set_frequency", prefs.getFloat("freq", FREQ_MHZ.toFloat()).toDouble()) }
        t { eng.callAttr("set_ppm", prefs.getInt("ppm", 0)) }
        // 增益要吸附到硬件真正支持的档位，并把吸附结果写回 prefs，
        // 否则状态栏显示吸附后的值、设置框显示用户填的原值，两边对不上。
        try {
            val actual = eng.callAttr("set_gain", prefs.getFloat("gain", 19.7f).toDouble()).toDouble()
            prefs.edit().putFloat("gain", actual.toFloat()).apply()
        } catch (_: Throwable) { }
        t { eng.callAttr("set_threshold", prefs.getFloat("thr", -55f).toDouble()) }
        t { eng.callAttr("set_hold_ms", prefs.getFloat("hold", 700f).toDouble()) }
        t { eng.callAttr("set_afc_enabled", prefs.getBoolean("afc", true)) }
        val km = prefs.getFloat("mykm", -1f)
        t { if (km >= 0f) eng.callAttr("set_my_km", km.toDouble()) else eng.callAttr("set_my_km", null) }
        t { eng.callAttr("set_keywords", prefs.getString("kw", "") ?: "") }
        t { eng.callAttr("set_filter_mode", prefs.getString("mode", "highlight")) }
        t { eng.callAttr("set_strict_filter", prefs.getBoolean("strict", true)) }
        t { eng.callAttr("set_err_warn", prefs.getBoolean("errwarn", true)) }
        // 乘车模式：自己坐的车次（只在下面「最近列车」更新，不刷上面）。
        // ★ 必须放在过滤设置【之后】：它会把"上面正显示本车"的残留清成
        //   "乘车模式：等待其他车次…"，若排在 set_filter_mode 前面，
        //   会被 strict 模式那句"没命中就重置上面"覆盖掉。
        t { eng.callAttr("set_ride_trains", prefs.getString("ridetrain", "") ?: "") }
        // ★ 按线路设的公里标同样要回填。
        // 不回填的话，重启后它静默丢失：状态列永远停在"未设置线路位置"，
        // ETA 不出现、接近告警永远不触发 —— 而设置框里还显示着用户填的值。
        t { applyRouteKm(eng) }
        // 列车接收历史：目录给引擎（一天一个文件），保留天数 0 = 永久
        t { eng.callAttr("set_history_dir", histDir()) }
        t { eng.callAttr("set_history_keep_days", prefs.getInt("histkeep", 0)) }
    }

    /** 历史文件目录（应用私有，不需要任何权限；导出后才能真正备份）。 */
    private fun histDir(): String = File(filesDir, "history").absolutePath

    /**
     * 把 prefs 里的"线路=公里标"回填给引擎，并清除本轮已经不再出现的线路。
     *
     * 为什么要记 routekm_applied：光调 set_route_km 只能"加/改"，
     * 用户把输入框清空时旧线路仍然留在引擎里继续参与计算 ——
     * 界面上设置没了、实际行为没变。所以要先清掉上一轮设过、这一轮没再出现的线路。
     */
    private fun applyRouteKm(eng: PyObject) {
        val spec = prefs.getString("routekm", "") ?: ""
        val prev = prefs.getString("routekm_applied", "") ?: ""
        for (part in prev.split(',')) {
            val name = part.substringBefore('=').trim()
            if (name.isNotEmpty() && !spec.contains(name)) {
                eng.callAttr("clear_route_km", name)
            }
        }
        for (part in spec.split(',')) {
            val idx = part.indexOf('=')
            if (idx <= 0) continue
            val name = part.substring(0, idx).trim()
            val v = part.substring(idx + 1).trim()
            if (name.isNotEmpty() && v.isNotEmpty()) eng.callAttr("set_route_km", name, v)
        }
        prefs.edit().putString("routekm_applied", spec).apply()
    }

    // ------------------------------------------------------------------
    //  输入限制：inputType 只是给输入法的"建议"，粘贴和部分第三方输入法能绕过，
    //  所以每个框都再加一层字符白名单过滤器；保存时还会做范围校验。
    // ------------------------------------------------------------------
    private fun filterOf(allow: (Char) -> Boolean) = InputFilter { src, start, end, _, _, _ ->
        var bad = false
        for (i in start until end) {
            if (!allow(src[i])) { bad = true; break }
        }
        if (bad) "" else null
    }

    /** 数字：只允许 0-9 和一个小数点 */
    private val kNum: (Char) -> Boolean = { c -> c.isDigit() || c == '.' }
    /** 带符号数字：允许负号（阈值、PPM 需要） */
    private val kNumSigned: (Char) -> Boolean = { c -> c.isDigit() || c == '.' || c == '-' }
    /** 主机地址：字母数字和 . - : */
    private val kHost: (Char) -> Boolean = { c -> c.isLetterOrDigit() || c == '.' || c == '-' || c == ':' }
    /** 车次/机车关键词：字母、数字、逗号、空格、汉字 */
    private val kKeyword: (Char) -> Boolean = { c ->
        c.isLetterOrDigit() || c == ',' || c == '，' || c == ' ' || c.code > 0x2E80
    }
    /** 线路公里标：在关键词基础上允许 = 和 . */
    private val kRoute: (Char) -> Boolean = { c ->
        c.isLetterOrDigit() || c == '=' || c == '.' || c == '-' || c == ' ' || c.code > 0x2E80
    }

    private fun labelOf(box: LinearLayout, text: String) {
        val t = TextView(this)
        t.text = text
        t.textSize = 13f
        box.addView(t)
    }

    /** 数字输入框（频率、增益、阈值……） */
    private fun numField(box: LinearLayout, text: String, value: String, signed: Boolean = false): EditText {
        labelOf(box, text)
        val e = EditText(this)
        e.inputType = if (signed)
            InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL or InputType.TYPE_NUMBER_FLAG_SIGNED
        else
            InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
        e.filters = arrayOf(filterOf(if (signed) kNumSigned else kNum))
        e.setText(value)
        box.addView(e)
        return e
    }

    /** 文本输入框（关键词、线路名……），按用途给不同白名单 */
    private fun textField(box: LinearLayout, text: String, value: String, allow: (Char) -> Boolean): EditText {
        labelOf(box, text)
        val e = EditText(this)
        e.inputType = InputType.TYPE_CLASS_TEXT
        e.filters = arrayOf(filterOf(allow))
        e.setText(value)
        box.addView(e)
        return e
    }

    private fun check(box: LinearLayout, label: String, on: Boolean): CheckBox {
        val c = CheckBox(this)
        c.text = label
        c.isChecked = on
        box.addView(c)
        return c
    }

    // ------------------------------------------------- RSP1 / RSP2（Mirics 芯片）
    /**
     * 识别表：知道是 Mirics 芯片的 USB ID。前三条是 SDRplay 三兄弟（RSP1/RSP1A/RSP2），
     * 后四条是 libmirisdr 那个年代的一体板（老式 DVB-T 棒，同芯片）。
     *
     * 这个表只用来"自动认出来"；表外的板子照样能用 —— 自检失败时会弹出【整条 USB 总线
     * 上的设备列表】，点哪台就拿哪台按 Mirics 打开。所以用户反馈"没找到设备"时，先看他
     * 那一屏截图里到底挂的是什么 ID，再决定要不要往表里加。
     */
    private val MIRI_KNOWN = arrayOf(
        0x1df7 to 0x2500, 0x1df7 to 0x3000, 0x1df7 to 0x3010,
        0x2040 to 0xd300, 0x07ca to 0x8591, 0x04bb to 0x0537, 0x0511 to 0x0037)

    /**
     * 前端波段切换表按型号分两套：SDRplay 三兄弟的滤波器/本振切换值跟通用 MSi2500 板
     * 不一样（libmirisdr 里就是 hw_switch_freq_plan_default / _sdrplay 两张表）。
     * 选错了能"打开、能设频率"，但天线段选错就收不到信号 —— 所以按 PID 选，别写死。
     * 返回：0 = 通用 MSi2500，1 = SDRplay。
     */
    private fun miriHwFlavour(d: UsbDevice): Int =
        if (d.vendorId == 0x1df7 &&
            (d.productId == 0x2500 || d.productId == 0x3000 || d.productId == 0x3010)) 1 else 0

    private fun isMiriKnown(d: UsbDevice): Boolean =
        MIRI_KNOWN.any { it.first == d.vendorId && it.second == d.productId }

    /** 一行设备描述：VID:PID + 能读到的厂商/产品名 + 系统设备路径（没授权时名字读不到）。 */
    private fun usbLine(d: UsbDevice): String {
        val nm = try {
            listOfNotNull(d.manufacturerName, d.productName).joinToString(" ")
        } catch (_: Throwable) { "" }
        return String.format(Locale.US, "%04X:%04X", d.vendorId, d.productId) +
            (if (nm.isBlank()) "" else "  " + nm) + "\n" + d.deviceName
    }

    /**
     * RSP1 / RSP1A / RSP2（Mirics MSi2500+MSi001 芯片）的入口。
     *
     * 这三型用的是 Mirics 芯片，有开源驱动（libmirisdr），所以能直接插手机用 —— 但它
     * 【不是】内部驱动那条路，而是「在本机起一个 rtl_tcp 服务器」：起好后 App 按
     * 【台架模式 + 127.0.0.1】连它。
     *
     * 认设备分三档，从确定到不确定：
     *   ① 刚在设备列表里手动点过的那台（用户已经明说要试它了）
     *   ② 识别表里的型号
     *   ③ 表里没有但厂商 ID 是 0x1df7 的（SDRplay 家新出的就用这档兜一下）
     * 三档都没有 = 不猜了，把整条 USB 总线列出来让用户看清 + 手动选（朋友测试时全靠这个）。
     */
    private fun rsp1Action() {
        val usb = getSystemService(Context.USB_SERVICE) as UsbManager
        val all = usb.deviceList.values.toList()
        val dev = all.firstOrNull { it.deviceName == pendingRsp1Dev?.deviceName }
            ?: all.firstOrNull { isMiriKnown(it) }
            ?: all.firstOrNull { it.vendorId == 0x1df7 }
        if (dev == null) {
            showUsbDeviceListDialog(all)
            return
        }
        pendingRsp1Dev = dev
        if (!usb.hasPermission(dev)) {
            ensureUsbPermReceiver()
            val flags = if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_IMMUTABLE else 0
            // setPackage：把这个广播明确限定给自己，免得被 Android 14 的隐式广播限制挡掉
            usb.requestPermission(dev,
                PendingIntent.getBroadcast(this, 0,
                    Intent("com.railfan.lbj.USB_PERMISSION").setPackage(packageName), flags))
            toast("请在系统弹窗里点【允许】，允许之后会自动接着自检")
            return
        }
        rsp1Probe(dev)
    }

    /**
     * 系统授权框的回调。授权通过就自动接上自检 —— 让用户少点一次、也少一个"忘了再点一次"
     * 的坑（朋友远程测试时这一步最容易被卡住）。
     * 注册失败也不影响功能：老办法"再点一次按钮"照样能用，所以这里只记日志不打扰用户。
     */
    private fun ensureUsbPermReceiver() {
        if (usbPermRx != null) return
        val rx = object : BroadcastReceiver() {
            override fun onReceive(c: Context?, i: Intent?) {
                val usb = getSystemService(Context.USB_SERVICE) as UsbManager
                val d = pendingRsp1Dev ?: return
                if (usb.hasPermission(d)) main.post { rsp1Probe(d) }
            }
        }
        try {
            ContextCompat.registerReceiver(this, rx,
                IntentFilter("com.railfan.lbj.USB_PERMISSION"), ContextCompat.RECEIVER_NOT_EXPORTED)
            usbPermRx = rx
        } catch (t: Throwable) {
            android.util.Log.w("MiriSdrDriver", "注册 USB 授权回调失败（再点一次按钮即可）", t)
        }
    }

    /** 自检：打开 -> 设 960k/频率 -> 读增益档 -> 关掉。不改任何设置、不串流，随便点。 */
    private fun rsp1Probe(dev: UsbDevice) {
        val usb = getSystemService(Context.USB_SERVICE) as UsbManager
        val name = dev.deviceName
        val hw = miriHwFlavour(dev)
        toast("正在自检…（结果会弹出来）")
        Thread {
            val conn = try { usb.openDevice(dev) } catch (t: Throwable) { null }
            if (conn == null) {
                main.post {
                    AlertDialog.Builder(this)
                        .setTitle("打不开这个 USB 设备")
                        .setMessage("系统拒绝了 openDevice：\n\n" + usbLine(dev) + "\n\n" +
                            "多半是被别的 App / 系统驱动占着（另一个 SDR App 没退干净，" +
                            "或者上一次的自检没关掉）。\n\n" +
                            "① 把其它收音机 / SDR / 电视 App 全部清掉\n" +
                            "② 拔了重插，再点一次自检")
                        .setPositiveButton("知道了", null)
                        .show()
                }
                return@Thread
            }
            // 自检步骤落盘：native 里崩了的话用户看不到 logcat，只能靠这个文件
            val trace = miriTraceFile()
            try { trace.delete() } catch (_: Throwable) { }
            val res = try {
                if (miriDevice == null) miriDevice = MiriSdrDevice()
                miriDevice!!.probe(conn.fileDescriptor, name, trace.absolutePath, hw)
            } catch (t: Throwable) {
                "自检异常：" + (t.message ?: t.toString())
            }
            try { conn.close() } catch (_: Throwable) { }
            main.post {
                AlertDialog.Builder(this)
                    .setTitle("自检结果  " +
                        String.format(Locale.US, "%04X:%04X", dev.vendorId, dev.productId))
                    .setMessage(res + "\n\n前端波段表：" +
                        (if (hw == 1) "SDRplay（RSP1/RSP1A/RSP2）" else "通用 MSi2500"))
                    .setPositiveButton("启动驱动") { _, _ -> rsp1Start(usb, dev) }
                    .setNegativeButton("关闭", null)
                    .show()
            }
        }.start()
    }

    /**
     * 没认出来时不猜：把当前挂着的 USB 设备全列出来。
     * 这一屏是给"远程测试"用的 —— 用户截图发过来，就能知道棒子到底认成了什么 ID，
     * 或者压根没被枚举（那就是供电 / OTG 线的问题）。
     */
    private fun showUsbDeviceListDialog(all: List<UsbDevice>) {
        android.util.Log.i("MiriSdrDriver", "USB 设备 " + all.size + " 个：" +
            all.joinToString(" | ") { usbLine(it).replace('\n', ' ') })
        if (all.isEmpty()) {
            AlertDialog.Builder(this)
                .setTitle("没找到 RSP1 类设备")
                .setMessage("手机现在【一个 USB 设备都没认到】—— 棒子没上电，或者系统没枚举它。\n\n" +
                    "① OTG 转接头/线接触不良或只能充电：拔了重插，换一根线\n" +
                    "② 供电不够（这种棒子比 RTL 电视棒费电）：换带供电的 OTG 扩展坞\n" +
                    "③ 个别机型设置里有【USB OTG】开关，确认是开着的\n\n" +
                    "插好之后系统一般会弹一下『已连接 USB 设备』；看到它了再点一次自检。\n" +
                    "（RTL 电视棒能用、只有这个棒子不认，基本就是 ① 或 ②）")
                .setPositiveButton("知道了", null)
                .show()
            return
        }
        val names = all.map { usbLine(it) }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle("USB 上现在有 " + all.size + " 个设备")
            .setMessage("没有已知的 SDRplay / Mirics 型号。\n\n" +
                "下面是当前全部 USB 设备：有你的棒子就点它，会强行按 Mirics 芯片打开试一次；\n" +
                "没有它、或者点了还是不行，请把这一屏【截图】发我。")
            .setItems(names) { _, i -> rsp1ForceProbe(all[i]) }
            .setNegativeButton("关闭", null)
            .show()
    }

    /** 用户手动点名的那台：记下来，流程照走（要授权就先授权，授权完自动接自检）。 */
    private fun rsp1ForceProbe(dev: UsbDevice) {
        pendingRsp1Dev = dev
        rsp1Action()
    }

    /** 自检步骤落盘文件（native 崩了之后全靠它）。 */
    private fun miriTraceFile(): File = File(filesDir, "miri_probe.log")

    /**
     * 进界面时看一眼上次自检跑完了没有：没有最后那行"自检结束"就说明 native 崩了，
     * 把落盘的最后几步显示出来。
     * 装到别人手机上时用户没有 adb、没有 logcat —— 这是唯一能把崩溃点带回来的渠道。
     */
    private fun checkMiriProbeTrace() {
        if (miriTraceChecked) return
        miriTraceChecked = true
        val f = miriTraceFile()
        val txt = try { if (f.exists()) f.readText() else "" } catch (_: Throwable) { "" }
        if (txt.isBlank()) return
        if (txt.contains("自检结束")) {
            try { f.delete() } catch (_: Throwable) { }
            return
        }
        AlertDialog.Builder(this)
            .setTitle("上次自检没跑完（App 崩在这里）")
            .setMessage(txt.trim() + "\n\n把这一屏截图发我。")
            .setPositiveButton("知道了") { _, _ -> try { f.delete() } catch (_: Throwable) { } }
            .setNegativeButton("先留着", null)
            .show()
    }

    /**
     * 真正起流：在本机 127.0.0.1:1234 开 rtl_tcp 服务。
     * 失败时给的是"照着做就能好"的清单，而不是一句"看日志" —— 装机给别人测时用户手里
     * 没有 logcat，弹窗必须自己把下一步说清。
     */
    private fun rsp1Start(usb: UsbManager, dev: UsbDevice) {
        val hw = miriHwFlavour(dev)
        Thread {
            var lastErr = ""
            // 失败就等一秒再试一次：USB 子系统刚被自检用过，偶尔要缓一下才肯重新开流。
            for (attempt in 1..2) {
                var conn: UsbDeviceConnection? = null
                var ok = false
                try {
                    conn = usb.openDevice(dev) ?: throw IllegalStateException("openDevice 返回空")
                    if (miriDevice == null) miriDevice = MiriSdrDevice()
                    try { miriConn?.close() } catch (_: Throwable) { }
                    miriConn = conn
                    // 增益：prefs 里是 dB，rtl_tcp 协议走 0.1dB 单位
                    val gainTenth = Math.round(prefs.getFloat("gain", 19.7f) * 10f)
                    val freqHz = Math.round(prefs.getFloat("freq", FREQ_MHZ.toFloat()) * 1e6)
                    // 取数方式用自检试出来的那个（ISOC / BULK 哪个能出数据）
                    val mode = try { miriDevice!!.preferredMode() } catch (t: Throwable) { null } ?: "ISOC"
                    ok = miriDevice!!.openAsync(miriDevice!!.handle(), conn.fileDescriptor,
                        gainTenth, 960000L, freqHz, 1234, prefs.getInt("ppm", 0), 0,
                        "127.0.0.1", dev.deviceName, hw, mode)
                    if (!ok) lastErr = "驱动内部起流失败（取数方式 $mode）"
                } catch (t: Throwable) {
                    lastErr = t.message ?: t.toString()
                    ok = false
                }
                if (ok) {
                    main.post {
                        // 起好了就替用户把这三项设好，免得他不知道还要勾台架模式
                        prefs.edit().putBoolean("bench", true)
                            .putString("host", "127.0.0.1")
                            .putString("tuner", "OTHER").apply()
                        AlertDialog.Builder(this)
                            .setTitle("RSP1 驱动已启动")
                            .setMessage("驱动已在本机 127.0.0.1:1234 提供数据。\n\n" +
                                "已顺手帮你设好：台架模式 ✓、服务器地址 127.0.0.1、" +
                                "增益档位【其它/网络源】。\n\n" +
                                "现在点【开始接收】即可。要换回 RTL 电视棒：" +
                                "把设置里的【台架模式】取消勾选。")
                            .setPositiveButton("知道了", null)
                            .show()
                    }
                    return@Thread
                }
                // 这次没成：把连接还回去（native 那边已经把 USB 和监听端口都收了），歇一下再试
                try { conn?.close() } catch (_: Throwable) { }
                miriConn = null
                if (attempt == 1) {
                    android.util.Log.w("MiriSdrDriver", "第一次启动失败（" + lastErr + "），1 秒后重试")
                    try { Thread.sleep(1000) } catch (_: InterruptedException) { }
                }
            }
            val why = lastErr
            main.post {
                AlertDialog.Builder(this)
                    .setTitle("驱动没起来")
                    .setMessage("试了两次都没起来。\n\n设备：" + usbLine(dev) + "\n原因：" + why + "\n\n" +
                        "① 设备被占用：把别的 SDR / 收音机 / 电视 App 全清掉，拔了重插再试一次\n" +
                        "② 采样率或频率回读是 0：这颗板子的时钟/固件跟通用 Mirics 不一样，" +
                        "请把【自检结果】那一屏截图发我\n" +
                        "③ 还不行就把手机重启一次（USB 子系统偶尔会卡在占用状态）")
                    .setPositiveButton("知道了", null)
                    .show()
            }
        }.start()
    }
    // -------------------------------------------------------------- 列车接收历史
    /**
     * 历史操作总入口。
     *
     * 引擎在（正在接收/暂停）就调引擎 —— 内存里那份是最新的；引擎已经被丢弃
     * （停止接收之后）就直接调 lbj_triplog 的 *_at 入口读磁盘：历史本来就存在磁盘上，
     * 任何时候都该能看、能导出、能导入。
     */
    private fun histCall(method: String, vararg args: Any?): String {
        val eng = engine
        return try {
            if (eng != null) eng.callAttr(method, *args).toString()
            else Python.getInstance().getModule("lbj_triplog")
                .callAttr(method + "_at", histDir(), *args).toString()
        } catch (t: Throwable) {
            android.util.Log.e("LBJHIST", method + " 失败", t)
            ""
        }
    }

    private fun histText(): String = histCall("history_export", pendingHistFmt, pendingHistScope, 7)

    private fun loadHistDays(): List<String> {
        return try {
            val a = JSONObject(histCall("history_days_json")).optJSONArray("days") ?: JSONArray()
            (0 until a.length()).mapNotNull {
                a.optJSONObject(it)?.optString("date")?.takeIf { d -> d.isNotEmpty() }
            }
        } catch (t: Throwable) {
            emptyList()
        }
    }

    /** 重新读日期列表，尽量停在原来那一天（keepDate 为空 = 停当前这天）。 */
    private fun refreshDaysAndList(keepDate: String? = null) {
        val want = keepDate ?: histDays.getOrNull(histIdx) ?: ""
        histStatus?.text = "读取中…"
        Thread {
            val days = loadHistDays()
            main.post {
                histDays = days
                val i = if (want.isNotEmpty()) days.indexOf(want) else 0
                histIdx = if (i >= 0) i else 0
                refreshHistory()
            }
        }.start()
    }

    /** 读选中那天的记录（放后台线程：首次调用可能要等 Python 起来）。 */
    private fun refreshHistory() {
        val date = histDays.getOrNull(histIdx) ?: ""
        val st = histStatus
        val ad = histAdapter
        Thread {
            val trips = try {
                JSONObject(histCall("history_day_json", date)).optJSONArray("trips") ?: JSONArray()
            } catch (t: Throwable) {
                JSONArray()
            }
            main.post {
                histTrips = trips
                ad?.notifyDataSetChanged()
                val keep = prefs.getInt("histkeep", 0)
                st?.text = (if (date.isEmpty()) "今天" else date) +
                    String.format(Locale.US, "　共 %d 趟\n保留：%s　点一行看详情，导出/导入在下面按钮",
                        trips.length(), if (keep <= 0) "永久" else keep.toString() + " 天")
            }
        }.start()
    }

    private fun histMove(delta: Int) {
        if (histDays.isEmpty()) { toast("还没有任何记录"); return }
        val n = histIdx + delta
        if (n < 0 || n >= histDays.size) { toast(if (delta > 0) "已经是最早的一天" else "已经是最近的一天"); return }
        histIdx = n
        refreshHistory()
    }

    private fun showHistory() {
        val pad = (resources.displayMetrics.density * 16).toInt()
        val col = LinearLayout(this)
        col.orientation = LinearLayout.VERTICAL
        col.setPadding(pad, pad / 2, pad, 0)
        val st = TextView(this)
        st.text = "读取中…"
        col.addView(st)
        histStatus = st

        val bar = LinearLayout(this)
        bar.orientation = LinearLayout.HORIZONTAL
        val bPrev = Button(this).apply { isAllCaps = false; text = "◀ 前一天" }
        bPrev.setOnClickListener { histMove(1) }
        val bNext = Button(this).apply { isAllCaps = false; text = "后一天 ▶" }
        bNext.setOnClickListener { histMove(-1) }
        val bClr = Button(this).apply { isAllCaps = false; text = "清空这天" }
        bClr.setOnClickListener { confirmHistClear() }
        for (b in arrayOf(bPrev, bNext, bClr)) {
            bar.addView(b, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        }
        col.addView(bar)

        val lv = ListView(this)
        histAdapter = HistAdapter()
        lv.adapter = histAdapter
        lv.setOnItemClickListener { _, _, i, _ -> showHistDetail(i) }
        col.addView(lv, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            (resources.displayMetrics.density * 300).toInt()))

        histDialog = AlertDialog.Builder(this)
            .setTitle("列车接收历史（点一行看详情）")
            .setView(col)
            .setPositiveButton("导出", null)
            .setNeutralButton("导入", null)
            .setNegativeButton("关闭", null)
            .create()
        histDialog?.setOnShowListener {
            // ★ 自己换监听：默认行为会在点完就自动关窗，而"导出"还要选范围/格式、
            //   "导入"还要选文件，窗先没了很别扭
            histDialog?.getButton(AlertDialog.BUTTON_POSITIVE)?.setOnClickListener { showHistExportDialog() }
            histDialog?.getButton(AlertDialog.BUTTON_NEUTRAL)?.setOnClickListener {
                histOpen.launch(arrayOf("text/*", "application/json", "application/octet-stream"))
            }
        }
        histDialog?.show()
        refreshDaysAndList()
    }

    /** 一行一条记录：车次 · 方向 / 起止时间 / 起止公里标 / 报文数 */
    private inner class HistAdapter : BaseAdapter() {
        override fun getCount(): Int = histTrips.length()
        override fun getItem(position: Int): Any = position
        override fun getItemId(position: Int): Long = position.toLong()
        override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
            val v = convertView ?: layoutInflater.inflate(R.layout.view_ch_row, parent, false)
            val t = histTrips.optJSONObject(position) ?: JSONObject()
            v.findViewById<TextView>(R.id.chIndex).text = String.format(Locale.US, "%02d", position + 1)
            val dir = t.optString("direction", "")
            val cat = t.optString("category", "")
            v.findViewById<TextView>(R.id.chName).text = t.optString("train", "----") +
                (if (dir.isEmpty()) "" else "  " + dir) + (if (cat.isEmpty()) "" else "  " + cat)
            v.findViewById<TextView>(R.id.chInfo).text = String.format(Locale.US,
                "%s~%s   %s→%s km   %d 条",
                t.optString("first_time", "--:--:--"), t.optString("last_time", "--:--:--"),
                t.optString("start_km", "?"), t.optString("end_km", "?"), t.optInt("n_msg"))
            v.findViewById<TextView>(R.id.chInfo).setTextColor(getColor(R.color.dim))
            return v
        }
    }

    private fun showHistDetail(i: Int) {
        val t = histTrips.optJSONObject(i) ?: return
        val sb = StringBuilder()
        // ★ 这里【不能】用 t.opt(key) 判空：org.json 在「键不存在 / 值为 null」时返回的是
        //   JSONObject.NULL 这个哨兵对象，而不是 Java 的 null。拿它去 String.format 会抛
        //   IllegalFormatConversionException 直接闪退 —— 真机实测：点没有经纬度的记录必崩。
        //   所以统一走下面两个安全取值：字符串把 null 当空串，数字先 isNull 再取。
        fun s(k: String): String = t.optString(k, "").let { if (it == "null") "" else it }
        fun d(k: String): Double? =
            if (t.isNull(k)) null else t.optDouble(k, Double.NaN).takeIf { !it.isNaN() }
        fun add(k: String, v: String) {
            if (v.isNotEmpty() && v != "null") sb.append(k).append("：").append(v).append('\n')
        }
        fun pair(k: String, a: String, b: String) {
            if (a.isEmpty() && b.isEmpty()) return
            add(k, if (b.isEmpty() || a == b) a else a + " → " + b)
        }
        fun pos(lon: Double?, lat: Double?): String =
            if (lon == null || lat == null) "" else String.format(Locale.US, "%.4f, %.4f", lon, lat)
        add("日期", s("date"))
        add("车次", s("train"))
        add("类别", s("category"))
        pair("方向", s("direction"), s("direction_last"))
        pair("机车", s("loco"), s("loco_last"))
        add("线路", s("route"))
        add("通联起止", s("first_time") + " ~ " + s("last_time"))
        pair("公里标", s("start_km"), s("end_km"))
        val mn = d("min_km"); val mx = d("max_km")
        if (mn != null && mx != null && mn != mx) {
            add("公里标范围", String.format(Locale.US, "%.1f ~ %.1f", mn, mx))
        }
        pair("端位", s("end_pos_first"), s("end_pos_last"))
        val p1 = pos(d("lon_first"), d("lat_first"))
        val p2 = pos(d("lon_last"), d("lat_last"))
        if (p1.isEmpty()) {
            add("经纬度", "这几条报文里没有（只有扩展帧才带）")
        } else {
            add("经纬度（首次）", p1)
            if (p2.isNotEmpty() && p2 != p1) add("经纬度（最后）", p2)
        }
        add("报文数", t.optInt("n_msg").toString())
        val sp = d("speed_max")
        if (sp != null && sp >= 0.0) add("最大速度", String.format(Locale.US, "%.0f km/h", sp))
        AlertDialog.Builder(this)
            .setTitle("车次 " + s("train"))
            .setMessage(sb.toString())
            .setPositiveButton("关闭", null)
            .show()
    }

    private fun showHistExportDialog() {
        val items = arrayOf(
            "今天 · CSV（Excel 直接打开）", "最近 7 天 · CSV", "全部 · CSV",
            "今天 · JSON（以后可再导入）", "最近 7 天 · JSON", "全部 · JSON")
        AlertDialog.Builder(this)
            .setTitle("导出历史")
            .setItems(items) { _, w ->
                pendingHistFmt = if (w >= 3) "json" else "csv"
                pendingHistScope = when (w % 3) { 0 -> "today"; 1 -> "recent"; else -> "all" }
                val today = LocalDate.now().toString()
                val tag = when (pendingHistScope) {
                    "today" -> today
                    "recent" -> "最近7天-" + today
                    else -> "全部-" + today
                }
                histCreate.launch("LBJ列车历史-" + tag + "." + pendingHistFmt)
            }
            .setNegativeButton(R.string.ch_cancel, null)
            .show()
    }

    private fun confirmHistClear() {
        val date = histDays.getOrNull(histIdx) ?: ""
        val label = if (date.isEmpty()) "今天" else date
        AlertDialog.Builder(this)
            .setTitle("清空 " + label + " 的接收记录？")
            .setPositiveButton("清空") { _, _ ->
                Thread {
                    val r = try { JSONObject(histCall("history_clear", date)) } catch (t: Throwable) { JSONObject() }
                    main.post {
                        toast(if (r.optBoolean("ok", false))
                            String.format(Locale.US, "已清空 %d 条", r.optInt("removed"))
                        else "清空失败：" + r.optString("why", ""))
                        refreshDaysAndList()
                    }
                }.start()
            }
            .setNegativeButton(R.string.ch_cancel, null)
            .show()
    }

    // 导出：系统的"新建文件"选择器（不需要存储权限，位置用户自己定）
    private val histCreate = registerForActivityResult(
        ActivityResultContracts.CreateDocument("text/plain")
    ) { uri ->
        val u = uri ?: return@registerForActivityResult
        Thread {
            try {
                val text = histText()
                contentResolver.openOutputStream(u)?.use { it.write(text.toByteArray(Charsets.UTF_8)) }
                main.post { toast("已导出：" + pendingHistFmt.uppercase(Locale.US) + "，" + text.length + " 字符") }
            } catch (t: Throwable) {
                main.post { toast("导出失败：" + (t.message ?: "")) }
            }
        }.start()
    }

    // 导入：系统的"打开文件"选择器，CSV/JSON 都认
    private val histOpen = registerForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri ->
        val u = uri ?: return@registerForActivityResult
        Thread {
            val text = try {
                contentResolver.openInputStream(u)?.bufferedReader()?.use { it.readText() } ?: ""
            } catch (t: Throwable) {
                ""
            }
            val r = try { JSONObject(histCall("history_import", text)) } catch (t: Throwable) { JSONObject() }
            main.post {
                if (r.optBoolean("ok", false)) {
                    toast(String.format(Locale.US, "导入完成：新增 %d 条，跳过 %d 条",
                        r.optInt("added"), r.optInt("skipped")))
                    if (histDialog?.isShowing == true) refreshDaysAndList() else showHistory()
                } else {
                    toast("导入失败：" + r.optString("why", "文件无法识别"))
                }
            }
        }.start()
    }
    private fun showSettings() {
        val pad = (resources.displayMetrics.density * 16).toInt()
        val box = LinearLayout(this)
        box.orientation = LinearLayout.VERTICAL
        box.setPadding(pad, pad, pad, pad)

        // 这里原本有一行"带宽 35 kHz · DC 避让 50 kHz · 采样 960 kS/s"。
        // 已删除：三项都是参考实现的固定默认值，用户改不了，
        // 而采样率在主界面状态栏本来就显示着（S:960k）。
        // 去掉后对话框变短，也缓解了下方"告警距离"被挤出可视区的问题。
        // 这三项的含义记在 docs/28 与 docs/11 里，需要时查文档即可。

        val cBench = check(box, "台架模式（不拉起驱动，直接连下面的服务器地址）", prefs.getBoolean("bench", false))

        // ★ 内置驱动开关。勾上后由本 App 自己起 rtl_tcp（不需要装外部驱动 App）；
        //   不勾走原来的 iqsrc:// 拉起外部驱动 App —— 这条回退路径必须保留。
        //   台架模式优先级更高：它连的是外部服务器，两条路不会同时生效。
        val cBuiltin = check(box, "使用内置驱动（本 App 自带 rtl_tcp，无需外部驱动 App；首次插棒需允许 USB 授权）",
            prefs.getBoolean("builtin", true))

        val eHost = textField(box, "服务器地址（★ 仅台架模式有效；不勾台架时一律连本机 127.0.0.1）",
            prefs.getString("host", "127.0.0.1") ?: "127.0.0.1", kHost)
        // 标题里直接写出默认频点：全国铁路的 LBJ 几乎都用这个频率，
        // 绝大多数人根本不需要改，改错了反而完全收不到。
        val eFreq = numField(box, "频率 MHz（★ 默认 821.2375 = 全国铁路统一频点）",
            prefs.getFloat("freq", FREQ_MHZ.toFloat()).toString())
        // ★ 两个调谐器的有效档位完全不同，标题里都要写准：
        //   R820T：0.0 ~ 49.6 dB（29 档）
        //   FC0013：-9.9 ~ 19.7 dB（23 档，而且有【负增益】档）
        //   之前写"FC0013 最大 15.7"是错的 —— 15.7 只是参考实现的默认值，不是它的上限。
        val eGain = numField(box, "增益 dB（R820T 0 ~ 49.6；FC0013 -9.9 ~ 19.7，含负档）",
            prefs.getFloat("gain", 19.7f).toString(), signed = true)

        // 这里曾经有个「自动增益（AGC）」开关，已删除。
        // 原因（全部实测，数据见 docs/41 第二十二节「数字版实测」）：
        //   FC0013 的 AGC 在这根棒上就是"顶到最大增益 + 削顶"—— 30 秒积分量 CNR，
        //   它三轮都比手动 19.7 低 0.3~0.5 dB，却把电平抬高 20 dB、削顶 4%；
        //   手动低档（-9.9 / 5.8 / 7.1）同样实测更差。也就是它没有任何可用场景，
        //   一个"一般不用勾"的开关不如不存在。要恢复，先看那节的数据。
        // ★ 实测做法（比原来那句"调到 PK Δ→0"靠谱得多）：
        //   PK 只在调谐频率 ±6.25kHz 的窗口里找峰值，频偏一大它就只看得到噪声，
        //   拿它当依据会得出完全错误的结论（本机就被这么误导过，以为只偏 -5ppm）。
        //   正确办法：用对讲机/已知信号，把频率调到能听清为止，
        //   偏移 Δ(Hz) ÷ 频率(Hz) × 1e6 就是 ppm。
        //   ● 符号必须实试：本机晶振偏快，要填【负】值（实测 -26），
        //     填反了会偏得更远（+26 时直接偏到姥姥家）。
        // 一句能照着算的白话就够了；不要再塞算例（用户反馈例子反而啰嗦）。
        val ePpm = numField(box, "PPM 校正（偏了多少 Hz ÷ 频率 Hz × 1000000）",
            prefs.getInt("ppm", 0).toString(), signed = true)
        val eThr = numField(box, "RSSI 门控阈值 dB（-140 ~ 0；收不到就调低到 -65）",
            prefs.getFloat("thr", -55f).toString(), signed = true)
        val eHold = numField(box, "门控释放保持 ms（0 ~ 10000；包中间断开就加大）",
            prefs.getFloat("hold", 700f).toString())
        // 增益档位表：决定了填进去的 dB 怎么"吸附"到设备真有的档位上。
        //   FC0013/FC0012 与 R820T 的档位完全不同（FC0013 还有负档）；
        //   RSP1/RSP2/Airspy 这类【非 RTL】设备走网络源，档位 App 不知道 ——
        //   选"其它/网络源"就不吸附，填多少原样转给服务器。
        val tunerNames = arrayOf("FC0013/FC0012", "R820T", "其它/网络源(RSP1 等)")
        val tunerVals = arrayOf("FC0013", "R820T", "OTHER")
        var tunerIdx = tunerVals.indexOf(prefs.getString("tuner",
            if (prefs.getBoolean("fc0013", true)) "FC0013" else "R820T")).coerceAtLeast(0)
        val btnTuner = Button(this)
        btnTuner.isAllCaps = false
        btnTuner.text = "增益档位：" + tunerNames[tunerIdx]
        btnTuner.setOnClickListener {
            tunerIdx = (tunerIdx + 1) % tunerNames.size
            btnTuner.text = "增益档位：" + tunerNames[tunerIdx]
        }
        box.addView(btnTuner)
        val cAfc = check(box, "启用 AFC 自动频率跟踪", prefs.getBoolean("afc", true))
        val cBeep = check(box, "提示音：每解出一趟车次响一声（走媒体音量，可调为 0）", prefs.getBoolean("beep", true))
        val cAlarm = check(box, "接近告警音 + 振动：进入告警距离时急促提示", prefs.getBoolean("alarm", true))
        val eAlarmKm = numField(box, "告警距离 km（正在接近且距离小于它 → 急促告警音）",
            prefs.getFloat("alarmkm", 5f).toString())
        val eKm = numField(box, "全局本站公里标 km（0 ~ 9999.9，可留空）",
            prefs.getFloat("mykm", -1f).let { if (it < 0) "" else it.toString() })
        val eRoute = textField(box, "按线路设公里标，格式 线路=公里标（如 京沪线=0123.4）",
            prefs.getString("routekm", "") ?: "", kRoute)
        // 列车接收历史保留天数：0 = 永久保留（默认）
        val eHist = numField(box, "列车历史保留天数（0 = 永久保留；填 180 就只留半年）",
            prefs.getInt("histkeep", 0).toString())

        // RSP1 / RSP1A / RSP2（Mirics 芯片）用的入口：自检 + 启动本机 rtl_tcp 服务。
        // 它不是"内部驱动"那条路，而是"本机外部服务器"：起好之后按【台架模式 + 127.0.0.1】接收。
        val btnRsp = Button(this)
        btnRsp.isAllCaps = false
        btnRsp.text = "RSP1 / RSP2（Mirics 芯片）自检 / 启动驱动（长按看 USB 设备列表）"
        btnRsp.setOnClickListener { rsp1Action() }
        // 长按 = 不管认不认识，直接看整条 USB 总线上挂着什么（远程排查用）
        btnRsp.setOnLongClickListener {
            val usb = getSystemService(Context.USB_SERVICE) as UsbManager
            showUsbDeviceListDialog(usb.deviceList.values.toList())
            true
        }
        box.addView(btnRsp)

        val view = ScrollView(this)
        view.addView(box)

        val dlg = AlertDialog.Builder(this)
            .setTitle(R.string.settings)
            .setView(view)
            .setPositiveButton("保存并应用", null)
            .setNegativeButton("取消", null)
            .create()

        dlg.setOnShowListener {
            dlg.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                // ---------------- 逐项校验，不合法就不保存 ----------------
                var ok = true
                fun chk(e: EditText, cond: Boolean, msg: String) {
                    if (cond) e.error = null else { e.error = msg; ok = false }
                }

                val host = eHost.text.toString().trim().ifEmpty { "127.0.0.1" }
                chk(eHost, Regex("^[A-Za-z0-9][A-Za-z0-9._:-]*$").matches(host), "地址格式不对")

                val freq = eFreq.text.toString().trim().toFloatOrNull()
                chk(eFreq, freq != null && freq >= 0.1f && freq <= 2000f, "请输入 0.1 ~ 2000")

                val gain = eGain.text.toString().trim().toFloatOrNull()
                // 允许负值：FC0013 有 -9.9 ~ -5.4 的负增益档（强信号时用它降互调）
                chk(eGain, gain != null && gain >= -10f && gain <= 50f, "请输入 -10 ~ 50")

                val ppm = ePpm.text.toString().trim().toIntOrNull()
                chk(ePpm, ppm != null && ppm >= -100 && ppm <= 100, "请输入 -100 ~ 100 的整数")

                val thr = eThr.text.toString().trim().toFloatOrNull()
                chk(eThr, thr != null && thr >= -140f && thr <= 0f, "请输入 -140 ~ 0")

                val hold = eHold.text.toString().trim().toFloatOrNull()
                chk(eHold, hold != null && hold >= 0f && hold <= 10000f, "请输入 0 ~ 10000")

                val kmTxt = eKm.text.toString().trim()
                val km = if (kmTxt.isEmpty()) -1f else kmTxt.toFloatOrNull()
                chk(eKm, km != null && (km < 0f || km <= 9999.9f), "请输入 0 ~ 9999.9，或留空")

                val alarmKm = eAlarmKm.text.toString().trim().toFloatOrNull()
                chk(eAlarmKm, alarmKm != null && alarmKm > 0f && alarmKm <= 100f, "请输入 0.1 ~ 100")

                val hist = eHist.text.toString().trim().toIntOrNull()
                chk(eHist, hist != null && hist >= 0 && hist <= 36500, "请输入 0 ~ 36500（0 = 永久）")

                val routeSpec = eRoute.text.toString().trim()
                var routeName = ""
                var routeVal = ""
                if (routeSpec.isNotEmpty()) {
                    val idx = routeSpec.indexOf('=')
                    if (idx <= 0) {
                        eRoute.error = "格式应为 线路=公里标"; ok = false
                    } else {
                        routeName = routeSpec.substring(0, idx).trim()
                        routeVal = routeSpec.substring(idx + 1).trim()
                        val rv = routeVal.toFloatOrNull()
                        if (routeName.isEmpty() || rv == null || rv < 0f || rv > 9999.9f) {
                            eRoute.error = "公里标应为 0 ~ 9999.9"; ok = false
                        }
                    }
                }

                if (!ok) {
                    toast("有输入不合法，请看对应输入框的红色提示")
                    return@setOnClickListener
                }

                // 内置驱动开关是"下次启动"才生效的：运行中改了不会立刻换驱动，
                // 和服务器地址一样，必须【停止】后再【开始接收】，否则用户以为切过去了。
                val builtinChanged = cBuiltin.isChecked != prefs.getBoolean("builtin", true)
                prefs.edit().putBoolean("bench", cBench.isChecked)
                    .putBoolean("builtin", cBuiltin.isChecked)
                    .putString("tuner", tunerVals[tunerIdx])
                    .putBoolean("fc0013", tunerVals[tunerIdx] == "FC0013")
                    .putString("host", host)
                    .putFloat("freq", freq!!).putFloat("gain", gain!!).putInt("ppm", ppm!!)
                    .putFloat("thr", thr!!).putFloat("hold", hold!!).putBoolean("afc", cAfc.isChecked)
                    .putBoolean("beep", cBeep.isChecked)
                    .putBoolean("alarm", cAlarm.isChecked).putFloat("alarmkm", alarmKm!!)
                    .putFloat("mykm", km!!).putString("routekm", routeSpec)
                    .putInt("histkeep", hist!!).apply()

                Thread {
                    try {
                        val eng = engine
                        // 停止之后 engine 已解绑，且此时改的只是 prefs，
                        // 下一个引擎会从 prefs 读 —— 不能再谎报"已应用"。
                        if (eng == null || !running) {
                            main.post { toast("已保存，开始接收后生效") }
                            return@Thread
                        }
                        // set_host 返回"要不要重启才生效"：TCP 连接是在 setup() 里
                        // 建立的，运行中改地址并不会重连。不提示的话，用户以为已经切回
                        // 本机驱动，实际还在从旧地址取数据（这正是台架模式那个 bug 的运行中版本）。
                        val needRestart = eng.callAttr("set_host", effectiveHost()).toBoolean() ||
                            builtinChanged
                        // ★ 调谐器型号必须【先于】增益下发：set_tuner 内部会按新表重新吸附一次增益，
                        //   顺序反了的话用户刚填的增益会被再吸附一次（虽然结果一样，但语义不清）。
                        eng.callAttr("set_tuner", tunerVals[tunerIdx])
                        eng.callAttr("set_frequency", freq.toDouble())
                        val actual = eng.callAttr("set_gain", gain.toDouble()).toDouble()
                        prefs.edit().putFloat("gain", actual.toFloat()).apply()
                        eng.callAttr("set_ppm", ppm)
                        eng.callAttr("set_threshold", thr.toDouble())
                        eng.callAttr("set_hold_ms", hold.toDouble())
                        eng.callAttr("set_afc_enabled", cAfc.isChecked)
                        if (km >= 0f) eng.callAttr("set_my_km", km.toDouble())
                        else eng.callAttr("set_my_km", null)
                        // 走统一入口：它还会把"这一轮不再出现"的旧线路清掉。
                        // 光调 set_route_km 的话，用户清空输入框后旧值仍然生效。
                        applyRouteKm(eng)
                        eng.callAttr("set_history_keep_days", hist)
                        main.post {
                            when {
                                needRestart ->
                                    toast("已应用；但改服务器地址或换驱动来源，都要【停止】后重新【开始接收】才生效")
                                // ★ 只有增益真的被硬件档位吸附过（你填的值 ≠ 实际值）才说明它。
                                //   以前每次保存都报"增益实际 19.7 dB"，哪怕根本没改增益，很吵（用户反馈）。
                                kotlin.math.abs(actual - gain.toDouble()) >= 0.05 ->
                                    toast("已应用（增益吸附到 %.1f dB —— 该调谐器没有你填的那一档）".format(actual))
                                else -> toast("已应用")
                            }
                        }
                    } catch (t: Throwable) {
                        main.post { toast("应用失败：" + (t.message ?: "")) }
                    }
                }.start()
                dlg.dismiss()
            }
        }
        dlg.show()
    }

    // ---------------------------------------------- 关注车次 / 关注模式 / 乘车模式
    private fun showKeywordDialog() {
        val pad = (resources.displayMetrics.density * 16).toInt()
        val box = LinearLayout(this)
        box.orientation = LinearLayout.VERTICAL
        box.setPadding(pad, pad, pad, pad)

        val eKw = textField(box, "关注的车次或机车，逗号分隔，留空=全部（可输字母/汉字/数字）",
            prefs.getString("kw", "") ?: "", kKeyword)
        // ★ 乘车模式：自己坐的车次。
        //   坐上车之后本车会周期性重发，不屏蔽的话上面的大面板一直被它刷屏，
        //   旁边路过的车次反而看不见。填进来后：本车【只在】下面「最近列车」
        //   持续更新，上面留给别的车；提示音也不会被本车触发。
        val eRide = textField(box,
            "乘车模式：自己乘坐的车次，逗号分隔（本车只在下面「最近列车」更新，不刷上面）",
            prefs.getString("ridetrain", "") ?: "", kKeyword)
        val cStrict = check(box, "关注模式：严格（只显示关注命中的车，否则留空）", prefs.getString("mode", "highlight") == "strict")
        val cBlock = check(box, "错包拦截（丢弃 BCH 无法纠正的报文）", prefs.getBoolean("strict", true))
        val cWarn = check(box, "干扰预警（探测到严重干扰时提示）", prefs.getBoolean("errwarn", true))

        // 多了一个输入框，小屏上可能顶到边；包一层 ScrollView 和设置对话框保持一致
        val view = ScrollView(this)
        view.addView(box)

        AlertDialog.Builder(this)
            .setTitle(R.string.keyword)
            .setView(view)
            .setPositiveButton("保存并应用") { _, _ ->
                // 全角逗号/顿号归一化成半角。
                // 输入白名单允许全角逗号（中文输入法下最自然的写法），
                // 但引擎按半角逗号切分 —— 不归一化的话"京A1，京B2"会被当成
                // 一整个关键词，永远匹配不上；再配合严格模式就等于完全收不到车。
                val kw = eKw.text.toString().trim()
                    .replace('，', ',').replace('、', ',').replace('；', ',')
                val ride = eRide.text.toString().trim()
                    .replace('，', ',').replace('、', ',').replace('；', ',')
                val mode = if (cStrict.isChecked) "strict" else "highlight"
                prefs.edit().putString("kw", kw).putString("mode", mode)
                    .putString("ridetrain", ride)
                    .putBoolean("strict", cBlock.isChecked)
                    .putBoolean("errwarn", cWarn.isChecked).apply()
                val eng = engine
                if (eng == null) { toast("已保存，开始接收后生效"); return@setPositiveButton }
                Thread {
                    try {
                        eng.callAttr("set_keywords", kw)
                        eng.callAttr("set_filter_mode", mode)
                        eng.callAttr("set_strict_filter", cBlock.isChecked)
                        eng.callAttr("set_err_warn", cWarn.isChecked)
                        // 放最后：见 applyPrefs 里同一处说明
                        eng.callAttr("set_ride_trains", ride)
                        main.post { toast("已应用") }
                    } catch (t: Throwable) {
                        main.post { toast("应用失败：" + (t.message ?: "")) }
                    }
                }.start()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    // -------------------------------------------------------------- 渲染

    /**
     * 把引擎报的原始错误翻译成【当前模式下用户到底该检查什么】。
     *
     * 参考实现里连接失败的文案是写死的"【连接被拒】请确认是否已授权 USB"。
     * 但台架模式下压根没有 USB 什么事 —— 手机连的是电脑上的模拟服务器。
     * 原样显示会让用户去查一个完全无关的东西，查半天也查不出结果。
     * 两种模式的排查方向完全相反，必须分开说。
     */
    private fun friendlyError(raw: String): String {
        val isConn = raw.contains("连接被拒") || raw.contains("TCP连接断开") ||
                     raw.contains("等待数据流超时")
        if (!isConn) return raw
        return if (prefs.getBoolean("bench", false)) {
            val host = effectiveHost()
            "连不上台架服务器 $host:1234\n" +
            "请依次检查：\n" +
            "① 电脑上是否已运行 tools/fake_rtl_tcp.py（要用 --bind 0.0.0.0）\n" +
            "② 手机和电脑是否连在同一个 WiFi\n" +
            "③ 上面的服务器地址是否就是电脑【当前】的 IP\n" +
            "（电脑换网络后 IP 会变，这是最常见的原因）"
        } else {
            "连不上本机 RTL-SDR 驱动 127.0.0.1:1234\n" +
            "请依次检查：\n" +
            "① 是否已安装 RTL-SDR 驱动 App\n" +
            "② 电视棒是否插好，并在弹窗里允许了 USB 权限\n" +
            "③ 驱动是否卡住了 —— 这种情况最常见：\n" +
            "    到「设置 → 应用」里把 RTL-SDR 驱动『强行停止』，再回来点开始接收。\n" +
            "    （只是重新打开驱动界面没有用，必须强行停止或重插电视棒）\n" +
            "④ 也可以把电视棒拔下来重插一次"
        }
    }

    private var lastLogMs = 0L

    private var lastRenderErrMs = 0L

    private fun render(json: String) {
        try { renderInner(json) } catch (t: Throwable) {
            // 不能静默：界面会莫名其妙停在某一帧不再刷新，用户完全无从判断。
            android.util.Log.e("LBJSTATE", "render 失败: " + t, t)
            val now = System.currentTimeMillis()
            if (now - lastRenderErrMs > 5000) {      // 节流，避免刷屏盖掉正常状态
                lastRenderErrMs = now
                tvHeader.text = "界面渲染出错（已跳过该帧）：" + (t.message ?: t.toString())
            }
        }
    }

    private fun renderInner(json: String) {
        val o = try { JSONObject(json) } catch (t: Throwable) {
            android.util.Log.e("LBJSTATE", "JSON 解析失败: " + json.take(200), t); return
        }

        // 台架模式下把状态镜像到 logcat，便于用 adb 直接验证解算结果
        if (prefs.getBoolean("bench", false)) {
            val now = System.currentTimeMillis()
            if (now - lastLogMs >= 1000) {
                lastLogMs = now
                android.util.Log.i("LBJSTATE", json)
            }
        }

        val err = o.optString("error", "")
        val run = o.optBoolean("running", false)
        lastSnapshot = o
        // 数据源实际采样率不对（RSP1 等非 RTL 设备常见）：明确提示一次
        val rw = o.optString("rate_warn", "")
        if (rw.isNotEmpty() && rw != lastRateWarn) {
            lastRateWarn = rw
            toast(rw)
        }
        if (run != running) {
            running = run
            if (run) voiceSaid.clear()      // 新一场接收：允许重新播报同一趟车
            updateButtons()
        }

        // 历史按钮上的角标：今天已经归档了几趟（引擎侧统计，停止后按最后一次的数）
        val hist = o.optJSONObject("history")
        if (hist != null) {
            val n = hist.optInt("today", 0)
            val want = if (n > 0) "历史($n)" else "历史"
            if (btnHistory.text != want) btnHistory.text = want
        }

        val now = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())
        val kws = o.optJSONArray("keywords")
        val kwStr = if (kws == null || kws.length() == 0) "无" else
            (0 until kws.length()).joinToString(",") { kws.optString(it) }
        val modeStr = if (o.optString("filter_mode", "highlight") == "strict") "严格" else "高亮"
        val flags = StringBuilder()
        val rides = o.optJSONArray("ride_trains")
        if (rides != null && rides.length() > 0) flags.append(" [乘车]")
        if (!o.optBoolean("strict_filter", true)) flags.append(" [拦截关]")
        if (!o.optBoolean("show_err_warn", true)) flags.append(" [预警关]")
        // 用词要和右上角那颗【关注车次】按钮一致 —— 之前这里写"过滤:"，
        // 用户点的是"关注车次"，两处对不上，看着有歧义。
        tvHeader.text = "$now  关注:$kwStr ($modeStr)$flags" +
                (if (err.isNotEmpty()) "\n⚠ " + friendlyError(err) else "")

        // 干扰 / 错包预警（参考实现里只显示 2 秒）
        val warn = o.optString("warning", "")
        if (warn.isEmpty()) {
            tvWarning.visibility = View.GONE
        } else {
            tvWarning.visibility = View.VISIBLE
            tvWarning.text = warn
        }

        // ---------- 频谱 ----------
        val arr = o.optJSONArray("spectrum")
        if (arr != null && arr.length() > 0) {
            val vals = FloatArray(arr.length())
            for (i in 0 until arr.length()) vals[i] = arr.optDouble(i, -120.0).toFloat()
            val zoom = o.optDouble("zoom_hz", 150000.0)
            val delta = if (o.isNull("peak_delta_hz")) Double.NaN else o.optDouble("peak_delta_hz", 0.0)
            val marker = if (delta.isNaN()) -1f else (0.5 + delta / (2 * zoom)).toFloat().coerceIn(0f, 1f)
            spectrum.update(vals, marker, 0.5f)
        }

        // ---------- 状态栏（与参考实现 _u6 底部四行一致）----------
        tvS1.text = String.format(Locale.US, "▲ %.4fM  G:%.1f  P:%d  S:%dk",
            o.optDouble("freq_mhz", FREQ_MHZ), o.optDouble("gain_db", 0.0),
            o.optInt("ppm", 0), o.optInt("sample_rate_k", 960))

        val rssi = o.optDouble("rssi", -140.0)
        val thr = o.optDouble("cs_threshold", -138.0)
        tvS2.text = String.format(Locale.US, "RSSI:%.0f 阈:%.0f RX:%s H:%.0f",
            rssi, thr, o.optString("gate", "OFF"), o.optDouble("rssi_hold_ms", 0.0))
        tvS2.setTextColor(if (rssi > thr) 0xFF2ECC71.toInt() else 0xFF8B949E.toInt())

        val fe = o.optDouble("afc_err_hz", 0.0)
        val af = o.optDouble("afc_hz", 0.0)
        tvS3.text = String.format(Locale.US, "FERR:%+.0f AFC:%+.0f S:%.2f", fe, af, o.optDouble("afc_score", 0.0))
        tvS3.setTextColor(if (Math.abs(fe) > 50 || Math.abs(af) > 50) 0xFF58A6FF.toInt() else 0xFF8B949E.toInt())

        if (o.isNull("peak_hz")) {
            tvS4.text = "PK:---.------M Δ:---.-k ---dB"
        } else {
            tvS4.text = String.format(Locale.US, "PK:%.6fM Δ:%+.1fk %.0fdB",
                o.optDouble("peak_hz", 0.0) / 1e6, o.optDouble("peak_delta_hz", 0.0) / 1e3,
                o.optDouble("peak_db", -120.0))
        }

        // ---------- 车次区 ----------
        val hit = o.optBoolean("is_hit", false) && kws != null && kws.length() > 0
        val train = o.optString("train", "----")
        tvTrain.text = if (train.isEmpty()) "----" else train
        tvTrain.setTextColor(if (hit) 0xFFE74C3C.toInt() else 0xFFE6EDF3.toInt())
        tvCategory.text = o.optString("category", "")
        tvCategory.setTextColor(if (hit) 0xFFE74C3C.toInt() else 0xFF2ECC71.toInt())

        val pos = o.optString("position", "---.-")
        val spd = o.optString("speed", "---")

        var etaSec = "---"; var etaTime = "--:--:--"
        var distTxt = "---"; var statusTxt = o.optString("eta_status", "未设置线路位置")
        if (!o.isNull("eta")) {
            val e = o.getJSONObject("eta")
            etaSec = e.optInt("seconds", 0).toString() + "s"
            etaTime = e.optString("time", "--:--:--")
            distTxt = String.format(Locale.US, "%.1f km", e.optDouble("distance_km", 0.0))
            statusTxt = e.optString("status", statusTxt)
        }
        val epTxt = if (o.isNull("end_pos")) "---"
                    else o.optString("end_pos", "---").let { if (it.isEmpty() || it == "null") "---" else it }

        setCell(0, etaSec)
        setCell(1, etaTime)
        // 乘车模式下这一格装的是【本车当前位置】（引擎用本车公里标实时更新它），
        // 所以标签也从"本站"改成"位置"，免得让人以为是个固定车站。
        gridLabelViews[2]?.text = if (isRideMode()) "位置:" else "本站:"
        setCell(2, o.optString("current_route_km_text", "---"))
        setCell(3, distTxt)
        setCell(4, statusTxt)
        setCell(5, o.optString("direction", "未知"))
        setCell(6, if (spd == "---") "---" else "$spd km/h")
        setCell(7, if (pos == "---.-") "---" else "$pos km")
        setCell(8, o.optString("loco", "----"))
        setCell(9, o.optString("loco_code", "---"))
        setCell(10, o.optString("route", "----"))
        setCell(11, o.optString("category", "----"))
        setCell(12, epTxt)
        // 经纬度的字段格式出自参考工程的 README，而原作者能跑起来的代码里从未使用过它，
        // 所以属于"推断格式"。Python 侧已经做了范围校验：不在中国铁路范围内的整对丢弃。
        // 这里显示"存疑"而不是把解析错的坐标当真显示——同时也方便用真实信号去验证格式。
        val geoBad = o.optString("geo_bad", "")
        fun geoCell(key: String): String = when {
            !o.isNull(key) -> String.format(Locale.US, "%.4f", o.optDouble(key, 0.0))
            geoBad.isNotEmpty() -> "存疑"
            else -> "---"
        }
        setCell(13, geoCell("lon"))
        setCell(14, geoCell("lat"))
        // 第 16 格：当前线路已积累的里程样本数（GPS 定位要用，顺便把格子填满）
        val ls = o.optJSONObject("line_samples")
        var n = 0
        if (ls != null) { for (k in ls.keys()) n += ls.optInt(k, 0) }
        setCell(15, if (n <= 0) "0（收车中）" else n.toString() + " 组")

        // 接近时把"距离/状态"标黄，远离时标灰
        val near = (statusTxt == "接近" || statusTxt == "即将到达")
        val far = (statusTxt == "远离/已过")
        val c = when {
            near -> 0xFFF1C40F.toInt()
            far -> 0xFF8B949E.toInt()
            else -> 0xFFE6EDF3.toInt()
        }
        gridValues[3]?.setTextColor(c)
        gridValues[4]?.setTextColor(c)

        // 接近告警音
        maybeAlarm(o)

        // ---------- 最近列车 ----------
        renderTrains(o.optJSONArray("trains"))
    }

    private fun toast(msg: String) {
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
    }
}
