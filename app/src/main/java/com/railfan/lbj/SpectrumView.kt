/*
 * LBJ Receiver —— Android 端 铁路列车接近预警接收机
 * Copyright (C) 2026 Scorpio-yzy
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * 本文件是 LBJ Receiver 的一部分，以 GPL-3.0-or-later 发布；详见 LICENSE 与 THIRD_PARTY.md。
 */

package com.railfan.lbj

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.util.AttributeSet
import android.view.View

/** 简易频谱柱状图：中心线 = 目标频率，黄线 = 目标区域内最强点 */
class SpectrumView @JvmOverloads constructor(
    context: Context, attrs: AttributeSet? = null, defStyle: Int = 0
) : View(context, attrs, defStyle) {

    private var bars: FloatArray = FloatArray(0)
    private var marker: Float = -1f
    private var center: Float = 0.5f

    private val barPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val gridPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = 0xFF30363D.toInt(); strokeWidth = 1f
    }
    private val linePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { strokeWidth = 2f }

    private var floorDb = -110f
    private var ceilDb = -20f

    fun update(values: FloatArray, markerFraction: Float, centerFraction: Float) {
        bars = values
        marker = markerFraction
        center = centerFraction
        // 动态量程：与参考实现 _u6 一致——取 10% 分位作底噪，跨度不足 20 dB 时补足
        if (values.isNotEmpty()) {
            val sorted = values.clone()
            sorted.sort()
            val lo = sorted[(sorted.size * 0.1f).toInt().coerceIn(0, sorted.size - 1)]
            var hi = sorted[sorted.size - 1]
            if (hi - lo < 20f) hi = lo + 20f
            floorDb = lo
            ceilDb = hi
        }
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val w = width.toFloat()
        val h = height.toFloat()
        if (w <= 0f || h <= 0f) return

        for (i in 1..3) {
            val y = h * i / 4f
            canvas.drawLine(0f, y, w, y, gridPaint)
        }
        if (bars.isEmpty()) return

        val n = bars.size
        val bw = w / n
        val span = (ceilDb - floorDb).coerceAtLeast(1f)
        for (i in 0 until n) {
            val t = ((bars[i] - floorDb) / span).coerceIn(0f, 1f)
            val bh = t * h
            barPaint.color = when {
                t > 0.72f -> 0xFFE74C3C.toInt()
                t > 0.42f -> 0xFFF1C40F.toInt()
                else -> 0xFF2ECC71.toInt()
            }
            val left = i * bw + 1f
            canvas.drawRect(left, h - bh, left + bw - 2f, h, barPaint)
        }

        linePaint.color = 0xFF58A6FF.toInt()
        canvas.drawLine(w * center, 0f, w * center, h, linePaint)

        if (marker in 0f..1f) {
            linePaint.color = 0xFFFFD33D.toInt()
            canvas.drawLine(w * marker, 0f, w * marker, h, linePaint)
        }
    }
}
