# -*- coding: utf-8 -*-
"""合成 POCSAG / LBJ 测试信号（编码规则逆向自 lbj_ref.py 的解码逻辑）"""
import numpy as np

BCD = "0123456789*U -)("
GEN = 0x769


def _rev4(v):
    return ((v & 1) << 3) | ((v & 2) << 1) | ((v & 4) >> 1) | ((v & 8) >> 3)


def _bch_rem(d31):
    d = d31
    for i in range(30, 9, -1):
        if (d >> i) & 1:
            d ^= GEN << (i - 10)
    return d & 0x3FF


def _make_cw(info21):
    d31 = (info21 << 10) | _bch_rem(info21 << 10)
    par = bin(d31).count('1') & 1
    return ((d31 << 1) | par) & 0xFFFFFFFF


def addr_cw(ric, func):
    addr = ric >> 3
    info = (0 << 20) | (addr << 2) | (func & 3)
    return _make_cw(info)


def msg_cw(chars):
    data20 = 0
    for i, ch in enumerate(chars):
        data20 |= _rev4(BCD.index(ch)) << (16 - i * 4)
    return _make_cw((1 << 20) | data20)


SYNC = 0x7CD215D8
IDLE = 0x7A89C197


def bits_of(value, n):
    return [(value >> (n - 1 - i)) & 1 for i in range(n)]


def build_basic_message(train, speed_kmh, km, ric=1234000, func=1):
    """
    基础报文 15 字符： [0:6]车次 [6:9]速度 [9]分隔 [10:15]公里标(末位是小数)
    """
    t = (train + '      ')[:6]
    s = '%03d' % int(speed_kmh)
    km_s = str(km).replace('.', '')
    km_s = ('00000' + km_s)[-5:]
    bcd = t + s + ' ' + km_s
    assert len(bcd) == 15, bcd

    bits = []
    for _ in range(18):                       # 576 bit 前导码
        bits += [1, 0] * 16
    bits = []
    pre = [1, 0] * 288                        # 576 bit
    bits += pre
    bits += bits_of(SYNC, 32)

    frame = ric & 7
    cws = [None] * 16
    cws[frame * 2] = addr_cw(ric, func)
    for i in range(3):
        cws[frame * 2 + 1 + i] = msg_cw(bcd[i * 5:(i + 1) * 5])
    for i in range(16):
        if cws[i] is None:
            cws[i] = IDLE
    for cw in cws:
        bits += bits_of(cw, 32)
    return bits, bcd


def synth_lbj_basic(train='K123', speed_kmh=45, km='0123.4', sample_rate=960000.0,
                    deviation=4500.0, offset_hz=50000.0, noise=0.03, amp=0.5,
                    repeat=2, seed=1234):
    rng = np.random.default_rng(seed)
    spb = int(round(sample_rate / 1200.0))
    chunks = []
    for r in range(repeat):
        bits, _ = build_basic_message(train, speed_kmh, km)
        # POCSAG: 逻辑 0 → +dev, 逻辑 1 → -dev
        f = np.where(np.array(bits) == 0, +deviation, -deviation)
        f = np.repeat(f, spb).astype(np.float64)
        f += offset_hz
        ph = np.cumsum(2.0 * np.pi * f / sample_rate)
        sig = amp * np.exp(1j * ph)
        chunks.append(sig)
    sig = np.concatenate(chunks)
    if noise > 0:
        sig = sig + (rng.standard_normal(len(sig)) +
                     1j * rng.standard_normal(len(sig))) * (noise / np.sqrt(2))
    return sig.astype(np.complex64)


# ---------------------------------------------------------------------------
#  扩展帧（RIC 1234002）编码 —— 与 lbj_ref.decode_lbj 的解码规则互逆
# ---------------------------------------------------------------------------
_NIB = "0123456789*U -)("


def _nib(v):
    return _NIB[v & 0xF]


def _bytes_to_bcd(bs):
    """每个字节 -> 2 个 BCD 字符（高半字节在前）"""
    out = []
    for b in bs:
        out.append(_nib((b >> 4) & 0xF))
        out.append(_nib(b & 0xF))
    return ''.join(out)


def build_extended_buf(prefix_bytes, loco_code, loco_no, route,
                       end_pos='30', lon='116378600', lat='39865300'):
    """
    构造 50 字符的扩展帧（对应 lbj_ref 里的 buf = bcd[-50:]）
      [0:4]   2 个 ASCII 字符（车次前缀，如 b'\x20K' -> 'K'）
      [4:12]  机车类型码(3) + 机车号(5)
      [12:14] 端位（解码器未直接使用，保留）
      [14:30] 线路名 8 字节 GBK
      [30:39] 经度（未解码）
      [39:47] 纬度（未解码）
      [47:50] 保留
    """
    p = (bytes(prefix_bytes) + b'\x00\x00')[:2]
    s = _bytes_to_bcd(p)                                   # 4
    s += (loco_code + '     ')[:3]                         # 3
    s += (loco_no + '     ')[:5]                           # 5
    s += (str(end_pos) + '00')[:2]                         # 2  端位 30/31/32
    rb = (route.encode('gbk') + b'\x00' * 8)[:8]
    s += _bytes_to_bcd(rb)                                 # 16 线路名
    s += (str(lon) + '000000000')[:9]                      # 9  经度 dddffffff
    s += (str(lat) + '00000000')[:8]                       # 8  纬度 ddffffff
    s += '000'                                             # 3
    assert len(s) == 50, (len(s), s)
    return s


def build_batch(train, speed_kmh, km, prefix_bytes=b'\x00\x00',
                loco_code='000', loco_no='00000', route='',
                ric_basic=1234000, ric_ext=1234002, func=1,
                end_pos='30', lon='116378600', lat='39865300'):
    """
    一个 POCSAG 批次（sync + 16 码字）同时装下基础帧和扩展帧：
      cw[0]     基础帧地址 (RIC 1234000, frame 0)
      cw[1..3]  基础帧 15 字符
      cw[4]     扩展帧地址 (RIC 1234002, frame 2)
      cw[5..14] 扩展帧 50 字符
      cw[15]    IDLE
    """
    t = (train + '      ')[:6]
    sp = '%03d' % int(speed_kmh)
    km_s = ('00000' + str(km).replace('.', ''))[-5:]
    basic = t + sp + ' ' + km_s
    assert len(basic) == 15, basic

    ext = build_extended_buf(prefix_bytes, loco_code, loco_no, route,
                             end_pos=end_pos, lon=lon, lat=lat)

    cws = [IDLE] * 16
    cws[0] = addr_cw(ric_basic, func)
    for i in range(3):
        cws[1 + i] = msg_cw(basic[i * 5:(i + 1) * 5])
    cws[4] = addr_cw(ric_ext, func)
    for i in range(10):
        cws[5 + i] = msg_cw(ext[i * 5:(i + 1) * 5])

    bits = [1, 0] * 288 + bits_of(SYNC, 32)
    for cw in cws:
        bits += bits_of(cw, 32)
    return bits


def synth_batch(train='20001', speed_kmh=45, km='0130.0', prefix_bytes=b'\x00\x00',
                loco_code='000', loco_no='00000', route='', func=1,
                sample_rate=960000.0, deviation=4500.0, offset_hz=50000.0,
                noise=0.02, amp=0.5, seed=None, tail_bits=320,
                end_pos='30', lon='116378600', lat='39865300'):
    """
    tail_bits: 批次结束后再补一段 1010 前导码。

    ★ 它补偿的到底是什么（已实测确认，见 tools/test_continuous.py）：
      DSP 需要【后续样本】才能把最后一个码字的 32 bit 吐出来。信号若正好
      停在最后一个码字上，末尾那条消息就不会被收尾 —— 这不是解码器的 bug，
      而是任何流式解调器的固有性质。

      真实 LBJ 在机车发射期间是【连续】的，永远有后续信号，所以这个性质
      在真机上不会咬人：实测把 5 个批次首尾相接（完全不加任何人工尾端），
      车次/线路/端位/经纬度全部正常解出。

    ★ 它【不可能】造成假解码：这段是纯 1010 前导码，不含同步字，
      解码器只会把它当成帧间填充，不会凭它解出任何消息。

    ★ 它也【不会】影响真实接收：lbj_synth 只被 lbj_engine.selftest() 引用，
      真实链路（RTL-SDR → rtl_tcp → lbj_ref）一行都不经过本文件。

      真正要小心的反而是相反方向的事：有限长度的测试信号总有个"最后一条消息"，
      所以宁可用连续流（多批首尾相接）来测，也不要只靠这段尾端。
    """
    rng = np.random.default_rng(seed)
    bits = build_batch(train, speed_kmh, km, prefix_bytes, loco_code, loco_no, route,
                       func=func, end_pos=end_pos, lon=lon, lat=lat)
    if tail_bits > 0:
        bits = bits + [1, 0] * (tail_bits // 2)
    spb = int(round(sample_rate / 1200.0))
    f = np.where(np.array(bits) == 0, +deviation, -deviation)
    f = np.repeat(f, spb).astype(np.float64) + offset_hz
    ph = np.cumsum(2.0 * np.pi * f / sample_rate)
    sig = amp * np.exp(1j * ph)
    if noise > 0:
        sig = sig + (rng.standard_normal(len(sig)) +
                     1j * rng.standard_normal(len(sig))) * (noise / np.sqrt(2))
    return sig.astype(np.complex64)

