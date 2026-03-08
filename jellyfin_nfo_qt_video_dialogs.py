from __future__ import annotations

import math
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, QTimer, Qt, QUrl, Signal, QEvent
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap, QRegion
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFrame,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QMessageBox,
    QPushButton,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from jellyfin_video_tools import ffmpeg_available, parse_segments, build_segment_previews, run_segment_export
from jellyfin_extras_rules import VIDEO_EXTS

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif", ".apng"}


# ---------------------------------------------------------------------------
#  CropState — deterministic crop / resolution / AR state machine
#  Three controls: AR selection, resolution R_w×R_h, crop box C_x/C_y/C_w/C_h.
#  All outputs are integers via round().  See spec sections I–VII.
# ---------------------------------------------------------------------------

class CropState:
    """Deterministic crop / resolution / AR state machine (Sections I–VII)."""

    # ---- construction ----
    def __init__(self, O_w: int, O_h: int):
        self.O_w = max(1, int(O_w))
        self.O_h = max(1, int(O_h))
        self.min_sz = max(1, math.floor(0.005 * min(self.O_w, self.O_h)))

        # Crop box (source pixels)
        self.C_x: int = 0
        self.C_y: int = 0
        self.C_w: int = self.O_w
        self.C_h: int = self.O_h

        # Export resolution
        self.R_w: int = self.O_w
        self.R_h: int = self.O_h

        # AR mode
        self.AR_mode: str = "free"          # "free" | "fixed"
        self.ratio: float = 1.0             # p/q  (used only when fixed)

        # History / control state
        self.manualResEdited: bool = False
        self.resLastEditedField: str = "none"   # w | h | both | none
        self.lastMaxAllowed_w: int = self.O_w
        self.lastMaxAllowed_h: int = self.O_h
        self.lastAction: str = "none"           # crop | res | ar | none

        self._on_change = None

    # ---- helpers ----
    def set_on_change(self, cb):
        self._on_change = cb

    def _notify(self):
        if self._on_change:
            self._on_change()

    @staticmethod
    def _clamp(v, lo, hi):
        return max(int(lo), min(int(hi), int(round(v))))

    def _max_allowed(self) -> tuple[int, int]:
        mw = max(1, math.floor(min(self.O_w, 2 * self.C_w)))
        mh = max(1, math.floor(min(self.O_h, 2 * self.C_h)))
        return mw, mh

    def _enforce(self):
        self.C_w = self._clamp(self.C_w, self.min_sz, self.O_w)
        self.C_h = self._clamp(self.C_h, self.min_sz, self.O_h)
        self.C_x = self._clamp(self.C_x, 0, self.O_w - self.C_w)
        self.C_y = self._clamp(self.C_y, 0, self.O_h - self.C_h)
        self.R_w = self._clamp(self.R_w, 1, self.O_w)
        self.R_h = self._clamp(self.R_h, 1, self.O_h)

    def _iter_fix_ratio(self, lim_w: int, lim_h: int, iters: int = 3):
        """Section V — iterative fixed-ratio correction."""
        if not self.ratio or self.ratio <= 0:
            return
        for _ in range(iters):
            ok = True
            rh = round(self.R_w / self.ratio)
            if rh > lim_h:
                self.R_h = lim_h
                self.R_w = self._clamp(round(self.R_h * self.ratio), 1, lim_w)
                ok = False
            else:
                self.R_h = self._clamp(rh, 1, lim_h)
            if self.R_w > lim_w:
                self.R_w = lim_w
                self.R_h = self._clamp(round(self.R_w / self.ratio), 1, lim_h)
                ok = False
            if ok:
                break

    def crop_expr(self) -> str:
        return f"{self.C_w}:{self.C_h}:{self.C_x}:{self.C_y}"

    # ---- Section IV.1: onARChange ----
    def onARChange(self, new_mode: str, new_ratio: float | None = None):
        self.lastAction = "ar"
        self.AR_mode = new_mode

        if new_mode == "fixed" and new_ratio and new_ratio > 0:
            self.ratio = new_ratio
            # Always maximise: fill full width or full height of source
            if round(self.O_w / self.ratio) <= self.O_h:
                self.C_w = self.O_w
                self.C_h = round(self.O_w / self.ratio)
            else:
                self.C_h = self.O_h
                self.C_w = round(self.O_h * self.ratio)
        else:
            # free: reset to full original frame
            self.C_w = self.O_w
            self.C_h = self.O_h

        self.C_w = self._clamp(self.C_w, self.min_sz, self.O_w)
        self.C_h = self._clamp(self.C_h, self.min_sz, self.O_h)
        # Centre the crop box
        self.C_x = self._clamp((self.O_w - self.C_w) // 2, 0, self.O_w - self.C_w)
        self.C_y = self._clamp((self.O_h - self.C_h) // 2, 0, self.O_h - self.C_h)

        mw, mh = self._max_allowed()

        # AR change always resets resolution to maximum
        if self.AR_mode == "fixed" and self.ratio > 0:
            self.R_w = mw
            rh = round(self.R_w / self.ratio)
            if rh <= mh:
                self.R_h = max(1, rh)
            else:
                self.R_h = mh
                self.R_w = self._clamp(round(self.R_h * self.ratio), 1, mw)
        else:
            self.R_w, self.R_h = mw, mh
        self.manualResEdited = False
        self.resLastEditedField = "none"

        self.lastMaxAllowed_w, self.lastMaxAllowed_h = mw, mh
        self._enforce()
        self._notify()

    # ---- Section IV.3: onResBlur ----
    def onResBlur(self, rw_text: str, rh_text: str, last_edited: str = "both"):
        try:
            rw = max(1, round(float((rw_text or "").strip() or "0"))) if (rw_text or "").strip() else 0
        except Exception:
            rw = 0
        try:
            rh = max(1, round(float((rh_text or "").strip() or "0"))) if (rh_text or "").strip() else 0
        except Exception:
            rh = 0

        if rw > self.O_w:
            rw = self.O_w
        if rh > self.O_h:
            rh = self.O_h

        crop_ratio = self.C_w / max(1, self.C_h)

        if self.AR_mode == "fixed" and self.ratio > 0:
            if last_edited == "w" and rw > 0:
                self.R_w = rw
                rh_c = round(rw / self.ratio)
                if rh_c <= self.O_h:
                    self.R_h = self._clamp(rh_c, 1, self.O_h)
                else:
                    self.R_h = self.O_h
                    self.R_w = self._clamp(round(self.R_h * self.ratio), 1, self.O_w)
                self._iter_fix_ratio(self.O_w, self.O_h)
            elif last_edited == "h" and rh > 0:
                self.R_h = rh
                rw_c = round(rh * self.ratio)
                if rw_c <= self.O_w:
                    self.R_w = self._clamp(rw_c, 1, self.O_w)
                else:
                    self.R_w = self.O_w
                    self.R_h = self._clamp(round(self.R_w / self.ratio), 1, self.O_h)
                self._iter_fix_ratio(self.O_w, self.O_h)
            else:  # both
                if rw > 0:
                    self.R_w = rw
                if rh > 0:
                    self.R_h = rh
                self._iter_fix_ratio(self.O_w, self.O_h)
        else:
            # free mode — resolution drives crop box
            rw_valid = rw > 0
            rh_valid = rh > 0
            if rw_valid and rh_valid:
                self.R_w = self._clamp(rw, 1, self.O_w)
                self.R_h = self._clamp(rh, 1, self.O_h)
            elif rw_valid:
                self.R_w = self._clamp(rw, 1, self.O_w)
                if crop_ratio > 0:
                    self.R_h = self._clamp(round(rw / crop_ratio), 1, self.O_h)
            elif rh_valid:
                self.R_h = self._clamp(rh, 1, self.O_h)
                if crop_ratio > 0:
                    self.R_w = self._clamp(round(rh * crop_ratio), 1, self.O_w)
            # Crop follows resolution — clamp and centre
            self.C_w = self._clamp(self.R_w, self.min_sz, self.O_w)
            self.C_h = self._clamp(self.R_h, self.min_sz, self.O_h)
            self.C_x = self._clamp((self.O_w - self.C_w) // 2, 0, self.O_w - self.C_w)
            self.C_y = self._clamp((self.O_h - self.C_h) // 2, 0, self.O_h - self.C_h)

        self.R_w = self._clamp(self.R_w, 1, self.O_w)
        self.R_h = self._clamp(self.R_h, 1, self.O_h)
        self.manualResEdited = True
        self.resLastEditedField = last_edited
        self.lastAction = "res"
        self.lastMaxAllowed_w = max(1, self.C_w)
        self.lastMaxAllowed_h = max(1, self.C_h)
        self._enforce()
        self._notify()

    def _free_dual_res(self, rw_in: int, rh_in: int):
        """Section IV.3 — free + both fields: adjust crop to match target ratio."""
        rw_in = min(rw_in, self.O_w)
        rh_in = min(rh_in, self.O_h)
        if rh_in <= 0:
            self.R_w = self._clamp(rw_in, 1, self.O_w)
            return
        target_ratio = rw_in / rh_in
        orig_cx = self.C_x + self.C_w / 2.0
        orig_cy = self.C_y + self.C_h / 2.0
        A = max(1, self.C_w * self.C_h)

        cw = round(math.sqrt(A * target_ratio))
        ch = round(cw / target_ratio) if target_ratio > 0 else 1

        if not (self.min_sz <= cw <= self.O_w and self.min_sz <= ch <= self.O_h):
            for _ in range(3):
                sd = min(1.0, self.O_w / max(1, cw), self.O_h / max(1, ch))
                cw = cw * sd
                ch = ch * sd
                su = max(1.0, self.min_sz / max(1e-9, cw), self.min_sz / max(1e-9, ch))
                cw = cw * su
                ch = ch * su
                cw = round(cw)
                ch = round(ch)

        self.C_w = self._clamp(cw, self.min_sz, self.O_w)
        self.C_h = self._clamp(ch, self.min_sz, self.O_h)
        self.C_x = self._clamp(round(orig_cx - self.C_w / 2.0), 0, self.O_w - self.C_w)
        self.C_y = self._clamp(round(orig_cy - self.C_h / 2.0), 0, self.O_h - self.C_h)

        nmw = max(1, math.floor(min(self.O_w, 2 * self.C_w)))
        nmh = max(1, math.floor(min(self.O_h, 2 * self.C_h)))
        self.R_w = self._clamp(round(rw_in), 1, nmw)
        self.R_h = self._clamp(round(rh_in), 1, nmh)

        if self.C_h > 0:
            cr = self.C_w / self.C_h
            rh_alt = round(self.R_w / cr)
            if rh_alt <= nmh:
                self.R_h = max(1, rh_alt)
            else:
                self.R_h = nmh
                self.R_w = self._clamp(round(self.R_h * cr), 1, nmw)
        self.lastMaxAllowed_w, self.lastMaxAllowed_h = nmw, nmh

    # ---- Section IV.4/5: crop drag ----
    def onCropDragMove(self, cx: int, cy: int, cw: int, ch: int):
        """Real-time constraint during drag — updates C only, never R."""
        self.C_w = self._clamp(cw, self.min_sz, self.O_w)
        self.C_h = self._clamp(ch, self.min_sz, self.O_h)
        self.C_x = self._clamp(cx, 0, self.O_w - self.C_w)
        self.C_y = self._clamp(cy, 0, self.O_h - self.C_h)

    def onCropDragEnd(self):
        """Update R after drag release.

        Rules when manualResEdited is False:  R = C  (resolution tracks crop).
        Rules when manualResEdited is True:
          fixed mode — grow box: R unchanged; shrink box: R = C (ratio-adjusted).
          free mode  — grow box: keep last-edited R, adjust the other by crop ratio;
                       shrink box: both R follow C.
        """
        crop_ratio = self.C_w / max(1, self.C_h)
        old_cw = self.lastMaxAllowed_w or self.C_w
        old_ch = self.lastMaxAllowed_h or self.C_h
        any_shrank = (self.C_w < old_cw) or (self.C_h < old_ch)

        if not self.manualResEdited:
            # R directly follows C
            if self.AR_mode == "fixed" and self.ratio > 0:
                self.R_w = max(1, self.C_w)
                rh = round(self.R_w / self.ratio)
                if rh <= self.O_h:
                    self.R_h = max(1, rh)
                else:
                    self.R_h = max(1, self.C_h)
                    self.R_w = self._clamp(round(self.R_h * self.ratio), 1, self.O_w)
            else:
                self.R_w = max(1, self.C_w)
                self.R_h = max(1, self.C_h)
        elif self.AR_mode == "fixed" and self.ratio > 0:
            # Fixed ratio + manual res: grow → R untouched; shrink → R = C
            if any_shrank:
                self.R_w = max(1, self.C_w)
                rh = round(self.R_w / self.ratio)
                if rh <= self.O_h:
                    self.R_h = max(1, rh)
                else:
                    self.R_h = max(1, self.C_h)
                    self.R_w = self._clamp(round(self.R_h * self.ratio), 1, self.O_w)
            # else: R stays unchanged
        else:
            # Free + manual res
            if any_shrank:
                # Shrink → both R follow C
                self.R_w = max(1, self.C_w)
                self.R_h = max(1, self.C_h)
            else:
                # Grow → keep last-edited R, adjust the other by new crop ratio
                if self.resLastEditedField == "w" and crop_ratio > 0:
                    self.R_h = self._clamp(round(self.R_w / crop_ratio), 1, self.O_h)
                elif self.resLastEditedField == "h" and crop_ratio > 0:
                    self.R_w = self._clamp(round(self.R_h * crop_ratio), 1, self.O_w)
                # else (both/none): R unchanged

        self.lastMaxAllowed_w = max(1, self.C_w)
        self.lastMaxAllowed_h = max(1, self.C_h)
        self.lastAction = "crop"
        self._enforce()
        self._notify()


def _image_target_edit(self, target_key: str, is_extra: bool):
    return self.extra_image_source_edits.get(target_key) if is_extra else self.image_source_edits.get(target_key)


def _pick_media_for_image_target(self, target_key: str, is_extra: bool):
    edit = _image_target_edit(self, target_key, is_extra)
    if edit is None:
        return
    path, _ = QFileDialog.getOpenFileName(
        self,
        "选择图片或视频",
        "",
        "媒体文件 (*.jpg *.jpeg *.png *.webp *.gif *.bmp *.tif *.tiff *.avif *.apng *.mp4 *.mkv *.avi *.mov *.wmv *.m4v *.webm *.mpeg *.mpg);;所有文件 (*.*)",
    )
    if not path:
        return
    edit.set_paths([str(path)])


def _detect_url_media_kind(url: str) -> str:
    txt = str(url or "").strip()
    if not txt:
        return "image"
    parsed = urlsplit(txt)
    path = parsed.path
    suf = Path(path).suffix.lower()
    if suf in VIDEO_EXTS:
        return "video"
    if suf in _IMAGE_EXTS:
        return "image"
    query = {k.lower(): v for k, v in parse_qs(parsed.query or "").items()}
    fmt = ((query.get("format") or [""])[0] or "").strip().lower()
    if fmt:
        if not fmt.startswith("."):
            fmt = f".{fmt}"
        if fmt in _IMAGE_EXTS:
            return "image"
        if fmt in VIDEO_EXTS:
            return "video"
    lower = txt.lower()
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host in {"pbs.twimg.com", "images.unsplash.com", "i.imgur.com"}:
        return "image"
    if any(k in lower for k in ("youtube.com", "youtu.be", "twitter.com", "x.com", "bilibili.com")):
        return "video"
    return "image"


def _pick_video_for_image_target(self, target_key: str, is_extra: bool):
    edit = _image_target_edit(self, target_key, is_extra)
    if edit is None:
        return
    p = self._pick_video()
    if not p:
        return
    edit.set_paths([str(p)])


def _download_url_for_image_target(self, edit, target_key: str, url: str, kind: str):
    def _apply_path(path_obj):
        path = path_obj if isinstance(path_obj, Path) else None
        if path is None:
            QMessageBox.warning(self, "下载失败", "下载失败，请查看日志。")
            return
        edit.set_paths([str(path)])

    def _job():
        if kind == "image":
            return self._download_image_from_url(url, target_key, show_dialog=False)
        return self._download_binary_from_url(url, target_key, kind="video", show_dialog=False)

    def _done(path_obj, err):
        if err:
            QMessageBox.critical(self, "下载失败", str(err))
            return
        path = path_obj if isinstance(path_obj, Path) else None
        if path is not None:
            edit.set_paths([str(path)])
            return
        normalized_video_url = self._normalize_video_download_url(url) if kind == "video" else url
        if (
            kind == "video"
            and self._last_ytdlp_need_cookie
            and (self._is_youtube_url(normalized_video_url) or self._is_twitter_url(normalized_video_url))
        ):
            login_tip = (
                "请在已打开的 WebView2 窗口完成登录。\n登录完成后点击“确认并继续”（将自动使用登录 Cookies）。"
            )
            if self._is_twitter_url(normalized_video_url):
                login_tip = (
                    "请在已打开的 WebView2 窗口登录 X/Twitter 账号。\n"
                    "登录完成后点击“确认并继续”（将自动使用登录 Cookies）。"
                )
            self._prompt_chromium_login_cookie_async(
                normalized_video_url,
                login_tip,
                lambda retry_cookie: self._run_async(
                    lambda: self._download_video_by_ytdlp(normalized_video_url, target_key, cookie_source=retry_cookie),
                    lambda retry_path_obj, retry_err: (
                        QMessageBox.critical(self, "下载失败", str(retry_err))
                        if retry_err
                        else _apply_path(retry_path_obj)
                    ),
                )
                if retry_cookie
                else None,
            )
            return
        QMessageBox.warning(self, "下载失败", "下载失败，请查看日志。")

    self._run_async(_job, _done)


def _open_video_url_for_image_target(self, target_key: str, is_extra: bool):
    edit = _image_target_edit(self, target_key, is_extra)
    if edit is None:
        return
    url, ok = QInputDialog.getText(self, "输入链接", "视频链接（http/https）")
    if not ok or not str(url or "").strip():
        return
    _download_url_for_image_target(self, edit, target_key, str(url), "video")


def _open_media_url_for_image_target(self, target_key: str, is_extra: bool):
    edit = _image_target_edit(self, target_key, is_extra)
    if edit is None:
        return
    url, ok = QInputDialog.getText(self, "输入链接", "图片/视频链接（http/https）")
    if not ok or not str(url or "").strip():
        return
    kind = _detect_url_media_kind(url)
    _download_url_for_image_target(self, edit, target_key, str(url), kind)


def _open_video_editor_for_image_target(self, target_key: str, is_extra: bool):
    edit = _image_target_edit(self, target_key, is_extra)
    if edit is None:
        return
    paths = edit.get_selected_paths() or edit.get_paths()
    chosen = ""
    for one in paths:
        txt = str(one or "").strip()
        if txt and Path(txt).suffix.lower() in VIDEO_EXTS:
            chosen = txt
            break
    if not chosen:
        p = self._pick_video()
        if not p:
            QMessageBox.warning(self, "提示", "请先选择或下载视频。")
            return
        chosen = str(p)
        edit.set_paths([chosen])
    checked = self._read_video_source(chosen, target_key)
    if checked is False or checked is None:
        return
    self._open_video_segment_dialog([checked], edit, output_kind="image")


def _open_media_editor_for_image_target(self, target_key: str, is_extra: bool):
    edit = _image_target_edit(self, target_key, is_extra)
    if edit is None:
        return
    paths = edit.get_selected_paths() or edit.get_paths()
    if not paths:
        QMessageBox.warning(self, "提示", "请先选择或下载图片/视频。")
        return
    chosen = str(paths[0] or "").strip()
    if not chosen:
        QMessageBox.warning(self, "提示", "请先选择或下载图片/视频。")
        return
    suffix = Path(chosen).suffix.lower()
    if suffix in VIDEO_EXTS:
        self._open_video_editor_for_image_target(target_key, is_extra)
        return
    self._open_image_crop_editor(target_key, is_extra)


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


class _VideoCropOverlay(QWidget):
    cropChanged = Signal(str)
    dragEnded = Signal()
    fallthroughClick = Signal()   # click outside crop box → e.g. toggle play
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
        self._pending_move_origin: QPoint | None = None   # click vs drag detect
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
        hit = self._hit_mode(event.position().toPoint())
        if not hit:
            # Outside crop box → play/pause
            self._drag_mode = ""
            self.fallthroughClick.emit()
            return
        if hit == "move":
            # Inside box but not on edge — might be click or drag
            self._drag_mode = "pending_move"
            self._pending_move_origin = event.position().toPoint()
            self._drag_start = self._widget_to_src(event.position().toPoint())
            self._drag_crop = self._crop.copy()
            return
        # Edge/corner resize → start immediately
        self._drag_mode = hit
        self._pending_move_origin = None
        self._drag_start = self._widget_to_src(event.position().toPoint())
        self._drag_crop = self._crop.copy()
        self.moved.emit()
        self.update()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        p = event.position().toPoint()
        self.moved.emit()
        if self._enabled and self._drag_mode == "pending_move":
            # Check if moved enough to start actual move-drag (5 px threshold)
            if self._pending_move_origin is not None:
                d = p - self._pending_move_origin
                if abs(d.x()) > 4 or abs(d.y()) > 4:
                    self._drag_mode = "move"
                    self._pending_move_origin = None
        if self._enabled and self._drag_mode and self._drag_mode != "pending_move":
            sx, sy = self._widget_to_src(p)
            self._apply_drag(sx, sy)
            self._emit_crop_changed()
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        was_pending = (self._drag_mode == "pending_move")
        had_real_drag = bool(self._drag_mode) and not was_pending
        if self._drag_mode and self._drag_mode != "pending_move":
            self._emit_crop_changed()
        self._drag_mode = ""
        self._pending_move_origin = None
        if was_pending:
            # Didn't drag far enough → treat as click → play/pause
            self.fallthroughClick.emit()
        elif had_real_drag:
            self.dragEnded.emit()
        super().mouseReleaseEvent(event)

    def sync_from_crop_state(self, cs: "CropState"):
        """Read authoritative C_x/C_y/C_w/C_h + AR from *cs* into the overlay."""
        self._crop = [float(cs.C_x), float(cs.C_y), float(cs.C_w), float(cs.C_h)]
        self._aspect_ratio = cs.ratio if cs.AR_mode == "fixed" else None
        self._enabled = True
        self.update()

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
        p = QPainter(self)
        shade = QColor(0, 0, 0, 95)
        full = self.rect()
        if rect != full:
            ry2 = rect.y() + rect.height()
            rx2 = rect.x() + rect.width()
            fy2 = full.y() + full.height()
            fx2 = full.x() + full.width()
            p.fillRect(QRect(full.x(), full.y(), full.width(), max(0, rect.y() - full.y())), shade)
            p.fillRect(QRect(full.x(), ry2, full.width(), max(0, fy2 - ry2)), shade)
            p.fillRect(QRect(full.x(), rect.y(), max(0, rect.x() - full.x()), rect.height()), shade)
            p.fillRect(QRect(rx2, rect.y(), max(0, fx2 - rx2), rect.height()), shade)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor("#3ea6ff"), 2))
        draw_rect = rect
        if draw_rect == full:
            draw_rect = draw_rect.adjusted(6, 6, -6, -6)
        p.drawRect(draw_rect)
        p.end()


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


class _NoShadowPopupComboBox(QComboBox):
    """与图片裁切一致：自绘箭头 + 无阴影弹层。"""

    def paintEvent(self, event):
        super().paintEvent(event)
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
        popup.show()
        anchor = self.mapToGlobal(QPoint(0, self.height()))
        popup.move(anchor.x(), popup.y())
        rect = popup.rect().adjusted(0, 0, -1, -1)
        if rect.width() > 0 and rect.height() > 0:
            path = QPainterPath()
            path.addRoundedRect(QRectF(rect), 12, 12)
            popup.setMask(QRegion(path.toFillPolygon().toPolygon()))


def _open_video_editor_for_target(self, target_key: str):
    edit = self.extra_video_source_edits.get(target_key)
    if edit is None:
        return
    paths = edit.get_paths()
    if not paths:
        QMessageBox.warning(self, "提示", "请先选择或下载视频。")
        return
    selected_paths = edit.get_selected_paths()
    chosen_path = selected_paths[0] if selected_paths else paths[0]
    checked = self._read_video_source(chosen_path, target_key)
    if checked is False or checked is None:
        return
    self._open_video_segment_dialog([checked], edit)

def _probe_video_duration_sec(self, video: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return max(0.0, float((proc.stdout or "0").strip() or "0"))
    except Exception:
        return 0.0

def _probe_video_size(self, video: Path) -> tuple[int, int]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return (0, 0)
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
                str(video),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        one = ((proc.stdout or "").strip().splitlines() or [""])[0].strip()
        m = re.match(r"^(\d+)x(\d+)$", one)
        if not m:
            return (0, 0)
        return (int(m.group(1)), int(m.group(2)))
    except Exception:
        return (0, 0)

def _extract_video_frame_pixmap(self, video: Path, sec: float) -> QPixmap | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-ss",
                f"{max(0.0, sec):.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "-",
            ],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        pix = QPixmap()
        if not pix.loadFromData(proc.stdout, "PNG"):
            return None
        return pix
    except Exception:
        return None

def _sec_to_hhmmss_mmm(self, sec: float) -> str:
    ms = max(0, int(round(sec * 1000)))
    h = ms // 3_600_000
    ms %= 3_600_000
    m = ms // 60_000
    ms %= 60_000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

def _ratio_text_to_float(self, text: str) -> float | None:
    t = (text or "").strip().lower()
    if not t or t == "free":
        return None
    try:
        if ":" in t:
            a, b = t.split(":", 1)
            v = float(a.strip()) / float(b.strip())
        else:
            v = float(t)
        return v if v > 0 else None
    except Exception:
        return None

def _center_crop_expr(self, width: int, height: int, ratio: float) -> str:
    if width <= 0 or height <= 0 or ratio <= 0:
        return "iw:ih:0:0"
    src_ratio = float(width) / float(height)
    if src_ratio >= ratio:
        crop_h = height
        crop_w = int(round(crop_h * ratio))
    else:
        crop_w = width
        crop_h = int(round(crop_w / ratio))
    crop_w = max(2, min(width, crop_w))
    crop_h = max(2, min(height, crop_h))
    crop_x = max(0, (width - crop_w) // 2)
    crop_y = max(0, (height - crop_h) // 2)
    return f"{crop_w}:{crop_h}:{crop_x}:{crop_y}"

def _draw_crop_preview_pixmap(self, src: QPixmap, crop_expr: str, src_w: int = 0, src_h: int = 0) -> QPixmap:
    pix = src.copy()
    if pix.isNull():
        return pix
    parts = [p.strip() for p in (crop_expr or "").split(":")]
    if len(parts) != 4:
        return pix
    try:
        w = int(float(parts[0]))
        h = int(float(parts[1]))
        x = int(float(parts[2]))
        y = int(float(parts[3]))
    except Exception:
        return pix
    sw = max(1, pix.width())
    sh = max(1, pix.height())
    base_w = max(1, src_w or sw)
    base_h = max(1, src_h or sh)
    rx = int(x * sw / base_w)
    ry = int(y * sh / base_h)
    rw = int(w * sw / base_w)
    rh = int(h * sh / base_h)
    rect = QRect(rx, ry, max(2, rw), max(2, rh)).intersected(QRect(0, 0, sw, sh))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(QPen(QColor("#3ea6ff"), 3))
    painter.drawRect(rect)
    painter.end()
    return pix

def _open_video_segment_dialog(self, videos: list[Path], out_edit: object | None = None, output_kind: str = "video"):
    if not ffmpeg_available():
        QMessageBox.critical(self, "缺少 FFmpeg", "未检测到 ffmpeg，请先安装并加入 PATH。")
        return
    if not videos:
        QMessageBox.warning(self, "提示", "当前没有可处理视频。")
        return
    dlg = QDialog(self)
    dlg.setWindowTitle("视频裁切分段（可视化）")
    dlg.resize(1100, 760)
    root = QVBoxLayout(dlg)

    selected_video = videos[0]
    duration_sec = self._probe_video_duration_sec(selected_video)
    duration_ms = max(1000, int(duration_sec * 1000))
    v_w, v_h = self._probe_video_size(selected_video)
    cs = CropState(v_w if v_w > 0 else 1920, v_h if v_h > 0 else 1080)

    title_row = QHBoxLayout()
    title_row.addWidget(QLabel("已检测视频: 1 个"))
    title_row.addWidget(QLabel(str(selected_video)), 1)
    root.addLayout(title_row)

    video_frame = QFrame()
    video_frame.setStyleSheet("QFrame{background:#111;border:1px solid #cfd6de;border-radius:6px;}")
    video_lay = QVBoxLayout(video_frame)
    video_lay.setContentsMargins(0, 0, 0, 0)
    video_lay.setSpacing(0)
    video_widget = _VideoPreviewLabel()
    video_widget.setMinimumHeight(460)
    video_widget.setStyleSheet("QLabel{background:#000;border:none;}")
    video_lay.addWidget(video_widget)
    root.addWidget(video_frame, 1)

    state: dict[str, object] = {"looping": False}
    player = QMediaPlayer(dlg)
    audio = QAudioOutput(dlg)
    video_sink = QVideoSink(dlg)
    player.setAudioOutput(audio)
    player.setVideoOutput(video_sink)
    player.setSource(QUrl.fromLocalFile(str(selected_video)))

    overlay = _VideoCropOverlay(video_widget, v_w if v_w > 0 else 1, v_h if v_h > 0 else 1, parent=dlg)
    overlay.show()
    overlay.raise_()

    play_btn = QPushButton("", dlg)
    play_btn.setIcon(dlg.style().standardIcon(QStyle.SP_MediaPlay))
    play_btn.setFixedSize(54, 54)
    play_btn.setStyleSheet(
        "QPushButton{background:rgba(18,22,28,120);border:1px solid rgba(255,255,255,50);border-radius:27px;}"
        "QPushButton:hover{background:rgba(18,22,28,170);}"
    )
    play_btn.setCursor(Qt.PointingHandCursor)

    timeline_frame = QFrame(dlg)
    timeline_frame.setStyleSheet("QFrame{background:rgba(18,22,28,95);border:1px solid rgba(255,255,255,35);border-radius:9px;}")
    timeline_lay = QHBoxLayout(timeline_frame)
    timeline_lay.setContentsMargins(10, 8, 10, 8)
    timeline_lay.setSpacing(8)
    range_slider = _RangeSlider()
    range_slider.setRange(0, duration_ms)
    range_slider.setMinimumSpan(500)
    range_slider.setValues(0, min(duration_ms, 30_000))
    range_slider.setPlayhead(0)
    timeline_lay.addWidget(range_slider, 1)

    drag_hint = QLabel("", dlg)
    drag_hint.setStyleSheet("QLabel{color:#fff;background:rgba(0,0,0,140);border-radius:6px;padding:3px 8px;}")
    drag_hint.hide()
    drag_hint_timer = QTimer(dlg)
    drag_hint_timer.setSingleShot(True)
    drag_hint_timer.setInterval(900)
    drag_hint_timer.timeout.connect(drag_hint.hide)

    ratio_frame = QFrame(dlg)
    ratio_lay = QHBoxLayout(ratio_frame)
    ratio_lay.setContentsMargins(0, 0, 0, 0)
    ratio_lay.setSpacing(6)
    ratio_lbl = QLabel("比例")
    ratio_lbl.setStyleSheet("QLabel{color:#fff;}")
    ratio_combo = _NoShadowPopupComboBox()
    ratio_combo.setEditable(True)
    ratio_combo.setFixedWidth(120)
    ratio_combo.setInsertPolicy(QComboBox.NoInsert)
    ratio_combo.setDuplicatesEnabled(False)
    popup_view = QListView(ratio_combo)
    popup_view.setObjectName("ratioPopupView")
    popup_view.setFrameShape(QFrame.NoFrame)
    popup_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    popup_view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    popup_view.setAttribute(Qt.WA_StyledBackground, True)
    popup_view.viewport().setObjectName("ratioPopupViewport")
    popup_view.viewport().setAttribute(Qt.WA_StyledBackground, True)
    ratio_combo.setView(popup_view)
    ratio_combo.setStyleSheet(
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
    ratio_combo.addItems(["16:9", "9:16", "4:3", "3:4", "1:1", "2:3", "3:2", "21:9", "free"])
    ratio_combo.setCurrentText("16:9")
    ratio_lay.addWidget(ratio_lbl)
    ratio_lay.addWidget(ratio_combo)
    res_lbl = QLabel("分辨率")
    res_lbl.setStyleSheet("QLabel{color:#fff;}")
    res_w = QLineEdit()
    res_w.setFixedWidth(62)
    res_w.setAlignment(Qt.AlignCenter)
    res_x = QLabel("x")
    res_x.setStyleSheet("QLabel{color:#fff;}")
    res_h = QLineEdit()
    res_h.setFixedWidth(62)
    res_h.setAlignment(Qt.AlignCenter)
    ratio_lay.addWidget(res_lbl)
    ratio_lay.addWidget(res_w)
    ratio_lay.addWidget(res_x)
    ratio_lay.addWidget(res_h)

    info_lbl = QLabel("", dlg)
    info_lbl.setStyleSheet("QLabel{color:#fff;background:rgba(0,0,0,130);border-radius:6px;padding:3px 8px;}")

    apply_btn = QPushButton("应用", dlg)
    apply_btn.setStyleSheet(
        "QPushButton{color:#111;background:#ffffff;border:1px solid #d1d5db;border-radius:8px;padding:6px 14px;font-weight:600;}"
        "QPushButton:hover{background:#f3f4f6;border-color:#c5cbd3;}"
        "QPushButton:pressed{background:#e5e7eb;}"
    )
    convert_btn = QPushButton("转换成图片", dlg)
    convert_btn.setStyleSheet(
        "QPushButton{color:#111;background:#ffffff;border:1px solid #d1d5db;border-radius:8px;padding:6px 14px;font-weight:600;}"
        "QPushButton:hover{background:#f3f4f6;border-color:#c5cbd3;}"
        "QPushButton:pressed{background:#e5e7eb;}"
    )
    convert_btn.setVisible(str(output_kind).lower() == "image")

    hide_timer = QTimer(dlg)
    hide_timer.setSingleShot(True)
    hide_timer.setInterval(900)
    hover_state = {"video": False, "timeline": False}

    def _place_overlays():
        top_left = video_widget.mapTo(dlg, QPoint(0, 0))
        vx, vy = top_left.x(), top_left.y()
        vw, vh = video_widget.width(), video_widget.height()
        margin = 12

        play_btn.move(vx + (vw - play_btn.width()) // 2, vy + (vh - play_btn.height()) // 2)

        ratio_frame.adjustSize()
        convert_btn.adjustSize()
        apply_btn.adjustSize()
        # 比例/应用固定放在视频框外下方，避免与视频内容重叠
        ratio_y = min(dlg.height() - ratio_frame.height() - margin, vy + vh + 8)
        apply_y = min(dlg.height() - apply_btn.height() - margin, vy + vh + 8)

        tl_w = max(180, vw - margin * 2)
        tl_h = max(40, timeline_frame.sizeHint().height())
        timeline_y = min(ratio_y, apply_y) - tl_h - 8
        timeline_frame.setGeometry(vx + margin, max(vy + margin, timeline_y), tl_w, tl_h)

        info_lbl.adjustSize()
        info_lbl.move(vx + margin, timeline_frame.y() - info_lbl.height() - 8)

        ratio_frame.move(vx + margin, ratio_y)

        apply_x = vx + vw - apply_btn.width() - margin
        apply_btn.move(apply_x, apply_y)
        if convert_btn.isVisible():
            convert_btn.move(apply_x - convert_btn.width() - 8, apply_y)

        drag_hint.adjustSize()
        drag_hint.move(vx + (vw - drag_hint.width()) // 2, timeline_frame.y() - drag_hint.height() - 10)

        play_btn.raise_()
        overlay.raise_()          # above play_btn → crop drag always works
        timeline_frame.raise_()
        info_lbl.raise_()
        ratio_frame.raise_()
        convert_btn.raise_()
        apply_btn.raise_()
        drag_hint.raise_()

    def _show_timeline():
        _place_overlays()
        timeline_frame.show()
        play_btn.show()
        if player.playbackState() == QMediaPlayer.PlayingState:
            hide_timer.start()
        else:
            hide_timer.stop()

    def _hide_timeline_if_needed():
        if player.playbackState() != QMediaPlayer.PlayingState:
            return
        if hover_state["video"] or hover_state["timeline"]:
            hide_timer.start()
            return
        timeline_frame.hide()
        play_btn.hide()

    hide_timer.timeout.connect(_hide_timeline_if_needed)

    def _timeline_enter(event):
        hover_state["timeline"] = True
        _show_timeline()
        QFrame.enterEvent(timeline_frame, event)

    def _timeline_leave(event):
        hover_state["timeline"] = False
        QFrame.leaveEvent(timeline_frame, event)

    timeline_frame.enterEvent = _timeline_enter  # type: ignore[method-assign]
    timeline_frame.leaveEvent = _timeline_leave  # type: ignore[method-assign]

    def _set_preview_pix(pix: QPixmap | None):
        if pix is None or pix.isNull():
            return
        state["last_preview_pix"] = pix
        w = max(10, video_widget.width() - 2)
        h = max(10, video_widget.height() - 2)
        video_widget.setPixmap(pix.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _refresh_preview_layout():
        _place_overlays()
        last = state.get("last_preview_pix")
        if isinstance(last, QPixmap) and (not last.isNull()):
            _set_preview_pix(last)

    def _update_play_btn_icon():
        is_playing = player.playbackState() == QMediaPlayer.PlayingState
        icon = dlg.style().standardIcon(QStyle.SP_MediaPause if is_playing else QStyle.SP_MediaPlay)
        play_btn.setIcon(icon)
        play_btn.setIconSize(QSize(26, 26))

    def _show_drag_hint(txt: str):
        drag_hint.setText(txt)
        drag_hint.adjustSize()
        drag_hint.show()
        _place_overlays()
        drag_hint_timer.start()

    # ---- CropState ↔ UI sync callback ----
    def _on_crop_state_changed():
        """Sync overlay, resolution text boxes, and state dict from CropState."""
        overlay.sync_from_crop_state(cs)
        res_w.blockSignals(True)
        res_h.blockSignals(True)
        res_w.setText(str(cs.R_w))
        res_h.setText(str(cs.R_h))
        res_w.blockSignals(False)
        res_h.blockSignals(False)
        state["crop_expr"] = cs.crop_expr()

    cs.set_on_change(_on_crop_state_changed)

    # ---- AR combo handler (Section IV.1) ----
    def _apply_ratio_crop():
        ratio = self._ratio_text_to_float(ratio_combo.currentText().strip())
        if ratio is None:
            cs.onARChange("free")
        else:
            cs.onARChange("fixed", ratio)

    # ---- Resolution blur handlers (Section IV.3) ----
    def _on_res_w_blur():
        # Skip if value unchanged (editingFinished fires on focus-loss too)
        try:
            v = max(1, round(float((res_w.text() or "0").strip() or "0")))
        except Exception:
            return
        if v == cs.R_w:
            return
        cs.onResBlur(res_w.text(), res_h.text(), "w")

    def _on_res_h_blur():
        try:
            v = max(1, round(float((res_h.text() or "0").strip() or "0")))
        except Exception:
            return
        if v == cs.R_h:
            return
        cs.onResBlur(res_w.text(), res_h.text(), "h")

    def _resolution_values() -> tuple[int, int]:
        return max(2, cs.R_w), max(2, cs.R_h)

    # ---- Crop overlay → CropState sync (Section IV.4/5) ----
    def _on_crop_changed(expr: str):
        state["crop_expr"] = expr
        x, y, w, h = overlay._crop
        cs.onCropDragMove(round(x), round(y), round(w), round(h))

    def _on_drag_ended():
        cs.onCropDragEnd()

    overlay.cropChanged.connect(_on_crop_changed)
    overlay.dragEnded.connect(_on_drag_ended)

    def _update_info():
        st, ed = range_slider.values()
        ph = range_slider.playhead()
        seg_len = max(0, ed - st)
        cur_off = max(0, ph - st)
        info_lbl.setText(f"区间时长 {self._sec_to_hhmmss_mmm(seg_len / 1000.0)} | 当前 {self._sec_to_hhmmss_mmm(cur_off / 1000.0)}")
        _place_overlays()

    def _build_crop_filter() -> str | None:
        # Full frame → no crop needed
        if cs.C_x == 0 and cs.C_y == 0 and cs.C_w >= cs.O_w and cs.C_h >= cs.O_h:
            return None
        expr = cs.crop_expr()
        parts = [p.strip() for p in expr.split(":")]
        if len(parts) != 4 or any(not p for p in parts):
            raise ValueError("裁切参数格式应为 w:h:x:y，例如 1920:1080:0:0")
        return f"crop={expr}"

    def _apply():
        try:
            vf = _build_crop_filter()
        except Exception as exc:
            QMessageBox.critical(dlg, "参数错误", str(exc))
            return
        # 先停播释放占用；此处仅生成裁切后的新文件并回填到 UI，
        # 真正覆盖目标文件在主界面“写入选中”时执行。
        _cleanup()
        st, ed = range_slider.values()
        seg_text = f"{self._sec_to_hhmmss_mmm(st / 1000.0)}-{self._sec_to_hhmmss_mmm(ed / 1000.0)}"
        try:
            segments = parse_segments(seg_text)
            cache_dir = Path(__file__).with_name(".nfo_video_cache")
            cache_dir.mkdir(parents=True, exist_ok=True)
            jobs = build_segment_previews([selected_video], segments, cache_dir.resolve())
            if not jobs:
                raise ValueError("未生成导出任务。")
        except Exception as exc:
            QMessageBox.critical(dlg, "参数错误", str(exc))
            return
        # UI 覆盖模式：始终导出到缓存文件，再由“写入选中”执行目标覆盖。
        tmp_out = cache_dir / selected_video.name
        source_path = selected_video.resolve()
        use_intermediate = False
        # 当输入已在缓存目录且同名时，输出到子目录同名文件，避免输入输出同路径和替换锁冲突。
        intermediate_out = (cache_dir / "_cut_tmp") / selected_video.name
        try:
            use_intermediate = (tmp_out.resolve() == source_path)
        except Exception:
            use_intermediate = False
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        if use_intermediate:
            intermediate_out.parent.mkdir(parents=True, exist_ok=True)
            try:
                intermediate_out.unlink(missing_ok=True)
            except Exception:
                pass
            jobs[0].output = intermediate_out
        else:
            jobs[0].output = tmp_out
        ok, failed, logs = run_segment_export(jobs, copy_stream=(vf is None), video_filter=vf)
        for line in logs:
            self._log(f"[视频分段] {line}")
        if failed == 0:
            produced_out = intermediate_out if use_intermediate else tmp_out
            self._log(f"[视频分段] 已生成裁切文件(待写入覆盖): {produced_out}")
            if out_edit is not None:
                current_paths = out_edit.get_paths()
                selected_key = str(selected_video.resolve()).casefold()
                replaced = False
                merged: list[str] = []
                for p in current_paths:
                    if (not replaced) and (str(Path(p).resolve()).casefold() == selected_key):
                        merged.append(str(produced_out))
                        replaced = True
                    else:
                        merged.append(p)
                if not replaced:
                    merged = [str(produced_out)] + [p for p in current_paths if p.strip()]
                out_edit.set_paths(merged)
            dlg.accept()
        else:
            try:
                tmp_out.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                intermediate_out.unlink(missing_ok=True)
            except Exception:
                pass
            QMessageBox.warning(dlg, "部分失败", f"视频处理完成：成功 {ok}，失败 {failed}")

    def _convert_to_apng():
        _cleanup()
        try:
            vf = _build_crop_filter()
        except Exception as exc:
            QMessageBox.critical(dlg, "参数错误", str(exc))
            return
        rw, rh = _resolution_values()
        vf_chain: list[str] = []
        if vf:
            vf_chain.append(vf)
        vf_chain.append(f"scale={rw}:{rh}:flags=lanczos")
        st, ed = range_slider.values()
        cache_dir = Path(__file__).with_name(".nfo_video_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)
        apng_out = cache_dir / f"{selected_video.stem}_clip.png"
        try:
            apng_out.unlink(missing_ok=True)
        except Exception:
            pass
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, st / 1000.0):.3f}",
            "-to",
            f"{max(0.0, ed / 1000.0):.3f}",
            "-i",
            str(selected_video),
            "-an",
            "-vf",
            ",".join(vf_chain),
            "-c:v",
            "apng",
            "-plays",
            "0",
            "-f",
            "apng",
            str(apng_out),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except Exception as exc:
            QMessageBox.critical(dlg, "转换失败", str(exc))
            return
        if proc.returncode != 0 or (not apng_out.exists()):
            err = ((proc.stderr or "").strip() or (proc.stdout or "").strip())[:1200]
            QMessageBox.critical(dlg, "转换失败", err or "ffmpeg 执行失败")
            return
        try:
            out_size = apng_out.stat().st_size
        except Exception:
            out_size = 0
        if out_size > 15 * 1024 * 1024:
            ans = QMessageBox.question(
                dlg,
                "图片过大",
                "转换后图片大于15M，Jellyfin可能无法识别，是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ans != QMessageBox.Yes:
                try:
                    apng_out.unlink(missing_ok=True)
                except Exception:
                    pass
                return
        if out_edit is not None:
            out_edit.set_paths([str(apng_out)])
        dlg.accept()

    def _toggle_play():
        if player.playbackState() == QMediaPlayer.PlayingState:
            state["looping"] = False
            player.pause()
        else:
            state["looping"] = True
            st, _ = range_slider.values()
            if player.position() < st:
                player.setPosition(st)
            player.play()
        _update_play_btn_icon()
        _show_timeline()

    def _on_frame(frame):
        try:
            img = frame.toImage()
        except Exception:
            return
        if not img.isNull():
            _set_preview_pix(QPixmap.fromImage(img))

    def _on_pos(pos: int):
        range_slider.setPlayhead(int(pos))
        _update_info()
        if bool(state.get("looping")):
            st, ed = range_slider.values()
            if pos >= ed:
                player.setPosition(st)

    def _on_range_changed(st: int, ed: int, active: str):
        if active == "start":
            player.setPosition(int(st))
            range_slider.setPlayhead(int(st))
            _show_drag_hint(f"首帧 {self._sec_to_hhmmss_mmm(st / 1000.0)}")
            _set_preview_pix(self._extract_video_frame_pixmap(selected_video, st / 1000.0))
        elif active == "end":
            player.setPosition(int(ed))
            range_slider.setPlayhead(int(ed))
            _show_drag_hint(f"尾帧 {self._sec_to_hhmmss_mmm(ed / 1000.0)}")
            _set_preview_pix(self._extract_video_frame_pixmap(selected_video, ed / 1000.0))
        _update_info()

    def _on_playhead_changed(pos: int):
        st, ed = range_slider.values()
        target = max(st, min(ed, int(pos))) if bool(state.get("looping")) else int(pos)
        if target != pos:
            range_slider.setPlayhead(target)
        player.setPosition(target)
        _show_drag_hint(f"进度 {self._sec_to_hhmmss_mmm(target / 1000.0)}")
        if player.playbackState() != QMediaPlayer.PlayingState:
            _set_preview_pix(self._extract_video_frame_pixmap(selected_video, target / 1000.0))
        _update_info()

    def _cleanup():
        try:
            state["looping"] = False
            player.stop()
        except Exception:
            pass

    dlg.finished.connect(lambda _r: _cleanup())
    video_sink.videoFrameChanged.connect(_on_frame)
    player.positionChanged.connect(_on_pos)
    player.playbackStateChanged.connect(lambda _s: _update_play_btn_icon())
    range_slider.rangeChanged.connect(_on_range_changed)
    range_slider.playheadChanged.connect(_on_playhead_changed)
    # Play button is a visual indicator only — overlay handles all clicks
    play_btn.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    overlay.fallthroughClick.connect(_toggle_play)
    ratio_combo.activated.connect(lambda _i: _apply_ratio_crop())
    ratio_edit = ratio_combo.lineEdit()
    if ratio_edit is not None:
        ratio_edit.editingFinished.connect(_apply_ratio_crop)
    res_w.editingFinished.connect(_on_res_w_blur)
    res_h.editingFinished.connect(_on_res_h_blur)
    apply_btn.clicked.connect(_apply)
    convert_btn.clicked.connect(_convert_to_apng)
    video_widget.entered.connect(lambda: (hover_state.__setitem__("video", True), _show_timeline()))
    video_widget.left.connect(lambda: hover_state.__setitem__("video", False))
    video_widget.moved.connect(_show_timeline)
    video_widget.resized.connect(_refresh_preview_layout)
    overlay.entered.connect(lambda: (hover_state.__setitem__("video", True), _show_timeline()))
    overlay.left.connect(lambda: hover_state.__setitem__("video", False))
    overlay.moved.connect(_show_timeline)

    # Initialize CropState with default 16:9 — callback auto-syncs overlay + res text
    cs.onARChange("fixed", 16 / 9)
    _set_preview_pix(self._extract_video_frame_pixmap(selected_video, 0.0))
    _update_play_btn_icon()
    _update_info()
    _refresh_preview_layout()
    QTimer.singleShot(0, _refresh_preview_layout)
    QTimer.singleShot(80, _refresh_preview_layout)
    _show_timeline()
    dlg.exec()

def bind_video_dialog_methods(cls):
    cls._pick_media_for_image_target = _pick_media_for_image_target
    cls._open_media_url_for_image_target = _open_media_url_for_image_target
    cls._open_media_editor_for_image_target = _open_media_editor_for_image_target
    cls._pick_video_for_image_target = _pick_video_for_image_target
    cls._open_video_url_for_image_target = _open_video_url_for_image_target
    cls._open_video_editor_for_image_target = _open_video_editor_for_image_target
    cls._open_video_editor_for_target = _open_video_editor_for_target
    cls._probe_video_duration_sec = _probe_video_duration_sec
    cls._probe_video_size = _probe_video_size
    cls._extract_video_frame_pixmap = _extract_video_frame_pixmap
    cls._sec_to_hhmmss_mmm = _sec_to_hhmmss_mmm
    cls._ratio_text_to_float = _ratio_text_to_float
    cls._center_crop_expr = _center_crop_expr
    cls._draw_crop_preview_pixmap = _draw_crop_preview_pixmap
    cls._open_video_segment_dialog = _open_video_segment_dialog

