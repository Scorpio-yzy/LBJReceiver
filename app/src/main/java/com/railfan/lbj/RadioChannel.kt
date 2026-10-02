package com.railfan.lbj

import android.content.SharedPreferences
import org.json.JSONArray
import org.json.JSONObject

/**
 * 一个信道。
 *
 * 设计上不切"航空/铁路/对讲"这类模式，而是像真收音机一样：
 * 每个频道起个自己的名字，把频率、制式、亚音、步进都存进去，随便改。
 */
data class RadioChannel(
    var name: String = "",
    var freqHz: Double = 457_000_000.0,
    var mode: String = "NFM",
    var stepHz: Double = 12_500.0,
    var ctcss: Double = 0.0,          // 0 = 关闭亚音
) {
    fun toJson(): JSONObject = JSONObject()
        .put("name", name)
        .put("freq", freqHz)
        .put("mode", mode)
        .put("step", stepHz)
        .put("ctcss", ctcss)

    companion object {
        const val COUNT = 100
        private const val KEY = "channels"

        /** 标准 CTCSS 亚音频率表（Hz），0 表示关闭 */
        val CTCSS_TONES: DoubleArray = doubleArrayOf(
            0.0, 67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5,
            91.5, 94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0,
            127.3, 131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5,
            167.9, 171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6,
            199.5, 203.5, 206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3,
            254.1
        )

        fun ctcssLabel(hz: Double): String =
            if (hz <= 0.0) "关" else String.format(java.util.Locale.US, "%.1f", hz)

        fun fromJson(o: JSONObject) = RadioChannel(
            name = o.optString("name", ""),
            freqHz = o.optDouble("freq", 457e6),
            mode = o.optString("mode", "NFM"),
            stepHz = o.optDouble("step", 12_500.0),
            ctcss = o.optDouble("ctcss", 0.0),
        )

        /** 默认给几个常见频道，其余留空由用户自己命名。用户说不要波段模式，所以只作起点。 */
        fun defaults(): MutableList<RadioChannel> {
            val list = MutableList(COUNT) { RadioChannel() }
            fun set(i: Int, n: String, mhz: Double, m: String, step: Double, ctcss: Double) {
                list[i] = RadioChannel(n, mhz * 1e6, m, step, ctcss)
            }
            set(0, "调机 1", 457.0000, "NFM", 12_500.0, 0.0)
            set(1, "调机 2", 457.5000, "NFM", 12_500.0, 0.0)
            set(2, "铁路平调", 450.0000, "NFM", 25_000.0, 0.0)
            set(3, "航空", 121.5000, "AM", 25_000.0, 0.0)
            set(4, "FM 广播", 98.7000, "WFM", 100_000.0, 0.0)
            set(5, "业余 2m", 145.0000, "NFM", 12_500.0, 88.5)
            set(6, "业余 70cm", 438.5000, "NFM", 12_500.0, 88.5)
            // 其余信道先给个中性起点，用户想怎么改都行
            for (i in 7 until COUNT) list[i] = RadioChannel("", 457.0000e6, "NFM", 12_500.0, 0.0)
            return list
        }

        fun load(prefs: SharedPreferences): MutableList<RadioChannel> {
            val s = prefs.getString(KEY, null) ?: return defaults()
            return try {
                val arr = JSONArray(s)
                val list = MutableList(COUNT) { RadioChannel() }
                for (i in 0 until COUNT) {
                    list[i] = if (i < arr.length()) fromJson(arr.getJSONObject(i)) else RadioChannel()
                }
                list
            } catch (_: Throwable) {
                defaults()
            }
        }

        fun save(prefs: SharedPreferences, list: List<RadioChannel>) {
            val arr = JSONArray()
            for (c in list) arr.put(c.toJson())
            prefs.edit().putString(KEY, arr.toString()).apply()
        }

        fun label(c: RadioChannel, idx: Int): String =
            if (c.name.isBlank()) String.format(java.util.Locale.US, "CH%02d", idx + 1) else c.name

        fun summary(c: RadioChannel, idx: Int): String = String.format(
            java.util.Locale.US, "%s  %.4f MHz  %s%s",
            label(c, idx), c.freqHz / 1e6, c.mode,
            if (c.ctcss > 0) "  亚音" + ctcssLabel(c.ctcss) else ""
        )
    }
}
