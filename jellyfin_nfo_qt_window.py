import re
import shutil
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal, QSize, QSizeF, QPoint, QRect, QRectF, QEvent, QObject, QRunnable, QThreadPool, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QImage, QImageReader, QMovie, QPainter, QPainterPath, QPen, QPixmap, QRegion
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QTreeWidget,
    QTreeWidgetItem,
    QLayout,
    QLayoutItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSizePolicy,
    QStyle,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QWidgetItem,
)

from jellyfin_extras_rules import AUDIO_EXTS, VIDEO_EXTS, build_extra_target_name, target_supports_multiple
from jellyfin_nfo_core import (
    ALL_TAGS,
    COMMON_WRITABLE,
    MULTI_VALUE_TAGS,
    NfoItem,
    WRITABLE_BY_MEDIA_TYPE,
    apply_artwork_files,
    collect_nfo_items,
    parse_nfo_fields,
    split_multi_values,
    validate_edit_values,
    write_nfo_fields,
)
from jellyfin_nfo_qt_layout import build_media_target_card, build_ui
from jellyfin_nfo_qt_services import bind_network_services_methods
from jellyfin_nfo_qt_video_dialogs import bind_video_dialog_methods
from jellyfin_nfo_qt_scan_tree import bind_scan_tree_methods
from jellyfin_nfo_qt_session_pg import bind_session_pg_methods

SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}

try:
    from PIL import Image, ImageSequence

    HAS_PIL = True
except Exception:
    HAS_PIL = False


class _ScanSignals(QObject):
    finished = Signal(int, object, str)
    progress = Signal(int, str, str, str, int, int)


class _ScanWorker(QRunnable):
    def __init__(self, token: int, paths: tuple[Path, ...]):
        super().__init__()
        self.token = token
        self.paths = paths
        self.signals = _ScanSignals()

    def run(self):
        try:
            roots = sorted({p.resolve() for p in self.paths}, key=lambda p: str(p).lower())
            if not roots:
                self.signals.finished.emit(self.token, [], "")
                return
            all_items: list[NfoItem] = []
            for root in roots:
                root_text = str(root)
                self.signals.progress.emit(self.token, root_text, "start", root_text, 0, 0)
                try:
                    one_items = collect_nfo_items(
                        {root},
                        lambda cur_dir, scanned, total, rt=root_text: self.signals.progress.emit(
                            self.token, rt, "scan", cur_dir, scanned, total
                        ),
                        True,
                        1,
                    )
                    all_items.extend(one_items)
                    self.signals.progress.emit(self.token, root_text, "done", root_text, 0, 0)
                except Exception as exc:
                    self.signals.progress.emit(self.token, root_text, "error", str(exc), 0, 0)
            uniq: dict[str, NfoItem] = {}
            for item in all_items:
                uniq[str(item.path).casefold()] = item
            items = list(uniq.values())
            items.sort(key=lambda i: (i.media_type, str(i.path).lower()))
            self.signals.finished.emit(self.token, items, "")
        except Exception as exc:
            self.signals.finished.emit(self.token, [], str(exc))


class _AsyncSignals(QObject):
    finished = Signal(object, str)


class _AsyncWorker(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = _AsyncSignals()

    def run(self):
        try:
            result = self.fn()
            self.signals.finished.emit(result, "")
        except Exception as exc:
            self.signals.finished.emit(None, str(exc))


class _ClickableFrame(QFrame):
    clicked = Signal(object)
    double_clicked = Signal(object)

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self._key = key

    def mousePressEvent(self, event):
        self.clicked.emit((self._key, event.modifiers()))
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit(self._key)
        super().mouseDoubleClickEvent(event)


class _VideoPreviewWidget(QGraphicsView):
    """QGraphicsView + QGraphicsVideoItem — 子控件可正常透明合成。"""
    clicked = Signal(object)
    double_clicked = Signal()
    entered = Signal()
    left = Signal()
    moved = Signal()
    resized = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._video_item = QGraphicsVideoItem()
        self._scene.addItem(self._video_item)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet("QGraphicsView{background:black;border:none;}")
        self._video_item.nativeSizeChanged.connect(self._fit_video)

    @property
    def video_item(self) -> QGraphicsVideoItem:
        return self._video_item

    def _fit_video(self):
        ns = self._video_item.nativeSize()
        if ns.isEmpty():
            return
        vw, vh = self.viewport().width(), self.viewport().height()
        scale = min(vw / ns.width(), vh / ns.height()) if ns.width() > 0 and ns.height() > 0 else 1.0
        sw, sh = ns.width() * scale, ns.height() * scale
        self._video_item.setSize(QSizeF(sw, sh))
        self._video_item.setPos((vw - sw) / 2, (vh - sh) / 2)
        self.setSceneRect(0, 0, vw, vh)

    def mousePressEvent(self, event):
        self.clicked.emit(event.modifiers())
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)

    def enterEvent(self, event):
        self.entered.emit()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.left.emit()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        self.moved.emit()
        super().mouseMoveEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_video()
        self.resized.emit()


class _VideoPreviewLabel(QLabel):
    entered = Signal()
    left = Signal()
    moved = Signal()
    resized = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAlignment(Qt.AlignCenter)

    def enterEvent(self, event):
        self.entered.emit()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.left.emit()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        self.moved.emit()
        super().mouseMoveEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()


class _VideoPlayBtn(QWidget):
    clicked = Signal()
    entered = Signal()
    left = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._playing = False
        self.setFixedSize(36, 36)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def set_playing(self, playing: bool):
        self._playing = bool(playing)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 80))
        p.drawRoundedRect(self.rect(), 8, 8)
        p.setBrush(QColor(255, 255, 255, 220))
        cx, cy = self.width() // 2, self.height() // 2
        if self._playing:
            p.drawRoundedRect(QRect(cx - 6, cy - 7, 4, 14), 1, 1)
            p.drawRoundedRect(QRect(cx + 2, cy - 7, 4, 14), 1, 1)
        else:
            p.drawPolygon([QPoint(cx - 5, cy - 8), QPoint(cx - 5, cy + 8), QPoint(cx + 9, cy)])

    def mousePressEvent(self, event):
        self.clicked.emit()

    def enterEvent(self, event):
        self.entered.emit()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.left.emit()
        super().leaveEvent(event)


class _VideoProgressBar(QWidget):
    seekRequested = Signal(int)
    entered = Signal()
    left = Signal()
    moved = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self._position_ms = 0
        self._duration_ms = 0
        self._dragging = False
        self.setFixedHeight(24)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def set_state(self, position_ms: int, duration_ms: int):
        self._position_ms = max(0, int(position_ms))
        self._duration_ms = max(0, int(duration_ms))
        self.update()

    def _pos_to_ms(self, x: int) -> int:
        pad = 6
        w = max(1, self.width() - pad * 2)
        t = max(0.0, min(1.0, (x - pad) / float(w)))
        return int(round(t * max(0, self._duration_ms)))

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 80))
        p.drawRoundedRect(self.rect(), 8, 8)
        pad = 6
        track_y = self.height() // 2 - 2
        track_w = max(1, self.width() - pad * 2)
        track = QRect(pad, track_y, track_w, 4)
        p.setBrush(QColor(255, 255, 255, 45))
        p.drawRoundedRect(track, 2, 2)
        ratio = 0.0 if self._duration_ms <= 0 else max(0.0, min(1.0, self._position_ms / float(self._duration_ms)))
        filled_w = int(round(track_w * ratio))
        if filled_w > 0:
            p.setBrush(QColor(62, 166, 255, 190))
            p.drawRoundedRect(QRect(pad, track_y, filled_w, 4), 2, 2)
        knob_x = pad + filled_w
        p.setBrush(QColor(62, 166, 255, 235))
        p.drawEllipse(QPoint(knob_x, self.height() // 2), 6, 6)

    def mousePressEvent(self, event):
        self._dragging = True
        self.seekRequested.emit(self._pos_to_ms(int(event.position().x())))

    def mouseMoveEvent(self, event):
        self.moved.emit()
        if self._dragging:
            self.seekRequested.emit(self._pos_to_ms(int(event.position().x())))

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False
            self.seekRequested.emit(self._pos_to_ms(int(event.position().x())))

    def enterEvent(self, event):
        self.entered.emit()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.left.emit()
        super().leaveEvent(event)


class _VideoTimeLabel(QWidget):
    entered = Signal()
    left = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = "00:00 / 00:00"
        self.setFixedHeight(24)
        self.setMinimumWidth(110)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def set_text(self, text: str):
        self._text = str(text)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 80))
        p.drawRoundedRect(self.rect(), 8, 8)
        p.setPen(QColor(255, 255, 255, 220))
        p.drawText(self.rect().adjusted(6, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft, self._text)

    def enterEvent(self, event):
        self.entered.emit()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.left.emit()
        super().leaveEvent(event)


class _RangeSlider(QWidget):
    rangeChanged = Signal(int, int, str)
    playheadChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._min = 0
        self._max = 1000
        self._start = 0
        self._end = 1000
        self._playhead = 0
        self._min_span = 500
        self._active = ""
        self.setMinimumHeight(28)
        self.setMouseTracking(True)

    def setRange(self, minimum: int, maximum: int):
        self._min = int(minimum)
        self._max = max(self._min + 1, int(maximum))
        self._start = max(self._min, min(self._start, self._max))
        self._end = max(self._start, min(self._end, self._max))
        self._playhead = max(self._min, min(self._playhead, self._max))
        self.update()

    def setValues(self, start: int, end: int):
        s = max(self._min, min(int(start), self._max))
        e = max(self._min, min(int(end), self._max))
        if e - s < self._min_span:
            e = min(self._max, s + self._min_span)
            s = max(self._min, e - self._min_span)
        self._start, self._end = s, e
        self.update()

    def values(self) -> tuple[int, int]:
        return (self._start, self._end)

    def playhead(self) -> int:
        return self._playhead

    def setPlayhead(self, value: int, emit_signal: bool = False):
        self._playhead = max(self._min, min(int(value), self._max))
        self.update()
        if emit_signal:
            self.playheadChanged.emit(self._playhead)

    def setMinimumSpan(self, span: int):
        self._min_span = max(1, int(span))

    def _track_rect(self) -> QRect:
        return QRect(12, self.height() // 2 - 3, max(20, self.width() - 24), 6)

    def _val_to_x(self, val: int) -> int:
        tr = self._track_rect()
        k = (val - self._min) / max(1, (self._max - self._min))
        return tr.left() + int(k * tr.width())

    def _x_to_val(self, x: int) -> int:
        tr = self._track_rect()
        k = (x - tr.left()) / max(1, tr.width())
        k = max(0.0, min(1.0, k))
        return self._min + int(round(k * (self._max - self._min)))

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        tr = self._track_rect()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#4a4f57"))
        p.drawRoundedRect(tr, 3, 3)
        x1 = self._val_to_x(self._start)
        x2 = self._val_to_x(self._end)
        sel = QRect(min(x1, x2), tr.top(), abs(x2 - x1), tr.height())
        p.setBrush(QColor("#3ea6ff"))
        p.drawRoundedRect(sel, 3, 3)
        for x in (x1, x2):
            p.setBrush(QColor("#f4f7fa"))
            p.setPen(QPen(QColor("#7c8a99"), 1))
            p.drawEllipse(QPoint(x, tr.center().y()), 7, 7)
        xp = self._val_to_x(self._playhead)
        p.setPen(QPen(QColor("#ffd54f"), 2))
        p.drawLine(xp, tr.top() - 6, xp, tr.bottom() + 6)
        p.setBrush(QColor("#ffd54f"))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPoint(xp, tr.center().y()), 3, 3)
        p.end()

    def mousePressEvent(self, event):
        x = int(event.position().x())
        xs = self._val_to_x(self._start)
        xe = self._val_to_x(self._end)
        if min(abs(x - xs), abs(x - xe)) <= 10:
            self._active = "start" if abs(x - xs) <= abs(x - xe) else "end"
        else:
            self._active = "playhead"
        self.mouseMoveEvent(event)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not self._active:
            super().mouseMoveEvent(event)
            return
        v = self._x_to_val(int(event.position().x()))
        if self._active == "start":
            self._start = min(v, self._end - self._min_span)
            self._start = max(self._min, self._start)
        elif self._active == "end":
            self._end = max(v, self._start + self._min_span)
            self._end = min(self._max, self._end)
        else:
            self._playhead = max(self._min, min(v, self._max))
        self.update()
        if self._active in {"start", "end"}:
            self.rangeChanged.emit(self._start, self._end, self._active)
        else:
            self.playheadChanged.emit(self._playhead)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._active = ""
        super().mouseReleaseEvent(event)


class _VideoCropOverlay(QWidget):
    cropChanged = Signal(str)
    entered = Signal()
    left = Signal()
    moved = Signal()

    def __init__(self, video_widget: QWidget, src_w: int, src_h: int, parent=None):
        super().__init__(parent or video_widget)
        self._video_widget = video_widget
        self._src_w = max(1, int(src_w))
        self._src_h = max(1, int(src_h))
        self._enabled = False
        self._crop_expr = "iw:ih:0:0"
        self._crop = [0.0, 0.0, float(self._src_w), float(self._src_h)]  # x,y,w,h (source space)
        self._aspect_ratio: float | None = None
        self._drag_mode = ""
        self._drag_start = (0.0, 0.0)
        self._drag_crop = self._crop.copy()
        self._edge_tol_px = 10
        self._min_crop_size = 12.0
        self.setStyleSheet("background: transparent;")
        self.setMouseTracking(True)
        self._video_widget.installEventFilter(self)
        self._sync_geometry()

    def set_enabled(self, on: bool):
        self._enabled = bool(on)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, not self._enabled)
        self.update()

    def set_crop_expr(self, expr: str):
        self._crop_expr = (expr or "").strip()
        parts = [p.strip() for p in self._crop_expr.split(":")]
        if len(parts) == 4:
            try:
                w = max(self._min_crop_size, float(parts[0]))
                h = max(self._min_crop_size, float(parts[1]))
                x = max(0.0, float(parts[2]))
                y = max(0.0, float(parts[3]))
                w = min(w, float(self._src_w))
                h = min(h, float(self._src_h))
                x = min(x, float(self._src_w) - w)
                y = min(y, float(self._src_h) - h)
                self._crop = [x, y, w, h]
            except Exception:
                pass
        self.update()

    def set_aspect_ratio(self, ratio: float | None):
        self._aspect_ratio = ratio if ratio and ratio > 0 else None

    def _current_expr(self) -> str:
        x, y, w, h = self._crop
        return f"{int(round(w))}:{int(round(h))}:{int(round(x))}:{int(round(y))}"

    def _emit_crop_changed(self):
        expr = self._current_expr()
        self._crop_expr = expr
        self.cropChanged.emit(expr)

    def _sync_geometry(self):
        parent = self.parentWidget()
        if parent is None:
            self.setGeometry(self._video_widget.rect())
        else:
            top_left = self._video_widget.mapTo(parent, QPoint(0, 0))
            self.setGeometry(QRect(top_left, self._video_widget.size()))
        self.raise_()

    def eventFilter(self, obj, event):
        if obj is self._video_widget and event.type() in {QEvent.Resize, QEvent.Move, QEvent.Show}:
            self._sync_geometry()
        return super().eventFilter(obj, event)

    def _content_rect(self) -> QRect:
        ww, wh = max(1, self.width()), max(1, self.height())
        src_ratio = self._src_w / max(1, self._src_h)
        widget_ratio = ww / max(1, wh)
        if src_ratio >= widget_ratio:
            draw_w = ww
            draw_h = int(draw_w / src_ratio)
            off_x, off_y = 0, (wh - draw_h) // 2
        else:
            draw_h = wh
            draw_w = int(draw_h * src_ratio)
            off_x, off_y = (ww - draw_w) // 2, 0
        return QRect(off_x, off_y, max(1, draw_w), max(1, draw_h))

    def _src_to_widget_rect(self, crop: list[float]) -> QRect:
        x, y, w, h = crop
        c = self._content_rect()
        rx = c.x() + int(x * c.width() / self._src_w)
        ry = c.y() + int(y * c.height() / self._src_h)
        rw = int(w * c.width() / self._src_w)
        rh = int(h * c.height() / self._src_h)
        return QRect(rx, ry, max(2, rw), max(2, rh)).intersected(c)

    def _widget_to_src(self, p: QPoint) -> tuple[float, float]:
        c = self._content_rect()
        px = max(c.left(), min(p.x(), c.right()))
        py = max(c.top(), min(p.y(), c.bottom()))
        sx = (px - c.x()) * self._src_w / max(1, c.width())
        sy = (py - c.y()) * self._src_h / max(1, c.height())
        return (float(sx), float(sy))

    def _hit_mode(self, p: QPoint) -> str:
        r = self._src_to_widget_rect(self._crop)
        if not r.contains(p):
            return ""
        near_l = abs(p.x() - r.left()) <= self._edge_tol_px
        near_r = abs(p.x() - r.right()) <= self._edge_tol_px
        near_t = abs(p.y() - r.top()) <= self._edge_tol_px
        near_b = abs(p.y() - r.bottom()) <= self._edge_tol_px
        if near_l and near_t:
            return "resize_tl"
        if near_r and near_t:
            return "resize_tr"
        if near_l and near_b:
            return "resize_bl"
        if near_r and near_b:
            return "resize_br"
        if near_l:
            return "resize_l"
        if near_r:
            return "resize_r"
        if near_t:
            return "resize_t"
        if near_b:
            return "resize_b"
        return "move"

    def _apply_drag(self, sx: float, sy: float):
        x0, y0, w0, h0 = self._drag_crop
        x1, y1, x2, y2 = x0, y0, x0 + w0, y0 + h0
        dsx = sx - self._drag_start[0]
        dsy = sy - self._drag_start[1]
        m = self._drag_mode
        if m == "move":
            x1 += dsx
            y1 += dsy
            x2 += dsx
            y2 += dsy
        elif m == "resize_tl":
            x1 += dsx
            y1 += dsy
        elif m == "resize_tr":
            x2 += dsx
            y1 += dsy
        elif m == "resize_bl":
            x1 += dsx
            y2 += dsy
        elif m == "resize_br":
            x2 += dsx
            y2 += dsy
        elif m == "resize_l":
            x1 += dsx
        elif m == "resize_r":
            x2 += dsx
        elif m == "resize_t":
            y1 += dsy
        elif m == "resize_b":
            y2 += dsy

        if x2 - x1 < self._min_crop_size:
            if "l" in m:
                x1 = x2 - self._min_crop_size
            else:
                x2 = x1 + self._min_crop_size
        if y2 - y1 < self._min_crop_size:
            if "t" in m:
                y1 = y2 - self._min_crop_size
            else:
                y2 = y1 + self._min_crop_size

        # 锁定比例：缩放时强制保持当前比例；free 模式不限制。
        if self._aspect_ratio and m != "move":
            ratio = float(self._aspect_ratio)
            w = max(self._min_crop_size, x2 - x1)
            h = max(self._min_crop_size, y2 - y1)
            if (w / h) >= ratio:
                w = h * ratio
            else:
                h = w / ratio

            if "l" in m and "r" not in m:
                x1 = x2 - w
            elif "r" in m and "l" not in m:
                x2 = x1 + w
            else:
                cx = (x1 + x2) / 2.0
                x1 = cx - (w / 2.0)
                x2 = cx + (w / 2.0)

            if "t" in m and "b" not in m:
                y1 = y2 - h
            elif "b" in m and "t" not in m:
                y2 = y1 + h
            else:
                cy = (y1 + y2) / 2.0
                y1 = cy - (h / 2.0)
                y2 = cy + (h / 2.0)

        if m == "move":
            w = x2 - x1
            h = y2 - y1
            x1 = min(max(0.0, x1), self._src_w - w)
            y1 = min(max(0.0, y1), self._src_h - h)
            x2 = x1 + w
            y2 = y1 + h
        else:
            x1 = min(max(0.0, x1), self._src_w - self._min_crop_size)
            y1 = min(max(0.0, y1), self._src_h - self._min_crop_size)
            x2 = min(max(self._min_crop_size, x2), self._src_w)
            y2 = min(max(self._min_crop_size, y2), self._src_h)

        # 比例约束后做一次边界平移，尽量保持当前尺寸。
        w = x2 - x1
        h = y2 - y1
        if x1 < 0:
            x2 -= x1
            x1 = 0.0
        if y1 < 0:
            y2 -= y1
            y1 = 0.0
        if x2 > self._src_w:
            shift = x2 - self._src_w
            x1 -= shift
            x2 = float(self._src_w)
        if y2 > self._src_h:
            shift = y2 - self._src_h
            y1 -= shift
            y2 = float(self._src_h)
        x1 = max(0.0, min(x1, self._src_w - w))
        y1 = max(0.0, min(y1, self._src_h - h))
        x2 = x1 + w
        y2 = y1 + h

        self._crop = [x1, y1, x2 - x1, y2 - y1]

    def _draw_rect(self) -> QRect:
        return self._src_to_widget_rect(self._crop)

    def mousePressEvent(self, event):
        if (not self._enabled) or event.button() != Qt.LeftButton:
            return
        self._drag_mode = self._hit_mode(event.position().toPoint())
        if not self._drag_mode:
            return
        self._drag_start = self._widget_to_src(event.position().toPoint())
        self._drag_crop = self._crop.copy()
        self.moved.emit()
        self.update()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        p = event.position().toPoint()
        self.moved.emit()
        if self._enabled and self._drag_mode:
            sx, sy = self._widget_to_src(p)
            self._apply_drag(sx, sy)
            self._emit_crop_changed()
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_mode:
            self._emit_crop_changed()
        self._drag_mode = ""
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        self.entered.emit()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.left.emit()
        super().leaveEvent(event)

    def paintEvent(self, _event):
        if not self._enabled:
            return
        rect = self._draw_rect()
        if rect.isEmpty():
            return
        if rect.width() > 4 and rect.height() > 4:
            rect = rect.adjusted(1, 1, -1, -1)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        shade = QColor(0, 0, 0, 95)
        full = self.rect()
        if rect != full:
            p.fillRect(QRect(full.left(), full.top(), full.width(), max(0, rect.top() - full.top())), shade)
            p.fillRect(QRect(full.left(), rect.bottom(), full.width(), max(0, full.bottom() - rect.bottom())), shade)
            p.fillRect(QRect(full.left(), rect.top(), max(0, rect.left() - full.left()), rect.height()), shade)
            p.fillRect(QRect(rect.right(), rect.top(), max(0, full.right() - rect.right()), rect.height()), shade)
        p.setPen(QPen(QColor("#3ea6ff"), 2))
        draw_rect = rect
        if draw_rect == full:
            draw_rect = draw_rect.adjusted(6, 6, -6, -6)
        p.drawRect(draw_rect)
        p.end()


class _ImageThumbLabel(QLabel):
    resized = Signal()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()


class _ImageCropCanvas(QWidget):
    selection_changed = Signal()

    def __init__(self, source_path: Path, parent=None):
        super().__init__(parent)
        self.source_path = source_path
        self._pix = self._load_preview_pixmap(source_path)
        self._img_w = max(1, self._pix.width())
        self._img_h = max(1, self._pix.height())
        self._img_rect = QRect(0, 0, 1, 1)
        self._crop = [0.0, 0.0, float(self._img_w), float(self._img_h)]
        self._aspect: float | None = 16.0 / 9.0
        self._mode = ""
        self._start = (0.0, 0.0)
        self._start_crop = self._crop.copy()
        self._edge_tol_px = 10
        self.setMouseTracking(True)
        self.setMinimumSize(700, 460)

    def _load_preview_pixmap(self, source_path: Path) -> QPixmap:
        # 优先走 Pillow 解码首帧，规避 Qt 对部分动图/特殊编码文件的崩溃风险。
        if HAS_PIL:
            try:
                img = Image.open(source_path)
                frames = ImageSequence.Iterator(img)
                first = next(frames, img).convert("RGBA")
                raw = first.tobytes("raw", "RGBA")
                qimg = QImage(raw, first.width, first.height, QImage.Format_RGBA8888).copy()
                pix = QPixmap.fromImage(qimg)
                if not pix.isNull():
                    return pix
            except Exception:
                pass
        return QPixmap(str(source_path))

    def set_aspect_ratio(self, ratio: float | None):
        self._aspect = ratio if ratio and ratio > 0 else None
        self._reset_crop_to_ratio()
        self.selection_changed.emit()
        self.update()

    def _reset_crop_to_ratio(self):
        if not self._aspect:
            self._crop = [0.0, 0.0, float(self._img_w), float(self._img_h)]
            return
        img_ratio = self._img_w / self._img_h if self._img_h > 0 else 1.0
        if img_ratio >= self._aspect:
            h = float(self._img_h)
            w = h * self._aspect
        else:
            w = float(self._img_w)
            h = w / self._aspect
        x1 = (self._img_w - w) / 2.0
        y1 = (self._img_h - h) / 2.0
        self._crop = [x1, y1, x1 + w, y1 + h]

    def get_crop_box(self) -> tuple[int, int, int, int]:
        x1, y1, x2, y2 = self._crop
        x1 = int(round(max(0, min(x1, self._img_w - 1))))
        y1 = int(round(max(0, min(y1, self._img_h - 1))))
        x2 = int(round(max(x1 + 1, min(x2, self._img_w))))
        y2 = int(round(max(y1 + 1, min(y2, self._img_h))))
        return (x1, y1, x2, y2)

    def _image_to_widget(self, x: float, y: float) -> tuple[int, int]:
        if self._img_w <= 0 or self._img_h <= 0:
            return (0, 0)
        rx = self._img_rect.x() + (x / self._img_w) * self._img_rect.width()
        ry = self._img_rect.y() + (y / self._img_h) * self._img_rect.height()
        return (int(round(rx)), int(round(ry)))

    def _widget_to_image(self, pos: QPoint) -> tuple[float, float]:
        if self._img_rect.width() <= 1 or self._img_rect.height() <= 1:
            return (0.0, 0.0)
        px = min(max(pos.x(), self._img_rect.left()), self._img_rect.right())
        py = min(max(pos.y(), self._img_rect.top()), self._img_rect.bottom())
        x = (px - self._img_rect.x()) * self._img_w / max(1, self._img_rect.width())
        y = (py - self._img_rect.y()) * self._img_h / max(1, self._img_rect.height())
        return (float(x), float(y))

    def _crop_contains(self, x: float, y: float) -> bool:
        x1, y1, x2, y2 = self._crop
        return x1 <= x <= x2 and y1 <= y <= y2

    def _edge_tol_image(self) -> tuple[float, float]:
        if self._img_rect.width() <= 1 or self._img_rect.height() <= 1:
            return (6.0, 6.0)
        tx = self._edge_tol_px * self._img_w / self._img_rect.width()
        ty = self._edge_tol_px * self._img_h / self._img_rect.height()
        return (max(3.0, tx), max(3.0, ty))

    def _hit_test_mode(self, x: float, y: float) -> str:
        x1, y1, x2, y2 = self._crop
        tx, ty = self._edge_tol_image()
        near_left = abs(x - x1) <= tx
        near_right = abs(x - x2) <= tx
        near_top = abs(y - y1) <= ty
        near_bottom = abs(y - y2) <= ty
        if near_left and near_top:
            return "resize_nw"
        if near_right and near_top:
            return "resize_ne"
        if near_left and near_bottom:
            return "resize_sw"
        if near_right and near_bottom:
            return "resize_se"
        if self._crop_contains(x, y):
            return "move"
        return "new"

    def _apply_corner_resize(self, mode: str, x: float, y: float):
        sx1, sy1, sx2, sy2 = self._start_crop
        if mode == "resize_nw":
            ax, ay = sx2, sy2
        elif mode == "resize_ne":
            ax, ay = sx1, sy2
        elif mode == "resize_sw":
            ax, ay = sx2, sy1
        else:
            ax, ay = sx1, sy1

        dx = x - ax
        dy = y - ay
        sign_x = -1.0 if mode in {"resize_nw", "resize_sw"} else 1.0
        sign_y = -1.0 if mode in {"resize_nw", "resize_ne"} else 1.0

        max_w = ax if sign_x < 0 else (float(self._img_w) - ax)
        max_h = ay if sign_y < 0 else (float(self._img_h) - ay)
        min_size = 2.0

        if self._aspect:
            pointer_w = abs(dx)
            pointer_h = abs(dy)
            desired_w = max(pointer_w, pointer_h * self._aspect)
            cap_w = min(max_w, max_h * self._aspect)
            width = min(max(desired_w, min_size), max(min_size, cap_w))
            height = width / self._aspect
            nx = ax + sign_x * width
            ny = ay + sign_y * height
        else:
            width = min(max(abs(dx), min_size), max(min_size, max_w))
            height = min(max(abs(dy), min_size), max(min_size, max_h))
            nx = ax + sign_x * width
            ny = ay + sign_y * height

        x1 = min(ax, nx)
        y1 = min(ay, ny)
        x2 = max(ax, nx)
        y2 = max(ay, ny)
        self._crop = [x1, y1, x2, y2]

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._pix.isNull():
            self._img_rect = QRect(0, 0, 1, 1)
            return
        # 不再额外预留固定黑边，让图片尽量铺满画布。
        avail_w = max(1, self.width())
        avail_h = max(1, self.height())
        scale = min(avail_w / self._img_w, avail_h / self._img_h)
        draw_w = max(1, int(round(self._img_w * scale)))
        draw_h = max(1, int(round(self._img_h * scale)))
        x = (self.width() - draw_w) // 2
        y = (self.height() - draw_h) // 2
        self._img_rect = QRect(x, y, draw_w, draw_h)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#f2f4f7"))
        if self._pix.isNull():
            p.setPen(QColor("#d0d7de"))
            p.drawText(self.rect(), Qt.AlignCenter, "图片加载失败")
            return
        p.drawPixmap(self._img_rect, self._pix)

        x1, y1 = self._image_to_widget(self._crop[0], self._crop[1])
        x2, y2 = self._image_to_widget(self._crop[2], self._crop[3])
        crop_rect = QRect(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))

        shade = QColor(0, 0, 0, 130)
        p.fillRect(QRect(self._img_rect.left(), self._img_rect.top(), self._img_rect.width(), max(0, crop_rect.top() - self._img_rect.top())), shade)
        p.fillRect(QRect(self._img_rect.left(), crop_rect.bottom(), self._img_rect.width(), max(0, self._img_rect.bottom() - crop_rect.bottom())), shade)
        p.fillRect(QRect(self._img_rect.left(), crop_rect.top(), max(0, crop_rect.left() - self._img_rect.left()), crop_rect.height()), shade)
        p.fillRect(QRect(crop_rect.right(), crop_rect.top(), max(0, self._img_rect.right() - crop_rect.right()), crop_rect.height()), shade)

        p.setPen(QPen(QColor("#4ea1ff"), 2))
        p.drawRect(crop_rect)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        x, y = self._widget_to_image(event.position().toPoint())
        self._start = (x, y)
        self._start_crop = self._crop.copy()
        self._mode = self._hit_test_mode(x, y)
        if self._mode == "new":
            self._crop = [x, y, x + 1.0, y + 1.0]
        self.update()

    def mouseMoveEvent(self, event):
        if not self._mode:
            return
        x, y = self._widget_to_image(event.position().toPoint())
        if self._mode == "move":
            sx, sy = self._start
            dx = x - sx
            dy = y - sy
            x1, y1, x2, y2 = self._start_crop
            w = x2 - x1
            h = y2 - y1
            nx1 = min(max(0.0, x1 + dx), self._img_w - w)
            ny1 = min(max(0.0, y1 + dy), self._img_h - h)
            self._crop = [nx1, ny1, nx1 + w, ny1 + h]
        elif self._mode.startswith("resize_"):
            self._apply_corner_resize(self._mode, x, y)
        else:
            sx, sy = self._start
            nx1 = min(sx, x)
            ny1 = min(sy, y)
            nx2 = max(sx, x)
            ny2 = max(sy, y)
            if self._aspect:
                w = max(1.0, nx2 - nx1)
                h = max(1.0, ny2 - ny1)
                if (w / h) >= self._aspect:
                    h = w / self._aspect
                else:
                    w = h * self._aspect
                if x >= sx:
                    nx2 = min(self._img_w, nx1 + w)
                else:
                    nx1 = max(0.0, nx2 - w)
                if y >= sy:
                    ny2 = min(self._img_h, ny1 + h)
                else:
                    ny1 = max(0.0, ny2 - h)
            self._crop = [max(0.0, nx1), max(0.0, ny1), min(float(self._img_w), nx2), min(float(self._img_h), ny2)]
        self.selection_changed.emit()
        self.update()

    def mouseReleaseEvent(self, _event):
        self._mode = ""

    def mouseDoubleClickEvent(self, _event):
        self._reset_crop_to_ratio()
        self.selection_changed.emit()
        self.update()


class _NoShadowPopupComboBox(QComboBox):
    """修复 Win 下拉弹层右下角灰边（系统阴影/外壳偏移）。"""

    def paintEvent(self, event):
        super().paintEvent(event)
        # 手绘下拉三角，避免不同平台对 QSS down-arrow 的不一致渲染。
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#4f6277"))
        cx = self.width() - 13
        cy = self.height() // 2 + 1
        p.drawPolygon([QPoint(cx - 5, cy + 3), QPoint(cx + 5, cy + 3), QPoint(cx, cy - 4)])

    def showPopup(self):
        super().showPopup()
        view = self.view()
        if view is None:
            return
        popup = view.window()
        if popup is None:
            return
        popup.setWindowFlag(Qt.Popup, True)
        popup.setWindowFlag(Qt.FramelessWindowHint, True)
        popup.setWindowFlag(Qt.NoDropShadowWindowHint, True)
        popup.setAttribute(Qt.WA_TranslucentBackground, False)
        popup.setAttribute(Qt.WA_StyledBackground, True)
        popup.setAutoFillBackground(True)
        # 重新 show 让窗口标记生效。
        popup.show()
        # 与输入框左边对齐，避免外壳偏右导致漏边。
        anchor = self.mapToGlobal(QPoint(0, self.height()))
        popup.move(anchor.x(), popup.y())
        # 强制圆角裁剪，彻底去掉右下角灰色残留。
        rect = popup.rect().adjusted(0, 0, -1, -1)
        if rect.width() > 0 and rect.height() > 0:
            path = QPainterPath()
            path.addRoundedRect(QRectF(rect), 12, 12)
            popup.setMask(QRegion(path.toFillPolygon().toPolygon()))


class _QtImageCropDialog(QDialog):
    def __init__(self, parent, source_path: Path, target_name: str):
        super().__init__(parent)
        self.source_path = source_path
        self.target_name = target_name
        self.result_path: Path | None = None
        self.setWindowTitle(f"{target_name} 图片裁切")
        self.resize(1120, 700)

        root = QVBoxLayout(self)
        self.canvas = _ImageCropCanvas(source_path, self)
        root.addWidget(self.canvas, 1)
        img_ratio = self.canvas._img_w / max(1, self.canvas._img_h)

        row = QHBoxLayout()
        row.addWidget(QLabel("比例"))
        self.aspect_combo = _NoShadowPopupComboBox()
        self.aspect_combo.setEditable(True)
        self.aspect_combo.setFixedWidth(120)
        self.aspect_combo.setInsertPolicy(QComboBox.NoInsert)
        self.aspect_combo.setDuplicatesEnabled(False)
        popup_view = QListView(self.aspect_combo)
        popup_view.setObjectName("ratioPopupView")
        popup_view.setFrameShape(QFrame.NoFrame)
        popup_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        popup_view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        popup_view.setAttribute(Qt.WA_StyledBackground, True)
        popup_view.viewport().setObjectName("ratioPopupViewport")
        popup_view.viewport().setAttribute(Qt.WA_StyledBackground, True)
        self.aspect_combo.setView(popup_view)
        self.aspect_combo.setStyleSheet(
            "QComboBox{"
            "border:1px solid #b9c4d0;"
            "border-radius:8px;"
            "padding:4px 30px 4px 10px;"
            "background:#f7f9fc;"
            "color:#1f2d3d;"
            "}"
            "QComboBox:hover{border-color:#9fb4cb;}"
            "QComboBox:focus{border-color:#82aee8;}"
            "QComboBox::drop-down{"
            "subcontrol-origin:padding;"
            "subcontrol-position:top right;"
            "width:24px;"
            "border-left:1px solid #d3dbe5;"
            "border-top-right-radius:8px;"
            "border-bottom-right-radius:8px;"
            "background:#eef3f9;"
            "}"
            "QComboBox::down-arrow{"
            "image:none;"
            "width:0px;height:0px;"
            "border:none;"
            "}"
            "QListView#ratioPopupView{"
            "outline:none;"
            "border:1px solid #b9c4d0;"
            "border-radius:12px;"
            "padding:3px;"
            "background:#ffffff;"
            "color:#1f2d3d;"
            "}"
            "QListView#ratioPopupView::viewport{"
            "border:none;"
            "border-radius:9px;"
            "background:#ffffff;"
            "}"
            "QListView#ratioPopupView::corner{"
            "background:#ffffff;"
            "}"
            "QListView#ratioPopupView::item{"
            "min-height:22px;"
            "padding:4px 8px;"
            "margin:1px 2px;"
            "border-radius:8px;"
            "background:#ffffff;"
            "color:#1f2d3d;"
            "}"
            "QListView#ratioPopupView::item:hover{"
            "background:#eaf2ff;"
            "color:#10243d;"
            "}"
            "QListView#ratioPopupView::item:selected{"
            "background:#dcecff;"
            "color:#10243d;"
            "}"
        )
        self.aspect_combo.addItems(["16:9", "9:16", "4:3", "3:4", "1:1", "2:3", "3:2", "21:9", "free"])
        self.aspect_combo.setCurrentText("16:9")
        # 清理历史残留空项，避免下拉出现“空白比例项”。
        for i in range(self.aspect_combo.count() - 1, -1, -1):
            if not self.aspect_combo.itemText(i).strip():
                self.aspect_combo.removeItem(i)
        row.addWidget(self.aspect_combo)
        reset_btn = QPushButton("重置")
        row.addWidget(reset_btn)
        self.box_label = QLabel("")
        row.addWidget(self.box_label, 1)
        apply_btn = QPushButton("应用裁切")
        close_btn = QPushButton("取消")
        row.addWidget(apply_btn)
        row.addWidget(close_btn)
        root.addLayout(row)

        reset_btn.clicked.connect(self._reset_crop)
        apply_btn.clicked.connect(self._apply_crop)
        close_btn.clicked.connect(self.reject)
        self.aspect_combo.activated.connect(lambda _i: self._apply_ratio_text())
        edit = self.aspect_combo.lineEdit()
        if edit is not None:
            edit.editingFinished.connect(self._apply_ratio_text)
        self.canvas.selection_changed.connect(self._refresh_box_label)
        self._apply_ratio_text()
        self._refresh_box_label()
        # 初始窗口按图片比例贴合，并限制在可用屏幕内，避免被任务栏遮挡。
        screen = self.screen() or (parent.screen() if parent is not None else None)
        if screen is not None:
            avail = screen.availableGeometry()
            max_canvas_w = max(760, avail.width() - 64)
            max_canvas_h = max(420, avail.height() - 160)
        else:
            max_canvas_w = 1120
            max_canvas_h = 700
        target_w = min(1120, max_canvas_w)
        canvas_h = int(target_w / max(0.1, img_ratio))
        if canvas_h > max_canvas_h:
            canvas_h = max_canvas_h
            target_w = int(canvas_h * max(0.1, img_ratio))
        self.resize(max(760, target_w), max(520, canvas_h + 88))

    def _apply_ratio_text(self):
        text = self.aspect_combo.currentText().strip()
        if not text or text in {"0", "free", "auto"}:
            # editable combobox 可能出现空文本；统一归一为 free，避免下拉显示空白项。
            if text != "free":
                self.aspect_combo.setCurrentText("free")
            self.canvas.set_aspect_ratio(None)
            return
        try:
            if ":" in text:
                a, b = text.split(":", 1)
                ratio = float(a.strip()) / float(b.strip())
            else:
                ratio = float(text)
            if ratio <= 0:
                raise ValueError("ratio<=0")
        except Exception:
            QMessageBox.warning(self, "比例错误", "请输入有效比例，如 16:9 / 2:3 / 1.7778")
            return
        self.canvas.set_aspect_ratio(ratio)

    def _reset_crop(self):
        self.canvas._reset_crop_to_ratio()
        self.canvas.selection_changed.emit()
        self.canvas.update()

    def _refresh_box_label(self):
        x1, y1, x2, y2 = self.canvas.get_crop_box()
        self.box_label.setText(f"裁切区域: ({x1},{y1}) - ({x2},{y2})  尺寸: {x2 - x1}x{y2 - y1}")

    def _apply_crop(self):
        x1, y1, x2, y2 = self.canvas.get_crop_box()
        try:
            cache_dir = Path(__file__).with_name(".nfo_image_cache")
            cache_dir.mkdir(parents=True, exist_ok=True)
            ext = self.source_path.suffix.lower() or ".png"
            out = cache_dir / f"{self.target_name}_cropped{ext}"
            if HAS_PIL:
                src = Image.open(self.source_path)
                frames = [frm.copy() for frm in ImageSequence.Iterator(src)]
                if not frames:
                    frames = [src.copy()]
                cropped = [frm.crop((x1, y1, x2, y2)) for frm in frames]
                duration = int(src.info.get("duration", 100))
                loop = int(src.info.get("loop", 0))
                if len(cropped) > 1 and ext in {".gif", ".png", ".webp"}:
                    cropped[0].save(
                        out,
                        save_all=True,
                        append_images=cropped[1:],
                        loop=loop,
                        duration=duration,
                    )
                else:
                    cropped[0].save(out)
            else:
                img = QImage(str(self.source_path))
                if img.isNull():
                    raise RuntimeError("图片读取失败")
                out_img = img.copy(x1, y1, x2 - x1, y2 - y1)
                if not out_img.save(str(out)):
                    raise RuntimeError("图片保存失败")
            self.result_path = out
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "裁切失败", f"保存裁切图片失败：{exc}")


class PathListWidget(QWidget):
    def __init__(self, media_kind: str = "image", parent=None):
        super().__init__(parent)
        self.media_kind = media_kind
        self._paths: list[str] = []
        self._selected: set[str] = set()
        self._cards: dict[str, _ClickableFrame] = {}
        self._image_thumbs: dict[str, QLabel] = {}
        self._image_pixmaps: dict[str, QPixmap] = {}
        self._image_movies_by_key: dict[str, QMovie] = {}
        self._movies: list[QMovie] = []
        self._players: list[QMediaPlayer] = []
        self._audios: list[QAudioOutput] = []
        self._image_ratio_cache: dict[str, float] = {}
        self._video_ratio_cache: dict[str, float] = {}
        self._async_image_workers: set[_AsyncWorker] = set()
        self._image_has_video_cards = False
        self._layout_rows = 1

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea{background:#f4f6f8;border:1px solid #cfd6de;border-radius:4px;}")
        self.host = QWidget()
        self.grid = QGridLayout(self.host)
        self.grid.setContentsMargins(1, 1, 1, 1)
        self.grid.setHorizontalSpacing(2)
        self.grid.setVerticalSpacing(2)
        self.scroll.setWidget(self.host)
        root.addWidget(self.scroll)
        self._sync_height()

    def _norm(self, p: str) -> str:
        # 避免 .resolve() 的 NAS I/O；casefold 已处理 Windows 大小写
        return str(Path(p)).casefold()

    def set_paths(self, paths: list[str]):
        seen: set[str] = set()
        cleaned: list[str] = []
        for one in paths:
            txt = one.strip()
            if not txt:
                continue
            key = self._norm(txt)
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(str(Path(txt)))
        self._paths = cleaned
        self._selected.clear()
        self._rebuild_cards()

    def get_paths(self) -> list[str]:
        return [x for x in self._paths if x.strip()]

    def get_selected_paths(self) -> list[str]:
        if not self._selected:
            return []
        out: list[str] = []
        for p in self._paths:
            if self._norm(p) in self._selected:
                out.append(p)
        return out

    def append_path(self, path: str):
        if not path.strip():
            return
        norm = self._norm(path)
        if norm in {self._norm(x) for x in self._paths}:
            return
        self._paths.append(str(Path(path)))
        self._rebuild_cards()

    def remove_selected(self):
        if not self._selected:
            return
        self._paths = [p for p in self._paths if self._norm(p) not in self._selected]
        self._selected.clear()
        self._rebuild_cards()

    def stop_all_media(self):
        # 释放预览播放器占用，避免写入阶段删除/覆盖媒体文件失败（WinError 32/5）
        for player in self._players:
            try:
                player.stop()
                player.setSource(QUrl())
            except Exception:
                pass
        for audio in self._audios:
            try:
                audio.setMuted(True)
            except Exception:
                pass

    def keyPressEvent(self, event):
        if event.key() in {Qt.Key_Backspace, Qt.Key_Delete}:
            self.remove_selected()
            event.accept()
            return
        super().keyPressEvent(event)

    def _open_path(self, key: str):
        target = None
        for p in self._paths:
            if self._norm(p) == key:
                target = Path(p)
                break
        if target is None or not target.exists():
            return
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        except Exception:
            pass

    def _on_card_clicked(self, payload):
        key, modifiers = payload
        if modifiers & Qt.ControlModifier:
            if key in self._selected:
                self._selected.remove(key)
            else:
                self._selected.add(key)
        else:
            self._selected = {key}
        self._refresh_selected_styles()

    def _refresh_selected_styles(self):
        for key, card in self._cards.items():
            if self.media_kind == "image":
                card.setStyleSheet("QFrame{background:transparent;border:none;}")
                thumb = self._image_thumbs.get(key)
                if thumb is None:
                    continue
                if key in self._selected:
                    thumb.setStyleSheet("QLabel{background:#ffffff;border:2px solid #82aee8;border-radius:4px;}")
                else:
                    thumb.setStyleSheet("QLabel{background:#ffffff;border:1px solid #cfd6de;border-radius:4px;}")
            elif self.media_kind == "video":
                if bool(card.property("wide_video")):
                    card.setStyleSheet("QFrame{background:transparent;border:none;}")
                elif key in self._selected:
                    card.setStyleSheet("QFrame{background:#dcecff;border:1px solid #82aee8;border-radius:6px;}")
                else:
                    card.setStyleSheet("QFrame{background:#eef3f8;border:1px solid #d9e1e8;border-radius:6px;}")
            else:
                if key in self._selected:
                    card.setStyleSheet("QFrame{background:#dcecff;border:1px solid #82aee8;border-radius:6px;}")
                else:
                    card.setStyleSheet("QFrame{background:#eef3f8;border:1px solid #d9e1e8;border-radius:6px;}")

    def _fit_size(self, max_w: int, max_h: int, ratio: float) -> tuple[int, int]:
        safe_ratio = ratio if ratio > 0 else 1.0
        if max_w <= 0 or max_h <= 0:
            return (1, 1)
        if (max_w / safe_ratio) <= max_h:
            w = max_w
            h = int(w / safe_ratio)
        else:
            h = max_h
            w = int(h * safe_ratio)
        return (max(1, w), max(1, h))

    def _fit_image_thumb(self, key: str):
        thumb = self._image_thumbs.get(key)
        if thumb is None:
            return
        sz = thumb.contentsRect().size()
        max_w = sz.width()
        max_h = sz.height()
        if max_w <= 2 or max_h <= 2:
            return
        ratio = self._image_ratio_cache.get(key, 1.0)
        target_w, target_h = self._fit_size(max_w, max_h, ratio)

        movie = self._image_movies_by_key.get(key)
        if movie is not None:
            movie.setScaledSize(QSize(target_w, target_h))
            return

        pix = self._image_pixmaps.get(key)
        if pix is None or pix.isNull():
            return
        thumb.setPixmap(pix.scaled(target_w, target_h, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _make_image_card(self, card: _ClickableFrame, path: Path, key: str):
        lay = QVBoxLayout(card)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        thumb = _ImageThumbLabel("加载中...")
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setMinimumHeight(120)
        thumb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        thumb.setStyleSheet("QLabel{background:#ffffff;border:1px solid #cfd6de;border-radius:4px;}")
        self._image_thumbs[key] = thumb
        thumb.resized.connect(lambda k=key: self._fit_image_thumb(k))
        lay.addWidget(thumb)

        widget_ref = self

        def _bg_read(p=str(path)):
            """后台线程：NAS 图片读取 + 解码，不阻塞 UI。"""
            pp = Path(p)
            if not pp.exists():
                return ("not_found", None, False)
            # 先尝试静态图片
            reader = QImageReader(p)
            reader.setAutoTransform(True)
            is_anim = reader.supportsAnimation() and reader.imageCount() > 1
            if is_anim:
                # 动画图片（GIF 等）需要在主线程用 QMovie 播放
                return ("animated", p, True)
            img = reader.read()
            if img.isNull():
                return ("read_failed", None, False)
            return ("ok", img, False)

        def _apply(result, _err):
            if not isinstance(result, tuple) or len(result) != 3:
                thumb.setText("预览失败")
                return
            status, payload, is_anim = result
            if status == "not_found":
                thumb.setText("文件不存在")
                return
            if status == "read_failed":
                thumb.setText("预览失败")
                return
            if status == "animated" and is_anim:
                # 动画 GIF 等必须在主线程创建 QMovie
                movie = QMovie(str(payload))
                if movie.isValid():
                    widget_ref._image_movies_by_key[key] = movie
                    widget_ref._image_pixmaps.pop(key, None)
                    thumb.setMovie(movie)
                    movie.start()
                    widget_ref._movies.append(movie)
                    widget_ref._fit_image_thumb(key)
                    return
                thumb.setText("预览失败")
                return
            # 静态图片：QImage → QPixmap（主线程瞬间完成）
            pix = QPixmap.fromImage(payload) if isinstance(payload, QImage) else QPixmap()
            if pix.isNull():
                thumb.setText("预览失败")
                return
            widget_ref._image_pixmaps[key] = pix
            widget_ref._image_movies_by_key.pop(key, None)
            widget_ref._fit_image_thumb(key)

        # 利用全局线程池后台加载，彻底避免 NAS I/O 阻塞主线程
        worker = _AsyncWorker(_bg_read)
        self._async_image_workers.add(worker)

        def _finish(result, err, w=worker):
            try:
                _apply(result, err)
            finally:
                self._async_image_workers.discard(w)

        worker.signals.finished.connect(_finish)
        QThreadPool.globalInstance().start(worker)

    def _image_col_count(self) -> int:
        w = self.scroll.viewport().width()
        if w >= 640:
            return 4
        if w >= 420:
            return 3
        return 2

    def _image_span(self, path: Path, col_count: int) -> int:
        key = self._norm(str(path))
        ratio = self._image_ratio_cache.get(key)
        if ratio is None:
            try:
                pix = QPixmap(str(path))
                if pix.isNull() or pix.height() <= 0:
                    ratio = 1.0
                else:
                    ratio = float(pix.width()) / float(pix.height())
            except Exception:
                ratio = 1.0
            self._image_ratio_cache[key] = ratio
        # 横图占整行；竖图/近方图占单格
        return col_count if ratio >= 1.25 else 1

    def _video_ratio(self, path: Path) -> float:
        key = self._norm(str(path))
        ratio = self._video_ratio_cache.get(key)
        if ratio is not None:
            return ratio
        ratio = 1.0
        ffprobe = shutil.which("ffprobe")
        if ffprobe:
            try:
                proc = subprocess.run(
                    [
                        ffprobe,
                        "-v",
                        "error",
                        "-select_streams",
                        "v:0",
                        "-show_entries",
                        "stream=width,height",
                        "-of",
                        "csv=p=0:s=x",
                        str(path),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                first = ((proc.stdout or "").strip().splitlines() or [""])[0].strip()
                m = re.match(r"^(\d+)x(\d+)$", first)
                if m:
                    w = int(m.group(1))
                    h = int(m.group(2))
                    if w > 0 and h > 0:
                        ratio = float(w) / float(h)
            except Exception:
                pass
        self._video_ratio_cache[key] = ratio
        return ratio

    def _video_span(self, path: Path, col_count: int) -> int:
        # 横向视频铺满整行；竖向视频按单格展示
        return col_count if self._video_ratio(path) >= 1.25 else 1

    def _make_video_card(self, card: _ClickableFrame, path: Path, key: str):
        lay = QVBoxLayout(card)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        video = _VideoPreviewWidget()
        video.setMinimumHeight(120)
        video.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        lay.addWidget(video)

        # 控件作为 QGraphicsView viewport 的子控件，透明合成正常
        vp = video.viewport()
        play_btn = _VideoPlayBtn(vp)
        progress_bar = _VideoProgressBar(vp)
        time_label = _VideoTimeLabel(vp)
        play_btn.hide()
        progress_bar.hide()
        time_label.hide()
        _ctrl_widgets = [play_btn, progress_bar, time_label]

        player = QMediaPlayer(card)
        audio = QAudioOutput(card)
        player.setAudioOutput(audio)
        player.setVideoOutput(video.video_item)
        player.setSource(QUrl.fromLocalFile(str(path)))
        self._players.append(player)
        self._audios.append(audio)

        def _fmt_time(ms: int) -> str:
            sec = max(0, int(ms // 1000))
            m, s = divmod(sec, 60)
            h, m = divmod(m, 60)
            return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

        def _sync_controls_state(position_ms: int | None = None):
            pos = int(player.position() if position_ms is None else position_ms)
            dur = int(player.duration() or 0)
            playing = player.playbackState() == QMediaPlayer.PlayingState
            play_btn.set_playing(playing)
            progress_bar.set_state(pos, dur)
            time_label.set_text(f"{_fmt_time(pos)} / {_fmt_time(dur)}")

        def _place_controls():
            margin = 10
            vw, vh = vp.width(), vp.height()
            bottom = max(6, vh - 36 - margin)
            play_btn.move(margin, bottom)
            play_btn.raise_()
            tw = min(130, max(90, vw // 4))
            time_label.setFixedWidth(tw)
            time_label.move(max(0, vw - tw - margin), bottom + 6)
            time_label.raise_()
            px = margin + play_btn.width() + 8
            pw = max(20, vw - margin * 2 - play_btn.width() - tw - 24)
            progress_bar.setFixedWidth(pw)
            progress_bar.move(px, bottom + 6)
            progress_bar.raise_()

        hover = {"video": False, "controls": False}
        hide_timer = QTimer(card)
        hide_timer.setSingleShot(True)
        hide_timer.setInterval(900)

        def _show_controls():
            _place_controls()
            for w in _ctrl_widgets:
                w.show()
            hide_timer.start()

        def _try_hide_controls():
            if hover["video"] or hover["controls"] or progress_bar._dragging:
                hide_timer.start()
                return
            for w in _ctrl_widgets:
                w.hide()

        def _on_ctrl_enter():
            hover["controls"] = True
            _show_controls()

        def _on_ctrl_leave():
            hover["controls"] = False

        for w in _ctrl_widgets:
            w.entered.connect(_on_ctrl_enter)
            w.left.connect(_on_ctrl_leave)
        progress_bar.moved.connect(_show_controls)
        hide_timer.timeout.connect(_try_hide_controls)

        def _toggle():
            if player.playbackState() == QMediaPlayer.PlayingState:
                player.pause()
            else:
                player.play()
            _sync_controls_state()
            _show_controls()

        play_btn.clicked.connect(_toggle)
        progress_bar.seekRequested.connect(lambda p: (player.setPosition(int(p)), _show_controls()))
        player.playbackStateChanged.connect(lambda _s: (_sync_controls_state(), _show_controls()))
        player.durationChanged.connect(lambda _d: _sync_controls_state())
        player.positionChanged.connect(lambda p: _sync_controls_state(int(p)))
        video.clicked.connect(lambda mods: self._on_card_clicked((key, mods)))
        video.double_clicked.connect(lambda: self._open_path(self._norm(str(path))))
        video.entered.connect(lambda: (hover.__setitem__("video", True), _show_controls()))
        video.left.connect(lambda: hover.__setitem__("video", False))
        video.moved.connect(lambda: (hover.__setitem__("video", True), _show_controls()))
        video.resized.connect(_place_controls)
        QTimer.singleShot(0, _place_controls)
        _sync_controls_state()

    def _make_audio_card(self, card: _ClickableFrame, path: Path):
        lay = QVBoxLayout(card)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        title = QLabel(path.name)
        title.setWordWrap(True)
        row = QHBoxLayout()
        play_btn = QPushButton("▶")
        open_btn = QPushButton("打开")
        row.addWidget(play_btn)
        row.addWidget(open_btn)
        row.addStretch(1)
        lay.addWidget(title)
        lay.addLayout(row)

        player = QMediaPlayer(card)
        audio = QAudioOutput(card)
        player.setAudioOutput(audio)
        player.setSource(QUrl.fromLocalFile(str(path)))
        self._players.append(player)
        self._audios.append(audio)

        def _toggle():
            if player.playbackState() == QMediaPlayer.PlayingState:
                player.pause()
                play_btn.setText("▶")
            else:
                player.play()
                play_btn.setText("⏸")

        play_btn.clicked.connect(_toggle)
        open_btn.clicked.connect(lambda: self._open_path(self._norm(str(path))))

    def _rebuild_cards(self):
        # 先停止并释放旧播放器，避免网络文件句柄残留导致后续删除/覆盖失败。
        for player in self._players:
            try:
                player.stop()
                player.setSource(QUrl())
            except Exception:
                pass
        self._movies.clear()
        self._players.clear()
        self._audios.clear()
        self._cards.clear()
        self._image_thumbs.clear()
        self._image_pixmaps.clear()
        self._image_movies_by_key.clear()
        self._async_image_workers.clear()
        self._image_has_video_cards = False
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for col in range(8):
            self.grid.setColumnStretch(col, 0)

        if self.media_kind == "image":
            col_count = self._image_col_count()
            for c in range(col_count):
                self.grid.setColumnStretch(c, 1)
            row = 0
            col = 0
            for raw in self._paths:
                path = Path(raw)
                key = self._norm(raw)
                card = _ClickableFrame(key)
                card.clicked.connect(self._on_card_clicked)
                card.double_clicked.connect(self._open_path)
                self._cards[key] = card
                is_video_src = path.suffix.lower() in VIDEO_EXTS
                if is_video_src:
                    self._image_has_video_cards = True
                    self._make_video_card(card, path, key)
                    span = self._video_span(path, col_count)
                    card.setProperty("wide_video", span >= col_count)
                else:
                    self._make_image_card(card, path, key)
                    span = self._image_span(path, col_count)
                if span >= col_count:
                    if col != 0:
                        row += 1
                        col = 0
                    self.grid.addWidget(card, row, 0, 1, col_count)
                    row += 1
                    col = 0
                else:
                    if col + span > col_count:
                        row += 1
                        col = 0
                    self.grid.addWidget(card, row, col, 1, span)
                    col += span
                    if col >= col_count:
                        row += 1
                        col = 0
            self._layout_rows = max(1, row + (1 if col > 0 else 0))
        elif self.media_kind == "video":
            col_count = 2
            for c in range(col_count):
                self.grid.setColumnStretch(c, 1)
            row = 0
            col = 0
            for raw in self._paths:
                path = Path(raw)
                key = self._norm(raw)
                card = _ClickableFrame(key)
                card.clicked.connect(self._on_card_clicked)
                card.double_clicked.connect(self._open_path)
                span = self._video_span(path, col_count)
                card.setProperty("wide_video", span >= col_count)
                self._cards[key] = card
                self._make_video_card(card, path, key)
                if span >= col_count:
                    if col != 0:
                        row += 1
                        col = 0
                    self.grid.addWidget(card, row, 0, 1, col_count)
                    row += 1
                    col = 0
                else:
                    if col + span > col_count:
                        row += 1
                        col = 0
                    self.grid.addWidget(card, row, col, 1, span)
                    col += span
                    if col >= col_count:
                        row += 1
                        col = 0
            self._layout_rows = max(1, row + (1 if col > 0 else 0))
        else:
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 1)
            for idx, raw in enumerate(self._paths):
                path = Path(raw)
                key = self._norm(raw)
                card = _ClickableFrame(key)
                card.clicked.connect(self._on_card_clicked)
                card.double_clicked.connect(self._open_path)
                self._cards[key] = card
                if self.media_kind == "video":
                    self._make_video_card(card, path, key)
                else:
                    self._make_audio_card(card, path)
                self.grid.addWidget(card, idx // 2, idx % 2)
            self._layout_rows = max(1, (len(self._paths) + 1) // 2)

        self._refresh_selected_styles()
        self._sync_height()

    def _sync_height(self):
        if self.media_kind == "image":
            row_h = 220 if bool(getattr(self, "_image_has_video_cards", False)) else 170
            visible_rows = 1
        elif self.media_kind == "video":
            row_h = 220
            visible_rows = min(max(1, self._layout_rows), 2)
        else:
            row_h = 90
            visible_rows = min(max(1, self._layout_rows), 3)
        frame = 14
        h = row_h * visible_rows + frame
        self.setMinimumHeight(h)
        self.setMaximumHeight(h)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.media_kind in {"image", "video"}:
            self._rebuild_cards()


class FlowLayout(QLayout):
    def __init__(self, parent=None, margin=0, spacing=4):
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    def addItem(self, item):
        self._items.append(item)

    def insertWidget(self, index: int, widget: QWidget):
        self.insertItem(index, QWidgetItem(widget))

    def insertItem(self, index: int, item):
        if index < 0 or index > len(self._items):
            self._items.append(item)
        else:
            self._items.insert(index, item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        l, t, r, b = self.getContentsMargins()
        size += QSize(l + r, t + b)
        return size

    def _do_layout(self, rect, test_only):
        x = rect.x()
        y = rect.y()
        line_height = 0
        spacing = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + spacing
            if next_x - spacing > rect.right() and line_height > 0:
                x = rect.x()
                y = y + line_height + spacing
                next_x = x + hint.width() + spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


class ChipWidget(QFrame):
    selected = Signal(object)
    removed = Signal(object)
    renamed = Signal(object, str)

    def __init__(self, value: str, parent=None):
        super().__init__(parent)
        self.value = value
        self._is_selected = False
        self.setObjectName("chip")
        self.setFocusPolicy(Qt.StrongFocus)
        lay = QHBoxLayout(self)
        # 不预留 x 的布局空间，允许悬浮覆盖少量文字
        lay.setContentsMargins(4, 1, 3, 1)
        lay.setSpacing(2)
        self.edit = QLineEdit(value)
        self.edit.setFrame(False)
        self.edit.setReadOnly(True)
        self.edit.setStyleSheet("QLineEdit{background:transparent;border:none;padding:0px;}")
        self.edit.setFocusPolicy(Qt.ClickFocus)
        self.edit.setMaximumHeight(18)
        self.edit.installEventFilter(self)
        self.edit.editingFinished.connect(self._finish_edit)
        lay.addWidget(self.edit)
        self.close_btn = QPushButton("x")
        self.close_btn.setFixedSize(11, 11)
        self.close_btn.setFocusPolicy(Qt.NoFocus)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setFlat(True)
        self.close_btn.setVisible(False)
        self.close_btn.setStyleSheet(
            "QPushButton{color:rgba(198,40,40,140);border:none;background:transparent;font-weight:bold;padding:0px;}"
            "QPushButton:hover{color:rgba(198,40,40,220);}"
        )
        self.close_btn.clicked.connect(lambda: self.removed.emit(self))
        self.close_btn.setParent(self)
        self.close_btn.raise_()
        self.set_selected(False)
        self._recalc_width()

    def set_selected(self, on: bool):
        self._is_selected = on
        self.close_btn.setVisible(on and self.edit.isReadOnly())
        self.close_btn.setStyleSheet(
            "QPushButton{color:%s;border:none;background:transparent;font-weight:bold;padding:0px;}"
            % ("rgba(198,40,40,240)" if on else "rgba(198,40,40,140)")
        )
        if on:
            self.setStyleSheet("QFrame#chip{background:#dcecff;border:1px solid #82aee8;border-radius:6px;}")
        else:
            self.setStyleSheet("QFrame#chip{background:#eef3f8;border:1px solid #d9e1e8;border-radius:6px;}")

    def _recalc_width(self):
        fm = self.edit.fontMetrics()
        text = self.edit.text().strip() or " "
        w = max(14, fm.horizontalAdvance(text) + 4)
        self.edit.setFixedWidth(w)
        self.updateGeometry()

    def _finish_edit(self):
        if self.edit.isReadOnly():
            return
        self.edit.setReadOnly(True)
        new_val = self.edit.text().strip()
        self.renamed.emit(self, new_val)
        self.close_btn.setVisible(self._is_selected)
        self._recalc_width()

    def begin_edit(self):
        self.edit.setReadOnly(False)
        # 进入编辑模式时隐藏 x，避免遮挡编辑文本
        self.close_btn.setVisible(False)
        self.edit.setFocus(Qt.MouseFocusReason)
        self.edit.selectAll()

    def mousePressEvent(self, event):
        self.selected.emit(self)
        self.setFocus(Qt.MouseFocusReason)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.selected.emit(self)
        self.begin_edit()
        super().mouseDoubleClickEvent(event)

    def eventFilter(self, obj, event):
        if obj is self.edit:
            if event.type() == QEvent.MouseButtonPress:
                self.selected.emit(self)
                return False
            if event.type() == QEvent.MouseButtonDblClick:
                self.selected.emit(self)
                self.begin_edit()
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if event.key() in {Qt.Key_Backspace, Qt.Key_Delete}:
            self.removed.emit(self)
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 悬浮在右上角，不参与布局占位
        x = self.width() - self.close_btn.width() - 2
        y = max(0, (self.height() - self.close_btn.height()) // 2)
        self.close_btn.move(x, y)


class GapWidget(QWidget):
    clicked = Signal(int)

    def __init__(self, index: int, parent=None):
        super().__init__(parent)
        self._index = index
        self.setFixedSize(3, 16)
        self.setCursor(Qt.IBeamCursor)

    def mousePressEvent(self, event):
        self.clicked.emit(self._index)
        super().mousePressEvent(event)


class MultiValueEditor(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._values: list[str] = []
        self._selected_index: int = -1
        self._insert_index: int = 0
        self._commit_lock = False
        self._ime_preedit: str = ""
        self._chips: list[ChipWidget] = []
        self._gaps: list[GapWidget] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.shell = QFrame()
        self.shell.setObjectName("multiValueShell")
        self.shell.setStyleSheet(
            "QFrame#multiValueShell{background:#f5f7fa;border:1px solid #d0d7de;border-radius:4px;}"
        )
        self.shell.setCursor(Qt.IBeamCursor)
        self.flow = FlowLayout(self.shell, margin=1, spacing=1)

        # 唯一输入点：始终只保留这一个内联输入光标控件
        self.input_edit = self._create_input_edit()
        self.shell.installEventFilter(self)
        root.addWidget(self.shell)
        self.setMinimumHeight(24)
        self.setMaximumHeight(48)
        self._rebuild_flow()

    def _create_input_edit(self) -> QLineEdit:
        edit = QLineEdit(self.shell)
        edit.setPlaceholderText("")
        edit.setFrame(False)
        edit.setMinimumWidth(8)
        edit.setMaximumHeight(18)
        edit.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        edit.setAttribute(Qt.WA_TranslucentBackground, True)
        edit.setStyleSheet(
            "QLineEdit{background:transparent;border:none;padding:0px;margin:0px;}"
            "QLineEdit:focus{background:transparent;border:none;}"
        )
        edit.installEventFilter(self)
        edit.textChanged.connect(self._sync_input_width)
        edit.raise_()
        return edit

    def _ensure_input_edit_alive(self):
        try:
            _ = self.input_edit.parent()
        except RuntimeError:
            self.input_edit = self._create_input_edit()

    def eventFilter(self, obj, event):
        if obj is self.shell and event.type() == QEvent.MouseButtonPress:
            if hasattr(event, "position"):
                pos = event.position().toPoint()
            else:
                pos = event.pos()
            child = self.shell.childAt(pos)
            if child is not None and child is not self.shell:
                return False
            self._set_insert_index(len(self._values))
            self._focus_input_end()
            return False
        if obj is self.shell and event.type() == QEvent.Resize:
            self._position_input_overlay()
            return False
        if obj is self.input_edit:
            if event.type() == QEvent.FocusOut:
                self._commit_input()
                self._ime_preedit = ""
                self._sync_input_width()
            elif event.type() == QEvent.InputMethod:
                # 中文输入法在“预编辑”阶段不会触发 textChanged，需要单独跟踪宽度。
                try:
                    self._ime_preedit = event.preeditString() or ""
                except Exception:
                    self._ime_preedit = ""
                self._sync_input_width()
                return False
            elif event.type() == QEvent.KeyPress:
                if event.key() in {Qt.Key_Return, Qt.Key_Enter}:
                    self._commit_input()
                    return True
                if event.key() == Qt.Key_Backspace and not self.input_edit.text():
                    self._delete_before_cursor()
                    return True
                if event.key() == Qt.Key_Delete and not self.input_edit.text():
                    self._delete_after_cursor()
                    return True
        return super().eventFilter(obj, event)

    def _split_values(self, raw: str) -> list[str]:
        values: list[str] = []
        seen: set[str] = set()
        for part in re.split(r"[\n,，;；/|、]+", raw.strip()):
            one = part.strip()
            if not one:
                continue
            key = one.casefold()
            if key in seen:
                continue
            seen.add(key)
            values.append(one)
        return values

    def _sync_input_width(self):
        self._ensure_input_edit_alive()
        fm = self.input_edit.fontMetrics()
        txt = self.input_edit.text()
        display_txt = f"{txt}{self._ime_preedit}"
        if self._insert_index < len(self._values):
            # 中间插入时默认窄光标槽；输入法预编辑期间临时扩宽，避免文字被裁剪。
            width = 20 if not display_txt else fm.horizontalAdvance(display_txt) + 12
        else:
            width = fm.horizontalAdvance(display_txt) + 8 if display_txt else 8
        input_w = max(8, min(220, width))
        self.input_edit.setFixedWidth(input_w)
        self._sync_active_gap_width(input_w, bool(display_txt))
        self.input_edit.updateGeometry()
        self._position_input_overlay()

    def _sync_active_gap_width(self, input_w: int, has_display_text: bool):
        if not self._gaps:
            return
        active_idx = max(0, min(self._insert_index, len(self._gaps) - 1))
        for i, gap in enumerate(self._gaps):
            if gap is None:
                continue
            if i == active_idx and self._insert_index < len(self._values) and has_display_text:
                # 中间插入且正在输入时，为输入框预留真实占位宽度，让后续 chip 即时让位。
                gap.setFixedWidth(max(3, input_w + 2))
            else:
                gap.setFixedWidth(3)

    def _focus_input_end(self):
        self._ensure_input_edit_alive()
        self._position_input_overlay()
        self.input_edit.show()
        self.input_edit.raise_()
        self.input_edit.setFocus(Qt.MouseFocusReason)
        self.input_edit.setCursorPosition(len(self.input_edit.text()))

    def _set_selected_index(self, idx: int):
        self._selected_index = idx
        for i, chip in enumerate(self._chips):
            chip.set_selected(i == idx)

    def _set_insert_index(self, idx: int):
        self._insert_index = max(0, min(len(self._values), idx))
        self._set_selected_index(-1)
        self._rebuild_flow()

    def _rebuild_flow(self):
        self._ensure_input_edit_alive()
        while self.flow.count():
            item = self.flow.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                if w is self.input_edit:
                    # 输入光标控件必须常驻，不能销毁。
                    w.hide()
                    continue
                # 旧 chip 若仅 setParent(None) 在 Windows 下可能短暂变成顶层小窗；
                # 这里直接隐藏并延迟销毁，避免出现“诡异弹窗”。
                w.hide()
                w.deleteLater()
        self._chips = []
        self._gaps = []

        for i, val in enumerate(self._values):
            gap = GapWidget(i, self.shell)
            gap.clicked.connect(self._on_gap_clicked)
            self.flow.addWidget(gap)
            self._gaps.append(gap)
            chip = ChipWidget(val, self.shell)
            chip.selected.connect(self._on_chip_selected)
            chip.removed.connect(self._on_chip_removed)
            chip.renamed.connect(self._on_chip_renamed)
            self._chips.append(chip)
            self.flow.addWidget(chip)

        end_gap = GapWidget(len(self._values), self.shell)
        end_gap.clicked.connect(self._on_gap_clicked)
        self.flow.addWidget(end_gap)
        self._gaps.append(end_gap)

        self._sync_input_width()
        self._set_selected_index(self._selected_index if 0 <= self._selected_index < len(self._values) else -1)
        QTimer.singleShot(0, self._position_input_overlay)

    def _position_input_overlay(self):
        self._ensure_input_edit_alive()
        if not self._gaps:
            self.input_edit.hide()
            return
        idx = max(0, min(self._insert_index, len(self._gaps) - 1))
        gap = self._gaps[idx]
        if gap is None:
            self.input_edit.hide()
            return
        # gap 变宽时左对齐到占位槽；常规窄 gap 时保持原有右侧锚点微调。
        if gap.width() > 6:
            x = gap.x() + 1
        else:
            x = gap.x() + gap.width() - 4
        max_x = max(0, self.shell.width() - self.input_edit.width() - 1)
        x = max(0, min(max_x, x))
        y = max(0, gap.y() + max(0, (gap.height() - self.input_edit.height()) // 2))
        self.input_edit.move(x, y)
        self.input_edit.raise_()

    def _dedup_keep_order(self, values: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for one in values:
            txt = one.strip()
            if not txt:
                continue
            key = txt.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(txt)
        return out

    def _on_gap_clicked(self, idx: int):
        self._set_insert_index(idx)
        self._focus_input_end()

    def _on_chip_selected(self, chip: ChipWidget):
        if chip not in self._chips:
            return
        idx = self._chips.index(chip)
        self._set_selected_index(idx)

    def _on_chip_removed(self, chip: ChipWidget):
        if chip not in self._chips:
            return
        idx = self._chips.index(chip)
        self._values.pop(idx)
        self._insert_index = max(0, min(self._insert_index, len(self._values)))
        self._selected_index = -1
        self._rebuild_flow()

    def _on_chip_renamed(self, chip: ChipWidget, new_value: str):
        if chip not in self._chips:
            return
        idx = self._chips.index(chip)
        split_vals = self._split_values(new_value)
        if not split_vals:
            self._values.pop(idx)
        elif len(split_vals) == 1:
            self._values[idx] = split_vals[0]
        else:
            self._values[idx : idx + 1] = split_vals
        self._values = self._dedup_keep_order(self._values)
        self._insert_index = max(0, min(idx + 1, len(self._values)))
        self._selected_index = -1
        self._rebuild_flow()

    def _commit_input(self):
        if self._commit_lock:
            return
        txt = self.input_edit.text().strip()
        if not txt:
            return
        self._commit_lock = True
        try:
            incoming = self._split_values(txt)
            if not incoming:
                return
            self._values[self._insert_index : self._insert_index] = incoming
            self._values = self._dedup_keep_order(self._values)
            self.input_edit.setText("")
            self._ime_preedit = ""
            self._insert_index = min(len(self._values), self._insert_index + len(incoming))
            self._selected_index = -1
            self._rebuild_flow()
        finally:
            self._commit_lock = False

    def _delete_before_cursor(self):
        if self._insert_index <= 0:
            return
        self._values.pop(self._insert_index - 1)
        self._insert_index -= 1
        self._selected_index = -1
        self._rebuild_flow()

    def _delete_after_cursor(self):
        if self._insert_index >= len(self._values):
            return
        self._values.pop(self._insert_index)
        self._selected_index = -1
        self._rebuild_flow()

    def set_values(self, values: list[str]):
        self._values = self._dedup_keep_order(values)
        self._selected_index = -1
        self._insert_index = len(self._values)
        self.input_edit.setText("")
        self._rebuild_flow()

    def get_values(self) -> list[str]:
        result = list(self._values)
        pending = self._split_values(self.input_edit.text())
        seen = {x.casefold() for x in result}
        for one in pending:
            key = one.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(one)
        return result

    def serialized(self) -> str:
        return "/".join(self.get_values())

    def add_values_from_text(self, raw_text: str):
        incoming = self._split_values(raw_text)
        if not incoming:
            return
        self._values = self._dedup_keep_order(self._values + incoming)
        self._insert_index = len(self._values)
        self._selected_index = -1
        self._rebuild_flow()


class JellyfinNfoQtWindow(QMainWindow):
    _log_signal = Signal(str)

    BASE_IMAGE_KEYS = ("primary", "backdrop", "banner", "logo", "thumb")
    VIDEO_ONLY_IMAGE_KEYS = {"clearlogo", "clearart"}
    MUSIC_ONLY_IMAGE_KEYS = {"disc", "cdart", "discart"}
    VIDEO_LIBRARY_TYPES = {"tvshow", "season", "episode", "movie", "video_item"}

    EXTRA_IMAGE_ROWS = [
        ("clearlogo", "徽标 (ClearLogo)"),
        ("disc", "光盘封面图 (Disc)"),
        ("cdart", "光盘封面图 (CDArt)"),
        ("discart", "光盘封面图 (DiscArt)"),
        ("clearart", "艺术图 (ClearArt)"),
    ]

    EXTRA_VIDEO_ROWS = [
        ("extras_folder_backdrops", "背景视频 (Backdrops)"),
        ("extras_folder_trailers", "预告片 (Trailers)"),
        ("extras_folder_samples", "示例片段 (Samples)"),
        ("extras_folder_interviews", "访谈 (Interviews)"),
        ("extras_folder_behind the scenes", "幕后花絮 (Behind the Scenes)"),
        ("extras_folder_deleted scenes", "删除片段 (Deleted Scenes)"),
        ("extras_folder_scenes", "场景 (Scenes)"),
        ("extras_folder_shorts", "短片 (Shorts)"),
        ("extras_folder_featurettes", "特辑 (Featurettes)"),
        ("extras_folder_clips", "片段 (Clips)"),
        ("extras_folder_other", "其他 (Other)"),
        ("extras_folder_extras", "额外内容 (Extras)"),
        ("suffix_trailer", "预告片后缀 (-trailer)"),
        ("suffix_sample", "示例片段后缀 (-sample)"),
        ("suffix_scene", "场景后缀 (-scene)"),
        ("suffix_clip", "片段后缀 (-clip)"),
        ("suffix_interview", "访谈后缀 (-interview)"),
        ("suffix_behindthescenes", "幕后花絮后缀 (-behindthescenes)"),
        ("suffix_deleted", "删除后缀 (-deleted)"),
        ("suffix_deletedscene", "删除片段后缀 (-deletedscene)"),
        ("suffix_featurette", "特辑后缀 (-featurette)"),
        ("suffix_short", "短片后缀 (-short)"),
        ("suffix_other", "其他后缀 (-other)"),
        ("suffix_extra", "额外内容后缀 (-extra)"),
    ]

    EXTRA_AUDIO_ROWS = [("extras_folder_theme-music", "主题音乐 (Theme Music)")]
    TREE_INDEX_ROLE = int(Qt.UserRole) + 1

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Jellyfin 全类型 NFO 元数据编辑器 (Qt)")
        self.resize(1280, 760)
        self.paths: set[Path] = set()
        self.items: list[NfoItem] = []
        self.field_edits: dict[str, QLineEdit] = {}
        self.multi_value_editors: dict[str, MultiValueEditor] = {}
        self.plot_edit: QTextEdit | None = None
        self._history_file = Path(__file__).with_name(".jellyfin_ops_history.json")
        self._session_pg_dsn = ""
        self._session_pg_key = "default"
        self._session_pg_ready = False
        self._session_pg_driver = None
        self._session_pg_error_logged = False
        self._auto_load_timer = QTimer(self)
        self._auto_load_timer.setSingleShot(True)
        self._auto_load_timer.timeout.connect(lambda: self.load_selected_metadata(silent_if_empty=True))
        self._session_save_timer = QTimer(self)
        self._session_save_timer.setSingleShot(True)
        self._session_save_timer.timeout.connect(self._save_ui_session)
        self._last_loaded_selection_key: tuple[str, ...] | None = None
        self._loaded_field_snapshot: dict[str, str] = {}
        self._loaded_media_paths_snapshot: dict[str, set[str]] = {}
        self._suspend_selection_change_prompt = False
        self._scan_pool = QThreadPool.globalInstance()
        self._scan_request_id = 0
        self._scan_workers: dict[int, _ScanWorker] = {}
        self._async_workers: set[_AsyncWorker] = set()
        self._login_panels: set[QDialog] = set()
        self._scan_progress_rows: dict[str, QTreeWidgetItem] = {}
        self._lazy_loaded_dirs: set[str] = set()
        self._lazy_loaded_check_ts: dict[str, float] = {}   # key -> monotonic() 上次后台校验时间
        self._pending_restore_selected_paths: set[str] = set()
        self._pending_restore_lazy_dirs: set[str] = set()
        self._media_target_cards: dict[str, QGroupBox] = {}

        self.image_source_edits = {k: PathListWidget("image") for k in ("primary", "backdrop", "banner", "logo", "thumb")}
        self.extra_image_source_edits = {k: PathListWidget("image") for k, _ in self.EXTRA_IMAGE_ROWS}
        self.extra_video_source_edits = {k: PathListWidget("video") for k, _ in self.EXTRA_VIDEO_ROWS}
        self.extra_audio_source_edits = {k: PathListWidget("audio") for k, _ in self.EXTRA_AUDIO_ROWS}
        self.provider_ids_edit = QLineEdit()
        self._log_lines: list[str] = []
        self._log_dialog: QDialog | None = None
        self._log_dialog_text: QPlainTextEdit | None = None
        self._last_ytdlp_error: str = ""
        self._last_ytdlp_need_cookie: bool = False
        self._last_ytdlp_age_restricted: bool = False
        self._cached_ytdlp_cmd_prefix: list[str] | None = None
        self._last_confirmed_video_url: str = ""
        self._log_signal.connect(self._log_on_main_thread)
        self._auto_configure_pg_session()
        self._build_ui()
        self._init_rename_state()
        self._ensure_pg_session_schema()
        self._restore_ui_session()

    def _create_multi_value_editor(self):
        return MultiValueEditor()

    def _create_item_tree_widget(self):
        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        tree.itemSelectionChanged.connect(self._on_item_selection_changed)
        tree.setContextMenuPolicy(Qt.CustomContextMenu)
        tree.customContextMenuRequested.connect(self._on_item_list_context_menu)
        tree.itemClicked.connect(lambda node, col: self._on_tree_clicked_for_rename(tree, node, col))
        tree.itemChanged.connect(lambda node, col: self._on_tree_item_changed_for_rename(node, col))
        return tree

    def _create_scan_worker(self, token: int, paths: tuple[Path, ...]):
        return _ScanWorker(token, paths)

    def _schedule_save_ui_session(self):
        self._session_save_timer.start(180)

    def _restore_scan_tree_state(self, restore_lazy_dirs: bool = True):
        if not self.items:
            self._pending_restore_selected_paths.clear()
            self._pending_restore_lazy_dirs.clear()
            return
        if restore_lazy_dirs and self._pending_restore_lazy_dirs:
            seed_items = [
                item
                for item in self.items
                if str(item.path.parent).casefold() in self._pending_restore_lazy_dirs
                and item.media_type in {"tvshow", "season", "artist", "movie"}
            ]
            if seed_items:
                try:
                    self._ensure_secondary_items_loaded(seed_items)
                except Exception as exc:
                    self._log(f"恢复延迟加载失败: {exc}")
        if self._pending_restore_selected_paths:
            self._reselect_by_paths(self._pending_restore_selected_paths)
            self.load_selected_metadata(silent_if_empty=True, force_reload=True)
        self._pending_restore_selected_paths.clear()
        self._pending_restore_lazy_dirs.clear()
        self._schedule_save_ui_session()

    def closeEvent(self, event):
        self._save_ui_session()
        if hasattr(self, "_shutdown_embedded_pg"):
            try:
                self._shutdown_embedded_pg()
            except Exception:
                pass
        super().closeEvent(event)

    def _build_ui(self):
        build_ui(self)

    def _build_media_target_card(
        self,
        grid: QGridLayout,
        index: int,
        label: str,
        key: str,
        kind: str,
        edit: PathListWidget,
        is_extra: bool,
        has_search: bool,
    ):
        build_media_target_card(self, grid, index, label, key, kind, edit, is_extra, has_search)

    def _allowed_media_targets_for_type(self, media_type: str) -> set[str]:
        allowed = set(self.BASE_IMAGE_KEYS)
        extra_video_keys = {k for k, _ in self.EXTRA_VIDEO_ROWS}
        extra_audio_keys = {k for k, _ in self.EXTRA_AUDIO_ROWS}
        if media_type in self.VIDEO_LIBRARY_TYPES:
            allowed.update(self.VIDEO_ONLY_IMAGE_KEYS)
            allowed.update(self.MUSIC_ONLY_IMAGE_KEYS)
            allowed.update(extra_video_keys)
            allowed.update(extra_audio_keys)
        elif media_type in {"artist", "album"}:
            allowed.update(self.MUSIC_ONLY_IMAGE_KEYS)
        else:
            allowed.update(self.VIDEO_ONLY_IMAGE_KEYS | self.MUSIC_ONLY_IMAGE_KEYS | extra_video_keys | extra_audio_keys)
        return allowed

    def _refresh_media_target_visibility(self, selected: list[NfoItem]):
        if not self._media_target_cards:
            return
        if not selected:
            visible_targets = set(self._media_target_cards.keys())
        else:
            targets_by_type = [self._allowed_media_targets_for_type(item.media_type) for item in selected]
            visible_targets = set.intersection(*targets_by_type) if targets_by_type else set(self._media_target_cards.keys())
        for key, card in self._media_target_cards.items():
            show = key in visible_targets
            card.setVisible(show)
            if show:
                continue
            if key in self.image_source_edits:
                self.image_source_edits[key].set_paths([])
            elif key in self.extra_image_source_edits:
                self.extra_image_source_edits[key].set_paths([])
            elif key in self.extra_video_source_edits:
                self.extra_video_source_edits[key].set_paths([])
            elif key in self.extra_audio_source_edits:
                self.extra_audio_source_edits[key].set_paths([])

    def _log(self, text: str):
        self._log_lines.append(text)
        if threading.current_thread() is threading.main_thread():
            if self._log_dialog_text is not None:
                self._log_dialog_text.appendPlainText(text)
        else:
            self._log_signal.emit(text)

    def _log_on_main_thread(self, text: str):
        if self._log_dialog_text is not None:
            self._log_dialog_text.appendPlainText(text)

    def _clear_logs(self):
        self._log_lines.clear()
        if self._log_dialog_text is not None:
            self._log_dialog_text.setPlainText("")
        self._log("日志已清空。")

    def _show_log_dialog(self):
        if self._log_dialog is None:
            dlg = QDialog(self)
            dlg.setWindowTitle("日志")
            dlg.resize(980, 380)
            lay = QVBoxLayout(dlg)
            txt = QPlainTextEdit()
            txt.setReadOnly(True)
            lay.addWidget(txt)
            btn_row = QHBoxLayout()
            clear_btn = QPushButton("清空日志")
            close_btn = QPushButton("关闭")
            clear_btn.clicked.connect(self._clear_logs)
            close_btn.clicked.connect(dlg.close)
            btn_row.addWidget(clear_btn)
            btn_row.addStretch(1)
            btn_row.addWidget(close_btn)
            lay.addLayout(btn_row)
            self._log_dialog = dlg
            self._log_dialog_text = txt
        if self._log_dialog_text is not None:
            self._log_dialog_text.setPlainText("\n".join(self._log_lines))
        self._log_dialog.show()
        self._log_dialog.raise_()
        self._log_dialog.activateWindow()

    def _run_async(self, fn, on_done):
        worker = _AsyncWorker(fn)
        self._async_workers.add(worker)

        def _finish(result, err):
            try:
                on_done(result, err)
            finally:
                self._async_workers.discard(worker)

        worker.signals.finished.connect(_finish)
        self._scan_pool.start(worker)

    def _show_chromium_login_panel(self, message: str, on_apply, on_cancel):
        dlg = QDialog(self)
        dlg.setWindowTitle("YouTube 登录控制 (WebView2)")
        dlg.setModal(False)
        dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        dlg.resize(520, 170)
        lay = QVBoxLayout(dlg)
        text = QLabel(message + "\n\n登录并定位到目标视频后，点击“确认并继续”。")
        text.setWordWrap(True)
        lay.addWidget(text, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel_btn = QPushButton("取消")
        apply_btn = QPushButton("确认并继续")
        row.addWidget(cancel_btn)
        row.addWidget(apply_btn)
        lay.addLayout(row)

        state = {"done": False}

        def _finish(ok: bool):
            if state["done"]:
                return
            state["done"] = True
            try:
                if ok:
                    on_apply()
                else:
                    on_cancel()
            finally:
                self._login_panels.discard(dlg)
                dlg.close()

        apply_btn.clicked.connect(lambda: _finish(True))
        cancel_btn.clicked.connect(lambda: _finish(False))
        dlg.finished.connect(lambda *_: _finish(False))
        self._login_panels.add(dlg)
        main_geo = self.geometry()
        x = max(0, main_geo.x() + main_geo.width() - dlg.width() - 12)
        y = max(0, main_geo.y() + 42)
        dlg.move(x, y)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _join_source_values(self, values: list[str]) -> str:
        return ", ".join(values)

    def _split_source_values(self, raw: str) -> list[str]:
        if not raw:
            return []
        values: list[str] = []
        seen: set[str] = set()
        for part in re.split(r"[\n,，;；|]+", raw.strip()):
            one = part.strip()
            if not one:
                continue
            key = one.casefold()
            if key in seen:
                continue
            seen.add(key)
            values.append(one)
        return values

    def _target_supports_multi(self, target_name: str) -> bool:
        return target_supports_multiple(target_name)

    def _pick_image(self) -> str:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择图片",
            "",
            "图片文件 (*.jpg *.jpeg *.png *.webp *.gif *.bmp *.tif *.tiff *.avif);;所有文件 (*.*)",
        )
        return path

    def _pick_video(self) -> str:
        path, _ = QFileDialog.getOpenFileName(self, "选择视频", "", "视频文件 (*.mp4 *.mkv *.avi *.mov *.wmv *.m4v *.webm *.mpeg *.mpg);;所有文件 (*.*)")
        return path

    def _pick_audio(self) -> str:
        path, _ = QFileDialog.getOpenFileName(self, "选择音频", "", "音频文件 (*.mp3 *.flac *.m4a *.aac *.wav *.ogg *.opus *.mka);;所有文件 (*.*)")
        return path

    def _pick_image_for_target(self, key: str, is_extra: bool):
        p = self._pick_image()
        if not p:
            return
        edit = self.extra_image_source_edits[key] if is_extra else self.image_source_edits[key]
        if is_extra and self._target_supports_multi(key):
            edit.append_path(p)
        else:
            edit.set_paths([p])

    def _pick_file_for_target(self, key: str, kind: str):
        p = self._pick_video() if kind == "video" else self._pick_audio()
        if not p:
            return
        edit = self.extra_video_source_edits[key] if kind == "video" else self.extra_audio_source_edits[key]
        if self._target_supports_multi(key):
            edit.append_path(p)
        else:
            edit.set_paths([p])

    def _open_image_crop_editor(self, target_key: str, is_extra: bool):
        edit = self.extra_image_source_edits[target_key] if is_extra else self.image_source_edits[target_key]
        paths = edit.get_paths()
        if not paths:
            QMessageBox.warning(self, "提示", "请先选择或下载图片。")
            return
        selected_paths = edit.get_selected_paths()
        chosen_path = selected_paths[0] if selected_paths else paths[0]
        checked = self._read_image_source(chosen_path, target_key)
        if checked is False or checked is None:
            return
        try:
            dialog = _QtImageCropDialog(self, checked, target_key)
            if dialog.exec() != QDialog.Accepted:
                return
            result = dialog.result_path
        except Exception as exc:
            QMessageBox.critical(self, "裁切失败", f"打开图片裁切器失败：{exc}")
            return
        if result is None:
            return
        chosen_key = str(Path(chosen_path).resolve()).casefold()
        merged: list[str] = []
        replaced = False
        for p in paths:
            if (not replaced) and (str(Path(p).resolve()).casefold() == chosen_key):
                merged.append(str(result))
                replaced = True
            else:
                merged.append(p)
        if not replaced:
            merged = [str(result)] + [p for p in paths if p.strip()]
        edit.set_paths(merged)
        self._log(f"已应用图片裁切结果: {result}")

    def _add_path(self, path_str: str):
        p = Path(path_str).resolve()
        if not p.exists():
            self._log(f"跳过不存在路径: {p}")
            return
        self.paths.add(p)

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择目录")
        if folder:
            self._add_path(folder)
            self.refresh_items()
            self._schedule_save_ui_session()

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择 NFO 文件", "", "NFO 文件 (*.nfo);;所有文件 (*.*)")
        for f in files:
            self._add_path(f)
        if files:
            self.refresh_items()
            self._schedule_save_ui_session()

    def clear_all(self):
        self._scan_request_id += 1
        self._scan_workers.clear()
        self._lazy_loaded_dirs.clear()
        self._lazy_loaded_check_ts.clear()
        self._pending_restore_selected_paths.clear()
        self._pending_restore_lazy_dirs.clear()
        self.paths.clear()
        self.items.clear()
        self._last_loaded_selection_key = None
        self.item_list.clear()
        if hasattr(self, "nfo_cover_gallery"):
            self.nfo_cover_gallery.clear()
        if hasattr(self, "nfo_cover_meta_list"):
            self.nfo_cover_meta_list.clear()
        if hasattr(self, "nfo_cover_preview"):
            self.nfo_cover_preview.setPixmap(QPixmap())
            self.nfo_cover_preview.setText("")
        if hasattr(self, "nfo_cover_title"):
            self.nfo_cover_title.setText("")
        if hasattr(self, "nfo_left_stack"):
            self.nfo_left_stack.setCurrentIndex(0)
        self._cover_gallery_pool = []
        self._cover_icon_load_jobs = []
        self._cover_row_root_dirs = []
        self._cover_row_cover_paths = []
        self._cover_path_cache = {}
        self._cover_icon_mem_cache = {}
        self._cover_full_pixmap_cache = {}
        self._cover_icon_loaded_rows = set()
        self._cover_icon_queued_rows = set()
        self._cover_icon_inflight_rows = set()
        for w in self.field_edits.values():
            w.setText("")
        if self.plot_edit is not None:
            self.plot_edit.setPlainText("")
        for mv in self.multi_value_editors.values():
            mv.set_values([])
        for m in (self.image_source_edits, self.extra_image_source_edits, self.extra_video_source_edits, self.extra_audio_source_edits):
            for w in m.values():
                w.set_paths([])
        self.provider_ids_edit.setText("")
        self.scan_stats_label.setText("统计：共 0 部电视剧，0 部电影，0 张专辑。")
        self._log("已清空。")
        self._schedule_save_ui_session()

    def _clear_edit_form_values(self):
        for w in self.field_edits.values():
            w.setText("")
        if self.plot_edit is not None:
            self.plot_edit.setPlainText("")
        for mv in self.multi_value_editors.values():
            mv.set_values([])
        for m in (self.image_source_edits, self.extra_image_source_edits, self.extra_video_source_edits, self.extra_audio_source_edits):
            for w in m.values():
                w.set_paths([])
        self.provider_ids_edit.setText("")
        self._loaded_field_snapshot = {}
        self._loaded_media_paths_snapshot = {}

    @staticmethod
    def _list_dir_cached(dir_path: Path, cache: dict[str, list[tuple[str, str, str, Path]]]) -> list[tuple[str, str, str, Path]]:
        """列出目录下所有文件，结果按 dir_path 缓存。

        返回列表元素: (stem_lower, suffix_lower, name_lower, resolved_path)
        """
        key = str(dir_path).casefold()
        cached = cache.get(key)
        if cached is not None:
            return cached
        entries: list[tuple[str, str, str, Path]] = []
        try:
            for f in dir_path.iterdir():
                try:
                    if not f.is_file():
                        continue
                    entries.append((f.stem.lower(), f.suffix.lower(), f.name.lower(), f))
                except OSError:
                    continue
        except OSError:
            pass
        entries.sort(key=lambda t: t[2])
        cache[key] = entries
        return entries

    def _find_existing_by_stems(self, root_dir: Path, stems: set[str], exts: set[str],
                                 dir_cache: dict[str, list[tuple[str, str, str, Path]]] | None = None) -> list[Path]:
        if dir_cache is not None:
            entries = self._list_dir_cached(root_dir, dir_cache)
            return [p for stem_l, suf_l, _n, p in entries if suf_l in exts and stem_l in stems]
        # 兼容旧调用方式（无缓存）
        hits: list[Path] = []
        try:
            iterator = root_dir.iterdir()
        except OSError:
            return []
        for f in iterator:
            if not f.is_file():
                continue
            if f.suffix.lower() not in exts:
                continue
            if f.stem.lower() in stems:
                hits.append(f)
        return sorted(hits, key=lambda p: p.name.lower())

    def _scan_existing_media_for_item(self, item: NfoItem,
                                       dir_cache: dict[str, list[tuple[str, str, str, Path]]] | None = None) -> dict[str, list[Path]]:
        root = item.path.parent
        try:
            if not root.exists() or not root.is_dir():
                return {}
        except OSError:
            return {}
        if dir_cache is None:
            dir_cache = {}
        found: dict[str, list[Path]] = {}
        occupied_base_paths: set[str] = set()
        base_image_aliases = {
            "primary": {"poster", "folder", "cover", "default", "movie", "show", "jacket"},
            "backdrop": {"backdrop", "fanart", "background", "art"},
            "banner": {"banner"},
            "logo": {"logo", "clearlogo"},
            "thumb": {"landscape", "thumb"},
        }
        for base_target, aliases in base_image_aliases.items():
            hits = self._find_existing_by_stems(root, aliases, SUPPORTED_IMAGE_EXTS, dir_cache)
            if hits:
                found[base_target] = hits[:1]
                occupied_base_paths.update(str(p) for p in hits[:1])
        for target, _ in self.EXTRA_IMAGE_ROWS:
            if target == "clearlogo":
                hits = self._find_existing_by_stems(root, {"clearlogo", "logo"}, SUPPORTED_IMAGE_EXTS, dir_cache)
            elif target in {"disc", "cdart", "discart"}:
                hits = self._find_existing_by_stems(root, {"disc", "cdart", "discart"}, SUPPORTED_IMAGE_EXTS, dir_cache)
            elif target == "clearart":
                hits = self._find_existing_by_stems(root, {"clearart"}, SUPPORTED_IMAGE_EXTS, dir_cache)
            else:
                hits = self._find_existing_by_stems(root, {target}, SUPPORTED_IMAGE_EXTS, dir_cache)
            hits = [p for p in hits if str(p) not in occupied_base_paths]
            if hits:
                found[target] = hits
        # 根目录文件缓存，用于 suffix_ 匹配
        root_entries = self._list_dir_cached(root, dir_cache)
        for target, _ in self.EXTRA_VIDEO_ROWS:
            if target.startswith("extras_folder_"):
                folder = target.replace("extras_folder_", "", 1)
                folder_path = root / folder
                sub_entries = self._list_dir_cached(folder_path, dir_cache)
                hits = [p for _stem, suf, _n, p in sub_entries if suf in VIDEO_EXTS]
                if hits:
                    found[target] = hits
            elif target.startswith("suffix_"):
                suffix_name = target.replace("suffix_", "", 1).lower()
                suffix_token = f"-{suffix_name}"
                hits = [p for stem_l, suf_l, _n, p in root_entries if suf_l in VIDEO_EXTS and stem_l.endswith(suffix_token)]
                if hits:
                    found[target] = hits
        for target, _ in self.EXTRA_AUDIO_ROWS:
            if target.startswith("extras_folder_"):
                folder = target.replace("extras_folder_", "", 1)
                folder_path = root / folder
                sub_entries = self._list_dir_cached(folder_path, dir_cache)
                hits = [p for _stem, suf, _n, p in sub_entries if suf in AUDIO_EXTS]
                if hits:
                    found[target] = hits
        return found

    def load_selected_metadata(
        self,
        silent_if_empty: bool = False,
        force_reload: bool = False,
        include_media_resources: bool = True,
    ):
        selected = self._selected_items()
        if not selected:
            self._refresh_media_target_visibility([])
            if not silent_if_empty:
                QMessageBox.warning(self, "提示", "请先选中至少一个 NFO 文件。")
            self._last_loaded_selection_key = None
            return
        if self._ensure_secondary_items_loaded(selected):
            selected = self._selected_items()
            if not selected:
                self._refresh_media_target_visibility([])
                self._last_loaded_selection_key = None
                return
        # 入口级拦截：无论刷新由何路径触发，切换到新 NFO 前都先确认未保存改动。
        if not bool(getattr(self, "_suspend_selection_change_prompt", False)):
            selected_paths_now = {str(item.path) for item in selected}
            if hasattr(self, "_confirm_save_before_selection_reload"):
                if not self._confirm_save_before_selection_reload(selected_paths_now):
                    return
        self._refresh_media_target_visibility(selected)
        selected_key = tuple(sorted(str(item.path) for item in selected))
        if not force_reload and self._last_loaded_selection_key == selected_key:
            return
        self._clear_edit_form_values()
        aggregate: dict[str, set[str]] = {tag: set() for tag in ALL_TAGS}
        failed = 0
        for item in selected:
            try:
                fields = parse_nfo_fields(item.path)
                allowed = WRITABLE_BY_MEDIA_TYPE.get(item.media_type, COMMON_WRITABLE)
                for tag, value in fields.items():
                    if tag not in allowed:
                        continue
                    if value and value.strip() not in {"-1", "-1.0"}:
                        aggregate[tag].add(value)
            except Exception as exc:
                failed += 1
                self._log(f"读取失败: {item.path} -> {exc}")

        for tag in ALL_TAGS:
            values = aggregate[tag]
            if not values:
                continue
            if tag == "plot":
                if self.plot_edit is None:
                    continue
                content = next(iter(values)) if len(values) == 1 else "\n\n-----\n\n".join(sorted(values))
                self.plot_edit.setPlainText(content)
                continue
            if tag in MULTI_VALUE_TAGS:
                merged: list[str] = []
                seen: set[str] = set()
                for raw in sorted(values):
                    for one in split_multi_values(raw):
                        k = one.casefold()
                        if k in seen:
                            continue
                        seen.add(k)
                        merged.append(one)
                mv = self.multi_value_editors.get(tag)
                if mv is not None:
                    mv.set_values(merged)
            elif len(values) == 1:
                self.field_edits[tag].setText(next(iter(values)))
            else:
                self.field_edits[tag].setText(" | ".join(sorted(values)))

        # 一级虚拟 NFO 兜底：title 为空时，默认回填为一级目录名（如电视剧名）。
        title_edit = self.field_edits.get("title")
        if title_edit is not None and not title_edit.text().strip() and len(selected) == 1:
            one = selected[0]
            fallback_title = ""
            try:
                all_items = getattr(self, "items", [])
                parent_of = getattr(self, "_tree_parent_of", {})
                idx_map = {str(it.path).casefold(): i for i, it in enumerate(all_items)}
                idx = idx_map.get(str(one.path).casefold())
                if isinstance(idx, int) and parent_of.get(idx) is None:
                    fallback_title = one.path.parent.name.strip() or one.path.stem.strip()
            except Exception:
                fallback_title = ""
            if fallback_title:
                title_edit.setText(fallback_title)

        self._last_loaded_selection_key = selected_key
        # 记录读取后的字段快照，仅用于"真正改动字段"的比较。
        snap: dict[str, str] = {}
        for tag, w in self.field_edits.items():
            snap[tag] = w.text().strip()
        for tag, mv in self.multi_value_editors.items():
            snap[tag] = mv.serialized().strip()
        if self.plot_edit is not None:
            snap["plot"] = self.plot_edit.toPlainText().strip()
        self._loaded_field_snapshot = snap
        self._log(f"已读取 {len(selected)} 个 NFO，失败 {failed} 个。")

        if include_media_resources:
            # 先清空媒体资源编辑框，后台扫描完再填入
            for edit in self.image_source_edits.values():
                edit.set_paths([])
            for edit in self.extra_image_source_edits.values():
                edit.set_paths([])
            for edit in self.extra_video_source_edits.values():
                edit.set_paths([])
            for edit in self.extra_audio_source_edits.values():
                edit.set_paths([])
            self._loaded_media_paths_snapshot = self._collect_ui_target_paths()

            # 后台异步扫描媒体资源（避免 NAS 目录列举阻塞 UI）
            media_load_token = int(getattr(self, "_media_load_token", 0)) + 1
            self._media_load_token = media_load_token
            frozen_selected = list(selected)
            frozen_key = selected_key

            def _media_scan_job():
                media_aggregate: dict[str, list[str]] = {}
                dir_cache: dict[str, list[tuple[str, str, str, Path]]] = {}
                for item in frozen_selected:
                    one = self._scan_existing_media_for_item(item, dir_cache)
                    for target, paths in one.items():
                        if not paths:
                            continue
                        media_aggregate.setdefault(target, [])
                        for p in paths:
                            p_str = str(p)
                            if p_str not in media_aggregate[target]:
                                media_aggregate[target].append(p_str)
                return media_aggregate

            def _media_scan_done(result, _err):
                # 如果用户已切换到其他选择，丢弃过期结果
                if media_load_token != int(getattr(self, "_media_load_token", -1)):
                    return
                if self._last_loaded_selection_key != frozen_key:
                    return
                media_aggregate = result if isinstance(result, dict) else {}
                for kind, edit in self.image_source_edits.items():
                    vals = media_aggregate.get(kind, [])
                    edit.set_paths(vals[:1] if vals else [])
                for target, edit in self.extra_image_source_edits.items():
                    vals = media_aggregate.get(target, [])
                    if not vals:
                        edit.set_paths([])
                    elif self._target_supports_multi(target):
                        edit.set_paths(vals)
                    else:
                        edit.set_paths(vals[:1])
                for target, edit in self.extra_video_source_edits.items():
                    vals = media_aggregate.get(target, [])
                    if not vals:
                        edit.set_paths([])
                    elif self._target_supports_multi(target):
                        edit.set_paths(vals)
                    else:
                        edit.set_paths(vals[:1])
                for target, edit in self.extra_audio_source_edits.items():
                    vals = media_aggregate.get(target, [])
                    if not vals:
                        edit.set_paths([])
                    elif self._target_supports_multi(target):
                        edit.set_paths(vals)
                    else:
                        edit.set_paths(vals[:1])
                self._loaded_media_paths_snapshot = self._collect_ui_target_paths()

            self._run_async(_media_scan_job, _media_scan_done)
        else:
            self._loaded_media_paths_snapshot = self._collect_ui_target_paths()

    def _parse_provider_ids(self, raw: str) -> dict[str, str] | bool:
        if not raw:
            return {}
        result: dict[str, str] = {}
        for item in re.split(r"[\n;/|，；、]+", raw.strip()):
            item = item.strip()
            if not item:
                continue
            if "=" not in item:
                QMessageBox.critical(self, "Provider ID 格式错误", f"缺少 '=': {item}")
                return False
            key, value = item.split("=", 1)
            tag = key.strip().lower()
            val = value.strip()
            if not tag or not val:
                QMessageBox.critical(self, "Provider ID 格式错误", f"键或值为空: {item}")
                return False
            if not tag.endswith("id"):
                QMessageBox.critical(self, "Provider ID 格式错误", f"标签需以 id 结尾: {tag}")
                return False
            result[tag] = val
        return result

    def _collect_ui_target_paths(self) -> dict[str, set[str]]:
        ui_map: dict[str, set[str]] = {}
        for target, w in self.image_source_edits.items():
            ui_map[target] = {str(Path(x)) for x in w.get_paths() if x.strip()}
        for target, w in self.extra_image_source_edits.items():
            ui_map[target] = {str(Path(x)) for x in w.get_paths() if x.strip()}
        for target, w in self.extra_video_source_edits.items():
            ui_map[target] = {str(Path(x)) for x in w.get_paths() if x.strip()}
        for target, w in self.extra_audio_source_edits.items():
            ui_map[target] = {str(Path(x)) for x in w.get_paths() if x.strip()}
        return ui_map

    def _collect_ui_field_snapshot(self) -> dict[str, str]:
        snap: dict[str, str] = {}
        for tag, w in self.field_edits.items():
            snap[tag] = w.text().strip()
        for tag, mv in self.multi_value_editors.items():
            snap[tag] = mv.serialized().strip()
        if self.plot_edit is not None:
            snap["plot"] = self.plot_edit.toPlainText().strip()
        return snap

    def _selected_tree_indices(self) -> list[int]:
        out: list[int] = []
        seen: set[int] = set()
        for node in self.item_list.selectedItems():
            idx = node.data(0, self.TREE_INDEX_ROLE)
            if not isinstance(idx, int):
                continue
            if not (0 <= idx < len(self.items)):
                continue
            if idx in seen:
                continue
            seen.add(idx)
            out.append(idx)
        return out

    def _collect_subtree_leaf_indices(self, seed_indices: set[int]) -> tuple[set[int], set[int]]:
        parent_of: dict[int, int | None] = getattr(self, "_tree_parent_of", {})
        children_of: dict[int, list[int]] = {}
        for child_idx, parent_idx in parent_of.items():
            if isinstance(parent_idx, int) and parent_idx != child_idx:
                children_of.setdefault(parent_idx, []).append(child_idx)
        subtree: set[int] = set()
        stack = [x for x in seed_indices if isinstance(x, int) and 0 <= x < len(self.items)]
        while stack:
            cur = stack.pop()
            if cur in subtree:
                continue
            subtree.add(cur)
            for child in children_of.get(cur, []):
                if 0 <= child < len(self.items):
                    stack.append(child)
        leaves = {idx for idx in subtree if not children_of.get(idx)}
        return subtree, leaves

    @staticmethod
    def _find_same_stem_video_for_nfo(nfo_path: Path) -> Path | None:
        try:
            if (not nfo_path.exists()) or (not nfo_path.is_file()):
                return None
        except OSError:
            return None
        stem_key = nfo_path.stem.casefold()
        parent = nfo_path.parent
        try:
            for one in parent.iterdir():
                try:
                    if not one.is_file():
                        continue
                except OSError:
                    continue
                if one.suffix.lower() not in VIDEO_EXTS:
                    continue
                if one.stem.casefold() == stem_key:
                    try:
                        return one.resolve()
                    except OSError:
                        return one
        except OSError:
            pass
        return None

    def fix_titles_from_video_name_for_selected_level(self):
        seed_indices = set(self._selected_tree_indices())
        if not seed_indices:
            QMessageBox.warning(self, "提示", "请先在左侧选中一个层级节点。")
            return
        subtree_indices, leaf_indices = self._collect_subtree_leaf_indices(seed_indices)
        if not leaf_indices:
            QMessageBox.information(self, "提示", "当前选中层级下没有可处理的叶子节点。")
            return
        leaf_nfo_paths = [self.items[idx].path for idx in sorted(leaf_indices) if 0 <= idx < len(self.items)]
        preview_text = (
            f"将修复 Jellyfin 吞掉的\"]\"后的标题内容，不要对电视剧等已正常刮削数据操作。\n"
            f"选中节点: {len(seed_indices)}\n"
            f"子树节点: {len(subtree_indices)}\n"
            f"叶子节点: {len(leaf_indices)}\n"
            f"说明：仅处理真实存在且能匹配到同名视频的 NFO，虚拟项会自动跳过。\n\n"
            f"确认执行？"
        )
        if QMessageBox.question(self, "批量修复标题", preview_text) != QMessageBox.StandardButton.Yes:
            return
        self._log(f"开始后台批量修复标题，待处理叶子节点: {len(leaf_nfo_paths)}")

        # 捕获纯函数引用，后台线程不再通过 self 访问任何 Qt 对象。
        _find_video = self._find_same_stem_video_for_nfo
        _log_signal = self._log_signal

        def _bg_fix_titles():
            updated = 0
            unchanged = 0
            failed = 0
            skipped_virtual = 0
            skipped_no_video = 0
            logs: list[str] = []
            for nfo_path in leaf_nfo_paths:
                try:
                    is_real_nfo = nfo_path.exists() and nfo_path.is_file()
                except OSError:
                    is_real_nfo = False
                if not is_real_nfo:
                    skipped_virtual += 1
                    continue
                matched_video = _find_video(nfo_path)
                if matched_video is None:
                    skipped_no_video += 1
                    continue
                fixed_title = matched_video.stem
                try:
                    current = parse_nfo_fields(nfo_path)
                    old_title = str(current.get("title", "") or "").strip()
                    if old_title == fixed_title:
                        unchanged += 1
                        continue
                    write_nfo_fields(nfo_path, {"title": fixed_title})
                    updated += 1
                    logs.append(f"title 修复成功: {nfo_path} -> {fixed_title}")
                except Exception as exc:
                    failed += 1
                    logs.append(f"title 修复失败: {nfo_path} -> {exc}")
                # 每积累 20 条日志批量推送一次，减少跨线程信号频率。
                if len(logs) >= 20:
                    _log_signal.emit("\n".join(logs))
                    logs.clear()
            if logs:
                _log_signal.emit("\n".join(logs))
            return {
                "updated": updated,
                "unchanged": unchanged,
                "failed": failed,
                "skipped_virtual": skipped_virtual,
                "skipped_no_video": skipped_no_video,
            }

        def _on_bg_done(result, err):
            self._fix_title_running = False
            if err:
                QMessageBox.critical(self, "修复失败", f"后台修复任务失败：{err}")
                return
            stats = result if isinstance(result, dict) else {}
            updated = int(stats.get("updated", 0) or 0)
            unchanged = int(stats.get("unchanged", 0) or 0)
            failed_count = int(stats.get("failed", 0) or 0)
            skipped_virtual = int(stats.get("skipped_virtual", 0) or 0)
            skipped_no_video = int(stats.get("skipped_no_video", 0) or 0)
            if updated > 0:
                try:
                    self.load_selected_metadata(silent_if_empty=True, force_reload=True, include_media_resources=False)
                except Exception:
                    pass
                if hasattr(self, "_schedule_save_ui_session"):
                    self._schedule_save_ui_session()
            QMessageBox.information(
                self,
                "修复完成",
                (
                    f"处理完成。\n"
                    f"已更新: {updated}\n"
                    f"无需变更: {unchanged}\n"
                    f"虚拟/不存在 NFO（已跳过）: {skipped_virtual}\n"
                    f"无同名视频（已跳过）: {skipped_no_video}\n"
                    f"失败: {failed_count}"
                ),
            )

        if getattr(self, "_fix_title_running", False):
            QMessageBox.warning(self, "提示", "上一次修复任务仍在运行中，请稍后再试。")
            return
        self._fix_title_running = True

        # 使用独立线程，不占用全局 QThreadPool（避免和封面加载互相阻塞）。
        _done_signal = _AsyncSignals()
        _done_signal.finished.connect(_on_bg_done)
        self._fix_title_done_signal = _done_signal  # prevent GC

        def _thread_entry():
            try:
                result = _bg_fix_titles()
                _done_signal.finished.emit(result, "")
            except Exception as exc:
                _done_signal.finished.emit(None, str(exc))

        t = threading.Thread(target=_thread_entry, daemon=True)
        t.start()

    def _has_unsaved_form_changes(self) -> bool:
        if not self._last_loaded_selection_key:
            return False
        if self._collect_ui_field_snapshot() != self._loaded_field_snapshot:
            return True
        if self._collect_ui_target_paths() != self._loaded_media_paths_snapshot:
            return True
        return False

    def _confirm_save_before_selection_reload(self, new_selected_paths: set[str]) -> bool:
        """切换 NFO 前检查未保存改动；返回 True 表示允许继续切换刷新。"""
        if self._suspend_selection_change_prompt:
            return True
        prev_paths = set(self._last_loaded_selection_key or ())
        if not prev_paths:
            return True
        if {p.casefold() for p in new_selected_paths} == {p.casefold() for p in prev_paths}:
            return True
        if not self._has_unsaved_form_changes():
            return True

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("检测到未保存修改")
        box.setText("可编辑 NFO 字段或媒体资源上传已修改。\n是否先保存再切换到新选择？")
        confirm_btn = box.addButton("确认", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(confirm_btn)
        box.exec()

        if box.clickedButton() is cancel_btn:
            # 取消 = 不保存，直接切换到新选择
            return True

        self._suspend_selection_change_prompt = True
        try:
            self._reselect_by_paths(prev_paths)
        finally:
            self._suspend_selection_change_prompt = False
        saved_ok = bool(self.apply_selected_metadata())
        if not saved_ok:
            self._suspend_selection_change_prompt = True
            try:
                self._reselect_by_paths(prev_paths)
            finally:
                self._suspend_selection_change_prompt = False
            return False
        self._suspend_selection_change_prompt = True
        try:
            self._reselect_by_paths(new_selected_paths)
        finally:
            self._suspend_selection_change_prompt = False
        return True

    def _delete_removed_media_for_item(self, item: NfoItem, ui_target_paths: dict[str, set[str]]):
        existing = self._scan_existing_media_for_item(item)
        controlled_targets = set(self.image_source_edits) | set(self.extra_image_source_edits) | set(self.extra_video_source_edits) | set(
            self.extra_audio_source_edits
        )
        for target in controlled_targets:
            wanted = ui_target_paths.get(target, set())
            wanted_names = {Path(x).name.casefold() for x in wanted if x}
            for p in existing.get(target, []):
                p_resolved = str(p.resolve())
                if p_resolved in wanted:
                    continue
                # UI 覆盖模式下来源可能是缓存目录文件；同名视为“保留该目标文件”。
                if p.name.casefold() in wanted_names:
                    continue
                try:
                    p.unlink(missing_ok=True)
                    self._log(f"已删除媒体文件: {p}")
                except Exception as exc:
                    self._log(f"删除失败: {p} -> {exc}")

    def _next_available_path(self, base_path: Path) -> Path:
        if not base_path.exists():
            return base_path
        idx = 1
        while True:
            candidate = base_path.parent / f"{base_path.stem}_{idx:03d}{base_path.suffix}"
            if not candidate.exists():
                return candidate
            idx += 1

    def _apply_extra_uploads(self, nfo_path: Path, uploads: dict[str, list[Path]]):
        for upload_target, sources in uploads.items():
            target_add_mode = self._target_supports_multi(upload_target)
            total_sources = len(sources)
            for src in sources:
                rel_dir, target_name = build_extra_target_name(upload_target, src)
                dest_dir = nfo_path.parent / rel_dir if rel_dir else nfo_path.parent
                dest_dir.mkdir(parents=True, exist_ok=True)
                canonical_dest = dest_dir / target_name
                try:
                    # 已经是目标文件时，不再复制，避免产生 *_001 垃圾副本。
                    if canonical_dest.exists() and canonical_dest.resolve() == src.resolve():
                        continue
                except Exception:
                    pass
                dest = canonical_dest
                # 多文件同批次写入时，才使用自动编号避免覆盖同名文件；
                # 单文件时默认覆盖目标文件（符合“删一个后保留一个”的预期）。
                if dest.exists() and target_add_mode and total_sources > 1:
                    dest = self._next_available_path(dest)
                if dest.resolve() != src.resolve():
                    shutil.copy2(src, dest)

    def _read_image_source(self, raw: str, target: str) -> Path | bool | None:
        if not raw.strip():
            return None
        p = Path(raw.strip())
        if not p.exists():
            QMessageBox.critical(self, "图片不存在", f"{target} 路径不存在：{p}")
            return False
        if p.suffix.lower() not in SUPPORTED_IMAGE_EXTS:
            QMessageBox.critical(self, "格式错误", f"{target} 不是受支持图片格式：{p.suffix}")
            return False
        return p.resolve()

    def _read_video_source(self, raw: str, target: str) -> Path | bool | None:
        if not raw.strip():
            return None
        p = Path(raw.strip())
        if not p.exists():
            QMessageBox.critical(self, "视频不存在", f"{target} 路径不存在：{p}")
            return False
        if p.suffix.lower() not in VIDEO_EXTS:
            QMessageBox.critical(self, "格式错误", f"{target} 不是受支持视频格式：{p.suffix}")
            return False
        return p.resolve()

    def _read_audio_source(self, raw: str, target: str) -> Path | bool | None:
        if not raw.strip():
            return None
        p = Path(raw.strip())
        if not p.exists():
            QMessageBox.critical(self, "音频不存在", f"{target} 路径不存在：{p}")
            return False
        if p.suffix.lower() not in AUDIO_EXTS:
            QMessageBox.critical(self, "格式错误", f"{target} 不是受支持音频格式：{p.suffix}")
            return False
        return p.resolve()

    def apply_selected_metadata(self) -> bool:
        media_editor_maps = (
            self.image_source_edits,
            self.extra_image_source_edits,
            self.extra_video_source_edits,
            self.extra_audio_source_edits,
        )

        selected = self._selected_items()
        if (not selected) and self._last_loaded_selection_key:
            # 某些操作链路后树控件可能短暂丢失选中态，尝试用最近一次读取快照恢复。
            try:
                self._reselect_by_paths(set(self._last_loaded_selection_key))
                selected = self._selected_items()
            except Exception:
                selected = []
        if not selected:
            QMessageBox.warning(self, "提示", "请先选中至少一个 NFO 文件。")
            return False
        # 以路径去重，避免同一路径 NFO 因树节点重复被处理两次。
        uniq_selected: list[NfoItem] = []
        seen_paths: set[str] = set()
        for item in selected:
            k = str(item.path).casefold()
            if k in seen_paths:
                continue
            seen_paths.add(k)
            uniq_selected.append(item)
        selected = uniq_selected
        edits: dict[str, str] = {}
        skipped_aggregated: list[str] = []
        for tag, w in self.field_edits.items():
            value = w.text().strip()
            if not value:
                continue
            if tag not in MULTI_VALUE_TAGS and " | " in value:
                skipped_aggregated.append(tag)
                continue
            old_value = self._loaded_field_snapshot.get(tag, "")
            if value == old_value:
                continue
            edits[tag] = value
        for tag, mv in self.multi_value_editors.items():
            values = mv.get_values()
            if not values:
                continue
            value = "/".join(values)
            old_value = self._loaded_field_snapshot.get(tag, "")
            if value == old_value:
                continue
            edits[tag] = value
        if self.plot_edit is not None:
            plot_value = self.plot_edit.toPlainText().strip()
            if plot_value and "\n\n-----\n\n" not in plot_value:
                old_plot = self._loaded_field_snapshot.get("plot", "")
                if plot_value != old_plot:
                    edits["plot"] = plot_value
            elif plot_value and "\n\n-----\n\n" in plot_value:
                skipped_aggregated.append("plot")
        if skipped_aggregated:
            self._log(f"提示: 以下字段为聚合展示值，未参与写入: {', '.join(sorted(set(skipped_aggregated)))}")

        ui_target_paths = self._collect_ui_target_paths()
        media_changed_targets = {
            key for key, now_set in ui_target_paths.items()
            if now_set != self._loaded_media_paths_snapshot.get(key, set())
        }

        image_sources: dict[str, Path] = {}
        for kind, w in self.image_source_edits.items():
            one_path = w.get_paths()[0] if w.get_paths() else ""
            checked = self._read_image_source(one_path, kind)
            if checked is False:
                return False
            if isinstance(checked, Path) and kind in media_changed_targets:
                image_sources[kind] = checked

        extra_image_uploads: dict[str, list[Path]] = {}
        for target, w in self.extra_image_source_edits.items():
            source_paths: list[Path] = []
            for one in w.get_paths():
                checked = self._read_image_source(one, target)
                if checked is False:
                    return False
                if isinstance(checked, Path):
                    source_paths.append(checked)
            if source_paths and not self._target_supports_multi(target):
                source_paths = [source_paths[0]]
            if source_paths and target in media_changed_targets:
                extra_image_uploads[target] = source_paths

        extra_video_uploads: dict[str, list[Path]] = {}
        for target, w in self.extra_video_source_edits.items():
            source_paths: list[Path] = []
            for one in w.get_paths():
                checked = self._read_video_source(one, target)
                if checked is False:
                    return False
                if isinstance(checked, Path):
                    source_paths.append(checked)
            if source_paths and not self._target_supports_multi(target):
                source_paths = [source_paths[0]]
            if source_paths and target in media_changed_targets:
                extra_video_uploads[target] = source_paths

        extra_audio_uploads: dict[str, list[Path]] = {}
        for target, w in self.extra_audio_source_edits.items():
            source_paths: list[Path] = []
            for one in w.get_paths():
                checked = self._read_audio_source(one, target)
                if checked is False:
                    return False
                if isinstance(checked, Path):
                    source_paths.append(checked)
            if source_paths and not self._target_supports_multi(target):
                source_paths = [source_paths[0]]
            if source_paths and target in media_changed_targets:
                extra_audio_uploads[target] = source_paths

        has_media_target_changes = bool(media_changed_targets)
        if not edits and not image_sources and not extra_image_uploads and not extra_video_uploads and not extra_audio_uploads and not has_media_target_changes:
            QMessageBox.warning(self, "提示", "没有填写任何要写入的字段或媒体资源。")
            return False
        errors = validate_edit_values(edits)
        if errors:
            QMessageBox.critical(self, "校验失败", "\n".join(errors))
            return False
        provider_edits = self._parse_provider_ids(self.provider_ids_edit.text().strip())
        if provider_edits is False:
            return False

        preview = [
            f"目标文件: {len(selected)}",
            f"字段修改数: {len(edits)}",
            f"附加 Provider IDs: {len(provider_edits) if isinstance(provider_edits, dict) else 0}",
            f"图片上传项: {len(image_sources)}",
            f"额外图片上传目标: {len(extra_image_uploads)}，文件总数: {sum(len(v) for v in extra_image_uploads.values())}",
            f"额外视频上传目标: {len(extra_video_uploads)}，文件总数: {sum(len(v) for v in extra_video_uploads.values())}",
            f"额外音频上传目标: {len(extra_audio_uploads)}，文件总数: {sum(len(v) for v in extra_audio_uploads.values())}",
            f"媒体路径变更目标数: {len(media_changed_targets)}（含清空删除）",
        ]
        if QMessageBox.question(self, "执行预览确认", "\n".join(preview) + "\n\n确认执行写入吗？") != QMessageBox.StandardButton.Yes:
            return False

        # 用户确认后再停止预览，避免“取消写入/校验失败”导致播放器被清空来源。
        for mapping in media_editor_maps:
            for editor in mapping.values():
                try:
                    editor.stop_all_media()
                except Exception:
                    pass

        ok = 0
        failed = 0
        for item in selected:
            try:
                allowed = WRITABLE_BY_MEDIA_TYPE.get(item.media_type, COMMON_WRITABLE)
                filtered_edits = {k: v for k, v in edits.items() if k in allowed}
                merged_edits = dict(filtered_edits)
                merged_edits.update(provider_edits if isinstance(provider_edits, dict) else {})
                if merged_edits:
                    # 再次对比当前 NFO，仅写入真实变化字段，避免“无改动也写 NFO”。
                    actual_edits = dict(merged_edits)
                    try:
                        current_fields = parse_nfo_fields(item.path)
                        actual_edits = {}
                        for tag, new_val in merged_edits.items():
                            old_val = str(current_fields.get(tag, "") or "").strip()
                            if str(new_val).strip() != old_val:
                                actual_edits[tag] = new_val
                    except Exception:
                        # 回退到原逻辑：读取失败时仍尝试写入用户提交的字段。
                        actual_edits = dict(merged_edits)
                    if actual_edits:
                        write_nfo_fields(item.path, actual_edits)
                self._delete_removed_media_for_item(item, ui_target_paths)
                for kind, src in image_sources.items():
                    apply_artwork_files(item.path, src, None, thumb_kind=kind)
                self._apply_extra_uploads(item.path, extra_image_uploads)
                self._apply_extra_uploads(item.path, extra_video_uploads)
                self._apply_extra_uploads(item.path, extra_audio_uploads)
                ok += 1
                self._log(f"写入成功: {item.path}")
            except Exception as exc:
                failed += 1
                self._log(f"写入失败: {item.path} -> {exc}")
        if failed == 0:
            # 成功写入后刷新快照，避免再次点击被视为重复改动。
            self._loaded_media_paths_snapshot = dict(ui_target_paths)
            self._loaded_field_snapshot = self._collect_ui_field_snapshot()

        # 写入后重建预览卡片，恢复播放器 source/video output 绑定。
        for mapping in media_editor_maps:
            for editor in mapping.values():
                try:
                    editor._rebuild_cards()
                except Exception:
                    pass
        QMessageBox.information(self, "完成", f"处理完成：成功 {ok}，失败 {failed}")
        return failed == 0

bind_network_services_methods(JellyfinNfoQtWindow)
bind_video_dialog_methods(JellyfinNfoQtWindow)
bind_scan_tree_methods(JellyfinNfoQtWindow)
bind_session_pg_methods(JellyfinNfoQtWindow)
