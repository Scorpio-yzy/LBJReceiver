# -*- coding: utf-8 -*-
# LBJ Receiver —— 列车接收历史（按"趟"归档）
# Copyright (C) 2026 Scorpio-yzy
# SPDX-License-Identifier: GPL-3.0-or-later
#
# 本文件是 LBJ Receiver 的一部分，以 GPL-3.0-or-later 发布；详见 LICENSE 与 THIRD_PARTY.md。
"""把收到的列车按"趟"归档，一天一个文件，可导出导入。

★ 为什么放在引擎侧（Python）而不是界面侧：
  界面只拿到节流后的快照，而且切后台/锁屏时界面轮询会停 —— 只有引擎一直在解
  报文（前台服务里），所以"当天收过哪些车"这件事必须由引擎记。

一趟的切分规则（用户确认过的口径）：
  · 同一车次，距上一条 < TRIP_GAP_S（30 分钟）→ 继续更新同一条
  · 间隔 ≥ 30 分钟 → 新的一趟
  · 方向翻转（上行↔下行）→ 新的一趟（换端了）。加 60 秒保护：刚开通联不到
    60 秒就翻转，多半是单个字段误读，不切。
  · 公里标跳变 > 200km 且间隔 > 5 分钟 → 新的一趟（同车次重号）
跨零点【不切断】——一条记录归属它首次出现的那一天（23:59 和 00:05 是同一趟）。

存储：root/YYYY-MM-DD.json，内容 {"date": "...", "trips": [...]}
      写入走"临时文件 + os.replace"，被系统杀掉也不会留下半个文件。
"""
import json
import os
import re
import threading
import time

TRIP_GAP_S = 1800.0        # 同一车次超过这么久没消息 = 新的一趟
FLIP_MIN_ALIVE_S = 60.0    # 通联不到这么久就"方向翻转"不认（防单个字段误读）
JUMP_KM = 200.0            # 公里标跳这么多 + 间隔够久 = 同车次重号
JUMP_MIN_GAP_S = 300.0
FLUSH_THROTTLE_S = 5.0     # 落盘节流（最多丢 5 秒内的更新，不会丢整条）
PRUNE_EVERY_S = 3600.0     # 至少一小时检查一次过期清理
DATE_FMT = '%Y-%m-%d'

# 占位值：报文里没解出来时是这些，不能用它们把已知信息冲掉
_PLACEHOLDERS = ('', '----', '---', '---.-', '未知', 'None', 'null', 'nan')

# CSV 列（导出与导入共用一张表，保证能原样来回导）
CSV_COLS = [
    ('日期', 'date'),
    ('车次', 'train'),
    ('类别', 'category'),
    ('方向', 'direction'),
    ('机车', 'loco'),
    ('线路', 'route'),
    ('开始时间', 'first_time'),
    ('结束时间', 'last_time'),
    ('起始公里标', 'start_km'),
    ('结束公里标', 'end_km'),
    ('起始端位', 'end_pos_first'),
    ('结束端位', 'end_pos_last'),
    ('起始经度', 'lon_first'),
    ('起始纬度', 'lat_first'),
    ('结束经度', 'lon_last'),
    ('结束纬度', 'lat_last'),
    ('报文数', 'n_msg'),
    ('最大速度', 'speed_max'),
]


def _valid(v):
    """这个值是不是"真解出来了"（占位值/空值都不算）。"""
    if v is None:
        return False
    s = str(v).strip()
    return s not in _PLACEHOLDERS


def _digits(s):
    """车次里的数字部分：'K323' -> '323'。"""
    return ''.join(ch for ch in str(s or '') if ch.isdigit())


def _same_train(a, a_pure, b, b_pure):
    """两个车次字符串是不是同一趟车。

    ★ 一趟车会分两次被解出：基础帧只有数字（'323'），扩展帧才带字母前缀（'K323'）。
      不归并的话历史里每一趟车都会变成两条（真机实测：184 条里一半是重复的）。
      规则与引擎 _same_train 一致：数字部分相同、且其中至少一个是纯数字形式，
      这样既能合并同一趟的两次，又不会把 G1 和 D1 这种真不同的车并掉。
    """
    a, b = str(a or ''), str(b or '')
    if not a or not b:
        return False
    if a == b:
        return True
    if _digits(a) != _digits(b):
        return False
    return bool(a_pure) or bool(b_pure)


def _num(v):
    try:
        return float(str(v).strip())
    except Exception:
        return None


def _csv_cell(v):
    """CSV 单元格：含逗号/引号/换行就加引号，引号翻倍（Excel 的规矩）。"""
    s = '' if v is None else str(v)
    if any(c in s for c in ',"\r\n'):
        return '"' + s.replace('"', '""') + '"'
    return s


def _csv_split(line):
    """切一行 CSV（支持引号里的逗号与转义引号）。"""
    out, cur, q = [], [], False
    i = 0
    while i < len(line):
        c = line[i]
        if q:
            if c == '"':
                if i + 1 < len(line) and line[i + 1] == '"':
                    cur.append('"')
                    i += 2
                    continue
                q = False
            else:
                cur.append(c)
        else:
            if c == '"':
                q = True
            elif c == ',':
                out.append(''.join(cur))
                cur = []
            else:
                cur.append(c)
        i += 1
    out.append(''.join(cur))
    return out


# ---------------------------------------------------------------------------
# 给界面用的入口。函数名与 LbjEngine 上的同名方法【故意保持一致】，这样界面只需要
# 传"方法名"：引擎在就调引擎（内存里是最新的），引擎没了（停止接收后会被丢弃）
# 就建一个临时 TripLog 直接操作磁盘 —— 历史本来就是磁盘上的东西，任何时候都该能看能导。
# ---------------------------------------------------------------------------
def _want_days(log, scope, recent):
    if scope == 'today':
        return [time.strftime(DATE_FMT)]
    if scope == 'recent':
        n = max(1, int(recent or 7))
        cutoff = time.time() - n * 86400.0
        out = []
        for d in log.days():
            try:
                if time.mktime(time.strptime(d, DATE_FMT)) >= cutoff:
                    out.append(d)
            except Exception:
                pass
        return out
    return None


def history_stats_json(log):
    d = log.stats()
    try:
        d['days'] = len(log.days())
    except Exception:
        d['days'] = 0
    return json.dumps(d, ensure_ascii=False)


def history_days_json(log):
    try:
        out = []
        for d in log.days():
            trips = json.loads(log.day_json(d)).get('trips') or []
            out.append({'date': d, 'n': len(trips)})
        return json.dumps({'days': out}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({'days': [], 'err': str(e)}, ensure_ascii=False)


def history_day_json(log, date=''):
    try:
        return log.day_json(date or '')
    except Exception as e:
        return json.dumps({'date': date, 'trips': [], 'err': str(e)}, ensure_ascii=False)


def history_export(log, fmt='csv', scope='all', recent=0):
    try:
        days = _want_days(log, scope, recent)
        if str(fmt).lower() == 'json':
            return log.export_json(days)
        return log.export_csv(days)
    except Exception as e:
        return json.dumps({'ok': False, 'why': str(e)}, ensure_ascii=False)


def history_import(log, text):
    try:
        r = log.import_text(text or '')
    except Exception as e:
        r = {'ok': False, 'why': str(e), 'added': 0, 'skipped': 0}
    return json.dumps(r, ensure_ascii=False)


def history_clear(log, date=''):
    try:
        n = log.clear(date or '')
    except Exception as e:
        return json.dumps({'ok': False, 'why': str(e), 'removed': 0}, ensure_ascii=False)
    return json.dumps({'ok': True, 'why': '', 'removed': int(n or 0)}, ensure_ascii=False)

# ---- 没有引擎实例时（停止接收后引擎会被丢弃）界面直接调这几个 ----
def history_stats_json_at(root):
    return history_stats_json(TripLog(root))


def history_days_json_at(root):
    return history_days_json(TripLog(root))


def history_day_json_at(root, date=''):
    return history_day_json(TripLog(root), date)


def history_export_at(root, fmt='csv', scope='all', recent=0):
    return history_export(TripLog(root), fmt, scope, recent)


def history_import_at(root, text):
    return history_import(TripLog(root), text)


def history_clear_at(root, date=''):
    return history_clear(TripLog(root), date)

class TripLog:
    """按趟归档的列车接收历史。所有公开方法都线程安全。"""

    def __init__(self, root=None, keep_days=0, gap_s=TRIP_GAP_S):
        self._lock = threading.RLock()
        self._root = ''
        self._gap_s = float(gap_s)
        self._keep_days = int(keep_days or 0)
        self._days = {}          # 'YYYY-MM-DD' -> [trip, ...]
        # 正在更新的那几趟：数字车次 -> [{'date','trip','train','pure'}, ...]
        # （同一数字可能有 G1/D1 这种不同趟，所以一个键下挂一个列表）
        self._open = {}
        self._dirty = set()      # 有改动、待落盘的日期
        self._last_flush = 0.0
        self._last_prune = 0.0
        self._err = ''
        if root:
            self.set_root(root, keep_days)

    # ---------------------------------------------------------------- 配置
    def set_root(self, root, keep_days=None):
        """由 Android 侧传入可写目录（filesDir/history）。返回 True 表示已启用。"""
        with self._lock:
            self.flush(force=True)
            self._root = str(root or '')
            self._days = {}
            self._open = {}
            self._dirty = set()
            if keep_days is not None:
                self._keep_days = int(keep_days or 0)
            if not self._root:
                return False
            try:
                os.makedirs(self._root, exist_ok=True)
            except Exception as e:
                self._err = str(e)
                return False
            self._prune(force=True)
            return True

    def set_keep_days(self, n):
        """保留天数：0 = 永久保留（用户默认）。"""
        with self._lock:
            self._keep_days = int(n or 0)
            self._prune(force=True)
            return self._keep_days

    def enabled(self):
        return bool(self._root)

    def error(self):
        return self._err

    # ---------------------------------------------------------------- 记录
    def on_train(self, rec):
        """收到一条列车报文就调这里。rec 字段见 lbj_engine._on_train 的 rec + 扩展字段。"""
        if not self._root:
            return
        train = str(rec.get('train') or '').strip()
        if not _valid(train):
            return
        try:
            now = float(rec.get('ts') or time.time())
        except Exception:
            now = time.time()
        tkey = str(rec.get('tkey') or _digits(train) or train)
        tpure = bool(rec.get('tpure')) if rec.get('tpure') is not None else train.isdigit()
        with self._lock:
            date = time.strftime(DATE_FMT, time.localtime(now))
            slot = self._open.setdefault(tkey, [])
            cur = None
            for e in slot:
                if _same_train(e.get('train'), e.get('pure'), train, tpure):
                    cur = e
                    break
            start_new = True
            if cur is not None:
                t = cur['trip']
                gap = now - float(t.get('last_ts') or 0.0)
                start_new = self._is_new_trip(t, rec, now, gap)
            if start_new:
                trip = self._new_trip(date, train, rec, now)
                self._load(date).append(trip)
                slot.append({'date': date, 'trip': trip, 'train': train, 'pure': tpure})
                self._dirty.add(date)
            else:
                trip = cur['trip']
                # 车次出现更完整的写法（扩展帧的 K323）就换成它，界面上才好看
                if len(str(train)) >= len(str(cur.get('train') or '')):
                    cur['train'] = train
                    trip['train'] = train
                cur['pure'] = tpure
                self._update(trip, rec, now)
                self._dirty.add(cur['date'])
            self._evict_stale(now)
            self._flush_if_due(now)

    def _is_new_trip(self, t, rec, now, gap):
        """判断这条消息是不是"新的一趟"（三条规则，见文件头）。"""
        if gap is None or gap >= self._gap_s:
            return True
        d_new = rec.get('direction')
        d_old = t.get('direction')
        if _valid(d_new) and _valid(d_old) and str(d_new) != str(d_old):
            # 方向翻转 = 换端。刚连上就翻的多半是误读，不给切。
            if (now - float(t.get('first_ts') or now)) > FLIP_MIN_ALIVE_S:
                return True
        km_new = _num(rec.get('position'))
        km_old = _num(t.get('end_km'))
        if (km_new is not None and km_old is not None
                and gap > JUMP_MIN_GAP_S and abs(km_new - km_old) > JUMP_KM):
            return True
        return False

    def _new_trip(self, date, train, rec, now):
        trip = {
            'date': date,
            'train': train,
            'tkey': _digits(train) or train,
            'category': rec.get('category') if _valid(rec.get('category')) else '',
            'direction': rec.get('direction') if _valid(rec.get('direction')) else '',
            'direction_last': rec.get('direction') if _valid(rec.get('direction')) else '',
            'loco': rec.get('loco') if _valid(rec.get('loco')) else '',
            'loco_last': rec.get('loco') if _valid(rec.get('loco')) else '',
            'loco_code': rec.get('loco_code') if _valid(rec.get('loco_code')) else '',
            'route': rec.get('route') if _valid(rec.get('route')) else '',
            'route_last': rec.get('route') if _valid(rec.get('route')) else '',
            'first_ts': now, 'last_ts': now,
            'first_time': time.strftime('%H:%M:%S', time.localtime(now)),
            'last_time': time.strftime('%H:%M:%S', time.localtime(now)),
            'start_km': rec.get('position') if _valid(rec.get('position')) else '',
            'end_km': rec.get('position') if _valid(rec.get('position')) else '',
            'min_km': _num(rec.get('position')), 'max_km': _num(rec.get('position')),
            'end_pos_first': rec.get('end_pos') if _valid(rec.get('end_pos')) else '',
            'end_pos_last': rec.get('end_pos') if _valid(rec.get('end_pos')) else '',
            'lon_first': None, 'lat_first': None, 'lon_last': None, 'lat_last': None,
            'geo_n': 0,
            'n_msg': 1,
            'speed_first': rec.get('speed') if _valid(rec.get('speed')) else '',
            'speed_last': rec.get('speed') if _valid(rec.get('speed')) else '',
            'speed_max': _num(rec.get('speed')) or 0.0,
        }
        self._geo(trip, rec)
        return trip

    def _update(self, trip, rec, now):
        trip['last_ts'] = now
        trip['last_time'] = time.strftime('%H:%M:%S', time.localtime(now))
        trip['n_msg'] = int(trip.get('n_msg', 1)) + 1
        pairs = (('position', 'end_km'), ('category', 'category'),
                 ('direction', 'direction_last'), ('loco', 'loco_last'),
                 ('loco_code', 'loco_code_last'), ('route', 'route_last'),
                 ('speed', 'speed_last'))
        for src_key, dst in pairs:
            v = rec.get(src_key)
            if _valid(v):
                trip[dst] = v
        # ★ 第一帧往往只是基础帧：车次只有数字，机车/线路/端位都还没解出来。
        #   这些"门面字段"当时是空的，后面扩展帧来了必须补上，否则历史里
        #   一等座以下的车永远没有机车和线路。
        fills = (('category', 'category'), ('direction', 'direction'), ('loco', 'loco'),
                 ('loco_code', 'loco_code'), ('route', 'route'))
        for src_key, dst in fills:
            v = rec.get(src_key)
            if _valid(v) and not _valid(trip.get(dst)):
                trip[dst] = v
        km = _num(trip.get('end_km'))
        if km is not None:
            if trip.get('min_km') is None or km < trip['min_km']:
                trip['min_km'] = km
            if trip.get('max_km') is None or km > trip['max_km']:
                trip['max_km'] = km
        sp = _num(trip.get('speed_last'))
        if sp is not None and sp > float(trip.get('speed_max') or 0.0):
            trip['speed_max'] = sp
        if _valid(rec.get('end_pos')):
            if not trip.get('end_pos_first'):
                trip['end_pos_first'] = rec.get('end_pos')
            trip['end_pos_last'] = rec.get('end_pos')
        self._geo(trip, rec)

    def _geo(self, trip, rec):
        """经纬度只有扩展帧才有；有几个有效值就记几个，不硬塞。"""
        lon, lat = rec.get('lon'), rec.get('lat')
        if lon is None or lat is None:
            return
        trip['geo_n'] = int(trip.get('geo_n', 0)) + 1
        if trip.get('lon_first') is None:
            trip['lon_first'] = lon
            trip['lat_first'] = lat
        trip['lon_last'] = lon
        trip['lat_last'] = lat

    def _evict_stale(self, now):
        """把长时间没消息的车次从"正在更新"表里拿掉（防无限增长）。"""
        for k in list(self._open.keys()):
            keep = [e for e in self._open[k]
                    if now - float(e['trip'].get('last_ts') or 0.0) <= self._gap_s * 4]
            if keep:
                self._open[k] = keep
            else:
                self._open.pop(k, None)

    # ---------------------------------------------------------------- 落盘
    # ★ 只接受严格的 YYYY-MM-DD。以前只在外层查"长度 == 10"，
    #   而 '../../a123' 正好 10 个字符 —— 导入一份手工构造的历史 JSON 就能让
    #   _write_day 往 App 私有目录之外写 .json（路径遍历）。
    _DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

    @classmethod
    def valid_date(cls, date):
        d = str(date or '').strip()
        return bool(cls._DATE_RE.match(d)) and os.path.basename(d) == d

    def _path(self, date):
        d = str(date or '').strip()
        if not TripLog.valid_date(d):
            raise ValueError('非法日期: %r' % (date,))
        return os.path.join(self._root, d + '.json')

    def _load(self, date):
        """取某一天的记录（第一次访问才读文件；读坏了当空，别让历史把引擎带崩）。"""
        if date in self._days:
            return self._days[date]
        trips = []
        p = self._path(date)
        if os.path.isfile(p):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    obj = json.load(f)
                if isinstance(obj, dict):
                    trips = list(obj.get('trips') or [])
                elif isinstance(obj, list):
                    trips = list(obj)
                trips = [t for t in trips if isinstance(t, dict)]
            except Exception as e:
                self._err = '读历史失败 %s: %s' % (date, e)
                trips = []
        self._days[date] = trips
        return trips

    def _write_day(self, date):
        trips = self._days.get(date)
        if trips is None:
            return False
        tmp = self._path(date) + '.tmp'
        try:
            if not trips:
                # 空的一天：留着空文件没意义，直接删掉
                if os.path.isfile(self._path(date)):
                    os.remove(self._path(date))
                return True
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'date': date, 'trips': trips}, f, ensure_ascii=False)
            os.replace(tmp, self._path(date))     # 原子替换：不会留半个文件
            return True
        except Exception as e:
            self._err = '写历史失败 %s: %s' % (date, e)
            return False

    def flush(self, force=False):
        """落盘。节流交给调用方（on_train 里 _flush_if_due）。"""
        with self._lock:
            dates = list(self._days.keys()) if force else list(self._dirty)
            for d in dates:
                if d in self._days:
                    self._write_day(d)
                self._dirty.discard(d)
            self._last_flush = time.time()
            return len(dates)

    def _flush_if_due(self, now):
        if self._dirty and (now - self._last_flush) >= FLUSH_THROTTLE_S:
            self.flush()

    def _prune(self, force=False):
        """按 keep_days 删过期文件（0 = 永久保留，什么都不删）。"""
        if not self._root or self._keep_days <= 0:
            return 0
        now = time.time()
        if not force and (now - self._last_prune) < PRUNE_EVERY_S:
            return 0
        self._last_prune = now
        cutoff = now - self._keep_days * 86400.0
        n = 0
        try:
            for name in os.listdir(self._root):
                if not name.endswith('.json') or len(name) < 15:
                    continue
                date = name[:-5]
                try:
                    ts = time.mktime(time.strptime(date, DATE_FMT))
                except Exception:
                    continue
                if ts < cutoff:
                    try:
                        os.remove(os.path.join(self._root, name))
                        self._days.pop(date, None)
                        n += 1
                    except Exception:
                        pass
        except Exception:
            pass
        return n

    # ---------------------------------------------------------------- 查询
    def days(self):
        """有记录的日期，新的在前（已清空/读坏的那天不算）。"""
        with self._lock:
            out = set(d for d, v in self._days.items() if v)
            if self._root:
                try:
                    for name in os.listdir(self._root):
                        if name.endswith('.json') and len(name) == 15:
                            out.add(name[:-5])
                except Exception:
                    pass
            return sorted(out, reverse=True)

    def day_json(self, date):
        """某一天的记录（JSON 字符串）。date 为空 = 今天。"""
        with self._lock:
            if not date:
                date = time.strftime(DATE_FMT)
            self.flush(force=True)
            trips = list(self._load(date))
        return json.dumps({'date': date, 'trips': trips}, ensure_ascii=False)

    def stats(self):
        """廉价统计（快照每次推送都要调，所以这里【不扫目录】）。"""
        with self._lock:
            today = time.strftime(DATE_FMT)
            try:
                n_today = len(self._load(today))
            except Exception:
                n_today = 0
            return {'enabled': bool(self._root), 'today': n_today,
                    'date': today, 'keep_days': self._keep_days, 'err': self._err}

    # ---------------------------------------------------------------- 导出
    def _collect(self, days=None):
        """把要导出的天收集成 [{'date':..,'trips':[..]}, ...]（新的在前）。"""
        with self._lock:
            self.flush(force=True)
            want = list(days) if days else self.days()
            out = []
            for d in want:
                trips = list(self._load(d))
                if trips:
                    out.append({'date': d, 'trips': trips})
            return out

    def export_json(self, days=None):
        return json.dumps({'days': self._collect(days),
                           'app': 'LBJReceiver', 'kind': 'train-history'},
                          ensure_ascii=False)

    def export_csv(self, days=None):
        """CSV：UTF-8 BOM + CRLF，Excel 直接双击不乱码。"""
        rows = [','.join(h for h, _ in CSV_COLS)]
        for day in self._collect(days):
            for t in day['trips']:
                vals = []
                for _, key in CSV_COLS:
                    v = t.get(key)
                    if key == 'date':
                        v = day['date']
                    vals.append(_csv_cell(v))
                rows.append(','.join(vals))
        return '\ufeff' + '\r\n'.join(rows) + '\r\n'

    # ---------------------------------------------------------------- 导入
    def _add_trip(self, date, trip):
        """插入一条（去重：同一天 + 同车次 + 同开始时间 视为同一条）。"""
        if not isinstance(trip, dict):
            return False
        train = str(trip.get('train') or '').strip()
        if not _valid(train):
            return False
        tkey = str(trip.get('tkey') or _digits(train) or train)
        trip['tkey'] = tkey
        lst = self._load(date)
        ft = str(trip.get('first_time') or '')
        for old in lst:
            # 按数字车次 + 开始时间 去重：基础帧的 '323' 和扩展帧的 'K323' 是同一条
            if str(old.get('tkey') or _digits(old.get('train')) or '') != tkey:
                continue
            if str(old.get('first_time') or '') == ft:
                return False
        trip = dict(trip)
        trip['date'] = date
        trip['train'] = train
        lst.append(trip)
        self._dirty.add(date)
        return True

    def _norm_trip(self, t):
        """导入进来的记录补齐缺省字段，免得界面读到 None 崩。"""
        if not t.get('tkey'):
            t['tkey'] = _digits(t.get('train')) or str(t.get('train') or '')
        for k, d in (('category', ''), ('direction', ''), ('loco', ''), ('route', ''),
                     ('first_time', ''), ('last_time', ''), ('start_km', ''), ('end_km', ''),
                     ('end_pos_first', ''), ('end_pos_last', '')):
            if t.get(k) is None:
                t[k] = d
        for k in ('n_msg', 'geo_n'):
            try:
                t[k] = int(t.get(k) or 0)
            except Exception:
                t[k] = 0
        for k in ('first_ts', 'last_ts'):
            if t.get(k) is None:
                t[k] = 0.0
        return t

    def import_text(self, text):
        """导入 JSON 或 CSV（认得出是哪种）。返回 {'ok':.., 'added':.., 'skipped':..}。"""
        s = (text or '').lstrip('\ufeff \t\r\n')
        if not s.strip():
            return {'ok': False, 'why': '文件是空的', 'added': 0, 'skipped': 0}
        with self._lock:
            added = skipped = 0
            try:
                if s[0] in '{[':
                    obj = json.loads(s)
                    if isinstance(obj, dict) and 'days' in obj:
                        day_list = obj.get('days') or []
                    elif isinstance(obj, dict):
                        day_list = [obj]
                    else:
                        day_list = [{'date': time.strftime(DATE_FMT), 'trips': obj}]
                    for day in day_list:
                        if not isinstance(day, dict):
                            continue
                        date = str(day.get('date') or '').strip()
                        if not TripLog.valid_date(date):
                            continue
                        for t in (day.get('trips') or []):
                            if self._add_trip(date, self._norm_trip(dict(t))):
                                added += 1
                            else:
                                skipped += 1
                elif s.startswith('日期') or ',' in s.split('\n')[0]:
                    added, skipped = self._import_csv(s)
                else:
                    return {'ok': False, 'why': '认不出文件格式（要本 App 导出的 CSV/JSON）',
                            'added': 0, 'skipped': 0}
            except Exception as e:
                return {'ok': False, 'why': '解析失败：%s' % e, 'added': added, 'skipped': skipped}
            self.flush(force=True)
        return {'ok': True, 'why': '', 'added': added, 'skipped': skipped}

    def _import_csv(self, text):
        lines = [l for l in text.replace('\r\n', '\n').replace('\r', '\n').split('\n') if l.strip()]
        if not lines:
            return 0, 0
        head = [h.strip() for h in _csv_split(lines[0])]
        keys = []
        for h in head:
            keys.append(dict(CSV_COLS).get(h))
        added = skipped = 0
        for line in lines[1:]:
            cells = _csv_split(line)
            trip = {}
            for k, cell in zip(keys, cells):
                if not k:
                    continue
                trip[k] = cell.strip()
            date = trip.get('date') or ''
            if not TripLog.valid_date(date):
                continue
            for nk in ('n_msg',):
                try:
                    trip[nk] = int(float(trip.get(nk) or 0))
                except Exception:
                    trip[nk] = 0
            for fk in ('lon_first', 'lat_first', 'lon_last', 'lat_last', 'speed_max'):
                try:
                    trip[fk] = float(trip.get(fk)) if trip.get(fk) not in ('', None) else None
                except Exception:
                    trip[fk] = None
            # CSV 里只有"时分秒"，用日期把它补回时间戳（跨零点的那条按当晚算）
            try:
                ts = time.mktime(time.strptime(date + ' ' + (trip.get('first_time') or '00:00:00'),
                                               '%Y-%m-%d %H:%M:%S'))
            except Exception:
                ts = 0.0
            trip['first_ts'] = ts
            trip['last_ts'] = ts
            trip['geo_n'] = 1 if trip.get('lon_first') is not None else 0
            if self._add_trip(date, self._norm_trip(trip)):
                added += 1
            else:
                skipped += 1
        return added, skipped

    # ---------------------------------------------------------------- 清理
    def clear(self, date=None):
        """清空某天（date 为空 = 今天）；date == '*' 清空全部。"""
        with self._lock:
            if date == '*':
                n = 0
                for d in self.days():
                    n += len(self._load(d))
                    try:
                        os.remove(self._path(d))
                    except Exception:
                        pass
                self._days = {}
                self._open = {}
                self._dirty = set()
                return n
            if not date:
                date = time.strftime(DATE_FMT)
            n = len(self._load(date))
            # 注意：必须把这一天【从内存里删掉】，不能留个空列表 ——
            # 留空列表的话 days() 还会报告这一天有记录（真机踩过）。
            self._days.pop(date, None)
            self._dirty.discard(date)
            for k in list(self._open.keys()):
                keep = [e for e in self._open[k] if e['date'] != date]
                if keep:
                    self._open[k] = keep
                else:
                    self._open.pop(k, None)
            try:
                os.remove(self._path(date))
            except Exception:
                pass
            return n
