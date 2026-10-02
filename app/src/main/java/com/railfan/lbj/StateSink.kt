/*
 * LBJ Receiver —— Android 端 铁路列车接近预警接收机
 * Copyright (C) 2026 Scorpio-yzy
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * 本文件是 LBJ Receiver 的一部分，以 GPL-3.0-or-later 发布；详见 LICENSE 与 THIRD_PARTY.md。
 */

package com.railfan.lbj

import android.os.Handler

/**
 * Python 侧的回调目标。
 * Chaquopy 会把本对象传给 Python，Python 调用 onState(json) 推送状态。
 */
class StateSink(private val main: Handler, private val onJson: (String) -> Unit) {

    @Suppress("unused")
    fun onState(json: String) {
        main.post { onJson(json) }
    }
}
