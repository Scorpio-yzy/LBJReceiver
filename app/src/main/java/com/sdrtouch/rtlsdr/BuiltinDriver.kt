/*
 * LBJ Receiver —— Android 端 铁路列车接近预警接收机
 * Copyright (C) 2026 Scorpio-yzy
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * 本文件是 LBJ Receiver 的一部分，以 GPL-3.0-or-later 发布；详见 LICENSE 与 THIRD_PARTY.md。
 */

package com.sdrtouch.rtlsdr

import android.content.Context
import android.content.Intent
import android.os.Build
import com.sdrtouch.core.SdrTcpArguments
import com.sdrtouch.core.devices.SdrDevice
import com.sdrtouch.rtlsdr.driver.RtlSdrDeviceProvider
import com.sdrtouch.tools.Log

/**
 * 「使用内置驱动」：把原本 rtl_tcp_andro 的 DeviceOpenActivity.startServer()
 * 那三步照搬进本进程，不再依赖外部驱动 App。
 *
 *   1) new RtlSdrDeviceProvider().loadNativeLibraries() + listDevices() 找设备
 *      （listDevices 走 UsbPermissionHelper，只看 device_filter 里的 VID/PID，
 *        不要求已授权 —— 授权会在真正打开设备时由 UsbPermissionObtainer 弹框申请）
 *   2) SdrTcpArguments.fromString("-a 127.0.0.1 -p ... -s ... -f ... -T 0")
 *   3) startForegroundService(BinaryRunnerService) + bindService(SdrServiceConnection)
 *
 * 放在 com.sdrtouch.rtlsdr 包里，是为了沿用驱动自己的包内接口
 * （SdrServiceConnection 的构造器），一行驱动源码都不用改。
 *
 * Python 侧完全无感：服务照样监听 127.0.0.1:1234，接口契约不变（A 方案）。
 */
object BuiltinDriver {

    private val provider = RtlSdrDeviceProvider()

    /** 当前这一轮的绑定；换一轮之前必须先解绑，否则两个连接会互相打架 */
    @Volatile private var connection: SdrServiceConnection? = null

    /** 保住最后一台设备的引用，别让 finalize() 在驱动还在跑时把 native 句柄回收了 */
    @Volatile private var device: SdrDevice? = null

    /** 最近一次失败的原因，供对话框显示；null 表示没记到错误 */
    @Volatile var lastError: String? = null
        private set

    /**
     * 起内置驱动。返回 true 只表示"服务已经拉起并绑上"，
     * 真正成没成要看随后对 127.0.0.1:1234 的那次 connect（与外部驱动路径同一个判据）。
     */
    fun start(ctx: Context, hwFreqHz: Long, port: Int, sampleRate: Int): Boolean {
        lastError = null
        val app = ctx.applicationContext

        // 先收拾上一轮：解绑（服务若再无别的客户端会自己收尾）
        unbind(ctx)

        if (!provider.loadNativeLibraries()) {
            lastError = "加载 librtlSdrAndroid.so 失败（本机 ABI 不在打包范围内？只打了 arm64-v8a）"
            return false
        }

        val devices: List<SdrDevice> = try {
            provider.listDevices(app, false)
        } catch (t: Throwable) {
            lastError = "枚举 USB 设备失败：" + (t.message ?: t.toString())
            return false
        }
        if (devices.isEmpty()) {
            lastError = "没有找到 RTL-SDR 电视棒（电视棒没插好，或 VID/PID 不在支持列表里）"
            return false
        }
        val dev = devices[0]

        val args: SdrTcpArguments = try {
            SdrTcpArguments.fromString("-a 127.0.0.1 -p " + port + " -s " + sampleRate +
                " -f " + hwFreqHz + " -T 0")
        } catch (t: Throwable) {
            lastError = "驱动参数不合法：" + (t.message ?: t.toString())
            return false
        }

        // 可选的状态监听：打开/关闭都记一笔，出错时能给用户一句人话
        dev.addOnStatusListener(object : SdrDevice.OnStatusListener {
            override fun onOpen(d: SdrDevice) {
                Log.appendLine("builtin driver open: " + d.name)
            }

            override fun onClosed(e: Throwable?) {
                if (e != null) {
                    lastError = "内置驱动已退出：" + (e.message ?: e.toString())
                    Log.appendLine("builtin driver closed: " + lastError)
                } else {
                    Log.appendLine("builtin driver closed normally")
                }
            }
        })

        val conn = SdrServiceConnection(dev, args) {
            // onServiceDisconnected：服务进程没了。成败仍以随后的 connect 为准。
            lastError = "内置驱动服务已被系统断开"
        }
        device = dev
        connection = conn

        return try {
            val si = Intent(app, BinaryRunnerService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ctx.startForegroundService(si)
            } else {
                ctx.startService(si)
            }
            if (!ctx.bindService(si, conn, Context.BIND_AUTO_CREATE)) {
                lastError = "绑定内置驱动服务失败"
                connection = null
                false
            } else {
                true
            }
        } catch (t: Throwable) {
            lastError = "启动内置驱动服务失败：" + (t.message ?: t.toString())
            connection = null
            false
        }
    }

    /** 解绑（幂等）。每轮重启驱动之前、以及 Activity 销毁时用。 */
    fun unbind(ctx: Context) {
        val c = connection ?: return
        connection = null
        try { ctx.unbindService(c) } catch (_: Throwable) { }
    }

    /**
     * 彻底停掉内置驱动：解绑 + stopService。
     *
     * ★ 只 unbind 是不够的：服务是 startForegroundService 起来的，
     *   解绑后它照样活着、照样监听 127.0.0.1:1234、照样占着 USB，
     *   通知栏那条"rtl_sdr"也一直挂着。
     *   切到【台架模式】时如果不调这个，就会出现"已经改用电脑上的模拟服务器了，
     *   本机却还在从 SDR 读"的假象（用户实际反馈过）。
     *   服务侧 onDestroy 会关掉设备、释放 USB。
     */
    fun stop(ctx: Context) {
        unbind(ctx)
        device = null
        try {
            ctx.stopService(Intent(ctx.applicationContext, BinaryRunnerService::class.java))
        } catch (_: Throwable) { }
    }
}
