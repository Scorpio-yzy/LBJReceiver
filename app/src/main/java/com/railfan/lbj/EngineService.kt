package com.railfan.lbj

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat

/**
 * 收音机/预警器运行期间的前台服务。
 *
 * ★ 为什么必须有它：
 *   之前锁屏后还能出声，纯粹是【碰巧】—— Activity 进后台后进程和 AudioTrack
 *   还活着而已。没有前台服务，MIUI 的省电策略随时会冻结或杀掉进程
 *   （通常几分钟到几十分钟），Doze 也会限制，于是出现"以为还在听，
 *   其实早就不出声了"。前台服务是唯一可靠的做法。
 *
 * 只负责挂一个常驻通知把进程保活，音频和解码仍在主进程里跑，不跨进程。
 */
class EngineService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val text = intent?.getStringExtra(EXTRA_TEXT) ?: "接收中"
        startForeground(NOTI_ID, buildNotification(text))
        return START_STICKY
    }

    private fun buildNotification(text: String): Notification {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            if (nm.getNotificationChannel(CHANNEL) == null) {
                val ch = NotificationChannel(
                    CHANNEL, "接收中", NotificationManager.IMPORTANCE_LOW
                )
                ch.description = "收音机 / 列车预警器运行期间的常驻提示"
                ch.setShowBadge(false)
                nm.createNotificationChannel(ch)
            }
        }
        val pi = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setOngoing(true)
            .setShowWhen(false)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setContentIntent(pi)
            .build()
    }

    companion object {
        private const val CHANNEL = "lbj_running"
        private const val NOTI_ID = 1001
        private const val EXTRA_TEXT = "text"
        private var running = false

        /** 引擎（收音机或预警器）开始工作时调用 */
        fun start(ctx: Context, text: String) {
            val i = Intent(ctx, EngineService::class.java).putExtra(EXTRA_TEXT, text)
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    ctx.startForegroundService(i)
                } else {
                    ctx.startService(i)
                }
                running = true
            } catch (_: Throwable) {
                // 后台启动前台服务可能被系统拒绝（Android 12+ 有后台启动限制）。
                // 被拒时不要崩，只是没有保活，行为退回到从前。
            }
        }

        /** 两个引擎都停了才真正停掉 */
        fun stop(ctx: Context) {
            if (!running) return
            running = false
            try {
                ctx.stopService(Intent(ctx, EngineService::class.java))
            } catch (_: Throwable) {
            }
        }
    }
}
