/*
 * LBJ Receiver —— Android 端 铁路列车接近预警接收机
 * Copyright (C) 2026 Scorpio-yzy
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * 本文件是 LBJ Receiver 的一部分，以 GPL-3.0-or-later 发布；详见 LICENSE 与 THIRD_PARTY.md。
 */

package com.railfan.lbj

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack

/**
 * 收音机的音频输出。
 *
 * Python 侧算好 48kHz 单声道 int16 PCM 后调用 write()，这里直接送进 AudioTrack。
 *
 * ★ 用 MODE_STREAM + 【非阻塞】write()。
 *
 * 以前这里是阻塞写，本意是"让 DSP 循自然被压到实时速度"。但真机上踩到了大坑：
 * 音频系统一旦抽风（焦点变化、路由切换、声道被别的 App 占住），阻塞写会一直卡着，
 * 而调用它的正是 Python 的 DSP 线程 —— DSP 卡住就不再读数据源，socket 于是不再被取走，
 * 驱动的发送缓冲很快填满，libusb 回调跟着被堵死，整条 USB 流永久停摆。
 * 现象就是：手台贴近发射（静噪打开、开始出声）之后"频谱卡住、App 还能点、但不再接收"。
 *
 * 现在改成非阻塞写：缓冲区满就丢掉这一块。听起来最多顿一下，绝不会把整条链路拖死。
 * （数据源本来就是实时的，DSP 不可能跑得比它快，所以不需要靠阻塞来节流。）
 */
class AudioSink(private val ctx: Context) {

    @Volatile private var track: AudioTrack? = null
    @Volatile private var vol = 0.8f
    private var am: AudioManager? = null

    fun start(): Boolean {
        stop()
        val rate = 48000
        val minBuf = AudioTrack.getMinBufferSize(
            rate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT
        )
        if (minBuf <= 0) return false
        // 约 340ms 缓冲：再短容易断音，再长按键反应就显得迟钝
        val buf = maxOf(minBuf * 4, rate / 2 * 2)
        val t = try {
            AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build()
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(rate)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .build()
                )
                .setBufferSizeInBytes(buf)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build()
        } catch (_: Throwable) {
            null
        } ?: return false

        t.setVolume(vol)
        try {
            t.play()
        } catch (_: Throwable) {
            try { t.release() } catch (_: Throwable) { }
            return false
        }
        track = t
        requestFocus()
        return true
    }

    /** 被丢掉的音频字节数（非阻塞写缓冲区满时累计，只用于诊断） */
    @Volatile var droppedBytes: Long = 0
        private set

    fun write(pcm: ByteArray) {
        val t = track ?: return
        try {
            val n = t.write(pcm, 0, pcm.size, AudioTrack.WRITE_NON_BLOCKING)
            if (n < pcm.size) {
                droppedBytes += (pcm.size - maxOf(n, 0)).toLong()
            }
        } catch (_: Throwable) {
        }
    }

    fun setVolume(v: Float) {
        vol = v.coerceIn(0f, 1f)
        try { track?.setVolume(vol) } catch (_: Throwable) { }
    }

    fun stop() {
        val t = track
        track = null
        if (t != null) {
            try { t.pause() } catch (_: Throwable) { }
            try { t.flush() } catch (_: Throwable) { }
            try { t.stop() } catch (_: Throwable) { }
            try { t.release() } catch (_: Throwable) { }
        }
        abandonFocus()
    }

    private fun requestFocus() {
        try {
            val m = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            am = m
            m.requestAudioFocus(null, AudioManager.STREAM_MUSIC, AudioManager.AUDIOFOCUS_GAIN)
        } catch (_: Throwable) { }
    }

    private fun abandonFocus() {
        try { am?.abandonAudioFocus(null) } catch (_: Throwable) { }
        am = null
    }
}
