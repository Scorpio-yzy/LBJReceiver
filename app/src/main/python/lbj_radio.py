# -*- coding: utf-8 -*-
# LBJ Receiver —— Android 端 铁路列车接近预警接收机
# Copyright (C) 2026 Scorpio-yzy
# SPDX-License-Identifier: GPL-3.0-or-later
#
# 本文件是 LBJ Receiver 的一部分，以 GPL-3.0-or-later 发布；详见 LICENSE 与 THIRD_PARTY.md。

"""
收音机 / 对讲机接收引擎。

同一根电视棒，换一套 DSP：

    IQ 960k
      → 数字下变频（连续相位 NCO，改频不断音）
      → 半带÷2 → 半带÷2 → 240k
      → FIR÷5 → 48k 复数
      → 鉴频（NFM/WFM）或包络检波（AM）
      → 音频带通 / 去加重
      → 静噪 → 音量 → 48k int16 PCM → Android AudioTrack

解调方式：
    NFM  —— 窄带调频。对讲机、铁路调车（457MHz 就是这一段）、UV 段。
    AM   —— 调幅。航空 118~137MHz 用的就是它。
    WFM  —— 宽带调频。87~108MHz 广播，带去加重。

★ 复用预警器的连接，不重连驱动
    切模式时只停 DSP 线程、**保持 TCP 连接**（就是预警器那套暂停/恢复机制），
    所以来回切换不会把驱动搞成"连得上却不推数据"的僵尸状态。

★ 只依赖 firwin / lfilter
    Android 上不用真 scipy（见 lbj_engine 的兼容层），所以这里所有滤波器
    都用 FIR 实现。一阶 IIR（去加重）用其冲激响应的 FIR 截断近似，
    50µs 时间常数在 48k 下约 22 个抽头就衰减到 1e-4 以下。
"""
import json
import math
import threading
import time

import numpy as np

import lbj_engine                       # 先导入它，装上 scipy 兼容层（顺序不能反）
from scipy.signal import firwin, lfilter
import lbj_ref as R

RTL_RATE = R.RTL_SAMPLE_RATE            # 960000
MID_RATE = R.MID_RATE                   # 240000
AUDIO_RATE = 48000
SPECTRUM_BINS = 32
ZOOM_HZ = 250000.0                      # 频谱显示范围：目标频率 ±250 kHz
DC_OFFSET_HZ = 50000.0                  # 躲开调谐器直流尖峰

MODES = ('NFM', 'AM', 'WFM')

# 亚音检测：48k 低通到 300Hz 后抽取到 3k，再用 0.5 秒滑窗做单点 DFT
CTCSS_DECIM = 16
CTCSS_FS = AUDIO_RATE // CTCSS_DECIM     # 3000
CTCSS_WIN = int(CTCSS_FS * 0.5)          # 1500 点 -> 分辨率 2Hz
# 说明：曾经做过"扫描时顺便识别未知亚音"的功能，用户反馈用不上，已删除。
# 保留的只有 set_ctcss()/_ctcss_check()：那是"已知亚音"的静噪门控（老功能，继续用）。

# 弱信号自动高切的最低音频带宽（Hz）
HC_MIN_HZ = 2000.0
# 判强弱的 RSSI 区间（dB）。实测：纯噪声底约 -46~-55，真实信号约 -20~-38。
#
# ★ 这里只能用【绝对 RSSI】，不能拿"相对跟踪底噪"来判断 ——
#   FM 是恒包络，持续信号下底噪跟踪会跟到信号本身，信噪比恒为 0，
#   结果强信号也被当成弱信号压窄带宽（测试抓出来过）。
HC_RSSI_LO = -58.0
HC_RSSI_HI = -35.0

# ---------------------------------------------------------------------------
# 扫描找频
#
# ★ 关键设计：【不要】每格重调一次硬件。
#   逐格重调的话，每格要等 PLL 重锁 + 300ms 静音窗，一格就是 300ms ——
#   10 MHz / 12.5kHz = 800 格，得扫 4 分钟，没法用。
#   改成 FFT 扫描：硬件一次停在一个 800 kHz 窗口上，抓一段 IQ 做一次 FFT，
#   一次就把这个窗口里 64 个格子全算出来，只有换窗口才动硬件。
# ---------------------------------------------------------------------------
SCAN_STEP_HZ = 12500.0        # 扫描格子（铁路/业余 NFM 常用步进）
SCAN_WIN_SPAN_HZ = 800000.0   # 每个硬件窗口只取中间 800kHz 用（两侧有模拟滤波器滚降）
SCAN_FFT_N = 32768            # 每窗口 FFT 点数（34ms @960k -> bin 29Hz）
SCAN_DISCARD_BLOCKS = 2       # 换窗口后先丢几块：rtl_tcp 缓冲里还有换频前的数据
SCAN_INTEG_BLOCKS = 2         # 每窗口用来积分的块数（多积一块，底噪估值更稳）
SCAN_DC_GUARD_HZ = 30000.0    # 直流尖峰附近这么多 Hz 内不判信号（尖峰固定在硬件中心）
SCAN_SPAN_DEFAULT_HZ = 10e6   # 默认：从当前频率向上扫 10 MHz
SCAN_MIN_MARGIN_DB = 6.0      # 判"有信号"的最小余量（相对扫出来的底噪）
SCAN_VERIFY_S = 0.90          # 命中后复核驻留（含调谐器稳定时间，顺便让人听到）
SCAN_VERIFY_SKIP = 10         # 复核前先跳过这么多块（等 PLL 稳，别把换频瞬间算进去）
SCAN_TONE_S = 1.15            # 读亚音驻留（要 1 秒窗才分得开 67.0/69.3）
SCAN_MAX_RESULTS = 40         # 结果表上限
# 门限 = 底噪 + margin。margin 默认按静噪档位推，用户可以在界面上手动指定
# （只想留强台就调大，怕漏弱信号就调小）。上下限是防手滑的护栏。
SCAN_MARGIN_DEFAULT_DB = 7.0  # 界面上默认填的余量
SCAN_MARGIN_MIN_DB = 2.0      # 再小就是噪声自己了
SCAN_MARGIN_MAX_DB = 40.0     # 再大连本地强台都进不来
# 电平接近满量程就说明前端被打饱和了：此时频率会偏、亚音会被削掉、
# 还会冒出一堆互调假信号 —— 必须明确告诉用户"这次读数不可信"。
SCAN_OVERLOAD_DB = -3.0       # 比这更响就认为过载（0 dB = 满量程）

# ---- 自动 PPM 校准（依托本地 FM 广播）----
# 为什么可以信 FM 广播：广播发射台锁 GPS（单频网要求），载波比手台准几个数量级。
# FC0013 这类便宜棒的晶振普遍偏得较多，所以把"自己找台并校准"做成一键。
AUTOCAL_LO_HZ = 87.5e6        # FM 广播段
AUTOCAL_HI_HZ = 108.0e6
AUTOCAL_GRID_HZ = 100e3       # 国内 FM 频点都在 100kHz 栅格上
AUTOCAL_MEAS_S = 4.0          # 正式测量驻留时长
AUTOCAL_VERIFY_S = 2.0        # 应用后复测时长
SCAN_HW_SETTLE_S = 0.12       # 换硬件窗口后额外等一会儿（按已处理音频时长算）

# 每种模式的参数：信道带宽、解调后音频增益
MODE_PARAMS = {
    'NFM': {'bw': 12000.0, 'gain': 8.0, 'hp': 300.0, 'lp': 3400.0, 'deemph': None},
    'AM':  {'bw': 9000.0,  'gain': 1.6, 'hp': 200.0, 'lp': 3500.0, 'deemph': None},
    'WFM': {'bw': 180000.0, 'gain': 2.5, 'hp': 60.0,  'lp': 15000.0, 'deemph': 50e-6},
}


def _fir_lp(nt, cut, fs):
    return firwin(nt, cut, fs=fs, window='blackmanharris').astype(np.float32)


def _fir_hp(nt, cut, fs):
    """高通用"全通减低通"实现，避免依赖 firwin 的 pass_zero 参数。"""
    h = -_fir_lp(nt, cut, fs)
    h[nt // 2] += 1.0
    return h.astype(np.float32)


class _Fir:
    """分块连续的单通道 FIR（实数），抽头少、够快。"""

    def __init__(self, h, decim=1):
        self.h = np.asarray(h, dtype=np.float32)
        self.decim = int(decim)
        self._z = np.zeros(max(0, self.h.size - 1), dtype=np.float64)

    def process(self, x):
        y, self._z = lfilter(self.h, 1.0, np.asarray(x, dtype=np.float64), zi=self._z)
        if self.decim > 1:
            return y[::self.decim]
        return y


class _VarLowPass:
    """截止频率可变的低通。

    用于"弱信号自动高切"：信噪比差的时候把音频带宽压窄，
    噪声（"滋滋"声）基本都在高频，压窄带宽等于直接把它切掉 ——
    这也是真实收音机在弱信号下的做法（soft mute / high cut）。

    系数按离散档位重建，避免每个数据块都去算一次 firwin。
    """

    STEPS = (15000.0, 11000.0, 8000.0, 6000.0, 4500.0, 3400.0, 2600.0, 2000.0)

    def __init__(self, fs, cut):
        self.fs = float(fs)
        self._cur = None
        self._f = None
        self.set(cut)

    def set(self, cut):
        c = min(self.STEPS, key=lambda s: abs(s - cut))
        if c == self._cur:
            return
        self._cur = c
        self._f = _Fir(_fir_lp(129, c, self.fs))

    def process(self, x):
        return self._f.process(x)


class _Deemph:
    """去加重：一阶低通 H(s)=1/(1+s·tau)，用其冲激响应的 FIR 截断近似。"""

    def __init__(self, tau, fs):
        a = math.exp(-1.0 / (tau * fs))
        k = 0
        while a ** k > 1e-5 and k < 512:
            k += 1
        h = np.array([(1.0 - a) * (a ** i) for i in range(k + 1)], dtype=np.float32)
        h /= h.sum()
        self._f = _Fir(h)

    def process(self, x):
        return self._f.process(x)


class RadioEngine:
    """收音机引擎。线程模型与预警器一致：一个后台线程读数据源并解调。"""

    def __init__(self, audio=None):
        self._audio = audio            # Kotlin AudioSink（可为 None，桌面测试时用假的）
        self._src = None
        self._thread = None
        self._running = False
        self._gen = 0
        self._err = ''
        self._lock = threading.Lock()

        self._freq = 457_000_000.0
        self._mode = 'NFM'
        self._step = 12_500.0
        self._volume = 0.8
        self._squelch_on = True
        # ★ 静噪不是绝对的 dB 门限，而是"比底噪高多少 dB"。
        #   原因：8bit 采样的噪声底就在 -50 左右，而真实信号也就 -38 上下，
        #   绝对门限根本分不开这两者（实测纯噪声一样判"有声"，喇叭一直在嘶嘶响）。
        #   所以这里自己跟踪底噪，门限 = 底噪 + 本值。
        # 默认比底噪高 12dB。为什么不是 6dB：这里的底噪取的是窗口内的【最小值】，
        # 而噪声瞬时值会在均值上下浮动，6dB 余量时噪声本身就会有一半时间超过门限，
        # 表现为"没信号也一直有声"（真机上就是这么暴露出来的）。
        self._squelch_db = 12.0
        self._floor = None
        self._thr = -140.0
        # ★ 这两个窗口按【已处理的音频时长】算，不能用墙上时间。
        #   用墙上时间的话，桌面测试跑得比实时快几十倍，窗口永远不结束，
        #   静噪就一直是关的（实测被测试抓出来过）。
        self._elapsed = 0.0                      # 已输出的音频秒数
        self._learn_until = 0.5                  # 起步阶段先摸一遍底噪
        self._mute_until = 0.0                   # 刚改过频率的静音窗
        # 亚音（CTCSS）：0 表示关闭。开着时只有收到对应亚音才放开静噪，
        # 用于同频多个台（中继台/车队）互不打扰。
        self._ctcss = 0.0
        self._ctcss_ok = False
        self._ctcss_buf = np.zeros(0, dtype=np.float64)
        self._gain_db = 19.7
        self._ppm = 0

        # 硬件（调谐器）实际停在哪 —— 软件换频要拿它算 DDC 偏移
        self._hw_center = self._freq - DC_OFFSET_HZ
        self._scan = None             # 扫描状态机（None = 没在扫）
        self._scan_last = None        # 上一次扫描的结果（界面要显示那张表）
        self._calib = None            # 手动 PPM 校准状态（量当前频率）
        self._autocal = None          # 自动 PPM 校准状态（自己找广播台）
        self._scan_sq = True          # 扫描前的静噪状态，结束时还原

        self._rssi = -140.0
        self._open = False
        self._silent_ms = 0.0
        self._spectrum = [-120.0] * SPECTRUM_BINS
        self._samples_out = 0
        self._last_snapshot = time.time()

        self._build()

    # ------------------------------------------------------------ DSP 装配
    def _build(self):
        p = MODE_PARAMS[self._mode]
        self._ddc = R._D1(RTL_RATE, DC_OFFSET_HZ)     # 下变频到目标频率
        self._hb = [R._D3(63), R._D3(63)]             # 960k → 240k
        self._ch = R._D2(MID_RATE, p['bw'])           # 信道滤波（240k 域）
        self._dec = R._D4(MID_RATE, AUDIO_RATE)       # 240k → 48k 复数
        self._fm = R._D5()                            # 鉴频器（跨块连续）
        self._adec = _Fir(_fir_lp(201, AUDIO_RATE * 0.45, MID_RATE), decim=MID_RATE // AUDIO_RATE)
        # 亚音在 67~254Hz，正好在音频高通(300Hz)的下面，所以单独引一路。
        #
        # ★ 低通之后还要抽取到 3kHz：标准亚音相邻只差几 Hz（67.0 / 71.9 / 74.4…），
        #   直接在 48kHz 上做 DFT，一窗才 1600 点、分辨率约 30Hz，根本分不开
        #   （实测 88.5Hz 的亚音会把 100Hz 也判成"有"）。抽到 3kHz 后
        #   0.5 秒窗口的分辨率是 2Hz，才够用。
        self._ctcss_lp = _Fir(_fir_lp(161, 300.0, AUDIO_RATE), decim=CTCSS_DECIM)
        self._ctcss_buf = np.zeros(0, dtype=np.float64)
        self._f_hp = _Fir(_fir_hp(129, p['hp'], AUDIO_RATE))
        # 音频低通改成"可变截止"：强信号给满带宽，弱信号自动收窄削噪声
        self._f_lp = _VarLowPass(AUDIO_RATE, p['lp'])
        self._deemph = _Deemph(p['deemph'], AUDIO_RATE) if p['deemph'] else None
        self._am_ref = 1e-3
        self._last = np.complex64(0)

    def _reset_dsp(self):
        with self._lock:
            self._build()

    # ------------------------------------------------------------ 参数
    def set_frequency(self, hz):
        hz = float(hz)
        if hz <= 0:
            return
        self._freq = hz
        # 硬件中心 = 目标 − 50kHz（DC 避让）；软件 DDC 再把它搬回来。
        # 扫描时硬件会停在别的窗口上，扫完要能把软件偏移还原，
        # 否则回到正常收听会听到相差几百 kHz 的电台。
        self._hw_center = hz - DC_OFFSET_HZ
        try:
            self._ddc.set_offset(DC_OFFSET_HZ)
        except Exception:
            pass
        # 调谐器换频后有一小段没稳，这期间的 RSSI 不能信：
        # 既不能拿去更新底噪，也不能放开静噪（否则喇叭会"噗"一声）。
        self._mute_until = self._elapsed + 0.3
        if self._src is not None:
            try:
                self._src._af(hz)
            except Exception as e:
                print('LBJ: 收音机改频失败 %s' % e, flush=True)

    def set_mode(self, m):
        m = str(m).upper()
        if m in MODES and m != self._mode:
            self._mode = m
            self._reset_dsp()
            print('LBJ: 收音机解调方式 → %s' % m, flush=True)

    def set_step(self, hz):
        self._step = float(hz)

    def set_volume(self, v):
        self._volume = max(0.0, min(1.0, float(v)))

    def set_squelch(self, db):
        self._squelch_db = float(db)

    def set_squelch_on(self, on):
        self._squelch_on = bool(on)

    def set_ctcss(self, hz):
        """模拟亚音频率（Hz）。传 0 表示关闭。"""
        self._ctcss = max(0.0, float(hz))
        self._ctcss_buf = np.zeros(0, dtype=np.float64)
        if self._ctcss > 0:
            print('LBJ: 收音机亚音 → %.1f Hz' % self._ctcss, flush=True)

    def set_gain(self, db):
        self._gain_db = float(db)
        if self._src is not None:
            try:
                self._src._ag(self._gain_db)
            except Exception:
                pass

    def set_ppm(self, ppm):
        """设频偏校正（ppm）。

        ★ 必须同步到参考实现的共享状态 R._g2['ppm']：
          预警器的 _reader_task 在【重连时】会用 _g2['ppm'] 重新下发一次校正值。
          只改自己这边的话，收音机里刚校准好的 ppm 会在预警器重连时被旧值覆盖。
        """
        self._ppm = int(ppm)
        R._g2['ppm'] = self._ppm
        if self._src is not None:
            try:
                self._src._ah(self._ppm)      # librtlsdr 会立刻重新调谐生效
            except Exception:
                pass

    # ------------------------------------------------------------ 数据源
    def adopt(self, src):
        """接管一个已经打开的 rtl_tcp 数据源（预警器暂停后交出来的）。"""
        self._src = src
        self._src._error = None
        self._apply_hw()
        print('LBJ: 收音机已接管现成连接（没有重连驱动）', flush=True)

    def make_source(self):
        """自己新建一个数据源（首次从预警器切过来、或独立启动时用）。"""
        src = R._A2('127.0.0.1', 1234, self._freq - DC_OFFSET_HZ,
                    RTL_RATE, 32768, dc_offset=DC_OFFSET_HZ)
        return src

    def _apply_hw(self):
        if self._src is None:
            return
        try:
            self._src._af(self._freq)
            self._src._ag(self._gain_db)
            self._src._ah(self._ppm)
        except Exception:
            pass

    def has_source(self):
        return self._src is not None

    def yield_source(self):
        """把数据源交出去（切回预警器），只停线程，不动连接。"""
        self.stop()
        src = self._src
        self._src = None
        return src

    def release_source(self):
        """彻底关掉数据源。"""
        self.stop()
        src = self._src
        self._src = None
        if src is not None:
            try:
                src.close()
            except Exception:
                pass

    def is_connected(self):
        return bool(self._src is not None and not self._src._error)

    def error(self):
        return self._err

    # ------------------------------------------------------------ 线程
    def start(self):
        if self._src is None:
            self._err = '没有数据源'
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        if self._src._error:
            self._err = str(self._src._error)
            return False
        self._running = True
        self._err = ''
        self._gen += 1
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print('LBJ: 收音机解码线程启动（%s %.4f MHz）' % (self._mode, self._freq / 1e6), flush=True)
        return True

    def stop(self):
        self._running = False
        self._gen += 1
        t = self._thread
        self._thread = None
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        return True

    def _loop(self):
        gen = self._gen
        n = 0
        block_reads = 0
        t_start = time.time()
        while self._running and gen == self._gen:
            try:
                iq = self._src.read()
            except Exception as e:
                if self._running and gen == self._gen:
                    self._err = str(e)
                    print('LBJ-RADIO-ERR read: %s' % e, flush=True)
                break
            if iq is None or len(iq) < 64:
                continue
            block_reads += 1
            # 自动 PPM 校准优先（它自己会换频、量载波）
            if self._autocal is not None and not self._autocal.get('done'):
                try:
                    self._autocal_tick(iq)
                except Exception as e:
                    self._err = '自动校准失败: %s' % e
                    print('LBJ-RADIO-ERR autocal: %s' % e, flush=True)
                    self._autocal = None
                n += len(iq)
                continue
            # 扫描期间走扫描状态机（它自己决定要不要出声）
            if self._scan is not None:
                try:
                    self._scan_tick(iq)
                except Exception as e:
                    self._err = '扫描失败: %s' % e
                    print('LBJ-RADIO-ERR scan: %s' % e, flush=True)
                    self._scan = None
                n += len(iq)
                if block_reads % 40 == 0:
                    s = self._scan
                    print('LBJ: 扫描中 %s %.4f MHz' % (
                        (s or {}).get('phase', '?'), (s or {}).get('cur_hz', 0) / 1e6), flush=True)
                continue
            try:
                pcm, rssi = self._process(iq)
            except Exception as e:
                self._err = '解调失败: %s' % e
                print('LBJ-RADIO-ERR dsp: %s' % e, flush=True)
                break
            self._rssi = rssi
            if self._audio is not None and pcm.size:
                try:
                    self._audio.write(pcm.tobytes())
                except Exception as e:
                    self._err = '音频输出失败: %s' % e
                    print('LBJ-RADIO-ERR audio: %s' % e, flush=True)
                    break
            n += len(iq)
            if block_reads % 40 == 0:
                el = max(1e-6, time.time() - t_start)
                print('LBJ: 收音机已处理 %.0fk 采样  rssi=%.1f %s %s' % (
                    n / 1000.0, rssi, self._mode, '有声' if self._open else '静噪'), flush=True)
        print('LBJ: 收音机线程结束 gen=%d n=%d err=%r' % (gen, n, self._err), flush=True)

    # ------------------------------------------------------------ 解调
    def _process(self, iq):
        p = MODE_PARAMS[self._mode]
        x = self._ddc.process(iq)
        for h in self._hb:
            x = h.process(x)
        self._spectrum_update(x)
        x = self._ch.process(x)

        if self._mode == 'WFM':
            rssi = 10.0 * np.log10(float(np.vdot(x, x).real) / max(1, len(x)) + 1e-12)
            audio = self._fm.process(x).astype(np.float64)
            self._calib_feed(audio, MID_RATE)     # 校准要的是"鉴频器直流"，必须在高切之前取
            audio = self._adec.process(audio)
        else:
            x = self._dec.process(x)
            rssi = 10.0 * np.log10(float(np.vdot(x, x).real) / max(1, len(x)) + 1e-12)
            if self._mode == 'NFM':
                audio = self._fm.process(x).astype(np.float64)
                self._calib_feed(audio, AUDIO_RATE)
            else:
                env = np.abs(x).astype(np.float64)
                ref = float(np.mean(env)) if env.size else self._am_ref
                self._am_ref = 0.98 * self._am_ref + 0.02 * ref
                audio = env / max(self._am_ref, 1e-9) - 1.0

        # 亚音检测要用【高通之前】的信号，否则亚音已经被滤掉了
        if self._ctcss > 0:
            self._ctcss_ok = self._ctcss_check(audio)
        else:
            self._ctcss_ok = True

        # ---- 弱信号自动高切 ----
        # 信噪比（当前 RSSI 相对跟踪到的底噪）越低，音频带宽压得越窄。
        # 4dB 以下给最窄，26dB 以上给满带宽，中间线性过渡。
        t = (rssi - HC_RSSI_LO) / (HC_RSSI_HI - HC_RSSI_LO)
        t = max(0.0, min(1.0, t))
        fmax = min(p['lp'], AUDIO_RATE * 0.45)
        self._f_lp.set(HC_MIN_HZ + t * (fmax - HC_MIN_HZ))

        audio = self._f_hp.process(audio)
        audio = self._f_lp.process(audio)
        if self._deemph is not None:
            audio = self._deemph.process(audio)

        # ---- 底噪跟踪 ----
        # 掉下去就快速跟、升上来就慢慢升，免得把一段语音当成新底噪。
        #
        # ★ 只在"上一帧判定为噪声"时才更新 —— 这一条很关键：
        #   否则一段【持续】的信号会把底噪一路抬上去，最后被自己的门限静掉
        #   （对讲机长时间按住发射、广播常发载波都会中招）。
        #
        # ★ 换频【不重置】底噪。重置的话，一旦正好落在信号上，
        #   底噪就等于信号本身，之后永远压不过自己的门限 —— 只剩噪声没有声。
        #   底噪靠"关着的时候慢慢往上爬"来适应环境变化就够了。
        dt = len(audio) / float(AUDIO_RATE)
        self._elapsed += dt
        now = self._elapsed
        prev_open = self._open
        if self._floor is None or now < self._learn_until:
            # 起步：取这一段的最小值当底噪，同时先静音
            if self._floor is None or rssi < self._floor:
                self._floor = rssi
            self._open = False
        elif now < self._mute_until:
            # 调谐器还没稳，冻住底噪并静音
            self._open = False
        else:
            if not prev_open:
                if rssi < self._floor:
                    self._floor = 0.8 * self._floor + 0.2 * rssi
                else:
                    self._floor = min(self._floor + 0.3 * dt, rssi)
            self._open = (not self._squelch_on) or (
                rssi >= self._floor + self._squelch_db and self._ctcss_ok)
        self._thr = self._floor + self._squelch_db
        if not self._squelch_on:
            self._open = True
        # 设了亚音但没收到对应亚音 -> 一律不放音
        if self._ctcss > 0 and not self._ctcss_ok:
            self._open = False
        if self._open:
            self._silent_ms = 250.0
        else:
            self._silent_ms = max(0.0, self._silent_ms - len(audio) * 1000.0 / AUDIO_RATE)
        if self._silent_ms <= 0.0:
            audio = np.zeros_like(audio)

        audio = np.clip(audio * p['gain'] * self._volume, -1.0, 1.0)
        self._samples_out += audio.size
        return (audio * 32767.0).astype(np.int16), rssi

    def _ctcss_check(self, x):
        """看亚音频率上有没有"一根谱线"。

        判据是该频点的能量占 300Hz 以下总能量的比例：
        纯正弦约 0.33，白噪声只有 0.005 上下，取 0.10 当门限很稳。
        用最近 0.5 秒的数据滑窗，兼顾分辨率与响应速度（真实对讲机约 250ms）。
        """
        try:
            y = self._ctcss_lp.process(x)
            if y.size:
                self._ctcss_buf = np.concatenate([self._ctcss_buf, y])[-CTCSS_WIN:]
            z = self._ctcss_buf
            n = z.size
            if n < CTCSS_WIN * 0.6:
                return self._ctcss_ok          # 数据还不够，沿用上一次的判断
            win = np.hanning(n)
            zw = z * win
            idx = np.arange(n, dtype=np.float64)
            w = 2.0 * np.pi * self._ctcss / float(CTCSS_FS)
            cg = float(np.dot(zw, np.cos(w * idx)))
            sg = float(np.dot(zw, np.sin(w * idx)))
            tot = float(np.dot(zw, zw))
            if tot <= 1e-18:
                return False
            return ((cg * cg + sg * sg) / (tot * n)) > 0.10
        except Exception:
            return self._ctcss_ok

    def _spectrum_update(self, x240):
        try:
            n = 1024
            if len(x240) < n:
                return
            win = np.hanning(n)
            seg = x240[:n] * win
            spec = np.fft.fftshift(np.fft.fft(seg))
            mag = np.abs(spec) / (n * np.mean(win))
            db = 20.0 * np.log10(np.maximum(mag, 1e-12))
            fa = np.fft.fftshift(np.fft.fftfreq(n, d=1.0 / float(MID_RATE)))
            # ★ 窗口中心必须是 0 Hz，不能是 DC_OFFSET_HZ。
            #
            #   DDC(_D1) 已经按 DC_OFFSET_HZ 把【调谐频率】搬到了基带 0 Hz，
            #   所以窗口中心就是调谐频率。原来写成 DC_OFFSET_HZ，
            #   等于把整张频谱平移了 50 kHz（32 格 / ±250kHz 时正好 3.2 格）——
            #   实测"对讲机在 438.5 发射，谱线却明显偏离中线"就是这么来的，
            #   那是显示偏，不是硬件频偏。
            off = 0.0
            m = (fa >= off - ZOOM_HZ) & (fa <= off + ZOOM_HZ)
            sub = db[m]
            if sub.size >= SPECTRUM_BINS:
                parts = np.array_split(sub, SPECTRUM_BINS)
                self._spectrum = [round(float(q.max()), 1) for q in parts]
            elif sub.size:
                self._spectrum = [round(float(v), 1) for v in sub]
        except Exception:
            pass

    # ==================================================== 扫描找频
    #
    # 用途：找到附近那个台的【频率 + 亚音】，然后去把自己的对讲机设成一样的。
    # 本机是纯接收，不参与通话。
    #
    # 流程：先（可选）读当前收听频率的亚音 -> FFT 扫一遍范围 -> 复核命中的点
    #       -> 每个点驻留 1 秒读亚音 -> 列成一张表 -> 停在最强那个信号上。

    def _tune_hw(self, hw_center):
        """把调谐器挪到 hw_center（扫描换窗口用）。

        注意 _af() 收的是【目标频率】，它自己会减掉 50kHz 的 DC 避让，
        所以要让硬件停在 hw_center，得传 hw_center + 50kHz。
        """
        self._hw_center = float(hw_center)
        if self._src is not None:
            try:
                self._src._af(float(hw_center) + DC_OFFSET_HZ)
            except Exception as e:
                print('LBJ: 扫描换窗口失败 %s' % e, flush=True)

    def _tune_sw(self, hz):
        """软件换频：硬件不动，只挪 DDC 偏移 —— 瞬间生效，没有 300ms 静音。

        只在当前硬件窗口（±480kHz）内有效；扫描复核命中的点都在窗口内，够用。
        """
        self._freq = float(hz)
        try:
            self._ddc.set_offset(float(hz) - float(self._hw_center))
        except Exception:
            pass

    def start_scan(self, span_hz=None, fine=True, margin_db=None):
        """开始扫描：从当前频率向上扫 span_hz，把有信号的频点列出来。返回 True。

        margin_db：判定门限要比底噪高多少 dB（None = 按静噪档位自动推）。
        """
        span = float(span_hz or SCAN_SPAN_DEFAULT_HZ)
        f0 = float(self._freq)
        nwin = max(1, int(np.ceil(span / SCAN_WIN_SPAN_HZ)))
        # ★ 两趟扫描，第二趟窗口错开半个窗口。
        #   原因：直流尖峰固定落在【硬件窗口中心】，中心 ±SCAN_DC_GUARD_HZ 内读数不可信，
        #   只扫一趟的话每隔一个窗口就有一条永久盲带（实测正好把测试信号埋了）。
        #   错开半窗后，第一趟的盲区正好落在第二趟的中间，反过来也是。
        wins = [f0 + SCAN_WIN_SPAN_HZ * (k + 0.5) for k in range(nwin)]
        wins += [f0 + SCAN_WIN_SPAN_HZ * (k + 1.0) for k in range(nwin)]
        self._scan_sq = self._squelch_on          # 扫描期间强制出声，结束时还原
        self._scan_last = None
        self._scan = {
            'phase': 'window',
            'start_hz': f0, 'end_hz': f0 + span, 'span_hz': span,
            'wins': wins, 'wi': 0, 'cur_hz': f0,
            'fine': bool(fine),
            'discard_left': 0, 'integ_left': 0, 'acc': [],
            'meas': [], 'hits': [], 'hi': 0, 'results': [],
            'floor': None, 'thr': None,
            'margin': None if margin_db is None else float(margin_db),
            'left': SCAN_TONE_S,
            'n_ch': 0, 'stop': False, 'vset': None, 'rssi_acc': [], 'pass': 0,
        }
        self._squelch_on = False
        self._open = False
        self._silent_ms = 0.0
        print('LBJ: 扫描开始 %.4f~%.4f MHz（%d 窗口，自动微调=%s，门限%s）'
              % (f0 / 1e6, (f0 + span) / 1e6, nwin, fine,
                 '自动' if margin_db is None else '底噪+%.0f dB' % float(margin_db)),
              flush=True)
        return True

    def stop_scan(self):
        if self._scan is not None:
            self._scan['stop'] = True
        return True

    @staticmethod
    def _margin_of(d):
        """取用户设的门限余量；没设过（或值离谱）返回 None，由调用方退回自动值。"""
        v = (d or {}).get('margin')
        if v is None:
            return None
        try:
            return max(SCAN_MARGIN_MIN_DB, min(SCAN_MARGIN_MAX_DB, float(v)))
        except Exception:
            return None

    def set_scan_margin(self, db):
        """手动改扫描门限（界面上的"门限"输入框），立刻生效，不用重扫。

        改门限时顺手把已经扫到的结果按新门限过一遍 —— 用户调高门限就是想
        把弱信号从表里去掉，让它们赖在表里就自相矛盾了。
        """
        try:
            v = max(SCAN_MARGIN_MIN_DB, min(SCAN_MARGIN_MAX_DB, float(db)))
        except Exception:
            return False
        for s in (self._scan, self._scan_last):
            if s is None:
                continue
            s['margin'] = v
            fl = s.get('floor')
            if fl is None:
                continue
            thr = float(fl) + (self._margin_of(s) or 0.0)
            s['thr'] = round(thr, 1)
            res = s.get('results') or []
            keep = [r for r in res if float(r.get('db') or -999.0) >= thr]
            if len(keep) != len(res):
                s['results'] = keep
                print('LBJ: 扫描门限 → 底噪+%.1f dB（%.1f dB），滤掉 %d 个弱信号，剩 %d 个'
                      % (v, thr, len(res) - len(keep), len(keep)), flush=True)
            else:
                print('LBJ: 扫描门限 → 底噪+%.1f dB（%.1f dB）'
                      % (v, thr), flush=True)
        return True

    # ------------------------------------------------------------ 扫描状态机
    def _scan_tick(self, iq):
        s = self._scan
        if s is None:
            return
        blk_s = len(iq) / float(RTL_RATE)
        ph = s['phase']

        # ---- 换硬件窗口 ----
        if ph == 'window':
            if s['stop'] or s['wi'] >= len(s['wins']):
                self._scan_finish_sweep(s)
                return
            c = s['wins'][s['wi']]
            s['cur_hz'] = c
            self._tune_hw(c)
            blk_n = int(SCAN_HW_SETTLE_S * RTL_RATE / max(1, len(iq)))
            s['discard_left'] = SCAN_DISCARD_BLOCKS + blk_n
            s['acc'] = []
            s['phase'] = 'discard'
            return

        # ---- 丢几块（缓冲里还有换频前的数据）----
        if ph == 'discard':
            s['discard_left'] -= 1
            if s['discard_left'] <= 0:
                s['integ_left'] = SCAN_INTEG_BLOCKS
                s['phase'] = 'measure'
            return

        # ---- 抓够就做一次 FFT，把这个窗口的格子全算出来 ----
        if ph == 'measure':
            # ★ 必须保持复数！写成 float32 会把虚部丢掉（NFM 直接解不出来）
            s['acc'].append(np.asarray(iq, dtype=np.complex64))
            s['integ_left'] -= 1
            if s['integ_left'] <= 0:
                self._scan_measure_window(s)
                s['acc'] = []
                s['wi'] += 1
                s['phase'] = 'window'
            return

        # ---- 复核候选：软件调过去，驻留几百毫秒顺便出声 ----
        if ph == 'verify':
            if s['stop']:
                s['phase'] = 'done'
                return
            if s['hi'] >= len(s['hits']):
                # ★ 一趟验收完就【接着扫下一趟】，不自己结束 ——
                #   用户要的是"不手动停就一直扫，扫到的都记在列表里"。
                s['pass'] = int(s.get('pass', 0)) + 1
                s['meas'] = []
                s['wi'] = 0
                s['phase'] = 'window'
                return
            h = s['hits'][s['hi']]
            if s.get('vset') != h['freq']:
                # ★ 必须【重新调硬件】：扫描结束时硬件停在最后一个窗口上，
                #   而候选可能在 10 MHz 之外，软件 DDC 只在 ±480kHz 内有效 ——
                #   用软件跳过去会量到完全错误的东西（这条踩过）。
                self.set_frequency(h['freq'])
                s['vset'] = h['freq']
                s['left'] = SCAN_VERIFY_S
                s['rssi_acc'] = []
                s['vskip'] = SCAN_VERIFY_SKIP
            pcm, rssi = self._process(iq)
            self._rssi = rssi
            if s.get('vskip', 0) > 0:
                s['vskip'] -= 1
            else:
                s['rssi_acc'].append(rssi)
            self._push_audio(pcm)
            s['cur_hz'] = h['freq']
            s['left'] -= blk_s
            if s['left'] <= 0:
                avg = float(np.mean(s['rssi_acc'])) if s['rssi_acc'] else -140.0
                print('LBJ: 复核 %.4f MHz  实测 %.1f dB  门限 %.1f'
                      % (h['freq'] / 1e6, avg, s['thr'] or -100.0), flush=True)
                if avg >= (s['thr'] if s['thr'] is not None else -100.0):
                    h['db'] = round(avg, 1)
                    if s['fine'] and h.get('cf'):
                        h['freq'] = float(h['cf'])      # 用峰值 bin 做微调
                    ovl = avg > SCAN_OVERLOAD_DB
                    fr = round(float(h['freq']), 1)
                    # 去重：同一个信号（1.5 格内）只留一条；重复扫到就更新强度
                    ex = None
                    for r0 in s['results']:
                        if abs(r0['freq'] - fr) <= SCAN_STEP_HZ * 1.5:
                            ex = r0
                            break
                    if ex is not None:
                        # ★ 同一个信号每再扫到一次，就把它和已有估计【平均】一次：
                        #   单次峰值估计受调制影响会偏（实测 95.9 会读成 95.9123），
                        #   多趟平均会一点点往真值收敛，越扫越准。
                        n0 = int(ex.get('n', 1))
                        n1 = min(n0, 30)      # 上限：老数据不能永远压着，环境变了要能跟上
                        ex['freq'] = round((ex['freq'] * n1 + fr) / (n1 + 1), 1)
                        ex['n'] = n0 + 1
                        m0 = min(n0, 9)
                        ex['db'] = round((ex['db'] * m0 + avg) / (m0 + 1), 1)
                        ex['ovl'] = ovl
                        if n0 % 5 == 0:
                            print('LBJ: 复扫 %.4f MHz -> 修正为 %.4f MHz（第 %d 次，共 %d 个）'
                                  % (fr / 1e6, ex['freq'] / 1e6, ex['n'], len(s['results'])),
                                  flush=True)
                    elif len(s['results']) < SCAN_MAX_RESULTS:
                        s['results'].append({'freq': fr, 'db': round(avg, 1), 'n': 1,
                                             'cur': False, 'ovl': ovl})
                        print('LBJ: 扫描命中 %.4f MHz  %.1f dB%s'
                              % (h['freq'] / 1e6, avg,
                                 '  ★过载：频率不可信，请降低增益或拉远距离' if ovl else ''),
                              flush=True)
                s['hi'] += 1
                s['vset'] = None
            return

        # ---- 收尾 ----
        if ph == 'done':
            res = s['results']
            self._squelch_on = getattr(self, '_scan_sq', True)
            self._scan_last = s
            self._scan = None
            if res:
                best = max(res, key=lambda r: r.get('db') or -999)
                self.set_frequency(best['freq'])       # 停在最强那个信号上
            else:
                self.set_frequency(s['start_hz'])
            print('LBJ: 扫描结束（扫了 %d 趟），共 %d 个信号'
              % (int(s.get('pass', 0)) + 1, len(res)), flush=True)


    # ------------------------------------------------------------ 自动 PPM 校准
    def start_autocalib(self):
        """一键自动校准 PPM：自己找本地 FM 广播台 -> 量载波频偏 -> 应用 -> 复测。

        为什么可以信 FM 广播：广播发射台锁 GPS（单频网必须），载波比手台准几个数量级。
        FC0013 这类便宜棒的晶振普遍偏得较多，所以把"自己找台并校准"做成一键。
        """
        if self._src is None:
            return {'ok': False, 'why': '没有数据源'}
        if self._mode != 'WFM':
            self.set_mode('WFM')
        lo, hi = AUTOCAL_LO_HZ, AUTOCAL_HI_HZ
        nwin = int(np.ceil((hi - lo) / SCAN_WIN_SPAN_HZ))
        wins = [lo + SCAN_WIN_SPAN_HZ * (k + 0.5) for k in range(nwin)]
        self._autocal = {
            'auto': True, 'phase': 'scan', 'done': False, 'feed': False,
            'wins': wins, 'wi': 0, 'discard_left': 0, 'integ_left': 0, 'acc': [],
            'best': None, 'cur_hz': lo, 'msg': '正在找本地广播台…',
            'lo': lo, 'hi': hi, 'want': 0, 'n': 0, 'sum': 0.0, 'fs': MID_RATE,
            'freq': float(self._freq), 'ppm0': int(self._ppm),
            'offset_hz': None, 'meas_hz': None, 'ppm_delta': None,
            'ppm_suggest': None, 'resid_hz': None,
        }
        print('LBJ: 自动 PPM 校准开始（FM %.1f~%.1f MHz，%d 个窗口）'
              % (lo / 1e6, hi / 1e6, nwin), flush=True)
        return {'ok': True, 'why': ''}

    def stop_autocalib(self):
        if self._autocal is not None:
            self._autocal['stop'] = True
        return True

    def clear_autocalib(self):
        self._autocal = None
        return True

    def autocal_state(self):
        c = self._autocal
        if c is None:
            return None
        w = max(1, len(c['wins']))
        if c['phase'] == 'scan':
            prog = c['wi'] / float(w)
        elif c['phase'] in ('measure', 'verify'):
            prog = 0.5 + 0.5 * (c['n'] / max(1.0, c['want']))
        else:
            prog = 1.0
        return {'auto': True, 'phase': c['phase'], 'done': bool(c['done']),
                'msg': c.get('msg', ''), 'progress': round(min(1.0, prog), 3),
                'freq': round(float(c.get('freq') or 0.0), 1),
                'scan_hz': round(float(c.get('cur_hz') or 0.0), 1),
                'ppm_now': c['ppm0'], 'offset_hz': c['offset_hz'],
                'meas_hz': c.get('meas_hz'),
                'ppm_suggest': c['ppm_suggest'], 'resid_hz': c['resid_hz'],
                'rssi': round(float(self._rssi), 1)}

    def _autocal_tick(self, iq):
        c = self._autocal
        if c is None:
            return
        ph = c['phase']

        if ph == 'scan':
            if c.get('stop'):
                c['phase'] = 'done'; c['done'] = True; c['msg'] = '已取消'
                return
            if c['discard_left'] > 0:
                c['discard_left'] -= 1
                return
            if c['integ_left'] > 0:
                c['acc'].append(np.asarray(iq, dtype=np.complex64))
                c['integ_left'] -= 1
                if c['integ_left'] == 0:
                    self._autocal_meas_window(c)
                    c['acc'] = []
                    c['wi'] += 1
                return
            if c['wi'] >= len(c['wins']):
                self._autocal_pick(c)
                return
            cc = c['wins'][c['wi']]
            c['cur_hz'] = cc
            self._tune_hw(cc)
            c['discard_left'] = 2
            c['integ_left'] = 1
            return

        if ph in ('measure', 'verify'):
            pcm, rssi = self._process(iq)      # 走正常解调，鉴频器直流被 _calib_feed 收走
            self._rssi = rssi
            self._push_audio(pcm)
            return

    def _autocal_meas_window(self, c):
        """扫一个窗口，记下最强的格子（跳过直流尖峰附近）。"""
        if not c['acc']:
            return
        x = np.concatenate(c['acc'])
        n = int(SCAN_FFT_N)
        x = x[:max(n, (x.size // n) * n)]
        if x.size < n:
            return
        acc = None
        for k in range(0, x.size - n + 1, n):
            p = self._fft_psd(x[k:k + n])
            acc = p if acc is None else acc + p
        if acc is None:
            return
        cc = float(c['wins'][c['wi']])
        df = RTL_RATE / float(n)
        half = max(1, int(SCAN_STEP_HZ / df / 2.0))
        for i in range(-int(SCAN_WIN_SPAN_HZ / SCAN_STEP_HZ / 2),
                       int(SCAN_WIN_SPAN_HZ / SCAN_STEP_HZ / 2) + 1):
            fq = cc + i * SCAN_STEP_HZ
            if fq < c['lo'] or fq > c['hi']:
                continue
            if abs(fq - cc) < SCAN_DC_GUARD_HZ:
                continue
            b = int(round((fq - cc) / df)) + n // 2
            lo = max(0, b - half); hi = min(n, b + half + 1)
            db = 10.0 * np.log10(float(np.sum(acc[lo:hi])) + 1e-20)
            if c['best'] is None or db > c['best'][1]:
                c['best'] = (fq, db)

    def _autocal_pick(self, c):
        b = c['best']
        if b is None:
            c['phase'] = 'done'; c['done'] = True; c['msg'] = 'FM 段里没扫到信号'
            return
        fpk, db = b
        # 广播频点都在 100kHz 栅格上，把峰值吸附过去（峰值受调制影响会有几 kHz 误差，
        # 但栅格宽 100kHz，吸附是安全的）
        grid = round(fpk / AUTOCAL_GRID_HZ) * AUTOCAL_GRID_HZ
        c['freq'] = grid
        c['msg'] = '找到 %.4f MHz（%.0f dB），吸附到 %.4f MHz 量载波' % (fpk / 1e6, db, grid / 1e6)
        print('LBJ: 自动校准 找到 %.4f MHz %.1f dB -> 栅格 %.4f MHz'
              % (fpk / 1e6, db, grid / 1e6), flush=True)
        self.set_frequency(grid)               # 硬件 = 目标 − 50k，DDC 复原
        c['phase'] = 'measure'
        c['want'] = AUTOCAL_MEAS_S * MID_RATE
        c['n'] = 0; c['sum'] = 0.0; c['feed'] = True; c['done'] = False

    def _autocal_after_measure(self, c):
        """一次驻留量完后：测 -> 应用 -> 复测。"""
        if c['phase'] == 'measure':
            c['meas_hz'] = c['offset_hz']          # 记下"应用前量到的"，复测会覆盖 offset_hz
            c['msg'] = '量得 %+.0f Hz，应用 ppm=%d，正在复测' % (c['offset_hz'], c['ppm_suggest'])
            self.set_ppm(c['ppm_suggest'])
            c['ppm0'] = c['ppm_suggest']
            c['phase'] = 'verify'
            c['want'] = AUTOCAL_VERIFY_S * MID_RATE
            c['n'] = 0; c['sum'] = 0.0; c['feed'] = True; c['done'] = False
            return
        c['resid_hz'] = c['offset_hz']
        c['phase'] = 'done'
        c['done'] = True
        c['msg'] = '校准完成：ppm=%d，复测残差 %+.0f Hz' % (c['ppm0'], c['offset_hz'] or 0.0)
        print('LBJ: 自动校准完成  应用 ppm=%d  复测残差 %+.0f Hz'
              % (c['ppm0'], c['offset_hz'] or 0.0), flush=True)

    # ------------------------------------------------------------ PPM 校准
    def start_calib(self, seconds=4.0):
        """开始测载波频偏。返回 {'ok':..., 'why':...}。

        原理：FM 鉴频器的输出，在"载波正好落在信道中心"时均值是 0；
        载波偏高多少 Hz，鉴频输出的【直流】就是多少 —— 换算 f = dc × fs/2。

        ★ 为什么不用频谱峰值/质心：FM 广播的 19kHz 导频、38kHz 立体声副载波、
          RDS 全在载波【上方】，会把峰值和质心整体拉高约 +2kHz（实测 95.9 读成
          95.9023 就是这么来的）。而音频本身没有直流分量，所以鉴频直流只反映
          载波偏了多少，不受调制内容影响。
        """
        if self._mode not in ('WFM', 'NFM'):
            return {'ok': False, 'why': '只有 NFM/WFM 能测（AM 没有鉴频器）'}
        if not self.is_connected():
            return {'ok': False, 'why': '没有数据源'}
        fs = MID_RATE if self._mode == 'WFM' else AUDIO_RATE
        self._calib = {'want': float(seconds) * fs, 'n': 0, 'sum': 0.0,
                       'freq': float(self._freq), 'ppm0': int(self._ppm),
                       'fs': fs, 'done': False, 'offset_hz': None, 'feed': True,
                       'ppm_delta': None, 'ppm_suggest': None, 'rssi': 0.0}
        return {'ok': True, 'why': ''}

    def calib_state(self):
        c = self._calib
        if c is None:
            return None
        prog = 0.0 if not c['want'] else min(1.0, c['n'] / c['want'])
        return {'done': bool(c['done']), 'progress': round(prog, 3),
                'freq': c['freq'], 'ppm_now': c['ppm0'],
                'offset_hz': c['offset_hz'], 'ppm_delta': c['ppm_delta'],
                'ppm_suggest': c['ppm_suggest'], 'rssi': round(float(self._rssi), 1)}

    def _calib_feed(self, x, fs):
        """把鉴频器输出喂给正在测量的校准容器（手动/自动共用）。"""
        if x.size == 0:
            return
        for c in (self._calib, self._autocal):
            if c is None or c.get('done') or not c.get('feed'):
                continue
            c['sum'] += float(np.sum(x))
            c['n'] += int(x.size)
            c['fs'] = fs
            if c['n'] >= c['want']:
                self._calib_finish(c)

    def _calib_finish(self, c):
        """一次驻留结束：鉴频器直流 -> 载波频偏 -> 建议 ppm。"""
        dc = c['sum'] / float(c['n'])
        off = dc * float(c['fs']) / 2.0
        delta = -off / float(c['freq']) * 1e6
        c['offset_hz'] = round(off, 1)
        c['ppm_delta'] = round(delta, 1)
        c['ppm_suggest'] = int(round(c['ppm0'] + delta))
        c['done'] = True
        c['feed'] = False
        print('LBJ: %s %.4f MHz  载波偏 %+.0f Hz  当前 ppm=%d  建议 %+d'
              % ('自动校准' if c.get('auto') else 'PPM 校准',
                 c['freq'] / 1e6, off, c['ppm0'], c['ppm_suggest']), flush=True)
        if c.get('auto'):
            self._autocal_after_measure(c)

    def _push_audio(self, pcm):
        if self._audio is not None and pcm is not None and pcm.size:
            try:
                self._audio.write(pcm.tobytes())
            except Exception as e:
                self._err = '音频输出失败: %s' % e

    def _fft_psd(self, seg):
        """一段 IQ 的功率谱。

        标定：sum(PSD) == 这段信号的均方值（汉宁窗已做能量归一），
        所以"某个带宽内的 PSD 求和"与正常收听时的 RSSI 是同一把尺子。
        """
        n = seg.size
        w = np.hanning(n).astype(np.float32)
        X = np.fft.fftshift(np.fft.fft(seg * w))
        return (X.real ** 2 + X.imag ** 2) / (n * float(np.sum(w ** 2)))

    def _scan_measure_window(self, s):
        """把一个硬件窗口的 IQ 做 FFT，算出窗口内每个 12.5kHz 格子的功率。"""
        if not s['acc']:
            return
        x = np.concatenate(s['acc'])
        n = int(SCAN_FFT_N)
        x = x[:max(n, (x.size // n) * n)]
        if x.size < n:
            return
        acc = None
        for k in range(0, x.size - n + 1, n):
            p = self._fft_psd(x[k:k + n])
            acc = p if acc is None else acc + p
        if acc is None:
            return
        c = float(s['wins'][s['wi']])
        df = RTL_RATE / float(n)
        half = max(1, int(SCAN_STEP_HZ / df / 2.0))
        ncell = int(SCAN_WIN_SPAN_HZ / SCAN_STEP_HZ / 2.0)
        for i in range(-ncell, ncell + 1):
            fq = c + i * SCAN_STEP_HZ
            if fq < s['start_hz'] - 1.0 or fq > s['end_hz'] + 1.0:
                continue
            b = int(round((fq - c) / df)) + n // 2
            lo = max(0, b - half)
            hi = min(n, b + half + 1)
            p = float(np.sum(acc[lo:hi]))
            # ★ 频率要用【峰值 bin + 抛物线插值】来报，不能用带内能量质心：
            #   格子宽 12.5kHz 但 FFT 的 bin 只有 29Hz，峰值插值能进到 kHz 以内；
            #   而质心会被 FM 广播的 19kHz 导频 / 38kHz 立体声副载波整体拉高
            #   （实测把 95.9 报成 95.9129）—— 质心不是载波频率。
            fpk = fq
            kmax = lo + int(np.argmax(acc[lo:hi]))
            if 0 < kmax < n - 1:
                y0, y1, y2 = float(acc[kmax - 1]), float(acc[kmax]), float(acc[kmax + 1])
                den = y0 - 2.0 * y1 + y2
                if abs(den) > 1e-30:
                    dl = 0.5 * (y0 - y2) / den
                    dl = max(-0.5, min(0.5, dl))
                    fpk = c + (kmax - n / 2.0 + dl) * df
            s['meas'].append((fq, 10.0 * np.log10(p + 1e-20), c, fpk))
            s['n_ch'] += 1

    def _scan_finish_sweep(self, s):
        """扫完所有窗口：估底噪、挑候选（连续超门限的合成一个，取峰值与该组能量重心）。"""
        m = s['meas']
        if not m:
            s['phase'] = 'done'
            return
        # 同一个格子可能被两趟都测到：取较大的；只被盲区覆盖过的格子直接丢掉
        best = {}
        for fq, d, c, fpk in m:
            if abs(fq - c) < SCAN_DC_GUARD_HZ:
                continue
            if fq not in best or d > best[fq][0]:
                best[fq] = (d, fpk)
        if not best:
            s['phase'] = 'done'
            return
        items = sorted(best.items())
        dbs = np.array([v[0] for _, v in items], dtype=np.float64)
        floor = float(np.percentile(dbs, 25))
        margin = self._margin_of(s) or max(SCAN_MIN_MARGIN_DB, float(self._squelch_db) * 0.6)
        thr = floor + margin
        s['floor'] = round(floor, 1)
        s['thr'] = round(thr, 1)

        hits = []
        cur = None
        for fq, (d, fpk) in items:
            if d >= thr:
                w = 10.0 ** (d / 10.0)
                if cur is None:
                    cur = {'freq': fq, 'db': d, 'w': w, 'wf': w * fq, 'fpk': fpk}
                else:
                    cur['w'] += w
                    cur['wf'] += w * fq
                    if d > cur['db']:
                        cur['freq'], cur['db'], cur['fpk'] = fq, d, fpk
            else:
                if cur is not None:
                    hits.append(cur)
                    cur = None
        if cur is not None:
            hits.append(cur)
        for h in hits:
            # 峰值 bin 估计（准）；万一是平的没插出来，退回能量质心
            h['cf'] = h.get('fpk') or (h['wf'] / h['w'] if h['w'] > 0 else h['freq'])
        hits.sort(key=lambda h: -h['db'])
        s['hits'] = hits[:SCAN_MAX_RESULTS]
        s['phase'] = 'verify'
        s['hi'] = 0
        s['vset'] = None
        print('LBJ: 扫描完成 %d 格（%d 格有效）  底噪 %.1f  门限 %.1f（底噪+%.1f）  候选 %d'
              % (len(m), len(items), floor, thr, margin, len(s['hits'])), flush=True)


    def _scan_snapshot(self):
        s = self._scan if self._scan is not None else self._scan_last
        if s is None:
            return None
        ph = s.get('phase')
        nw = max(1, len(s.get('wins') or [1]))
        prog = (s.get('wi', 0) + (1.0 if ph in ('verify', 'tone', 'done') else 0.0)) / nw
        return {
            'active': bool(self._scan is not None),
            'phase': ph,
            'cur_hz': round(float(s.get('cur_hz') or 0.0), 1),
            'start_hz': s.get('start_hz'), 'end_hz': s.get('end_hz'),
            'progress': round(min(1.0, prog), 3),
            'floor': s.get('floor'), 'thr': s.get('thr'),
            'margin': s.get('margin'),
            'n_ch': s.get('n_ch', 0),
            'pass': int(s.get('pass', 0)),
            'n_hit': len(s.get('hits') or []),
            'results': s.get('results') or [],
        }

    # ------------------------------------------------------------ 状态
    def snapshot(self):
        return {
            'running': bool(self._running),
            'kind': 'radio',
            'freq': self._freq,
            'mode': self._mode,
            'step': self._step,
            'rssi': round(float(self._rssi), 1),
            'squelch_db': self._squelch_db,
            'threshold': round(float(self._thr), 1),
            'floor': round(float(self._floor), 1) if self._floor is not None else None,
            'squelch_on': self._squelch_on,
            'ctcss': self._ctcss,
            'ctcss_ok': bool(self._ctcss_ok),
            'open': bool(self._open),
            'volume': self._volume,
            'gain_db': self._gain_db,
            'ppm': self._ppm,
            'spectrum': list(self._spectrum),
            'samples': self._samples_out,
            'scan': self._scan_snapshot(),
            'calib': self.calib_state(),
            'autocal': self.autocal_state(),
            'err': self._err,
        }

    def snapshot_json(self):
        return json.dumps(self.snapshot(), ensure_ascii=False)
