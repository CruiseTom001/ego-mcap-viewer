"""desktop.py —— MCAP 视频查看器 · Windows 原生桌面版

打开一个文件夹，就能把里面的 .mcap 一个接一个顺着看完。
左边播放列表，看完当前自动跳下一个（可在工具栏关闭）。

界面：PySide6 / Qt6
解码：OpenCV 自己解码 + Qt 自绘（不经 QtMultimedia，避免额外的编解码器差异）
音频：Windows MCI（可选，失败自动禁用；不支持变速时按策略自动静音）

时间同步
    每条通道都有独立的逐帧时间戳（相对 manifest 的 time_base_ns，单位秒）。
    取帧用 bisect 找「不晚于主时钟的最后一帧」，而不是「主时钟 × 平均 fps」，
    所以相机起点不同、变帧率、中间丢帧都能对齐。
"""

import os
import sys
import json
import time
import bisect
import ctypes
import re
import threading
import traceback
from concurrent.futures import CancelledError

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from PySide6.QtCore import (Qt, QTimer, QThread, Signal, QElapsedTimer,
                            QSettings, QSize, QPointF, QRect, QRectF, QEvent)
from PySide6.QtGui import (QAction, QKeySequence, QPainter, QColor, QPen, QFont,
                           QPixmap, QImage, QPainterPath, QBrush)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton, QToolBar,
    QVBoxLayout, QHBoxLayout, QGridLayout, QSplitter, QListWidget, QListWidgetItem,
    QSlider, QComboBox, QCheckBox, QFileDialog, QProgressDialog, QMessageBox,
    QSizePolicy, QStatusBar, QMenu, QDialog, QTextEdit, QAbstractItemView,
    QScrollArea, QGroupBox, QFormLayout, QDialogButtonBox, QTabWidget, QToolButton
)

import appcache
import playlist as PL
import prepare as PREP
import watchstate
import queue_manager as QM
import markers as MK

APP_NAME = 'MCAP 视频查看器'
COLORS = ['#4c8dff', '#22d3a6', '#ffb02e', '#ff6b9d', '#a78bfa', '#38bdf8',
          '#f97316', '#84cc16', '#e879f9', '#facc15']

try:
    import numpy as np
    import cv2
    HAS_CV = True
except Exception:
    np = None
    cv2 = None
    HAS_CV = False

#: 落后超过此帧数就直接 seek 到主时钟附近，避免某一路越追越落后。
#: 值过大时 2×/5× 会把 CPU 全花在已经过时、永远不会显示的帧上。
MAX_SEQUENTIAL_CATCHUP = 8


# ================================================================== 工具函数
def pick_frame(times, t):
    """返回「不晚于 t 的最后一帧」下标；t 早于首帧时返回 -1。

    这是全程序唯一的选帧逻辑：播放、拖动、逐帧、导出都用它。
    """
    if not times:
        return -1
    i = bisect.bisect_right(times, t + 1e-9) - 1
    return i if i >= 0 else -1


def is_primary_view(cam):
    """桌面版只展示 camera2 / camera3；同时识别 key 和完整 topic。"""
    text = '%s %s' % (cam.get('key', ''), cam.get('topic', ''))
    return re.search(r'(?:camera|cam)[_-]?(?:2|3)(?!\d)', text.lower()) is not None


class QueueRow(QFrame):
    """三队列的一行：双击执行主要操作（缓存后播放 / 播放 / 提示重新缓存）"""

    def __init__(self, main_action, parent=None):
        super().__init__(parent)
        self._main_action = main_action
        self.setCursor(Qt.PointingHandCursor)

    def mouseDoubleClickEvent(self, ev):
        if self._main_action:
            self._main_action()
        super().mouseDoubleClickEvent(ev)


class MarkerBar(QWidget):
    """不合格片段标记条：红=不合格片段，黄线=待闭合的起点，蓝线=当前播放位置"""

    manageRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(10)
        self.setMinimumWidth(120)
        self.setToolTip('红色 = 已标注的不合格片段；黄色竖线 = 已按下第一下 X、'
                        '等待第二下闭合的起点。\n右键可打开「标注管理」逐段删除。')
        self._duration = 0.0
        self._segments = []
        self._pending = None
        self._t = 0.0

    def mousePressEvent(self, ev):
        if ev.button() == Qt.RightButton:
            self.manageRequested.emit()
            ev.accept()
            return
        super().mousePressEvent(ev)

    def set_data(self, duration, segments, pending, t=0.0):
        self._duration = float(duration or 0.0)
        self._segments = list(segments or [])
        self._pending = pending
        self._t = float(t or 0.0)
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor('#2a2f3a'))
        d = self._duration if self._duration > 0 else 1.0
        for a, b in self._segments:
            x1 = int(max(0.0, min(1.0, a / d)) * w)
            x2 = int(max(0.0, min(1.0, b / d)) * w)
            p.fillRect(x1, 0, max(1, x2 - x1), h, QColor('#e5484d'))
        if self._pending is not None:
            x = int(max(0.0, min(1.0, self._pending / d)) * w)
            p.fillRect(x, 0, 2, h, QColor('#ffb02e'))
        prog = int(max(0.0, min(1.0, (self._t / d) if d else 0.0)) * w)
        p.fillRect(prog, 0, 1, h, QColor('#4c8dff'))


def cache_outdir(fid):
    """桌面精简缓存的目录：fid@desktop_camera2_camera3_noaudio_v2。

    与 server.py 的完整缓存（全部相机通道、无后缀目录）通过命名空间分离，
    互不冲突；同一份源文件的两套缓存可以共存。
    """
    return appcache.cache_dir(appcache.cache_key(fid)) if fid else ''


class ElidedLabel(QLabel):
    """中间省略的文件名标签：宽度不够时显示 DAS-Ego_20260911...65416a8a.mcap"""

    def __init__(self, text='', parent=None):
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        if text:
            self.setText_full(text)

    def setText_full(self, text):
        self._full = text
        self.setToolTip(text)
        self._relayout()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._relayout()

    def _relayout(self):
        fm = self.fontMetrics()
        self.setText(fm.elidedText(self._full, Qt.ElideMiddle,
                                   max(40, self.width() - 2)))


def downscale(img, target_w):
    """等比例缩小到「宽度不超过 target_w」，用 INTER_AREA 抗锯齿。

    返回 (图像, 是否缩小过)；已经不宽于目标时原样返回。
    """
    if not target_w:
        return img, False
    h, w = img.shape[:2]
    if w <= target_w:
        return img, False
    scale = target_w / float(w)
    out_w = max(1, int(target_w))
    out_h = max(1, int(round(h * scale)))
    return cv2.resize(img, (out_w, out_h), interpolation=cv2.INTER_AREA), True


def bgr_to_qimage(bgr):
    h, w = bgr.shape[:2]
    return QImage(bgr.tobytes(), w, h, w * 3, QImage.Format_BGR888).copy()


def imread_unicode(path):
    """cv2.imread 在中文路径下会失败，用 imdecode 绕开"""
    try:
        buf = np.fromfile(path, dtype=np.uint8)
        if buf.size == 0:
            return None
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:
        return None


# ------------------------------------------------------------------ 参考框
#: 参考框最小尺寸（占画面比例）。比这更小的拖拽视为误触，不生成框。
MIN_ROI_SIZE = 0.03


def normalize_roi(roi):
    """把 (x, y, w, h) 夹到 [0,1] 内并保证宽高为正；非法返回 None"""
    if not roi:
        return None
    try:
        x, y, w, h = (float(v) for v in roi)
    except (TypeError, ValueError):
        return None
    x = min(max(x, 0.0), 1.0)
    y = min(max(y, 0.0), 1.0)
    w = min(max(w, 0.0), 1.0 - x)
    h = min(max(h, 0.0), 1.0 - y)
    if w < MIN_ROI_SIZE or h < MIN_ROI_SIZE:
        return None
    return (x, y, w, h)


def roi_from_points(a, b):
    """两个归一化点 → 归一化矩形；太小或缺失返回 None"""
    if a is None or b is None:
        return None
    x0, y0 = min(a[0], b[0]), min(a[1], b[1])
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    return normalize_roi((x0, y0, x1 - x0, y1 - y0))


def roi_to_pixels(roi, width, height):
    """归一化参考框 → 源图像素矩形 (x, y, w, h)，用于导出时把框画进画面"""
    r = normalize_roi(roi)
    if r is None or width <= 0 or height <= 0:
        return None
    x, y, w, h = r
    return (int(round(x * width)), int(round(y * height)),
            max(1, int(round(w * width))), max(1, int(round(h * height))))


ROI_COLOR_BGR = (255, 255, 255)      # 白色虚线，和 Genrobot 官方 Monitor 一致

#: 参考框默认形状。官方 Monitor 里那个是「四角特别圆的圆角矩形」——
#: 顶边/侧边有明显平直段，四角以约 0.3 倍短边为半径过渡。不是椭圆也不是尖角矩形。
ROI_DEFAULT_SHAPE = 'roundrect'

#: 依据 Genrobot Monitor 当前 camera0/camera2 画布测得的近似默认工作区。
#: 坐标基于整幅画面的归一化坐标：(x, y, width, height)。用户拖拽标定值优先。
OFFICIAL_DEFAULT_ROI = (0.25, 0.13, 0.50, 0.73)


def default_roi_for_camera(key):
    """camera2 / camera3 的官网近似默认框；其他通道不自动添加。"""
    text = str(key or '').lower()
    if re.search(r'(?:camera|cam)[_-]?(?:2|3)(?!\d)', text):
        return OFFICIAL_DEFAULT_ROI
    return None


def _roi_corner_radius(w, h):
    """圆角矩形参考框的圆角半径（与官方 Monitor 的形状对齐）"""
    return max(1.0, min(w, h) * 0.30)


def _rounded_rect_polyline(x, y, w, h, r, seg=2.0):
    """沿圆角矩形周长采样一圈点，供 cv2 画虚线用"""
    r = min(r, w / 2.0, h / 2.0)
    pts = []

    def arc(cx, cy, a0, a1):
        steps = max(2, int(abs(a1 - a0) * r / seg))
        for i in range(steps + 1):
            a = np.deg2rad(a0 + (a1 - a0) * i / steps)
            pts.append((cx + r * np.cos(a), cy + r * np.sin(a)))

    pts.append((x + r, y))
    pts.append((x + w - r, y))
    arc(x + w - r, y + r, -90, 0)
    pts.append((x + w, y + h - r))
    arc(x + w - r, y + h - r, 0, 90)
    pts.append((x + r, y + h))
    arc(x + r, y + h - r, 90, 180)
    pts.append((x, y + r))
    arc(x + r, y + r, 180, 270)
    return pts


def _dashed_polyline(img, pts, color, thick, dash, gap):
    """把折线画成虚线（按弧长交替画/跳）"""
    if len(pts) < 2:
        return
    acc = 0.0
    drawing = True
    seg_start = pts[0]
    for i in range(1, len(pts)):
        p0, p1 = pts[i - 1], pts[i]
        d = float(np.hypot(p1[0] - p0[0], p1[1] - p0[1]))
        while d >= (dash if drawing else gap) - acc:
            need = (dash if drawing else gap) - acc
            t = need / d if d else 0.0
            end = (p0[0] + (p1[0] - p0[0]) * t, p0[1] + (p1[1] - p0[1]) * t)
            if drawing:
                cv2.line(img, (int(round(seg_start[0])), int(round(seg_start[1]))),
                         (int(round(end[0])), int(round(end[1]))), color, thick)
            p0 = end
            d -= need
            acc = 0.0
            drawing = not drawing
            seg_start = p0
        acc += d
        seg_start = p1
    if drawing and seg_start != pts[-1]:
        cv2.line(img, (int(round(seg_start[0])), int(round(seg_start[1]))),
                 (int(round(pts[-1][0])), int(round(pts[-1][1]))), color, thick)


def draw_roi_on_frame(img, roi, label=None, shape=ROI_DEFAULT_SHAPE, dim=False):
    """把参考框画到 numpy BGR 图像上（导出留证用）。img 原地修改。

    shape: 'roundrect'（默认，与官方 Monitor 一致）/ 'ellipse' / 'rect'
    dim:   是否把框外压暗。官方那条线没有压暗，默认关。
    """
    box = roi_to_pixels(roi, img.shape[1], img.shape[0])
    if box is None:
        return img
    x, y, w, h = box
    h_img, w_img = img.shape[:2]
    color = ROI_COLOR_BGR
    thick = 2
    center = (x + w // 2, y + h // 2)

    if dim:
        mask = np.full((h_img, w_img), 255, dtype=np.uint8)
        if shape == 'ellipse':
            cv2.ellipse(mask, center, (max(1, w // 2), max(1, h // 2)), 0, 0, 360, 0, -1)
        elif shape == 'roundrect':
            pts = _rounded_rect_polyline(x, y, w, h, _roi_corner_radius(w, h))
            cv2.fillPoly(mask, [np.array(pts, dtype=np.int32)], 0)
        else:
            cv2.rectangle(mask, (x, y), (x + w, y + h), 0, -1)
        img[mask > 0] = (img[mask > 0].astype(np.float32) * 0.72).astype(np.uint8)

    if shape == 'ellipse':
        axes = (max(1, w // 2), max(1, h // 2))
        # 用短线段拼出虚线椭圆（cv2 没有虚线样式）
        step, gapd, a = 12, 8, 0
        while a < 360:
            cv2.ellipse(img, center, axes, 0, a, min(a + step, 360), color, thick)
            a += step + gapd
    elif shape == 'roundrect':
        # 虚线长度随画面宽度缩放，1600px 下约 13px，和官方观感一致
        dash = max(6, int(round(w_img * 0.008)))
        _dashed_polyline(img,
                         _rounded_rect_polyline(x, y, w, h, _roi_corner_radius(w, h)),
                         color, thick, dash, dash)
    else:
        dash, gapd = 14, 9
        for x0 in range(x, x + w, dash + gapd):
            cv2.line(img, (x0, y), (min(x0 + dash, x + w), y), color, thick)
            cv2.line(img, (x0, y + h), (min(x0 + dash, x + w), y + h), color, thick)
        for y0 in range(y, y + h, dash + gapd):
            cv2.line(img, (x, y0), (x, min(y0 + dash, y + h)), color, thick)
            cv2.line(img, (x + w, y0), (x + w, min(y0 + dash, y + h)), color, thick)
        tick = max(10, min(26, w // 8))
        for (cx, cy, dx, dy) in ((x, y, 1, 1), (x + w, y, -1, 1),
                                 (x, y + h, 1, -1), (x + w, y + h, -1, -1)):
            cv2.line(img, (cx, cy), (cx + dx * tick, cy), color, 3)
            cv2.line(img, (cx, cy), (cx, cy + dy * tick), color, 3)
    if label:
        cv2.putText(img, label, (x + 6, max(18, y + 20)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, color, 2)
    return img


# ------------------------------------------------------------------ 鱼眼去畸变
def camera_calibration(cam):
    """从 manifest 的相机条目里取出鱼眼标定。

    返回 ((fx, fy, cx, cy), (k1, k2), 标定对应的图像宽度)；没有标定返回 None。
    这份标定就写在 mcap 的 CameraCalibration 消息里，所以每段录像都自带。
    """
    cal = (cam or {}).get('calibration') or {}
    K = cal.get('K') or []
    D = cal.get('D') or []
    try:
        width = int(cal.get('width') or 0)
    except (TypeError, ValueError):
        width = 0
    if width <= 0 or len(K) < 6:
        return None
    if len(D) >= 6:
        # 这套设备的 D = [fx, fy, cx, cy, k1, k2]，前四位与 K 重复
        k1, k2 = float(D[4]), float(D[5])
    elif len(D) >= 2:
        k1, k2 = float(D[0]), float(D[1])
    else:
        return None
    return ((float(K[0]), float(K[4]), float(K[2]), float(K[5])),
            (k1, k2), width)


def fisheye_undistort(img, calib):
    """把鱼眼画面拉直（等距模型，D=[k1, k2, 0, 0]）。失败时原样返回。

    calib 来自 camera_calibration()。已实测：用录像自带标定拉直后，
    显示器变方、货架垂直，与 Genrobot Monitor 的 Undistorted frame 一致。
    """
    if img is None or not calib:
        return img
    try:
        fx, fy, cx, cy = calib[0]
        k1, k2 = calib[1]
        src_w = float(calib[2]) or float(img.shape[1])
        s = img.shape[1] / src_w
        Kmat = np.array([[fx * s, 0, cx * s],
                         [0, fy * s, cy * s],
                         [0, 0, 1]], dtype=np.float64)
        Dv = np.array([k1, k2, 0.0, 0.0], dtype=np.float64)
        return cv2.fisheye.undistortImage(img, Kmat, Dv, Knew=Kmat)
    except Exception:
        return img




# ================================================================== 解码线程
class CamStream(QThread):
    """一路相机一条线程：按帧号解码并缩放，随时提供「最新可用帧」。

    线程安全：_want / _force 由 request / invalidate 写，_frame / _index / _cur
    由线程写、latest 读，全部走同一把锁。
    """

    def __init__(self, path=None, target_w=800, frame_files=None, parent=None):
        super().__init__(parent)
        self.path = path
        self.frame_files = frame_files          # 图片序列模式：文件路径列表
        self.target_w = target_w
        self.frames = len(frame_files) if frame_files else 0
        self.fps = 0.0
        self.width = 0
        self.height = 0
        self.error = ''
        self.catchup_limit = MAX_SEQUENTIAL_CATCHUP
        self.calib = None           # 鱼眼标定 (K4, (k1,k2), 源宽度)
        self.undistort = False      # 是否把鱼眼画面拉直（在缩放之后做，省 CPU）
        self._lock = threading.Lock()
        self._frame = None
        self._index = -1
        self._cur = -1
        self._want = 0
        self._force = False
        self._run = True

    # ---- 外部接口 ----
    def request(self, idx):
        with self._lock:
            self._want = max(0, int(idx))

    def invalidate(self):
        """强制重新解码当前帧（切换清晰度 / 单路放大时用）"""
        with self._lock:
            self._force = True
            self._want = self._cur if self._cur >= 0 else self._want

    def latest(self):
        with self._lock:
            return self._frame, self._index

    def stop(self):
        self.request_stop()
        try:
            self.wait(4000)
        except Exception:
            pass

    def request_stop(self):
        with self._lock:
            self._run = False
        self.requestInterruption()

    def set_undistort(self, on, calib=None):
        """切换鱼眼去畸变。切换后强制重解当前帧，画面立刻变化。"""
        if calib is not None:
            self.calib = calib
        self.undistort = bool(on) and self.calib is not None
        self.invalidate()

    # ---- 内部 ----
    def _emit(self, img, idx):
        try:
            img, _ = downscale(img, self.target_w)
        except Exception:
            pass
        if self.undistort and self.calib:
            # 在缩放之后的分辨率上做去畸变：800px 约 20ms，1600px 约 84ms
            try:
                img = fisheye_undistort(img, self.calib)
            except Exception:
                pass
        with self._lock:
            self._frame = img
            self._index = idx
            self._cur = idx

    def run(self):
        try:
            if self.frame_files:
                self._run_images()
            else:
                self._run_video()
        except Exception as e:                       # pragma: no cover
            self.error = '%s' % e

    def _run_images(self):
        self.frames = len(self.frame_files)
        while self._run:
            with self._lock:
                want = self._want
                cur = self._cur
                force = self._force
                self._force = False
            if want == cur and not force:
                self.msleep(4)
                continue
            want = max(0, min(want, len(self.frame_files) - 1))
            img = imread_unicode(self.frame_files[want])
            if img is None:
                self.error = '图片读取失败'
                self.msleep(30)
                continue
            if not self.height:
                self.height, self.width = img.shape[:2]
            self._emit(img, want)

    def _run_video(self):
        cap = cv2.VideoCapture(self.path)
        try:
            if not cap.isOpened():
                self.error = '无法打开视频文件'
                return
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            # OpenCV 元数据不可用时保留上层用 manifest 真实帧时间兜底的值
            if total > 0:
                self.frames = total
            if fps > 0:
                self.fps = fps
            while self._run:
                with self._lock:
                    want = self._want
                    cur = self._cur
                    force = self._force
                    self._force = False
                if want == cur and not force:
                    self.msleep(3)
                    continue
                if self.frames:
                    want = max(0, min(want, self.frames - 1))

                if force and want == cur:
                    # 尺寸变了，只需按新尺寸重解当前帧
                    cap.set(cv2.CAP_PROP_POS_FRAMES, want)
                    ok, fr = cap.read()
                    if ok:
                        self._emit(fr, want)
                    continue

                if want > cur and (want - cur) <= self.catchup_limit:
                    # 中间帧只 grab，不做昂贵的 BGR retrieve/转换；最后一帧才 read。
                    ok = True
                    while cur + 1 < want and ok and self._run:
                        ok = cap.grab()
                        cur += 1
                    fr = None
                    if ok and self._run:
                        ok, fr = cap.read()
                        cur += 1
                    if ok and fr is not None:
                        self._emit(fr, want)
                    elif self.frames:
                        with self._lock:
                            self._cur = want
                    continue

                cap.set(cv2.CAP_PROP_POS_FRAMES, want)
                ok, fr = cap.read()
                if not ok:
                    if self.frames:
                        with self._lock:
                            self._cur = want
                    continue
                self._emit(fr, want)
        finally:
            cap.release()


# ================================================================== 画面格
class FrameView(QWidget):
    """单路画面。除了显示视频，还可以叠加一个「参考框」——

    参考框用于人工比对构图（例如 Genrobot 规范里「双手应落在虚线区域内、
    人脸和第三人的手不得进入」）。那个虚线只在采集 App 的实时预览里，不会录进
    码流，所以这里由使用者自己在画面上画一个，位置按「归一化坐标」保存，
    与窗口大小、清晰度档位都无关。
    """

    doubleClicked = Signal()
    roiChanged = Signal(object)      # (x, y, w, h) 归一化 0~1；None 表示清除

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(80, 60)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._img = None
        self._hint = '等待画面…'
        self.roi = None              # (x, y, w, h)，归一化到画面本身
        self.show_roi = True
        self.calibrating = False
        self.roi_shape = ROI_DEFAULT_SHAPE   # roundrect / ellipse / rect
        self.dim_outside = False     # 官方那条线没有压暗，默认关
        self._drag_from = None
        self._drag_to = None
        self.setMouseTracking(True)

    def set_image(self, img):
        self._img = img
        self.update()

    def set_hint(self, txt):
        if self._hint != txt:
            self._hint = txt
            self.update()

    # -------------------------------------------------------- 参考框
    def set_roi(self, roi):
        self.roi = normalize_roi(roi)
        self.update()

    def set_show_roi(self, on):
        self.show_roi = bool(on)
        self.update()

    def set_calibrating(self, on):
        self.calibrating = bool(on)
        self._drag_from = self._drag_to = None
        self.setCursor(Qt.CrossCursor if self.calibrating else Qt.ArrowCursor)
        self.update()

    def set_roi_shape(self, shape):
        """'roundrect'（默认，与官方 Monitor 一致）/ 'ellipse' / 'rect'"""
        self.roi_shape = shape if shape in ('roundrect', 'ellipse', 'rect') \
            else ROI_DEFAULT_SHAPE
        self.update()

    def set_dim_outside(self, on):
        self.dim_outside = bool(on)
        self.update()

    def image_rect(self):
        """画面在控件里的实际显示矩形（等比缩放后居中，可能有黑边）"""
        if self._img is None:
            return QRect()
        iw, ih = self._img.width(), self._img.height()
        r = self.rect()
        if iw <= 0 or ih <= 0 or r.width() <= 0 or r.height() <= 0:
            return QRect()
        s = min(r.width() / iw, r.height() / ih)
        w, h = max(1, int(iw * s)), max(1, int(ih * s))
        return QRect(r.x() + (r.width() - w) // 2, r.y() + (r.height() - h) // 2, w, h)

    def _to_norm(self, pt):
        ir = self.image_rect()
        if ir.isEmpty():
            return None
        x = (pt.x() - ir.x()) / float(ir.width())
        y = (pt.y() - ir.y()) / float(ir.height())
        return (min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0))

    def _to_px(self, roi):
        ir = self.image_rect()
        if roi is None or ir.isEmpty():
            return QRect()
        x, y, w, h = roi
        return QRect(int(round(ir.x() + x * ir.width())),
                     int(round(ir.y() + y * ir.height())),
                     max(1, int(round(w * ir.width()))),
                     max(1, int(round(h * ir.height()))))

    def _current_roi(self):
        """标定中：优先用正在拖拽的框"""
        if self._drag_from and self._drag_to:
            return roi_from_points(self._drag_from, self._drag_to)
        return self.roi

    # -------------------------------------------------------- 鼠标
    def mousePressEvent(self, ev):
        if self.calibrating and ev.button() == Qt.LeftButton:
            p = self._to_norm(ev.position().toPoint())
            if p is not None:
                self._drag_from = self._drag_to = p
                self.update()
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self.calibrating and self._drag_from is not None:
            p = self._to_norm(ev.position().toPoint())
            if p is not None:
                self._drag_to = p
                self.update()
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self.calibrating and self._drag_from is not None and ev.button() == Qt.LeftButton:
            p = self._to_norm(ev.position().toPoint())
            if p is not None:
                self._drag_to = p
            roi = roi_from_points(self._drag_from, self._drag_to)
            self._drag_from = self._drag_to = None
            if roi is None:
                # 只是点了一下（没拖出面积）：保持原样，别把已有框弄丢
                self.update()
            else:
                self.roi = roi
                self.roiChanged.emit(self.roi)
                self.update()
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        if self.calibrating:          # 标定时不触发「单路放大」
            ev.accept()
            return
        self.doubleClicked.emit()

    def contextMenuEvent(self, ev):
        if self.calibrating:
            ev.accept()
            return
        m = QMenu(self)
        a1 = m.addAction('清除参考框')
        a1.setEnabled(self.roi is not None)
        a2 = m.addAction('清除全部参考框')
        act = m.exec(ev.globalPos())
        if act == a1:
            self.set_roi(None)
            self.roiChanged.emit(None)
        elif act == a2:
            self.roiChanged.emit('__all__')

    # -------------------------------------------------------- 绘制
    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor('#000000'))
        if self._img is None:
            p.setPen(QColor('#6b7686'))
            p.drawText(self.rect(), Qt.AlignCenter, self._hint)
            return
        ir = self.image_rect()
        if ir.isEmpty():
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawImage(ir, self._img)
        p.setRenderHint(QPainter.SmoothPixmapTransform, False)

        roi = self._current_roi()
        dragging = self._drag_from is not None
        if self.calibrating or (self.show_roi and roi is not None):
            self._paint_roi(p, ir, roi, dragging)

    def _paint_roi(self, p, ir, roi, dragging):
        if roi is None:
            if dragging:
                return
            p.setPen(QColor('#8b95a7'))
            p.setFont(QFont('Microsoft YaHei UI', 9))
            p.drawText(ir, Qt.AlignHCenter | Qt.AlignBottom,
                       '标定模式：按住左键拖拽画出参考框')
            return

        rect = self._to_px(roi)
        if rect.isEmpty():
            return
        ellipse = self.roi_shape == 'ellipse'
        roundrect = self.roi_shape == 'roundrect'

        # 框外压暗（可选；官方那条线没有压暗，默认关）
        if self.dim_outside:
            dim = QColor(0, 0, 0, 70)
            if ellipse:
                outer = QPainterPath()
                outer.addRect(QRectF(ir))
                inner = QPainterPath()
                inner.addEllipse(QRectF(rect))
                p.fillPath(outer.subtracted(inner), dim)
            elif roundrect:
                outer = QPainterPath()
                outer.addRect(QRectF(ir))
                inner = QPainterPath()
                r = _roi_corner_radius(rect.width(), rect.height())
                inner.addRoundedRect(QRectF(rect), r, r)
                p.fillPath(outer.subtracted(inner), dim)
            else:
                p.fillRect(QRect(ir.x(), ir.y(), ir.width(), rect.y() - ir.y()), dim)
                p.fillRect(QRect(ir.x(), rect.bottom() + 1, ir.width(),
                                 ir.y() + ir.height() - rect.bottom() - 1), dim)
                p.fillRect(QRect(ir.x(), rect.y(), rect.x() - ir.x(), rect.height()), dim)
                p.fillRect(QRect(rect.right() + 1, rect.y(),
                                 ir.x() + ir.width() - rect.right() - 1, rect.height()),
                           dim)

        # 白色虚线，与官方 Monitor 一致；拖拽中变蓝以示区别
        # 细虚线（官方是短划），底下垫一圈深色描边，白手套/白桌面上也看得清
        color = QColor('#4c8dff' if dragging else '#ffffff')

        def _path():
            path = QPainterPath()
            if ellipse:
                path.addEllipse(QRectF(rect))
            elif roundrect:
                r = _roi_corner_radius(rect.width(), rect.height())
                path.addRoundedRect(QRectF(rect), r, r)
            else:
                path.addRect(QRectF(rect))
            return path

        shadow = QPen(QColor(0, 0, 0, 140), 4)
        shadow.setStyle(Qt.DashLine)
        shadow.setDashPattern([5, 4])
        p.setPen(shadow)
        p.drawPath(_path())
        pen = QPen(color, 2)
        pen.setStyle(Qt.DashLine)
        pen.setDashPattern([5, 4])
        p.setPen(pen)
        p.drawPath(_path())

        if not ellipse and not roundrect:
            # 尖角矩形模式保留四角加粗
            pen2 = QPen(color, 3)
            p.setPen(pen2)
            tick = max(8, min(18, rect.width() // 8))
            for (cx, cy, dx, dy) in ((rect.left(), rect.top(), 1, 1),
                                     (rect.right(), rect.top(), -1, 1),
                                     (rect.left(), rect.bottom(), 1, -1),
                                     (rect.right(), rect.bottom(), -1, -1)):
                p.drawLine(cx, cy, cx + dx * tick, cy)
                p.drawLine(cx, cy, cx, cy + dy * tick)

        x, y, w, h = roi
        p.setPen(QColor('#ffffff'))
        p.setFont(QFont('Consolas', 8))
        p.drawText(QRect(rect.x() + 4, max(ir.y(), rect.y() + 2), 200, 14),
                   Qt.AlignLeft | Qt.AlignTop,
                   '%d%%,%d%%  %d%%x%d%%' % (round(x * 100), round(y * 100),
                                             round(w * 100), round(h * 100)))


class VideoPane(QFrame):
    doubleClicked = Signal(str)
    roiChanged = Signal(str, object)      # (相机 key, 归一化参考框 或 None / '__all__')

    def __init__(self, cam, color, times, parent=None):
        super().__init__(parent)
        self.cam = cam
        self.key = cam['key']
        self.color = color
        self.times = list(times or [])
        self.fps = float(cam.get('fps') or 0.0)
        self.frames = int(cam.get('frames') or 0)
        self.stream = None
        self.last_idx = -1
        self._shown_frame = None
        self.setObjectName('pane')
        self.setStyleSheet(
            '#pane{background:#141821;border:1px solid #2b323f;border-radius:8px;}'
            '#pane:hover{border-color:%s;}' % color)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        head = QWidget()
        head.setStyleSheet('background:#1a1f28;border-top-left-radius:8px;'
                           'border-top-right-radius:8px;')
        hl = QHBoxLayout(head)
        hl.setContentsMargins(8, 3, 8, 3)
        hl.setSpacing(8)
        self.lbl_name = QLabel(cam['key'])
        self.lbl_name.setStyleSheet('color:%s;font-weight:700;font-size:11px;' % color)
        self.lbl_info = QLabel('')
        self.lbl_info.setStyleSheet('color:#8b95a7;font-size:10.5px;')
        self.lbl_info.setFont(QFont('Consolas', 9))
        hl.addWidget(self.lbl_name)
        hl.addWidget(self.lbl_info)
        hl.addStretch(1)
        lay.addWidget(head)

        self.calib = camera_calibration(cam)   # 鱼眼标定（没有则 None）
        self.undistort = False
        self.view = FrameView()
        self.view.doubleClicked.connect(lambda: self.doubleClicked.emit(self.key))
        self.view.roiChanged.connect(lambda roi: self.roiChanged.emit(self.key, roi))
        lay.addWidget(self.view, 1)

    # ---------------------------------------------------------- 去畸变
    def set_calibration(self, calib):
        """把这一路的鱼眼标定交给解码线程；换文件时会重新调用"""
        self.calib = calib
        if self.stream:
            self.stream.calib = calib
            self.stream.invalidate()

    def set_undistort(self, on):
        self.undistort = bool(on)
        if self.stream:
            self.stream.set_undistort(self.undistort, self.calib)

    # ---------------------------------------------------------- 参考框
    def set_roi(self, roi):
        self.view.set_roi(roi)

    def roi(self):
        return self.view.roi

    def set_roi_shape(self, shape):
        self.view.set_roi_shape(shape)

    def set_dim_outside(self, on):
        self.view.set_dim_outside(on)

    def set_show_roi(self, on):
        self.view.set_show_roi(on)

    def set_calibrating(self, on):
        self.view.set_calibrating(on)

    def start(self, mp4_path=None, frame_files=None, target_w=800):
        self.stream = CamStream(mp4_path, target_w, frame_files, self)
        # 先用 manifest 的真实帧数 / fps 兜底
        self.stream.frames = self.frames
        self.stream.fps = self.fps
        self.stream.calib = self.calib
        self.stream.undistort = self.undistort and self.calib is not None
        self.stream.start()
        self.view.set_hint('正在解码…')

    def stop(self):
        if self.stream:
            self.stream.request_stop()
            self.stream.stop()
            self.stream = None

    def refresh(self, t):
        if not self.stream:
            return
        idx = pick_frame(self.times, t)
        if idx < 0:
            # 主时钟还没走到这一路的首帧，不能提前显示首帧
            self.view.set_hint('等待相机信号…')
            self.lbl_info.setText('— 等待信号 —')
            return
        self.stream.request(idx)
        frame, got = self.stream.latest()
        # 60Hz 调度下同一解码帧可能连续显示数次；只转换一次 QImage。
        if frame is not None and got >= 0 and frame is not self._shown_frame:
            self.view.set_image(bgr_to_qimage(frame))
            self._shown_frame = frame
        elif self.stream.error:
            self.view.set_hint('⚠ ' + self.stream.error)
        self.last_idx = idx
        self.lbl_info.setText('#%-5d %9.4fs' % (idx, t))


# ================================================================== IMU 曲线
class ImuChart(QWidget):
    SERIES = [('gyro_x', '#4c8dff', 'av', 0), ('gyro_y', '#22d3a6', 'av', 1),
              ('gyro_z', '#ffb02e', 'av', 2), ('acc_x', '#ff6b9d', 'la', 0),
              ('acc_y', '#a78bfa', 'la', 1), ('acc_z', '#38bdf8', 'la', 2)]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(116)
        self.data = None
        self.duration = 1.0
        self.t = 0.0
        self.show_gyro = True
        self.show_accel = True
        self._cache = None
        self._L = None

    def set_data(self, data, duration):
        self.data = data
        self.duration = max(0.001, float(duration or 1.0))
        self._cache = None
        self.update()

    def clear(self):
        self.set_data(None, 1.0)

    def set_time(self, t):
        self.t = t
        self.update()

    def set_visible_series(self, gyro, accel):
        self.show_gyro, self.show_accel = gyro, accel
        self._cache = None
        self.update()

    def _layout(self):
        W, H = max(1, self.width()), max(1, self.height())
        padL, padR, padT, padB = 48, 10, 8, 18
        return W, H, padL, padR, padT, padB, max(10, W - padL - padR), max(10, H - padT - padB)

    def _build(self):
        _W, _H, padL, padR, padT, padB, iw, ih = self._layout()
        d = self.data
        t = d['t']
        n = len(t)
        maxabs = 1e-6
        for _nm, _c, grp, i in self.SERIES:
            if (grp == 'av' and not self.show_gyro) or (grp == 'la' and not self.show_accel):
                continue
            arr = d[grp][i]
            for k in range(0, n, 3):
                v = arr[k]
                if v > maxabs:
                    maxabs = v
                elif -v > maxabs:
                    maxabs = -v
        maxabs *= 1.08

        def X(sec):
            return padL + (sec / self.duration) * iw

        def Y(v):
            return padT + ih * (0.5 - v / (2 * maxabs))

        paths = []
        step = max(1, n // max(1, iw * 2))
        for _nm, color, grp, i in self.SERIES:
            if (grp == 'av' and not self.show_gyro) or (grp == 'la' and not self.show_accel):
                continue
            arr = d[grp][i]
            path = QPainterPath()
            first = True
            for k in range(0, n, step):
                if first:
                    path.moveTo(QPointF(X(t[k]), Y(arr[k])))
                    first = False
                else:
                    path.lineTo(QPointF(X(t[k]), Y(arr[k])))
            paths.append((path, color))
        return dict(paths=paths, maxabs=maxabs, padL=padL, padR=padR, padT=padT,
                    padB=padB, iw=iw, ih=ih)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor('#171b24'))
        if not self.data:
            p.setPen(QColor('#6b7686'))
            p.drawText(self.rect(), Qt.AlignCenter, '该文件不含 IMU 数据')
            return
        if self._cache is None or self._cache.size() != self.size():
            self._L = self._build()
            pm = QPixmap(self.size())
            pm.fill(QColor('#171b24'))
            pp = QPainter(pm)
            self._draw_static(pp, self._L)
            pp.end()
            self._cache = pm
        p.drawPixmap(0, 0, self._cache)
        L = self._L
        px = L['padL'] + (min(self.t, self.duration) / self.duration) * L['iw']
        p.setPen(QPen(QColor('#ff5d5d'), 1.5))
        p.drawLine(QPointF(px, L['padT']), QPointF(px, L['padT'] + L['ih']))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor('#ff5d5d')))
        p.drawEllipse(QPointF(px, L['padT']), 2.6, 2.6)

    def _draw_static(self, p, L):
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setFont(QFont('Consolas', 8))
        for k in range(5):
            y = L['padT'] + L['ih'] * k / 4
            p.setPen(QPen(QColor('#2b323f'), 1))
            p.drawLine(QPointF(L['padL'], y), QPointF(L['padL'] + L['iw'], y))
            p.setPen(QColor('#6b7686'))
            p.drawText(QRectF(0, y - 8, L['padL'] - 6, 16),
                       Qt.AlignRight | Qt.AlignVCenter, '%.2f' % (L['maxabs'] * (1 - k / 2)))
        p.setPen(QColor('#6b7686'))
        p.drawText(QRectF(L['padL'], L['padT'] + L['ih'] + 2, 60, 14), Qt.AlignLeft, '0s')
        p.drawText(QRectF(L['padL'] + L['iw'] - 60, L['padT'] + L['ih'] + 2, 60, 14),
                   Qt.AlignRight, '%.1fs' % self.duration)
        p.setRenderHint(QPainter.Antialiasing, False)
        for path, color in L['paths']:
            p.setPen(QPen(QColor(color), 1.0))
            p.drawPath(path)


# ================================================================== MCI 音频
class MciAudio:
    """用 Windows MCI 播放 WAV，无需额外依赖；失败自动禁用。

    MCI 的 waveaudio 设备通常不支持 ``set speed``，本类会探测一次；
    不支持时由桌面端执行「非 1× 自动静音」策略，
    绝不允许出现视频变速而音频仍以 1× 播放的情况。
    """

    def __init__(self):
        self.ok = False
        self.speed_supported = False
        self.alias = 'mcapview_audio'
        self._winmm = None
        try:
            self._winmm = ctypes.WinDLL('winmm.dll')
            self.ok = True
        except Exception:
            self.ok = False

    # ---- 底层 ----
    def _send(self, cmd):
        if not self.ok or self._winmm is None:
            return ''
        buf = ctypes.create_unicode_buffer(512)
        try:
            ret = self._winmm.mciSendStringW(ctypes.c_wchar_p(cmd), buf, 512, None)
        except Exception:
            return ''
        return buf.value if ret == 0 else ''

    def _cmd_ok(self, cmd):
        if not self.ok or self._winmm is None:
            return False
        try:
            return self._winmm.mciSendStringW(ctypes.c_wchar_p(cmd), None, 0, None) == 0
        except Exception:
            return False

    # ---- 设备 ----
    def load(self, path):
        self.close()
        if not self.ok:
            return False
        safe = path.replace('"', '')
        if not self._cmd_ok('open "%s" type waveaudio alias %s' % (safe, self.alias)):
            self.ok = False
            return False
        if not self._send('status %s length' % self.alias):
            self.close()
            self.ok = False
            return False
        self._send('set %s time format milliseconds' % self.alias)
        self.speed_supported = self._probe_speed()
        return True

    def _probe_speed(self):
        """探测 MCI 能否真正改变播放速度（waveaudio 一般不支持）"""
        try:
            before = self._send('status %s speed' % self.alias)
            self._send('set %s speed 2000' % self.alias)
            after = self._send('status %s speed' % self.alias)
            self._send('set %s speed 1000' % self.alias)
            return bool(before) and bool(after) and before != after
        except Exception:
            return False

    def set_speed(self, rate):
        if not (self.ok and self.speed_supported):
            return False
        return self._cmd_ok('set %s speed %d' % (self.alias, int(rate * 1000)))

    def play_from(self, ms):
        return self._cmd_ok('play %s from %d' % (self.alias, max(0, int(ms))))

    def pause(self):
        self._send('pause %s' % self.alias)

    def resume(self):
        self._send('resume %s' % self.alias)

    def stop(self):
        self._send('stop %s' % self.alias)
        self._send('seek %s to start' % self.alias)

    def set_volume(self, v):
        self._send('setaudio %s volume to %d'
                   % (self.alias, int(max(0.0, min(1.0, v)) * 1000)))

    def close(self):
        if self._winmm is not None:
            try:
                self._winmm.mciSendStringW(
                    ctypes.c_wchar_p('close %s' % self.alias), None, 0, None)
            except Exception:
                pass


class QtAudio:
    """macOS / Linux 音频后端：QtMultimedia（AVFoundation / GStreamer 后端）。

    与 MciAudio 相同的接口与容错策略：
      * 任何一步失败 → ok=False，桌面端自动静音（绝不阻塞视频播放）；
      * 变速支持靠 setPlaybackRate 探测，不支持时由桌面端执行
        「非 1× 自动静音」策略。
    """

    def __init__(self):
        self.ok = False
        self.speed_supported = False
        self.alias = 'qt_audio'
        self._player = None
        self._out = None
        self._position_ms = 0
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
        except Exception:
            return
        try:
            self._out = QAudioOutput()
            self._out.setVolume(1.0)
            self._player = QMediaPlayer()
            self._player.setAudioOutput(self._out)
            self.ok = True
        except Exception:
            self.ok = False

    def _try(self, fn, *a):
        try:
            fn(*a)
            return True
        except Exception:
            return False

    def load(self, path):
        self.close()
        if not self.ok or self._player is None:
            return False
        from PySide6.QtCore import QUrl
        try:
            self._player.setSource(QUrl.fromLocalFile(os.path.abspath(path)))
            self.speed_supported = self._probe_speed()
            return True
        except Exception:
            self.ok = False
            return False

    def _probe_speed(self):
        """探测能否真正改变播放速率（AVFoundation 对音频通常支持）"""
        p = self._player
        if p is None:
            return False
        try:
            before = p.playbackRate()
            p.setPlaybackRate(2.0)
            after = p.playbackRate()
            p.setPlaybackRate(before)
            return after != before or abs(after - 2.0) < 0.01
        except Exception:
            return False

    def set_speed(self, rate):
        if not (self.ok and self._player is not None):
            return False
        try:
            self._player.setPlaybackRate(float(rate))
            return True
        except Exception:
            return False

    def play_from(self, ms):
        if not (self.ok and self._player is not None):
            return False
        try:
            self._player.setPosition(max(0, int(ms)))
            self._player.play()
            return True
        except Exception:
            return False

    def pause(self):
        if self._player is not None:
            self._try(self._player.pause)

    def resume(self):
        if self._player is not None:
            self._try(self._player.play)

    def stop(self):
        if self._player is not None:
            self._try(self._player.stop)
            self._try(self._player.setPosition, 0)

    def set_volume(self, v):
        if self._out is not None:
            self._try(self._out.setVolume,
                      float(max(0.0, min(1.0, v))))

    def close(self):
        if self._player is not None:
            self._try(self._player.stop)
            self._try(self._player.setSource, None)
        self.ok = self.ok and self._player is not None


def make_audio():
    """按平台选择音频后端；都失败时返回的后端 ok=False（自动静音）"""
    if sys.platform == 'darwin':
        return QtAudio()
    return MciAudio()


# ================================================================== 准备线程
class PrepareWorker(QThread):
    progressed = Signal(float, str)
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, path, outdir, parent=None):
        super().__init__(parent)
        self.path = path
        self.outdir = outdir
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()
        self.requestInterruption()

    def run(self):
        try:
            man = PREP.prepare(self.path, self.outdir,
                               progress=lambda p, m: self.progressed.emit(p, m),
                               cancel_event=self._cancel,
                               camera_pred=appcache.profile_keeps_topic,
                               profile=appcache.CACHE_PROFILE)
            if self._cancel.is_set():
                return
            self.finished_ok.emit(man)
        except CancelledError:
            return
        except Exception:
            if not self._cancel.is_set():
                self.failed.emit(traceback.format_exc())


# ================================================================== 主窗口
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1560, 980)
        self.setMinimumSize(1080, 680)
        self.setAcceptDrops(True)
        self.setStyleSheet(self._style())

        self.settings = QSettings('MCAPViewer', 'Desktop')
        self.folder = ''
        self.items = []
        self.index = -1
        self.manifest = None
        self.fid = None
        self.panes = {}
        self.order = []
        self.visible = set()
        self.solo_key = None
        self.cols = 3
        self.quality_w = 800
        self.rois = {}               # 相机 key -> 归一化参考框 [x, y, w, h]
        self.show_roi = False
        self.calibrating = False
        self.roi_shape = ROI_DEFAULT_SHAPE
        self.dim_outside = False
        self.undistort = False
        self.t = 0.0
        self.duration = 0.0
        self.playing = False
        self.speed = 1.0
        # P1.6E-R2：播放指标（仅 debug/状态，不写 manifest）
        self.pb = dict(decoded=0, rendered=0, dropped=0, catchup_seek_count=0,
                       max_drift_ms=0.0, camera_switch_latency_ms=0.0)
        self._lag_since = None
        self._last_catchup = 0.0
        self._finish_emitted = False
        self.t_start = 0.0
        self.clock = QElapsedTimer()
        self.tick_n = 0
        self.worker = None          # 兼容保留（缓存任务已移交 QueueManager）
        self.warm = None
        self.imu_data = None
        self.loading = False
        self.audio = make_audio()      # Windows=MCI / macOS=QtMultimedia
        self.audio_offset = 0.0
        self.audio_started = False
        self.audio_muted_by_speed = False
        self._ratio_key = None
        self._rs_timer = None
        self._dlg = None
        self._closing = False
        self._scrubbing = False
        self._fast_rr = 0
        self.sids = []               # 与 self.items 平行的 source_id 列表
        self._pending_play_sid = None  # 点名缓存后等待就绪的 sid（'__next__'=自动连播）
        self._prompted_folder = None   # 已弹过「全部看完」询问的文件夹（防重复弹）
        self._last_report_path = ''    # 最近一次生成的定位合格率报告
        self._bad_norm = []            # 当前视频的规范化不合格片段（缓存给标记条）
        self._bad_pending = None       # 待闭合的起点

        # 三队列控制器（未看 / 已缓存 / 已看完），缓存管理的唯一决策者
        self.qm = QM.CacheQueueManager(self)
        self.qm.on_status = self._say
        # F-E2：Direct（打开即播）会话状态；generation/worker 由 session 内部管理
        self._direct_session = None
        self._playback_backend = 'NONE'
        self._direct_ttpf_t0 = None
        self._direct_ttpf_ms = None
        self._direct_autoplay = True
        self._direct_sid = None
        self._pending_resume_time = None
        self._direct_anchor_pending = False       # 仅在首帧/seek/切换后锚定一次
        import direct_playback as _DP
        self._DP = _DP
        # 启动对账：安全清理已退休桌面 profile（如 noaudio 升级前的 v1）的陈旧缓存
        try:
            appcache.purge_retired_desktop_caches(PREP.running_jobs())
        except Exception:
            pass

        self._build_ui()
        self.qm.queuesChanged.connect(self._refresh_queues)
        self.qm.itemProgress.connect(self._on_qm_progress)
        self.qm.itemError.connect(self._on_qm_error)
        self.qm.currentReady.connect(self._on_qm_ready)
        self.qm.cacheDeleted.connect(self._on_qm_deleted)
        # 删除缓存前先停流并等句柄释放（Windows 上文件被占用会删不掉）
        self.qm.before_delete = self._before_cache_delete
        self._build_timer()
        self._restore()
        QApplication.instance().installEventFilter(self)
        QTimer.singleShot(150, self._auto_start)

    @staticmethod
    def _style():
        return """
        QMainWindow, QWidget { background:#10131a; color:#e8ecf3;
            font-family:'Microsoft YaHei UI','Segoe UI'; font-size:12.5px; }
        QToolBar { background:#171b24; border-bottom:1px solid #2b323f;
                   padding:6px 8px; spacing:6px; }
        QToolBar QToolButton, QPushButton { background:#1e232e; border:1px solid #2b323f;
            border-radius:7px; padding:5px 12px; color:#e8ecf3; }
        QToolBar QToolButton:hover, QPushButton:hover { border-color:#4c8dff; color:#4c8dff; }
        QPushButton:disabled, QToolButton:disabled { color:#59616f; border-color:#242a35; }
        QPushButton#primary { background:#4c8dff; border-color:#4c8dff; color:#fff;
                              font-weight:600; }
        QPushButton#primary:hover { background:#5f9bff; color:#fff; }
        QListWidget { background:#171b24; border:1px solid #2b323f; border-radius:8px;
                      padding:4px; outline:none; }
        QListWidget::item { padding:6px 8px; border-radius:6px; margin:2px 1px; }
        QListWidget::item:selected { background:#243b63; color:#e8ecf3; }
        QListWidget::item:hover { background:#1e232e; }
        QSlider::groove:horizontal { height:5px; background:#1e232e; border-radius:3px; }
        QSlider::sub-page:horizontal { background:#4c8dff; border-radius:3px; }
        QSlider::handle:horizontal { width:13px; margin:-5px 0; border-radius:7px;
                                     background:#4c8dff; }
        QComboBox, QCheckBox { background:#1e232e; border:1px solid #2b323f;
                               border-radius:6px; padding:3px 8px; }
        QComboBox::drop-down { border:0; }
        QComboBox QAbstractItemView { background:#1e232e; border:1px solid #2b323f;
                                      selection-background-color:#243b63; }
        QGroupBox { border:1px solid #2b323f; border-radius:8px; margin-top:9px;
                    padding-top:8px; }
        QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 4px;
                           color:#8b95a7; }
        QLabel#clock { font-family:Consolas; font-size:15px; }
        QLabel#dim { color:#8b95a7; }
        QStatusBar { background:#171b24; border-top:1px solid #2b323f; color:#8b95a7; }
        QSplitter::handle { background:#2b323f; width:1px; }
        QScrollBar:vertical { background:#171b24; width:10px; margin:0; }
        QScrollBar::handle:vertical { background:#333b49; border-radius:5px; min-height:24px; }
        QScrollBar::add-line, QScrollBar::sub-line { height:0; }
        QScrollBar:horizontal { background:#171b24; height:10px; }
        QScrollBar::handle:horizontal { background:#333b49; border-radius:5px; min-width:24px; }
        QMenu { background:#1e232e; border:1px solid #2b323f; }
        QMenu::item:selected { background:#243b63; }
        QProgressDialog { background:#171b24; }
        """

    # ---------------------------------------------------------------- 界面
    def _build_ui(self):
        tb = QToolBar('main')
        tb.setMovable(False)
        self.addToolBar(tb)

        act = QAction('📁  打开文件夹…', self)
        act.setShortcut(QKeySequence('Ctrl+O'))
        act.triggered.connect(self.choose_folder)
        tb.addAction(act)

        act = QAction('打开单个文件…', self)
        act.setShortcut(QKeySequence('Ctrl+Shift+O'))
        act.triggered.connect(self.choose_file)
        tb.addAction(act)

        tb.addSeparator()
        self.act_prev = QAction('⏮  上一个', self)
        self.act_prev.setShortcut(QKeySequence('PgUp'))
        self.act_prev.triggered.connect(lambda: self.step_file(-1))
        tb.addAction(self.act_prev)
        self.act_next = QAction('下一个  ⏭', self)
        self.act_next.setShortcut(QKeySequence('PgDown'))
        self.act_next.triggered.connect(lambda: self.step_file(1))
        tb.addAction(self.act_next)

        tb.addSeparator()
        self.chk_auto = QCheckBox('播完自动下一个')
        self.chk_auto.setChecked(bool(self.settings.value('auto_next', True, type=bool)))
        self.chk_auto.stateChanged.connect(
            lambda: self.settings.setValue('auto_next', self.chk_auto.isChecked()))
        tb.addWidget(self.chk_auto)

        tb.addSeparator()
        self.chk_roi = QCheckBox('参考框')
        self.chk_roi.setToolTip('在画面上叠加虚线参考框，用于人工比对构图\n'
                                '（例如核对「双手是否落在虚线区域内」）')
        self.chk_roi.toggled.connect(self._roi_toggle_show)
        tb.addWidget(self.chk_roi)
        self.btn_calib = QPushButton('调整框线')
        self.btn_calib.setCheckable(True)
        self.btn_calib.setToolTip('打开后在画面上按住左键拖拽即可画出/微调参考框；\n'
                                  '在画面上点右键可清除当前/全部参考框')
        self.btn_calib.toggled.connect(self._roi_toggle_calib)
        tb.addWidget(self.btn_calib)

        tb.addSeparator()
        self.chk_undist = QCheckBox('去畸变')
        self.chk_undist.setToolTip('把鱼眼画面拉直（标定参数来自录像本身）。\n'
                                   '文档里那条构图虚线就是画在去畸变后的画面上的，\n'
                                   '要先开这个再调整框线。')
        self.chk_undist.toggled.connect(self._undistort_toggled)
        tb.addWidget(self.chk_undist)

        # 低频参考框操作收纳进菜单，减少顶栏拥挤（保留原控件对象，仅供逻辑复用）
        self.btn_roi_menu = QToolButton()
        self.btn_roi_menu.setText('参考框设置 ▾')
        self.btn_roi_menu.setPopupMode(QToolButton.InstantPopup)
        roi_menu = QMenu(self.btn_roi_menu)
        roi_menu.addAction('恢复官网默认框', self._roi_restore_defaults)
        roi_menu.addAction('应用到所有相机', self._roi_apply_to_all)
        roi_menu.addAction('清除全部', self._roi_clear_all)
        shape_menu = roi_menu.addMenu('框形状')
        self._shape_actions = []
        for _label, _val in (('圆角矩形', 'roundrect'),
                             ('椭圆', 'ellipse'), ('矩形', 'rect')):
            _act = shape_menu.addAction(_label)
            _act.setCheckable(True)
            _act.triggered.connect(lambda _=False, v=_val: self._set_roi_shape(v))
            self._shape_actions.append(_act)
        self.act_dim = roi_menu.addAction('框外压暗')
        self.act_dim.setCheckable(True)
        self.act_dim.toggled.connect(self._dim_toggled)
        self.btn_roi_menu.setMenu(roi_menu)
        tb.addWidget(self.btn_roi_menu)

        # 以下低频控件不再上工具栏，但保留对象供逻辑/测试使用
        self.btn_roi_apply = QPushButton('应用到所有相机')
        self.btn_roi_apply.clicked.connect(self._roi_apply_to_all)
        self.btn_roi_clear = QPushButton('清除全部')
        self.btn_roi_clear.clicked.connect(self._roi_clear_all)
        self.btn_roi_default = QPushButton('恢复默认框')
        self.btn_roi_default.clicked.connect(self._roi_restore_defaults)
        self.cmb_shape = QComboBox()
        self.cmb_shape.addItem('圆角矩形框', 'roundrect')
        self.cmb_shape.addItem('椭圆框', 'ellipse')
        self.cmb_shape.addItem('矩形框', 'rect')
        self.cmb_shape.currentIndexChanged.connect(self._roi_shape_changed)

        sp = QWidget()
        sp.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(sp)
        self.lbl_title = QLabel('未打开文件')
        self.lbl_title.setObjectName('dim')
        tb.addWidget(self.lbl_title)

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.splitter = QSplitter(Qt.Horizontal)
        root.addWidget(self.splitter)

        # 左栏
        left = QWidget()
        left.setMinimumWidth(360)          # 长文件名不得把左栏挤到 550px 以上
        left.setMaximumWidth(560)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(10, 10, 6, 10)
        lv.setSpacing(8)
        t1 = QLabel('播放列表')
        t1.setStyleSheet('font-weight:700;')
        lv.addWidget(t1)
        self.lbl_folder = QLabel('未选择文件夹')
        self.lbl_folder.setObjectName('dim')
        self.lbl_folder.setWordWrap(True)
        lv.addWidget(self.lbl_folder)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.queue_layouts = {}
        for key, title in (('unwatched', '未看 (0)'),
                           ('cached', '已缓存 (0/3)'),
                           ('watched', '已看完 (0)')):
            host = QWidget()
            v = QVBoxLayout(host)
            v.setContentsMargins(4, 4, 4, 4)
            v.setSpacing(4)
            v.addStretch(1)
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.NoFrame)
            area.setWidget(host)
            self.tabs.addTab(area, title)
            self.queue_layouts[key] = v
        lv.addWidget(self.tabs, 1)

        self.btn_refresh = QPushButton('重新扫描文件夹')
        self.btn_refresh.clicked.connect(lambda: self.load_folder(self.folder))
        self.btn_refresh.setEnabled(False)
        lv.addWidget(self.btn_refresh)
        self.btn_clear = QPushButton('清空队列与缓存')
        self.btn_clear.setToolTip('清空三个队列（含已看完记录）并删除所有桌面缓存；\n'
                                  '正在播放的视频与原始 .mcap 文件不动。')
        self.btn_clear.clicked.connect(self.clear_queues_and_cache)
        lv.addWidget(self.btn_clear)

        box = QGroupBox('当前文件')
        self.form = QFormLayout(box)
        self.form.setLabelAlignment(Qt.AlignRight)
        # 常驻只留 5 行，其余进「详细信息…」弹窗，给三队列列表留空间
        self.finfo = {}
        self._fname_label = ElidedLabel('—')
        self._fname_label.setStyleSheet('font-family:Consolas;color:#c8d0dc;')
        self.form.addRow('文件名', self._fname_label)
        for k in ('时长', '当前状态', '缓存大小', '视角'):
            v = QLabel('—')
            v.setStyleSheet('font-family:Consolas;color:#c8d0dc;')
            v.setWordWrap(True)
            self.finfo[k] = v
            self.form.addRow(k, v)
        self.finfo['文件名'] = self._fname_label
        b = QPushButton('详细信息…')
        b.clicked.connect(self._show_details)
        self.form.addRow('', b)
        lv.addWidget(box)
        self.splitter.addWidget(left)

        # 右栏
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(6, 10, 10, 6)
        rv.setSpacing(8)

        bar = QHBoxLayout()
        bar.addWidget(QLabel('布局'))
        for n in (1, 2, 3):
            b = QPushButton('%d 列' % n)
            b.setMaximumWidth(56)
            b.clicked.connect(lambda _=False, k=n: self.set_columns(k))
            bar.addWidget(b)
        bar.addSpacing(10)
        b = QPushButton('全选相机')
        b.clicked.connect(lambda: self.set_all_cams(True))
        bar.addWidget(b)
        b = QPushButton('全不选')
        b.clicked.connect(lambda: self.set_all_cams(False))
        bar.addWidget(b)
        bar.addSpacing(10)
        bar.addWidget(QLabel('解码'))
        self.cmb_q = QComboBox()
        self.cmb_q.addItem('标准 (800px)', 800)
        self.cmb_q.addItem('清晰 (1100px)', 1100)
        self.cmb_q.addItem('原始 (1600px)', 1600)
        self.cmb_q.addItem('流畅 (560px)', 560)
        self.cmb_q.currentIndexChanged.connect(self._quality_changed)
        bar.addWidget(self.cmb_q)
        self.lbl_sel = QLabel('')
        self.lbl_sel.setObjectName('dim')
        bar.addWidget(self.lbl_sel)
        bar.addStretch(1)
        b = QPushButton('导出当前画面')
        b.clicked.connect(self.snapshot_grid)
        bar.addWidget(b)
        self.btn_export = QPushButton('导出 ▾')
        self.btn_export.clicked.connect(self._export_menu)
        bar.addWidget(self.btn_export)
        rv.addLayout(bar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(6)
        self.scroll.setWidget(self.grid_host)
        rv.addWidget(self.scroll, 1)

        self.imu_box = QGroupBox('IMU')
        iv = QVBoxLayout(self.imu_box)
        iv.setContentsMargins(8, 4, 8, 6)
        iv.setSpacing(4)
        ih = QHBoxLayout()
        self.chk_gyro = QCheckBox('陀螺仪')
        self.chk_acc = QCheckBox('加速度')
        self.chk_gyro.setChecked(True)
        self.chk_acc.setChecked(True)
        self.chk_gyro.stateChanged.connect(self._imu_opts)
        self.chk_acc.stateChanged.connect(self._imu_opts)
        self.lbl_imu = QLabel('')
        self.lbl_imu.setFont(QFont('Consolas', 9))
        self.lbl_imu.setStyleSheet('color:#8b95a7;')
        ih.addWidget(self.chk_gyro)
        ih.addWidget(self.chk_acc)
        ih.addStretch(1)
        ih.addWidget(self.lbl_imu)
        iv.addLayout(ih)
        self.imu_chart = ImuChart()
        iv.addWidget(self.imu_chart)
        rv.addWidget(self.imu_box)

        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([420, 1180])

        # 底部
        bottom = QWidget()
        bottom.setStyleSheet('background:#171b24;border-top:1px solid #2b323f;')
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(12, 8, 12, 10)
        bl.setSpacing(6)

        tl = QHBoxLayout()
        self.lbl_clock = QLabel('00:00.000 / 00:00.000')
        self.lbl_clock.setObjectName('clock')
        self.lbl_clock.setMinimumWidth(190)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.sliderPressed.connect(self._slider_down)
        self.slider.sliderReleased.connect(self._slider_up)
        self.slider.valueChanged.connect(self._slider_moved)
        self.lbl_frame = QLabel('—')
        self.lbl_frame.setObjectName('dim')
        self.lbl_frame.setMinimumWidth(250)
        self.lbl_frame.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        tl.addWidget(self.lbl_clock)
        tl.addWidget(self.slider, 1)
        tl.addWidget(self.lbl_frame)
        bl.addLayout(tl)

        self.marker_bar = MarkerBar()
        self.marker_bar.manageRequested.connect(self._manage_marks)
        bl.addWidget(self.marker_bar)

        ct = QHBoxLayout()
        for txt, fn in (('⏮', lambda: self.seek(0)),
                        ('◀|', lambda: self.frame_step(-1))):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            ct.addWidget(b)
        self.btn_play = QPushButton('▶  播放')
        self.btn_play.setObjectName('primary')
        self.btn_play.setMinimumWidth(96)
        self.btn_play.clicked.connect(self.toggle_play)
        ct.addWidget(self.btn_play)
        self.btn_finished = QPushButton('✓ 标记已看完')
        self.btn_finished.setToolTip('结束本视频：移入「已看完」并删除桌面缓存'
                                     '（原始文件保留不动）')
        self.btn_finished.clicked.connect(
            lambda: self.finish_current_video('manual'))
        ct.addWidget(self.btn_finished)
        ct.addSpacing(12)
        self.btn_bad = QPushButton('✗ 标不合格')
        self.btn_bad.setToolTip('把当前时刻记为不合格片段：\n'
                                '第一下按下 = 片段起点，第二下按下 = 片段终点。\n'
                                '键盘上连按两下 X 等效（Ctrl+Z 撤销上一段）。')
        self.btn_bad.clicked.connect(self._mark_bad_point)
        ct.addWidget(self.btn_bad)
        self.btn_bad_undo = QPushButton('↶ 撤销标记')
        self.btn_bad_undo.setToolTip('撤销本视频最后标注的一段不合格片段')
        self.btn_bad_undo.clicked.connect(self._undo_bad_mark)
        ct.addWidget(self.btn_bad_undo)
        self.btn_bad_manage = QPushButton('标注管理…')
        self.btn_bad_manage.setToolTip('查看已标注的不合格片段：可定位复核、逐段删除、'
                                       '清空本视频全部标注')
        self.btn_bad_manage.clicked.connect(self._manage_marks)
        ct.addWidget(self.btn_bad_manage)
        self.lbl_bad = QLabel('不合格 0 段 · 0.0 秒')
        self.lbl_bad.setObjectName('dim')
        ct.addWidget(self.lbl_bad)
        ct.addSpacing(12)
        for txt, fn in (('|▶', lambda: self.frame_step(1)),
                        ('⏭', lambda: self.seek(self.duration))):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            ct.addWidget(b)

        ct.addSpacing(12)
        self.btn_prev_f = QPushButton('⏮ 上一个文件')
        self.btn_prev_f.clicked.connect(lambda: self.step_file(-1))
        self.btn_next_f = QPushButton('下一个文件 ⏭')
        self.btn_next_f.clicked.connect(lambda: self.step_file(1))
        ct.addWidget(self.btn_prev_f)
        ct.addWidget(self.btn_next_f)

        ct.addSpacing(14)
        ct.addWidget(QLabel('速度'))
        self.cmb_speed = QComboBox()
        for s in (0.1, 0.25, 0.5, 1, 1.5, 2, 4, 5, 8):
            self.cmb_speed.addItem(('%g×' % s), s)
        self.cmb_speed.setCurrentIndex(3)
        self.cmb_speed.currentIndexChanged.connect(self._speed_changed)
        ct.addWidget(self.cmb_speed)

        self.chk_loop = QCheckBox('循环当前')
        ct.addWidget(self.chk_loop)
        ct.addStretch(1)
        self.btn_sound = QPushButton('🔇 声音')
        self.btn_sound.setCheckable(True)
        self.btn_sound.toggled.connect(self._toggle_sound)
        ct.addWidget(self.btn_sound)
        bl.addLayout(ct)

        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(0)
        wl.addWidget(central, 1)
        wl.addWidget(bottom)
        self.setCentralWidget(wrap)

        self.setStatusBar(QStatusBar())
        self.lbl_queues = QLabel('未看 0 ｜ 已缓存 0/%d ｜ 已看完 0 ｜ 缓存 —'
                                 % QM.MAX_CACHED_ITEMS)
        self.statusBar().addPermanentWidget(self.lbl_queues)
        self._say('就绪 —— 点「打开文件夹…」选择装有 .mcap 的目录')

    # ---------------------------------------------------------------- 定时器
    def _build_timer(self):
        self.timer = QTimer(self)
        # 30fps 素材在 2× 时每个刷新周期前进约一帧，避免固定隔帧显示。
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        # 拖动时把密集 valueChanged 合并成最多 10 次/秒的预览解码。
        # 松手后仍会执行一次精确 seek，因此不会损失定位精度。
        self.scrub_timer = QTimer(self)
        self.scrub_timer.setSingleShot(True)
        self.scrub_timer.setInterval(100)
        self.scrub_timer.timeout.connect(self._render_scrub_preview)

    #: P1.6E-R2：正式 Render FPS 上限（源 fps<=60 / 30 / 20 / 15）
    RENDER_FPS_CAP = {1.0: 60, 2.0: 30, 4.0: 20, 8.0: 15}

    def _render_cap_fps(self):
        """上限 render FPS：按倍速固定，1x 时再受素材帧率约束（异常一律兜底 30）"""
        try:
            cap = self.RENDER_FPS_CAP.get(float(self.speed), 15)
            if float(self.speed) <= 1.0:
                prim = self.primary_pane()
                if prim is not None and (prim.fps or 0) > 0:
                    cap = min(cap, prim.fps)
            return max(5, int(cap))
        except Exception:
            return 30

    def _apply_render_cap(self):
        """用 timer 间隔限制 UI render FPS；媒体时间由时钟独立推进"""
        if getattr(self, 'timer', None) is None:
            return
        self.timer.setInterval(max(16, int(1000.0 / self._render_cap_fps())))

    def _playback_lag(self):
        """target media time - 最新已解码帧的媒体时间（秒）。

        注意：stream.latest() 返回 (帧图像, 帧序号)；媒体时间要按
        帧序号去 pane.times 里取。本函数必须永不抛异常——它跑在播放主循环里，
        一旦抛出会打断 _tick，表现为「画面完全不动」。
        """
        try:
            prim = self.primary_pane()
            if prim is None or not getattr(prim, 'stream', None):
                return 0.0
            frame, idx = prim.stream.latest()
            times = getattr(prim, 'times', None)
            if frame is None or not times or idx is None:
                return 0.0
            if idx < 0 or idx >= len(times):
                return 0.0
            return max(0.0, self.t - float(times[int(idx)]))
        except Exception:
            return 0.0

    def _tick(self):
        if self._closing:
            return
        self.tick_n += 1
        if self._scrubbing:
            self._update_clock()
            return
        if self._direct_session is not None:
            # F-E2：Direct 帧来自后台 worker（单请求 + generation），
            # 这里只推进媒体时钟并把目标时间交给 session
            if self.playing and self.duration > 0:
                self.t = (getattr(self, '_direct_anchor_media_s', self.t_start)
                          + self.clock.elapsed() / 1000.0 * self.speed)
                if self.t >= self.duration:
                    self.t = self.duration
                    self._on_reach_end()
                    return
                self._direct_session.tick(self.t)
            self._update_clock()
            return
        if self.playing and self.duration > 0:
            self.t = (getattr(self, '_direct_anchor_media_s', self.t_start)
                          + self.clock.elapsed() / 1000.0 * self.speed)
            # P1.6E-R2：drift 统计 + 高倍速 keyframe-aware catch-up
            try:
                lag = self._playback_lag()
                self.pb['max_drift_ms'] = max(self.pb['max_drift_ms'], lag * 1000.0)
            except Exception:
                lag = 0.0
            # 高倍速不主动 seek 追赶（会让解码器反复重定位 → 画面卡死）；
            # 丢帧追赶由 pane.stream.catchup_limit 完成（>2x 时为 4），
            # 这里只累计「发生过多少次需要追赶」用于诊断。
            if self.speed >= 4.0 and lag > 1.0:
                self.pb['catchup_seek_count'] += 1
            self._lag_since = None
            if self.t >= self.duration:
                self.t = self.duration
                self._render_panes()
                self._on_reach_end()
                return
        self._render_panes()
        self._update_clock()
        self._tick_audio()

    def _render_panes(self):
        keys = self._active_keys()
        # P1.6E-R2：高倍速只解码「当前正在看的 camera」（不再两路轮流解码）
        if self.playing and self.speed > 2.0:
            pk = None
            for _k in (getattr(self, 'order', ()) or ()):
                if _k in self.panes:
                    pk = _k
                    break
            if pk is not None:
                keys = [k for k in keys if k == pk]
        # P1.6E-R2：高倍速下解码必然落后 —— 这里只是**跳帧**（显示最新已解码帧），
        # 绝不停止渲染（否则 8x 会画面冻结）。真正丢帧由 stream.catchup_limit 承担。
        if self.playing and self.speed >= 2.0 and self._playback_lag() > 0.5:
            self.pb['dropped'] += 1
        for k in keys:
            self.panes[k].refresh(self.t)
            self.pb['rendered'] += 1
        self.imu_chart.set_time(self.t)
        prim = self.primary_pane()
        if prim is not None:
            idx = pick_frame(prim.times, self.t)
            self.lbl_frame.setText('帧 %s / %d · %.2f fps · %g×' % (
                ('—' if idx < 0 else str(idx)), prim.frames, prim.fps or 0.0, self.speed))
        else:
            self.lbl_frame.setText('—')
        if self.imu_data:
            i = pick_frame(self.imu_data['t'], self.t)
            if i >= 0:
                av, la = self.imu_data['av'], self.imu_data['la']
                self.lbl_imu.setText(
                    'gyro %7.3f %7.3f %7.3f  |  acc %7.3f %7.3f %7.3f' % (
                        av[0][i], av[1][i], av[2][i], la[0][i], la[1][i], la[2][i]))

    def _update_clock(self):
        self.lbl_clock.setText('%s / %s' % (PL.human_time(self.t), PL.human_time(self.duration)))
        if not self.slider.isSliderDown():
            if self.duration > 0 and self.t == self.t:      # 排除 NaN
                ratio = max(0.0, min(1.0, self.t / self.duration))
            else:
                ratio = 0.0
            v = int(round(ratio * 1000))
            self.slider.blockSignals(True)
            self.slider.setValue(v)
            self.slider.blockSignals(False)
        if getattr(self, 'marker_bar', None) is not None:
            self._tick_marker_bar()

    def primary_pane(self):
        for k in self.order:
            if k in self.visible:
                return self.panes[k]
        return self.panes.get(self.order[0]) if self.order else None

    # ---------------------------------------------------------------- 时间轴
    def _slider_down(self):
        """拖动时间轴：先暂停，松手后如果原来在播就继续播"""
        self._was_playing = self.playing
        self._scrubbing = True
        self.pause()

    def _slider_up(self):
        self._scrubbing = False
        self.scrub_timer.stop()
        self.seek(self.duration * self.slider.value() / 1000.0)
        if getattr(self, '_was_playing', False):
            self.play()

    def _slider_moved(self, v):
        if self.slider.isSliderDown():
            self.t = max(0.0, min(self.duration, self.duration * v / 1000.0))
            self._update_clock()
            self.imu_chart.set_time(self.t)
            if not self.scrub_timer.isActive():
                self.scrub_timer.start()

    def _render_scrub_preview(self):
        if self._scrubbing and not self._closing:
            self._render_panes()

    # ---------------------------------------------------------------- 文件夹
    def choose_folder(self):
        start = self.folder or str(self.settings.value('last_dir', os.path.expanduser('~')))
        d = QFileDialog.getExistingDirectory(self, '选择包含 .mcap / .mp4 文件的文件夹', start)
        if d:
            self.load_folder(d)

    def choose_file(self):
        start = self.folder or str(self.settings.value('last_dir', os.path.expanduser('~')))
        f, _ = QFileDialog.getOpenFileName(
            self, '选择视频文件', start,
            '视频文件 (*.mcap *.mp4);;MCAP 录像 (*.mcap);;'
            'MP4 视频 (*.mp4);;所有文件 (*.*)')
        if f:
            self.load_folder(os.path.dirname(f), select=os.path.abspath(f))

    def load_folder(self, folder, select=None, autoplay=True):
        if not folder or not os.path.isdir(folder):
            return
        self.folder = os.path.abspath(folder)
        self.settings.setValue('last_dir', self.folder)
        self._prompted_folder = None      # 新文件夹重新允许「全部看完」询问
        self.items = PL.scan_folder(self.folder)
        # 记住文件夹类型（清空队列后条目为空，仍要按原类型显示文案）
        self._mode_cache = ('mp4' if any(PL.is_direct_format(it['name'])
                                        for it in self.items) else 'mcap')
        self.lbl_folder.setText('%s\n共 %d 个视频文件' % (self.folder, len(self.items)))
        self.btn_refresh.setEnabled(True)
        if not self.items:
            self.sids = []
            self._refresh_queues()
            # P1.6E-R1：正常事件 0 弹窗，只走状态栏
            self._say('这个文件夹里没有找到 .mcap 录像或 .mp4 视频', 8000)
            return
        # 全部新文件进入三队列；QM 负责对账、持久化和按需补缓存
        self.sids = []
        paths = []
        for it in self.items:
            try:
                sid = self.qm.sid_of(it['path'])
            except OSError:
                sid = None
            self.sids.append(sid)
            if sid:
                paths.append(it['path'])
        self.qm.load_folder(self.folder, paths)
        sid = None
        if select:
            for i, it in enumerate(self.items):
                if appcache.same_path(it['path'], select):
                    sid = self.sids[i]
                    break
        if autoplay:
            if sid is None:
                sid = self.sids[0]
            if sid:
                self.play_sid(sid, autoplay=True)
        elif self.folder_mode() == 'mp4':
            self._say('%d 个本地视频：可直接播放（不写入缓存）；'
                      '按两下 X 标注不合格片段' % len(self.items), 15000)
        else:
            self._say('已加载 %d 个文件；双击任一条目开始播放' % len(self.items))

    def step_file(self, delta):
        if not self.items:
            self._say('请先打开一个文件夹')
            return
        cur = self.sids[self.index] if 0 <= self.index < len(self.sids) else None
        target = self.qm.next_playable_sid(cur, delta)
        if target is None:
            self._say('这个方向上没有可播放的文件了')
            return
        # 目标是已缓存 → 直接播；是未看 → 缓存完成后自动播；已看完项会被跳过
        self.play_sid(target, autoplay=True)

    def _update_nav(self):
        has = bool(self.items)
        cur = self.sids[self.index] if 0 <= self.index < len(self.sids) else None
        prev_ok = has and self.qm.next_playable_sid(cur, -1) is not None
        next_ok = has and self.qm.next_playable_sid(cur, 1) is not None
        self.act_prev.setEnabled(prev_ok)
        self.act_next.setEnabled(next_ok)
        self.btn_prev_f.setEnabled(prev_ok)
        self.btn_next_f.setEnabled(next_ok)
        if has and 0 <= self.index < len(self.items):
            self._say('第 %d / %d 个文件 · %s' % (
                self.index + 1, len(self.items), self.items[self.index]['name']))

    # ---------------------------------------------------------------- 打开文件
    def play_sid(self, sid, autoplay=True):
        """播放入口：已缓存→直接开；未缓存→点名缓存，就绪后自动播放"""
        qit = self.qm.items.get(sid) if sid else None
        if qit is None:
            return
        if qit['state'] in (QM.CACHED, QM.PLAYING):
            idx = self.sids.index(sid) if sid in self.sids else -1
            self.open_index(idx, autoplay=autoplay)
            return
        if qit['state'] == QM.WATCHED:
            self._say('该文件已看完，缓存已删除；点该行的「重新缓存」可再次观看')
            return
        # F-E2：未缓存 Indexed MCAP → 先尝试 Direct 即时播放（失败回落旧缓存流程）
        if self._maybe_open_direct(qit, sid, autoplay=autoplay):
            return
        # UNWATCHED / CACHING / ERROR：进缓存流程，就绪后 currentReady 接管
        self._pending_play_sid = sid
        if self.qm.request_play(sid) is False:
            self._pending_play_sid = None
            return
        self._show_cache_dialog(qit['name'])

    # ------------------------------------------------------- F-E2 Direct 接线
    def _maybe_open_direct(self, qit, sid, autoplay=True):
        """未缓存 Indexed MCAP → Direct 即时播放；不命中则返回 False 走旧流程"""
        path = (qit or {}).get('path') or ''
        if not path.lower().endswith('.mcap'):
            return False
        try:
            kind, _reason = self._DP.select_backend(path)
        except Exception:
            return False
        if kind != 'direct':
            return False
        self._open_direct_session(path, sid, autoplay=autoplay)
        return True

    def _direct_session_manifest(self, path, duration=0.0):
        """为 Direct 会话构造 UI 骨架 manifest（无 stream/无 times，仅用于建 pane）"""
        try:
            st = os.stat(path)
            size = st.st_size
        except OSError:
            size = 0
        cams = []
        for key, topic in (('camera2', '/robot0/sensor/camera2/compressed'),
                           ('camera3', '/robot0/sensor/camera3/compressed')):
            cams.append(dict(key=key, topic=topic, playable=True, primary=True,
                             frames=0, fps=0.0, start_offset_s=0.0,
                             file=None, times_file=None, direct=True))
        return dict(cache_profile='direct', duration_s=float(duration),
                    cameras=cams, audio=None, imu=None,
                    summary=dict(name=os.path.basename(path), size=size,
                                 message_count='—'),
                    source=path, direct_session=True)

    def _open_direct_session(self, path, sid, autoplay=True):
        self._close_direct_session()
        self._teardown()
        self._direct_ttpf_t0 = time.perf_counter()
        self._direct_ttpf_ms = None
        self._direct_autoplay = bool(autoplay)
        self._direct_sid = sid
        self._playback_backend = 'MCAP_DIRECT_OPENING'
        self._direct_anchor_pending = True
        self._say('正在打开视频…')
        ses = self._DP.DirectPlaybackSession(path)
        self._direct_session = ses
        ses.opened.connect(self._on_direct_opened)
        ses.frameReady.connect(self._on_direct_frame)
        ses.failed.connect(self._on_direct_failed)
        ses.open_async()

    def _on_direct_opened(self, backend, ttfp_ms):
        ses = self._direct_session
        if ses is None:
            return
        dur = ses.duration()
        self.fid = appcache.file_id(ses.mcap_path)
        self._apply(self._direct_session_manifest(ses.mcap_path, dur), cached=True)
        self._playback_backend = 'MCAP_DIRECT'
        self._say('正在播放（直接播放 · 打开耗时 %.0f ms）' % ttfp_ms)

    def _on_direct_frame(self, frame):
        ses = self._direct_session
        if ses is None or frame is None:
            return                       # 双保险：旧 session 的迟到帧直接丢弃
        pane = self.panes.get(frame.camera)
        if pane is not None and getattr(pane, 'view', None) is not None:
            pane.view.set_image(frame.image)
        # 只在需要时锚定时钟（首帧 / seek / 切相机后的第一帧）。
        # 若每帧都重设 t 并 restart clock，媒体时钟会被反复拉回，
        # 实测会把 1x 拖慢到约 0.5x。
        if getattr(self, '_direct_anchor_pending', False):
            self._direct_anchor_media_s = float(frame.media_time)
            self.t = float(frame.media_time)
            self.t_start = self.t
            self.clock.restart()
            self._direct_anchor_count = getattr(self, '_direct_anchor_count', 0) + 1
            self._direct_anchor_pending = False
        if self._direct_ttpf_t0 is not None:
            self._direct_ttpf_ms = (time.perf_counter()
                                    - self._direct_ttpf_t0) * 1000.0
            self._direct_ttpf_t0 = None
        if self._direct_autoplay and not self.playing:
            if not self.clock.isValid():
                self.clock.restart()
            self.playing = True
            self.btn_play.setText('⏸  暂停')

    def _on_direct_failed(self, reason):
        """Direct 失败 → 保存续播时间点，回落既有缓存流程（0 Modal）"""
        self._say('直接播放不可用，正在使用兼容模式…', 12000)
        ses = self._direct_session
        self._pending_resume_time = getattr(ses, 'resume_time', None)
        sid = self._direct_sid
        self._close_direct_session()
        self._playback_backend = 'CACHE_REQUIRED'
        if sid and sid in self.qm.items and self.qm.items[sid]['state'] != QM.WATCHED:
            self._pending_play_sid = sid
            if self.qm.request_play(sid) is not False:
                self._show_cache_dialog(self.qm.items[sid]['name'])

    def _close_direct_session(self):
        ses = self._direct_session
        self._direct_session = None
        self._direct_sid = None
        if ses is not None:
            try:
                ses.close()
            except Exception:
                pass
        if self._playback_backend not in ('CACHE_REQUIRED',):
            self._playback_backend = 'NONE'

    def open_index(self, idx, autoplay=True):
        """打开一个已有有效缓存的文件（未缓存的会被转回 play_sid 流程）"""
        if self.loading:
            self._say('正在解析上一个文件，请稍候…')
            return
        if not (0 <= idx < len(self.items)):
            return
        it = self.items[idx]
        sid = self.sids[idx] if idx < len(self.sids) else None
        qit = self.qm.items.get(sid) if sid else None
        fid = appcache.file_id(it['path'])
        outdir = cache_outdir(fid)
        if PL.is_direct_format(it['name']):
            # MP4 等直读文件：不需要缓存封装，直接读原文件
            man = self._direct_manifest(it)
            if man is None:
                self._say('这个文件无法解码（缺 opencv 或文件损坏）：%s'
                          % it['name'], 15000)
                return
        else:
            man = PREP.load_manifest(outdir, source_path=it['path'])
            if qit is not None and man is None \
                    and qit['state'] not in (QM.CACHED, QM.PLAYING):
                # 缓存失效/不存在：统一走三队列的缓存流程
                self.play_sid(sid, autoplay=autoplay)
                return
        self._teardown()
        self.index = idx
        self._update_nav()
        self.fid = fid
        self.lbl_title.setText('%d/%d · %s' % (idx + 1, len(self.items), it['name']))
        self.setWindowTitle('%s —— %s' % (APP_NAME, it['name']))
        self.tabs.setCurrentIndex(1)          # 正在播放的项在「已缓存」页
        if sid:
            self.qm.set_current(sid)

        if man is not None:
            self._apply(man, cached=True)
            if autoplay:
                QTimer.singleShot(320, self.play)
            return

        # 兜底：状态机认为有缓存但校验失败（外部改动等）→ 重新排队缓存
        if not HAS_CV:
            # P1.6E-R1：可恢复错误不弹窗，改状态栏
            self._say('缺少 opencv-python，无法解码视频；请运行：pip install opencv-python',
                      15000)
            return
            return
        self._pending_play_sid = sid
        if self.qm.request_play(sid) is False:
            self._pending_play_sid = None
            return
        self._show_cache_dialog(it['name'])

    def _show_cache_dialog(self, name):
        self.loading = True
        self._say('正在缓存 %s …' % name)
        dlg = QProgressDialog('正在缓存（只封装视角 2/3）…', '取消', 0, 1000, self)
        dlg.setWindowTitle(APP_NAME)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumWidth(480)
        dlg.setMinimumDuration(0)
        dlg.setValue(1)
        dlg.canceled.connect(self._cancel_pending_cache)
        self._dlg = dlg

    def _cancel_pending_cache(self):
        """进度框「取消」：停掉点名缓存，项目回到未看且不自动重试"""
        sid = self._pending_play_sid
        self._pending_play_sid = None
        self.loading = False
        if self._dlg:
            self._dlg.close()
            self._dlg = None
        if sid and sid != '__next__':
            self.qm.cancel_cache(sid)

    def _on_qm_error(self, sid, error):
        """缓存失败：若正在等它播放，立即解锁界面；详情走非模态弹窗"""
        if sid == self._pending_play_sid:
            self._pending_play_sid = None
            self.loading = False
            if self._dlg:
                self._dlg.close()
                self._dlg = None
            self._say('缓存失败：%s' % (error or ''), 10000)
            box = QMessageBox(QMessageBox.Warning, '缓存失败',
                              error or '缓存失败，可稍后在「未看」队列点「重试缓存」。',
                              QMessageBox.Ok, self)
            box.setModal(False)
            box.show()
            self._err_box = box

    def _on_qm_progress(self, sid, p, msg):
        if sid == self._pending_play_sid or self._pending_play_sid == '__next__':
            self._on_progress(p, msg)
        elif self._pending_play_sid is None and self._dlg is None:
            it = self.qm.items.get(sid) or {}
            self._say('正在缓存 %s · %d%%' % (
                it.get('name', sid), round((p or 0) * 100)), 1500)

    def _on_qm_ready(self, sid):
        if self._pending_play_sid in (sid, '__next__'):
            self._pending_play_sid = None
            if self._dlg:
                self._dlg.close()
                self._dlg = None
            self.loading = False
            if sid in self.sids:
                self.open_index(self.sids.index(sid), autoplay=True)

    def _before_cache_delete(self, sid):
        """QM 删缓存前的回调：如果删的正是当前打开的文件，先停流释放句柄。

        任何删除路径（看完、腾槽位、清空）都会走这里，保证「清空」不会
        因为文件被占用而变成假清空。
        """
        cur = None
        if 0 <= self.index < len(self.sids):
            cur = self.sids[self.index]
        if sid != cur and sid != self.qm.current_sid:
            return
        streams = [p.stream for p in self.panes.values() if p.stream]
        self._teardown()
        self.qm.set_current(None)
        deadline = time.time() + 4.0
        while time.time() < deadline and any(s.isRunning() for s in streams):
            QApplication.processEvents()
            time.sleep(0.01)

    def _on_qm_deleted(self, sid, freed):
        it = self.qm.items.get(sid) or {}
        self._say('已释放 %s，%s' % (
            it.get('name', sid), PL.human_size(freed)))

    def finish_current_video(self, reason):
        """三、看完：natural_end=自然播完 / manual=手动标记。重复调用幂等。

        拖动到结尾、End 键、逐帧到结尾都不会走到这里——只有正常播放时
        主时钟真正越过结尾（_on_reach_end）或用户点按钮才算。
        """
        if not self.sids or not (0 <= self.index < len(self.sids)):
            return
        sid = self.sids[self.index]
        qit = self.qm.items.get(sid)
        if qit is None or qit['state'] in (QM.WATCHED, QM.CLEANUP_PENDING) \
                or qit.get('cleanup_pending'):
            return
        finished_t = self.t              # 先取位置再清场，否则永远保存 0
        finished_duration = self.duration
        # 兜底：只按了一下 X 就结束观看时，把未闭合的起点闭合到当前位置，
        # 不让标注员漏标一段
        segs, pending = self.qm.markers_of(sid)
        if pending is not None:
            segs, _p, closed = MK.add_point(segs, pending, finished_t,
                                            finished_duration)
            self.qm.update_markers(sid, segs, None)
            if closed:
                self._say('已自动闭合未结束的不合格标注：%.3f ~ %.3f 秒'
                          % closed, 8000)
        streams = [p.stream for p in self.panes.values() if p.stream]
        self._teardown()
        # 删除缓存前必须等解码线程真正退出，否则 Windows 句柄占用会删除失败
        deadline = time.time() + 4.0
        while time.time() < deadline and any(s.isRunning() for s in streams):
            QApplication.processEvents()
            time.sleep(0.01)
        self.qm.set_current(None)
        self.qm.update_position(sid, finished_t)
        self.qm.mark_watched(sid, reason, finished_duration)
        self._show_empty_state('已看完\n\n缓存已释放\n\n可在左侧「已看完」队列点击'
                               '「重新缓存」再次观看')
        if self.qm.folder_all_watched():
            # 全看完：先出报告 + 询问是否清空/选下一个文件夹（不再自动跳下一个）
            self._on_folder_complete()
            return
        if self.chk_auto.isChecked():
            nxt = self.qm.next_cached_sid()
            if nxt:
                self._say('已看完，自动切到下一个 …')
                QTimer.singleShot(300, lambda: self.play_sid(nxt))
            elif self.qm.has_unwatched():
                self._pending_play_sid = '__next__'
                self._say('已看完。正在缓存下一项，完成后自动播放…', 8000)
            else:
                self._say('这个文件夹全部看完了', 12000)

    # ---------------------------------------------------------------- 不合格标注
    def _current_sid_for_mark(self):
        if not self.sids or not (0 <= self.index < len(self.sids)):
            return None
        return self.sids[self.index]

    def _refresh_marker_ui(self):
        sid = self._current_sid_for_mark()
        segs, pending = self.qm.markers_of(sid) if sid else ([], None)
        norm = MK.normalize_segments(segs, self.duration or 0.0)
        self._bad_norm = norm
        self._bad_pending = pending
        total = sum(b - a for a, b in norm)
        txt = '不合格 %d 段 · %.1f 秒' % (len(norm), total)
        if pending is not None:
            txt += '（起点 %.2fs 待闭合）' % pending
        if txt != self.lbl_bad.text():
            self.lbl_bad.setText(txt)
        self.btn_bad_undo.setEnabled(bool(norm))
        self.btn_bad_manage.setEnabled(bool(norm) or pending is not None)
        self._tick_marker_bar()

    def _tick_marker_bar(self):
        self.marker_bar.set_data(self.duration or 0.0,
                                 getattr(self, '_bad_norm', []),
                                 getattr(self, '_bad_pending', None), self.t)

    def _mark_bad_point(self):
        """按一下 X（或点按钮）：第一下记起点，第二下闭合成不合格片段"""
        sid = self._current_sid_for_mark()
        if sid is None:
            self._say('先打开一个视频再标不合格')
            return
        segs, pending = self.qm.markers_of(sid)
        new_segs, new_pending, closed = MK.add_point(
            segs, pending, self.t, self.duration)
        self.qm.update_markers(sid, new_segs, new_pending)
        if closed:
            self._say('已标不合格：%.3f ~ %.3f 秒（共 %.2f 秒）'
                      % (closed[0], closed[1], closed[1] - closed[0]), 6000)
        elif new_pending is not None:
            self._say('片段起点 %.3f 秒 —— 再按一下 X 结束这一段' % new_pending, 6000)
        else:
            self._say('两下 X 位置太近（不足 1 毫秒），这一段已忽略', 6000)
        self._refresh_marker_ui()

    def _undo_bad_mark(self):
        sid = self._current_sid_for_mark()
        if sid is None:
            return
        segs, _pending = self.qm.markers_of(sid)
        if not segs:
            self._say('这个视频还没有标注过不合格片段')
            return
        segs = list(segs)[:-1]
        self.qm.update_markers(sid, segs, None)
        self._say('已撤销最后一段；本视频还剩 %d 段不合格' % len(segs), 6000)
        self._refresh_marker_ui()

    def clear_bad_marks(self):
        """清掉本视频的全部标注"""
        sid = self._current_sid_for_mark()
        if sid is None:
            return
        segs, _pending = self.qm.markers_of(sid)
        if not segs and self._bad_pending is None:
            self._say('这个视频还没有标注过不合格片段')
            return
        self.qm.update_markers(sid, [], None)
        self._refresh_marker_ui()
        self._say('已清空本视频的不合格标注')

    def _delete_bad_segment(self, sid, index):
        """删除某段不合格标注（标注管理弹窗与测试都走这里）"""
        segs, _pending = self.qm.markers_of(sid)
        norm = MK.normalize_segments(segs, self.duration or 0.0)
        if not (0 <= index < len(norm)):
            return False
        new_segs = MK.remove_segment(segs, index)
        self.qm.update_markers(sid, new_segs, '__keep__')
        self._refresh_marker_ui()
        return True

    def _manage_marks(self):
        """标注管理弹窗：查看每一段、定位复核、逐段删除、清空本视频"""
        sid = self._current_sid_for_mark()
        if sid is None:
            self._say('先打开一个视频再管理标注')
            return
        it = self.qm.items.get(sid) or {}
        dlg = QDialog(self)
        dlg.setWindowTitle('不合格标注管理 —— %s' % it.get('name', ''))
        dlg.resize(600, 420)
        v = QVBoxLayout(dlg)
        hint = QLabel('进度条下方的红色片段就是这里列出的不合格区间；\n'
                      '「定位」会把播放位置跳到该片段起点，方便复核后再决定是否删除。')
        hint.setWordWrap(True)
        hint.setObjectName('dim')
        v.addWidget(hint)
        lst = QListWidget()
        v.addWidget(lst, 1)

        def reload_list():
            lst.clear()
            segs, pending = self.qm.markers_of(sid)
            norm = MK.normalize_segments(segs, self.duration or 0.0)
            for i, (a, b) in enumerate(norm):
                li = QListWidgetItem('第 %d 段   %s ~ %s   （%.3f 秒）' % (
                    i + 1, MK.fmt_hms(a), MK.fmt_hms(b), b - a))
                li.setData(Qt.UserRole, i)
                lst.addItem(li)
            if pending is not None:
                li = QListWidgetItem('（未闭合的起点：%s —— 播放中再按一次 X 即可闭合）'
                                     % MK.fmt_hms(pending))
                li.setData(Qt.UserRole, None)
                lst.addItem(li)
            if not lst.count():
                li = QListWidgetItem('本视频还没有不合格标注')
                li.setData(Qt.UserRole, None)
                lst.addItem(li)

        def selected_index():
            cur = lst.currentItem()
            if cur is None:
                return None
            return cur.data(Qt.UserRole)

        def do_locate():
            idx = selected_index()
            if idx is None:
                self._say('先在列表里选中一段')
                return
            segs, _p = self.qm.markers_of(sid)
            norm = MK.normalize_segments(segs, self.duration or 0.0)
            if 0 <= idx < len(norm):
                self.seek(norm[idx][0])
                self._say('已跳到第 %d 段起点 %s' % (idx + 1,
                                                MK.fmt_hms(norm[idx][0])))

        def do_delete():
            idx = selected_index()
            if idx is None:
                self._say('先在列表里选中要删除的一段')
                return
            segs, _p = self.qm.markers_of(sid)
            norm = MK.normalize_segments(segs, self.duration or 0.0)
            if self._delete_bad_segment(sid, idx):
                reload_list()
                self._say('已删除第 %d 段；本视频还剩 %d 段不合格'
                          % (idx + 1, max(0, len(norm) - 1)), 6000)

        def do_clear():
            if not self.qm.markers_of(sid)[0] and self._bad_pending is None:
                return
            r = QMessageBox.question(dlg, APP_NAME,
                                     '清空本视频（%s）的全部不合格标注？'
                                     % it.get('name', ''))
            if r != QMessageBox.Yes:
                return
            self.qm.update_markers(sid, [], None)
            self._refresh_marker_ui()
            reload_list()
            self._say('已清空本视频的不合格标注')

        row = QHBoxLayout()
        b_locate = QPushButton('定位到选中片段')
        b_locate.clicked.connect(do_locate)
        b_del = QPushButton('删除选中片段')
        b_del.clicked.connect(do_delete)
        b_clear = QPushButton('清空本视频全部标注')
        b_clear.clicked.connect(do_clear)
        row.addWidget(b_locate)
        row.addWidget(b_del)
        row.addWidget(b_clear)
        row.addStretch(1)
        v.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        bb.clicked.connect(lambda _b: dlg.accept())
        v.addWidget(bb)
        reload_list()
        lst.itemDoubleClicked.connect(lambda _i: do_locate())
        dlg.exec()
        self._refresh_marker_ui()

    # ---------------------------------------------------------------- 报告与清空
    def _generate_report(self):
        """生成定位合格率报告；返回写入路径（失败返回 ''）"""
        try:
            summary = MK.summarize(self.qm.summary_items(), self.folder)
            path = MK.write_report(self.folder, summary,
                                   fallback_dir=appcache.user_data_dir())
            self._say('定位合格率报告已生成：%s（合格率 %.2f%%）'
                      % (path, summary['rate']), 20000)
            self._last_report_path = path
            return path
        except Exception as exc:
            self._say('报告生成失败：%s' % exc, 15000)
            return ''

    def _on_folder_complete(self):
        """该文件夹全部看完：生成报告，并询问是否清空队列与缓存、选下一个文件夹"""
        if not self.folder or self.qm is None or not self.qm.order:
            return
        if not self.qm.folder_all_watched():
            return
        if self._prompted_folder == self.folder:
            return
        self._prompted_folder = self.folder
        path = self._generate_report()
        summary = MK.summarize(self.qm.summary_items(), self.folder)
        # P1.6E-R1：正常事件 0 弹窗 —— 默认实现不询问、不自动清空，只走状态栏；
        # 需要清空时由用户主动点「清空队列与缓存」（那里保留一次确认）。
        if self._confirm_folder_complete(summary, path):
            self.clear_queues_and_cache(ask=False, then_choose=True)
        else:
            self._say('本文件夹 %d 个视频已全部看完 · 定位合格率 %.2f%% · 报告：%s；'
                      '需要清空时点左侧「清空队列与缓存」'
                      % (summary['count'], summary['rate'],
                         os.path.basename(path) if path else '生成失败'), 20000)

    def _confirm_folder_complete(self, summary, path):
        """P1.6E-R1：文件夹全部看完**不再弹窗**（正常事件 0 modal）。

        返回 False = 不自动清空（用户需要时自己点「清空队列与缓存」，那里保留确认）。
        测试可覆盖本方法确认无 QMessageBox.exec 调用。
        """
        return False

    def clear_queues_and_cache(self, ask=True, then_choose=False):
        """清空三个队列（含已看完记录）与所有桌面缓存；原始 .mcap 不动"""
        if self.qm is None or not self.qm.order:
            self._say('还没有可清空的队列')
            return
        if ask:
            if self.folder_mode() == 'mp4':
                text = ('清空三个队列（含已看完记录）？\n\n'
                        '本地视频是直接播放的，不产生缓存，原文件不会被动到。')
            else:
                text = ('清空三个队列（含已看完记录）并删除所有桌面缓存？\n\n'
                        '原始 .mcap 文件不会被删除，之后仍可重新缓存观看。')
            r = QMessageBox.question(self, APP_NAME, text,
                                     QMessageBox.Yes | QMessageBox.No,
                                     QMessageBox.No)
            if r != QMessageBox.Yes:
                self._say('已取消清空')
                return
        # 先停播放并清空当前项，让「正在播放不删缓存」的保护不拦清空
        self._teardown()
        self.qm.set_current(None)
        res = self.qm.clear_all()
        if self.folder_mode() == 'mp4':
            msg = '已清空队列：%d 个本地视频已从队列移除（原文件保留）' % len(self.items)
        else:
            msg = '已清空队列与缓存：删除 %d 项缓存' % len(res['deleted'])
            if res['failed']:
                msg += '，%d 项删除失败（正在被占用，稍后可再点一次）' % len(res['failed'])
        # 队列已空：本地也卸载，避免「当前文件 / 上一个 / 下一个」还指着已清空的项
        self.items = []
        self.sids = []
        self.index = -1
        self.fid = None
        self.lbl_title.setText('未打开文件')
        self._fname_label.setText_full('—')
        for v in self.finfo.values():
            v.setText('—')
        self._details = None
        self._refresh_queues()
        self._update_nav()
        self._say(msg, 15000)
        self._show_empty_state('队列已清空（缓存已全部删除）\n\n'
                               '点左侧「重新扫描文件夹」可重新载入这批视频')
        self._prompted_folder = None
        if then_choose:
            self.choose_folder()

    def _on_progress(self, p, msg):
        # setValue() 可能处理嵌套事件并触发对话框关闭；使用局部引用且每步确认，
        # 避免关闭窗口/取消解析时出现 NoneType 竞态。
        dlg = self._dlg
        if dlg is not None:
            dlg.setValue(int(max(0.0, min(1.0, p)) * 1000))
            if self._dlg is dlg:
                dlg.setLabelText(msg)

    # ---------------------------------------------------------------- 数据应用
    def _teardown(self):
        self._close_direct_session()
        """彻底清场：停流、清网格、清信息。看完后绝不残留最后一帧，
        避免用户误以为缓存仍可播放。"""
        self.playing = False
        self.btn_play.setText('▶  播放')
        for p in self.panes.values():
            if p.stream:
                p.stream.request_stop()
        for p in self.panes.values():
            p.stop()
        while self.grid.count():
            it = self.grid.takeAt(0)
            wid = it.widget()
            if wid is not None and wid is not getattr(self, 'empty_hint', None):
                wid.setParent(None)
                wid.deleteLater()
        self.panes.clear()
        self.order.clear()
        self.visible.clear()
        self.solo_key = None
        self.audio.stop()
        self.audio.close()
        self.audio_started = False
        self.imu_data = None
        self.imu_chart.clear()
        self.imu_box.setVisible(False)
        self.lbl_imu.setText('—')
        self.t = 0.0
        self.duration = 0.0
        self.manifest = None
        self.slider.setValue(0)
        self.lbl_frame.setText('—')
        self.lbl_clock.setText('— / —')
        self.lbl_title.setText('未打开文件')
        self._fname_label.setText_full('—')
        for v in self.finfo.values():
            v.setText('—')
        self._details = None
        self._bad_norm = []
        self._bad_pending = None
        if getattr(self, 'marker_bar', None) is not None:
            self.marker_bar.set_data(0.0, [], None, 0.0)
            self.lbl_bad.setText('不合格 0 段 · 0.0 秒')
            self.btn_bad_undo.setEnabled(False)
            self.btn_bad_manage.setEnabled(False)

    def _show_empty_state(self, text):
        """在右侧画面区显示居中空状态提示"""
        if getattr(self, 'empty_hint', None) is None:
            self.empty_hint = QLabel(self.grid_host)
            self.empty_hint.setObjectName('dim')
            self.empty_hint.setAlignment(Qt.AlignCenter)
            self.empty_hint.setStyleSheet(
                'color:#8b95a7;font-size:16px;background:transparent;')
        self.empty_hint.setText(text)
        self.empty_hint.setParent(self.grid_host)
        self.empty_hint.setGeometry(self.grid_host.rect())
        self.empty_hint.raise_()
        self.empty_hint.show()

    def _hide_empty_state(self):
        if getattr(self, 'empty_hint', None) is not None:
            self.empty_hint.hide()

    def _direct_manifest(self, item):
        """把可直读的视频文件（MP4 等）包装成与缓存 manifest 同构的结构。

        小技巧：cam['file'] 直接放绝对路径——_build_panes 里
        os.path.join(outdir, file) 遇到绝对路径会原样返回，因此播放链路零改动。
        """
        info = PL.probe_mp4(item['path'])
        if info is None:
            return None
        return dict(
            cache_profile='direct',
            source=item['path'],
            duration_s=float(info['duration_s']),
            cameras=[dict(
                key=os.path.splitext(item['name'])[0] or 'video',
                topic='', kind='mp4', playable=True, primary=True,
                file=item['path'], fps=float(info['fps']),
                frames=int(info['frames']), times_file=None,
                start_offset_s=0.0, width=info.get('width'),
                height=info.get('height'), warnings=[])],
            imu=None, audio=None,
            summary=dict(name=item['name'], size=item['size'],
                         message_count='—', path=item['path']),
            notes=['本地视频文件：直接读取原文件，不写入缓存；音频暂不播放'])

    def _apply(self, man, cached=False):
        self.manifest = man
        self.duration = float(man.get('duration_s') or man.get('duration') or 0.0)
        self.t = 0.0
        a = man.get('audio') or {}
        self.audio_offset = float(a.get('start_offset_s') or 0.0)
        self.audio_started = False
        self._build_panes(man.get('cameras') or [])
        self._load_audio(man)
        self._load_imu(man)
        self._fill_info(man, cached)
        self.slider.setValue(0)
        self._update_clock()
        tag = ('（本地文件直读）' if man.get('cache_profile') == 'direct'
               else ('（缓存）' if cached else '（新解析）'))
        self._say('%s 已就绪%s —— %d 路相机 · %.3f 秒' % (
            (man.get('summary') or {}).get('name', ''), tag,
            len(self.order), self.duration))

    def _fill_info(self, man, cached):
        """「当前文件」区只常驻 5 行；其余信息存入 _details，供详细信息弹窗显示"""
        s = man.get('summary') or {}
        direct = (man.get('cache_profile') == 'direct')
        self._fname_label.setText_full(s.get('name', '—'))
        self.finfo['时长'].setText('%.3f 秒' % self.duration)
        self.finfo['当前状态'].setText(
            '已就绪（本地文件直读）' if direct
            else '已就绪（%s）' % ('命中缓存，秒开' if cached else '新解析'))
        used = appcache.cache_size(self.fid) if self.fid else 0
        self.finfo['缓存大小'].setText(
            '—（直读）' if direct
            else (PL.human_size(used) if used else '—'))
        cams = man.get('cameras') or []
        okc = [c for c in cams if c.get('playable')]
        self.finfo['视角'].setText(
            '本地视频 1 路' if direct else 'camera2/3 可播 %d 路' % len(self.order))
        notes = []
        for c in cams:
            if not c.get('playable'):
                notes.append('%s：%s' % (c['key'], c.get('error') or '不可播放'))
        imu = man.get('imu')
        a = man.get('audio')
        if a and a.get('note'):
            notes.append(a['note'])
        if imu and imu.get('note'):
            notes.append(imu['note'])
        for c in cams:
            if c.get('warnings'):
                notes.append('%s：%s' % (c['key'], c['warnings'][0]))
        # 详细信息（弹窗内容，随文件更新）
        self._details = [
            ('文件名', s.get('name', '—')),
            ('源路径', man.get('source') or '—'),
            ('大小', PL.human_size(s.get('size', 0))),
            ('时长', '%.3f 秒' % self.duration),
            ('消息数', str(s.get('message_count', '—'))),
            ('IMU', ('%d 采样%s' % (imu['count'],
                     '（多通道，取第一路）' if imu.get('other_channels') else ''))
             if imu else '无'),
            ('音频', ('%d Hz / %dch / %d 块%s' % (
                a['sample_rate'], a['channels'], a['chunks'],
                '（多通道，取第一路）' if a.get('other_channels') else ''))
             if a else '无'),
            ('视角', '%d 路（桌面仅显示视角 2/3：%d 路）' % (len(cams), len(self.order))),
            ('缓存策略', '不适用（直读本地文件）' if direct else
             '三队列管理（未看/已缓存/已看完）· 最多保留 %d 个 · 精简缓存 %s'
             % (QM.MAX_CACHED_ITEMS, appcache.CACHE_PROFILE)),
            ('当前缓存', '不写入缓存（直读原文件）' if direct else
             '%s · %s' % ('命中缓存，秒开' if cached else '新解析并写入缓存',
                          man.get('cache_profile') or 'full')),
            ('本文件缓存', PL.human_size(used) if used else '—'),
            ('通道说明', '；'.join(notes) if notes else '正常'),
        ]
        self._refresh_marker_ui()

    def _show_details(self):
        if not getattr(self, '_details', None):
            self._say('还没有打开文件')
            return
        d = QDialog(self)
        d.setWindowTitle('详细信息')
        d.resize(640, 480)
        v = QVBoxLayout(d)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        for k, val in self._details:
            lbl = QLabel(str(val))
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lbl.setWordWrap(True)
            form.addRow(k, lbl)
        v.addLayout(form)
        v.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok)
        bb.accepted.connect(d.accept)
        v.addWidget(bb)
        d.exec()

    def _build_panes(self, cams):
        self._hide_empty_state()
        while self.grid.count():
            it = self.grid.takeAt(0)
            wid = it.widget()
            if wid:
                wid.setParent(None)
                wid.deleteLater()
        outdir = cache_outdir(self.fid) if self.fid else ''
        cams = [cam for cam in cams
                if is_primary_view(cam) or cam.get('primary')]
        for i, cam in enumerate(cams):
            if not cam.get('playable'):
                continue
            times = self._load_times(outdir, cam)
            pane = VideoPane(cam, COLORS[i % len(COLORS)], times, self)
            pane.doubleClicked.connect(self.toggle_solo)
            pane.roiChanged.connect(self._on_pane_roi)
            pane.setParent(self.grid_host)
            # 参考框按相机 key 保存，换文件后同一台设备的同一路会自动沿用
            roi = self.rois.get(cam['key'])
            if roi is None:
                roi = default_roi_for_camera(cam['key'])
                if roi is not None:
                    # 首次打开即有框；保存后导出、换文件和下次启动都使用同一坐标。
                    self.rois[cam['key']] = list(roi)
            pane.set_roi(roi)
            pane.set_show_roi(self.show_roi)
            pane.set_calibrating(self.calibrating)
            pane.set_roi_shape(self.roi_shape)
            pane.set_dim_outside(self.dim_outside)
            pane.set_calibration(camera_calibration(cam))
            pane.set_undistort(self.undistort)
            self.panes[cam['key']] = pane
            self.order.append(cam['key'])
            mp4 = frame_files = None
            if cam.get('kind') == 'mp4':
                mp4 = os.path.join(outdir, cam.get('file') or '')
                if not os.path.isfile(mp4):
                    mp4 = None
            elif cam.get('kind') == 'images':
                d = os.path.join(outdir, cam.get('dir') or '')
                frame_files = [os.path.join(d, fn)
                               for fn in (cam.get('frames_list') or [])]
                frame_files = frame_files or None
            if mp4 or frame_files:
                pane.start(mp4, frame_files, self.quality_w)
            else:
                pane.view.set_hint('缓存文件缺失')
        self.visible = set(self.order)
        self._save_rois()
        self._relayout()

    @staticmethod
    def _load_times(outdir, cam):
        """逐帧时间戳：优先读 .times；缺失时用 start_offset + fps 兜底"""
        n = int(cam.get('frames') or 0)
        path = cam.get('times_file')
        if path:
            try:
                times = PREP.read_times(os.path.join(outdir, path))
                if len(times) == n:
                    return times
            except Exception:
                pass
        off = float(cam.get('start_offset_s') or 0.0)
        fps = float(cam.get('fps') or 0.0) or 30.0
        return [off + i / fps for i in range(n)]

    def _active_keys(self):
        return [k for k in self.order
                if k in self.visible and (self.solo_key is None or k == self.solo_key)]

    def _relayout(self):
        while self.grid.count():
            it = self.grid.takeAt(0)
            wid = it.widget()
            if wid and wid not in self.panes.values():
                wid.setParent(None)
                wid.deleteLater()
        keys = self._active_keys()
        shown = set(keys)
        # 移出布局并不会隐藏控件，必须显式 setVisible，否则单路放大时其他画面会残留
        for k, p in self.panes.items():
            p.setVisible(k in shown)
        if not keys:
            lab = QLabel('没有已勾选的相机 —— 点上方「全选相机」')
            lab.setAlignment(Qt.AlignCenter)
            lab.setStyleSheet('color:#6b7686;padding:60px;')
            lab.setParent(self.grid_host)
            self.grid.addWidget(lab, 0, 0)
            self.lbl_sel.setText('显示 0 / %d 路' % len(self.order))
            return
        cols = 1 if self.solo_key else max(1, min(self.cols, len(keys)))
        for n, k in enumerate(keys):
            self.grid.addWidget(self.panes[k], n // cols, n % cols)
        rows = (len(keys) + cols - 1) // cols
        # 先清掉上一次布局留下的伸缩权重，否则切回单路时格子只占 1/3 宽
        for c in range(12):
            self.grid.setColumnStretch(c, 0)
        for r in range(12):
            self.grid.setRowStretch(r, 0)
        for c in range(cols):
            self.grid.setColumnStretch(c, 1)
        for r in range(rows):
            self.grid.setRowStretch(r, 1)
        self.lbl_sel.setText('显示 %d / %d 路' % (len(keys), len(self.order)))
        self._fit_cells(cols, rows)

    def _fit_cells(self, cols, rows):
        vw = max(240, self.scroll.viewport().width() - 6 * (cols - 1))
        vh = max(180, self.scroll.viewport().height() - 6 * (rows - 1))
        key = (vw, vh, cols, rows)
        if key == self._ratio_key:
            return
        self._ratio_key = key
        cw = vw / cols
        ch = vh / rows
        h = min(ch, cw * (1300.0 / 1600.0))
        for k in self.order:
            self.panes[k].setMinimumHeight(int(max(100, h)))

    def set_columns(self, n):
        self.cols = n
        self._relayout()

    def set_all_cams(self, on):
        self.visible = set(self.order) if on else set()
        self.solo_key = None
        self._relayout()
        self._apply_quality()

    def toggle_solo(self, key):
        self.solo_key = None if self.solo_key == key else key
        self._relayout()
        self._apply_quality()

    def _apply_quality(self):
        """切换清晰度 / 单路放大后立刻重解当前帧（暂停状态下也要马上变）"""
        w = 1600 if self.solo_key else self.quality_w
        if self.speed > 2.0:
            w = min(w, 560)
        keys = self._active_keys()
        if self.speed > 2.0 and len(keys) > 1:
            keys = keys[:1]
        for k in keys:
            p = self.panes[k]
            if p.stream:
                p.stream.target_w = w
                p.stream.invalidate()
        self._render_panes()

    def _quality_changed(self):
        self.quality_w = self.cmb_q.currentData() or 800
        self._apply_quality()

    # ---------------------------------------------------------------- 参考框
    def _load_rois(self):
        raw = self.settings.value('rois', '', type=str) or ''
        out = {}
        if raw:
            try:
                for k, v in (json.loads(raw) or {}).items():
                    r = normalize_roi(v)
                    if r:
                        out[k] = list(r)
            except Exception:
                pass
        return out

    def _save_rois(self):
        try:
            self.settings.setValue('rois', json.dumps(self.rois))
        except Exception:
            pass

    def _roi_toggle_show(self, on):
        self.show_roi = bool(on)
        for p in self.panes.values():
            p.set_show_roi(self.show_roi)
        try:
            self.settings.setValue('roi_show', self.show_roi)
        except Exception:
            pass
        if self.show_roi:
            if any(p.roi() for p in self.panes.values()):
                self._say('参考框已显示；可点「标定参考框」拖拽微调')
            else:
                self._roi_restore_defaults()

    def _roi_toggle_calib(self, on):
        self.calibrating = bool(on)
        for p in self.panes.values():
            p.set_calibrating(self.calibrating)
        if self.calibrating:
            if not self.chk_roi.isChecked():
                self.chk_roi.setChecked(True)
            self._say('标定参考框：在画面上按住左键拖拽即可画出；'
                      '画面上点右键可清除。标定完请关掉这个按钮。', 12000)
        else:
            self._say('已退出标定模式')

    def _roi_apply_to_all(self):
        """把当前相机（单路放大时就是它）的参考框复制给全部相机"""
        keys = self._active_keys()
        src = self.solo_key or (keys[0] if keys else None)
        if not src or src not in self.panes:
            self._say('还没有可用的相机')
            return
        roi = self.panes[src].roi()
        if not roi:
            self._say('%s 还没有参考框，先拖一个出来再应用' % src)
            return
        for k, p in self.panes.items():
            p.set_roi(roi)
            self.rois[k] = list(roi)
        self._save_rois()
        self._say('已把 %s 的参考框应用到全部 %d 路相机' % (src, len(self.panes)))

    def _roi_clear_all(self):
        for p in self.panes.values():
            p.set_roi(None)
        self.rois.clear()
        self._save_rois()
        self._say('已清除全部参考框')

    def _roi_restore_defaults(self):
        """恢复 camera2 / camera3 的官网近似默认工作区并立即显示。"""
        restored = 0
        for key, pane in self.panes.items():
            roi = default_roi_for_camera(key)
            if roi is None:
                continue
            pane.set_roi(roi)
            pane.set_show_roi(True)
            self.rois[key] = list(roi)
            restored += 1
        self._save_rois()
        self.show_roi = True
        if hasattr(self, 'chk_roi') and not self.chk_roi.isChecked():
            self.chk_roi.blockSignals(True)
            self.chk_roi.setChecked(True)
            self.chk_roi.blockSignals(False)
        self.settings.setValue('roi_show', True)
        if restored:
            self._say('已恢复 camera2/3 官网近似默认框（25%,13%,50%×73%）')
        else:
            self._say('当前没有 camera2/3 画面可恢复')

    def _on_pane_roi(self, key, roi):
        if isinstance(roi, str):                 # '__all__'
            self._roi_clear_all()
            return
        if roi:
            self.rois[key] = list(roi)
            self._save_rois()
            x, y, w, h = roi
            self._say('%s 参考框已保存：位置 %d%%,%d%%　大小 %d%%×%d%%'
                      % (key, round(x * 100), round(y * 100),
                         round(w * 100), round(h * 100)))
        else:
            self.rois.pop(key, None)
            self._save_rois()
            self._say('%s 参考框已清除' % key)

    def _undistort_toggled(self, on):
        self.undistort = bool(on)
        try:
            self.settings.setValue('undist', self.undistort)
        except Exception:
            pass
        for p in self.panes.values():
            p.set_undistort(self.undistort)
        if self.undistort:
            self._say('已开启鱼眼去畸变（标定来自录像本身）。'
                      '参考框要在去畸变画面上重新标一次，因为两种画面的坐标不通用。',
                      12000)
        else:
            self._say('已关闭去畸变，显示原始鱼眼画面')

    def _roi_shape_changed(self):
        self.roi_shape = self.cmb_shape.currentData() or 'ellipse'
        try:
            self.settings.setValue('roi_shape', self.roi_shape)
        except Exception:
            pass
        for p in self.panes.values():
            p.set_roi_shape(self.roi_shape)
        self._sync_roi_menu_checks()

    def _set_roi_shape(self, shape):
        """菜单入口设置框形状（与 cmb_shape 保持同一份数据）"""
        i = self.cmb_shape.findData(shape)
        if i >= 0:
            self.cmb_shape.setCurrentIndex(i)      # 触发 _roi_shape_changed
        else:
            self.roi_shape = shape
            self._sync_roi_menu_checks()

    def _dim_toggled(self, on):
        self.dim_outside = bool(on)
        try:
            self.settings.setValue('roi_dim', self.dim_outside)
        except Exception:
            pass
        for p in self.panes.values():
            p.set_dim_outside(self.dim_outside)
        if self.dim_outside:
            self._say('框外压暗已开启：只保留参考框内亮度，便于比对构图')

    def _sync_roi_menu_checks(self):
        for act in getattr(self, '_shape_actions', []):
            act.setChecked(act.text().startswith(
                {'roundrect': '圆角矩形', 'ellipse': '椭圆',
                 'rect': '矩形'}.get(self.roi_shape, '')))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self._rs_timer is None:
            self._rs_timer = QTimer(self)
            self._rs_timer.setSingleShot(True)
            self._rs_timer.setInterval(80)
            self._rs_timer.timeout.connect(self._relayout)
        self._rs_timer.start()

    def showEvent(self, ev):
        super().showEvent(ev)
        self._ratio_key = None
        QTimer.singleShot(80, self._relayout)

    # ---------------------------------------------------------------- 播放
    def play(self):
        if not self.panes or self.duration <= 0:
            return
        if self.t >= self.duration - 0.001:
            self.t = 0.0
        self.t_start = self.t
        self.clock.restart()
        self.playing = True
        self._finish_emitted = False
        self._apply_render_cap()
        self.btn_play.setText('⏸  暂停')
        self.audio_started = False
        self._sync_audio(restart=True)

    def pause(self):
        self.playing = False
        self.btn_play.setText('▶  播放')
        self.audio.pause()
        self.audio_started = False

    def toggle_play(self):
        self.pause() if self.playing else self.play()

    def seek(self, t):
        if self._direct_session is not None:
            # F-E2：交给 session（内部 generation++ 并丢过期帧），不在此处手动解码
            self.t = max(0.0, min(self.duration, t))
            self.t_start = self.t
            self.clock.restart()
            self._direct_anchor_pending = True
            self._direct_anchor_media_s = self.t
            self._direct_session.seek(self.t)
            self._update_clock()
            return
        self.t = max(0.0, min(self.duration, t))
        self.t_start = self.t
        self.clock.restart()
        self._render_panes()
        self._update_clock()
        self._sync_audio(restart=True)

    def frame_step(self, d):
        self.pause()
        prim = self.primary_pane()
        if prim is None or not prim.times:
            return
        cur = pick_frame(prim.times, self.t)
        if cur < 0:
            cur = 0
        nxt = max(0, min(len(prim.times) - 1, cur + d))
        self.seek(prim.times[nxt])
        self._say('第 %d / %d 帧' % (nxt, prim.frames))

    def _on_reach_end(self):
        if self._finish_emitted:          # P1.6E-R2：结束事件只触发一次
            return
        self._finish_emitted = True
        if self.chk_loop.isChecked():
            # 循环播放不标记已看完：否则循环中的视频会被立刻删掉缓存
            self.seek(0.0)
            self.play()
            return
        self.pause()
        self.seek(self.duration)
        # 只有正常播放时主时钟真正越过结尾才算 natural_end；
        # 拖动到结尾 / End 键 / 逐帧到结尾都不会进入这里。
        self.finish_current_video('natural_end')

    def _speed_changed(self):
        self.speed = self.cmb_speed.currentData() or 1.0
        self._apply_render_cap()          # P1.6E-R2：按倍速设 render FPS 上限
        for pane in self.panes.values():
            if pane.stream:
                pane.stream.catchup_limit = 4 if self.speed > 2.0 else MAX_SEQUENTIAL_CATCHUP
        self._apply_quality()
        if self.playing:
            self.t_start = self.t
            self.clock.restart()
        self._apply_speed_policy()
        self._sync_audio(restart=True)
        if self.speed > 2.0 and len(self._active_keys()) > 1:
            self._say('%g× 高倍速模式：优先流畅播放第一路相机；其它路低频预览。'
                      '双击任意画面可单路流畅播放。' % self.speed, 9000)

    def _apply_speed_policy(self):
        """视频变速时音频必须同步变速；MCI 做不到就自动静音并明确提示"""
        if not self.audio.ok:
            return
        if abs(self.speed - 1.0) < 1e-9:
            if self.audio_muted_by_speed:
                self.audio_muted_by_speed = False
                self.btn_sound.setChecked(True)
                self._say('速度已回到 1×，音频已恢复')
            return
        if self.audio.speed_supported and self.audio.set_speed(self.speed):
            return
        if self.btn_sound.isChecked():
            self.audio_muted_by_speed = True
            self.btn_sound.setChecked(False)
            self.audio.pause()
            self._say('当前速度 %g×：系统音频接口（MCI）不支持变速播放，已自动静音；'
                      '回到 1× 会自动恢复。' % self.speed, 9000)

    # ---------------------------------------------------------------- 音频
    def _audio_ms(self, t):
        """主时钟 → 音频设备时间；早于音频起点时返回 None（此时不许出声）"""
        rel = t - self.audio_offset
        if rel < 0:
            return None
        return int(rel * 1000)

    def _sync_audio(self, restart=False):
        """暂停 / 播放 / 拖动 / 循环 / 换文件后都调用，保证音频与画面同步"""
        if not self.audio.ok:
            return
        if not self.btn_sound.isChecked():
            self.audio.pause()
            self.audio_started = False
            return
        ms = self._audio_ms(self.t)
        if ms is None:
            self.audio.stop()
            self.audio_started = False
            return
        if restart:
            self.audio.stop()
            self.audio_started = False
        if self.playing:
            if not self.audio_started:
                if self.audio.play_from(ms):
                    self.audio_started = True
            else:
                self.audio.resume()
        else:
            self.audio.pause()
            self.audio_started = False

    def _tick_audio(self):
        """播放中跨过音频起点时才开始，避免提前出声"""
        if not (self.playing and self.audio.ok and self.btn_sound.isChecked()):
            return
        if self.audio_started:
            return
        ms = self._audio_ms(self.t)
        if ms is not None and self.audio.play_from(ms):
            self.audio_started = True

    def _load_audio(self, man):
        self.audio.close()
        self.audio_started = False
        self.audio_muted_by_speed = False
        a = man.get('audio')
        self.btn_sound.setEnabled(False)
        self.btn_sound.setChecked(False)
        if not a:
            self.btn_sound.setText('🔇 无音频')
            return
        if not a.get('playable', True):
            self.btn_sound.setText('🔇 音频格式不支持')
            return
        wav = os.path.join(cache_outdir(self.fid), a.get('file') or 'audio.wav')
        if not os.path.isfile(wav):
            self.btn_sound.setText('🔇 无音频')
            return
        if not self.audio.ok:
            self.btn_sound.setText('🔇 音频不可用')
            return
        if not self.audio.load(wav):
            self.btn_sound.setText('🔇 音频不可用')
            return
        self.btn_sound.setText('🔊 声音')
        self.btn_sound.setEnabled(True)

    def _toggle_sound(self, on):
        if not self.audio.ok:
            return
        if on:
            if abs(self.speed - 1.0) >= 1e-9 and not self.audio.speed_supported:
                self.audio_muted_by_speed = False
                self._say('当前速度 %g×：MCI 不支持变速播放，无法开启声音' % self.speed)
                self.btn_sound.setChecked(False)
                return
            self.audio.set_volume(0.85)
        self._sync_audio(restart=True)

    # ---------------------------------------------------------------- IMU
    def _load_imu(self, man):
        path = os.path.join(cache_outdir(self.fid), 'imu.json')
        if not (man.get('imu') and os.path.isfile(path)):
            self.imu_data = None
            self.imu_chart.clear()
            self.imu_box.setVisible(False)
            return
        import json
        try:
            with open(path, encoding='utf-8') as fh:
                self.imu_data = json.load(fh)
        except Exception:
            self.imu_data = None
            self.imu_chart.clear()
            self.imu_box.setVisible(False)
            return
        self.imu_box.setVisible(True)
        self.imu_chart.set_data(self.imu_data, self.duration)

    def _imu_opts(self):
        self.imu_chart.set_visible_series(self.chk_gyro.isChecked(), self.chk_acc.isChecked())

    # ---------------------------------------------------------------- 三队列界面
    #: 队列行高度（固定，不依赖 sizeHint）
    ROW_H = 60

    def folder_mode(self):
        """当前文件夹类型：'mcap'（走三队列缓存）/ 'mp4'（本地视频直读）。

        实际数据里一个文件夹只会是一种格式；万一混放也按 'mp4' 处理——
        用与格式无关的「可播放」表述，功能完全不受影响。
        清空队列后条目为空，因此这里优先用装载时记下的模式。
        """
        if getattr(self, '_mode_cache', None):
            return self._mode_cache
        if self.qm is None or not self.qm.order:
            return 'mcap'
        for sid in self.qm.order:
            if self.qm.items[sid].get('direct'):
                return 'mp4'
        return 'mcap'

    def _sync_mode_widgets(self, mode=None):
        """按文件夹类型调整与缓存相关的文案（MP4 文件夹里不该出现「缓存」字样）"""
        mode = mode or self.folder_mode()
        if mode == 'mcap':
            self.btn_clear.setText('清空队列与缓存')
            self.btn_clear.setToolTip(
                '清空三个队列（含已看完记录）并删除所有桌面缓存；\n'
                '正在播放的视频与原始 .mcap 文件不动。')
        else:
            self.btn_clear.setText('清空队列')
            self.btn_clear.setToolTip(
                '清空三个队列（含已看完记录）；\n'
                '本地视频是直接播放的，不产生缓存，原文件也不会被删除。')

    @staticmethod
    def _clear_layout(layout):
        """清空布局里的全部子项（含 stretch），行 widget 一并销毁"""
        while layout.count():
            it = layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _refresh_queues(self):
        """QueueManager 状态 → 三个页签的行 + 底部一条队列状态"""
        if self.qm is None:
            return
        q = self.qm.queues()
        for key, items in (('unwatched', q['unwatched']),
                           ('cached', q['cached']),
                           ('watched', q['watched'])):
            layout = self.queue_layouts[key]
            self._clear_layout(layout)
            for it in items:
                layout.addWidget(self._make_row(it))
            layout.addStretch(1)
        caching = next((it for it in q['unwatched']
                        if it['state'] == QM.CACHING), None)
        n_slim = len([it for it in q['cached'] if not it.get('direct')])
        n_direct = len([it for it in q['cached'] if it.get('direct')])
        mode = self.folder_mode()
        if mode == 'mcap':
            # MCAP 录像：槽位是有意义的（最多留 3 个），按原样显示
            tab1 = '已缓存 (%d/%d)' % (n_slim, QM.MAX_CACHED_ITEMS)
            bar = '未看 %d ｜ 已缓存 %d/%d ｜ 已看完 %d ｜ 缓存 %s' % (
                len(q['unwatched']), n_slim, QM.MAX_CACHED_ITEMS,
                len(q['watched']), PL.human_size(self.qm.cache_usage_bytes()))
        else:
            # 本地视频（MP4）：不写缓存、没有槽位概念，用「可播放」表述
            n_play = n_slim + n_direct
            tab1 = '可播放 (%d)' % n_play
            bar = '未看 %d ｜ 可播放 %d ｜ 已看完 %d ｜ 共 %d 个' % (
                len(q['unwatched']), n_play, len(q['watched']),
                len(self.items))
        self.tabs.setTabText(0, '未看 (%d)' % len(q['unwatched']))
        self.tabs.setTabText(1, tab1)
        self.tabs.setTabText(2, '已看完 (%d)' % len(q['watched']))
        self.lbl_queues.setText(bar)
        self._sync_mode_widgets(mode)
        if caching:
            self._say('正在缓存 %s · %d%%' % (
                caching['name'], round((caching.get('progress') or 0) * 100)), 1500)

    def _row_no(self, sid):
        try:
            return self.sids.index(sid) + 1
        except ValueError:
            return 0

    def _badge_text(self, it):
        if it.get('cleanup_pending'):
            return '待清理'
        st = it['state']
        if it.get('direct'):
            if st == QM.PLAYING:
                return '播放中'
            if st == QM.WATCHED:
                return '已看完'
            return '可播放'          # 本地视频：不需要缓存，随时可播
        if st == QM.CACHING:
            return '缓存中 %d%%' % round((it.get('progress') or 0) * 100)
        if st == QM.ERROR:
            return '失败'
        if st == QM.PLAYING:
            return '播放中'
        if st == QM.WATCHED:
            return '已看完'
        if st == QM.CACHED:
            return '已缓存'
        return '等待'

    def _make_row(self, it):
        """两行布局：[序号][文件名(中间省略)][按钮] / [大小·时间][状态徽标]"""
        w = QueueRow(lambda: self.play_sid(it['sid']))
        w.setFixedHeight(self.ROW_H - 2)
        cur = (it['sid'] == self.qm.current_sid)
        if cur:
            w.setStyleSheet('QueueRow{background:#1a2c4e;border-left:3px solid #4c8dff;}')
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 5, 6, 5)
        v.setSpacing(1)
        top = QHBoxLayout()
        top.setSpacing(6)
        no = QLabel('%d.' % self._row_no(it['sid']))
        no.setObjectName('dim')
        top.addWidget(no)
        name = ElidedLabel(it['name'])
        name.setToolTip(it['path'])
        if cur:
            name.setStyleSheet('color:#4c8dff;font-weight:600;')
        top.addWidget(name, 1)
        btn = None
        sid, st = it['sid'], it['state']
        if it.get('cleanup_pending'):
            btn = QPushButton('重试清理')
            btn.clicked.connect(lambda _=False, s=sid: self.qm._retry_cleanup(s))
        elif st == QM.ERROR:
            btn = QPushButton('重试缓存')
            btn.clicked.connect(lambda _=False, s=sid: self._row_recache(s))
        elif st == QM.CACHING:
            btn = QPushButton('取消')
            btn.clicked.connect(lambda _=False, s=sid: self.qm.cancel_cache(s))
        elif st in (QM.CACHED, QM.PLAYING):
            btn = QPushButton('播放')
            btn.clicked.connect(lambda _=False, s=sid: self.play_sid(s))
        elif st == QM.WATCHED:
            btn = QPushButton('重新缓存')
            btn.clicked.connect(lambda _=False, s=sid: self._row_recache(s))
        else:
            btn = QPushButton('缓存')
            btn.clicked.connect(lambda _=False, s=sid: self.qm.request_cache(s))
        btn.setFixedWidth(82)
        top.addWidget(btn)
        v.addLayout(top)
        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        mt = (it.get('mtime_ns') or 0) / 1e9
        meta = QLabel('%s · %s' % (
            PL.human_size(it.get('size') or 0),
            time.strftime('%Y-%m-%d %H:%M', time.localtime(mt)) if mt else '—'))
        meta.setObjectName('dim')
        bottom.addWidget(meta)
        bottom.addStretch(1)
        badge = QLabel(self._badge_text(it))
        badge.setStyleSheet('color:#8b95a7;')
        bottom.addWidget(badge)
        v.addLayout(bottom)
        return w

    def _row_recache(self, sid):
        """未看项的重试 / 已看完项的重新缓存，统一入口"""
        it = self.qm.items.get(sid)
        if it is None:
            return
        if it['state'] == QM.WATCHED:
            self._say('已加入缓存队列：%s' % it['name'])
        self.qm.request_recache(sid)

    # ---------------------------------------------------------------- 导出
    def _export_menu(self):
        if not self.manifest:
            self._say('还没有打开文件')
            return
        m = QMenu(self)
        a1 = m.addAction('当前画面（选中相机）PNG…')
        a2 = m.addAction('当前画面（所有相机合成）PNG…')
        m.addSeparator()
        a3 = m.addAction('当前视频 MP4（无损）…')
        a4 = m.addAction('当前视频原始 H.264 码流…')
        m.addSeparator()
        a5 = m.addAction('IMU 数据 CSV…')
        a6 = m.addAction('音频 WAV…')
        act = m.exec(self.btn_export.mapToGlobal(self.btn_export.rect().bottomLeft()))
        if act == a1:
            self._snap_one()
        elif act == a2:
            self.snapshot_grid()
        elif act == a3:
            self._export_file('mp4')
        elif act == a4:
            self._export_file('raw')
        elif act == a5:
            self._export_file('imu')
        elif act == a6:
            self._export_file('audio')

    def _stem(self):
        if not self.manifest:
            return 'mcap'
        return os.path.splitext((self.manifest.get('summary') or {}).get('name', 'mcap'))[0]

    def _grab(self, key, max_w=None):
        """按同一套选帧逻辑（bisect on times）取当前帧，导出与播放完全一致"""
        pane = self.panes.get(key)
        cam = next((c for c in (self.manifest or {}).get('cameras', [])
                    if c['key'] == key), None)
        if not pane or not cam or not HAS_CV:
            return None
        idx = pick_frame(pane.times, self.t)
        if idx < 0:
            return None
        outdir = cache_outdir(self.fid)
        try:
            if cam.get('kind') == 'mp4':
                cap = cv2.VideoCapture(os.path.join(outdir, cam['file']))
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok, fr = cap.read()
                cap.release()
                if not ok:
                    return None
            else:
                files = cam.get('frames_list') or []
                if not (0 <= idx < len(files)):
                    return None
                fr = imread_unicode(os.path.join(outdir, cam['dir'], files[idx]))
                if fr is None:
                    return None
        except Exception:
            return None
        if max_w and fr.shape[1] > max_w:
            fr, _ = downscale(fr, max_w)
        # 导出的画面与屏幕上看到的一致：去畸变和参考框开着就一起应用
        if self.undistort:
            calib = getattr(pane, 'calib', None) or camera_calibration(cam)
            if calib:
                try:
                    fr = fisheye_undistort(fr, calib)
                except Exception:
                    pass
        roi = self.rois.get(key) or default_roi_for_camera(key)
        if self.show_roi and roi:
            try:
                fr = draw_roi_on_frame(fr, roi, label=key,
                                       shape=self.roi_shape,
                                       dim=self.dim_outside)
            except Exception:
                pass
        return fr

    def _snap_one(self):
        keys = self._active_keys()
        key = self.solo_key or (keys[0] if keys else None)
        img = self._grab(key) if key else None
        if img is None:
            self._say('取帧失败')
            return
        fn, _ = QFileDialog.getSaveFileName(
            self, '保存当前画面',
            os.path.join(self.folder or '.', '%s_%s_%.3fs.png' % (self._stem(), key, self.t)),
            'PNG 图片 (*.png)')
        if not fn:
            return
        if self._save_img(img, fn):
            self._say('已导出 %s' % os.path.basename(fn))
        else:
            # P1.6E-R1：可恢复错误不弹窗
            self._say('保存失败，请换一个路径试试', 8000)

    def snapshot_grid(self):
        keys = self._active_keys()
        if not keys:
            self._say('没有可导出的画面')
            return
        tiles = []
        for k in keys:
            img = self._grab(k, max_w=1000)
            if img is None:
                continue
            idx = pick_frame(self.panes[k].times, self.t)
            cv2.putText(img, '%s  #%d' % (k, idx), (12, 34),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
            tiles.append(img)
        if not tiles:
            self._say('取帧失败')
            return
        cols = max(1, min(3, len(tiles)))
        rows = (len(tiles) + cols - 1) // cols
        h = max(t.shape[0] for t in tiles)
        w = max(t.shape[1] for t in tiles)
        gap = 6
        canvas = np.full((rows * h + (rows + 1) * gap, cols * w + (cols + 1) * gap, 3),
                         16, dtype=np.uint8)
        for n, t in enumerate(tiles):
            r, c = divmod(n, cols)
            y, x = gap + r * (h + gap), gap + c * (w + gap)
            canvas[y:y + t.shape[0], x:x + t.shape[1]] = t
        fn, _ = QFileDialog.getSaveFileName(
            self, '保存合成画面',
            os.path.join(self.folder or '.', '%s_%.3fs.png' % (self._stem(), self.t)),
            'PNG 图片 (*.png)')
        if not fn:
            return
        if self._save_img(canvas, fn):
            self._say('已导出合成画面（%d 路）' % len(tiles))

    @staticmethod
    def _save_img(img, path):
        try:
            from PIL import Image
            Image.fromarray(img[:, :, ::-1]).save(path)
            return True
        except Exception:
            pass
        try:
            ext = os.path.splitext(path)[1] or '.png'
            ok, buf = cv2.imencode(ext, img)
            if not ok:
                return False
            buf.tofile(path)
            return True
        except Exception:
            return False

    def _export_file(self, kind):
        cd = cache_outdir(self.fid)
        stem = self._stem()
        keys = self._active_keys()
        first = self.solo_key or (keys[0] if keys else None)
        if kind == 'mp4':
            cam = next((c for c in self.manifest['cameras'] if c['key'] == first), None)
            if not cam or cam.get('kind') != 'mp4':
                self._say('没有可导出的 MP4')
                return
            fn, _ = QFileDialog.getSaveFileName(
                self, '导出 MP4', os.path.join(self.folder or '.', '%s_%s.mp4' % (stem, first)),
                'MP4 视频 (*.mp4)')
            if fn:
                self._copy(os.path.join(cd, cam['file']), fn)
        elif kind == 'raw':
            cam = next((c for c in self.manifest['cameras'] if c.get('source_raw')), None)
            if not cam:
                self._say('没有原始码流（图片序列通道没有）')
                return
            fn, _ = QFileDialog.getSaveFileName(
                self, '导出 H.264',
                os.path.join(self.folder or '.', '%s_%s.h264' % (stem, cam['key'])),
                'H.264 码流 (*.h264)')
            if fn:
                self._copy(os.path.join(cd, cam['source_raw']), fn)
        elif kind == 'imu':
            src = os.path.join(cd, 'imu.json')
            if not os.path.isfile(src):
                self._say('该文件不含 IMU 数据')
                return
            fn, _ = QFileDialog.getSaveFileName(
                self, '导出 IMU', os.path.join(self.folder or '.', '%s_imu.csv' % stem),
                'CSV 表格 (*.csv)')
            if not fn:
                return
            import json
            with open(src, encoding='utf-8') as fh:
                d = json.load(fh)
            with open(fn, 'w', encoding='utf-8-sig', newline='') as fh:
                fh.write('time_s,gyro_x,gyro_y,gyro_z,acc_x,acc_y,acc_z\n')
                for i, t in enumerate(d['t']):
                    fh.write('%.6f,%.9f,%.9f,%.9f,%.9f,%.9f,%.9f\n' % (
                        t, d['av'][0][i], d['av'][1][i], d['av'][2][i],
                        d['la'][0][i], d['la'][1][i], d['la'][2][i]))
            self._say('已导出 IMU CSV')
        elif kind == 'audio':
            src = os.path.join(cd, 'audio.wav')
            if not os.path.isfile(src):
                self._say('该文件不含音频')
                return
            fn, _ = QFileDialog.getSaveFileName(
                self, '导出音频', os.path.join(self.folder or '.', '%s_audio.wav' % stem),
                'WAV 音频 (*.wav)')
            if fn:
                self._copy(src, fn)

    def _copy(self, src, dst):
        import shutil
        try:
            shutil.copyfile(src, dst)
            self._say('已导出 %s' % os.path.basename(dst))
        except Exception as e:
            # P1.6E-R1：可恢复错误不弹窗
            self._say('导出失败：%s' % e, 10000)

    # ---------------------------------------------------------------- 其它
    def _say(self, msg, ms=9000):
        self.statusBar().showMessage(msg, ms)

    def _restore(self):
        g = self.settings.value('geometry')
        if g:
            self.restoreGeometry(g)
        sp = self.settings.value('splitter')
        if sp:
            self.splitter.restoreState(sp)
        self.cols = int(self.settings.value('cols', 3, type=int) or 3)
        self.quality_w = int(self.settings.value('quality', 800, type=int) or 800)
        i = self.cmb_q.findData(self.quality_w)
        if i >= 0:
            self.cmb_q.setCurrentIndex(i)
        # 参考框：按相机 key 持久化，下次打开同一台设备的录像自动沿用
        self.rois = self._load_rois()
        self.show_roi = bool(self.settings.value('roi_show', True, type=bool))
        self.chk_roi.setChecked(self.show_roi)
        self.roi_shape = str(self.settings.value('roi_shape', ROI_DEFAULT_SHAPE)
                             or ROI_DEFAULT_SHAPE)
        i = self.cmb_shape.findData(self.roi_shape)
        if i >= 0:
            self.cmb_shape.setCurrentIndex(i)
        self._sync_roi_menu_checks()
        self.dim_outside = bool(self.settings.value('roi_dim', False, type=bool))
        self.act_dim.setChecked(self.dim_outside)
        self.undistort = bool(self.settings.value('undist', False, type=bool))
        self.chk_undist.setChecked(self.undistort)
        if not HAS_CV:
            QTimer.singleShot(600, lambda: QMessageBox.critical(
                self, APP_NAME,
                '缺少 opencv-python，无法解码视频。\n\n请运行：\npip install opencv-python'))

    def _shutdown_threads(self):
        """安全停掉所有线程，避免 QThread: Destroyed while thread is still running"""
        if self.timer is not None:
            self.timer.stop()
        if getattr(self, 'scrub_timer', None) is not None:
            self.scrub_timer.stop()
        if self.qm is not None:
            self.qm.shutdown()
            if self.qm.worker is not None and self.qm.worker.isRunning():
                self.qm.worker.wait(5000)
        for w in (self.worker, self.warm):
            if w is not None and w.isRunning():
                try:
                    w.cancel()
                except Exception:
                    pass
        for p in self.panes.values():
            if p.stream:
                p.stream.request_stop()
        for p in self.panes.values():
            p.stop()
        for w in (self.worker, self.warm):
            if w is not None:
                try:
                    w.wait(5000)
                except Exception:
                    pass
        self.audio.stop()
        self.audio.close()

    def closeEvent(self, ev):
        self._closing = True
        try:
            self.settings.setValue('geometry', self.saveGeometry())
            self.settings.setValue('splitter', self.splitter.saveState())
            self.settings.setValue('cols', self.cols)
            self.settings.setValue('quality', self.quality_w)
            self.settings.setValue('last_dir', self.folder)
        except Exception:
            pass
        self.playing = False
        self._shutdown_threads()
        super().closeEvent(ev)

    def _auto_start(self):
        args = [a for a in sys.argv[1:] if not a.startswith('-')]
        if args:
            p = os.path.abspath(args[0])
            if os.path.isdir(p):
                self.load_folder(p)
            elif os.path.isfile(p):
                self.load_folder(os.path.dirname(p), select=p)
            return
        last = str(self.settings.value('last_dir', '') or '')
        if last and os.path.isdir(last):
            self.load_folder(last, autoplay=False)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        urls = ev.mimeData().urls()
        if not urls:
            return
        p = urls[0].toLocalFile()
        if os.path.isdir(p):
            self.load_folder(p)
        elif p.lower().endswith('.mcap'):
            self.load_folder(os.path.dirname(p), select=p)

    # 全局按键：列表 / 按钮拿焦点时也能用
    def eventFilter(self, obj, ev):
        if (ev.type() == QEvent.KeyPress and self.isActiveWindow()
                and not self.loading and not self._closing):
            if not QApplication.activeModalWidget():
                if not isinstance(QApplication.focusWidget(), QTextEdit):
                    if self._handle_key(ev):
                        return True
        return super().eventFilter(obj, ev)

    def keyPressEvent(self, ev):
        if not self._handle_key(ev):
            super().keyPressEvent(ev)
            return
        ev.accept()

    def _handle_key(self, ev):
        k = ev.key()
        mod = ev.modifiers()
        if k == Qt.Key_Space:
            self.toggle_play()
        elif k == Qt.Key_Left:
            self.frame_step(-1)
        elif k == Qt.Key_Right:
            self.frame_step(1)
        elif k == Qt.Key_Home:
            self.seek(0)
        elif k == Qt.Key_End:
            self.seek(self.duration)
        elif k == Qt.Key_PageUp:
            self.step_file(-1)
        elif k == Qt.Key_PageDown:
            self.step_file(1)
        elif k in (Qt.Key_Plus, Qt.Key_Equal):
            self.cmb_speed.setCurrentIndex(
                min(self.cmb_speed.count() - 1, self.cmb_speed.currentIndex() + 1))
        elif k == Qt.Key_Minus:
            self.cmb_speed.setCurrentIndex(max(0, self.cmb_speed.currentIndex() - 1))
        elif k == Qt.Key_0:
            self.solo_key = None
            self._relayout()
            self._apply_quality()
        elif Qt.Key_1 <= k <= Qt.Key_9:
            n = k - Qt.Key_1
            if n < len(self.order):
                self.toggle_solo(self.order[n])
        elif k == Qt.Key_S and (mod & Qt.ControlModifier):
            self.snapshot_grid()
        elif k == Qt.Key_R and not (mod & Qt.ControlModifier):
            self.chk_roi.setChecked(not self.chk_roi.isChecked())
        elif k == Qt.Key_X:
            # 按两下 X = 标一段不合格（第一下起点、第二下终点）
            self._mark_bad_point()
        elif k == Qt.Key_Z and (mod & Qt.ControlModifier):
            self._undo_bad_mark()
        else:
            return False
        return True


# ================================================================== 入口
def self_check():
    """启动器调用的桌面栈烟雾检查；不显示主窗口。"""
    import pycheck
    reason = pycheck.validate_platform()
    if reason:
        raise RuntimeError(reason)
    appcache.ensure_cache_root()
    app = QApplication.instance()
    owns_app = app is None
    if owns_app:
        app = QApplication([sys.argv[0]])
    probe = QWidget()
    probe.resize(8, 8)
    probe.close()
    audio = MciAudio()
    audio.close()
    info = dict(
        ok=True,
        python=sys.version.split()[0],
        windows_build=pycheck.windows_build(),
        bits=64 if sys.maxsize > 2 ** 32 else 32,
        cache=appcache.CACHE_ROOT,
        opencv=getattr(cv2, '__version__', None),
        audio_available=audio.ok,
    )
    print(json.dumps(info, ensure_ascii=False))
    if owns_app:
        app.quit()
    return 0


def _write_startup_error(detail):
    """pythonw / .app 没有控制台，启动异常必须落盘，且兼容只读程序目录。"""
    app_dir = (os.path.dirname(os.path.abspath(sys.executable))
               if getattr(sys, 'frozen', False) else HERE)
    candidates = [os.path.join(app_dir, 'startup-error.log'),
                  os.path.join(appcache.user_data_dir(), 'startup-error.log')]
    for path in candidates:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'a', encoding='utf-8') as fh:
                fh.write(detail.rstrip() + '\n')
            return path
        except Exception:
            continue
    return ''


def _show_fatal_dialog(text):
    """跨平台兜底错误弹窗：Windows 用 MessageBoxW，macOS 用 osascript。"""
    if os.name == 'nt':
        try:
            ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x10)
            return
        except Exception:
            return
    if sys.platform == 'darwin':
        try:
            import subprocess
            safe = text.replace('\\', '\\\\').replace('"', '\\"')[:1500]
            subprocess.Popen(['osascript', '-e',
                              'display dialog "%s" with title "%s" buttons {"好"} '
                              'default button "好" with icon stop'
                              % (safe, APP_NAME)])
        except Exception:
            pass


def main():
    import pycheck
    reason = pycheck.validate_platform()
    if reason:
        raise RuntimeError(reason)
    try:
        appcache.ensure_cache_root()
    except Exception:
        pass
    QApplication.setApplicationName(APP_NAME)
    QApplication.setOrganizationName('MCAPViewer')
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == '__main__':
    try:
        if '--self-check' in sys.argv[1:]:
            sys.exit(self_check())
        sys.exit(main())
    except Exception:
        detail = '启动失败：\n\n' + traceback.format_exc()
        log_path = _write_startup_error(detail)
        suffix = ('\n\n错误日志：' + log_path) if log_path else ''
        _show_fatal_dialog(detail + suffix)
        raise
