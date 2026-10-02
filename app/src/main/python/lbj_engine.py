# -*- coding: utf-8 -*-
"""
LBJ 接收机 —— Android 引擎（驱动层）

设计原则：**不重写任何 DSP / 协议代码**。
所有的下变频、AFC、RSSI 门控、POCSAG 解码、BCH 纠错、LBJ 报文解析
全部直接调用 lbj_ref.py（即 Sdr-Is-Fun/RTL_SDR_LBJ_RECEIVER 原作者代码，GPL-3.0）。

本文件只负责三件事：
  1. 按 main() 的顺序把各个组件装配起来
  2. 跑后台线程读 rtl_tcp 数据流
  3. 把状态以 JSON 推给 Android UI

原作者：Sdr-Is-Fun  https://github.com/Sdr-Is-Fun/RTL_SDR_LBJ_RECEIVER
本文件同样以 GPL-3.0 发布。
"""
import json
import os
import re
import sys
import threading
import time
import types
import numpy as np


# ---------------------------------------------------------------------------
# scipy 兼容层
#
# lbj_ref.py 只用到 scipy.signal 的 3 个函数：firwin / lfilter / lfilter_zi。
# Android 上装 scipy 会让 APK 增大 ~40 MB，因此这里用 numpy 提供等价实现。
#
# 已验证的等价性（tools/test_shim.py）：
#   lfilter       —— 分块流式输出与真 scipy 差 < 3e-16，等价
#   lfilter_zi    —— 状态长度一致，等价
#   三个 blackmanharris 窗的 firwin —— 与真 scipy 差 < 1e-6，等价
#   gaussian 窗的 firwin —— 曾漏掉 scale=True 的 DC 归一化（DC 增益 0.7256 而非 1.0），
#                          现已修正；修正后与真 scipy 一致
# 注意：不要在这里写"完全等价"这种没有测试支撑的话。
#
# 设置环境变量 LBJ_USE_REAL_SCIPY=1 可强制使用真正的 scipy。
# ---------------------------------------------------------------------------
def _win(numtaps, window):
    """复刻 scipy.signal.get_window 本工程用到的几种窗"""
    n = np.arange(numtaps, dtype=float)
    m = float(numtaps - 1) if numtaps > 1 else 1.0
    if window is None:
        return np.ones(numtaps, dtype=float)
    if isinstance(window, (tuple, list)) and len(window) >= 2:
        name = str(window[0]).lower()
        p = float(window[1])
        if name == 'gaussian':
            return np.exp(-0.5 * ((n - m / 2.0) / p) ** 2)
        if name == 'kaiser':
            try:
                from numpy import i0
                return i0(p * np.sqrt(np.maximum(0.0, 1.0 - ((2.0 * n / m) - 1.0) ** 2))) / i0(p)
            except Exception:
                return np.ones(numtaps, dtype=float)
    name = str(window).lower()
    if name == 'blackmanharris':
        a = (0.35875, 0.48829, 0.14128, 0.01168)
        return (a[0] - a[1] * np.cos(2 * np.pi * n / m)
                + a[2] * np.cos(4 * np.pi * n / m)
                - a[3] * np.cos(6 * np.pi * n / m))
    if name == 'hamming':
        return np.hamming(numtaps)
    if name in ('hann', 'hanning'):
        return np.hanning(numtaps)
    if name == 'blackman':
        return np.blackman(numtaps)
    return np.ones(numtaps, dtype=float)


def _install_scipy_shim():
    sig = types.ModuleType('scipy.signal')

    def firwin(numtaps, cutoff, fs=None, window=None, **kw):
        n = np.arange(numtaps, dtype=float) - (numtaps - 1) / 2.0
        c = np.atleast_1d(np.asarray(cutoff, dtype=float))
        if fs is not None:
            c = c / (float(fs) / 2.0)      # 归一化到 Nyquist
        h = np.zeros(numtaps, dtype=float)
        for cc in c:
            h += cc * np.sinc(cc * n)      # 单一截止频率的低通
        h = h * _win(numtaps, window)
        # scipy 的 scale=True（默认值）会把低通归一化到 DC 增益 = 1。
        # 漏掉这一步，通带会整体小一截（实测 b_smooth 的 DC 增益只有 0.7256）。
        # 因为 _d8 的门限是自适应峰值、整体尺度不变，所以它不是当前故障，
        # 但会白白吃掉弱信号余量，且让"与 scipy 等价"的说法不成立。
        if kw.get('scale', True):
            s = h.sum()
            if s != 0.0:
                h = h / s
        return h

    def lfilter_zi(b, a):
        nb = len(np.atleast_1d(np.asarray(b)))
        na = len(np.atleast_1d(np.asarray(a)))
        return np.zeros(max(nb, na) - 1, dtype=float)

    def lfilter(b, a, x, zi=None):
        b = np.atleast_1d(np.asarray(b, dtype=float))
        a = np.atleast_1d(np.asarray(a, dtype=float))
        x = np.asarray(x).ravel()
        if a.size != 1 or a[0] != 1.0:
            raise NotImplementedError('scipy shim: only a=[1.0] is supported')
        # 本工程里 lfilter 既用于实数音频，也用于复数 IQ，必须保持 dtype
        dt = np.complex128 if np.iscomplexobj(x) else np.float64
        n = b.size
        if n == 1:
            return np.asarray(b[0] * x, dtype=dt), np.zeros(0, dtype=dt)
        # 状态用「最近 n-1 个输入样本」表示（与本层自身返回的状态自洽）
        if zi is None:
            hist = np.zeros(n - 1, dtype=dt)
        else:
            hist = np.asarray(zi).ravel()
            if np.iscomplexobj(hist) and not np.iscomplexobj(x):
                hist = hist.real
            hist = hist.astype(dt, copy=False)
            if hist.size < n - 1:
                hist = np.concatenate([np.zeros(n - 1 - hist.size, dtype=dt), hist])
            elif hist.size > n - 1:
                hist = hist[-(n - 1):]
        ext = np.concatenate([hist, x.astype(dt, copy=False)])
        y = np.convolve(ext, b)[n - 1:n - 1 + x.size]
        return y, ext[-(n - 1):]

    sig.firwin = firwin
    sig.lfilter = lfilter
    sig.lfilter_zi = lfilter_zi
    pkg = types.ModuleType('scipy')
    pkg.signal = sig
    sys.modules['scipy'] = pkg
    sys.modules['scipy.signal'] = sig


_FORCE_SHIM = os.environ.get('LBJ_FORCE_SHIM') == '1'
_HAVE_SCIPY = False
if not _FORCE_SHIM:
    try:
        import scipy.signal as _probe  # noqa: F401
        _HAVE_SCIPY = True
    except Exception:
        _HAVE_SCIPY = False
if not _HAVE_SCIPY:
    _install_scipy_shim()


import lbj_ref as R

SPECTRUM_BINS = 32    # 频谱显示格数
ZOOM_HZ = 150000.0    # 频谱显示范围：目标频率 ±150 kHz


# ---------------------------------------------------------------------------
# 把父类里"发 Android 广播"的行为换成空操作（我们直接用 UI 回调）
# ---------------------------------------------------------------------------
def _noop_intent(self, *args, **kwargs):
    return None


R.LBJRealtimeDecoder.send_local_intent = _noop_intent


_HEXMAP = {'*': 'A', 'U': 'B', ' ': 'C', '-': 'D', ')': 'E', '(': 'F'}
_END_NAME = {'30': '无端', '31': 'A端', '32': 'B端'}

# 到达状态里代表"正在接近"的取值（来自 lbj_ref._A0）
_APPROACH_STATES = ('接近', '即将到达')

# 干扰预警里带 ANSI 颜色码（参考实现是给终端用的），显示到 TextView 上会变成
# "←[5m←[41m←[97m ⚠ ..." 这种乱码，推给界面前必须剥掉。
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')

# ---------------------------------------------------------------------------
# 自检互斥（必须是【模块级】）
#
# selftest() 会用临时引擎改写参考实现的模块级全局量 _g0/_g1/_g2，
# 而正在运行的采集线程读的正是同一批全局量 —— 两者会互相污染。
#
# 为什么不能用实例字段（self._paused）：
#   Kotlin 侧自检时是【新建一个 LbjEngine】再调 selftest_json()，
#   所以 self 是那个临时实例，对正在跑的实例毫无约束力。
#   实测：接收中点自检，真车报文会被临时引擎设的 strict+['99999'] 静默丢弃。
# 所以锁和标志都必须挂在模块上，才能跨实例生效。
# ---------------------------------------------------------------------------
def _train_digits(s):
    """取出车次里的数字部分：'K323' -> '323'，'323' -> '323'"""
    return ''.join(ch for ch in str(s) if ch.isdigit())


def _train_key(s):
    """车次比较用的规范形式：去掉空白、转大写。"""
    return re.sub(r'\s+', '', str(s or '')).upper()


def _same_train(a, b):
    """判断两个车次字符串是否指向【同一趟车】。

    一趟车会分两次被解出：
      · 基础帧只带数字部分（'323'）—— 此时机车/线路/端位/经纬度都还未知
      · 扩展帧带字母前缀（'K323'）—— 并补上机车/线路/端位/经纬度
    如果按整串比较，这两次会被当成两趟车，"最近列车"里就会出现成对的重复项
    （一行有机车、一行是 ----，速度公里标完全相同）。

    所以按"数字部分相同，且其中至少有一个是纯数字形式"来归并 ——
    既能合并同一趟车的两次，又不会把 G1 和 D1 这种真正不同的车误并。
    """
    a, b = str(a or ''), str(b or '')
    if not a or not b:
        return False
    if a == b:
        return True
    ad, bd = _train_digits(a), _train_digits(b)
    if not ad or ad != bd:
        return False
    return a.isdigit() or b.isdigit()


# ---------------------------------------------------------------------------
# 调谐器增益档位
#
# ★ 不同调谐器的有效增益档位【完全不同】，必须分开：
#     R820T ：0.0 ~ 49.6 dB（29 档）—— 现代电视棒绝大多数是这颗
#     FC0013：-9.9 ~ 19.7 dB（23 档）—— 便宜的蓝色小棒常见；有【负增益】档
#   用错表的后果：
#     · 界面显示的"增益实际 X dB"是假的（librtlsdr 内部还会再吸附一次）
#     · 请求 15.7 这种非 FC0013 档位的值时，硬件实际落到 17.9 或 7.1
#     · FC0013 的负增益档无法使用（强信号时本来可以降下来减少互调）
#
#   FC0013 这张表是从设备上实测读出来的（rtl_test -t 会打印
#   "Supported gain values (23): ..."），不是抄文档。
#   FC0012 与 FC0013 在 librtlsdr 里共用同一张表。
# ---------------------------------------------------------------------------
FC0013_GAINS = [-9.9, -7.3, -6.5, -6.3, -6.0, -5.8, -5.4,
                5.8, 6.1, 6.3, 6.5, 6.7, 6.8, 7.0, 7.1,
                17.9, 18.1, 18.2, 18.4, 18.6, 18.8, 19.1, 19.7]


_SELFTEST_LOCK = threading.RLock()
_SELFTEST_ACTIVE = False
_SELFTEST_STARTED_AT = 0.0
# ★ 看门狗上限。自检正常只要几秒，但万一 _SELFTEST_ACTIVE 因为任何意外
#   （线程被杀、解码里出现死循环……）卡在 True，采集线程就会【永远丢块】——
#   那就成了"一个测试功能把真实接收搞死"，是本末倒置。
#   超过这个时长一律当作自检已结束：宁可这一次自检结果不准，也绝不能让接收停摆。
_SELFTEST_MAX_S = 90.0


_SELFTEST_WARNED = False


def _selftest_running():
    """自检是否真的还在进行中（带看门狗）"""
    global _SELFTEST_WARNED
    if not _SELFTEST_ACTIVE:
        return False
    if time.time() - _SELFTEST_STARTED_AT > _SELFTEST_MAX_S:
        # 只警告一次：这个函数每处理一个数据块就会被调两次，
        # 每次都打印的话 logcat 会被瞬间刷满，反而盖住真正有用的信息。
        if not _SELFTEST_WARNED:
            _SELFTEST_WARNED = True
            print('LBJ-WARN 自检标志位已超时 %.0f 秒，强制恢复接收（本次自检结果可能不可信）'
                  % _SELFTEST_MAX_S, flush=True)
        return False
    return True


class _UiDecoder(R.LBJRealtimeDecoder):
    """只在父类基础上把"刷新界面"改成回调 UI。解码逻辑完全继承。"""

    def __init__(self, engine, estimator):
        super().__init__(R.BASEBAND_RATE, R.BAUD_RATE, arrival_estimator=estimator)
        self._engine = engine

    def decode_lbj(self, bcd, valid_for_eta=True):
        # 参考实现只解析 车次/速度/公里标/机车/线路；端位和经纬度它没取，这里顺手补上
        #（不改动原作者代码）。
        #
        # 注意：一趟车会分两次进入这里——先基础帧（车次/速度/公里标），后扩展帧
        #（机车/线路/端位/经纬度）。如果在基础帧时不清理，两次推送之间会显示出
        # 上一趟车的端位，看起来像"端位在跳"。所以基础帧到达时先清空附加信息。
        a = self.current_addr
        if a in (1233999, 1234000):
            self._engine.extra = {}
            # 合并帧（基础 15 字符 + 扩展 50 字符）里同样带端位/经纬度。
            # 只按 addr 白名单判断会把它们整段丢掉，症状是"机车和线路都解出来了，
            # 端位/经度/纬度却永远显示 ---"，而且 _line 永远没有样本 →
            # "GPS 定位本站"永远提示样本不足。所以这里补一次。
            if len(bcd) >= 65:
                self._engine._capture_extra(a, bcd)
        elif a in (1234001, 1234002):
            self._engine._capture_extra(a, bcd)
        return super().decode_lbj(bcd, valid_for_eta)

    def check_filter_and_update_ui(self):
        # 完全沿用父类的实现：关键词匹配、命中判定、严格模式拦截、状态字段更新。
        # 父类里唯一与 Android 有关的一步是 send_local_intent()（发 am broadcast），
        # 本文件顶部已把它替换成空操作，因此这里可以直接调用父类，逻辑零复制。
        #
        # ★ 乘车模式（ride_trains）在这里拦截：【自己坐的那趟车不调用父类】，
        #   于是 _g2 不会被它覆盖 —— 上面的大面板保持显示上一趟【别的】车，
        #   而它照样进下面「最近列车」并持续更新（见 _on_train）。
        #   src 必须传 _g1：此刻 _g2 还是上一趟车的数据，不能拿它当这趟车的信息源。
        train = R._g1.get('train', '----')
        if self._engine.is_ride_train(train):
            self._engine._on_train(train, src=R._g1)
            return False
        ok = super().check_filter_and_update_ui()
        if ok:
            self._engine._on_train(R._g2.get('train', '----'))
        return ok


class LbjEngine:

    def __init__(self, push=None):
        """push: 可调用对象，接收一个 JSON 字符串（Android 侧实现）"""
        self._push = push
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        self._src = None
        self._frontend = None
        self._decoder = None
        self._gate = None
        self._estimator = None
        self._err = ''
        self._last_push = 0.0
        self._spectrum = [-120.0] * SPECTRUM_BINS
        self._peak_hz = None
        self._peak_db = None
        self._peak_delta = None
        self._trains = []          # 最近解出的车次记录
        self._trains_seen = {}

        # 默认参数
        self.freq_mhz = 821.2375
        self.gain_db = 15.7
        self.tuner = 'R820T'          # 'R820T' 或 'FC0013'，决定用哪张增益档位表
        self.ppm = 1
        self.bw_khz = 35.0
        self.cs_threshold = -55.0
        self.afc_enabled = True
        self.afc_max_hz = 8000.0
        self.afc_gain = 0.45
        self.rssi_hold_ms = 700.0
        self.my_km = None
        # 用户（或 applyPrefs）自己设的本站公里标。乘车模式会用本车实时公里标
        # 覆盖 self.my_km，但【不能】污染这个值 —— 退出乘车模式要能恢复回来。
        self.user_my_km = None
        self.route_km = {}
        # 数据源地址。默认【硬编码】为本机 127.0.0.1 —— 也就是手机上那个驱动 App。
        # 绝对不要从 R.TCP_HOST 取默认值：那是模块级全局量，而 Chaquopy 的
        # Python 解释器在 App 进程内一直存活，上一次会话设过的值会残留下来，
        # 导致"取消台架模式后仍然去连电脑"。这里必须每次都给确定的初值。
        self.tcp_host = '127.0.0.1'
        self.tcp_port = 1234
        # 注意：这里【不再】用实例字段来控制自检暂停。
        # 因为 Kotlin 侧自检时用的是另一个引擎实例，实例字段对
        # 正在采集的那个毫无约束力 —— 必须用模块级的 _SELFTEST_ACTIVE。
        # 采集线程"代数"。stop() 会 +1 使当前线程作废，被遗弃的线程即使晚几秒
        # 才从 read() 里醒过来，也不允许再把状态推给界面（详见 stop/_loop）。
        self._gen = 0
        # pause() 之后连接是否还留着（'停止→开始' 靠它做到不重连）
        self._paused_conn = False
        # 是否已经彻底断开。注意不能拿 _paused_conn 当"连没连上"用 ——
        # 那个标志只有 pause() 才会置位，于是 stop() 之前问它永远得到 False
        # （实测踩过：结果每次都走完整断开，驱动被反复重启）。
        self._stopped = True
        # 数据源是否真的 open 成功过。setup() 里就把 _src 建好了（但没连），
        # 所以光看 _src 是不是 None、_src._error 是不是空，
        # 会把"还没连"误判成"已连接"（实测踩过）。
        self._opened = False
        self.extra = {}               # 端位 / 经纬度（参考实现未解析的字段）
        # 线路里程样本：route -> [(公里标, 纬度, 经度, 时间), ...]
        # LBJ 报文里同时带"公里标"和"经纬度"，所以可以让列车替我们把
        # "铁路里程 ↔ 地理坐标"的对应关系丈量出来，不需要任何外部地图数据。
        self._line = {}
        # 显示 / 过滤开关（对应参考实现的 B/W/M/F 键）
        self.keywords = []
        self.filter_mode = 'highlight'
        self.strict_filter = True
        self.show_err_warn = True
        # 乘车模式（自己坐的车次）：这些车【只】在下面「最近列车」里持续更新，
        # 不刷上面的大面板。用于乘车时监听——自己那趟车会周期性重发，
        # 不屏蔽的话上面一直它刷屏，旁边路过的车次反而看不见。
        self.ride_trains = []

    # ---------------------------------------------------------------- 装配
    def setup(self):
        """完全照抄参考实现 main() 的装配顺序。"""
        self._stopped = False
        self._opened = False
        # 第一件事就是清掉上一场会话留在模块全局量里的仪表盘数据。
        # 不做的话，新引擎的第一帧就会把上一趟车原样显示出来。
        self._reset_session_state()
        sample_rate = R.RTL_SAMPLE_RATE
        dc_hz = R.DEFAULT_DC_OFFSET_HZ
        fc = self.freq_mhz * 1000000.0
        hw_tune = fc - dc_hz

        R._g2['freq'] = fc
        R._g2['gain'] = self.gain_db
        R._g2['ppm'] = self.ppm
        R._g2['dc_offset_hz'] = dc_hz
        R._g2['bw_khz'] = self.bw_khz
        R._g2['cs_threshold'] = self.cs_threshold

        # 记住建源所需参数：驱动刚被拉起时端口可能还没监听，需要重建数据源重试连接
        self._hw_tune = hw_tune
        self._dc_hz = dc_hz
        self._sample_rate = sample_rate
        self._src = self._make_src()
        self._opened = False          # 刚建好、还没连
        self._frontend = R._D7(sample_rate, R.HALFBAND_STAGES, R.MID_RATE,
                               dc_offset=dc_hz, user_offset=0.0,
                               bw=self.bw_khz * 1000.0, rssi_offset=0.0,
                               afc_enable=self.afc_enabled,
                               afc_max_hz=self.afc_max_hz,
                               afc_gain=self.afc_gain)
        self._estimator = R._A0(user_km=self.my_km,
                                max_seconds=R.DEFAULT_ETA_MAX_SECONDS,
                                route_km_map=dict(self.route_km))
        R._g2['_arrival_estimator'] = self._estimator
        self._decoder = _UiDecoder(self, self._estimator)
        self._gate = R._A1(on_db=self.cs_threshold,
                           hysteresis_db=R.DEFAULT_RSSI_HYST_DB,
                           hold_ms=self.rssi_hold_ms,
                           confirm_blocks=1,
                           enabled=True)
        # 把显示/过滤开关同步给参考实现的全局状态
        R._g0['keywords'] = list(self.keywords)
        R._g0['filter_mode'] = self.filter_mode
        R._g0['strict_filter'] = self.strict_filter
        R._g0['show_err_warn'] = self.show_err_warn
        return self

    def _make_src(self):
        return R._A2(self.tcp_host, self.tcp_port, self._hw_tune, self._sample_rate,
                     R.BLOCK_SIZE, dc_offset=self._dc_hz)

    # ---------------------------------------------------------------- 运行
    def start(self, launch_driver=None):
        """launch_driver: Android 侧用来拉起 RTL-SDR 驱动 App 的可调用对象"""
        self.stop()
        self.setup()
        self._err = ''
        # Android 上由 Kotlin 负责发 iqsrc:// Intent，Python 侧不再自己 subprocess
        R._A2._start_android_driver = lambda self_: None
        if launch_driver is not None:
            try:
                launch_driver()
            except Exception as e:
                self._err = '拉起驱动失败: %s' % e
        R._g0['running'] = True
        self._running = True
        # 注意：start() 【不再】自己连接。
        # 连接由 connect() 分步完成 —— 这样 Kotlin 侧可以先短试一次：
        #   连上了  -> 说明驱动本来就在跑，直接复用，【不要】再去拉起它
        #   连不上  -> 才去拉起驱动，然后再长试一次
        # 这条路径不需要任何"端口探测"，因此不会误伤驱动（见 docs/33）。
        print('LBJ: 已装配，等待连接 %s:%d' % (self.tcp_host, self.tcp_port), flush=True)
        return True

    def connect(self, timeout_s=25.0, settle_s=6.0, fail_fast_refused=False):
        """尝试连接数据源，返回 True/False。可以分次调用（先短试、再长试）。

        settle_s：判定"已经连上"所需的静默时长。
          · 收到数据  -> 立刻算成功（最可靠）
          · 没数据但也没报错，且已过 settle_s -> 也算成功
          ★ 本机 loopback 上"连接被拒"是【瞬间】返回的，
            所以短试(settle_s≈2.5)足以分辨"驱动在跑"和"驱动没跑"。
        """
        ok = self._open_with_retry(float(timeout_s), float(settle_s),
                                   bool(fail_fast_refused))
        if ok and (self._thread is None or not self._thread.is_alive()):
            self._running = True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            print('LBJ: 采集线程已启动', flush=True)
        return ok

    def _open_with_retry(self, timeout_s=25.0, settle_s=6.0, fail_fast_refused=False):
        """等待数据源就绪 —— 只做"连接被拒就重试"，绝不做"连上再断开"。

        ★ 为什么不能用"先探测端口再连"的写法：
          rtl_tcp_andro 的设计是【一次只服务一个客户端，客户端一走就把整个服务停掉】。
          端口探测一连上就关闭，驱动会立刻报
              serveClient cannot send to client. Code 32, exception Broken pipe
          然后 "TCP server shutting down" 直接退出 ——
          我们本意是确认它就绪，结果把它弄死了。
          而【连接被拒】是在驱动侧什么都不留下的（根本没建立连接），所以重试完全安全。

        返回 True 表示已连上。
        """
        deadline = time.time() + timeout_s
        attempt = 0
        # 每次进入都从一个【全新且未打开】的数据源开始：
        # connect() 可能被分次调用（先短试再长试），上一次失败的数据源不能复用。
        try:
            if self._src is not None:
                self._src.close()
        except Exception:
            pass
        self._src = self._make_src()
        self._opened = False       # 换了数据源就要重新连
        while time.time() < deadline:
            attempt += 1
            self._src.open()
            # ★ 判定"这一次到底连上没有"，唯一不会骗人的信号是【队列里真的收到数据了】。
            #
            #   踩过的两个坑：
            #   ① 只等 0.5 秒就假定成功 —— 实测"连接被拒"最长要 2 秒才报错，会把失败当成成功；
            #   ② 用 getpeername() 判断 —— 实测【连接进行中】它也会返回对端地址，
            #      于是地址不可达时同样被误判成成功。
            #   reader 线程只有真正连上、收到数据之后才会往 _q 里放东西，所以它最可靠。
            #   数据源的 socket 超时是 5 秒，所以等 8 秒足以拿到明确结论。
            attempt_start = time.time()
            attempt_end = min(deadline, attempt_start + max(8.0, settle_s + 2.0))
            got_data = False
            while time.time() < attempt_end:
                if self._src._error:
                    break
                if self._src._q.qsize() > 0:
                    got_data = True
                    break
                time.sleep(0.15)
            if not self._src._error and got_data:
                # ★ 连上了必须把之前失败留下的错误信息清掉。
                #   否则会出现这种自相矛盾的界面：频谱在动、RSSI 有读数（说明确实连上了），
                #   标题栏却还挂着上一次"连接被拒"的旧消息。
                #   connect() 会被分次调用（先短试、再长试），上一次失败写进 self._err 的内容
                #   不会自动消失 —— 实测就踩了这个。
                self._err = ''
                self._opened = True
                if attempt > 1:
                    print('LBJ: 第 %d 次尝试连接成功' % attempt, flush=True)
                return True
            # 没收到数据、但也没报错，且已经过了 settle_s —— 认为已连上。
            # settle_s 必须大于"连接被拒"的返回时间，否则会把失败误判成成功。
            # 本机 loopback 上是瞬间返回的；Windows 上实测最长 2.1 秒，所以短试取 2.5 秒。
            if not self._src._error and (time.time() - attempt_start) >= settle_s:
                self._err = ''          # 同上：连上了就清掉旧的失败信息
                self._opened = True
                print('LBJ: 已连接（%.1f 秒内未出现错误），暂时没有数据' % settle_s, flush=True)
                return True
            if not self._src._error:
                self._src._error = '等待连接结果超时'
            msg = str(self._src._error)
            # 注意：错误文案是"【连接被**拒**】"，不是"拒绝"—— 一个字的差别
            # 会让判断永远不成立，重试直接失效（这里踩过）。
            retryable = ('拒' in msg) or ('refused' in msg.lower())
            if fail_fast_refused and retryable:
                # 调用方要求"端口没人监听就立刻放弃"（不要在这里傻等）。
                # 用途：App 先用短试判断驱动在不在跑 —— 不在跑就马上去拉起它，
                # 没必要把短试的超时时间耗完。
                print('LBJ: 端口没人监听，立即返回以便去拉起驱动', flush=True)
                self._err = msg
                return False
            if not retryable:
                # 不是"还没起来"，而是别的问题（网络不可达等），不必再等
                print('LBJ: 连接失败且不可重试：%s' % msg, flush=True)
                self._err = msg
                return False
            if time.time() >= deadline:
                print('LBJ: 等待数据源超时（%s）' % msg, flush=True)
                self._err = msg
                return False
            try:
                self._src.close()
            except Exception:
                pass
            time.sleep(0.5)
            self._src = self._make_src()      # 换一个新的数据源再试
            self._opened = False
            print('LBJ: 数据源还没就绪（%s），重试第 %d 次…' % (msg, attempt + 1), flush=True)
        self._err = self._err or '等待数据源超时'
        return False

    def pause(self):
        """停止解码，但【保持与驱动的连接】。

        ★ 为什么要这样：rtl_tcp_andro 是"客户端一走就把整个服务器停掉"的设计。
          于是每次"停止→开始"都要重新拉起驱动，而快速反复启停会让它来不及
          收拾上一个会话 —— 实测会出现"TCP 连上了、驱动却不推数据"，
          8 个数据块之后 read() 就超时（err='等待数据流超时，硬件可能未授权'）。

          保持连接就没有这个问题：'开始'只是重启本地的解码线程，
          完全不碰驱动，因此是瞬时的、也不会再有重连竞态。

        代价：驱动会继续往这个 socket 推数据，我们只是把它丢掉。
              长期不用时应该调用 stop() 真正断开（Kotlin 侧有延时兜底）。
        """
        if self._thread is not None and self._thread.is_alive():
            self._running = False
            self._gen += 1                    # 作废当前采集线程
            self._thread.join(timeout=1.5)
            self._thread = None
        self._paused_conn = (self._src is not None)
        print('LBJ: 已暂停解码（连接保持）', flush=True)
        return True

    def resume(self):
        """从 pause() 的状态恢复解码。返回 False 表示连接已坏，需要重来。"""
        if not self._paused_conn or self._src is None:
            return False
        if self._src._error:
            print('LBJ: 连接已失效（%s），需要重新连接' % self._src._error, flush=True)
            self._paused_conn = False
            return False
        if self._thread is not None and self._thread.is_alive():
            return True                        # 已经在跑了
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print('LBJ: 已恢复解码（连接是原来的，没有重连）', flush=True)
        return True

    def is_connected(self):
        """当前是否还持有一条【可用】的连接。

        判据是"没有彻底断开 + 数据源没报错"，而不是 _paused_conn
        （那个标志只有 pause() 才置位，拿来当连接状态会永远为 False）。
        """
        return bool(not self._stopped and self._opened
                    and self._src is not None and not self._src._error)

    def stop(self):
        self._running = False
        R._g0['running'] = False
        self._paused_conn = False
        self._stopped = True
        self._opened = False
        # 作废当前采集线程：它此后推送的任何状态都不再被接受。
        # 没有这一步的话，被遗弃的旧线程会在 10 秒后把
        # "running=False + 等待数据流超时，硬件可能未授权" 推给【共享的】StateSink，
        # 界面于是显示"已停止"并把【开始】按钮重新点亮 —— 而此时新引擎正在正常接收。
        # 用户会去查 USB 授权，其实完全无关。
        self._gen += 1
        if self._src is not None:
            try:
                # 参考实现的 _A2.read() 是 20×get(0.5s)，且不看 _running，
                # 单纯 close() 打断不了它（要空转满 10 秒）。
                # 它每 0.5s 会检查一次 _error，所以先置错，让它在下一个 tick 立刻抛。
                self._src._error = '已停止'
            except Exception:
                pass
            try:
                self._src.close()
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        return True

    # ---------------------------------------------------- 交给收音机 / 收回来
    def take_source(self):
        """暂停预警解码，并把数据源交出去给收音机接管。

        ★ 只停解码线程，**不动 TCP 连接**。收音机用完还回来即可继续，
          全程不重连驱动 —— 也就不会碰上"驱动连得上却不推数据"那类毛病。
        """
        self.pause()
        src = self._src
        self._src = None
        self._paused_conn = False
        self._opened = False
        return src

    def give_source(self, src):
        """把收音机用完的数据源收回来，继续预警解码。"""
        if src is None:
            return False
        self._src = src
        try:
            self._src._error = None
        except Exception:
            pass
        self._paused_conn = True
        self._opened = True
        # 收音机期间频率/增益都改过了，这里恢复成预警器自己的一套。
        #
        # ★ set_frequency() 收的是【MHz】（它内部自己乘 1e6），
        #   而 R._g2['freq'] 存的是 Hz —— 直接传过去等于把 Hz 当 MHz，
        #   界面上的频率会显示成一长串乱码（真机上出现过），
        #   实际下发给调谐器的频率也被乘爆了。用自己的 freq_mhz 最稳。
        try:
            self.set_frequency(self.freq_mhz)
        except Exception:
            pass
        try:
            self.set_gain(self.gain_db)
        except Exception:
            pass
        try:
            self.set_ppm(self.ppm)
        except Exception:
            pass
        # 前端滤波器和 AFC 里还留着收音机那段数据的尾巴，清掉免得影响判决
        try:
            if self._frontend is not None:
                self._frontend.reset_afc()
        except Exception:
            pass
        try:
            self._err = ''
        except Exception:
            pass
        return self.resume()

    def _loop(self):
        gen = self._gen          # 记住自己是第几代；stop() 会 +1 把我作废
        print('LBJ: DSP 线程启动 gen=%d' % gen, flush=True)
        n = 0
        while self._running and gen == self._gen:
            try:
                iq = self._src.read()
            except Exception as e:
                self._err = str(e)
                # ★ 必须把这个错误同时记到【数据源】上。
                #   resume() 和 is_connected() 判断"这条连接还活着吗"，看的就是
                #   _src._error；只记在引擎级的 _err 上，它们会一直以为连接是好的。
                #   实测症状（用户报的）：拔掉电视棒后点【开始接收】，界面显示
                #   "已恢复接收"，但 DSP 线程 n=0 立刻退出、频谱一直卡住，怎么点
                #   都恢复不了 —— 因为每次都被判成"可以恢复"，永远不去重新拉起驱动。
                if self._src is not None:
                    try:
                        self._src._error = str(e)
                    except Exception:
                        pass
                print('LBJ-ERR read: %s' % e, flush=True)
                break
            if _selftest_running():
                # 自检进行中：直接丢块。这个快速路径放在加锁之前，
                # 免得采集线程在自检的几秒里一直堵在锁上，之后又补处理一批陈旧数据。
                continue
            try:
                with _SELFTEST_LOCK:
                    if _selftest_running():
                        continue
                    self._process_block(iq)
            except Exception:
                import traceback
                self._err = 'DSP 异常: ' + traceback.format_exc().splitlines()[-1]
                print('LBJ-ERR dsp:\n' + traceback.format_exc(), flush=True)
                break
            n += 1
            if n == 1:
                print('LBJ: 已处理第 1 个数据块，rssi=%s' % R._g2.get('rssi'), flush=True)
            elif n % 200 == 0:
                print('LBJ: 已处理 %d 块  rssi=%s gate=%s' % (
                    n, R._g2.get('rssi'), R._g2.get('rssi_gate')), flush=True)
        self._running = False
        print('LBJ: DSP 线程结束 gen=%d n=%d err=%r' % (gen, n, self._err), flush=True)
        # 只有"仍然是当前代"的线程才有资格推送最终状态。
        # 被 stop() 作废的旧线程到这里直接闭嘴。
        if gen == self._gen:
            self._maybe_push(force=True)
        else:
            print('LBJ: 本线程已被 stop() 作废，不再推送状态', flush=True)

    # ------------------------------------------------------- 单块数据处理
    def _process_block(self, iq):
        """一个数据块的完整处理：频谱 → 前端 → 解码。测试时可直接调用。"""
        if len(iq) >= 1024:
            self._spectrum_update(iq)

        pcm, rssi, active = self._frontend.process(iq, rssi_gate=self._gate)
        R._g2['rssi'] = rssi
        R._g2['afc_hz'] = self._frontend.afc.afc_hz
        R._g2['afc_err_hz'] = self._frontend.afc.last_err_hz
        R._g2['afc_score'] = self._frontend.afc.last_score
        R._g2['rssi_gate'] = self._gate.state
        R._g2['rssi_hold_ms'] = self._gate.hold_left_ms

        if self._frontend.consume_afc_updated():
            self._decoder.reset_dpll_soft()

        if active:
            self._decoder.process_chunk(pcm)
        elif self._gate.just_deactivated:
            self._decoder.reset_receiver_state()
            if self._frontend.afc.enabled:
                self._frontend.reset_afc()

        self._maybe_push()

    def _spectrum_update(self, iq):
        n = 1024
        win = np.hanning(n)
        seg = iq[:n] * win
        spec = np.fft.fftshift(np.fft.fft(seg))
        mag = np.abs(spec) / (n * np.mean(win))
        db = 20.0 * np.log10(np.maximum(mag, 1e-12))
        freq_axis = np.fft.fftshift(np.fft.fftfreq(n, d=1.0 / float(R.RTL_SAMPLE_RATE)))
        hw = float(getattr(self._src, 'freq_hz', 0.0))
        tgt = float(R._g2.get('freq', hw))
        off = tgt - hw

        # 只显示目标频率 ±ZOOM_HZ，每格约 9 kHz，足够看清信道
        m = (freq_axis >= off - ZOOM_HZ) & (freq_axis <= off + ZOOM_HZ)
        sub = db[m]
        if sub.size >= SPECTRUM_BINS:
            parts = np.array_split(sub, SPECTRUM_BINS)
            self._spectrum = [round(float(p.max()), 1) for p in parts]
        else:
            self._spectrum = [round(float(v), 1) for v in sub]

        # 目标信号区域内的最强点（对应参考实现的 PK 显示）
        half = max(1000.0, self.bw_khz * 1000.0) * 0.5
        mask = (freq_axis >= off - half) & (freq_axis <= off + half)
        idx = np.where(mask)[0]
        if len(idx):
            k = idx[int(np.argmax(db[idx]))]
            self._peak_hz = hw + float(freq_axis[k])
            self._peak_delta = self._peak_hz - tgt
            self._peak_db = float(db[k])
        else:
            self._peak_hz = self._peak_delta = self._peak_db = None

    # ------------------------------------------------------------- 状态
    def _on_train(self, train, src=None):
        if not train or train == '----':
            return
        now = time.time()
        # src：这条记录的数据来源。默认 _g2（普通路径，父类刚把 _g1 灌进 _g2）；
        # 乘车模式下父类没被调用，_g2 还是上一趟车的，必须传 _g1。
        s = src if src is not None else R._g2
        cat = s.get('category')
        if not cat:
            try:
                cat = R._u5(train, bool(s.get('is_detailed', False)))
            except Exception:
                cat = ''
        rec = {
            'train': train,
            'category': cat,
            'direction': s.get('direction', '未知'),
            'speed': s.get('speed', '---'),
            'position': s.get('position', '---.-'),
            'loco': s.get('loco', '----'),
            'route': s.get('route', '----'),
            'time': time.strftime('%H:%M:%S', time.localtime(now)),
            'ts': now,
            # 乘车模式标记：界面据此 ①不给自己坐的车响提示音 ②在列表里区别显示
            'muted': self.is_ride_train(train),
        }
        # ★ 乘车模式：我就在这趟车上，所以"我的位置"就是本车报的当前公里标。
        #   把它实时写进 my_km，界面上的"本站"就跟着我移动 —— 这样"距离/ETA"
        #   变成"别的车离我多远"，而不是对一个固定车站算（用户确认要这个语义）。
        #   用了 _apply_ride_km 而不是 set_my_km：不能污染用户自己设的那个值。
        if rec['muted']:
            self._apply_ride_km(s.get('position'))

        # 注意：一趟车会进来两次——基础帧一次、扩展帧一次。
        # 经纬度只有扩展帧那次才有，所以采样必须放在去重判断【之前】，
        # 否则会被去重逻辑提前 return 掉，一个样本都采不到。
        self._collect_line_sample(route=s.get('route'), pos=s.get('position'))

        # ★ 按【车次】归并：同一趟车在列表里只占一行，每次收到就更新它并移到最前面。
        #
        # 为什么必须归并而不是逐条追加：
        #   1. 一趟车会解出两次（基础帧 + 扩展帧），整串比较会把它们当成两趟车，
        #      列表里就会出现成对的重复项；
        #   2. 真实 LBJ 是周期性重发的，同一趟车每隔几秒就来一次 ——
        #      逐条追加的话，列表会被同一趟车刷满。
        # "最近列车"的正确语义就是"最近听到过哪几趟车"，所以按车次归并才对。
        self._trains_seen[train] = now
        for idx, old in enumerate(self._trains):
            if not _same_train(old['train'], train):
                continue
            merged = dict(old)
            # 用新到的信息覆盖，但不要用 "未知/----" 这类占位值把已知信息冲掉
            for k, v in rec.items():
                if str(v) in ('', '----', '---', '---.-', '未知'):
                    continue
                merged[k] = v
            merged['time'] = rec['time']
            merged['ts'] = rec['ts']
            self._trains.pop(idx)
            self._trains.insert(0, merged)
            self._maybe_push(force=True)
            return
        self._trains.insert(0, rec)
        # 保留更多历史：界面按屏幕高度显示 10~16 行，多留一些免得刚看到就被挤掉
        del self._trains[40:]
        self._maybe_push(force=True)

    def _collect_line_sample(self, route=None, pos=None):
        """把 (公里标, 经纬度) 记进线路样本表。

        route/pos 由调用方显式传入 —— 乘车模式下 _g2 是上一趟车的，
        不能拿它当这趟车的公里标/线路，否则会往样本表里灌错数据。
        """
        try:
            lon = self.extra.get('lon')
            lat = self.extra.get('lat')
            if route is None:
                route = R._g2.get('route', '----')
            if pos is None:
                pos = R._g2.get('position', '---.-')
            if lon is None or lat is None or not route or route == '----' or pos == '---.-':
                return
            km = float(pos)
            if not (0.0 <= km <= 9999.9):
                return
            with self._lock:
                arr = self._line.setdefault(route, [])
                for s in arr:
                    if abs(s[0] - km) < 0.05 and abs(s[1] - lat) < 5e-4:
                        return                  # 同一位置附近不重复记
                arr.append((km, float(lat), float(lon), time.time()))
                arr.sort(key=lambda s: s[0])
                if len(arr) > 5000:
                    del arr[:1500]
        except Exception as e:
            # 采样是增强功能，失败不该中断接收；但也不能完全无声
            print('LBJ-ERR 线路采样失败: %s' % e, flush=True)

    @staticmethod
    def _dist_m(lat1, lon1, lat2, lon2):
        import math
        rr = 6371000.0
        p1 = math.radians(lat1); p2 = math.radians(lat2)
        dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * rr * math.asin(math.sqrt(max(0.0, min(1.0, a))))

    @staticmethod
    def _dist_to_segment_m(lat, lon, lat1, lon1, lat2, lon2):
        """点到【线段】的最短距离（米）。

        为什么必须用"到线段的距离"而不是"到最近样本点的距离"：
          样本是稀疏的 —— 刚开始可能十几公里才有一个点。站在铁轨正上方的人，
          到最近的【样本点】也可能有 8 公里，于是被误判成"离线太远"。
          实测：第 1 轮样本最大间隔 16.4 km，人就在线路上却报"离线 7.5 公里"。
        用线段距离就没有这个问题：站在线路上，距离就是 0，与样本疏密无关。
        """
        import math
        coslat = math.cos(math.radians((lat1 + lat2) / 2.0))
        kx = 111320.0 * coslat            # 该纬度上 1 度经度约多少米
        ky = 110540.0                     # 1 度纬度约多少米
        px = (lon - lon1) * kx
        py = (lat - lat1) * ky
        dx = (lon2 - lon1) * kx
        dy = (lat2 - lat1) * ky
        l2 = dx * dx + dy * dy
        if l2 <= 0.0:
            return math.hypot(px, py)
        t = max(0.0, min(1.0, (px * dx + py * dy) / l2))
        return math.hypot(px - t * dx, py - t * dy)

    @staticmethod
    def _project_km(lat, lon, a, b):
        """把手机位置投影到 a-b 这一段上，返回该处的公里标"""
        import math
        coslat = math.cos(math.radians((a[1] + b[1]) / 2.0))
        vx = (b[2] - a[2]) * coslat
        vy = b[1] - a[1]
        px = (lon - a[2]) * coslat
        py = lat - a[1]
        l2 = vx * vx + vy * vy
        t = 0.0 if l2 <= 0.0 else max(0.0, min(1.0, (px * vx + py * vy) / l2))
        return a[0] + t * (b[0] - a[0])

    def locate_by_gps_json(self, lat, lon, route=None):
        """用手机 GPS 坐标 + 采集到的线路样本，反推自己所处的公里标。

        做法：把所有样本按公里标排序连成折线（即线路的实际形状），
        找离自己最近的那一段做投影插值，得到自己的公里标。
        门槛判的是【到线路的距离】，所以样本稀疏时也不会误判。
        这与列车接近预警手表里"北斗定位 + 铁路地理信息平台"的效果等价，
        区别是我们不需要预装地图数据库——数据是列车自己报出来的。
        """
        import math
        try:
            lat = float(lat); lon = float(lon)
        except Exception:
            return json.dumps({'ok': False, 'reason': '坐标无效'}, ensure_ascii=False)

        best = None
        # 线路 -> 到这条线折线的最短距离（米）。失败提示要用它，
        # 口径必须和下面的判定一致（都是"到折线"，不是"到最近样本点"）。
        per_line_d = {}
        # 先取一份快照再算。_collect_line_sample() 在 DSP 线程里会对同一个列表
        # 调 sort()，而 CPython 的 list.sort() 会把列表【临时清空】——
        # 直接读会撞上空列表，ds[0] 抛 IndexError，用户看到
        # "定位失败：list index out of range"。收车密集时概率显著上升。
        with self._lock:
            routes = [route] if route else list(self._line.keys())
            snap = {k: list(v) for k, v in self._line.items()}
        for name in routes:
            arr = snap.get(name) or []
            if len(arr) < 2:
                continue
            # 按公里标排序后，相邻两点连成折线 —— 这才是"线路"的实际形状
            arr = sorted(arr, key=lambda s: s[0])
            # 门槛用【到线路的距离】，不是到最近样本点的距离（见 _dist_to_segment_m 的说明）。
            # 这样站在铁轨上的人不论样本多稀疏都能匹配上，只要收够 2 个点即可。
            seg = None
            for i in range(len(arr) - 1):
                a, b = arr[i], arr[i + 1]
                if abs(b[0] - a[0]) < 0.05:
                    continue                       # 公里标几乎相同的两点连不成线段
                d = self._dist_to_segment_m(lat, lon, a[1], a[2], b[1], b[2])
                if seg is None or d < seg[0]:
                    seg = (d, a, b)
            if seg is None:
                continue
            d_line, sa, sb = seg
            per_line_d[name] = d_line
            if d_line > 5000.0:                    # 离这条线超过 5 km，认为不在线上
                continue
            km = self._project_km(lat, lon, sa, sb)
            if best is None or d_line < best[0]:
                best = (d_line, name, round(km, 1))

        if best is None:
            # ★ 必须区分开三种失败原因 —— 用户要做的事完全不同。
            #
            # 之前一律报"还没有足够的线路样本"，于是出现这种情况：
            # 明明已经收了上百个样本，只是人站得离线路远，却提示"样本不够"，
            # 用户就会一直傻等更多列车 —— 而等再多也永远不会成功。
            per_route = {k: len(v) for k, v in self._line.items()}
            enough = {k: v for k, v in per_route.items() if v >= 2}
            if not per_route:
                reason = ('还没收到任何带经纬度的报文；'
                          '经纬度只在【扩展帧】里，有些线路/车型不发扩展帧。')
            elif not enough:
                reason = '每条线路的样本都不到 2 个，再收几趟车就有了。'
            else:
                # ★ 口径必须和判定一致：判定用的是"到线路折线的距离"，
                #   这里也用它。之前这里用的是"到最近【样本点】的距离"，
                #   样本稀疏时会严重偏大 —— 代码注释里写过实测"人就在线路上
                #   却报离线 7.5 公里"，判定早就改用折线了，提示却漏改，
                #   于是用户看到"离最近线路 28.2 公里"以为自己真离线那么远。
                near_d, near_name = None, ''
                for nm, d in per_line_d.items():
                    if near_d is None or d < near_d:
                        near_d, near_name = d, nm
                reason = '离你最近的是【%s】，约 %.1f 公里 —— 超出了判定范围。' % (near_name, (near_d or 0) / 1000.0)
                reason += chr(10) + chr(10)
                reason += ('判定要求手机位置离线路在 5 公里以内：'
                           '本功能靠列车报出的公里标+经纬度来反推你的位置，'
                           '离得太远就无法确定你在这条线的哪一段。')
                reason += chr(10) + chr(10)
                reason += '请走到铁路附近（或站在能看到线路的地方）再试。'
            return json.dumps({
                'ok': False,
                'reason': reason,
                'samples': per_route,
            }, ensure_ascii=False)
        return json.dumps({
            'ok': True,
            'km': best[2],
            'route': best[1],
            'dist_m': round(best[0]),
            'samples': len(self._line.get(best[1], [])),
        }, ensure_ascii=False)

    def snapshot(self):
        g2 = R._g2
        # 直接使用参考实现 _A0 计算好的到达估算，不重复造轮子
        eta = None
        if g2.get('eta_seconds') is not None:
            eta = {
                'seconds': int(g2['eta_seconds']),
                'distance_km': g2.get('eta_distance_km'),
                'time': g2.get('eta_time', '--:--:--'),
                'status': g2.get('eta_status', ''),
                'train': g2.get('eta_train', '----'),
                'route': g2.get('eta_route', '----'),
            }

        # 参考实现的预警文本里带 ANSI 颜色码（本来是给终端用的）。
        # 原样塞进 TextView 会显示成 "←[5m←[41m←[97m ⚠ ..." 这种乱码。
        warn = _ANSI_RE.sub('', str(g2.get('warning', ''))).strip()
        if not warn or (time.time() - float(g2.get('warning_time', 0.0))) >= 2.0:
            warn = ''

        return {
            'running': bool(self._running),
            'error': self._err,
            'tcp_host': self.tcp_host,
            'tcp_port': self.tcp_port,
            'freq_mhz': self.freq_mhz,
            'gain_db': self.gain_db,
            'ppm': self.ppm,
            'bw_khz': self.bw_khz,
            'sample_rate_k': int(R.RTL_SAMPLE_RATE // 1000),
            'cs_threshold': self.cs_threshold,
            'rssi_hold_ms': round(float(self._gate.hold_left_ms), 0) if self._gate else 0,
            'rssi': round(float(g2.get('rssi', -140.0)), 1),
            'gate': g2.get('rssi_gate', 'OFF'),
            'afc_hz': round(float(g2.get('afc_hz', 0.0)), 1),
            'afc_err_hz': round(float(g2.get('afc_err_hz', 0.0)), 1),
            'afc_score': round(float(g2.get('afc_score', 0.0)), 2),
            'warning': warn,
            'is_hit': bool(g2.get('is_hit', False)),
            'keywords': list(self.keywords),
            'ride_trains': list(self.ride_trains),
            'filter_mode': self.filter_mode,
            'strict_filter': self.strict_filter,
            'show_err_warn': self.show_err_warn,
            'current_route': self.current_route(),
            'current_route_km_text': g2.get('current_route_km_text', '---'),
            'known_routes': self.known_routes(),
            'route_km_map': {k: (v if isinstance(v, str) else v) for k, v in self.route_km.items()},
            'eta_status': g2.get('eta_status', '未设置线路位置'),
            'eta_time': g2.get('eta_time', '--:--:--'),
            'eta_seconds': g2.get('eta_seconds'),
            'eta_distance_km': g2.get('eta_distance_km'),
            'approach': g2.get('eta_status') in _APPROACH_STATES,
            'line_samples': {k: len(v) for k, v in self._line.items()},
            'end_pos': self.extra.get('end_pos'),
            'lon': self.extra.get('lon'),
            'lat': self.extra.get('lat'),
            # 收到经纬度字段但范围不合理时的原始值，界面用来显示"存疑"
            'geo_bad': self.extra.get('geo_bad', ''),
            'gain_list': list(self._gain_table()),
            'tuner': self.tuner,
            'peak_hz': self._peak_hz,
            'peak_delta_hz': self._peak_delta,
            'peak_db': self._peak_db,
            'spectrum': self._spectrum,
            'zoom_hz': ZOOM_HZ,
            'train': g2.get('train', '----'),
            'category': g2.get('category', '等待信号...'),
            'direction': g2.get('direction', '未知'),
            'speed': g2.get('speed', '---'),
            'position': g2.get('position', '---.-'),
            'loco': g2.get('loco', '----'),
            'loco_code': g2.get('loco_code', '---'),
            'route': g2.get('route', '----'),
            'is_detailed': bool(g2.get('is_detailed', False)),
            'my_km': self.my_km,
            'route_km': dict(self.route_km),
            'eta': eta,
            # 交给界面自己决定显示几行（16 行表格 + 余量）
            'trains': self._trains[:24],
            # 参考实现里的计数器叫 word_count，没有 words_seen。
            # 之前名字写错，被 getattr 的默认值吞掉，这一格永远是 0。
            'sync_words': int(getattr(self._decoder, 'word_count', 0)) if self._decoder else 0,
        }

    def snapshot_json(self):
        return json.dumps(self.snapshot(), ensure_ascii=False)

    def selftest_json(self):
        return json.dumps(self.selftest(), ensure_ascii=False)

    def _maybe_push(self, force=False):
        now = time.time()
        if not force and (now - self._last_push) < 0.3:
            return
        self._last_push = now
        if self._push is None:
            return
        try:
            payload = self.snapshot_json()
        except Exception:
            import traceback
            self._err = 'snapshot 失败: ' + traceback.format_exc().splitlines()[-1]
            print('LBJ-ERR snapshot:\n' + traceback.format_exc(), flush=True)
            return
        try:
            # Android 侧传进来的是 Kotlin/Java 对象（有 onState 方法），
            # 不是 Python 可调用对象，所以必须显式调用方法名。
            if hasattr(self._push, 'onState'):
                self._push.onState(payload)
            else:
                self._push(payload)
        except Exception:
            import traceback
            self._err = 'push 到 UI 失败: ' + traceback.format_exc().splitlines()[-1]
            print('LBJ-ERR push:\n' + traceback.format_exc(), flush=True)

    # ------------------------------------------------------- 运行时设置
    # 以下每一项都严格对齐参考实现对应按键的处理流程（含必要的状态复位）。

    def _reset_after_retune(self):
        """改频率 / PPM 后必须复位 AFC 与 RSSI 门控，否则会带着旧偏移接收。
        对应参考实现 T/P 按键里的 frontend.reset_afc() 与 rssi_gate.reset()。"""
        if self._frontend is not None:
            try:
                self._frontend.reset_afc()
            except Exception:
                pass
        if self._gate is not None:
            try:
                self._gate.reset()
            except Exception:
                pass
        if self._decoder is not None:
            try:
                self._decoder.reset_receiver_state()
            except Exception:
                pass

    def set_frequency(self, mhz):
        """对应参考实现的 [T] 改频率"""
        self.freq_mhz = float(mhz)
        fc = self.freq_mhz * 1000000.0
        R._g2['freq'] = fc
        if self._src is not None:
            self._src._af(fc)
        self._reset_after_retune()
        return True

    def _gain_table(self):
        """当前调谐器的有效增益档位表"""
        return FC0013_GAINS if self.tuner == 'FC0013' else R.R820T_GAINS

    def set_tuner(self, name):
        """选择调谐器型号（'R820T' 或 'FC0013'），决定增益档位表"""
        n = str(name or '').upper()
        self.tuner = 'FC0013' if n.startswith('FC') else 'R820T'
        # 换表之后重新吸附一次当前增益，免得停留在一个新型号里不存在的档位上
        self.set_gain(self.gain_db)
        return self.tuner

    def tuner_info(self):
        """给界面用的信息：型号、档位数、范围"""
        t = self._gain_table()
        return {'tuner': self.tuner, 'count': len(t),
                'min': min(t), 'max': max(t)}

    def set_gain(self, db):
        """对应参考实现的 [G] 增益：按【当前调谐器】的有效档位吸附，并把实际值回报给界面。

        ★ 故意【不】调用参考实现的 _src._ag()：它写死了按 R820T 的表吸附
          （min(R820T_GAINS, ...)）并把结果下发，对 FC0013 会吸附到
          一个根本不存在的档位 —— 界面于是显示一个假的"实际增益"。
          这里改成：自己按当前调谐器吸附，然后直接下发命令。
          命令常量仍取自参考实现，协议格式与它完全一致。
        """
        self.gain_db = float(db)
        # 无论 _src 在不在，都先按当前调谐器吸附：
        #   _src 为 None 时（applyPrefs 就在这个阶段调用）也必须吸附，
        #   否则 _A2._reader_task 建连后会用 R._g2['gain'] 下发一个无效值。
        self.gain_db = float(min(self._gain_table(), key=lambda x: abs(x - self.gain_db)))
        if self._src is not None:
            try:
                self._src._send_cmd(R.CMD_SET_GAINMODE, 1)
                self._src._send_cmd(R.CMD_SET_GAIN, int(round(self.gain_db * 10)))
            except Exception as e:
                print('LBJ-ERR set_gain 下发失败: %s' % e, flush=True)
        R._g2['gain'] = self.gain_db
        return self.gain_db

    def set_ppm(self, ppm):
        """对应参考实现的 [P] PPM 校正"""
        self.ppm = int(ppm)
        R._g2['ppm'] = self.ppm
        if self._src is not None:
            self._src._ah(self.ppm)
        self._reset_after_retune()
        return True

    def set_threshold(self, db):
        """对应参考实现的 [R] RSSI 门控阈值"""
        self.cs_threshold = float(db)
        R._g2['cs_threshold'] = self.cs_threshold
        if self._gate is not None:
            self._gate._ae(self.cs_threshold)
        return True

    def set_push(self, push):
        """解绑/重绑状态推送目标。

        Activity 销毁时必须调用 set_push(None)：否则 Python 侧会一直持有
        StateSink → 它的 lambda → 已销毁的 Activity，状态推过去只会白发。
        """
        self._push = push
        return True

    def set_host(self, host):
        """设置数据源地址。**只有台架模式才允许指向非本机地址。**

        注意：不再修改 R.TCP_HOST —— 那个模块级全局量会跨引擎实例残留，
        是之前"取消台架模式仍连电脑"的根源之一。
        """
        h = (host or '').strip() or '127.0.0.1'
        changed = (h != self.tcp_host)
        self.tcp_host = h
        # TCP 连接是在 setup() 里建立的，运行中改地址【不会】重连。
        # 这里如实返回"是否需要重启才生效"，让界面不要再谎报"已应用"。
        return bool(changed and self._running)

    def set_hold_ms(self, ms):
        """RSSI 门控释放保持时间（参考实现的 --rssi-hold-ms）"""
        self.rssi_hold_ms = float(ms)
        if self._gate is not None:
            self._gate.hold_ms = float(ms)
        return True

    def set_afc_enabled(self, on):
        self.afc_enabled = bool(on)
        if self._frontend is not None:
            try:
                self._frontend.afc.enabled = bool(on)
            except Exception as e:
                print('LBJ-ERR 设置 AFC 开关失败: %s' % e, flush=True)
            if not on:
                # 关掉 AFC 必须把已经累积的频偏复位：
                # 参考实现 _D6.process 在 enabled=False 时直接 return，
                # 再也不会调用 ddc.set_offset，于是中心频率会永久偏着最多 ±8kHz，
                # 只有重启才恢复；界面还会继续显示一个非零的 AFC 值，与开关自相矛盾。
                try:
                    self._frontend.reset_afc()
                except Exception as e:
                    print('LBJ-ERR reset_afc 失败: %s' % e, flush=True)
        if not on:
            R._g2['afc_hz'] = 0.0
            R._g2['afc_err_hz'] = 0.0
            R._g2['afc_score'] = 0.0
        return True

    def set_my_km(self, km):
        """全局默认本站公里标（0 ~ 9999.9，None = 清除）"""
        if km is None or (isinstance(km, str) and not km.strip()):
            self.my_km = None
        else:
            try:
                v = float(km)
            except (TypeError, ValueError):
                return False
            if not (0.0 <= v <= 9999.9):
                # 必须在【设置时】就拦掉。参考实现 _A0._a8 会校验并拒绝，
                # 但重启后 _A0.__init__ 只做 float() 不校验 —— 越界值会被接受，
                # 于是 ETA 永远显示"ETA过大"，且用户完全不知道是自己上一轮填错了。
                print('LBJ-ERR set_my_km 越界被拒: %r' % (km,), flush=True)
                return False
            self.my_km = v
        # 这是"用户/applyPrefs 的意图"，记下来供退出乘车模式时恢复
        self.user_my_km = self.my_km
        if self._estimator is not None:
            self._estimator._a8(self.my_km)
        return True

    def _apply_ride_km(self, km):
        """乘车模式：把"我的位置"设成本车当前公里标（实时更新）。

        ★ 与 set_my_km 的区别：这里【不】动 user_my_km。
          乘车时我一直在移动，固定车站没有意义 —— "我的位置"就该是本车位置；
          但用户自己设的那个值要留着，退出乘车模式得能恢复。
        """
        try:
            v = float(km)
        except (TypeError, ValueError):
            return False
        if not (0.0 <= v <= 9999.9):
            return False
        self.my_km = v
        if self._estimator is not None:
            self._estimator._a8(v)
        return True

    def set_route_km(self, route, km):
        """对应参考实现的 [K]：按线路设置本站公里标。km 可为 "0123.4KM" 或数字"""
        if not route:
            return False
        if km is None or (isinstance(km, str) and not km.strip()):
            return self.clear_route_km(route)
        self.route_km[str(route)] = km
        if self._estimator is not None:
            self._estimator._a9(str(route), km)
        return True

    def clear_route_km(self, route):
        """对应参考实现的 [K] 留空 = 清除该线路的公里标"""
        if not route:
            return False
        self.route_km.pop(str(route), None)
        if self._estimator is not None:
            self._estimator._aa(str(route))
        return True

    def current_route(self):
        """当前报文的线路名（参考实现的 _A0._a0(_g2['route'])）"""
        try:
            return R._A0._a0(R._g2.get('route'))
        except Exception:
            return ''

    def known_routes(self):
        """从可靠报文中自动提取到的线路列表"""
        try:
            return list(self._estimator.known_routes)
        except Exception:
            return []

    # --------------------------------------------- 扩展帧附加字段
    def _capture_extra(self, addr, bcd):
        """从 50 字符扩展帧里取出端位与经纬度（对应 buf[12:14] / [30:39] / [39:47]）"""
        # 只看长度、不看 addr：合并帧（1233999/1234000，65 字符）也带这些字段，
        # 而它的 [30:39]/[39:47] 与独立扩展帧布局相同。
        if len(bcd) < 50:
            return
        buf = bcd[-50:]
        # 先在【本地】字典上算完，最后一次性原子替换 self.extra。
        # 之前是分三步往 self.extra 里写，而 UI 线程的 clear_dashboard()
        # 会把 self.extra 整体重新绑定到新字典 —— 撞上时这一帧的坐标会写进
        # 已经被丢弃的旧字典里，用户看到经纬度莫名其妙空一格。
        new = dict(self.extra)
        try:
            ep = buf[12:14]
            if ep in _END_NAME:
                new['end_pos'] = _END_NAME[ep]
                new['end_raw'] = ep
        except Exception:
            pass
        # 经纬度：按 README 的约定是 lon=dddffffff(9位) / lat=ddffffff(8位)。
        # 但注意：这个字段在参考实现真正跑起来的代码里从未被使用，格式属于"推断"，
        # 所以必须做范围校验——宁可显示"---"，也不能把解析错的坐标当真给用户看。
        # 经纬度是成对出现的，两个都合理才采用；只对一个也当作不可信整对丢弃。
        vlon = vlat = None
        try:
            a = buf[30:39]
            b = buf[39:47]
            if a.isdigit() and b.isdigit():
                tlon = float(a[:3] + '.' + a[3:])
                tlat = float(b[:2] + '.' + b[2:])
                # 范围取中国铁路的实际包络再留余量：经度 73.5~135.1E(喀什~抚远)，
                # 纬度 18.2~53.6N(三亚~漠河)。格式里没有符号位，南半球的值本来也无法编码，
                # 所以收紧下界能多拦掉一批解析错的垃圾数据。
                if (73.0 <= tlon <= 136.0) and (16.0 <= tlat <= 55.0):
                    vlon, vlat = tlon, tlat
                else:
                    # 记下来供界面提示，避免"明明收到字段却什么都不显示"的困惑
                    new['geo_bad'] = '%.4f,%.4f' % (tlon, tlat)
        except Exception:
            pass
        # 显式赋值（含 None），保证上一帧的旧坐标不会残留
        new['lon'] = vlon
        new['lat'] = vlat
        # 单次原子替换：读方（snapshot / _collect_line_sample）要么看到完整的旧值，
        # 要么看到完整的新值，不会看到写了一半的中间态。
        with self._lock:
            self.extra = new

    # --------------------------------------------- 显示 / 过滤 / 开关
    def set_keywords(self, keywords):
        """对应参考实现的 [F] 关注车次或机车（逗号分隔）"""
        if isinstance(keywords, str):
            # 同时接受全角逗号/顿号/分号：中文输入法下用户很自然会这么打，
            # 而只切半角会把整串当成一个关键词，永远匹配不上。
            items = re.split(r'[,，、;；]', keywords)
        else:
            items = list(keywords or [])
        self.keywords = [k.strip().upper() for k in items if k and k.strip()]
        R._g0['keywords'] = list(self.keywords)
        return True

    def set_filter_mode(self, mode):
        """对应参考实现的 [M] 过滤模式：strict（仅显示命中）/ highlight（高亮）"""
        m = 'strict' if str(mode) == 'strict' else 'highlight'
        self.filter_mode = m
        R._g0['filter_mode'] = m
        if m == 'strict' and not R._g2.get('is_hit', False):
            R._g2.update({
                'train': '----', 'direction': '未知', 'speed': '---',
                'position': '---.-', 'loco': '----', 'loco_code': '---',
                'route': '----', 'category': '等待命中...',
            })
        return True

    def set_strict_filter(self, on):
        """对应参考实现的 [B] 错包拦截：拦截 BCH 无法纠正的报文"""
        self.strict_filter = bool(on)
        R._g0['strict_filter'] = bool(on)
        return True

    def set_err_warn(self, on):
        """对应参考实现的 [W] 干扰预警"""
        self.show_err_warn = bool(on)
        R._g0['show_err_warn'] = bool(on)
        return True

    # --------------------------------------------- 乘车模式（过滤自己的车次）
    def set_ride_trains(self, trains):
        """乘车模式：自己乘坐的车次（逗号分隔；空串 = 关闭）。

        这些车【只】在下面「最近列车」里持续更新，不刷上面的大面板 ——
        否则自己那趟车会周期性重发、把上面刷满，旁边路过的车次反而看不见。
        与 set_keywords 一样接受全角逗号/顿号/分号。
        """
        if isinstance(trains, str):
            items = re.split(r'[,，、;；]', trains)
        else:
            items = list(trains or [])
        self.ride_trains = [_train_key(k) for k in items if k and k.strip()]
        # 退出乘车模式：把"我的位置"还给用户自己设的那个值。
        # 不还的话，它会永远停在最后一次本车报的公里标上，用户还以为设置丢了。
        if not self.ride_trains and self.my_km != self.user_my_km:
            self.my_km = self.user_my_km
            if self._estimator is not None:
                self._estimator._a8(self.my_km)
        # 如果上面正显示着刚被列入"自己坐的车"的那趟，立刻清掉：
        # 乘车模式下它不再更新 _g2，不清的话会一直僵在那儿当"当前列车"。
        if self.is_ride_train(R._g2.get('train', '----')):
            R._g2.update({
                'train': '----', 'direction': '未知', 'speed': '---',
                'position': '---.-', 'loco': '----', 'loco_code': '---',
                'route': '----', 'category': '乘车模式：等待其他车次…',
                'is_hit': False,
                'eta_seconds': None, 'eta_time': '--:--:--',
                'eta_distance_km': None, 'eta_status': '乘车模式',
                'eta_train': '----', 'eta_route': '----',
            })
        self._maybe_push(force=True)
        return True

    def is_ride_train(self, train):
        """这趟车是不是【自己坐的那趟】。

        用与「最近列车」归并完全相同的规则（数字部分相同 + 至少一方是纯数字），
        所以基础帧 '323' 和扩展帧 'K323' 都会被认成同一趟。
        """
        t = _train_key(train)
        if not t or t == '----':
            return False
        for m in self.ride_trains:
            if _same_train(m, t):
                return True
        return False

    def _reset_session_state(self):
        """把参考实现的【模块级】仪表盘全局量复位成初值。

        这些全局量（_g1/_g2）在 App 进程内是永生的 —— Chaquopy 只启动一个解释器，
        sys.modules 又缓存模块对象；而 LbjEngine 每次"开始接收"都会新建一个。
        如果装配时不显式复位，新引擎的 snapshot() 会把【上一场会话】的车次/速度/
        公里标/机车/线路/ETA 原样报给界面，看上去就像附近真有一趟车挂在那儿。

        实测：上一场解出 K1234 / 88 / 0456.7，新引擎 setup 后 snapshot 依旧是这三个值，
        连"接近 / 到达 120s"都一起继承。

        必须在 setup() 的最前面调用 —— 晚了就没用：_A0.__init__ → _a7 只在
        "本站公里标未设置"（km is None）时才复位 eta_*，而正常用户都设了本站公里标。
        """
        R._g2.update({
            'train': '----', 'direction': '未知', 'speed': '---',
            'position': '---.-', 'loco': '----', 'loco_code': '---',
            'route': '----', 'category': '等待信号...', 'warning': '',
            'warning_time': 0, 'eta_seconds': None, 'eta_time': '--:--:--',
            'eta_distance_km': None, 'eta_status': '等待信号',
            'eta_train': '----', 'eta_route': '----',
        })
        R._g1.update({
            'train': '----', 'direction': '未知', 'speed': '---',
            'position': '---.-', 'loco': '----', 'loco_code': '---',
            'route': '----', 'route_valid': False, 'is_detailed': False,
        })
        self._trains = []
        self._trains_seen = {}
        with self._lock:
            self.extra = {}

    def clear_dashboard(self):
        """对应参考实现的 [C] 清屏"""
        self._reset_session_state()
        self._maybe_push(force=True)
        return True

    # ---------------------------------------------------------- 自检
    def _decode_once(self, iq, keywords=None, mode='highlight'):
        """用一份全新引擎跑一遍合成信号，返回 (解出的车次, 统计)"""
        eng = LbjEngine(push=None)
        eng.setup()

        class _FakeSrc:
            freq_hz = eng.freq_mhz * 1000000.0 - R.DEFAULT_DC_OFFSET_HZ

        eng._src = _FakeSrc()
        if keywords is not None:
            eng.set_keywords(keywords)
        eng.set_filter_mode(mode)

        found = []
        eng._on_train = lambda t: found.append(t)

        stats = {'bits': 0, 'sync': 0, 'parse': 0}
        _orig_bit = eng._decoder.process_bit_streaming

        def _wrapped_bit(bit):
            stats['bits'] += 1
            st0 = eng._decoder.state
            _orig_bit(bit)
            if st0 == 0 and eng._decoder.state == 1:
                stats['sync'] += 1

        eng._decoder.process_bit_streaming = _wrapped_bit
        _orig_parse = eng._decoder.trigger_lbj_parse

        def _wrapped_parse():
            stats['parse'] += 1
            _orig_parse()

        eng._decoder.trigger_lbj_parse = _wrapped_parse

        block = R.BLOCK_SIZE
        for i in range(0, len(iq), block):
            eng._process_block(iq[i:i + block])
        return found, stats, eng

    def selftest(self):
        """用合成信号跑一遍完整链路，不接硬件也能验证软件是否正常。

        覆盖三项：
          1. 基础解码（车次/速度/公里标能否解出）
          2. 关注车次过滤（命中关键词应能收到）
          3. 严格模式拦截（不命中关键词应被过滤掉）

        ⚠ 自检必须隔离副作用：
          参考实现的 _g0/_g1/_g2 是【模块级全局】。自检会创建临时引擎，
          临时引擎的 setup()/set_keywords()/set_filter_mode() 会覆盖这些全局量，
          把正在运行的实例一起带偏——典型症状是"接收中点自检，之后界面再也不刷新"
          （因为全局被留成了 keywords=['99999'] + filter_mode='strict'）。
          所以这里先快照全局，结束后原样恢复；同时暂停在线采集，避免读到半污染状态。
        """
        from lbj_synth import synth_lbj_basic, synth_batch

        global _SELFTEST_ACTIVE, _SELFTEST_STARTED_AT, _SELFTEST_WARNED
        saved_g0 = dict(R._g0)
        saved_g1 = dict(R._g1)
        saved_g2 = dict(R._g2)
        # 拿【模块级】锁：整个自检期间，任何在线采集线程都进不了 _process_block。
        # 必须是模块级 —— Kotlin 侧自检时用的是【新建的另一个】LbjEngine 实例，
        # 实例级锁对它俩毫无约束力（这正是"接收中点自检会丢真车"的根因）。
        _SELFTEST_LOCK.acquire()
        try:
            _SELFTEST_ACTIVE = True
            _SELFTEST_STARTED_AT = time.time()
            _SELFTEST_WARNED = False
            iq = synth_lbj_basic(train='12345', speed_kmh=45, km='0123.4',
                                 sample_rate=R.RTL_SAMPLE_RATE, deviation=4500.0,
                                 offset_hz=R.DEFAULT_DC_OFFSET_HZ, noise=0.03, amp=0.5)

            found, stats, eng = self._decode_once(iq)
            hit, _, _ = self._decode_once(iq, keywords='12345', mode='highlight')
            blocked, _, _ = self._decode_once(iq, keywords='99999', mode='strict')

            # 第四趟：扩展帧，覆盖 端位 / 经纬度 / 线路。
            # 之前自检只发基础帧，所以"合并帧把端位经纬度整段丢掉"这类问题
            # 自检根本发现不了 —— 真实报文的合并帧格式就是这样漏掉的。
            # 用【连续流】而不是"单批 + 尾端"：真实 LBJ 是连续发射的。
            # 连发两批，第一批的扩展帧靠第二批的地址码字收尾 —— 这才是真机上的路径；
            # 只发单批的话，末尾消息要靠人工补的前导码才能收尾，测的就不是真实形态了。
            # （最后一批仍需一点点尾端，因为 DSP 要有后续样本才能吐出最后一个码字。）
            _ext_kw = dict(train='12345', speed_kmh=45, km='0123.4',
                           prefix_bytes=b'\x20G', loco_code='310', loco_no='0201',
                           route='京沪线', func=1, end_pos='31',
                           lon='116378600', lat='39865300',
                           sample_rate=R.RTL_SAMPLE_RATE, deviation=4500.0,
                           offset_hz=R.DEFAULT_DC_OFFSET_HZ, noise=0.03, amp=0.5)
            iq_ext = np.concatenate([synth_batch(tail_bits=0, **_ext_kw),
                                     synth_batch(tail_bits=64, **_ext_kw)])
            _, _, eng_ext = self._decode_once(iq_ext)
            ex = dict(eng_ext.extra)

            result = {
                # 门槛必须覆盖它自己声称的三项检查。之前只看 len(found)>0，
                # 于是关键词/严格模式真的坏了也照样报"自检通过 ✅"。
                # 再补上扩展帧字段，否则解码器的附加字段永远不在自检范围内。
                'ok': bool(len(found) > 0 and len(hit) > 0 and len(blocked) == 0
                           and ex.get('end_pos') and ex.get('lon') is not None
                           and ex.get('lat') is not None),
                'end_pos': ex.get('end_pos'),
                'lon': ex.get('lon'),
                'lat': ex.get('lat'),
                'trains': found[:4],
                'train': R._g1.get('train', '----'),
                'speed': R._g1.get('speed', '---'),
                'position': R._g1.get('position', '---.-'),
                'bits': stats['bits'],
                'sync': stats['sync'],
                'parse': stats['parse'],
                'rssi': round(float(R._g2.get('rssi', -140.0)), 1),
                'filter_hit': len(hit) > 0,
                'filter_block': len(blocked) == 0,
            }
        finally:
            # 原样恢复，保证正在运行的实例不受任何影响
            R._g0.clear(); R._g0.update(saved_g0)
            R._g1.clear(); R._g1.update(saved_g1)
            R._g2.clear(); R._g2.update(saved_g2)
            _SELFTEST_ACTIVE = False
            _SELFTEST_LOCK.release()
        return result
