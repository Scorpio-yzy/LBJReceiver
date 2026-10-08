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
# 音频自动增益（AGC）
# ---------------------------------------------------------------------------
# 为什么需要：调频鉴频器的输出幅度【正比于频偏】。手台/对讲的频偏只有 2.5~3 kHz，
# 而调频广播是 25~75 kHz —— 差 10~30 倍。以前只有一套固定档位增益（NFM 8.0 / WFM 2.5），
# 结果就是"听广播正常、听对讲机特别轻"，而且广播那边其实一直被削顶。
# 这里改成按实测电平归一化：广播压下来（不再削顶）、对讲机抬上去，两种信号听感一致。
AGC_TARGET = 0.22      # 目标有效值（约 -13 dBFS）
AGC_GAIN_MIN = 0.25    # 最多压到 1/4
AGC_GAIN_MAX = 30.0    # 最多抬 30 倍（约 30dB）
# 包络跟踪：往上涨（信号变强）要快，往下降要慢，否则语音的停顿会把底噪抬起来
AGC_ATTACK = 0.4
AGC_RELEASE = 0.10
# 信号贴到底噪（差不到这么多 dB）时最多只抬 4 倍：
# 否则"关掉静噪听弱信号"会变成满屋子嘶嘶声（增益把噪声也归一化了）。
AGC_MIN_SNR_DB = 8.0
AGC_WEAK_GAIN_MAX = 4.0

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
SCAN_WIN_FRACTION = 0.8333    # 一个窗口只用采样率的 83%（留边：躲开两侧滚降和奈奎斯特）
SCAN_WIN_SPAN_HZ = 800000.0   # 960k 采样时的窗口宽（= 960k × 0.833，其余速率按比例算）
SCAN_FFT_N = 32768            # 每窗口 FFT 点数（34ms @960k -> bin 29Hz）
SCAN_DISCARD_BLOCKS = 2       # 换窗口后先丢几块：rtl_tcp 缓冲里还有换频前的数据
SCAN_INTEG_BLOCKS = 2         # 每窗口用来积分的块数（多积一块，底噪估值更稳）
SCAN_DC_GUARD_HZ = 30000.0    # 直流尖峰附近这么多 Hz 内不判信号（尖峰固定在硬件中心）
SCAN_SPAN_DEFAULT_HZ = 20e6   # 默认：当前频率【上下各 10 MHz】（实测一趟约 2.4 秒）
# 换窗口的那点固定开销（等 PLL + 丢缓冲块）是每窗口都要付一次的，
# 所以"一趟扫得快"的关键是【窗口少】，不是 FFT 快：把采样率提到 2.4MS/s，
# 一个窗口就能盖 2MHz（960k 时只有 800kHz），上下各 10MHz 从 52 个窗口降到 20 个。
# 实测 FC0013：960k 一趟约 15s，2.4M 约 3s。驱动不认这个速率就退回 960k（校验见状态机）。
SCAN_RATE = 2400000
SCAN_WIN_SPAN_FAST_HZ = 2000000.0
# ★ 驱动不一定给你请求的速率，块大小也不固定（实测本机 65536 采样一块，
#   请求 2.4M 实测只有约 1.5M）。所以速率必须【量出来】：量到多少就用它当
#   bin->Hz 的尺子、按它定窗口宽 —— 否则扫出来的频率会整体偏几倍。
SCAN_RATE_CHECK_N = 8         # 用最初几块的到达间隔量真实速率
SCAN_RATE_MIN = 200e3         # 量出来的速率限制在这个范围内才算数
SCAN_RATE_MAX = 3.2e6
SCAN_RATE_TOL = 50e3          # 和当前假定的速率差这么多就重排窗口
SCAN_HW_LO_HZ = 24e6          # 电视棒能覆盖的下限
SCAN_HW_HI_HZ = 1700e6        # 上限
SCAN_MIN_MARGIN_DB = 6.0      # 判"有信号"的最小余量（相对扫出来的底噪）
# 一条命中占了多少格子 -> 猜它是什么制式：FM 广播占 ~200kHz，NFM/AM 只占一两格。
# 窄带信号光看频谱分不开 NFM 和 AM，所以只定这个界，另一个靠复核时换制式重试。
SCAN_WFM_BW_HZ = 100e3
# 门限附近的电平是抖的，会把一个电台的裙边切成好几段碎候选（实测 FM 广播被切成 5~6 段，
# 复核时间全浪费在这些碎片上）。隔这么多个格子以内还算同一簇，两头一并。
SCAN_CLUSTER_GAP_CELLS = 3
# 真正的"占用带宽"要按【峰值以下 6dB】量，不能按门限量：门限是用户为了滤弱信号设的，
# 设高一点就把 FM 广播的边带切掉了（实测 9dB 门限下，200kHz 的电台只剩 38kHz）。
SCAN_OCC_6DB = 6.0            # 占用带宽的判据：比峰值低这么多 dB
SCAN_OCC_MAX_HZ = 200e3       # 往两边最多找到这么远（别把隔壁台算进来）
SCAN_WFM_BW6_HZ = 60e3        # -6dB 带宽超过这个就当宽信号（FM 广播 ~200kHz）
# 宽信号（FM 广播）的判断/合并容差。真机实测：95.9 这个台不同趟的估计能在
# 95.901~95.962 之间飘（61kHz），-6dB 带宽只量到 88kHz（电台频谱中间强、两边弱），
# 所以宽信号直接按"广播级"容差 90kHz 走；窄带仍然只认一个格子。
SCAN_WFM_MERGE_HZ = 90e3
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
SCAN_HW_SETTLE_S = 0.0        # 额外等待；换频后的等待交给 SCAN_DISCARD_BLOCKS（那几块本就是脏数据）

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
        # 音频 AGC 的状态：包络参考与当前增益（只在静噪打开时自适应）
        self._agc_ref = 0.0
        self._agc_gain = 1.0
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
        # 增益用哪张档位表由调谐器型号决定（OTHER = RSP1 这类网络源，直通 -20~102dB）。
        # 必须先于 set_gain 设好，否则会按 R820T 的表把 RSP1 的增益压到 49.6 以下。
        self._tuner = 'R820T'
        self._ppm = 0

        # 硬件（调谐器）实际停在哪 —— 软件换频要拿它算 DDC 偏移
        self._hw_center = self._freq - DC_OFFSET_HZ
        self._scan = None             # 扫描状态机（None = 没在扫）
        self._scan_rate = RTL_RATE    # 数据源当前实际跑着的采样率（扫描提速时会提上去）
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
            # ★ 制式一变，噪声带宽就变了（WFM 200k vs NFM 12.5k），RSSI 基线整个平移：
            #   底噪必须重新学习，否则静噪会被卡在"永久打开"的状态（持续嘶嘶）。
            self._floor = None
            self._learn_until = self._elapsed + 0.5
            self._mute_until = self._elapsed + 0.2
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

    def set_tuner(self, name):
        """记录调谐器型号 —— 决定增益用哪张档位表（'OTHER' = RSP1 这类网络源，直通）。"""
        # ★ 增益/制式/调谐器一变，RSSI 基线整体平移：底噪必须重新学习。
        #   底噪只在"静噪关着"时更新（见 _tick 里的注释），而调高增益后静噪会一直开着，
        #   底噪于是再也不更新 → 门限永远被超过 → 喇叭持续嘶嘶，只能换频/重进收音机。
        self._floor = None
        self._learn_until = self._elapsed + 0.5
        self._mute_until = self._elapsed + 0.2

        self._tuner = str(name or 'R820T')
        return self._tuner

    def gain_apply(self):
        """把当前增益下发给数据源（按当前调谐器的表吸附过）。

        ★ 不用参考实现的 _src._ag()：它写死按 R820T 表吸附（最大 49.6 dB），
          对 RSP1/RSP2（0~102 dB）会把用户填的 60~80 dB 悄悄压回去 ——
          真机现象就是"能解码列车、但收音机声音很小，而且怎么调增益都没反应"。
          这里和预警器走同一个 snap_gain()，再直接下发 rtl_tcp 命令。
        """
        if self._src is None:
            return
        try:
            self._src._send_cmd(R.CMD_SET_GAINMODE, 1)
            self._src._send_cmd(R.CMD_SET_GAIN, int(round(self._gain_db * 10)))
        except Exception as e:
            print('LBJ-ERR 收音机增益下发失败: %s' % e, flush=True)

    def set_gain(self, db):
        """设增益。返回吸附后的实际值（界面显示用）。"""
        # ★ 增益/制式/调谐器一变，RSSI 基线整体平移：底噪必须重新学习。
        #   底噪只在"静噪关着"时更新（见 _tick 里的注释），而调高增益后静噪会一直开着，
        #   底噪于是再也不更新 → 门限永远被超过 → 喇叭持续嘶嘶，只能换频/重进收音机。
        self._floor = None
        self._learn_until = self._elapsed + 0.5
        self._mute_until = self._elapsed + 0.2

        self._gain_db = lbj_engine.snap_gain(self._tuner, db)
        R._g2['gain'] = self._gain_db      # 预警器重连时会用这个值重新下发
        self.gain_apply()
        return self._gain_db

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
            self._src._ah(self._ppm)
        except Exception:
            pass
        self.gain_apply()       # 走统一的吸附+下发（不能用 _ag：它写死按 R820T 表）

    def has_source(self):
        return self._src is not None

    def _restore_rate(self):
        """把数据源采样率还原成预警器要的 960k。

        ★ 扫描为了扫得快会把速率提到 2.4M（SCAN_RATE），而这条 TCP 连接【不会重连】——
          交回预警器前不还原的话，预警器的 DSP 链（半带 960k→240k、÷5→48k、1200 波特）
          会去解 2.4M 的流：频率/带宽整体错位、解不出任何车次，
          而且只有"停止→开始"（重连时 _reader_task 才重发速率）才恢复。
        """
        if self._src is None:
            return
        if int(getattr(self, '_scan_rate', RTL_RATE) or RTL_RATE) == int(RTL_RATE):
            return
        try:
            self._set_rate(RTL_RATE)
            print('LBJ: 交回预警器前把采样率还原为 %d' % int(RTL_RATE), flush=True)
        except Exception as e:
            print('LBJ-ERR 还原采样率失败: %s' % e, flush=True)
        self._scan_rate = int(RTL_RATE)

    def yield_source(self):
        """把数据源交出去（切回预警器），只停线程，不动连接。"""
        self.stop()
        self._restore_rate()
        src = self._src
        self._src = None
        return src

    def release_source(self):
        """彻底关掉数据源。"""
        self.stop()
        self._restore_rate()
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
                    # 扫描把速率提到过 2.4M：异常退出这条路以前不还原，
                    # 于是收音机/预警器都按错的速率继续跑（频率全错、解不出东西）。
                    self._restore_rate()
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

        # ---- 音频自动增益 ----
        # 只按"静噪打开"时的电平自适应：静噪关着的那段音频已经被清零，
        # 拿它当参考会把增益一路抬满，一开噪就是满幅嘶嘶声。
        audio = audio * p['gain']
        if audio.size:
            ref = float(np.sqrt(np.mean(audio * audio)))
            if self._open and ref > 1e-7:
                if self._agc_ref <= 1e-9:
                    self._agc_ref = ref
                elif ref > self._agc_ref:
                    self._agc_ref = (1.0 - AGC_ATTACK) * self._agc_ref + AGC_ATTACK * ref
                else:
                    self._agc_ref = (1.0 - AGC_RELEASE) * self._agc_ref + AGC_RELEASE * ref
        g = 1.0
        if self._open and self._agc_ref > 1e-9:
            g = AGC_TARGET / self._agc_ref
            if self._floor is not None and rssi < self._floor + AGC_MIN_SNR_DB:
                if g > AGC_WEAK_GAIN_MAX:
                    g = AGC_WEAK_GAIN_MAX
            if g < AGC_GAIN_MIN:
                g = AGC_GAIN_MIN
            elif g > AGC_GAIN_MAX:
                g = AGC_GAIN_MAX
        # 增益本身也平滑一下，避免逐块跳变（推子感）
        self._agc_gain = 0.5 * self._agc_gain + 0.5 * g
        audio = np.clip(audio * self._agc_gain * self._volume, -1.0, 1.0)
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

    def _scan_win_span(self):
        """一个硬件窗口能用多宽 = 当前(实测)采样率 × 0.833（取到 kHz）。

        960k -> 800kHz（和原来一致），实测 1.5M -> 1.28MHz，2.4M -> 2MHz。
        """
        r = float(self._scan_rate or RTL_RATE)
        return max(200e3, round(r * SCAN_WIN_FRACTION / 1000.0) * 1000.0)

    def _scan_wins(self, lo, span):
        """按当前采样率切窗口：两趟，第二趟错开半个窗口。

        ★ 为什么要两趟：直流尖峰固定落在【硬件窗口中心】，中心 ±SCAN_DC_GUARD_HZ
          内读数不可信，只扫一趟的话每隔一个窗口就有一条永久盲带（实测正好把测试
          信号埋了）。错开半窗后，第一趟的盲区落在第二趟的窗口中间，反过来也是，
          每个格子至少有一次是"在窗口中间"量到的 —— 顺便躲开窗口两侧的滚降。
        """
        w = self._scan_win_span()
        nwin = max(1, int(np.ceil(span / w)))
        wins = [lo + w * (k + 0.5) for k in range(nwin)]
        wins += [lo + w * (k + 1.0) for k in range(nwin)]
        return wins

    def start_scan(self, span_hz=None, fine=True, margin_db=None, rate_hz=None):
        """开始扫描：以当前频率为中心，上下各 span_hz/2 一路扫过去。

        margin_db：判定门限要比底噪高多少 dB（None = 按静噪档位自动推）。
        rate_hz  ：扫描期间用的采样率（None = 自动挑最快的 SCAN_RATE；
                   合成数据/不可提速的数据源传 RTL_RATE）。
        复核用的是【当前制式】（self._mode）—— 外面设成什么就扫什么，
        扫描窗里有制式切换键，扫的过程中也能换。
        """
        span = float(span_hz or SCAN_SPAN_DEFAULT_HZ)
        f0 = float(self._freq)
        lo = max(SCAN_HW_LO_HZ, f0 - span / 2.0)      # ★ 上下各一半，不是只往上扫
        hi = min(SCAN_HW_HI_HZ, f0 + span / 2.0)
        span = max(SCAN_STEP_HZ, hi - lo)
        self._scan_rate = int(rate_hz or SCAN_RATE)
        self._set_rate(self._scan_rate)
        wins = self._scan_wins(lo, span)
        nwin = len(wins) // 2
        self._scan_sq = self._squelch_on          # 扫描期间强制出声，结束时还原
        self._scan_last = None
        self._scan = {
            'phase': 'window',
            'start_hz': lo, 'end_hz': hi, 'span_hz': span,
            'fast': self._scan_rate > RTL_RATE,
            'rc_n': 0, 'rc_t0': time.time(),
            'blk_n': 0, 'smp_n': 0, 'sweep_t0': time.time(),
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
        print('LBJ: 扫描开始 %.4f~%.4f MHz（上下各 %.0f MHz，%d 窗口×2 趟，采样率 %d，'
              '自动微调=%s，门限%s）'
              % (lo / 1e6, hi / 1e6, span / 2e6, nwin, self._scan_rate, fine,
                 '自动' if margin_db is None else '底噪+%.0f dB' % float(margin_db)),
              flush=True)
        return True

    def stop_scan(self, keep_hz=None):
        """停扫描。keep_hz 给了就停在它上面（用户点"设为当前频率"时用）。"""
        s = self._scan
        if s is not None:
            s['stop'] = True
            if keep_hz:
                s['keep_hz'] = float(keep_hz)
        return True

    def clear_scan(self):
        """清空结果表（界面上的"清空"）。

        正在扫的那一轮也清 —— 只清界面的话，下一轮快照又把引擎里的结果填回来了。
        """
        for s in (self._scan, self._scan_last):
            if s is not None:
                s['results'] = []
        print('LBJ: 扫描结果已清空', flush=True)
        return True

    def _scan_tol(self, bw):
        """判断"是不是同一个信号"的频率容差。

        ★ 正在听 WFM 时直接给"广播级"90kHz：FM 广播自己就占 200kHz，电台之间至少隔
          200kHz（国内 100kHz 栅格也不会有两个台挤在一起），而同一个台不同趟的估计
          能飘几十 kHz（真机实测 95.9 裂成 95.901/95.962，97.4 裂成 97.452/97.399，
          而且这些碎片单独看往往只量到 12kHz 宽，靠带宽根本认不出是同一个台）。
        窄带（NFM/AM）则必须守住一个格子 —— 12.5kHz 的相邻信道不能并成一条。
        """
        b = float(bw or 0.0)
        if self._mode == 'WFM' or b >= SCAN_WFM_BW6_HZ:
            return max(SCAN_WFM_MERGE_HZ, b * 0.5)
        return max(SCAN_STEP_HZ * 0.9, b * 0.4)

    def _scan_merge_dups(self, s):
        """把结果表里"同一个台的两条"并成一条（次数加权平均，保留扫到次数多的）。"""
        out = []
        for r in sorted(s.get('results') or [], key=lambda x: -int(x.get('n', 1))):
            hit = None
            for g in out:
                if abs(r['freq'] - g['freq']) <= max(self._scan_tol(r.get('bw')),
                                                     self._scan_tol(g.get('bw'))):
                    hit = g
                    break
            if hit is None:
                out.append(r)
            else:
                n1 = int(hit.get('n', 1))
                n2 = int(r.get('n', 1))
                hit['freq'] = round((hit['freq'] * n1 + r['freq'] * n2) / float(n1 + n2), 1)
                hit['db'] = round((hit['db'] * n1 + r['db'] * n2) / float(n1 + n2), 1)
                hit['n'] = n1 + n2
                hit['ovl'] = bool(hit.get('ovl')) or bool(r.get('ovl'))
        if len(out) != len(s.get('results') or []):
            print('LBJ: 结果表里同一个信号并成一条：%d -> %d'
                  % (len(s.get('results') or []), len(out)), flush=True)
        s['results'] = out
        return out

    def _scan_put_result(self, s, h, fr, avg, ovl):
        """把一条复核通过的命中并进结果表；重复扫到就按次平均，越扫越准。"""
        tol = self._scan_tol(h.get('bw6') or h.get('bw'))
        ex = None
        best_d = None
        for r0 in s['results']:
            d0 = abs(float(r0.get('freq') or 0.0) - fr)
            if d0 <= max(tol, self._scan_tol(r0.get('bw'))) and (best_d is None or d0 < best_d):
                ex, best_d = r0, d0
        if ex is not None:
            # ★ 同一个信号每再扫到一次，就把它和已有估计【平均】一次：
            #   单次估计受调制影响会偏（实测 95.9 会读成 95.9123），多趟平均往真值收敛。
            n0 = int(ex.get('n', 1))
            n1 = min(n0, 30)          # 上限：老数据不能永远压着，环境变了要能跟上
            ex['freq'] = round((ex['freq'] * n1 + fr) / (n1 + 1), 1)
            ex['n'] = n0 + 1
            m0 = min(n0, 9)
            ex['db'] = round((ex['db'] * m0 + avg) / (m0 + 1), 1)
            ex['ovl'] = ovl
            ex['mode'] = self._mode
            ex['bw'] = h.get('bw6') or h.get('bw')
            if n0 % 5 == 0:
                print('LBJ: 复扫 %.4f MHz -> 修正为 %.4f MHz（第 %d 次，共 %d 个）'
                      % (fr / 1e6, ex['freq'] / 1e6, ex['n'], len(s['results'])), flush=True)
                self._scan_merge_dups(s)
            return True
        if len(s['results']) < SCAN_MAX_RESULTS:
            s['results'].append({'freq': fr, 'db': round(avg, 1), 'n': 1,
                                 'cur': False, 'ovl': ovl,
                                 'mode': self._mode,
                                 'bw': h.get('bw6') or h.get('bw')})
            print('LBJ: 扫描命中 %.4f MHz  %.1f dB  %s（占用约 %.0f kHz）%s'
                  % (fr / 1e6, avg, self._mode,
                     (h.get('bw6') or h.get('bw') or 0.0) / 1e3,
                     '  ★过载：频率不可信，请降低增益或拉远距离' if ovl else ''), flush=True)
            self._scan_merge_dups(s)
            return True
        return False

    def _set_rate(self, rate):
        """切数据源采样率（扫描提速用），并记住实际值 —— FFT 靠它把 bin 换算成 Hz。"""
        self._scan_rate = int(rate)
        if self._src is None:
            return
        try:
            self._src._send_cmd(R.CMD_SET_SAMPLERATE, int(rate))
        except Exception as e:
            print('LBJ: 切采样率失败 %s' % e, flush=True)

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
        blk_s = len(iq) / float(self._scan_rate or RTL_RATE)
        s['blk_n'] = int(s.get('blk_n', 0)) + 1      # 这一趟的块数/采样数（用来算真实速度）
        s['smp_n'] = int(s.get('smp_n', 0)) + len(iq)

        # ---- 量真实采样率（驱动可能不认请求值，块大小也不固定）----
        # 只有连着真实数据源才量：合成数据/离线测试没有真实时序，量出来是假的。
        if self._src is not None and s.get('rc_n', 0) < SCAN_RATE_CHECK_N:
            s['rc_n'] = int(s.get('rc_n', 0)) + 1
            if s['rc_n'] == SCAN_RATE_CHECK_N:
                dt = (time.time() - float(s.get('rc_t0') or time.time())) / (SCAN_RATE_CHECK_N - 1)
                blk = max(1, len(iq))
                real = blk / dt if dt > 1e-6 else float(RTL_RATE)
                real = max(SCAN_RATE_MIN, min(SCAN_RATE_MAX, real))
                if abs(real - float(self._scan_rate or RTL_RATE)) > SCAN_RATE_TOL:
                    # 假定值不对：改用它当尺子，并按新窗口宽重排这一趟
                    # （重排前先清掉用旧尺子量出来的格子，那些频率是错的）
                    self._scan_rate = real
                    s['wins'] = self._scan_wins(s['start_hz'], s['span_hz'])
                    s['wi'] = 0
                    s['meas'] = []
                    s['discard_left'] = SCAN_DISCARD_BLOCKS
                    s['phase'] = 'window'
                    s['fast'] = real > RTL_RATE * 1.05
                    print('LBJ: 实测采样率 %.2f MS/s（请求 %d，块 %d 采样），'
                          '窗口改 %.0f kHz × %d 个/趟'
                          % (real / 1e6, SCAN_RATE, blk, self._scan_win_span() / 1e3,
                             len(s['wins']) // 2), flush=True)
                    return
                s['fast'] = self._scan_rate > RTL_RATE * 1.05
        ph = s['phase']

        # ---- 换硬件窗口 ----
        if ph == 'window':
            if s['stop'] or s['wi'] >= len(s['wins']):
                self._scan_finish_sweep(s)
                return
            c = s['wins'][s['wi']]
            s['cur_hz'] = c
            self._tune_hw(c)
            blk_n = int(SCAN_HW_SETTLE_S * (self._scan_rate or RTL_RATE) / max(1, len(iq)))
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
                s['blk_n'] = 0; s['smp_n'] = 0
                s['sweep_t0'] = time.time()
                if s.get('fast'):
                    self._set_rate(SCAN_RATE)     # 复核用的 960k 切回快速档
                return
            h = s['hits'][s['hi']]
            if s.get('vset') != h['freq']:
                # 复核就用【用户当前设的制式】（扫描窗里有制式切换键），不自动分类：
                # 外面设 NFM 就按 NFM 听、设 AM 就按 AM 听，扫出来的就是那一类信号。
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
                    self._scan_put_result(s, h, fr, avg, ovl)
                    s['hi'] += 1
                    s['vset'] = None
                else:
                    # 没过就丢掉：复核用的 RSSI 是"信道内的总功率"，NFM 12kHz 和
                    #   AM 9kHz 量出来几乎一样，换个制式再听一遍只是白花一倍时间
                    #   （实测每个候选 2 秒）。所以要扫哪一类，就在外面把制式设成那一类。
                    s['hi'] += 1
                    s['vset'] = None
            return

        # ---- 收尾 ----
        if ph == 'done':
            res = s['results']
            self._set_rate(RTL_RATE)                  # 扫描结束恢复正常收听
            self._squelch_on = getattr(self, '_scan_sq', True)
            self._scan_last = s
            self._scan = None
            if s.get('keep_hz'):
                # 用户在列表里点了"设为当前频率"：听他的，别自作主张停在最强信号上
                self.set_frequency(s['keep_hz'])
                for it in res:
                    if abs(float(it.get('freq') or 0) - float(s['keep_hz'])) < 1.0:
                        it['cur'] = True
            elif res:
                best = max(res, key=lambda r: r.get('db') or -999)
                self.set_frequency(best['freq'])       # 停在最强那个信号上
                # ★ "当前收听"标记：Kotlin 的扫描结果表用它显示"（当前收听）"，
                #   以前全代码没有一处置 True，这个标记永远不会出现。
                best['cur'] = True
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
            # ★ 校准会临时把收音机调到 FM 广播台量载波；用户原来在听的频率必须记下来，
            #   结束（或中途取消）时调回去。不记的话 Kotlin 会把"引擎频率变了"当成
            #   用户调谐，把 FM 台写回当前信道 —— 点一次校准，信道就变成 87~108MHz。
            'orig_freq': float(self._freq),
            'offset_hz': None, 'meas_hz': None, 'ppm_delta': None,
            'ppm_suggest': None, 'resid_hz': None,
        }
        print('LBJ: 自动 PPM 校准开始（FM %.1f~%.1f MHz，%d 个窗口）'
              % (lo / 1e6, hi / 1e6, nwin), flush=True)
        return {'ok': True, 'why': ''}

    def stop_autocalib(self):
        if self._autocal is not None:
            self._autocal['stop'] = True
            # 取消也要调回原频率：这时可能已经调到 FM 台在量载波了
            try:
                of = self._autocal.get('orig_freq')
                if of:
                    self.set_frequency(float(of))
            except Exception:
                pass
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
        # 校准时听的是 FM 广播台：结束就把收音机调回用户原来在听的频率
        try:
            of = c.get('orig_freq')
            if of:
                self.set_frequency(float(of))
        except Exception as e:
            print('LBJ-ERR 校准后恢复收听频率失败: %s' % e, flush=True)
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
        # ★ ppm 建议值必须夹紧：dc 只要偏 0.1 就是 ~12kHz≈122ppm 的误判，
        #   而 libmirisdr/librtlsdr 的 ppm 会直接写进晶振校正 —— 不夹的话会越校越偏。
        c['ppm_suggest'] = int(round(c['ppm0'] + delta))
        if c['ppm_suggest'] > 200: c['ppm_suggest'] = 200
        if c['ppm_suggest'] < -200: c['ppm_suggest'] = -200
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
        rate = float(self._scan_rate or RTL_RATE)
        df = rate / float(n)
        half = max(1, int(SCAN_STEP_HZ / df / 2.0))
        win_span = self._scan_win_span()
        ncell = int(win_span / SCAN_STEP_HZ / 2.0)
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
        # ★ 自动门限算出的 margin 也要写回：_scan_snapshot 发的是 s.get('margin')，
        #   不写的话界面永远显示不出"（底噪+X）"。
        s['margin'] = round(float(margin), 1)

        hits = []
        cur = None
        for idx, (fq, (d, fpk)) in enumerate(items):
            if d >= thr:
                w = 10.0 ** (d / 10.0)
                # 隔着几个格子的低谷不算断开：一个电台的裙边会围着门限上下抖
                if cur is not None and idx - int(cur['last']) > SCAN_CLUSTER_GAP_CELLS:
                    hits.append(cur)
                    cur = None
                if cur is None:
                    cur = {'freq': fq, 'db': d, 'w': w, 'wf': w * fq, 'fpk': fpk,
                           'cells': 1, 'last': idx, 'f_lo': fq, 'f_hi': fq}
                else:
                    cur['w'] += w
                    cur['wf'] += w * fq
                    cur['cells'] = int(cur.get('cells', 1)) + 1
                    if d > cur['db']:
                        cur['freq'], cur['db'], cur['fpk'] = fq, d, fpk
                cur['last'] = idx
                cur['f_hi'] = fq
        if cur is not None:
            hits.append(cur)
        for h in hits:
            # 峰值 bin 估计（准）；万一是平的没插出来，退回能量质心
            h['cf'] = h.get('fpk') or (h['wf'] / h['w'] if h['w'] > 0 else h['freq'])
            h['bw'] = round(float(h.get('cells', 1)) * SCAN_STEP_HZ, 1)   # 门限以上的宽度
            h['mode'] = self._mode              # 复核时用的制式（下面显示/存信道都用它）
            # 从峰值格子往两边找 -6dB 边界，得到真实占用带宽
            step = float(SCAN_STEP_HZ)
            lim = int(SCAN_OCC_MAX_HZ / step)
            lvl = float(h['db']) - SCAN_OCC_6DB
            fpk_f = float(h['freq'])
            lo_f = hi_f = fpk_f
            # ★ 不能"碰到第一个低谷就停"：FM 频谱里载波/导频之间有深谷，一停就把
            #   200kHz 的电台量成 12kHz。取 ±200kHz 内【最远】那个还在 -6dB 以上的格子。
            for k in range(1, lim + 1):
                f = fpk_f - k * step
                v = best.get(f)
                if v is not None and v[0] >= lvl:
                    lo_f = f
            for k in range(1, lim + 1):
                f = fpk_f + k * step
                v = best.get(f)
                if v is not None and v[0] >= lvl:
                    hi_f = f
            h['bw6'] = round(hi_f - lo_f + step, 1)
            if h['bw6'] >= SCAN_WFM_BW6_HZ and (float(h['db']) - floor) >= 12.0:
                # ★ 宽信号（FM 广播）报【-6dB 占用带的正中】，不报最强格子：
                #   调制带里最强的那一格随节目内容乱跑（实测能偏 ±75kHz），
                #   而占用带中心就是电台的载波频率，稳得多。
                h['cf'] = (lo_f + hi_f) / 2.0
        hits.sort(key=lambda h: -h['db'])
        # ★ 强信号的占用带里如果还站着别的命中，那是同一个电台的边带碎块
        #   （实测 95.9 会在 95.82 处再报一条），只留最强的那条。
        keep = []
        for h in hits:
            inside = False
            for g in keep:
                if abs(h['freq'] - g['freq']) <= self._scan_tol(g.get('bw6') or g.get('bw')):
                    inside = True
                    break
            if not inside:
                keep.append(h)
        if len(keep) != len(hits):
            print('LBJ: 合并同一信号 %d 条 -> %d 条' % (len(hits), len(keep)), flush=True)
        hits = keep
        s['hits'] = hits[:SCAN_MAX_RESULTS]
        self._set_rate(RTL_RATE)     # 复核要走正常 DSP，那个是按 960k 搭的
        s['phase'] = 'verify'
        s['hi'] = 0
        s['vset'] = None
        # 速度实测：一趟用了多久、跑掉的块数和采样数（采样数/时间 = 数据源真实速率）
        el = max(1e-3, time.time() - float(s.get('sweep_t0') or time.time()))
        nb = max(1, int(s.get('blk_n', 0)))
        ns = int(s.get('smp_n', 0))
        print('LBJ: 扫描完成 %d 格（%d 格有效）  底噪 %.1f  门限 %.1f（底噪+%.1f）  候选 %d'
              '  用时 %.1fs/趟（%d 块 %.0f kS/s 平均块 %d）'
              % (len(m), len(items), floor, thr, margin, len(s['hits']),
                 el, nb, ns / el / 1000.0, ns // nb), flush=True)


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
            'margin': s.get('margin'), 'rate': int(self._scan_rate or RTL_RATE),
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
