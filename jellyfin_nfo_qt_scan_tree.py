from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import warnings
from time import monotonic
from pathlib import Path
from urllib.request import urlopen

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QParallelAnimationGroup, QPropertyAnimation, QRect, QSize, QTimer, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QImage, QImageReader, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QAbstractItemDelegate, QApplication, QLabel, QLineEdit, QListWidgetItem, QMenu, QMessageBox, QStyledItemDelegate, QTreeWidgetItem, QWidget

from jellyfin_extras_rules import VIDEO_EXTS as _RENAME_VIDEO_EXTS

from jellyfin_nfo_core import NfoItem, collect_nfo_items, load_cached_nfo_items, validate_and_rescan_root


MEDIA_TYPE_CN = {
    "tvshow": "电视剧",
    "season": "季度",
    "episode": "剧集",
    "movie": "电影",
    "movie_or_video_item": "电影",
    "artist": "艺人",
    "album": "专辑",
}
PRIMARY_POSTER_EXTS_ORDER = [".jpg", ".png", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".avif"]
_COVER_COL_W = 150
_COVER_ICON_H_PORTRAIT = 236
_COVER_ICON_H_LANDSCAPE = 84  # 16:9
_COVER_ITEM_W = 162
_COVER_TEXT_H = 24
_LEGACY_COVER_ICON_H_PORTRAIT = 267


class _CoverLoadingSpinner(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(28, 28)
        self.hide()

    def _tick(self):
        self._angle = (self._angle + 24) % 360
        self.update()

    def start(self):
        self.show()
        self.raise_()
        if not self._timer.isActive():
            self._timer.start(30)
        self.update()

    def stop(self):
        self._timer.stop()
        self.hide()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        c = self.rect().center()
        r = min(self.width(), self.height()) // 2 - 3
        p.translate(c)
        p.rotate(self._angle)
        pen = QPen(QColor(44, 105, 255))
        pen.setWidth(3)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(QRect(-r, -r, r * 2, r * 2), 0, 220 * 16)


class _LeftBusyOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setStyleSheet("background:rgba(230,236,245,170);border-radius:8px;")
        self.tip = QLabel("后台加载中...", self)
        self.tip.setStyleSheet("color:#1f2937;font-weight:600;background:transparent;")
        self.hide()

    def place_center(self):
        cx = self.width() // 2
        cy = self.height() // 2
        self.tip.adjustSize()
        self.tip.move(max(0, cx - self.tip.width() // 2), max(0, cy - self.tip.height() // 2))

    def start(self):
        self.show()
        self.raise_()
        self.place_center()

    def stop(self):
        self.hide()


class _CoverTransitionWidget(QWidget):
    def __init__(self, pix: QPixmap, parent=None, clip_rect: QRect | None = None):
        super().__init__(parent)
        self._pix = pix
        self._clip_rect = QRect(clip_rect) if isinstance(clip_rect, QRect) and clip_rect.isValid() else QRect()
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        if self._clip_rect.isValid():
            local_clip = self._clip_rect.translated(-self.x(), -self.y())
            p.setClipRect(local_clip)
        rect = self.rect().adjusted(1, 1, -1, -1)
        if rect.width() <= 0 or rect.height() <= 0:
            return
        if self._pix.isNull():
            return
        ratio = self._pix.width() / max(1, self._pix.height())
        tw = rect.width()
        th = max(1, int(tw / max(0.001, ratio)))
        if th > rect.height():
            th = rect.height()
            tw = max(1, int(th * ratio))
        target = QRect(rect.x() + (rect.width() - tw) // 2, rect.y() + (rect.height() - th) // 2, tw, th)
        p.drawPixmap(target, self._pix)


class _CoverImageMaskWidget(QWidget):
    """仅遮住目标封面区域，避免返回动画期间出现双图。"""
    def __init__(self, color: QColor, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(self._color)
        p.drawRoundedRect(self.rect(), 8, 8)


def _image_cache_root_dir() -> Path:
    root = Path(tempfile.gettempdir()) / "jellyfin_nfo_qt_cache" / "images"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _project_image_cache_root_dir() -> Path:
    root = Path(__file__).with_name(".nfo_image_cache")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _portable_path_tail_signature(path_obj: Path, depth: int = 4) -> str:
    """生成尽量与挂载盘符无关的路径签名，用于跨设备复用缓存键。"""
    try:
        parts = [str(x).strip().casefold() for x in path_obj.parts if str(x).strip()]
    except Exception:
        parts = []
    if parts:
        tail = parts[-max(1, int(depth)) :]
        return "/".join(tail)
    try:
        return str(path_obj.name).strip().casefold()
    except Exception:
        return ""


def _resolve_item_index_by_path(self, path_value: str | None, fallback_index: int | None = None) -> int | None:
    path_text = str(path_value or "").strip()
    if path_text:
        try:
            idx_map = {str(it.path).casefold(): i for i, it in enumerate(getattr(self, "items", []))}
            hit = idx_map.get(path_text.casefold())
            if isinstance(hit, int) and 0 <= hit < len(getattr(self, "items", [])):
                return hit
        except Exception:
            pass
    if isinstance(fallback_index, int) and 0 <= fallback_index < len(getattr(self, "items", [])):
        return fallback_index
    return None


def _cover_preview_target_size(self, pix_w: int, pix_h: int) -> QSize:
    if not hasattr(self, "nfo_cover_preview") or not hasattr(self, "nfo_left_stack"):
        return QSize(220, 220)
    area = self.nfo_left_stack.size()
    preview_rect = self.nfo_cover_preview.contentsRect()
    max_w = min(max(220, int(area.width() * 0.92)), max(1, preview_rect.width()))
    max_h = min(max(220, int(area.height() * 0.80)), max(1, preview_rect.height()))
    if pix_w <= 0 or pix_h <= 0:
        return QSize(max_w, max_h)
    tw = max_w
    th = max(1, int(tw * (pix_h / max(1, pix_w))))
    if th > max_h:
        th = max_h
        tw = max(1, int(th * (pix_w / max(1, pix_h))))
    return QSize(max(1, tw), max(1, th))


def _scale_preview_pixmap(self, pix: QPixmap) -> QPixmap:
    if not isinstance(pix, QPixmap) or pix.isNull() or not hasattr(self, "nfo_cover_preview"):
        return QPixmap()
    sz = self._cover_preview_target_size(pix.width(), pix.height())
    dpr = 1.0
    try:
        dpr = max(1.0, float(self.nfo_cover_preview.devicePixelRatioF()))
    except Exception:
        dpr = 1.0
    scaled = pix.scaled(
        QSize(max(1, int(sz.width() * dpr)), max(1, int(sz.height() * dpr))),
        Qt.KeepAspectRatio,
        Qt.SmoothTransformation,
    )
    if scaled.isNull():
        return QPixmap()
    scaled.setDevicePixelRatio(dpr)
    return scaled


def _get_cover_pixmap_shared(self, path_str: str) -> QPixmap:
    webp_path = self._ensure_cover_original_webp(path_str)
    if not webp_path:
        return QPixmap()
    p = Path(webp_path)
    try:
        st = p.stat()
        sig = f"{str(p).casefold()}|{st.st_mtime_ns}|{st.st_size}"
    except Exception:
        sig = str(p).casefold()
    key = hashlib.sha1(sig.encode("utf-8", errors="ignore")).hexdigest()
    pix_cache: dict[str, QPixmap] = getattr(self, "_cover_full_pixmap_cache", {})
    cached = pix_cache.get(key)
    if cached is not None and (not cached.isNull()):
        return cached
    reader = QImageReader(str(p))
    reader.setAutoTransform(True)
    img = reader.read()
    if img.isNull():
        return QPixmap()
    pix = QPixmap.fromImage(img)
    if pix.isNull():
        return QPixmap()
    pix_cache[key] = pix
    if len(pix_cache) > 96:
        pix_cache.clear()
        pix_cache[key] = pix
    self._cover_full_pixmap_cache = pix_cache
    return pix


def _format_season_name(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return "第 1 季"
    m = re.search(r"(?i)\bseason\s*(\d+)\b", text)
    if m:
        return f"第 {int(m.group(1))} 季"
    m = re.search(r"(?i)\bs(\d{1,2})\b", text)
    if m:
        return f"第 {int(m.group(1))} 季"
    m = re.search(r"第\s*(\d+)\s*季", text)
    if m:
        return f"第 {int(m.group(1))} 季"
    return text


def _friendly_item_title(item: NfoItem) -> str:
    mt = item.media_type
    mt_cn = MEDIA_TYPE_CN.get(mt, mt)
    if mt == "season":
        name = _format_season_name(item.path.parent.name)
    elif mt == "episode":
        stem = _normalize_sxxexx_case(item.path.stem.strip())
        name = stem if stem else item.path.parent.name
    else:
        name = item.path.parent.name.strip() or item.path.stem.strip()
    return f"{mt_cn} | {name}"


def _cover_caption(item: NfoItem) -> str:
    title = _friendly_item_title(item)
    if "|" in title:
        return title.split("|", 1)[1].strip()
    return item.path.parent.name.strip() or item.path.stem.strip()


def _cover_gallery_signature(self) -> tuple[str, ...]:
    root_types = {"tvshow", "movie", "movie_or_video_item", "artist", "album"}
    parent_of: dict[int, int | None] = getattr(self, "_tree_parent_of", {})
    keys: list[str] = []
    for idx, item in enumerate(getattr(self, "items", [])):
        if parent_of.get(idx) is not None:
            continue
        if item.media_type not in root_types:
            continue
        keys.append(str(item.path.parent).casefold())
    return tuple(sorted(keys))


def _cover_icon_height_for_kind(self, kind: str) -> int:
    k = str(kind or "").strip().lower()
    raw = getattr(self, "_cover_icon_h_landscape", _COVER_ICON_H_LANDSCAPE) if k == "landscape" else getattr(self, "_cover_icon_h_portrait", _COVER_ICON_H_PORTRAIT)
    try:
        out = int(raw)
    except Exception:
        out = _COVER_ICON_H_LANDSCAPE if k == "landscape" else _COVER_ICON_H_PORTRAIT
    return max(56, out)


def _cover_text_height(self) -> int:
    # 文本区最小高度（1 行），实际按文本行数动态扩展。
    if not hasattr(self, "nfo_cover_gallery"):
        return _COVER_TEXT_H
    fm = self.nfo_cover_gallery.fontMetrics()
    return max(_COVER_TEXT_H, int(fm.height() + 8))


def _cover_item_text_height(self, text: str) -> int:
    if not hasattr(self, "nfo_cover_gallery"):
        return _COVER_TEXT_H
    fm = self.nfo_cover_gallery.fontMetrics()
    lines = max(1, min(3, str(text or "").count("\n") + 1))
    return max(_COVER_TEXT_H, int(fm.height() * lines + 8))


def _cover_kind_for_size(width: int, height: int) -> str:
    w = max(1, int(width))
    h = max(1, int(height))
    return "landscape" if w >= h else "portrait"


def _format_cover_caption_multiline(self, text: str, max_lines: int = 3) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    if not hasattr(self, "nfo_cover_gallery"):
        return raw
    fm = self.nfo_cover_gallery.fontMetrics()
    max_w = max(40, _COVER_COL_W - 8)
    remain = raw
    lines: list[str] = []
    for idx in range(max_lines):
        if not remain:
            break
        if idx == max_lines - 1:
            lines.append(fm.elidedText(remain, Qt.ElideRight, max_w))
            remain = ""
            break
        lo, hi = 1, len(remain)
        best = 1
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = remain[:mid]
            if fm.horizontalAdvance(candidate) <= max_w:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        lines.append(remain[:best])
        remain = remain[best:]
    return "\n".join(lines)


def _pick_primary_cover_fast(root_dir: Path) -> Path | None:
    # 避免 iterdir 全目录扫描导致 UI 卡顿：优先探测常见命名。
    for stem in ("folder", "poster", "cover", "default", "movie", "show", "jacket"):
        for ext in PRIMARY_POSTER_EXTS_ORDER:
            p = root_dir / f"{stem}{ext}"
            try:
                if p.exists() and p.is_file():
                    return p.resolve()
            except Exception:
                continue
    return None


def _pick_thumb_cover_fast(root_dir: Path) -> Path | None:
    # 详情页优先缩略图（thumb/landscape），没有时再回退主封面。
    for stem in ("thumb", "landscape"):
        for ext in PRIMARY_POSTER_EXTS_ORDER:
            p = root_dir / f"{stem}{ext}"
            try:
                if p.exists() and p.is_file():
                    return p.resolve()
            except Exception:
                continue
    return _pick_primary_cover_fast(root_dir)


def _selected_items(self) -> list[NfoItem]:
    result: list[NfoItem] = []
    seen: set[int] = set()
    for node in self.item_list.selectedItems():
        idx = node.data(0, self.TREE_INDEX_ROLE)
        if not isinstance(idx, int):
            continue
        if idx in seen:
            continue
        if 0 <= idx < len(self.items):
            seen.add(idx)
            result.append(self.items[idx])
    return result


def _selected_items_with_descendants(self, seed_indices: set[int] | None = None) -> list[NfoItem]:
    parent_of: dict[int, int | None] = getattr(self, "_tree_parent_of", {})
    if seed_indices is None:
        roots: set[int] = set()
        for node in self.item_list.selectedItems():
            idx = node.data(0, self.TREE_INDEX_ROLE)
            if isinstance(idx, int) and (0 <= idx < len(self.items)):
                roots.add(idx)
    else:
        roots = {int(i) for i in seed_indices if isinstance(i, int) and (0 <= int(i) < len(self.items))}
    if not roots:
        return []
    children_of: dict[int, list[int]] = {}
    for child_idx, p_idx in parent_of.items():
        if isinstance(child_idx, int) and isinstance(p_idx, int):
            children_of.setdefault(p_idx, []).append(child_idx)
    expanded: set[int] = set()
    stack = list(roots)
    while stack:
        cur = stack.pop()
        if cur in expanded:
            continue
        expanded.add(cur)
        for c in children_of.get(cur, []):
            if c not in expanded:
                stack.append(c)
    return [self.items[i] for i in sorted(expanded) if 0 <= i < len(self.items)]

def refresh_items(self):
    self._scan_request_id += 1
    token = self._scan_request_id
    self._last_loaded_selection_key = None
    self.item_list.clear()
    if hasattr(self, "nfo_cover_gallery"):
        self.nfo_cover_gallery.clear()
    self._cover_gallery_pool = []
    self._cover_gallery_loaded_count = 0
    self._cover_icon_load_jobs = []
    self._cover_row_root_dirs = []
    self._cover_row_cover_paths = []
    self._cover_path_cache = {}
    self._cover_icon_mem_cache = {}
    self._cover_full_pixmap_cache = {}
    self._cover_icon_loaded_rows = set()
    self._cover_icon_queued_rows = set()
    self._cover_visible_load_scheduled = False
    self._cover_scrolling = False
    self._cover_scroll_idle_token = int(getattr(self, "_cover_scroll_idle_token", 0)) + 1
    self._cover_append_scheduled = False
    self._cover_icon_load_token = int(getattr(self, "_cover_icon_load_token", 0)) + 1
    if hasattr(self, "nfo_cover_meta_list"):
        self.nfo_cover_meta_list.clear()
    if hasattr(self, "nfo_cover_title"):
        self.nfo_cover_title.setText("")
    if hasattr(self, "nfo_cover_preview"):
        self.nfo_cover_preview.setPixmap(QPixmap())
        self.nfo_cover_preview.setText("暂无封面")
    self.scan_stats_label.setText("统计：扫描中...")
    self._scan_progress_rows.clear()
    if self.paths:
        for p in sorted(self.paths, key=lambda x: str(x).lower()):
            text = str(p.resolve())
            row = QTreeWidgetItem([f"等待: {text}"])
            self.item_list.addTopLevelItem(row)
            self._scan_progress_rows[text.casefold()] = row
    else:
        self.item_list.addTopLevelItem(QTreeWidgetItem(["扫描中..."]))
    worker = self._create_scan_worker(token, tuple(self.paths))
    worker.signals.progress.connect(self._on_scan_progress)
    worker.signals.finished.connect(self._on_scan_finished)
    self._scan_workers[token] = worker
    self._scan_pool.start(worker)
    self._log("开始后台扫描 NFO...")

def _on_scan_progress(self, token: int, root_text: str, status: str, detail: str, scanned: int, total: int):
    if token != self._scan_request_id:
        return
    key = root_text.casefold()
    row = self._scan_progress_rows.get(key)
    if row is None:
        row = QTreeWidgetItem([f"等待: {root_text}"])
        self.item_list.addTopLevelItem(row)
        self._scan_progress_rows[key] = row
    if status == "error":
        row.setText(0, f"失败: {root_text} -> {detail}")
        self._log(f"扫描失败: {root_text} -> {detail}")
        return
    if status == "start":
        row.setText(0, f"扫描中: {root_text}")
    elif status == "scan":
        row.setText(0, f"扫描中: {root_text} | 当前子目录: {detail} | 进度: {scanned}/{total}")
    elif status == "done":
        row.setText(0, f"完成: {root_text}")

def _build_item_tree(self):
    self.item_list.clear()
    if not self.items:
        return
    key_to_idx = {str(it.path).casefold(): i for i, it in enumerate(self.items)}

    def _closest_ancestor_index(item: NfoItem, basenames: tuple[str, ...]) -> int | None:
        cur = item.path.parent
        while True:
            for name in basenames:
                key = str(cur / name).casefold()
                idx = key_to_idx.get(key)
                if isinstance(idx, int):
                    return idx
            if cur.parent == cur:
                return None
            cur = cur.parent

    parent_of: dict[int, int | None] = {}
    for idx, item in enumerate(self.items):
        mt = item.media_type
        if mt == "season":
            parent_of[idx] = _closest_ancestor_index(item, ("tvshow.nfo",))
        elif mt == "episode":
            parent_of[idx] = _closest_ancestor_index(item, ("season.nfo", "tvshow.nfo"))
        elif mt == "album":
            parent_of[idx] = _closest_ancestor_index(item, ("artist.nfo",))
        elif mt == "movie_or_video_item":
            parent_of[idx] = _closest_ancestor_index(item, ("movie.nfo",))
        else:
            parent_of[idx] = None
    self._tree_parent_of = parent_of

    created: dict[int, QTreeWidgetItem] = {}

    def _build_node(i: int) -> QTreeWidgetItem:
        existing = created.get(i)
        if existing is not None:
            return existing
        item = self.items[i]
        node = QTreeWidgetItem([_friendly_item_title(item)])
        node.setData(0, self.TREE_INDEX_ROLE, i)
        created[i] = node
        p_idx = parent_of.get(i)
        if isinstance(p_idx, int) and p_idx != i:
            parent_node = _build_node(p_idx)
            parent_node.addChild(node)
        else:
            self.item_list.addTopLevelItem(node)
        return node

    for i in range(len(self.items)):
        _build_node(i)
    self.item_list.collapseAll()
    self._apply_left_title_filter()


def _apply_left_title_filter(self):
    if not hasattr(self, "item_list"):
        return
    q = str(getattr(self, "_left_title_filter_text", "") or "").strip().casefold()
    for i in range(self.item_list.topLevelItemCount()):
        node = self.item_list.topLevelItem(i)
        if node is None:
            continue
        idx = node.data(0, self.TREE_INDEX_ROLE)
        if not isinstance(idx, int) or not (0 <= idx < len(getattr(self, "items", []))):
            node.setHidden(False if not q else True)
            continue
        title = _cover_caption(self.items[idx]).casefold()
        node.setHidden(bool(q) and (q not in title))


def _on_left_title_filter_changed(self, text: str):
    self._left_title_filter_text = str(text or "")
    self._apply_left_title_filter()
    if hasattr(self, "nfo_left_stack") and self.nfo_left_stack.currentIndex() == 1:
        self._refresh_cover_gallery()

def _on_scan_finished(self, token: int, items_obj: object, err: str):
    self._scan_workers.pop(token, None)
    if token != self._scan_request_id:
        return
    self._lazy_loaded_dirs.clear()
    if hasattr(self, "_lazy_loaded_check_ts"):
        self._lazy_loaded_check_ts.clear()
    if err:
        self.items = []
        self.item_list.clear()
        self.scan_stats_label.setText("统计：扫描失败。")
        self._log(f"扫描失败: {err}")
        return
    items = items_obj if isinstance(items_obj, list) else []
    self.items = [x for x in items if isinstance(x, NfoItem)]
    self._build_item_tree()
    if hasattr(self, "_refresh_cover_gallery") and hasattr(self, "nfo_left_stack") and self.nfo_left_stack.currentIndex() != 0:
        self._refresh_cover_gallery()
    self._update_scan_stats_label()
    # 扫描完成后回放上次会话：延迟加载状态 + 上次选中项。
    if hasattr(self, "_restore_scan_tree_state"):
        self._restore_scan_tree_state()
    self._log(f"扫描完成，共发现 NFO: {len(self.items)}")

def _visit_tree_nodes(self, fn):
    def _walk(node: QTreeWidgetItem):
        fn(node)
        for i in range(node.childCount()):
            _walk(node.child(i))

    for i in range(self.item_list.topLevelItemCount()):
        _walk(self.item_list.topLevelItem(i))

def _reselect_by_paths(self, selected_paths: set[str]):
    first_hit: QTreeWidgetItem | None = None
    normalized_paths = {str(p).casefold() for p in (selected_paths or set())}
    self.item_list.blockSignals(True)
    try:
        self._visit_tree_nodes(lambda n: n.setSelected(False))

        def _mark(node: QTreeWidgetItem):
            nonlocal first_hit
            idx = node.data(0, self.TREE_INDEX_ROLE)
            if not isinstance(idx, int):
                return
            if 0 <= idx < len(self.items):
                p = str(self.items[idx].path).casefold()
                if p in normalized_paths:
                    node.setSelected(True)
                    if first_hit is None:
                        first_hit = node

        self._visit_tree_nodes(_mark)
        if first_hit is not None:
            self.item_list.setCurrentItem(first_hit)
    finally:
        self.item_list.blockSignals(False)
    if first_hit is not None:
        try:
            self.item_list.scrollToItem(first_hit, self.item_list.PositionAtCenter)
        except Exception:
            try:
                self.item_list.scrollToItem(first_hit)
            except Exception:
                pass

def _ensure_secondary_items_loaded(self, selected: list[NfoItem]) -> bool:
    """延迟加载子级条目。

    **优先从 SQLite 扫描缓存秒级加载**（不做磁盘签名校验），
    然后在后台线程异步校验签名；发现变更时无感刷新树节点。

    已加载过的目录再次被选中时，若距上次校验已超过冷却时间（60 秒），
    也会在后台静默校验一次磁盘签名，检测新增/删除并无感刷新。
    """
    _RECHECK_COOLDOWN = 60  # 秒
    if not selected:
        return False

    check_ts: dict[str, float] = getattr(self, "_lazy_loaded_check_ts", {})
    now = monotonic()

    roots_to_load: set[Path] = set()       # 首次加载
    roots_to_recheck: set[Path] = set()    # 已加载但需要重新校验
    for item in selected:
        if item.media_type not in {"tvshow", "season", "artist", "movie"}:
            continue
        root_dir = item.path.parent
        key = str(root_dir).casefold()
        if key not in self._lazy_loaded_dirs:
            roots_to_load.add(root_dir)
            self._lazy_loaded_dirs.add(key)
            check_ts[key] = now
        else:
            # 已加载过 → 检查冷却时间
            last = check_ts.get(key, 0.0)
            if (now - last) >= _RECHECK_COOLDOWN:
                roots_to_recheck.add(root_dir)
                check_ts[key] = now
    self._lazy_loaded_check_ts = check_ts

    if not roots_to_load and not roots_to_recheck:
        return False

    # ---- 阶段 1：首次加载 → 从 SQLite 缓存秒级加载（主线程，无磁盘签名 I/O）----
    added = False
    if roots_to_load:
        existing = {str(x.path).casefold() for x in self.items}
        old_cover_sig = _cover_gallery_signature(self)
        for one_root in sorted(roots_to_load, key=lambda p: str(p).lower()):
            cached = load_cached_nfo_items(one_root)
            if cached:
                for item in cached:
                    k = str(item.path).casefold()
                    if k in existing:
                        continue
                    existing.add(k)
                    self.items.append(item)
                    added = True
        if added:
            selected_paths = {str(x.path).casefold() for x in selected}
            old_scroll = self.item_list.verticalScrollBar().value()
            self._build_item_tree()
            new_cover_sig = _cover_gallery_signature(self)
            if (
                hasattr(self, "_refresh_cover_gallery")
                and hasattr(self, "nfo_left_stack")
                and self.nfo_left_stack.currentIndex() != 0
                and old_cover_sig != new_cover_sig
            ):
                self._refresh_cover_gallery()
            self._reselect_by_paths(selected_paths)
            self.item_list.verticalScrollBar().setValue(old_scroll)

    # ---- 阶段 2：后台异步校验签名，变更时无感刷新 ----
    # 包含首次加载目录的校验 + 已加载目录的冷却重检
    all_roots_to_validate = list(roots_to_load | roots_to_recheck)
    phase1_hit = bool(added)

    def _bg_validate():
        """在后台线程跑磁盘签名校验 + 可能的重新扫描。"""
        changed: dict[str, list[NfoItem]] = {}
        for one_root in all_roots_to_validate:
            try:
                new_items = validate_and_rescan_root(one_root)
                if new_items is not None:
                    changed[str(one_root).casefold()] = new_items
                elif str(one_root).casefold() in {str(r).casefold() for r in roots_to_load} and not phase1_hit:
                    # 首次加载 + 缓存签名匹配但阶段 1 无数据 → 冷启动兜底全扫
                    fallback = collect_nfo_items({one_root}, quick_scan=True, max_depth=1)
                    if fallback:
                        changed[str(one_root).casefold()] = fallback
            except Exception:
                continue
        return changed

    def _bg_done(result, _err):
        changed = result if isinstance(result, dict) else {}
        if not changed:
            if hasattr(self, "_schedule_save_ui_session"):
                self._schedule_save_ui_session()
            return
        # 有变更或首次冷启动数据，无感刷新
        cur_existing = {str(x.path).casefold() for x in self.items}
        refresh_added = False
        # 同时检测已删除的条目
        removed_keys: set[str] = set()
        for _root_key, new_items in changed.items():
            new_keys = {str(it.path).casefold() for it in new_items}
            # 找出属于该 root 但已不在新扫描结果中的旧条目
            for old_item in list(self.items):
                old_key = str(old_item.path).casefold()
                old_root = str(old_item.path.parent).casefold()
                if old_root == _root_key and old_key not in new_keys:
                    removed_keys.add(old_key)
            for item in new_items:
                k = str(item.path).casefold()
                if k in cur_existing:
                    continue
                cur_existing.add(k)
                self.items.append(item)
                refresh_added = True
        if removed_keys:
            self.items = [it for it in self.items if str(it.path).casefold() not in removed_keys]
            refresh_added = True
        if refresh_added:
            sel_paths = {str(x.path).casefold() for x in self._selected_items()}
            scroll_val = self.item_list.verticalScrollBar().value()
            self._build_item_tree()
            new_cov = _cover_gallery_signature(self)
            refresh_cover_sig = _cover_gallery_signature(self)
            if (
                hasattr(self, "_refresh_cover_gallery")
                and hasattr(self, "nfo_left_stack")
                and self.nfo_left_stack.currentIndex() != 0
            ):
                self._refresh_cover_gallery()
            self._reselect_by_paths(sel_paths)
            self.item_list.verticalScrollBar().setValue(scroll_val)
        if hasattr(self, "_schedule_save_ui_session"):
            self._schedule_save_ui_session()

    if all_roots_to_validate:
        if hasattr(self, "_run_async"):
            self._run_async(_bg_validate, _bg_done)
        else:
            _bg_done(_bg_validate(), "")

    if hasattr(self, "_schedule_save_ui_session"):
        self._schedule_save_ui_session()
    return added

def _update_scan_stats_label(self):
    counts = {
        "tvshow": 0,
        "movie": 0,
        "album": 0,
    }
    for item in self.items:
        mt = item.media_type
        if mt in counts:
            counts[mt] += 1
    self.scan_stats_label.setText(
        f"统计：共 {counts['tvshow']} 部电视剧，{counts['movie']} 部电影，{counts['album']} 张专辑。"
    )

def _on_item_list_context_menu(self, pos):
    node = self.item_list.itemAt(pos)
    if node is None:
        return
    idx = node.data(0, self.TREE_INDEX_ROLE)
    if not isinstance(idx, int) or not (0 <= idx < len(self.items)):
        return
    target_item = self.items[idx]
    selected_indices: set[int] = set()
    for one in self.item_list.selectedItems():
        one_idx = one.data(0, self.TREE_INDEX_ROLE)
        if isinstance(one_idx, int) and (0 <= one_idx < len(self.items)):
            selected_indices.add(one_idx)
    is_multi_select = len(selected_indices) > 1
    seed_indices = selected_indices if idx in selected_indices else {idx}
    selected_targets = self._selected_items_with_descendants(seed_indices)
    menu = QMenu(self)
    if is_multi_select:
        season_offset_action = menu.addAction("季度偏移...")
        chosen = menu.exec(self.item_list.viewport().mapToGlobal(pos))
        if chosen is season_offset_action:
            if hasattr(self, "_open_season_episode_offset_dialog"):
                self._open_season_episode_offset_dialog(selected_targets)
        return
    rename_action = None
    if _get_rename_initial_text(target_item) is not None:
        rename_action = menu.addAction("重命名")
    open_path_action = menu.addAction("打开文件路径")
    download_video_action = menu.addAction("下载视频到此 NFO 目录")
    season_offset_action = None
    if any(str(getattr(it, "media_type", "") or "") in {"tvshow", "season", "episode"} for it in selected_targets):
        season_offset_action = menu.addAction("季度偏移...")
    season_rename_action = None
    if str(getattr(target_item, "media_type", "") or "") in {"tvshow", "season"}:
        season_rename_action = menu.addAction("Season 批量重命名...")
    chosen = menu.exec(self.item_list.viewport().mapToGlobal(pos))
    if rename_action is not None and chosen is rename_action:
        self._start_tree_item_rename(self.item_list, node)
        return
    if (season_offset_action is not None) and (chosen is season_offset_action):
        if hasattr(self, "_open_season_episode_offset_dialog"):
            self._open_season_episode_offset_dialog(selected_targets)
        return
    if (season_rename_action is not None) and (chosen is season_rename_action):
        if hasattr(self, "_open_season_renamer_for_nfo"):
            self._open_season_renamer_for_nfo(target_item.path)
        return
    if chosen is download_video_action:
        if hasattr(self, "_download_video_to_nfo_dir_via_webview"):
            self._download_video_to_nfo_dir_via_webview(target_item.path)
        return
    if chosen is not open_path_action:
        return
    target_dir = target_item.path.parent
    if not target_dir.exists():
        QMessageBox.warning(self, "提示", f"路径不存在：{target_dir}")
        return
    try:
        ok = QDesktopServices.openUrl(QUrl.fromLocalFile(str(target_dir)))
        if not ok:
            raise RuntimeError("系统未能打开该路径")
    except Exception as exc:
        QMessageBox.critical(self, "打开失败", f"无法打开路径：{exc}")


def _on_cover_meta_list_context_menu(self, pos):
    if not hasattr(self, "nfo_cover_meta_list"):
        return
    row = self.nfo_cover_meta_list.itemAt(pos)
    if row is None:
        return
    row_path = row.data(0, int(Qt.UserRole) + 1)
    idx = _resolve_item_index_by_path(self, row_path, row.data(0, Qt.UserRole))
    if not isinstance(idx, int):
        return
    row.setData(0, Qt.UserRole, idx)
    target_item = self.items[idx]
    selected_indices: set[int] = set()
    for one in self.nfo_cover_meta_list.selectedItems():
        one_path = one.data(0, int(Qt.UserRole) + 1)
        one_idx = _resolve_item_index_by_path(self, one_path, one.data(0, Qt.UserRole))
        if isinstance(one_idx, int):
            selected_indices.add(one_idx)
    is_multi_select = len(selected_indices) > 1
    seed_indices = selected_indices if idx in selected_indices else {idx}
    selected_targets = self._selected_items_with_descendants(seed_indices)
    menu = QMenu(self)
    if is_multi_select:
        season_offset_action = menu.addAction("季度偏移...")
        chosen = menu.exec(self.nfo_cover_meta_list.viewport().mapToGlobal(pos))
        if chosen is season_offset_action:
            if hasattr(self, "_open_season_episode_offset_dialog"):
                self._open_season_episode_offset_dialog(selected_targets)
        return
    rename_action = None
    if _get_rename_initial_text(target_item) is not None:
        rename_action = menu.addAction("重命名")
    open_path_action = menu.addAction("打开文件路径")
    download_video_action = menu.addAction("下载视频到此 NFO 目录")
    season_offset_action = None
    if any(str(getattr(it, "media_type", "") or "") in {"tvshow", "season", "episode"} for it in selected_targets):
        season_offset_action = menu.addAction("季度偏移...")
    season_rename_action = None
    if str(getattr(target_item, "media_type", "") or "") in {"tvshow", "season"}:
        season_rename_action = menu.addAction("Season 批量重命名...")
    chosen = menu.exec(self.nfo_cover_meta_list.viewport().mapToGlobal(pos))
    if rename_action is not None and chosen is rename_action:
        self._start_tree_item_rename(self.nfo_cover_meta_list, row)
        return
    if (season_offset_action is not None) and (chosen is season_offset_action):
        if hasattr(self, "_open_season_episode_offset_dialog"):
            self._open_season_episode_offset_dialog(selected_targets)
        return
    if (season_rename_action is not None) and (chosen is season_rename_action):
        if hasattr(self, "_open_season_renamer_for_nfo"):
            self._open_season_renamer_for_nfo(target_item.path)
        return
    if chosen is download_video_action:
        if hasattr(self, "_download_video_to_nfo_dir_via_webview"):
            self._download_video_to_nfo_dir_via_webview(target_item.path)
        return
    if chosen is not open_path_action:
        return
    target_dir = target_item.path.parent
    if not target_dir.exists():
        QMessageBox.warning(self, "提示", f"路径不存在：{target_dir}")
        return
    try:
        ok = QDesktopServices.openUrl(QUrl.fromLocalFile(str(target_dir)))
        if not ok:
            raise RuntimeError("系统未能打开该路径")
    except Exception as exc:
        QMessageBox.critical(self, "打开失败", f"无法打开路径：{exc}")

# ---------------------------------------------------------------------------
#  Inline rename (delayed second click + right-click menu)
# ---------------------------------------------------------------------------

_RENAME_SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"}
_RENAME_RELATED_EXTS = _RENAME_VIDEO_EXTS | {".nfo"} | _RENAME_SUBTITLE_EXTS


_ITEM_IS_EDITABLE = 2  # Qt.ItemFlag.ItemIsEditable == 0x2


class _RenameClickOutsideFilter(QObject):
    """Watches for mouse clicks outside the rename editor and forces commit."""

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Type.MouseButtonPress:
            return False
        window = self.parent()
        editor = getattr(window, "_rename_editor_widget", None)
        if editor is None or not editor.isVisible():
            self._detach()
            return False
        try:
            gp = event.globalPosition().toPoint()
        except AttributeError:
            gp = event.globalPos()
        er = QRect(editor.mapToGlobal(QPoint(0, 0)), editor.size())
        if er.contains(gp):
            return False
        editor.clearFocus()
        self._detach()
        return False

    def _detach(self):
        app = QApplication.instance()
        if app:
            app.removeEventFilter(self)


def _add_item_flag_editable(node: QTreeWidgetItem):
    tree = node.treeWidget()
    if tree:
        tree.blockSignals(True)
    try:
        node.setFlags(node.flags() | Qt.ItemIsEditable)
    except Exception:
        pass
    if tree:
        tree.blockSignals(False)


def _remove_item_flag_editable(node: QTreeWidgetItem):
    tree = node.treeWidget()
    if tree:
        tree.blockSignals(True)
    try:
        node.setFlags(node.flags() & ~Qt.ItemIsEditable)
    except Exception:
        pass
    if tree:
        tree.blockSignals(False)


class _RenameDelegate(QStyledItemDelegate):
    """Provides a custom initial edit value while keeping the display text intact."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._initial: str | None = None

    def set_initial(self, text: str | None):
        self._initial = text

    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        if isinstance(editor, QLineEdit):
            editor.setMinimumHeight(max(28, option.rect.height()))
            editor.setStyleSheet(
                "QLineEdit{padding:2px 4px;border:2px solid #5b8def;"
                "border-radius:4px;background:#fff;font-size:13px;}"
            )
        return editor

    def updateEditorGeometry(self, editor, option, index):
        r = option.rect
        h = max(r.height(), 28)
        editor.setGeometry(r.x(), r.y() - (h - r.height()) // 2, r.width(), h)

    def eventFilter(self, obj, event):
        if isinstance(obj, QLineEdit) and event.type() == QEvent.Type.FocusOut:
            reason = getattr(event, "reason", lambda: None)()
            if reason != Qt.FocusReason.PopupFocusReason:
                self.commitData.emit(obj)
                self.closeEditor.emit(obj, QAbstractItemDelegate.EndEditHint.NoHint)
                return True
        return super().eventFilter(obj, event)

    def setEditorData(self, editor, index):
        if self._initial is not None and isinstance(editor, QLineEdit):
            editor.setText(self._initial)
            editor.selectAll()
            self._initial = None
        else:
            super().setEditorData(editor, index)


def _get_rename_initial_text(item: NfoItem) -> str | None:
    mt = str(getattr(item, "media_type", "") or "")
    if mt == "tvshow":
        return item.path.parent.name
    if mt == "season":
        m = re.match(r"^season\s*(\d+)$", item.path.parent.name.strip(), re.IGNORECASE)
        return str(int(m.group(1))) if m else item.path.parent.name
    if mt in {"episode", "movie_or_video_item"}:
        return _normalize_sxxexx_case(item.path.stem)
    return None


def _init_rename_state(self):
    self._rename_node: QTreeWidgetItem | None = None
    self._rename_tree = None
    self._rename_idx: int = -1
    self._rename_original_display: str = ""
    self._rename_committed = False
    self._suppress_rename_change = False
    self._rename_sel_path: str = ""
    self._rename_sel_ms: float = 0.0
    self._rename_click_node: QTreeWidgetItem | None = None
    self._rename_click_tree = None
    self._rename_click_timer = QTimer(self)
    self._rename_click_timer.setSingleShot(True)
    self._rename_click_timer.timeout.connect(self._consume_rename_click)
    self._rename_editor_widget: QLineEdit | None = None
    self._rename_click_outside_filter = _RenameClickOutsideFilter(self)


def _start_tree_item_rename(self, tree, node):
    idx_role = self.TREE_INDEX_ROLE if tree is self.item_list else Qt.UserRole
    idx = node.data(0, idx_role)
    if not isinstance(idx, int) or not (0 <= idx < len(self.items)):
        return
    item = self.items[idx]
    initial = _get_rename_initial_text(item)
    if initial is None:
        return
    delegate = tree.itemDelegate()
    if not isinstance(delegate, _RenameDelegate):
        delegate = _RenameDelegate(tree)
        tree.setItemDelegate(delegate)
    delegate.set_initial(initial)
    self._rename_node = node
    self._rename_tree = tree
    self._rename_idx = idx
    self._rename_original_display = node.text(0)
    self._rename_committed = False
    _add_item_flag_editable(node)
    tree.editItem(node, 0)
    self._rename_editor_widget = None
    for child in tree.viewport().findChildren(QLineEdit):
        if child.isVisible():
            self._rename_editor_widget = child
            break
    app = QApplication.instance()
    if app:
        app.installEventFilter(self._rename_click_outside_filter)

    def _on_close(ed, hint):
        self._rename_close_handler = None
        self._rename_editor_widget = None
        self._rename_click_outside_filter._detach()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            try:
                tree.itemDelegate().closeEditor.disconnect(_on_close)
            except (TypeError, RuntimeError):
                pass
        if not self._rename_committed:
            _remove_item_flag_editable(node)
            self._rename_node = None

    prev = getattr(self, "_rename_close_handler", None)
    if prev is not None:
        self._rename_close_handler = None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            try:
                tree.itemDelegate().closeEditor.disconnect(prev)
            except (TypeError, RuntimeError):
                pass
    self._rename_close_handler = _on_close
    tree.itemDelegate().closeEditor.connect(_on_close)


def _on_tree_item_changed_for_rename(self, node, column):
    if getattr(self, "_suppress_rename_change", False):
        return
    if getattr(self, "_rename_node", None) is not node:
        return
    self._suppress_rename_change = True
    try:
        self._rename_committed = True
        self._rename_node = None
        new_text = node.text(0).strip()
        idx = self._rename_idx
        original_display = self._rename_original_display
        _remove_item_flag_editable(node)
        if not (0 <= idx < len(self.items)):
            node.setText(0, original_display)
            return
        item = self.items[idx]
        initial = _get_rename_initial_text(item)
        if not new_text or new_text == initial:
            node.setText(0, original_display)
            return
        mt = str(getattr(item, "media_type", "") or "")
        ok = False
        if mt == "tvshow":
            ok = self._exec_rename_folder(item, new_text)
        elif mt == "season":
            ok = self._exec_rename_season(item, new_text)
        elif mt in {"episode", "movie_or_video_item"}:
            ok = self._exec_rename_episode(item, new_text)
        if ok:
            node.setText(0, _friendly_item_title(self.items[idx]))
        else:
            node.setText(0, original_display)
    finally:
        self._suppress_rename_change = False


def _exec_rename_folder(self, item: NfoItem, new_name: str) -> bool:
    old_dir = item.path.parent.resolve()
    safe = re.sub(r'[\\/:*?"<>|]+', "", new_name).strip()
    if not safe:
        QMessageBox.warning(self, "重命名失败", "名称不合法。")
        return False
    new_dir = old_dir.parent / safe
    if new_dir.resolve() == old_dir:
        return False
    if new_dir.exists():
        QMessageBox.warning(self, "重命名失败", f"目标文件夹已存在：{new_dir.name}")
        return False
    try:
        old_dir.rename(new_dir)
    except Exception as exc:
        QMessageBox.critical(self, "重命名失败", str(exc))
        return False
    old_prefix = str(old_dir)
    new_prefix = str(new_dir.resolve())
    for i, it in enumerate(self.items):
        p = str(it.path.resolve())
        if p == old_prefix or p.startswith(old_prefix + "\\") or p.startswith(old_prefix + "/"):
            new_path = Path(new_prefix + p[len(old_prefix):])
            self.items[i] = NfoItem(path=new_path, media_type=it.media_type, display=it.display)
    self._log(f"[重命名] 文件夹: {old_dir.name} -> {safe}")
    return True


def _exec_rename_season(self, item: NfoItem, new_num_text: str) -> bool:
    try:
        new_num = int(new_num_text.strip())
    except ValueError:
        QMessageBox.warning(self, "重命名失败", "请输入有效的季号数字。")
        return False
    if new_num < 0:
        QMessageBox.warning(self, "重命名失败", "季号不能为负数。")
        return False
    old_dir = item.path.parent.resolve()
    new_dir = old_dir.parent / f"Season {new_num}"
    if new_dir.resolve() == old_dir:
        return False
    if new_dir.exists():
        QMessageBox.warning(self, "重命名失败", f"目标文件夹已存在：{new_dir.name}")
        return False
    try:
        old_dir.rename(new_dir)
    except Exception as exc:
        QMessageBox.critical(self, "重命名失败", str(exc))
        return False
    old_prefix = str(old_dir)
    new_prefix = str(new_dir.resolve())
    for i, it in enumerate(self.items):
        p = str(it.path.resolve())
        if p == old_prefix or p.startswith(old_prefix + "\\") or p.startswith(old_prefix + "/"):
            new_path = Path(new_prefix + p[len(old_prefix):])
            self.items[i] = NfoItem(path=new_path, media_type=it.media_type, display=it.display)
    self._log(f"[重命名] 季度文件夹: {old_dir.name} -> Season {new_num}")
    return True


def _normalize_sxxexx_case(text: str) -> str:
    return re.sub(
        r'(?<!\w)[sS](\d+)[eE](\d+)(?!\w)',
        lambda m: f"S{m.group(1)}E{m.group(2)}",
        text,
    )

def _exec_rename_episode(self, item: NfoItem, new_stem: str) -> bool:
    safe = re.sub(r'[\\/:*?"<>|]+', "", new_stem).strip()
    safe = _normalize_sxxexx_case(safe)
    if not safe:
        QMessageBox.warning(self, "重命名失败", "名称不合法。")
        return False
    old_stem = item.path.stem
    parent_dir = item.path.parent
    to_rename: list[Path] = []
    try:
        for f in parent_dir.iterdir():
            if f.is_file() and f.stem.casefold() == old_stem.casefold() and f.suffix.lower() in _RENAME_RELATED_EXTS:
                to_rename.append(f)
    except Exception:
        pass
    if not to_rename:
        QMessageBox.warning(self, "重命名失败", "未找到可重命名的关联文件。")
        return False
    for f in to_rename:
        target = f.with_name(f"{safe}{f.suffix}")
        if target.exists() and target.resolve() != f.resolve():
            QMessageBox.warning(self, "重命名失败", f"目标文件已存在：{target.name}")
            return False
    try:
        for f in to_rename:
            target = f.with_name(f"{safe}{f.suffix}")
            if target.resolve() != f.resolve():
                f.rename(target)
    except Exception as exc:
        QMessageBox.critical(self, "重命名失败", str(exc))
        return False
    idx_map = {str(one.path).casefold(): i for i, one in enumerate(self.items)}
    for f in to_rename:
        if f.suffix.lower() == ".nfo":
            i = idx_map.get(str(f).casefold())
            if isinstance(i, int) and 0 <= i < len(self.items):
                old_it = self.items[i]
                self.items[i] = NfoItem(
                    path=f.with_name(f"{safe}{f.suffix}"),
                    media_type=old_it.media_type,
                    display=old_it.display,
                )
    self._log(f"[重命名] 剧集文件: {old_stem} -> {safe} ({len(to_rename)} 个文件)")
    return True


def _on_tree_clicked_for_rename(self, tree, node, column):
    if getattr(self, "_rename_node", None) is not None:
        return
    self._rename_click_timer.stop()
    if len(tree.selectedItems()) != 1:
        return
    idx_role = self.TREE_INDEX_ROLE if tree is self.item_list else Qt.UserRole
    idx = node.data(0, idx_role)
    if not isinstance(idx, int) or not (0 <= idx < len(self.items)):
        return
    item_path = str(self.items[idx].path)
    if item_path != getattr(self, "_rename_sel_path", ""):
        return
    now = monotonic() * 1000.0
    sel_ms = float(getattr(self, "_rename_sel_ms", 0.0))
    if (now - sel_ms) > 500:
        self._rename_click_node = node
        self._rename_click_tree = tree
        self._rename_click_timer.start(50)


def _consume_rename_click(self):
    node = getattr(self, "_rename_click_node", None)
    tree = getattr(self, "_rename_click_tree", None)
    if node is None or tree is None:
        return
    if len(tree.selectedItems()) != 1:
        return
    sel = tree.selectedItems()[0]
    if sel is not node:
        return
    self._start_tree_item_rename(tree, node)


def _on_item_selection_changed(self):
    if bool(getattr(self, "_suspend_selection_change_prompt", False)):
        return
    self._auto_load_timer.stop()
    self._rename_click_timer.stop()
    selected_now = self._selected_items()
    if len(selected_now) == 1:
        cur_path = str(selected_now[0].path)
        if cur_path != getattr(self, "_rename_sel_path", ""):
            self._rename_sel_path = cur_path
            self._rename_sel_ms = monotonic() * 1000.0
    else:
        self._rename_sel_path = ""
        self._rename_sel_ms = 0.0
    if len(selected_now) != 1:
        if hasattr(self, "_schedule_save_ui_session"):
            self._schedule_save_ui_session()
        return
    selected_paths = {str(item.path) for item in selected_now}
    if hasattr(self, "_confirm_save_before_selection_reload"):
        if not self._confirm_save_before_selection_reload(selected_paths):
            return
    self._refresh_media_target_visibility(self._selected_items())
    self._auto_load_timer.start(120)
    if hasattr(self, "_schedule_save_ui_session"):
        self._schedule_save_ui_session()


def _set_cover_preview(self, image_path: Path | None):
    if not hasattr(self, "nfo_cover_preview"):
        return
    if image_path is None or (not image_path.exists()):
        self.nfo_cover_preview.setPixmap(QPixmap())
        self.nfo_cover_preview.setText("暂无封面")
        return
    pix = QPixmap(str(image_path))
    if pix.isNull():
        self.nfo_cover_preview.setPixmap(QPixmap())
        self.nfo_cover_preview.setText("暂无封面")
        return
    self.nfo_cover_preview.setText("")
    sharp = self._scale_preview_pixmap(pix)
    self.nfo_cover_preview.setPixmap(sharp if not sharp.isNull() else pix)


def _ensure_cover_loading_spinner(self):
    spinner = getattr(self, "_cover_loading_spinner", None)
    if isinstance(spinner, _CoverLoadingSpinner):
        return spinner
    spinner = _CoverLoadingSpinner(self.nfo_cover_preview)
    self._cover_loading_spinner = spinner
    return spinner


def _place_cover_loading_spinner(self):
    if not hasattr(self, "nfo_cover_preview"):
        return
    spinner = self._ensure_cover_loading_spinner()
    x = max(0, (self.nfo_cover_preview.width() - spinner.width()) // 2)
    y = max(0, (self.nfo_cover_preview.height() - spinner.height()) // 2)
    spinner.move(x, y)


def _set_cover_preview_async(self, image_path: Path | None):
    if not hasattr(self, "nfo_cover_preview"):
        return
    token = int(getattr(self, "_cover_preview_load_token", 0)) + 1
    self._cover_preview_load_token = token
    self.nfo_cover_preview.setPixmap(QPixmap())
    self.nfo_cover_preview.setText("加载中...")
    source_raw = str(image_path) if image_path is not None else ""

    def _job():
        if image_path is None or (not image_path.exists()):
            return (None, "")
        cached_webp = self._ensure_cover_original_webp(source_raw)
        if not cached_webp:
            return (None, "")
        reader = QImageReader(cached_webp)
        reader.setAutoTransform(True)
        img = reader.read()
        if img.isNull():
            return (None, "")
        return (img, "")

    def _done(result, err):
        if token != int(getattr(self, "_cover_preview_load_token", -1)):
            return
        if err:
            self.nfo_cover_preview.setPixmap(QPixmap())
            self.nfo_cover_preview.setText("暂无封面")
            return
        img: QImage | None = result[0] if isinstance(result, tuple) else None
        if img is None or img.isNull():
            self.nfo_cover_preview.setPixmap(QPixmap())
            self.nfo_cover_preview.setText("暂无封面")
            return
        pix = QPixmap.fromImage(img)
        if pix.isNull():
            self.nfo_cover_preview.setPixmap(QPixmap())
            self.nfo_cover_preview.setText("暂无封面")
            return
        self.nfo_cover_preview.setText("")
        sharp = self._scale_preview_pixmap(pix)
        self.nfo_cover_preview.setPixmap(sharp if not sharp.isNull() else pix)

    self._run_async(_job, _done)


def _show_cover_preview_loading(self):
    if not hasattr(self, "nfo_cover_preview"):
        return
    self.nfo_cover_preview.setPixmap(QPixmap())
    self.nfo_cover_preview.setText("")
    self._cover_preview_load_token = int(getattr(self, "_cover_preview_load_token", 0)) + 1


def _apply_detail_preview_height_for_kind(self, kind: str):
    if not hasattr(self, "nfo_cover_preview"):
        return
    k = str(kind or "").strip().lower()
    if k == "landscape":
        # 横屏详情预览框压低，避免出现过高留白。
        self.nfo_cover_preview.setMinimumHeight(120)
        self.nfo_cover_preview.setMaximumHeight(320)
    else:
        # 竖屏保持原有视觉占比。
        self.nfo_cover_preview.setMinimumHeight(180)
        self.nfo_cover_preview.setMaximumHeight(16777215)


def _ensure_left_busy_overlay(self):
    host = getattr(self, "nfo_left_group", None)
    if host is None:
        return None
    overlay = getattr(self, "_left_busy_overlay", None)
    if isinstance(overlay, _LeftBusyOverlay):
        return overlay
    overlay = _LeftBusyOverlay(host)
    self._left_busy_overlay = overlay
    return overlay


def _show_left_busy_overlay(self):
    # 已禁用左侧“后台加载中”遮罩提示，避免与列表加载提示重复。
    return


def _hide_left_busy_overlay(self):
    self._left_busy_active = False
    overlay = getattr(self, "_left_busy_overlay", None)
    if isinstance(overlay, _LeftBusyOverlay):
        overlay.stop()


def _show_left_busy_overlay_if_active(self, token: int):
    # 已禁用左侧“后台加载中”遮罩提示，保留调用点但不做显示。
    return


def _start_cover_enter_transition(self, list_item: QListWidgetItem):
    if not hasattr(self, "nfo_cover_gallery") or not hasattr(self, "nfo_cover_preview"):
        return
    overlay_parent = getattr(self, "nfo_left_group", self)
    old_anim = getattr(self, "_cover_enter_anim", None)
    if old_anim is not None:
        try:
            old_anim.stop()
        except Exception:
            pass
    old_box = getattr(self, "_cover_enter_box", None)
    if isinstance(old_box, QWidget):
        try:
            old_box.hide()
            old_box.deleteLater()
        except RuntimeError:
            # 旧对象可能已被 Qt 提前销毁，忽略即可。
            pass
        self._cover_enter_box = None
    rect = self.nfo_cover_gallery.visualItemRect(list_item)
    if not rect.isValid():
        return
    payload = list_item.data(Qt.UserRole) if list_item is not None else None
    icon_sz = self.nfo_cover_gallery.iconSize()
    if isinstance(payload, dict):
        thumb_size = payload.get("thumb_size")
        if isinstance(thumb_size, (list, tuple)) and len(thumb_size) == 2:
            try:
                icon_sz = QSize(max(1, int(thumb_size[0])), max(1, int(thumb_size[1])))
            except Exception:
                pass
    src_w = max(40, min(icon_sz.width(), rect.width() - 8))
    src_h = max(40, min(icon_sz.height(), rect.height() - 8))
    src_x = rect.x() + max(0, (rect.width() - src_w) // 2)
    src_y = rect.y() + 4
    src_top_left = self.nfo_cover_gallery.viewport().mapTo(overlay_parent, rect.topLeft())
    src_rect = QRect(src_top_left.x() + (src_x - rect.x()), src_top_left.y() + (src_y - rect.y()), src_w, src_h)
    preview_rect = self.nfo_cover_preview.contentsRect()
    dst_top_left = self.nfo_cover_preview.mapTo(overlay_parent, preview_rect.topLeft())
    dst_rect = QRect(dst_top_left, preview_rect.size())
    if not dst_rect.isValid():
        return
    pix = QPixmap()
    if isinstance(payload, dict):
        source_path = str(getattr(self, "_detail_cover_locked_path", "") or "").strip()
        if not source_path:
            source_path = str(payload.get("cover_path") or "").strip()
        if not source_path:
            root_dir = str(payload.get("root_dir") or "").strip()
            if root_dir:
                source_path = self._resolve_cover_path_for_root(root_dir)
        if source_path:
            # 动画源也只使用 cover_original_raw 原图缓存。
            pix = self._get_cover_pixmap_shared(source_path)

    def _fit_rect(outer: QRect, w: int, h: int) -> QRect:
        if w <= 0 or h <= 0:
            return outer
        ow, oh = max(1, outer.width()), max(1, outer.height())
        tw = ow
        th = max(1, int(tw * (h / max(1, w))))
        if th > oh:
            th = oh
            tw = max(1, int(th * (w / max(1, h))))
        return QRect(outer.x() + (ow - tw) // 2, outer.y() + (oh - th) // 2, tw, th)

    src_anim_rect = src_rect
    dst_anim_rect = dst_rect
    if not pix.isNull():
        src_anim_rect = _fit_rect(src_rect, pix.width(), pix.height())
        dst_anim_rect = _fit_rect(dst_rect, pix.width(), pix.height())
    # 记录进入详情时的原始卡片封面区域，返回时可直接反向动画，避免先切页带来的延迟。
    self._detail_source_anim_rect = QRect(src_anim_rect)

    self._detail_transition_pix = pix
    # 动画前只用 FastTransformation（运动中看不出差别），避免阻塞主线程。
    if not pix.isNull() and hasattr(self, "nfo_cover_preview"):
        preview_sz = self._cover_preview_target_size(pix.width(), pix.height())
        self._detail_preview_prescaled = pix.scaled(preview_sz, Qt.KeepAspectRatio, Qt.FastTransformation)
    else:
        self._detail_preview_prescaled = QPixmap()
    anim_pix = pix
    if not pix.isNull():
        max_w = max(src_anim_rect.width(), dst_anim_rect.width())
        max_h = max(src_anim_rect.height(), dst_anim_rect.height())
        if max_w > 0 and max_h > 0:
            anim_pix = pix.scaled(QSize(max_w, max_h), Qt.KeepAspectRatio, Qt.FastTransformation)
    clip_rect = QRect()
    if hasattr(self, "nfo_cover_gallery"):
        vp = self.nfo_cover_gallery.viewport() if hasattr(self.nfo_cover_gallery, "viewport") else None
        if vp is not None:
            clip_top_left = vp.mapTo(overlay_parent, QPoint(0, 0))
            clip_rect = QRect(clip_top_left, vp.size())
        else:
            clip_top_left = self.nfo_cover_gallery.mapTo(overlay_parent, QPoint(0, 0))
            clip_rect = QRect(clip_top_left, self.nfo_cover_gallery.size())
    overlay = _CoverTransitionWidget(anim_pix, overlay_parent, clip_rect)
    overlay.setGeometry(src_anim_rect)
    overlay.show()
    overlay.raise_()
    anim_group = QParallelAnimationGroup(overlay)
    anim_pos = QPropertyAnimation(overlay, b"pos", anim_group)
    anim_pos.setDuration(240)
    anim_pos.setEasingCurve(QEasingCurve.InOutCubic)
    anim_pos.setStartValue(src_anim_rect.topLeft())
    anim_pos.setEndValue(dst_anim_rect.topLeft())
    anim_size = QPropertyAnimation(overlay, b"size", anim_group)
    anim_size.setDuration(240)
    anim_size.setEasingCurve(QEasingCurve.InOutCubic)
    anim_size.setStartValue(src_anim_rect.size())
    anim_size.setEndValue(dst_anim_rect.size())
    anim_group.addAnimation(anim_pos)
    anim_group.addAnimation(anim_size)
    anim_group.finished.connect(overlay.deleteLater)
    anim_group.finished.connect(self._on_cover_enter_transition_finished)
    anim_group.start()
    self._cover_enter_box = overlay
    self._cover_enter_anim = anim_group


def _on_cover_enter_transition_finished(self):
    if not hasattr(self, "nfo_cover_preview"):
        return
    # 严格模式：动画结束后不再重新加载，只显示同一份原图的缩放结果。
    final_pix = QPixmap()
    base = getattr(self, "_detail_transition_pix", QPixmap())
    if isinstance(base, QPixmap) and (not base.isNull()):
        final_pix = self._scale_preview_pixmap(base)
    if isinstance(final_pix, QPixmap) and (not final_pix.isNull()):
        self.nfo_cover_preview.setText("")
        self.nfo_cover_preview.setPixmap(final_pix)
    else:
        # 极端失败时才回退异步加载原图缓存路径。
        locked = str(getattr(self, "_detail_cover_locked_path", "") or "").strip()
        if locked:
            self._set_cover_preview_async(Path(locked))
    busy_token = int(getattr(self, "_show_busy_after_anim_token", 0))
    if busy_token > 0:
        # 确保放大动画结束后再显示加载转圈，避免收尾帧卡顿。
        QTimer.singleShot(40, lambda t=busy_token: self._show_left_busy_overlay_if_active(t))
        self._show_busy_after_anim_token = 0


def _replace_preview_with_smooth(self):
    """异步将 FastTransformation 预览替换为 SmoothTransformation 高质量版本。"""
    if not hasattr(self, "nfo_cover_preview"):
        return
    locked = str(getattr(self, "_detail_cover_locked_path", "") or "").strip()
    if locked:
        self._set_cover_preview_async(Path(locked))
        return
    pix = getattr(self, "_detail_transition_pix", QPixmap())
    if not isinstance(pix, QPixmap) or pix.isNull():
        return
    smooth = self._scale_preview_pixmap(pix)
    if not smooth.isNull():
        self.nfo_cover_preview.setPixmap(smooth)


def _flush_deferred_media_resource_refresh(self, token: int):
    if token != int(getattr(self, "_defer_media_resource_token", -1)):
        return
    self._defer_media_resource_refresh = False
    if not hasattr(self, "nfo_left_stack") or self.nfo_left_stack.currentIndex() != 2:
        return
    try:
        self.load_selected_metadata(silent_if_empty=True, force_reload=True, include_media_resources=True)
    except Exception:
        return


def _trigger_detail_data_load_after_anim(self, token: int, payload: dict):
    if token != int(getattr(self, "_detail_anim_token", -1)):
        return
    if not isinstance(payload, dict):
        return
    if not hasattr(self, "nfo_left_stack") or self.nfo_left_stack.currentIndex() != 2:
        return
    self._detail_pending_payload = None
    self._populate_cover_detail_after_transition(payload)


def _set_left_view_toggle_state(self, mode: str):
    if not hasattr(self, "nfo_view_list_btn") or not hasattr(self, "nfo_view_cover_btn"):
        return
    self.nfo_view_list_btn.blockSignals(True)
    self.nfo_view_cover_btn.blockSignals(True)
    self.nfo_view_list_btn.setChecked(mode == "list")
    self.nfo_view_cover_btn.setChecked(mode != "list")
    self.nfo_view_list_btn.blockSignals(False)
    self.nfo_view_cover_btn.blockSignals(False)


def _switch_left_nfo_view(self, mode: str):
    if not hasattr(self, "nfo_left_stack"):
        return
    if mode == "list":
        self._defer_media_resource_refresh = False
        self.nfo_left_stack.setCurrentIndex(0)
        self._set_left_view_toggle_state("list")
        return
    self.nfo_left_stack.setCurrentIndex(1)
    self._set_left_view_toggle_state("cover")
    QTimer.singleShot(0, self._refresh_cover_gallery)


def _sync_cover_selection_from_tree(self):
    """列表->图表切换时，按当前树选中项定位到对应一级封面卡片。"""
    if not hasattr(self, "nfo_cover_gallery") or not hasattr(self, "item_list"):
        return
    focus_idx = None
    cur_node = self.item_list.currentItem()
    if cur_node is not None:
        one = cur_node.data(0, self.TREE_INDEX_ROLE)
        if isinstance(one, int):
            focus_idx = one
    if not isinstance(focus_idx, int):
        selected = self._selected_items() if hasattr(self, "_selected_items") else []
        if selected:
            try:
                idx_map = {str(it.path).casefold(): i for i, it in enumerate(getattr(self, "items", []))}
                focus_idx = idx_map.get(str(selected[0].path).casefold())
            except Exception:
                focus_idx = None
    if not isinstance(focus_idx, int):
        return
    parent_of: dict[int, int | None] = getattr(self, "_tree_parent_of", {})
    root_idx = int(focus_idx)
    walked: set[int] = set()
    while isinstance(parent_of.get(root_idx), int) and root_idx not in walked:
        walked.add(root_idx)
        root_idx = int(parent_of.get(root_idx))
    root_path_cf = str(self.items[root_idx].path).casefold() if 0 <= root_idx < len(self.items) else ""
    target_item = None
    target_row = -1
    for row in range(self.nfo_cover_gallery.count()):
        it = self.nfo_cover_gallery.item(row)
        if it is None:
            continue
        payload = it.data(Qt.UserRole)
        if not isinstance(payload, dict):
            continue
        payload_path_cf = str(payload.get("root_path") or "").strip().casefold()
        payload_idx = payload.get("root_index")
        if (payload_path_cf and payload_path_cf == root_path_cf) or (isinstance(payload_idx, int) and payload_idx == root_idx):
            target_item = it
            target_row = row
            break
    if target_item is None:
        return
    self.nfo_cover_gallery.blockSignals(True)
    try:
        self.nfo_cover_gallery.clearSelection()
        self.nfo_cover_gallery.setCurrentItem(target_item)
        target_item.setSelected(True)
    finally:
        self.nfo_cover_gallery.blockSignals(False)
    self._detail_source_cover_row = target_row
    try:
        self.nfo_cover_gallery.scrollToItem(target_item, self.nfo_cover_gallery.PositionAtCenter)
    except Exception:
        self.nfo_cover_gallery.scrollToItem(target_item)


def _back_to_cover_gallery(self):
    if not hasattr(self, "nfo_left_stack"):
        return
    if self.nfo_left_stack.currentIndex() != 2:
        return
    self._start_cover_back_transition()


def _start_cover_back_transition(self):
    if not hasattr(self, "nfo_cover_gallery") or not hasattr(self, "nfo_cover_preview") or not hasattr(self, "nfo_left_stack"):
        return
    # 若上次动画被中断，先恢复被隐藏的目标卡片图像。
    hidden_row = int(getattr(self, "_cover_back_hidden_row", -1))
    hidden_icon = getattr(self, "_cover_back_hidden_icon", None)
    hidden_deco = getattr(self, "_cover_back_hidden_deco", None)
    if hidden_row >= 0:
        prev_item = self.nfo_cover_gallery.item(hidden_row)
        if prev_item is not None:
            if isinstance(hidden_icon, QIcon):
                prev_item.setIcon(hidden_icon)
            prev_item.setData(Qt.DecorationRole, hidden_deco)
    self._cover_back_hidden_row = -1
    self._cover_back_hidden_icon = QIcon()
    self._cover_back_hidden_deco = None
    overlay_parent = getattr(self, "nfo_left_group", self)
    # 先取详情页预览区域几何与当前图像，作为反向动画起点。
    preview_rect = self.nfo_cover_preview.contentsRect()
    src_top_left = self.nfo_cover_preview.mapTo(overlay_parent, preview_rect.topLeft())
    src_rect = QRect(src_top_left, preview_rect.size())
    if not src_rect.isValid():
        src_rect = QRect(0, 0, 1, 1)
    pix = self.nfo_cover_preview.pixmap() if hasattr(self.nfo_cover_preview, "pixmap") else QPixmap()
    if (not isinstance(pix, QPixmap)) or pix.isNull():
        base = getattr(self, "_detail_transition_pix", QPixmap())
        pix = base if isinstance(base, QPixmap) else QPixmap()
    # 先切回图表模式页，再播放缩小动画，保证背景是图表模式。
    self._defer_media_resource_refresh = False
    self.nfo_left_stack.setCurrentIndex(1)
    self._set_left_view_toggle_state("cover")

    def _calc_item_cover_rect(item: QListWidgetItem | None) -> QRect:
        if item is None:
            return QRect()
        rect = self.nfo_cover_gallery.visualItemRect(item)
        if not rect.isValid():
            return QRect()
        payload = item.data(Qt.UserRole) if item is not None else None
        icon_sz = self.nfo_cover_gallery.iconSize()
        if isinstance(payload, dict):
            thumb_size = payload.get("thumb_size")
            if isinstance(thumb_size, (list, tuple)) and len(thumb_size) == 2:
                try:
                    icon_sz = QSize(max(1, int(thumb_size[0])), max(1, int(thumb_size[1])))
                except Exception:
                    pass
        icon_w = int(icon_sz.width()) if isinstance(icon_sz, QSize) else 0
        icon_h = int(icon_sz.height()) if isinstance(icon_sz, QSize) else 0
        if icon_w <= 0:
            icon_w = max(40, rect.width() - 8)
        if icon_h <= 0:
            icon_h = max(40, rect.height() - _cover_text_height(self) - 8)
        dst_w = max(40, min(icon_w, rect.width() - 8))
        dst_h = max(40, min(icon_h, rect.height() - 8))
        src_x = rect.x() + max(0, (rect.width() - dst_w) // 2)
        src_y = rect.y() + 4
        dst_top_left = self.nfo_cover_gallery.viewport().mapTo(overlay_parent, rect.topLeft())
        return QRect(dst_top_left.x() + (src_x - rect.x()), dst_top_left.y() + (src_y - rect.y()), dst_w, dst_h)

    # 优先使用“当前实时布局”计算目标位置，避免结束前与底图位置有偏差。
    try:
        self.nfo_cover_gallery.doItemsLayout()
    except Exception:
        pass
    dst_rect = QRect()
    target_item: QListWidgetItem | None = None
    source_row = int(getattr(self, "_detail_source_cover_row", -1))
    if 0 <= source_row < self.nfo_cover_gallery.count():
        target_item = self.nfo_cover_gallery.item(source_row)
        dst_rect = _calc_item_cover_rect(target_item)
    if not dst_rect.isValid() and self.nfo_cover_gallery.count() > 0:
        target_item = self.nfo_cover_gallery.currentItem() or self.nfo_cover_gallery.item(0)
        dst_rect = _calc_item_cover_rect(target_item)
    if not dst_rect.isValid():
        # 最后兜底：使用进入详情时缓存矩形。
        cached_dst = getattr(self, "_detail_source_anim_rect", QRect())
        if isinstance(cached_dst, QRect) and cached_dst.isValid():
            dst_rect = QRect(cached_dst)
    if not dst_rect.isValid():
        self._finish_back_to_cover_gallery()
        return

    def _fit_rect(outer: QRect, w: int, h: int) -> QRect:
        if w <= 0 or h <= 0:
            return outer
        ow, oh = max(1, outer.width()), max(1, outer.height())
        tw = ow
        th = max(1, int(tw * (h / max(1, w))))
        if th > oh:
            th = oh
            tw = max(1, int(th * (w / max(1, h))))
        return QRect(outer.x() + (ow - tw) // 2, outer.y() + (oh - th) // 2, tw, th)

    # 返回动画终点必须对齐“列表封面占位外框”，否则结束会出现位置抽动。
    src_anim_rect = QRect(src_rect)
    dst_anim_rect = QRect(dst_rect)
    self._cover_back_target_rect = QRect(dst_rect)
    old_anim = getattr(self, "_cover_enter_anim", None)
    if old_anim is not None:
        try:
            old_anim.stop()
        except Exception:
            pass
    old_box = getattr(self, "_cover_enter_box", None)
    if isinstance(old_box, QWidget):
        try:
            old_box.hide()
            old_box.deleteLater()
        except Exception:
            pass
        self._cover_enter_box = None

    anim_pix = pix
    if isinstance(pix, QPixmap) and (not pix.isNull()):
        max_w = max(1, max(src_anim_rect.width(), dst_anim_rect.width()))
        max_h = max(1, max(src_anim_rect.height(), dst_anim_rect.height()))
        scaled = pix.scaled(QSize(max_w, max_h), Qt.KeepAspectRatio, Qt.FastTransformation)
        if not scaled.isNull():
            # 过渡层统一用 1x，避免高 DPI 下 drawPixmap 出现二次缩放错位。
            try:
                scaled.setDevicePixelRatio(1.0)
            except Exception:
                pass
            anim_pix = scaled

    # 动画期间仅隐藏目标卡片图像（保留标题），避免双图且不出现白色遮罩。
    if target_item is not None:
        row = self.nfo_cover_gallery.row(target_item)
        if row >= 0:
            self._cover_back_hidden_row = row
            icon = target_item.icon()
            self._cover_back_hidden_icon = QIcon(icon) if isinstance(icon, QIcon) else QIcon()
            self._cover_back_hidden_deco = target_item.data(Qt.DecorationRole)
            target_item.setIcon(QIcon())
            target_item.setData(Qt.DecorationRole, None)
    clip_rect = QRect()
    if hasattr(self, "nfo_cover_gallery"):
        vp = self.nfo_cover_gallery.viewport() if hasattr(self.nfo_cover_gallery, "viewport") else None
        if vp is not None:
            clip_top_left = vp.mapTo(overlay_parent, QPoint(0, 0))
            clip_rect = QRect(clip_top_left, vp.size())
        else:
            clip_top_left = self.nfo_cover_gallery.mapTo(overlay_parent, QPoint(0, 0))
            clip_rect = QRect(clip_top_left, self.nfo_cover_gallery.size())
    overlay = _CoverTransitionWidget(anim_pix, overlay_parent, clip_rect)
    overlay.setGeometry(src_anim_rect)
    overlay.show()
    overlay.raise_()
    anim_group = QParallelAnimationGroup(overlay)
    anim_pos = QPropertyAnimation(overlay, b"pos", anim_group)
    anim_pos.setDuration(220)
    anim_pos.setEasingCurve(QEasingCurve.InOutCubic)
    anim_pos.setStartValue(src_anim_rect.topLeft())
    anim_pos.setEndValue(dst_anim_rect.topLeft())
    anim_size = QPropertyAnimation(overlay, b"size", anim_group)
    anim_size.setDuration(220)
    anim_size.setEasingCurve(QEasingCurve.InOutCubic)
    anim_size.setStartValue(src_anim_rect.size())
    anim_size.setEndValue(dst_anim_rect.size())
    anim_group.addAnimation(anim_pos)
    anim_group.addAnimation(anim_size)
    anim_group.finished.connect(overlay.deleteLater)
    anim_group.finished.connect(self._finish_back_to_cover_gallery)
    anim_group.start()
    self._cover_enter_box = overlay
    self._cover_enter_anim = anim_group


def _finish_back_to_cover_gallery(self):
    hidden_row = int(getattr(self, "_cover_back_hidden_row", -1))
    hidden_icon = getattr(self, "_cover_back_hidden_icon", None)
    hidden_deco = getattr(self, "_cover_back_hidden_deco", None)
    if hidden_row >= 0 and hasattr(self, "nfo_cover_gallery"):
        item = self.nfo_cover_gallery.item(hidden_row)
        if item is not None:
            if isinstance(hidden_icon, QIcon):
                item.setIcon(hidden_icon)
            item.setData(Qt.DecorationRole, hidden_deco)
    self._cover_back_hidden_row = -1
    self._cover_back_hidden_icon = QIcon()
    self._cover_back_hidden_deco = None
    # 返回结束后，把目标项封面立即替换为本次详情同源图缩放结果，避免“动画结束瞬间变糊”。
    target_rect = getattr(self, "_cover_back_target_rect", QRect())
    source_row = int(getattr(self, "_detail_source_cover_row", -1))
    base_pix = getattr(self, "_detail_transition_pix", QPixmap())
    if isinstance(base_pix, QPixmap) and (not base_pix.isNull()) and isinstance(target_rect, QRect) and target_rect.isValid() and source_row >= 0 and hasattr(self, "nfo_cover_gallery"):
        item = self.nfo_cover_gallery.item(source_row)
        if item is not None:
            try:
                dpr = max(2.0, float(self.nfo_cover_gallery.devicePixelRatioF()))
            except Exception:
                dpr = 2.0
            dst_px_w = max(1, int(target_rect.width() * dpr))
            dst_px_h = max(1, int(target_rect.height() * dpr))
            inner = base_pix.scaled(QSize(dst_px_w, dst_px_h), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            if not inner.isNull():
                fitted = QPixmap(dst_px_w, dst_px_h)
                fitted.fill(Qt.transparent)
                p = QPainter(fitted)
                p.drawPixmap(max(0, (dst_px_w - inner.width()) // 2), max(0, (dst_px_h - inner.height()) // 2), inner)
                p.end()
                try:
                    fitted.setDevicePixelRatio(dpr)
                except Exception:
                    pass
                item.setIcon(QIcon(fitted))
                item.setData(Qt.DecorationRole, fitted)
    self._cover_back_target_rect = QRect()
    # 返回画廊后再清理详情树，避免切换瞬间抖动/卡顿。
    if hasattr(self, "nfo_cover_meta_list"):
        self.nfo_cover_meta_list.clear()
    if hasattr(self, "nfo_cover_preview"):
        self.nfo_cover_preview.setPixmap(QPixmap())
        self.nfo_cover_preview.setText("")


def _refresh_cover_gallery(self):
    if not hasattr(self, "nfo_cover_gallery"):
        return
    self.nfo_cover_gallery.clear()
    if not getattr(self, "items", None):
        return
    parent_of: dict[int, int | None] = getattr(self, "_tree_parent_of", {})
    children_of: dict[int, list[int]] = {}
    for child_idx, p_idx in parent_of.items():
        if isinstance(p_idx, int):
            children_of.setdefault(p_idx, []).append(child_idx)
    root_types = {"tvshow", "movie", "movie_or_video_item", "artist", "album"}
    q = str(getattr(self, "_left_title_filter_text", "") or "").strip().casefold()
    pool: list[dict] = []
    for idx, item in enumerate(self.items):
        if parent_of.get(idx) is not None:
            continue
        if item.media_type not in root_types:
            continue
        title = _cover_caption(item)
        if q and (q not in title.casefold()):
            continue
        stack = [idx]
        sub_indices: list[int] = []
        while stack:
            cur = stack.pop()
            sub_indices.append(cur)
            stack.extend(children_of.get(cur, []))
        pool.append(
            {
                "root_index": idx,
                "root_path": str(item.path),
                "meta_indices": sub_indices,
                "meta_paths": [str(self.items[x].path) for x in sub_indices if 0 <= x < len(self.items)],
                "title": title,
                "root_dir": str(item.path.parent),
            }
        )
    self._cover_gallery_pool = pool
    self._cover_gallery_loaded_count = len(pool)
    self._cover_icon_load_jobs = []
    self._cover_icon_inflight_rows = set()
    self._cover_icon_async_max = 4
    self._cover_bg_cursor = 0
    self._cover_row_root_dirs = []
    self._cover_row_cover_paths = []
    self._cover_path_cache = {}
    self._cover_icon_loaded_rows = set()
    self._cover_icon_queued_rows = set()
    self._cover_visible_load_scheduled = False
    self._cover_reflow_scheduled = False
    self._cover_scrolling = False
    self._cover_scroll_idle_token = int(getattr(self, "_cover_scroll_idle_token", 0)) + 1
    self._cover_append_scheduled = False
    self._cover_icon_load_token = int(getattr(self, "_cover_icon_load_token", 0)) + 1
    self._cover_orientation_seen_landscape = 0
    self._cover_orientation_seen_portrait = 0

    portrait_h_raw = int(getattr(self, "_cover_icon_h_portrait", _COVER_ICON_H_PORTRAIT) or _COVER_ICON_H_PORTRAIT)
    # 兼容历史会话：旧默认值 267 会覆盖新参数，导致“改了没效果”。
    if portrait_h_raw == _LEGACY_COVER_ICON_H_PORTRAIT:
        portrait_h_raw = _COVER_ICON_H_PORTRAIT
        self._cover_icon_h_portrait = portrait_h_raw
        if hasattr(self, "_schedule_save_ui_session"):
            self._schedule_save_ui_session()
    self._cover_icon_h_portrait = max(56, int(portrait_h_raw))
    self._cover_icon_h_landscape = _cover_icon_height_for_kind(self, "landscape")
    # 混排模式：竖图与横图分开计算，不使用全局单一高度。
    self._cover_gallery_hint_mode = "auto"
    self.nfo_cover_gallery.setIconSize(QSize())
    self.nfo_cover_gallery.setGridSize(QSize())
    self.nfo_cover_gallery.setUniformItemSizes(False)
    hint_w = _COVER_ITEM_W
    hint_h = self._cover_icon_h_portrait + _cover_text_height(self)
    kind_cache: dict[str, str] = getattr(self, "_cover_kind_cache", {})
    for info in pool:
        title = str(info.get("title", "")).strip()
        root_dir_str = str(info.get("root_dir", "")).strip()
        root_key = root_dir_str.casefold()
        known_kind = str(kind_cache.get(root_key, "landscape") or "landscape").strip().lower()
        if known_kind not in {"portrait", "landscape"}:
            known_kind = "landscape"
        gallery_item = QListWidgetItem(title)
        gallery_item.setTextAlignment(Qt.AlignHCenter | Qt.AlignBottom)
        gallery_item.setText(_format_cover_caption_multiline(self, title, max_lines=3))
        gallery_item.setSizeHint(QSize(hint_w, hint_h))
        gallery_item.setData(
            Qt.UserRole,
            {
                "root_index": info.get("root_index"),
                "root_path": info.get("root_path", ""),
                "meta_indices": info.get("meta_indices", []),
                "meta_paths": info.get("meta_paths", []),
                "root_dir": root_dir_str,
                "cover_path": "",
                "cover_kind": known_kind,
                "raw_title": title,
            },
        )
        self.nfo_cover_gallery.addItem(gallery_item)
        self._cover_row_root_dirs.append(root_dir_str)
        self._cover_row_cover_paths.append("")
    self._reflow_cover_gallery_rows()
    self._sync_cover_selection_from_tree()
    QTimer.singleShot(0, self._load_visible_cover_icons)


def _schedule_cover_gallery_reflow(self, delay_ms: int = 0):
    if not hasattr(self, "nfo_cover_gallery"):
        return
    if bool(getattr(self, "_cover_reflow_scheduled", False)):
        return
    self._cover_reflow_scheduled = True

    def _run():
        self._cover_reflow_scheduled = False
        if not hasattr(self, "nfo_cover_gallery"):
            return
        self._reflow_cover_gallery_rows()
        try:
            self.nfo_cover_gallery.doItemsLayout()
        except Exception:
            pass
        self.nfo_cover_gallery.viewport().update()

    QTimer.singleShot(max(0, int(delay_ms)), _run)


def _reflow_cover_gallery_rows(self):
    if not hasattr(self, "nfo_cover_gallery"):
        return
    gallery = self.nfo_cover_gallery
    count = gallery.count()
    if count <= 0:
        return
    spacing = max(0, int(gallery.spacing()))
    vw = max(1, gallery.viewport().width())
    cell_w = max(1, _COVER_ITEM_W + spacing)
    cols = max(1, vw // cell_w)
    for row_start in range(0, count, cols):
        row_end = min(count, row_start + cols)
        only_landscape = True
        for idx in range(row_start, row_end):
            item = gallery.item(idx)
            if item is None:
                only_landscape = False
                break
            payload = item.data(Qt.UserRole)
            kind = payload.get("cover_kind") if isinstance(payload, dict) else "unknown"
            if kind != "landscape":
                only_landscape = False
                break
        row_icon_h = _cover_icon_height_for_kind(self, "landscape") if only_landscape else _cover_icon_height_for_kind(self, "portrait")
        row_text_h = _COVER_TEXT_H
        for idx in range(row_start, row_end):
            item = gallery.item(idx)
            if item is None:
                continue
            row_text_h = max(row_text_h, _cover_item_text_height(self, item.text()))
        row_item_h = row_icon_h + row_text_h
        for idx in range(row_start, row_end):
            item = gallery.item(idx)
            if item is None:
                continue
            item.setSizeHint(QSize(_COVER_ITEM_W, row_item_h))


def _apply_cover_gallery_hint_mode(self, mode: str):
    if not hasattr(self, "nfo_cover_gallery"):
        return
    m = str(mode or "").strip().lower()
    if m not in {"portrait", "landscape"}:
        return
    self._cover_gallery_hint_mode = m
    icon_h = _cover_icon_height_for_kind(self, m)
    placeholder_h = icon_h + 46
    self.nfo_cover_gallery.setIconSize(QSize(_COVER_COL_W, icon_h))
    self.nfo_cover_gallery.setGridSize(QSize(_COVER_ITEM_W, placeholder_h))
    self.nfo_cover_gallery.setUniformItemSizes(True)
    # 模式切换后强制重排当前画廊，避免旧尺寸残留导致“行高不一致”。
    self._cover_icon_load_jobs = []
    self._cover_icon_queued_rows = set()
    self._cover_icon_loaded_rows = set()
    self._cover_icon_load_token = int(getattr(self, "_cover_icon_load_token", 0)) + 1
    for row in range(self.nfo_cover_gallery.count()):
        item = self.nfo_cover_gallery.item(row)
        if item is None:
            continue
        item.setSizeHint(QSize(_COVER_ITEM_W, placeholder_h))
        # 清空旧图标，按新模式统一尺寸重新解码。
        item.setData(Qt.DecorationRole, None)
        payload = item.data(Qt.UserRole)
        if isinstance(payload, dict) and "thumb_size" in payload:
            payload.pop("thumb_size", None)
            item.setData(Qt.UserRole, payload)
    self._schedule_visible_icon_load(delay_ms=0, force=True)


def _configure_cover_gallery_metrics_by_orientation(self, pool: list[dict]):
    if not hasattr(self, "nfo_cover_gallery"):
        return
    gallery = self.nfo_cover_gallery
    landscape_votes = 0
    portrait_votes = 0
    checked = 0
    # 抽样检测若干封面方向：横图用横向卡片尺寸，其他场景保持竖向布局。
    for info in pool:
        if checked >= 16:
            break
        root_dir_str = str(info.get("root_dir", "")).strip()
        if not root_dir_str:
            continue
        cover_path = self._resolve_cover_path_for_root(root_dir_str)
        if not cover_path:
            continue
        checked += 1
        try:
            reader = QImageReader(cover_path)
            sz = reader.size()
            if not sz.isValid():
                continue
            w = max(1, int(sz.width()))
            h = max(1, int(sz.height()))
        except Exception:
            continue
        if w >= int(h * 1.15):
            landscape_votes += 1
        elif h >= int(w * 1.15):
            portrait_votes += 1
    use_landscape = landscape_votes > portrait_votes and landscape_votes > 0
    if use_landscape:
        gallery.setIconSize(QSize(236, 136))
        gallery.setGridSize(QSize(258, 194))
    else:
        gallery.setIconSize(QSize(150, 220))
        gallery.setGridSize(QSize(170, 270))


def _estimate_cover_batch_size(self, first_screen: bool = False) -> int:
    # 兼容保留：条目不再懒加载，仅图片懒加载。
    return 0


def _append_cover_gallery_items(self, count: int):
    # 兼容保留：条目不再懒加载，仅图片懒加载。
    return


def _ensure_cover_gallery_fill(self):
    # 兼容保留：条目不再懒加载，仅图片懒加载。
    self._schedule_visible_icon_load()


def _estimate_visible_icon_window(self) -> tuple[int, int]:
    count = self.nfo_cover_gallery.count()
    if count <= 0:
        return (0, 0)
    vw = max(1, self.nfo_cover_gallery.viewport().width())
    vh = max(1, self.nfo_cover_gallery.viewport().height())
    cell_w = 170
    # 可变高度场景下使用经验值估算可见窗口即可。
    cell_h = 220
    cols = max(1, vw // cell_w)
    rows = max(1, vh // cell_h) + 1
    start_row = max(0, self.nfo_cover_gallery.verticalScrollBar().value() // cell_h)
    start = min(count, start_row * cols)
    end = min(count, (start_row + rows) * cols)
    pad = cols * 2
    return (max(0, start - pad), min(count, end + pad))


def _schedule_visible_icon_load(self, delay_ms: int = 20, force: bool = False):
    if bool(getattr(self, "_cover_visible_load_scheduled", False)):
        return
    if (not force) and bool(getattr(self, "_cover_scrolling", False)):
        return
    self._cover_visible_load_scheduled = True

    def _run():
        self._cover_visible_load_scheduled = False
        self._load_visible_cover_icons()

    QTimer.singleShot(max(0, int(delay_ms)), _run)


def _resolve_cover_path_for_root(self, root_dir_str: str) -> str:
    root_key = (root_dir_str or "").strip().casefold()
    if not root_key:
        return ""
    cache: dict[str, str] = getattr(self, "_cover_path_cache", {})
    if root_key in cache:
        return cache[root_key]
    cover = _pick_primary_cover_fast(Path(root_dir_str))
    out = str(cover) if cover is not None else ""
    cache[root_key] = out
    self._cover_path_cache = cache
    return out


def _resolve_detail_cover_path_for_root(self, root_dir_str: str) -> str:
    root = (root_dir_str or "").strip()
    if not root:
        return ""
    try:
        p = _pick_thumb_cover_fast(Path(root))
        return str(p) if p is not None else ""
    except Exception:
        return ""


def _ensure_cover_original_webp(self, path_str: str) -> str:
    # 兼容旧函数名：现在缓存“原图文件”本体，不做转码。
    raw_path = (path_str or "").strip()
    if not raw_path:
        return ""
    try:
        cache_dirs = [
            _project_image_cache_root_dir() / "cover_original_raw",
            _image_cache_root_dir() / "cover_original_raw",
        ]
        if raw_path.startswith(("http://", "https://")):
            raw_no_query = raw_path.split("?", 1)[0].split("#", 1)[0]
            ext = Path(raw_no_query).suffix.lower() or ".img"
            key = hashlib.sha1(raw_path.encode("utf-8", errors="ignore")).hexdigest()
            for one_dir in cache_dirs:
                try:
                    one_dir.mkdir(parents=True, exist_ok=True)
                    out = one_dir / f"{key}{ext}"
                    if out.exists() and out.stat().st_size > 0:
                        return str(out)
                    with urlopen(raw_path, timeout=10) as resp:
                        data = resp.read()
                    if not data:
                        continue
                    out.write_bytes(data)
                    if out.exists() and out.stat().st_size > 0:
                        return str(out)
                except Exception:
                    continue
            return ""

        src = Path(raw_path)
        if not src.exists() or (not src.is_file()):
            return ""
        try:
            st = src.stat()
            tail_sig = _portable_path_tail_signature(src, depth=5)
            src_sig = f"{tail_sig}|{st.st_mtime_ns}|{st.st_size}"
        except Exception:
            src_sig = _portable_path_tail_signature(src, depth=5) or str(src.name).casefold()
        ext = src.suffix.lower() or ".img"
        key = hashlib.sha1(src_sig.encode("utf-8", errors="ignore")).hexdigest()
        for one_dir in cache_dirs:
            try:
                one_dir.mkdir(parents=True, exist_ok=True)
                out = one_dir / f"{key}{ext}"
                if out.exists() and out.stat().st_size > 0:
                    return str(out)
                shutil.copy2(src, out)
                if out.exists() and out.stat().st_size > 0:
                    return str(out)
            except Exception:
                continue
        # 缓存失败时回退直接读原图，避免功能不可用。
        return str(src)
    except Exception:
        return ""


def _decode_cover_icon(self, path_str: str) -> tuple[QPixmap | None, QSize, str]:
    try:
        original_webp = self._ensure_cover_original_webp(path_str)
        if not original_webp:
            return (None, QSize(_COVER_COL_W, _cover_icon_height_for_kind(self, "portrait")), "unknown")
        src = Path(original_webp)
        try:
            st = src.stat()
            sig = f"{str(src).casefold()}|{st.st_mtime_ns}|{st.st_size}"
        except Exception:
            sig = str(src).casefold()
        reader = QImageReader(str(src))
        src_size = reader.size()
        if not src_size.isValid():
            return (None, QSize(_COVER_COL_W, _cover_icon_height_for_kind(self, "portrait")), "unknown")
        kind = _cover_kind_for_size(src_size.width(), src_size.height())
        target_w = _COVER_COL_W
        target_h = _cover_icon_height_for_kind(self, kind)
        key = hashlib.sha1(f"{sig}|{target_w}x{target_h}".encode("utf-8", errors="ignore")).hexdigest()
        mem_cache: dict[str, QPixmap] = getattr(self, "_cover_icon_mem_cache", {})
        cached_pix = mem_cache.get(key)
        if cached_pix is not None and (not cached_pix.isNull()):
            return (cached_pix, QSize(target_w, target_h), kind)
        thumb_dir = _image_cache_root_dir() / f"cover_thumb_{target_w}x{target_h}"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = thumb_dir / f"{key}.webp"
        if thumb_path.exists():
            disk_pix = QPixmap(str(thumb_path))
            if not disk_pix.isNull():
                normalized = QPixmap(target_w, target_h)
                normalized.fill(Qt.transparent)
                inner_disk = disk_pix.scaled(QSize(target_w, target_h), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                p_disk = QPainter(normalized)
                p_disk.drawPixmap(max(0, (target_w - inner_disk.width()) // 2), max(0, (target_h - inner_disk.height()) // 2), inner_disk)
                p_disk.end()
                mem_cache[key] = normalized
                self._cover_icon_mem_cache = mem_cache
                return (normalized, QSize(target_w, target_h), kind)
        pix_full = self._get_cover_pixmap_shared(path_str)
        if pix_full.isNull():
            return (None, QSize(target_w, target_h), kind)
        inner = pix_full.scaled(
            QSize(target_w, target_h),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        if inner.isNull():
            return (None, QSize(target_w, target_h), kind)
        # 固定横/竖框比例，非标准比例图片在框内留白显示。
        pix = QPixmap(target_w, target_h)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        x = max(0, (pix.width() - inner.width()) // 2)
        y = max(0, (pix.height() - inner.height()) // 2)
        p.drawPixmap(x, y, inner)
        p.end()
        try:
            pix.toImage().save(str(thumb_path), "WEBP")
        except Exception:
            pass
        mem_cache[key] = pix
        # 控制内存缓存上限，避免长时间运行无限增长。
        if len(mem_cache) > 512:
            mem_cache.clear()
            mem_cache[key] = pix
        self._cover_icon_mem_cache = mem_cache
        return (pix, QSize(target_w, target_h), kind)
    except Exception:
        return (None, QSize(_COVER_COL_W, _cover_icon_height_for_kind(self, "portrait")), "unknown")


def _prepare_cover_thumb_task(self, path_str: str, dpr: float) -> tuple[QImage, int, int, str, float] | None:
    """后台解码原图并缩放，主线程仅负责落图。"""
    original_webp = self._ensure_cover_original_webp(path_str)
    if not original_webp:
        return None
    reader = QImageReader(str(original_webp))
    src_size = reader.size()
    if not src_size.isValid():
        return None
    kind = _cover_kind_for_size(src_size.width(), src_size.height())
    target_w = _COVER_COL_W
    target_h = _cover_icon_height_for_kind(self, kind)
    reader.setAutoTransform(True)
    img = reader.read()
    if img.isNull():
        return None
    # 列表封面至少使用 2x 超采样，避免 1x 显示时出现明显糊感。
    render_dpr = max(2.0, float(dpr or 1.0))
    pixel_w = max(1, int(target_w * render_dpr))
    pixel_h = max(1, int(target_h * render_dpr))
    # 关键：直接从原图缩到像素目标，避免“先缩小再放大”造成的不可逆模糊。
    inner = img.scaled(QSize(pixel_w, pixel_h), Qt.KeepAspectRatio, Qt.SmoothTransformation)
    if inner.isNull():
        return None
    canvas = QImage(pixel_w, pixel_h, QImage.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.transparent)
    p = QPainter(canvas)
    p.drawImage(max(0, (pixel_w - inner.width()) // 2), max(0, (pixel_h - inner.height()) // 2), inner)
    p.end()
    return (canvas, target_w, target_h, kind, render_dpr)


def _on_cover_gallery_scroll_idle(self, token: int):
    if token != int(getattr(self, "_cover_scroll_idle_token", -1)):
        return
    self._cover_scrolling = False
    self._schedule_visible_icon_load(delay_ms=36, force=True)


def _load_visible_cover_icons(self):
    """收集需要加载封面图标的行号，提交给后台线程。

    **不做任何磁盘 I/O**：封面路径解析也放到后台线程，
    主线程仅根据行号判断是否已完成/已排队。
    """
    if not hasattr(self, "nfo_cover_gallery"):
        return
    is_scrolling = bool(getattr(self, "_cover_scrolling", False))
    start, end = self._estimate_visible_icon_window()
    if is_scrolling:
        start, end = (0, 0)
    # jobs 现在是 (row, root_dir_str) 而非 (row, cover_path)
    jobs: list[tuple[int, str]] = getattr(self, "_cover_icon_load_jobs", [])
    queued: set[int] = getattr(self, "_cover_icon_queued_rows", set())
    loaded: set[int] = getattr(self, "_cover_icon_loaded_rows", set())
    roots: list[str] = getattr(self, "_cover_row_root_dirs", [])
    enqueue_budget = 8
    enqueued_this_round = 0
    has_more_candidates = False
    for row in range(start, end):
        if enqueued_this_round >= enqueue_budget:
            has_more_candidates = True
            break
        if row in loaded or row in queued:
            continue
        if not (0 <= row < len(roots)):
            continue
        queued.add(row)
        jobs.append((row, roots[row]))
        enqueued_this_round += 1

    # 后台补缓存：对未显示行持续预加载
    count = self.nfo_cover_gallery.count()
    bg_cursor = int(getattr(self, "_cover_bg_cursor", 0))
    bg_budget = 6
    bg_enqueued = 0
    bg_scanned = 0
    while count > 0 and bg_enqueued < bg_budget and bg_scanned < count:
        row = bg_cursor % count
        bg_cursor += 1
        bg_scanned += 1
        if start <= row < end:
            continue
        if row in loaded or row in queued:
            continue
        if not (0 <= row < len(roots)):
            continue
        queued.add(row)
        jobs.append((row, roots[row]))
        bg_enqueued += 1
    self._cover_bg_cursor = bg_cursor
    self._cover_icon_load_jobs = jobs
    self._cover_icon_queued_rows = queued
    self._cover_icon_loaded_rows = loaded
    if jobs:
        token = int(getattr(self, "_cover_icon_load_token", 0))
        QTimer.singleShot(0, lambda t=token: self._continue_cover_icon_loading(t))
    elif has_more_candidates:
        self._schedule_visible_icon_load(delay_ms=48, force=True)


def _on_cover_gallery_scrolled(self, _value: int):
    if not hasattr(self, "nfo_cover_gallery"):
        return
    self._cover_scrolling = True
    token = int(getattr(self, "_cover_scroll_idle_token", 0)) + 1
    self._cover_scroll_idle_token = token
    QTimer.singleShot(90, lambda t=token: self._on_cover_gallery_scroll_idle(t))


def _continue_cover_icon_loading(self, token: int):
    if token != getattr(self, "_cover_icon_load_token", -1):
        return
    jobs = getattr(self, "_cover_icon_load_jobs", [])
    if not jobs:
        return
    queued: set[int] = getattr(self, "_cover_icon_queued_rows", set())
    loaded: set[int] = getattr(self, "_cover_icon_loaded_rows", set())
    inflight: set[int] = getattr(self, "_cover_icon_inflight_rows", set())
    max_async = max(1, int(getattr(self, "_cover_icon_async_max", 4)))
    submitted = 0
    while jobs and len(inflight) < max_async:
        row, root_dir_str = jobs.pop(0)
        queued.discard(row)
        if row < 0 or row >= self.nfo_cover_gallery.count():
            continue
        if row in loaded or row in inflight:
            continue
        if not root_dir_str:
            loaded.add(row)
            continue
        inflight.add(row)
        submitted += 1

        try:
            list_dpr = max(2.0, float(self.nfo_cover_gallery.devicePixelRatioF()))
        except Exception:
            list_dpr = 2.0

        def _job(rd=root_dir_str, d=list_dpr):
            # 封面路径解析 + 图片解码全部在后台线程执行，不阻塞主线程
            cover_path = self._resolve_cover_path_for_root(rd)
            if not cover_path:
                return ("no_cover", "")
            thumb = self._prepare_cover_thumb_task(cover_path, d)
            if thumb is None:
                return ("no_cover", cover_path)
            # 返回 6 元素元组: 原 5 元素 + cover_path
            return (thumb[0], thumb[1], thumb[2], thumb[3], thumb[4], cover_path)

        def _done(result, err, r=row, t=token):
            inflight_local: set[int] = getattr(self, "_cover_icon_inflight_rows", set())
            inflight_local.discard(r)
            self._cover_icon_inflight_rows = inflight_local
            if t != getattr(self, "_cover_icon_load_token", -1):
                return
            if r < 0 or r >= self.nfo_cover_gallery.count():
                return
            loaded_local: set[int] = getattr(self, "_cover_icon_loaded_rows", set())
            if r in loaded_local:
                return
            item = self.nfo_cover_gallery.item(r)
            if item is None:
                loaded_local.add(r)
                self._cover_icon_loaded_rows = loaded_local
                return
            layout_changed_local = False
            try:
                if (not err) and isinstance(result, tuple) and len(result) == 6:
                    img, icon_w, icon_h, cover_kind, render_dpr, resolved_cover_path = result
                    pix = QPixmap.fromImage(img) if isinstance(img, QImage) else QPixmap()
                    if not pix.isNull():
                        try:
                            pix.setDevicePixelRatio(max(1.0, float(render_dpr)))
                        except Exception:
                            pass
                        item.setIcon(QIcon(pix))
                        item.setData(Qt.DecorationRole, pix)
                        item.setSizeHint(QSize(_COVER_ITEM_W, int(icon_h) + _cover_item_text_height(self, item.text())))
                        layout_changed_local = True
                        payload = item.data(Qt.UserRole)
                        if isinstance(payload, dict):
                            payload["thumb_size"] = [int(icon_w), int(icon_h)]
                            payload["cover_kind"] = str(cover_kind or "unknown")
                            payload["cover_path"] = str(resolved_cover_path or "")
                            item.setData(Qt.UserRole, payload)
                            root_key = str(payload.get("root_dir", "") or "").strip().casefold()
                            if root_key and cover_kind in {"portrait", "landscape"}:
                                cache = getattr(self, "_cover_kind_cache", {})
                                if not isinstance(cache, dict):
                                    cache = {}
                                if cache.get(root_key) != cover_kind:
                                    cache[root_key] = cover_kind
                                    self._cover_kind_cache = cache
                                    if hasattr(self, "_schedule_save_ui_session"):
                                        self._schedule_save_ui_session()
            finally:
                loaded_local.add(r)
                self._cover_icon_loaded_rows = loaded_local
                if layout_changed_local and hasattr(self, "nfo_cover_gallery"):
                    self._schedule_cover_gallery_reflow(18)
                if getattr(self, "_cover_icon_load_jobs", []):
                    QTimer.singleShot(0, lambda tt=t: self._continue_cover_icon_loading(tt))

        self._run_async(_job, _done)

    self._cover_icon_queued_rows = queued
    self._cover_icon_loaded_rows = loaded
    self._cover_icon_inflight_rows = inflight
    if jobs and submitted <= 0:
        QTimer.singleShot(28, lambda: self._continue_cover_icon_loading(token))


def _open_cover_detail(self, list_item: QListWidgetItem):
    row = self.nfo_cover_gallery.row(list_item) if hasattr(self, "nfo_cover_gallery") else -1
    now_ms = int(monotonic() * 1000)
    last_row = int(getattr(self, "_cover_last_open_row", -1))
    last_ms = int(getattr(self, "_cover_last_open_ms", 0))
    if row >= 0 and row == last_row and (now_ms - last_ms) < 360:
        return
    self._cover_last_open_row = row
    self._cover_last_open_ms = now_ms
    # 记录进入详情前的画廊行，返回动画优先缩回这一项。
    self._detail_source_cover_row = row
    timer = getattr(self, "_cover_single_click_timer", None)
    if isinstance(timer, QTimer):
        timer.stop()
    self._pending_cover_click_payload = None
    payload = list_item.data(Qt.UserRole) if list_item is not None else None
    if not isinstance(payload, dict):
        return
    cover_locked = str(payload.get("cover_path") or "").strip()
    if not cover_locked and hasattr(self, "nfo_cover_gallery"):
        try:
            row_idx = self.nfo_cover_gallery.row(list_item)
            row_paths = getattr(self, "_cover_row_cover_paths", [])
            if isinstance(row_idx, int) and 0 <= row_idx < len(row_paths):
                cover_locked = str(row_paths[row_idx] or "").strip()
        except Exception:
            cover_locked = ""
    if not cover_locked:
        root_dir = str(payload.get("root_dir") or "").strip()
        if root_dir:
            cover_locked = self._resolve_cover_path_for_root(root_dir)
    self._detail_cover_locked_path = cover_locked
    root_index = _resolve_item_index_by_path(self, payload.get("root_path"), payload.get("root_index"))
    if not isinstance(root_index, int):
        return
    if hasattr(self, "nfo_cover_title"):
        raw_title = str(payload.get("raw_title") or "").strip()
        self.nfo_cover_title.setText(raw_title if raw_title else list_item.text().replace("\n", "").strip())
    # 只使用原图缓存（cover_original_raw），不回退列表缩放图。
    reused_pix = QPixmap()
    if cover_locked:
        try:
            reused_pix = self._get_cover_pixmap_shared(cover_locked)
        except Exception:
            reused_pix = QPixmap()
    self._detail_cover_reused = isinstance(reused_pix, QPixmap) and (not reused_pix.isNull())
    self._detail_cover_reused_pix = reused_pix if self._detail_cover_reused else QPixmap()
    kind_guess = str(payload.get("cover_kind") or "").strip().lower()
    if kind_guess not in {"portrait", "landscape"} and self._detail_cover_reused:
        kind_guess = _cover_kind_for_size(reused_pix.width(), reused_pix.height())
    if kind_guess not in {"portrait", "landscape"}:
        kind_guess = "portrait"
    self._apply_detail_preview_height_for_kind(kind_guess)
    # 双击先立刻切到详情页并开启动画，重逻辑延后到动画后执行。
    self.nfo_left_stack.setCurrentIndex(2)
    self._set_left_view_toggle_state("cover")
    self._left_busy_active = True
    self._left_busy_token = int(getattr(self, "_left_busy_token", 0)) + 1
    self._defer_media_resource_refresh = True
    self._defer_media_resource_token = int(getattr(self, "_defer_media_resource_token", 0)) + 1
    self._detail_anim_token = int(getattr(self, "_detail_anim_token", 0)) + 1
    self._detail_pending_payload = dict(payload)
    self._show_busy_after_anim_token = int(self._left_busy_token)
    # 动画期间保持详情区域占位可见，避免布局重排导致只剩一个大框。
    if hasattr(self, "nfo_cover_preview"):
        self.nfo_cover_preview.show()
        self.nfo_cover_preview.setPixmap(QPixmap())
        self.nfo_cover_preview.setText("")
    else:
        self._show_cover_preview_loading()
    # meta list 已在返回画廊时清空，此处只补占位项。
    if self.nfo_cover_meta_list.topLevelItemCount() == 0:
        try:
            placeholder = QTreeWidgetItem(["加载中..."])
            placeholder.setData(0, Qt.UserRole, -1)
            self.nfo_cover_meta_list.addTopLevelItem(placeholder)
        except Exception:
            pass
    # 直接启动画画，优先响应；重逻辑延后执行。
    try:
        self._start_cover_enter_transition(list_item)
    except Exception as exc:
        self._hide_left_busy_overlay()
        if hasattr(self, "_log"):
            self._log(f"详情过渡动画失败: {exc}")
        return
    # 与进入动画并行启动元数据树加载，避免等待动画结束再开始加载。
    self._detail_pending_payload = None
    self._populate_cover_detail_after_transition(dict(payload))


def _populate_cover_detail_after_transition(self, payload: dict):
    root_index = _resolve_item_index_by_path(self, payload.get("root_path"), payload.get("root_index"))
    if not isinstance(root_index, int):
        self._hide_left_busy_overlay()
        return
    # 动画后再异步准备数据，遮罩层持续显示直到主数据装载结束。
    cover_locked = str(getattr(self, "_detail_cover_locked_path", "") or "").strip()
    root_item = self.items[root_index]
    can_scan_secondary = root_item.media_type in {"tvshow", "season", "artist", "movie", "movie_or_video_item"}
    root_dir_key = ""
    need_scan_secondary = False
    if can_scan_secondary:
        root_dir_key = str(root_item.path.parent).casefold()
        loaded_dirs: set[str] = getattr(self, "_lazy_loaded_dirs", set())
        need_scan_secondary = root_dir_key not in loaded_dirs
        if need_scan_secondary and root_dir_key:
            # 进入详情首次加载一次；后续复用内存中已加载项，不再重复扫描磁盘/网络路径。
            loaded_dirs.add(root_dir_key)
            self._lazy_loaded_dirs = loaded_dirs

    def _job():
        discovered = []
        try:
            if need_scan_secondary:
                one_root = root_item.path.parent
                discovered = collect_nfo_items({one_root}, quick_scan=True, max_depth=1)
        except Exception:
            discovered = []
        cover_raw_local = cover_locked
        if not cover_raw_local:
            cover_raw_local = str(payload.get("cover_path") or "").strip()
        return (discovered, cover_raw_local)

    def _done(result, _err):
        try:
            discovered, cover_raw = result if isinstance(result, tuple) else ([], "")
            old_cover_sig = _cover_gallery_signature(self)
            existing = {str(x.path).casefold() for x in self.items}
            added = False
            for item in discovered:
                key = str(item.path).casefold()
                if key in existing:
                    continue
                existing.add(key)
                self.items.append(item)
                added = True
            if added:
                self._build_item_tree()
                new_cover_sig = _cover_gallery_signature(self)
                if (
                    hasattr(self, "_refresh_cover_gallery")
                    and hasattr(self, "nfo_left_stack")
                    and self.nfo_left_stack.currentIndex() == 1
                    and old_cover_sig != new_cover_sig
                ):
                    self._refresh_cover_gallery()
            # 关键约束：详情图不再走第二套加载流程，始终使用动画同源图。
            self.nfo_cover_meta_list.clear()
            parent_of: dict[int, int | None] = getattr(self, "_tree_parent_of", {})
            valid_indices: set[int] = set()
            stack = [root_index]
            while stack:
                cur = stack.pop()
                if cur in valid_indices or not (0 <= cur < len(self.items)):
                    continue
                valid_indices.add(cur)
                for child_idx, p_idx in parent_of.items():
                    if p_idx == cur:
                        stack.append(child_idx)
            if not valid_indices:
                valid_indices.add(root_index)
            children_of: dict[int, list[int]] = {}
            for child_idx in valid_indices:
                p_idx = parent_of.get(child_idx)
                if isinstance(p_idx, int) and p_idx in valid_indices:
                    children_of.setdefault(p_idx, []).append(child_idx)
            root_pick = root_index if root_index in valid_indices else next(iter(valid_indices))
            index_to_row: dict[int, QTreeWidgetItem] = {}

            def _build_meta_node(idx: int, parent_row: QTreeWidgetItem | None = None):
                row = QTreeWidgetItem([_friendly_item_title(self.items[idx])])
                row.setData(0, Qt.UserRole, idx)
                row.setData(0, int(Qt.UserRole) + 1, str(self.items[idx].path))
                index_to_row[idx] = row
                if parent_row is None:
                    self.nfo_cover_meta_list.addTopLevelItem(row)
                else:
                    parent_row.addChild(row)
                child_list = sorted(children_of.get(idx, []), key=lambda one: _friendly_item_title(self.items[one]).lower())
                for c_idx in child_list:
                    _build_meta_node(c_idx, row)

            _build_meta_node(root_pick)
            self.nfo_cover_meta_list.collapseAll()
            top = self.nfo_cover_meta_list.topLevelItem(0)
            if top is not None:
                top.setExpanded(True)
            # 进入详情默认选中一级主 NFO（电视剧/电影根节点），不自动跳到二级项。
            pick_idx = root_pick
            pick_item = index_to_row.get(pick_idx)
            if pick_item is not None:
                parent = pick_item.parent()
                while parent is not None:
                    parent.setExpanded(True)
                    parent = parent.parent()
                self.nfo_cover_meta_list.setCurrentItem(pick_item)
                pick_item.setSelected(True)
                # 右侧字段刷新（含NFO解析）再后置，避免与详情树刷出同帧卡顿。
                QTimer.singleShot(180, self._on_cover_meta_selection_changed)
            cur_defer_token = int(getattr(self, "_defer_media_resource_token", 0))
            QTimer.singleShot(950, lambda t=cur_defer_token: self._flush_deferred_media_resource_refresh(t))
        finally:
            self._hide_left_busy_overlay()

    self._run_async(_job, _done)


def _consume_cover_single_click(self):
    payload = getattr(self, "_pending_cover_click_payload", None)
    self._pending_cover_click_payload = None
    if not isinstance(payload, dict):
        return
    root_index = _resolve_item_index_by_path(self, payload.get("root_path"), payload.get("root_index"))
    if not isinstance(root_index, int):
        meta_paths = payload.get("meta_paths", [])
        if isinstance(meta_paths, list) and meta_paths:
            root_index = _resolve_item_index_by_path(self, meta_paths[0], None)
    if not isinstance(root_index, int):
        meta_indices = payload.get("meta_indices", [])
        if isinstance(meta_indices, list) and meta_indices:
            root_index = meta_indices[0]
    if not isinstance(root_index, int):
        return
    if not self._select_tree_item_by_index(root_index):
        return
    self.load_selected_metadata(silent_if_empty=True, force_reload=True)
    if hasattr(self, "_schedule_save_ui_session"):
        self._schedule_save_ui_session()


def _on_cover_gallery_context_menu(self, pos):
    if not hasattr(self, "nfo_cover_gallery"):
        return
    list_item = self.nfo_cover_gallery.itemAt(pos)
    if list_item is None:
        return
    payload = list_item.data(Qt.UserRole)
    if not isinstance(payload, dict):
        return
    root_idx = _resolve_item_index_by_path(self, payload.get("root_path"), payload.get("root_index"))
    if not isinstance(root_idx, int):
        return
    target_item = self.items[root_idx]
    menu = QMenu(self)
    rename_action = None
    if _get_rename_initial_text(target_item) is not None:
        rename_action = menu.addAction("重命名")
    open_path_action = menu.addAction("打开文件路径")
    chosen = menu.exec(self.nfo_cover_gallery.viewport().mapToGlobal(pos))
    if rename_action is not None and chosen is rename_action:
        self._start_cover_gallery_rename(list_item, root_idx)
        return
    if chosen is open_path_action:
        target_dir = target_item.path.parent
        if not target_dir.exists():
            QMessageBox.warning(self, "提示", f"路径不存在：{target_dir}")
            return
        try:
            ok = QDesktopServices.openUrl(QUrl.fromLocalFile(str(target_dir)))
            if not ok:
                raise RuntimeError("系统未能打开该路径")
        except Exception as exc:
            QMessageBox.critical(self, "打开失败", f"无法打开路径：{exc}")


def _start_cover_gallery_rename(self, list_item: QListWidgetItem, root_idx: int):
    item = self.items[root_idx]
    initial = _get_rename_initial_text(item)
    if initial is None:
        return
    from PySide6.QtWidgets import QInputDialog
    mt = str(getattr(item, "media_type", "") or "")
    if mt == "season":
        label = "输入新的季号："
    elif mt in {"episode", "movie_or_video_item"}:
        label = "输入新文件名（不含扩展名）："
    else:
        label = "输入新名称："
    new_text, ok = QInputDialog.getText(self, "重命名", label, text=initial)
    if not ok or not new_text.strip():
        return
    new_text = new_text.strip()
    if new_text == initial:
        return
    success = False
    if mt == "tvshow":
        success = self._exec_rename_folder(item, new_text)
    elif mt == "season":
        success = self._exec_rename_season(item, new_text)
    elif mt in {"episode", "movie_or_video_item"}:
        success = self._exec_rename_episode(item, new_text)
    if success:
        new_title = _cover_caption(self.items[root_idx])
        list_item.setText(_format_cover_caption_multiline(self, new_title, max_lines=3))
        payload = list_item.data(Qt.UserRole)
        if isinstance(payload, dict):
            payload["raw_title"] = new_title
            payload["root_path"] = str(self.items[root_idx].path)
            payload["root_index"] = int(root_idx)
            payload["root_dir"] = str(self.items[root_idx].path.parent)
            list_item.setData(Qt.UserRole, payload)


def _on_cover_gallery_item_clicked(self, list_item: QListWidgetItem):
    payload = list_item.data(Qt.UserRole) if list_item is not None else None
    if not isinstance(payload, dict):
        return
    timer = getattr(self, "_cover_single_click_timer", None)
    if not isinstance(timer, QTimer):
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._consume_cover_single_click)
        self._cover_single_click_timer = timer
    self._pending_cover_click_payload = dict(payload)
    # 单击延迟 0.5s 再响应，避免与双击进入详情冲突。
    timer.start(500)


def _on_cover_gallery_item_pressed(self, list_item: QListWidgetItem):
    if not hasattr(self, "nfo_cover_gallery"):
        return
    row = self.nfo_cover_gallery.row(list_item)
    now_ms = int(monotonic() * 1000)
    last_row = int(getattr(self, "_cover_last_press_row", -1))
    last_ms = int(getattr(self, "_cover_last_press_ms", 0))
    app = QApplication.instance()
    dci = app.doubleClickInterval() if app is not None else 240
    self._cover_last_press_row = row
    self._cover_last_press_ms = now_ms
    # 在第二次按下时提前进入详情，避免等待双击信号造成体感延迟。
    if row >= 0 and row == last_row and (now_ms - last_ms) <= (int(dci) + 50):
        self._open_cover_detail(list_item)


def _select_tree_item_by_index(self, target_idx: int) -> bool:
    if not (0 <= target_idx < len(self.items)):
        return False
    found_node = None

    def _scan(node):
        nonlocal found_node
        idx = node.data(0, self.TREE_INDEX_ROLE)
        if idx == target_idx:
            found_node = node

    self._visit_tree_nodes(_scan)
    if found_node is None:
        # 图片模式下可能先命中未懒加载到树的节点，先按目标项触发一次懒加载后重试。
        try:
            seed = self.items[target_idx]
            if self._ensure_secondary_items_loaded([seed]):
                self._visit_tree_nodes(_scan)
        except Exception:
            pass
    if found_node is None:
        return False
    self.item_list.blockSignals(True)
    try:
        self._visit_tree_nodes(lambda n: n.setSelected(False))
        found_node.setSelected(True)
        self.item_list.setCurrentItem(found_node)
    finally:
        self.item_list.blockSignals(False)
    return True


def _on_cover_meta_selection_changed(self):
    if not hasattr(self, "nfo_cover_meta_list"):
        return
    self._rename_click_timer.stop()
    selected = self.nfo_cover_meta_list.selectedItems()
    if len(selected) == 1:
        row = selected[0]
        idx = _resolve_item_index_by_path(
            self,
            row.data(0, int(Qt.UserRole) + 1) if row else "",
            row.data(0, Qt.UserRole) if row else None,
        )
        if isinstance(idx, int):
            row.setData(0, Qt.UserRole, idx)
            cur_path = str(self.items[idx].path)
            if cur_path != getattr(self, "_rename_sel_path", ""):
                self._rename_sel_path = cur_path
                self._rename_sel_ms = monotonic() * 1000.0
    elif len(selected) != 1:
        self._rename_sel_path = ""
        self._rename_sel_ms = 0.0
    if len(selected) != 1:
        return
    row = selected[0] if selected else self.nfo_cover_meta_list.currentItem()
    if row is None:
        return
    idx = _resolve_item_index_by_path(self, row.data(0, int(Qt.UserRole) + 1), row.data(0, Qt.UserRole))
    if not isinstance(idx, int):
        return
    row.setData(0, Qt.UserRole, idx)
    self._cover_meta_pending_idx = int(idx)
    token = int(getattr(self, "_cover_meta_select_token", 0)) + 1
    self._cover_meta_select_token = token
    QTimer.singleShot(90, lambda t=token: self._apply_cover_meta_selection(t))


def _apply_cover_meta_selection(self, token: int):
    if token != int(getattr(self, "_cover_meta_select_token", -1)):
        return
    idx = int(getattr(self, "_cover_meta_pending_idx", -1))
    if idx < 0:
        return
    if not self._select_tree_item_by_index(idx):
        return
    # 详情页下方列表选中只刷新字段，避免每次都做媒体资源扫描导致卡顿。
    self.load_selected_metadata(silent_if_empty=True, force_reload=True, include_media_resources=False)
    # 停留后再补一次媒体资源刷新：兼顾切换流畅与上传区内容正确。
    QTimer.singleShot(260, lambda t=token: self._apply_cover_meta_media_refresh(t))
    if hasattr(self, "_schedule_save_ui_session"):
        self._schedule_save_ui_session()


def _apply_cover_meta_media_refresh(self, token: int):
    # 仅对最后一次稳定选择生效，避免快速切换时重复重刷。
    if token != int(getattr(self, "_cover_meta_select_token", -1)):
        return
    if not hasattr(self, "nfo_left_stack") or self.nfo_left_stack.currentIndex() != 2:
        return
    try:
        self.load_selected_metadata(silent_if_empty=True, force_reload=True, include_media_resources=True)
    except Exception:
        return


def bind_scan_tree_methods(cls):
    cls._selected_items = _selected_items
    cls._selected_items_with_descendants = _selected_items_with_descendants
    cls.refresh_items = refresh_items
    cls._on_scan_progress = _on_scan_progress
    cls._build_item_tree = _build_item_tree
    cls._on_scan_finished = _on_scan_finished
    cls._visit_tree_nodes = _visit_tree_nodes
    cls._reselect_by_paths = _reselect_by_paths
    cls._ensure_secondary_items_loaded = _ensure_secondary_items_loaded
    cls._update_scan_stats_label = _update_scan_stats_label
    cls._on_item_list_context_menu = _on_item_list_context_menu
    cls._on_cover_meta_list_context_menu = _on_cover_meta_list_context_menu
    cls._on_item_selection_changed = _on_item_selection_changed
    cls._apply_left_title_filter = _apply_left_title_filter
    cls._on_left_title_filter_changed = _on_left_title_filter_changed
    cls._set_cover_preview = _set_cover_preview
    cls._set_cover_preview_async = _set_cover_preview_async
    cls._cover_preview_target_size = _cover_preview_target_size
    cls._scale_preview_pixmap = _scale_preview_pixmap
    cls._get_cover_pixmap_shared = _get_cover_pixmap_shared
    cls._ensure_cover_loading_spinner = _ensure_cover_loading_spinner
    cls._place_cover_loading_spinner = _place_cover_loading_spinner
    cls._show_cover_preview_loading = _show_cover_preview_loading
    cls._apply_detail_preview_height_for_kind = _apply_detail_preview_height_for_kind
    cls._ensure_left_busy_overlay = _ensure_left_busy_overlay
    cls._show_left_busy_overlay = _show_left_busy_overlay
    cls._hide_left_busy_overlay = _hide_left_busy_overlay
    cls._show_left_busy_overlay_if_active = _show_left_busy_overlay_if_active
    cls._start_cover_enter_transition = _start_cover_enter_transition
    cls._on_cover_enter_transition_finished = _on_cover_enter_transition_finished
    cls._replace_preview_with_smooth = _replace_preview_with_smooth
    cls._flush_deferred_media_resource_refresh = _flush_deferred_media_resource_refresh
    cls._trigger_detail_data_load_after_anim = _trigger_detail_data_load_after_anim
    cls._populate_cover_detail_after_transition = _populate_cover_detail_after_transition
    cls._set_left_view_toggle_state = _set_left_view_toggle_state
    cls._switch_left_nfo_view = _switch_left_nfo_view
    cls._sync_cover_selection_from_tree = _sync_cover_selection_from_tree
    cls._back_to_cover_gallery = _back_to_cover_gallery
    cls._start_cover_back_transition = _start_cover_back_transition
    cls._finish_back_to_cover_gallery = _finish_back_to_cover_gallery
    cls._estimate_cover_batch_size = _estimate_cover_batch_size
    cls._append_cover_gallery_items = _append_cover_gallery_items
    cls._ensure_cover_gallery_fill = _ensure_cover_gallery_fill
    cls._estimate_visible_icon_window = _estimate_visible_icon_window
    cls._schedule_visible_icon_load = _schedule_visible_icon_load
    cls._resolve_cover_path_for_root = _resolve_cover_path_for_root
    cls._resolve_detail_cover_path_for_root = _resolve_detail_cover_path_for_root
    cls._ensure_cover_original_webp = _ensure_cover_original_webp
    cls._prepare_cover_thumb_task = _prepare_cover_thumb_task
    cls._decode_cover_icon = _decode_cover_icon
    cls._on_cover_gallery_scroll_idle = _on_cover_gallery_scroll_idle
    cls._load_visible_cover_icons = _load_visible_cover_icons
    cls._on_cover_gallery_scrolled = _on_cover_gallery_scrolled
    cls._schedule_cover_gallery_reflow = _schedule_cover_gallery_reflow
    cls._reflow_cover_gallery_rows = _reflow_cover_gallery_rows
    cls._apply_cover_gallery_hint_mode = _apply_cover_gallery_hint_mode
    cls._configure_cover_gallery_metrics_by_orientation = _configure_cover_gallery_metrics_by_orientation
    cls._refresh_cover_gallery = _refresh_cover_gallery
    cls._continue_cover_icon_loading = _continue_cover_icon_loading
    cls._open_cover_detail = _open_cover_detail
    cls._consume_cover_single_click = _consume_cover_single_click
    cls._on_cover_gallery_context_menu = _on_cover_gallery_context_menu
    cls._start_cover_gallery_rename = _start_cover_gallery_rename
    cls._on_cover_gallery_item_clicked = _on_cover_gallery_item_clicked
    cls._on_cover_gallery_item_pressed = _on_cover_gallery_item_pressed
    cls._select_tree_item_by_index = _select_tree_item_by_index
    cls._on_cover_meta_selection_changed = _on_cover_meta_selection_changed
    cls._apply_cover_meta_selection = _apply_cover_meta_selection
    cls._apply_cover_meta_media_refresh = _apply_cover_meta_media_refresh
    cls._init_rename_state = _init_rename_state
    cls._start_tree_item_rename = _start_tree_item_rename
    cls._on_tree_item_changed_for_rename = _on_tree_item_changed_for_rename
    cls._on_tree_clicked_for_rename = _on_tree_clicked_for_rename
    cls._consume_rename_click = _consume_rename_click
    cls._exec_rename_folder = _exec_rename_folder
    cls._exec_rename_season = _exec_rename_season
    cls._exec_rename_episode = _exec_rename_episode

