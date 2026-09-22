"""FIX002 补丁：锚点计数 + 基准脚本独立缓存根（用后即删）"""
import io

# ---------- 1) desktop.py：加锚点计数（供回归与基准观测） ----------
p = 'desktop.py'
src = io.open(p, encoding='utf-8').read()
if '_direct_anchor_count' not in src:
    old = """        self._direct_anchor_media_s = 0.0         # Direct 媒体时钟唯一基准（相对秒）"""
    new = """        self._direct_anchor_media_s = 0.0         # Direct 媒体时钟唯一基准（相对秒）
        self._direct_anchor_count = 0             # 锚定次数（正常播放不得增长）"""
    assert old in src, 'anchor var not found'
    src = src.replace(old, new, 1)
    old2 = """            self._direct_anchor_media_s = float(frame.media_time)
            self.t = self._direct_anchor_media_s
            self.t_start = self.t
            self.clock.restart()
            self._direct_anchor_pending = False"""
    new2 = """            self._direct_anchor_media_s = float(frame.media_time)
            self.t = self._direct_anchor_media_s
            self.t_start = self.t
            self.clock.restart()
            self._direct_anchor_count += 1
            self._direct_anchor_pending = False"""
    assert old2 in src, 'anchor block not found'
    src = src.replace(old2, new2, 1)
    io.open(p, 'w', encoding='utf-8').write(src)
    print('desktop.py：已加 _direct_anchor_count')
else:
    print('desktop.py：锚点计数已存在')

# ---------- 2) 基准脚本：独立缓存根 + 时钟指标 ----------
p2 = 'tests/f6f_step0_desktop_baseline.py'
s2 = io.open(p2, encoding='utf-8').read()

s2 = s2.replace(
    """os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ['MCAPVIEWER_CACHE'] = os.path.join(TMP, 'f6f_cache')
os.environ['MCAPVIEWER_STATE_DIR'] = os.path.join(TMP, 'f6f_state')""",
    """os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
# FIX002：本次运行使用**独立空缓存根**（绝不删共享根，避免沙箱批量删除保护）
RUN_ROOT = os.path.join(TMP, 'f6f_step0_%s' % time.strftime('%H%M%S'))
os.makedirs(RUN_ROOT, exist_ok=True)
os.environ['MCAPVIEWER_CACHE'] = os.path.join(RUN_ROOT, 'cache_main')
os.environ['MCAPVIEWER_STATE_DIR'] = os.path.join(RUN_ROOT, 'state')""")

s2 = s2.replace(
    """        shutil.rmtree(appcache.CACHE_ROOT, ignore_errors=True)
        rec = dict(label=label, size_mb=round(os.path.getsize(src) / 1048576, 1))""",
    """        # 每个样本用新的独立子目录（mkdir，不删除任何已有目录）
        sub = os.path.join(RUN_ROOT, 'cache_%s' % label.replace(' ', '_'))
        os.makedirs(sub, exist_ok=True)
        appcache.CACHE_ROOT = sub
        rec = dict(label=label, size_mb=round(os.path.getsize(src) / 1048576, 1))""")

s2 = s2.replace(
    """        rec['ttfp_ms'] = round(getattr(win, '_direct_ttpf_ms', None) or -1, 1)
        rec['backend'] = win._playback_backend
        rec['set_image_calls_at_ttfp'] = len(calls)""",
    """        rec['ttfp_ms'] = round(getattr(win, '_direct_ttpf_ms', None) or -1, 1)
        rec['backend'] = win._playback_backend
        rec['set_image_calls_at_ttfp'] = len(calls)
        rec['clock_valid'] = bool(win.clock.isValid())
        rec['anchor_count_after_open'] = getattr(win, '_direct_anchor_count', -1)
        rec['negative_t_at_open'] = bool(win.t < 0)""")

s2 = s2.replace(
    """        rec['rate_1x'] = measure(1.0, 10.0)
        rec['rate_2x'] = measure(2.0, 10.0)""",
    """        rec['rate_1x'] = measure(1.0, 10.0)
        rec['rate_2x'] = measure(2.0, 10.0)
        rec['clock_valid_after'] = bool(win.clock.isValid())
        rec['negative_t_after'] = bool(win.t < 0)
        rec['anchor_count_total'] = getattr(win, '_direct_anchor_count', -1)
        rec['slider_value'] = win.slider.value()
        rec['slider_max'] = win.slider.maximum()""")

io.open(p2, 'w', encoding='utf-8').write(s2)
print('基准脚本：独立缓存根 + 时钟指标 已应用')
