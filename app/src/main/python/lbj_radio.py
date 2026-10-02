# -*- coding: utf-8 -*-
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

# 弱信号自动高切的最低音频带宽（Hz）
HC_MIN_HZ = 2000.0
# 判强弱的 RSSI 区间（dB）。实测：纯噪声底约 -46~-55，真实信号约 -20~-38。
#
# ★ 这里只能用【绝对 RSSI】，不能拿"相对跟踪底噪"来判断 ——
#   FM 是恒包络，持续信号下底噪跟踪会跟到信号本身，信噪比恒为 0，
#   结果强信号也被当成弱信号压窄带宽（测试抓出来过）。
HC_RSSI_LO = -58.0
HC_RSSI_HI = -35.0

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
        self._ppm = int(ppm)
        if self._src is not None:
            try:
                self._src._ah(self._ppm)
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
            audio = self._adec.process(audio)
        else:
            x = self._dec.process(x)
            rssi = 10.0 * np.log10(float(np.vdot(x, x).real) / max(1, len(x)) + 1e-12)
            if self._mode == 'NFM':
                audio = self._fm.process(x).astype(np.float64)
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
            'err': self._err,
        }

    def snapshot_json(self):
        return json.dumps(self.snapshot(), ensure_ascii=False)
