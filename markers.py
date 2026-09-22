"""markers.py —— 不合格片段标记与定位合格率统计（纯逻辑，不依赖 Qt）

文件名约定（Genrobot 采集）：

    DAS-Ego_20260911203440_none_none_689985_65416a8a
             └────┬───┘└──┬─┘ └──┬─┘ └──┬───┘ └───┬──┘
               8 位日期   6 位时间  设备号   随机尾号

    * 20260911  = 拍摄日期（YYYYMMDD）
    * 203440    = 当天开始拍摄时间（HHMMSS）
    * 689985    = 设备号

标注口径（写进报告，避免歧义）：
    * 标注员在播放时按两下 X 把一段标为「不合格片段」（第一次按下=起点，
      第二次按下=终点；两段重叠时会合并）；
    * 不合格时长 = 所有不合格片段的并集时长（截断在 [0, 视频时长] 内）；
    * 合格时长 = 视频总时长 − 不合格时长；
    * 合格率 = 合格时长 / 视频总时长（没有时长记录时记 0）。
"""

import os
import re
import sys
import time
import datetime

#: 采集文件名：日期 + 时间 + 设备号（6 位十六进制，可含字母，如 9fb723）+ 随机尾号
#: 依据《元数据填写指南（仅 Genrobot）》第一节：
#:   DAS-Ego_20260815082408_none_none_9fb723_3839df9d.mcap
#:            └─ 日期 ─┘└ 时间 ┘        └设备号┘ └随机尾号┘
#: 设备号是十六进制（可能是 9fb723 / 3b6fb9 这种带字母的），不是纯数字，
#: 所以这里用 [0-9a-fA-F]{4,8} 而不是 \d+；尾号长度不定（6~10 位）。
NAME_RE = re.compile(
    r'^(?P<prefix>[^_]*)_(?P<date>\d{8})(?P<time>\d{6})_'
    r'(?P<mid1>[^_]*)_(?P<mid2>[^_]*)_(?P<device>[0-9a-fA-F]{4,8})_'
    r'(?P<tail>[0-9a-fA-F]+)$')

REPORT_PREFIX = '定位合格率报告'


def parse_name(name):
    """解析采集文件名；不符合约定的名字返回 None（绝不猜）。"""
    stem = os.path.splitext(os.path.basename(name or ''))[0]
    m = NAME_RE.match(stem)
    if not m:
        return None
    date, tm, device = m.group('date'), m.group('time'), m.group('device')
    try:
        d = datetime.date(int(date[:4]), int(date[4:6]), int(date[6:8]))
    except ValueError:
        return None
    return dict(stem=stem, date=date, time=tm, device=device,
                date_text=d.strftime('%Y-%m-%d'),
                time_text='%s:%s:%s' % (tm[:2], tm[2:4], tm[4:6]))


def folder_device_info(names):
    """文件夹内所有文件名 → 设备号 / 采集日期（跨天则标记为多日期）"""
    infos = [parse_name(n) for n in names]
    infos = [i for i in infos if i]
    devices = sorted({i['device'] for i in infos})
    dates = sorted({i['date'] for i in infos})
    return dict(
        devices=devices,
        dates=dates,                      # 原始 YYYYMMDD 列表（供元数据表写法转换）
        device_text=devices[0] if len(devices) == 1 else (
            '、'.join(devices) if devices else '未知'),
        date_text=(str(datetime.date(int(dates[0][:4]), int(dates[0][4:6]),
                                    int(dates[0][6:8])))
                   if len(dates) == 1 else
                   ('、'.join(dates) if dates else '未知')),
        multiple_devices=len(devices) > 1,
        multiple_dates=len(dates) > 1)


def normalize_segments(segments, duration=None):
    """区间规范化：截断到 [0, duration]、排序、合并重叠、丢弃空段。

    返回 [(start, end), ...]（秒，浮点）。
    """
    clean = []
    for seg in segments or []:
        try:
            a, b = float(seg[0]), float(seg[1])
        except (TypeError, ValueError, IndexError):
            continue
        if b < a:
            a, b = b, a
        if duration and duration > 0:
            a = max(0.0, min(a, duration))
            b = max(0.0, min(b, duration))
        if b - a <= 1e-6:
            continue
        clean.append((a, b))
    clean.sort()
    merged = []
    for a, b in clean:
        if merged and a <= merged[-1][1] + 1e-6:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def add_point(segments, pending, t, duration=None):
    """按 X 的状态机：第一下记起点，第二下闭合成一段。

    返回 (segments, pending, closed)：
      * closed 为本次新生成的区间 (start, end) 或 None；
      * 两次按在同一位置（长度≈0）时丢弃并提示。
    """
    segs = list(segments or [])
    if duration and duration > 0:
        t = max(0.0, min(float(t), duration))
    else:
        t = max(0.0, float(t))
    if pending is None:
        return segs, t, None
    a, b = float(pending), t
    if b < a:
        a, b = b, a
    if b - a <= 1e-3:
        return segs, None, None          # 长度过短：丢弃
    segs.append([a, b])
    return normalize_segments(segs, duration), None, (a, b)


def remove_segment(segments, index):
    """删除第 index 段（按规范化后的顺序），返回新列表。越界原样返回。"""
    norm = normalize_segments(segments)
    if not (0 <= index < len(norm)):
        return norm
    norm.pop(index)
    return norm


def bad_total(segments, duration=None):
    """不合格总时长（规范化为并集之后）"""
    return sum(b - a for a, b in normalize_segments(segments, duration))


def fmt_hms(seconds):
    """秒 → H:MM:SS.mmm（报告里给人看）"""
    seconds = max(0.0, float(seconds or 0.0))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - h * 3600 - m * 60
    return '%d:%02d:%06.3f' % (h, m, s)


MONTHS_ABBR = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
               'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')

#: 开始/结束时间的取整粒度（《元数据填写指南》五：10 分钟）
ROUND_STEP_S = 600


def time_of_day_s(info):
    """文件名里的 HHMMSS → 当天秒数；非法返回 None"""
    tm = (info or {}).get('time') or ''
    if len(tm) != 6 or not tm.isdigit():
        return None
    h, m, s = int(tm[:2]), int(tm[2:4]), int(tm[4:])
    if h > 23 or m > 59 or s > 59:
        return None
    return h * 3600 + m * 60 + s


def fmt_clock(sec):
    """秒 → HH:MM:SS（时刻格式，用于展示原始开始时刻）"""
    sec = int(max(0, sec))
    return '%02d:%02d:%02d' % (sec // 3600, (sec % 3600) // 60, sec % 60)


def fmt_hhmm(sec):
    """秒 → H:MM（元数据表里的写法，如 8:20 / 9:10）"""
    sec = int(max(0, sec))
    return '%d:%02d' % (sec // 3600, (sec % 3600) // 60)


def floor_to_step(sec, step=ROUND_STEP_S):
    """向前取整（向下）：08:24:08 → 08:20"""
    return int(sec // step) * step


def ceil_to_step(sec, step=ROUND_STEP_S):
    """向后取整（向上）：09:09:50 → 09:10；正好整 10 分钟时保持不变"""
    sec = int(sec)
    if sec % step == 0:
        return sec
    return (sec // step + 1) * step


def date_meta_text(date8):
    """20260815 → 15-Aug-24（元数据表里的写法）"""
    if not date8 or len(str(date8)) != 8:
        return '—'
    y, m, d = str(date8)[:4], int(str(date8)[4:6]), int(str(date8)[6:8])
    if not 1 <= m <= 12:
        return '—'
    return '%02d-%s-%s' % (d, MONTHS_ABBR[m - 1], y[2:])


def day_window(rows):
    """当天拍摄窗口（《元数据填写指南》三 / 四 / 五节口径）：

      * 当地开始时间 = 最早一段的**开始时刻**，向前取整到 10 分钟；
      * 当地结束时间 = 最晚一段的**开始时刻 + 该段时长**，向后取整到 10 分钟
        （时长来自程序记录的视频时长；末段缺时长时按可用时长估算并注明）。

    行数据来自 summarize() 的 rows（含 name/时长/start 时刻信息）。
    """
    pts = []
    for r in rows or []:
        t = time_of_day_s(r.get('info'))
        if t is None:
            continue
        pts.append((t, float(r.get('duration') or 0.0), r.get('name', '')))
    if not pts:
        return dict(ok=False,
                    note='文件名不符合采集命名约定，无法推算拍摄窗口')
    pts.sort()
    first_t, _first_d, _n = pts[0]
    last_t, last_d, last_name = pts[-1]
    notes = []
    if last_d > 0:
        last_end = last_t + last_d
    else:
        cand = [t + d for t, d, _ in pts if d > 0]
        if cand:
            last_end = max(cand)
            notes.append('末段视频时长缺失，结束时间按可用时长估算')
        else:
            last_end = None
            notes.append('缺少视频时长，无法推算结束时间')
    start_r = floor_to_step(first_t)
    end_r = ceil_to_step(last_end) if last_end is not None else None
    if last_end is not None and last_end - first_t > 20 * 3600:
        notes.append('时间跨度超过 20 小时，疑似跨天，请人工核对')
    return dict(
        ok=True,
        first_start=first_t, last_start=last_t, last_duration=last_d,
        last_end=last_end, last_name=last_name,
        start_rounded=start_r, end_rounded=end_r,
        start_text=fmt_hhmm(start_r),
        end_text=fmt_hhmm(end_r) if end_r is not None else '—',
        first_clock=fmt_clock(first_t),
        last_clock=fmt_clock(last_t),
        notes=notes,
    )


def summarize(items, folder=''):
    """文件夹汇总。items: [{'name','path','duration_s','segments'}]"""
    rows = []
    total = bad = 0.0
    for it in items:
        dur = float(it.get('duration_s') or 0.0)
        segs = normalize_segments(it.get('segments'), dur)
        bad_s = sum(b - a for a, b in segs)
        bad_s = min(bad_s, dur) if dur > 0 else bad_s
        good = max(0.0, dur - bad_s)
        rows.append(dict(
            name=it.get('name', ''), path=it.get('path', ''),
            duration=dur, bad=bad_s, good=good,
            segments=segs,
            rate=(good / dur * 100.0) if dur > 0 else 0.0,
            info=parse_name(it.get('name', ''))))
    total = sum(r['duration'] for r in rows)
    bad = sum(r['bad'] for r in rows)
    good = max(0.0, total - bad)
    meta = folder_device_info([r['name'] for r in rows])
    window = day_window(rows)
    return dict(folder=folder, rows=rows, total=total, bad=bad, good=good,
                rate=(good / total * 100.0) if total > 0 else 0.0,
                count=len(rows), meta=meta, window=window,
                marked_count=sum(1 for r in rows if r['segments']))


def report_text(summary, generated=None):
    """生成报告正文（txt）"""
    generated = generated or datetime.datetime.now()
    meta = summary['meta']
    win = summary.get('window') or {}
    lines = []
    lines.append('MCAP 定位合格率报告')
    lines.append('=' * 72)
    lines.append('【元数据（可直接填入元数据表）】')
    dates = meta.get('dates') or []
    meta_date = date_meta_text(dates[0]) if len(dates) == 1 else '多日期'
    lines.append('设备号      ：%s' % meta['device_text'])
    lines.append('采集日期    ：%s（表内写法：%s）' % (meta['date_text'], meta_date))
    if win.get('ok'):
        start_note = '最早一段 %s 向前取整到 10 分钟' % win['first_clock']
        if win.get('end_rounded') is not None:
            d = win.get('last_duration') or 0.0
            end_note = ('最晚一段 %s + 时长 %s = %s，向后取整到 10 分钟'
                        % (win['last_clock'], fmt_hms(d),
                           fmt_clock(win['last_end'])))
        else:
            end_note = '缺少时长，无法推算'
        lines.append('当地开始时间：%s（%s）' % (win['start_text'], start_note))
        lines.append('当地结束时间：%s（%s）' % (win['end_text'], end_note))
        if win.get('notes'):
            lines.append('            注：%s' % '；'.join(win['notes']))
    else:
        lines.append('当地开始时间：—（%s）' % win.get('note', '无法推算'))
        lines.append('当地结束时间：—')
    lines.append('- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -')
    lines.append('文件夹    ：%s' % (summary.get('folder') or '—'))
    lines.append('视频数量  ：%d' % summary['count'])
    lines.append('生成时间  ：%s' % generated.strftime('%Y-%m-%d %H:%M:%S'))
    lines.append('')
    lines.append('定位合格率汇总')
    lines.append('-' * 72)
    lines.append('总定位时长 ：%s（%.3f 秒）' % (fmt_hms(summary['total']),
                                              summary['total']))
    lines.append('不合格时长 ：%s（%.3f 秒）' % (fmt_hms(summary['bad']),
                                              summary['bad']))
    lines.append('合格时长   ：%s（%.3f 秒）' % (fmt_hms(summary['good']),
                                              summary['good']))
    lines.append('合格率     ：%.2f%%' % summary['rate'])
    lines.append('')
    lines.append('明细（有不合格片段的视频才列出片段）')
    lines.append('-' * 72)
    for r in summary['rows']:
        info = r.get('info') or {}
        name = r['name']
        lines.append('%s' % name)
        lines.append('    拍摄时间：%s  设备号：%s  时长：%s' % (
            info.get('time_text', '—'), info.get('device', '—'),
            fmt_hms(r['duration'])))
        lines.append('    不合格：%s   合格率：%.2f%%' % (
            fmt_hms(r['bad']), r['rate']))
        for a, b in r['segments']:
            lines.append('      - %s ~ %s（%.3f 秒）' % (
                fmt_hms(a), fmt_hms(b), b - a))
    lines.append('')
    lines.append('说明')
    lines.append('-' * 72)
    lines.append('1. 不合格片段由标注员在播放时按两下 X 手工标注（第一下=起点，第二下=终点）；')
    lines.append('2. 不合格时长是所有标注片段的并集（重叠部分只计一次），')
    lines.append('   已截断在单个视频时长范围内；')
    lines.append('3. 合格时长 = 总定位时长 − 不合格时长；合格率 = 合格时长 ÷ 总定位时长；')
    lines.append('4. 未标注的部分一律按合格计；本报告仅统计当前文件夹内的视频；')
    lines.append('5. 设备号与采集日期取自文件名（DAS-Ego_日期时间_none_none_设备号_随机尾号）；')
    lines.append('   当地开始时间 = 最早一段的开始时刻向前取整到 10 分钟；')
    lines.append('   当地结束时间 = 最晚一段的开始时刻 + 该段时长，向后取整到 10 分钟。')
    lines.append('')
    return '\n'.join(lines)


def report_path(folder, summary):
    """报告文件路径（旧版回退位置）：写在视频文件夹里，文件名带设备号与日期"""
    meta = summary['meta']
    dev = meta['device_text'] if not meta['multiple_devices'] else '多设备'
    date = meta['date_text'] if not meta['multiple_dates'] else '多日期'
    name = '%s_设备%s_%s.txt' % (REPORT_PREFIX, dev, date)
    return os.path.join(folder, name)


def report_file_name(summary):
    """报告文件名（P1.6E 起的正式命名）：设备名 + 采集日期。

    例：``设备689985_20260911.txt``（日期取文件名里的 8 位 YYYYMMDD）。
    """
    meta = summary.get('meta') or {}
    devices = meta.get('devices') or []
    dates = meta.get('dates') or []
    dev = ('设备' + devices[0]) if len(devices) == 1 else '设备多台'
    date = dates[0] if len(dates) == 1 else '多日期'
    return '%s_%s.txt' % (dev, date)


def report_dir():
    """报告目录（P1.6E 起的正式位置）：软件目录下的 ``ego_report\\``。

    * 打包后（frozen）= EXE 所在目录；源码运行 = markers.py 所在目录；
    * 目录不可写（如 EXE 放在只读位置）时回退到用户数据目录下的 ego_report。
    """
    base = os.path.dirname(os.path.abspath(
        sys.executable if getattr(sys, 'frozen', False) else __file__))
    d = os.path.join(base, 'ego_report')
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, '.write_probe')
        with open(probe, 'w', encoding='utf-8') as fh:
            fh.write('1')
        os.remove(probe)
        return d
    except OSError:
        pass
    try:
        from appcache import user_data_dir
        d = os.path.join(user_data_dir(), 'ego_report')
    except Exception:
        d = os.path.join(os.path.expanduser('~'), 'MCAPViewer', 'ego_report')
    os.makedirs(d, exist_ok=True)
    return d


def write_report(folder, summary, fallback_dir=None):
    """写报告；返回写入路径。

    候选顺序（P1.6E 起）：
      1. 软件目录下的 ``ego_report\\``（正式位置，文件名 = 设备名 + 采集日期）；
      2. fallback_dir 下的 ``ego_report\\``（调用方传用户数据目录时的回退）；
      3. 视频文件夹（旧版位置，最后回退）。
    """
    text = report_text(summary)
    name = report_file_name(summary)
    candidates = [os.path.join(report_dir(), name)]
    if fallback_dir:
        candidates.append(os.path.join(fallback_dir, 'ego_report', name))
    candidates.append(os.path.join(folder, name))   # 旧位置回退，命名统一
    last_error = None
    for path in candidates:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + '.tmp-%d' % os.getpid()
            with open(tmp, 'w', encoding='utf-8-sig') as fh:
                fh.write(text)
            os.replace(tmp, path)
            return path
        except OSError as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    return ''
