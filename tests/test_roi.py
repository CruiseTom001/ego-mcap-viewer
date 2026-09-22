"""参考框（构图比对辅助线）测试

覆盖：归一化坐标换算 / 拖拽标定 / 窗口缩放与画质切换下的稳定性 /
      框外压暗与四角加粗确实画出来了 / 导出时把框一起画进画面 / 设置持久化

参考框用于人工比对构图（Genrobot 规范第四节：双手应落在虚线区域内、
人脸和第三人的手不得进入）。那条虚线只在采集 App 的实时预览里、不会录进码流，
所以由使用者自己在画面上画一个，并按归一化坐标保存。
"""

import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np                                  # noqa: E402
from PySide6.QtCore import Qt, QPointF, QEvent, QSettings   # noqa: E402
from PySide6.QtGui import QImage, QMouseEvent       # noqa: E402
from PySide6.QtWidgets import QApplication          # noqa: E402

import desktop as D                                 # noqa: E402


def _app():
    return QApplication.instance() or QApplication([])


def _view(w=200, h=100, img_w=1600, img_h=1300):
    v = D.FrameView()
    v.resize(w, h)
    img = QImage(img_w, img_h, QImage.Format_RGB32)
    img.fill(Qt.white)          # 用白色测试图，压暗效果才量得出来
    v.set_image(img)
    return v


def _press(view, x, y):
    ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(x, y), QPointF(x, y),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(view, ev)


def _move(view, x, y):
    ev = QMouseEvent(QEvent.MouseMove, QPointF(x, y), QPointF(x, y),
                     Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(view, ev)


def _release(view, x, y):
    ev = QMouseEvent(QEvent.MouseButtonRelease, QPointF(x, y), QPointF(x, y),
                     Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
    QApplication.sendEvent(view, ev)


class NormalizeCase(unittest.TestCase):
    def test_official_default_roi(self):
        self.assertEqual(D.OFFICIAL_DEFAULT_ROI, (0.25, 0.13, 0.50, 0.73))
        self.assertEqual(D.default_roi_for_camera('camera2'), D.OFFICIAL_DEFAULT_ROI)
        self.assertEqual(D.default_roi_for_camera('camera3'), D.OFFICIAL_DEFAULT_ROI)
        self.assertIsNone(D.default_roi_for_camera('camera1'))
        self.assertIsNone(D.default_roi_for_camera('camera20'))

    def test_valid(self):
        self.assertEqual(D.normalize_roi((0.1, 0.2, 0.3, 0.4)), (0.1, 0.2, 0.3, 0.4))

    def test_clamped_to_unit_square(self):
        r = D.normalize_roi((0.9, 0.9, 0.5, 0.5))
        self.assertAlmostEqual(r[0], 0.9)
        self.assertAlmostEqual(r[2], 0.1, places=9)      # 被裁到右边界
        self.assertAlmostEqual(r[3], 0.1, places=9)

    def test_negative_offset_clamped(self):
        r = D.normalize_roi((-0.2, -0.3, 0.5, 0.5))
        self.assertEqual(r[0], 0.0)
        self.assertEqual(r[1], 0.0)

    def test_too_small_rejected(self):
        self.assertIsNone(D.normalize_roi((0.1, 0.1, 0.01, 0.5)))
        self.assertIsNone(D.normalize_roi((0.1, 0.1, 0.5, 0.001)))

    def test_garbage_rejected(self):
        for bad in (None, (), [], 'x', (1, 2), ('a', 'b', 'c', 'd')):
            self.assertIsNone(D.normalize_roi(bad), bad)

    def test_zero_size_rejected(self):
        self.assertIsNone(D.normalize_roi((0.5, 0.5, 0, 0)))


class FromPointsCase(unittest.TestCase):
    def test_any_drag_direction(self):
        want = (0.1, 0.2, 0.3, 0.3)
        for a, b in (((0.1, 0.2), (0.4, 0.5)), ((0.4, 0.5), (0.1, 0.2)),
                     ((0.4, 0.2), (0.1, 0.5)), ((0.1, 0.5), (0.4, 0.2))):
            got = D.roi_from_points(a, b)
            for i in range(4):
                self.assertAlmostEqual(got[i], want[i], places=9, msg=str((a, b)))

    def test_tiny_drag_rejected(self):
        self.assertIsNone(D.roi_from_points((0.5, 0.5), (0.505, 0.505)))

    def test_missing_point(self):
        self.assertIsNone(D.roi_from_points(None, (0.5, 0.5)))
        self.assertIsNone(D.roi_from_points((0.5, 0.5), None))


class ToPixelsCase(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(D.roi_to_pixels((0.1, 0.2, 0.3, 0.4), 1000, 500),
                         (100, 100, 300, 200))

    def test_none_and_invalid(self):
        self.assertIsNone(D.roi_to_pixels(None, 100, 100))
        self.assertIsNone(D.roi_to_pixels((0.1, 0.1, 0.01, 0.01), 100, 100))
        self.assertIsNone(D.roi_to_pixels((0.1, 0.1, 0.5, 0.5), 0, 0))


class DrawOnFrameCase(unittest.TestCase):
    """导出时把参考框画进画面。默认椭圆 + 白色，与 Genrobot 文档一致"""

    def test_ellipse_dim_outside_keeps_inside(self):
        img = np.full((400, 600, 3), 200, dtype=np.uint8)
        before = img.copy()
        D.draw_roi_on_frame(img, (0.25, 0.25, 0.5, 0.5), label='camera2', dim=True)
        # 椭圆外压暗了
        self.assertLess(int(img[5, 5].mean()), int(before[5, 5].mean()))
        # 椭圆中心保持原亮度
        self.assertGreater(int(img[200, 300].mean()), 180)
        # 白色虚线确实画出来了
        self.assertGreater(int(img[200, :].max()), 240)

    def test_ellipse_default_no_dim(self):
        """文档里的框没有压暗，所以默认不该动框外像素"""
        img = np.full((400, 600, 3), 200, dtype=np.uint8)
        D.draw_roi_on_frame(img, (0.25, 0.25, 0.5, 0.5))
        self.assertAlmostEqual(int(img[5, 5].mean()), 200)

    def test_white_dash_is_white(self):
        img = np.zeros((400, 600, 3), dtype=np.uint8)
        D.draw_roi_on_frame(img, (0.25, 0.25, 0.5, 0.5))
        ys, xs = np.where(img[:, :, 0] > 240)
        self.assertGreater(len(ys), 0, '应该能找到白色虚线像素')
        self.assertGreater(img[ys[0], xs[0], 1], 240)
        self.assertGreater(img[ys[0], xs[0], 2], 240)

    def test_rect_shape_still_works(self):
        img = np.zeros((400, 600, 3), dtype=np.uint8)
        D.draw_roi_on_frame(img, (0.25, 0.25, 0.5, 0.5), shape='rect')
        # 矩形上边是水平虚线：一行上应有很多列白色
        self.assertGreater(int((img[100] > 240).sum()), 10)

    def test_roundrect_is_default_and_draws_flat_edges(self):
        """默认形状应是圆角矩形：顶边中段有平直的白色虚线，四角是圆弧"""
        img = np.zeros((400, 600, 3), dtype=np.uint8)
        D.draw_roi_on_frame(img, (0.25, 0.25, 0.5, 0.5))       # 不传 shape，用默认
        x, y, w, h = 150, 100, 300, 200
        # 顶边中段（避开圆角）应有白色虚线
        self.assertGreater(int((img[y, x + 80:x + w - 80] > 240).sum()), 10)
        # 左边中段也应有
        self.assertGreater(int((img[y + 60:y + h - 60, x] > 240).sum()), 10)
        # 而顶边靠左的「圆角区」上没有直角（角落像素应为黑）
        self.assertLess(int(img[y, x].mean()), 60)
        self.assertLess(int(img[y + h - 1, x + w - 1].mean()), 60)

    def test_roundrect_corner_radius(self):
        r = D._roi_corner_radius(300, 200)
        self.assertAlmostEqual(r, 60.0, places=6)      # 0.3 × 短边
        self.assertEqual(D._roi_corner_radius(10, 10), 3.0)

    def test_roundrect_polyline_closes(self):
        pts = D._rounded_rect_polyline(0, 0, 300, 200, 60)
        self.assertGreater(len(pts), 20)
        # 起点应在顶边偏右（避开左上圆角）
        self.assertEqual(pts[0], (60.0, 0.0))
        # 所有点都在矩形范围内
        for px, py in pts:
            self.assertLessEqual(px, 300.5)
            self.assertLessEqual(py, 200.5)
            self.assertGreaterEqual(px, -0.5)
            self.assertGreaterEqual(py, -0.5)

    def test_dashed_polyline_alternates(self):
        img = np.zeros((60, 400, 3), dtype=np.uint8)
        D._dashed_polyline(img, [(10, 30), (390, 30)], (255, 255, 255), 2, 10, 10)
        row = img[30, :, 0]
        # 一条 380px 的线画 10/10 虚线：应有明有暗
        self.assertGreater(int((row > 240).sum()), 100)
        self.assertGreater(int((row < 10).sum()), 100)

    def test_returns_same_array(self):
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        self.assertIs(D.draw_roi_on_frame(img, (0.1, 0.1, 0.5, 0.5)), img)

    def test_none_roi_is_noop(self):
        img = np.full((50, 50, 3), 7, dtype=np.uint8)
        D.draw_roi_on_frame(img, None)
        self.assertTrue((img == 7).all())


class UndistortCase(unittest.TestCase):
    """鱼眼去畸变：标定用测试夹具合成（不依赖本机真实缓存 / 绝对路径）

    数值取自一次真实 Genrobot 录像的 CameraCalibration（1600x1300），
    保证量级真实；来源改为夹具后可在任何机器完整运行。
    """

    @classmethod
    def setUpClass(cls):
        # 与 prepare._cal_for / desktop.camera_calibration 的解析格式保持一致：
        # calibration = {'K': [fx,fy,cx,cy] 或 3x3, 'D': [..., k1, k2]}
        cls.cam = dict(
            key='camera2',
            calibration={'width': 1600,
                         'K': [509.28, 0.0, 813.06,
                               0.0, 511.23, 637.07,
                               0.0, 0.0, 1.0],
                         'D': [509.28, 511.23, 813.06, 637.07, 0.5124, 0.5738]})
        cls.calib = D.camera_calibration(cls.cam)

    def test_calibration_parsed(self):
        self.assertIsNotNone(self.calib)
        fx, fy, cx, cy = self.calib[0]
        k1, k2 = self.calib[1]
        self.assertAlmostEqual(fx, 509.28, places=2)
        self.assertAlmostEqual(fy, 511.23, places=2)
        self.assertAlmostEqual(cx, 813.06, places=2)
        self.assertAlmostEqual(cy, 637.07, places=2)
        self.assertAlmostEqual(k2, 0.5738, places=3)
        self.assertEqual(self.calib[2], 1600)

    def test_calibration_none_when_absent(self):
        self.assertIsNone(D.camera_calibration({}))
        self.assertIsNone(D.camera_calibration({'calibration': {}}))
        self.assertIsNone(D.camera_calibration({'calibration': {'K': [], 'D': []}}))

    def test_undistort_changes_the_image(self):
        import cv2
        img = np.zeros((650, 800, 3), dtype=np.uint8)
        cv2.line(img, (0, 325), (800, 325), (0, 255, 0), 3)
        cv2.circle(img, (400, 325), 120, (0, 0, 255), 3)
        out = D.fisheye_undistort(img, self.calib)
        self.assertEqual(out.shape, img.shape)
        self.assertGreater(int(np.abs(out.astype(int) - img.astype(int)).max()), 0,
                           '去畸变应该改变图像')

    def test_undistort_scales_with_resolution(self):
        for w, h in ((800, 650), (1600, 1300), (560, 455)):
            img = np.zeros((h, w, 3), dtype=np.uint8)
            out = D.fisheye_undistort(img, self.calib)
            self.assertEqual(out.shape, img.shape, (w, h))

    def test_undistort_handles_bad_input(self):
        self.assertIsNone(D.fisheye_undistort(None, self.calib))
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        self.assertIsNotNone(D.fisheye_undistort(img, None))
        self.assertIsNotNone(D.fisheye_undistort(img, ((1, 1, 1, 1), (0, 0), 1600)))


class FrameViewGeometryCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_image_rect_is_letterboxed_and_centred(self):
        v = _view(200, 100, 1600, 1300)
        ir = v.image_rect()
        # 等比：宽高比与源一致
        self.assertAlmostEqual(ir.width() / ir.height(), 1600 / 1300, places=1)
        # 居中：左右留白相等（±1 取整误差）
        self.assertAlmostEqual(ir.x(), v.width() - ir.x() - ir.width(), delta=1)
        # 高度顶满
        self.assertAlmostEqual(ir.height(), 100, delta=1)

    def test_corners_map_to_unit_square(self):
        """注意 _to_norm 用的是「像素中心」约定：最右下角那个像素的中心
        在 (width-1)/width，不会正好等于 1.0，这是正常现象。"""
        v = _view(200, 100, 1600, 1300)
        ir = v.image_rect()
        self.assertAlmostEqual(v._to_norm(ir.topLeft())[0], 0.0, places=6)
        self.assertAlmostEqual(v._to_norm(ir.topLeft())[1], 0.0, places=6)
        self.assertAlmostEqual(v._to_norm(ir.bottomRight())[0], 1.0, delta=0.02)
        self.assertAlmostEqual(v._to_norm(ir.bottomRight())[1], 1.0, delta=0.02)

    def test_norm_px_round_trip(self):
        v = _view(300, 200, 1600, 1300)
        for roi in ((0.0, 0.0, 1.0, 1.0), (0.25, 0.5, 0.5, 0.25), (0.1, 0.1, 0.2, 0.3)):
            back = v._to_norm(v._to_px(roi).topLeft())
            self.assertAlmostEqual(back[0], roi[0], places=2)
            self.assertAlmostEqual(back[1], roi[1], places=2)

    def test_roi_is_independent_of_widget_size(self):
        """归一化坐标的意义：窗口大小变了，框还框在同一块画面上"""
        for w, h in ((200, 100), (800, 600), (411, 137)):
            v = _view(w, h, 1600, 1300)
            v.set_roi((0.2, 0.3, 0.5, 0.4))
            self.assertEqual(v.roi, (0.2, 0.3, 0.5, 0.4))
            rect = v._to_px(v.roi)
            ir = v.image_rect()
            self.assertGreaterEqual(rect.x(), ir.x() - 1)
            self.assertLessEqual(rect.right(), ir.right() + 1)

    def test_roi_is_independent_of_decode_resolution(self):
        """切换清晰度档位后，框仍然落在画面同一位置"""
        v = _view(400, 300, 1600, 1300)
        v.set_roi((0.25, 0.25, 0.5, 0.5))
        r1 = v._to_px(v.roi)
        img = QImage(800, 650, QImage.Format_RGB32)     # 换成半分辨率
        img.fill(0)
        v.set_image(img)
        r2 = v._to_px(v.roi)
        self.assertEqual(v.roi, (0.25, 0.25, 0.5, 0.5))
        # 两种分辨率下，框相对画面中心的偏移比例应当一致
        ir1 = v.image_rect()
        self.assertAlmostEqual(r2.x() / ir1.width(), r1.x() / ir1.width(), places=1)


class DragCalibrationCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_drag_creates_roi(self):
        v = _view(200, 100, 1600, 1300)
        v.set_calibrating(True)
        ir = v.image_rect()
        x0 = ir.x() + int(ir.width() * 0.25)
        y0 = ir.y() + int(ir.height() * 0.25)
        x1 = ir.x() + int(ir.width() * 0.75)
        y1 = ir.y() + int(ir.height() * 0.75)
        _press(v, x0, y0)
        _move(v, x1, y1)
        _release(v, x1, y1)
        self.assertIsNotNone(v.roi)
        x, y, w, h = v.roi
        for got, want in ((x, 0.25), (y, 0.25), (w, 0.5), (h, 0.5)):
            self.assertAlmostEqual(got, want, delta=0.02)

    def test_click_without_drag_keeps_existing_roi(self):
        v = _view(200, 100)
        v.set_calibrating(True)
        v.set_roi((0.2, 0.2, 0.5, 0.5))
        ir = v.image_rect()
        _press(v, ir.x() + 10, ir.y() + 10)
        _release(v, ir.x() + 10, ir.y() + 10)
        self.assertEqual(v.roi, (0.2, 0.2, 0.5, 0.5))

    def test_drag_outside_image_is_clamped(self):
        v = _view(300, 200, 1600, 1300)
        v.set_calibrating(True)
        _press(v, 0, 0)                      # 左上黑边外
        _release(v, 299, 199)                # 右下黑边外
        self.assertIsNotNone(v.roi)
        x, y, w, h = v.roi
        self.assertGreaterEqual(x, 0.0)
        self.assertGreaterEqual(y, 0.0)
        self.assertLessEqual(x + w, 1.0001)
        self.assertLessEqual(y + h, 1.0001)

    def test_roi_changed_signal_emitted(self):
        v = _view(200, 100)
        v.set_calibrating(True)
        got = []
        v.roiChanged.connect(got.append)
        ir = v.image_rect()
        _press(v, ir.x() + 20, ir.y() + 10)
        _release(v, ir.x() + ir.width() - 20, ir.y() + ir.height() - 10)
        self.assertEqual(len(got), 1)
        self.assertIsNotNone(got[0])

    def test_no_drag_when_not_calibrating(self):
        v = _view(200, 100)
        v.set_calibrating(False)
        ir = v.image_rect()
        _press(v, ir.x() + 20, ir.y() + 10)
        _release(v, ir.x() + 120, ir.y() + 80)
        self.assertIsNone(v.roi)

    def test_double_click_ignored_while_calibrating(self):
        v = _view(200, 100)
        v.set_calibrating(True)
        hits = []
        v.doubleClicked.connect(lambda: hits.append(1))
        ev = QMouseEvent(QEvent.MouseButtonDblClick, QPointF(10, 10), QPointF(10, 10),
                         Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(v, ev)
        self.assertEqual(hits, [])


class RenderCase(unittest.TestCase):
    """真的画一遍，确认没有异常、且框外确实被压暗"""

    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_paints_with_roi(self):
        """默认（圆角矩形、不压暗）：框内外亮度相同，但框上必须画出了白虚线"""
        v = _view(400, 300, 1600, 1300)
        v.resize(400, 300)
        v.set_show_roi(True)
        v.set_roi((0.2, 0.2, 0.6, 0.6))
        pm = v.grab()
        self.assertFalse(pm.isNull())
        img = pm.toImage()
        ir = v.image_rect()
        outside = img.pixelColor(ir.center().x(), ir.y() + 2).lightness()
        inside = img.pixelColor(ir.center().x(), ir.center().y()).lightness()
        self.assertEqual(outside, inside, '官方的框不压暗，框内外应一样亮')
        # 框的顶边中段（圆角矩形平直段）应能找到白色虚线
        found_white = False
        yy = ir.y() + int(ir.height() * 0.2)
        for x in range(ir.x() + ir.width() // 4, ir.right() - ir.width() // 4, 2):
            if img.pixelColor(x, yy).lightness() > 240:
                found_white = True
                break
        self.assertTrue(found_white, '圆角矩形顶边应能找到白色虚线')

    def test_paints_each_shape(self):
        for shape in ('roundrect', 'ellipse', 'rect'):
            with self.subTest(shape=shape):
                v = _view(400, 300, 1600, 1300)
                v.set_roi_shape(shape)
                v.set_show_roi(True)
                v.set_roi((0.2, 0.2, 0.6, 0.6))
                self.assertFalse(v.grab().isNull())

    def test_default_shape_is_roundrect(self):
        v = _view(300, 200)
        self.assertEqual(v.roi_shape, D.ROI_DEFAULT_SHAPE)
        self.assertEqual(D.ROI_DEFAULT_SHAPE, 'roundrect')

    def test_paints_dim_outside(self):
        """显式打开压暗：框外比框内暗"""
        v = _view(400, 300, 1600, 1300)
        v.resize(400, 300)
        v.set_dim_outside(True)
        v.set_show_roi(True)
        v.set_roi((0.2, 0.2, 0.6, 0.6))
        img = v.grab().toImage()
        ir = v.image_rect()
        outside = img.pixelColor(ir.center().x(), ir.y() + 2).lightness()
        inside = img.pixelColor(ir.center().x(), ir.center().y()).lightness()
        self.assertLess(outside, inside,
                        '框外应比框内暗（outside=%s inside=%s）'
                        % (outside, inside))
        self.assertGreater(inside, 200)

    def test_shape_switching(self):
        v = _view(300, 200)
        v.set_roi_shape('roundrect')
        self.assertEqual(v.roi_shape, 'roundrect')
        v.set_roi_shape('ellipse')
        self.assertEqual(v.roi_shape, 'ellipse')
        v.set_roi_shape('rect')
        self.assertEqual(v.roi_shape, 'rect')
        v.set_roi_shape('nonsense')
        self.assertEqual(v.roi_shape, D.ROI_DEFAULT_SHAPE)   # 未知值回落

    def test_paints_without_roi(self):
        v = _view(300, 200)
        v.set_show_roi(True)
        v.set_roi(None)
        self.assertFalse(v.grab().isNull())

    def test_paints_while_calibrating_without_roi(self):
        v = _view(300, 200)
        v.set_calibrating(True)
        self.assertFalse(v.grab().isNull())

    def test_paints_empty(self):
        v = D.FrameView()
        v.resize(200, 100)
        self.assertFalse(v.grab().isNull())


class PersistenceCase(unittest.TestCase):
    """参考框按相机 key 存进 QSettings，换文件/重启后沿用"""

    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def setUp(self):
        QSettings('MCAPViewer', 'Desktop').clear()

    def test_load_round_trip(self):
        w = D.MainWindow()
        try:
            w.rois = {'camera2': [0.1, 0.2, 0.3, 0.4]}
            w._save_rois()
            back = w._load_rois()
            self.assertEqual(sorted(back), ['camera2'])
            for a, b in zip(back['camera2'], [0.1, 0.2, 0.3, 0.4]):
                self.assertAlmostEqual(a, b, places=6)
        finally:
            w.close()

    def test_reference_frame_is_enabled_by_default(self):
        w = D.MainWindow()
        try:
            self.assertTrue(w.show_roi)
            self.assertTrue(w.chk_roi.isChecked())
        finally:
            w.close()

    def test_load_ignores_corrupt_entries(self):
        w = D.MainWindow()
        try:
            w.settings.setValue('rois', '{"camera2":[0.1,0.2,0.3,0.4],'
                                        '"bad":[1], "tiny":[0,0,0.001,0.001],'
                                        '"camera3":"oops"}')
            back = w._load_rois()
            self.assertEqual(sorted(back), ['camera2'])
        finally:
            w.close()

    def test_load_survives_garbage_json(self):
        w = D.MainWindow()
        try:
            w.settings.setValue('rois', 'not json at all')
            self.assertEqual(w._load_rois(), {})
        finally:
            w.close()

    def test_clear_all_wipes_settings(self):
        w = D.MainWindow()
        try:
            w.rois = {'camera2': [0.1, 0.2, 0.3, 0.4]}
            w._save_rois()
            w._roi_clear_all()
            self.assertEqual(w.rois, {})
            self.assertEqual(w._load_rois(), {})
        finally:
            w.close()

    def test_on_pane_roi_saves_and_clears(self):
        w = D.MainWindow()
        try:
            w._on_pane_roi('camera2', (0.2, 0.2, 0.4, 0.4))
            self.assertIn('camera2', w.rois)
            self.assertIn('camera2', w._load_rois())
            w._on_pane_roi('camera2', None)
            self.assertNotIn('camera2', w.rois)
            self.assertNotIn('camera2', w._load_rois())
        finally:
            w.close()


if __name__ == '__main__':
    unittest.main()
