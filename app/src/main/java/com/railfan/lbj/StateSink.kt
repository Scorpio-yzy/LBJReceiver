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
