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
 * ★ 用 MODE_STREAM + 阻塞式 write()：
 *   缓冲区写满时 write() 会阻塞，于是 Python 的 DSP 循环自然被压到实时速度，
 *   既不需要额外做节流，也不会因为跑得比数据源快而把队列抽干。
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

    fun write(pcm: ByteArray) {
        val t = track ?: return
        try {
            t.write(pcm, 0, pcm.size)
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
