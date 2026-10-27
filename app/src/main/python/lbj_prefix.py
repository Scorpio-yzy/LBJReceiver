# -*- coding: utf-8 -*-
"""车次字头 -> 列车类别/等级判定（含 2026 年车次编排改革）。

为什么要单独一个模块：lbj_ref.py 顶部 import numpy/scipy（DSP 需要），而这条判定是纯字符串
逻辑。单独放这里，既能在桌面 Python 上直接自测（python lbj_prefix.py），也让规则只有一处。

规则来源：2026-10 国铁集团《列车车次编排规定》修订（人民日报/人民铁道公布）。
  · 动车组：X 350km/h 以上复兴号 / G 350 / T 300 / K 200~250 / D 200 及以下动力集中 /
            C 200 及以下城际 / S 160 及以下市域旅客列车
  · 普速：  Z 160 快速 / N 140 快速 / V 120（V1-V5998 快速、V6001-V6998 普快、
            V7001-V7998 普慢）/ Y 普速旅游（Y1-Y998 专线、Y1001-Y9998 专列）
  · 货运：  B = 货物班列（原 X 改 B）；其余仍按数字号段判定（与原规则一致）

实施分两批：2026-11 下旬（普速 T->N、K->V，货物 X->B，Z/Y 部分号段）与
2026-12 下旬（动车组 D->K 等，T/K 改为高速动车组）。
字母在同一时刻只有一种含义，所以 T/K/D/X/Z 在不同批次的语义不同 —— 这里按【第二批生效日】
分"旧规则 / 新规则"两套；N、V、B、S 只在新规则里出现，任何时间都认（过渡期先收到也能显示对）。
"""
from __future__ import annotations

import datetime as _dt

# 官方只给"12 月下旬"，取 2026-12-21 作为保守分界（下旬第一天）。
# 早于它按旧规则（T=特快、K=快速、X=行包、Z=直达特快、D=动车组、G=高速、C=城际）。
PHASE2_START = _dt.datetime(2026, 12, 21, 0, 0, 0)

# 旧规则：改革开放前（以及 2026-11 下旬之前）的字头含义
LEGACY = {
    'G': '高速动车组',
    'D': '动车组列车',
    'C': '城际动车组',
    'T': '特快旅客列车',
    'K': '快速旅客列车',
    'Z': '直达特快列车',
    'Y': '旅游旅客列车',
    'X': '行包货物列车',
}

# 新规则（2026 版）：动车组 7 个 + 普速 Z/N/V/Y + 货运 B
PHASE2 = {
    'X': '350km/h 以上复兴号动车组',
    'G': '350km/h 复兴号动车组',
    'T': '300km/h 动车组',
    'K': '200~250km/h 动车组',
    'D': '200km/h 及以下动力集中动车组',
    'C': '200km/h 及以下城际动车组',
    'S': '160km/h 及以下市域旅客列车',
    'Z': '160km/h 快速旅客列车',
    'N': '140km/h 快速旅客列车',
    'Y': '普速旅游列车',
    'B': '货物班列',
}

# 过渡期也能认的新字头（第一批就生效，与旧规则不冲突）
_ALWAYS = {'N', 'V', 'B', 'S'}

# 货运列车按数字号段（与原规则一致，未受本次改革影响）
_FREIGHT_RANGES = [
    (10001, 19998, '技术直达货运列车'), (20001, 29998, '直通货运列车'),
    (30001, 39998, '区段货运列车'), (40001, 44998, '摘挂列车'),
    (45001, 49998, '小运转列车'), (50001, 50998, '客车单机'),
    (51001, 51998, '货车单机'), (52001, 52998, '小运转单机'),
    (53001, 54998, '补机'), (55001, 55998, '试运行列车'),
    (56001, 56998, '轨道车、小型工程车'), (57001, 57998, '路用列车'),
    (58101, 58998, '救援列车'), (60001, 69998, '工厂自备车'),
    (70001, 70998, '超限货运列车'), (71001, 72998, '万吨货物列车'),
    (73001, 74998, '冷藏列车'), (75001, 75998, '集装箱专列'),
    (80001, 81748, '直达货运/五定班列'), (81751, 81998, '快运货运列车'),
    (82001, 84998, '煤炭直达列车'), (85001, 85998, '石油直达列车'),
    (86001, 86998, '始发直达列车'), (87001, 87998, '空车直达列车'),
    (88001, 88998, '汽运专列'), (90001, 91998, '军用列车(满载)'),
    (93001, 94998, '军用列车(空车)'),
]


def _digits(s):
    return ''.join(ch for ch in s if ch.isdigit())


def _is_phase2(now):
    """是否已进入第二批（高速车次改用新含义）。now 可注入，便于测试。"""
    return (now or _dt.datetime.now()) >= PHASE2_START


def _v_category(n):
    """V 字头按号段细分（120km/h 普速）：V1-V5998 快速、V6001-V6998 普快、V7001-V7998 普慢。"""
    if n is None:
        return PHASE2['V']
    if 1 <= n <= 5998:
        return '快速旅客列车(120km/h)'
    if 6001 <= n <= 6998:
        return '普通旅客快车(120km/h)'
    if 7001 <= n <= 7998:
        return '普通旅客慢车(120km/h)'
    return '普速旅客列车(120km/h)'


def _y_category(n):
    """Y 字头按号段细分：Y1-Y998 旅游专线、Y1001-Y9998 旅游专列。"""
    if n is None:
        return PHASE2['Y']
    if 1 <= n <= 998:
        return '普速旅游专线列车'
    if 1001 <= n <= 9998:
        return '普速旅游专列'
    return PHASE2['Y']


def category_for(train_no, is_detailed=True, now=None):
    """车次 -> 类别的可读文字。规则见模块注释。

    is_detailed：是否已经收到"详细报文（扩展帧）"。基础帧只有数字、无法判字头，
    此时普通的四/五位车次一律返回"等待详细报文确认..."，收到扩展帧后再确定。
    """
    if not train_no:
        return '等待信号...'
    tn = str(train_no).replace(' ', '').strip().upper()
    if tn in ('----', '等待信号...'):
        return '等待信号...'

    head = tn[:1]
    n = None
    ds = _digits(tn)
    if ds:
        try:
            n = int(ds)
        except ValueError:
            n = None

    phase2 = _is_phase2(now)
    table = PHASE2 if phase2 else LEGACY

    if head.isalpha():
        if head == 'V':
            return _v_category(n)
        if head == 'Y':
            return _y_category(n) if phase2 else LEGACY['Y']
        if head in table:
            return table[head]
        if head in _ALWAYS:
            return PHASE2[head]
        # 未知字母（将来又改规则）：给一个不误导的说法，别硬套成"普通旅客列车"
        return '未知字头(%s)列车' % head

    # 00 开头：回送车次（与改革无关）
    if tn.startswith('00') and n is not None:
        if 1 <= n <= 100:
            return '动车组有火回送'
        if 101 <= n <= 198:
            return '动车组无火跨局回送'
        if 201 <= n <= 298:
            return '动车组无火管内回送'
        if 301 <= n <= 398:
            return '跨局回送客车'
        if 401 <= n <= 498:
            return '管内回送客车'

    if n is not None:
        for lo, hi, cat in _FREIGHT_RANGES:
            if lo <= n <= hi:
                return cat
        if 1 <= n <= 9998:
            return '普通旅客列车' if is_detailed else '等待详细报文确认...'
    return '未知类别'


# ---------------------------------------------------------------------------
# 自测：python lbj_prefix.py
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    OLD = _dt.datetime(2026, 10, 1)
    NEW = _dt.datetime(2027, 1, 1)
    cases = [
        # (车次, 详细?, now, 期望)
        ('K123', True, OLD, '快速旅客列车'),
        ('T358', True, OLD, '特快旅客列车'),
        ('Z12', True, OLD, '直达特快列车'),
        ('X105', True, OLD, '行包货物列车'),
        ('N123', True, OLD, '140km/h 快速旅客列车'),      # 过渡期先收到也不误判
        ('V100', True, OLD, '快速旅客列车(120km/h)'),
        ('V6500', True, OLD, '普通旅客快车(120km/h)'),
        ('V7500', True, OLD, '普通旅客慢车(120km/h)'),
        ('B80001', True, OLD, '货物班列'),
        ('S101', True, OLD, '160km/h 及以下市域旅客列车'),
        ('K123', True, NEW, '200~250km/h 动车组'),        # 第二批后语义翻转
        ('T358', True, NEW, '300km/h 动车组'),
        ('X105', True, NEW, '350km/h 以上复兴号动车组'),
        ('D2287', True, NEW, '200km/h 及以下动力集中动车组'),
        ('G1', True, NEW, '350km/h 复兴号动车组'),
        ('Z12', True, NEW, '160km/h 快速旅客列车'),
        ('Y101', True, NEW, '普速旅游专线列车'),
        ('Y1200', True, NEW, '普速旅游专列'),
        ('1234', False, NEW, '等待详细报文确认...'),
        ('1234', True, NEW, '普通旅客列车'),
        ('10501', True, NEW, '技术直达货运列车'),
        ('A12', True, NEW, '未知字头(A)列车'),
    ]
    bad = 0
    for train, detailed, now, want in cases:
        got = category_for(train, detailed, now)
        ok = (got == want)
        bad += 0 if ok else 1
        print('%s %-8s %-10s -> %-28s (期望 %s)' % ('OK ' if ok else 'BAD', train,
              '新规则' if now >= PHASE2_START else '旧规则', got, want))
    print('\n%d/%d 通过' % (len(cases) - bad, len(cases)))
    raise SystemExit(1 if bad else 0)
