import html
import io
import re
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

from PySide6.QtCore import QObject, Qt, QTimer, Signal, QSize
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

try:
    from PIL import Image

    HAS_PIL = True
except Exception:
    HAS_PIL = False


class _DialogSignals(QObject):
    preview_ready = Signal(int, int, QPixmap)
    search_done = Signal(int, object, str)


class _SmoothListWidget(QListWidget):
    resized = Signal()

    def wheelEvent(self, event):
        sb = self.verticalScrollBar()
        if sb is None:
            super().wheelEvent(event)
            return

        pixel_delta = event.pixelDelta().y()
        if pixel_delta != 0:
            sb.setValue(sb.value() - int(pixel_delta * 1.2))
            event.accept()
            return

        angle_y = event.angleDelta().y()
        if angle_y == 0:
            super().wheelEvent(event)
            return

        # 固定小步滚动，避免一次滚轮“翻一页”。
        step = max(28, min(48, sb.singleStep() or 32))
        sb.setValue(sb.value() - int(angle_y / 120) * step)
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()


def _normalize_url(raw: str) -> str | None:
    one = html.unescape(raw).replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
    if one.startswith("http%3A") or one.startswith("https%3A"):
        one = unquote(one)
    if one.startswith("/"):
        return None
    if not one.startswith(("http://", "https://")):
        return None
    return one.strip()


def _is_thumbnail_url(url: str) -> bool:
    low = url.lower()
    if "gstatic.com/images?q=tbn:" in low:
        return True
    if "encrypted-tbn" in low:
        return True
    if re.search(r"[?&](w|h)=\d{2,4}\b", low):
        return True
    return False


def _download_image_preview_bytes(raw_url: str, timeout_sec: int = 4, max_bytes: int = 350 * 1024) -> bytes | None:
    url = raw_url.strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=timeout_sec) as resp:
            content_len = resp.headers.get("Content-Length")
            if content_len:
                try:
                    if int(content_len) > max_bytes:
                        return None
                except Exception:
                    pass
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                return None
            return data
    except Exception:
        return None


def _extract_google_out_url(raw_href: str) -> str | None:
    href = html.unescape(raw_href).replace("\\/", "/").strip()
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("/"):
        href = "https://www.google.com" + href
    parsed = urlparse(href)
    if parsed.scheme not in {"http", "https"}:
        return None
    query_map = parse_qs(parsed.query)
    q_values = query_map.get("q", [])
    if q_values:
        target = unquote(q_values[0]).strip()
        t_parsed = urlparse(target)
        if t_parsed.scheme in {"http", "https"}:
            return target
    return href


def _fetch_google_image_results(keyword: str, limit: int = 240) -> list[dict[str, str]]:
    query = quote_plus(keyword)
    thumb_patterns = [r'(?:data-src|src)="(https?://[^"]+)"']
    full_candidates: list[str] = []
    thumb_candidates: list[str] = []
    size_hints: list[str] = []
    source_page_candidates: list[str] = []
    seen_full: set[str] = set()
    seen_thumb: set[str] = set()

    page_count = max(1, min(2, (limit + 119) // 120))
    for page in range(page_count):
        start = page * 100
        search_urls = [
            f"https://www.google.com/search?tbm=isch&client=firefox-b-d&source=lnt&hl=en&safe=off&q={query}&start={start}",
            f"https://www.google.com.hk/search?tbm=isch&client=firefox-b-d&source=lnt&hl=zh-CN&safe=off&q={query}&start={start}",
        ]
        html_pages: list[str] = []
        for one_url in search_urls:
            try:
                req = Request(
                    one_url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/137.0.0.0 Safari/537.36"
                        ),
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Referer": "https://www.google.com/",
                    },
                )
                with urlopen(req, timeout=8) as resp:
                    html_pages.append(resp.read().decode("utf-8", errors="ignore"))
            except Exception:
                continue
        if not html_pages:
            continue
        merged_html = "\n".join(html_pages)

        rg_meta_blocks = re.findall(
            r'<[^>]*class="rg_meta[^"]*"[^>]*>(.*?)</',
            merged_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        for raw_json in rg_meta_blocks:
            try:
                import json

                item = json.loads(html.unescape(raw_json))
            except Exception:
                continue
            full_one = _normalize_url(str(item.get("ou", "") or ""))
            thumb_one = _normalize_url(str(item.get("tu", "") or ""))
            if not full_one or _is_thumbnail_url(full_one) or full_one in seen_full:
                continue
            seen_full.add(full_one)
            full_candidates.append(full_one)
            if thumb_one and thumb_one not in seen_thumb:
                seen_thumb.add(thumb_one)
                thumb_candidates.append(thumb_one)
            else:
                thumb_candidates.append(full_one)
            ow = item.get("ow")
            oh = item.get("oh")
            if isinstance(ow, int) and isinstance(oh, int) and ow > 0 and oh > 0:
                size_hints.append(f"{ow}x{oh}")
            else:
                size_hints.append("")

        formatted = re.sub(r"\r\n?|\n", "", merged_html)
        playnite_patterns = [
            r'\["(https://encrypted-[^,"]+?)",\d+,\d+\],\["(https?://.+?)",(\d+),(\d+)\]',
            r'\["(https:\\/\\/encrypted-[^,"]+?)",\d+,\d+\],\["(https?:.+?)",(\d+),(\d+)\]',
        ]
        parsed_matches: list[tuple[str, str, str, str]] = []
        for pat in playnite_patterns:
            parsed_matches.extend(re.findall(pat, formatted, flags=re.IGNORECASE))
        for thumb_raw, full_raw, h_raw, w_raw in parsed_matches:
            full_one = _normalize_url(full_raw)
            if not full_one or _is_thumbnail_url(full_one) or full_one in seen_full:
                continue
            seen_full.add(full_one)
            full_candidates.append(full_one)
            thumb_one = _normalize_url(thumb_raw)
            if thumb_one and thumb_one not in seen_thumb:
                seen_thumb.add(thumb_one)
                thumb_candidates.append(thumb_one)
            else:
                thumb_candidates.append(full_one)
            try:
                size_hints.append(f"{int(w_raw)}x{int(h_raw)}")
            except Exception:
                size_hints.append("")

        card_pairs = re.findall(
            r'<td class="e3goi".*?<a href="([^"]+)".*?<img[^>]+src="([^"]+)"',
            merged_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        for href_raw, thumb_raw in card_pairs:
            source_page = _extract_google_out_url(href_raw)
            thumb_one = _normalize_url(thumb_raw)
            if source_page and source_page not in source_page_candidates:
                source_page_candidates.append(source_page)
            if thumb_one and _is_thumbnail_url(thumb_one) and thumb_one not in seen_thumb:
                seen_thumb.add(thumb_one)
                thumb_candidates.append(thumb_one)

        for pattern in thumb_patterns:
            for raw in re.findall(pattern, merged_html, flags=re.IGNORECASE):
                one = _normalize_url(raw)
                if not one:
                    continue
                if one in seen_thumb:
                    continue
                seen_thumb.add(one)
                if _is_thumbnail_url(one):
                    thumb_candidates.append(one)

        if len(full_candidates) >= limit:
            break

    # 标准页为空时，回退到 basic/udm=2 页面，至少拿到缩略图 + 来源页链接
    if not full_candidates and not thumb_candidates:
        basic_urls = [
            f"https://www.google.com.hk/search?udm=2&tbm=isch&hl=zh-CN&safe=off&filter=0&q={query}&start=0",
            f"https://www.google.com/search?udm=2&tbm=isch&hl=en&safe=off&filter=0&q={query}&start=0",
        ]
        for one_url in basic_urls:
            try:
                req = Request(
                    one_url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/137.0.0.0 Safari/537.36"
                        ),
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Referer": "https://www.google.com/",
                    },
                )
                with urlopen(req, timeout=8) as resp:
                    basic_html = resp.read().decode("utf-8", errors="ignore")
            except Exception:
                continue
            card_pairs = re.findall(
                r'<td class="e3goi".*?<a href="([^"]+)".*?<img[^>]+src="([^"]+)"',
                basic_html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            for href_raw, thumb_raw in card_pairs:
                source_page = _extract_google_out_url(href_raw)
                thumb_one = _normalize_url(thumb_raw)
                if source_page and source_page not in source_page_candidates:
                    source_page_candidates.append(source_page)
                if thumb_one and _is_thumbnail_url(thumb_one) and thumb_one not in seen_thumb:
                    seen_thumb.add(thumb_one)
                    thumb_candidates.append(thumb_one)
            if thumb_candidates:
                break

    if not full_candidates and not thumb_candidates:
        return []
    results: list[dict[str, str]] = []
    if full_candidates:
        max_items = min(limit, len(full_candidates))
        for i in range(max_items):
            full_url = full_candidates[i]
            thumb_url = thumb_candidates[i] if i < len(thumb_candidates) else full_url
            results.append(
                {
                    "full_url": full_url,
                    "thumb_url": thumb_url,
                    "label": "原图候选",
                    "original_size": size_hints[i] if i < len(size_hints) else "",
                    "source_page_url": source_page_candidates[i] if i < len(source_page_candidates) else "",
                }
            )
        return results

    max_items = min(limit, len(thumb_candidates))
    for i in range(max_items):
        results.append(
            {
                "full_url": thumb_candidates[i],
                "thumb_url": thumb_candidates[i],
                "label": "仅缩略图候选",
                "original_size": "",
                "source_page_url": source_page_candidates[i] if i < len(source_page_candidates) else "",
            }
        )
    return results


def _resolve_original_image_from_source_page(source_page_url: str) -> str | None:
    source_page_url = source_page_url.strip()
    if not source_page_url:
        return None
    try:
        req = Request(
            source_page_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.google.com/",
            },
        )
        with urlopen(req, timeout=8) as resp:
            body = resp.read(1_000_000).decode("utf-8", errors="ignore")
    except Exception:
        return None

    def _valid(raw: str) -> str | None:
        one = html.unescape(raw).replace("\\/", "/").replace("\\u0026", "&").replace("\\u003d", "=").strip()
        if one.startswith("//"):
            one = "https:" + one
        if one.startswith("/"):
            one = urljoin(source_page_url, one)
        p = urlparse(one)
        if p.scheme not in {"http", "https"}:
            return None
        if _is_thumbnail_url(one):
            return None
        return one

    primary_patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for pat in primary_patterns:
        for raw in re.findall(pat, body, flags=re.IGNORECASE):
            one = _valid(raw)
            if one:
                return one
    return None


class ImageSearchDialog(QDialog):
    def __init__(self, parent, keyword: str, downloader: Callable[[str], Path | None]):
        super().__init__(parent)
        self.setWindowTitle("图片搜索")
        self.resize(980, 720)
        self._closing = False
        self._cleanup_done = False
        self._downloader = downloader
        # 搜索主任务与缩略图下载分池，避免缩略图任务占满线程导致“搜索无响应”。
        self._search_executor = ThreadPoolExecutor(max_workers=1)
        self._preview_executor = ThreadPoolExecutor(max_workers=8)
        self._signals = _DialogSignals(self)
        self._signals.preview_ready.connect(self._on_preview_ready)
        self._signals.search_done.connect(self._on_search_done_main)
        self._futures: set[Future] = set()
        self._future_search_ids: dict[Future, int] = {}
        self._search_seq = 0
        self._active_search_id = 0
        self._search_timeout_timer = QTimer(self)
        self._search_timeout_timer.setSingleShot(True)
        self._search_timeout_timer.setInterval(15000)
        self._search_timeout_timer.timeout.connect(self._on_search_timeout)
        self._results: list[dict[str, str]] = []
        self._selected: dict[str, str] | None = None
        self._next_index = 0
        self._page_size = 12
        self._pending_preview_items: dict[int, QListWidgetItem] = {}
        self.selected_path: Path | None = None

        root = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("搜索关键词"))
        self.keyword_edit = QLineEdit(keyword)
        top.addWidget(self.keyword_edit, 1)
        self.search_btn = QPushButton("搜索")
        self.search_btn.clicked.connect(self._start_search)
        top.addWidget(self.search_btn)
        root.addLayout(top)

        self.status_label = QLabel("请输入关键词后点击搜索。")
        root.addWidget(self.status_label)

        self.list_widget = _SmoothListWidget()
        self.list_widget.setViewMode(QListWidget.IconMode)
        self.list_widget.setResizeMode(QListWidget.Adjust)
        self.list_widget.setMovement(QListWidget.Static)
        self.list_widget.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list_widget.setSpacing(8)
        self.list_widget.setIconSize(QSize(200, 140))
        self.list_widget.setGridSize(QSize(220, 210))
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.setWordWrap(True)
        self.list_widget.setSelectionRectVisible(False)
        self.list_widget.setFocusPolicy(Qt.NoFocus)
        self.list_widget.setStyleSheet(
            "QListWidget::item{"
            "margin: 2px;"
            "padding: 4px;"
            "color: #1f2d3d;"
            "background: rgba(0,0,0,0);"
            "border: 1px solid rgba(0,0,0,0);"
            "border-radius: 8px;"
            "}"
            "QListWidget::item:selected{"
            "color: #10243d;"
            "background: rgba(130,174,232,0.35);"
            "border: 1px solid rgba(92,144,220,0.95);"
            "border-radius: 8px;"
            "}"
            "QListWidget::item:selected:active{"
            "color: #10243d;"
            "background: rgba(130,174,232,0.35);"
            "border: 1px solid rgba(92,144,220,0.95);"
            "border-radius: 8px;"
            "}"
            "QListWidget::item:selected:!active{"
            "color: #10243d;"
            "background: rgba(130,174,232,0.30);"
            "border: 1px solid rgba(92,144,220,0.85);"
            "border-radius: 8px;"
            "}"
        )
        self.list_widget.verticalScrollBar().setSingleStep(32)
        self.list_widget.itemDoubleClicked.connect(self._apply_selected)
        self.list_widget.itemSelectionChanged.connect(self._on_selection_changed)
        self.list_widget.verticalScrollBar().valueChanged.connect(self._on_scroll_changed)
        self.list_widget.resized.connect(self._update_grid_layout)
        root.addWidget(self.list_widget, 1)

        btns = QHBoxLayout()
        self.apply_btn = QPushButton("应用所选图片")
        self.apply_btn.clicked.connect(self._apply_selected)
        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.reject)
        btns.addWidget(self.apply_btn)
        btns.addWidget(self.close_btn)
        btns.addStretch(1)
        root.addLayout(btns)

        if keyword.strip():
            QTimer.singleShot(120, self._start_search)
        QTimer.singleShot(0, self._update_grid_layout)

    def _update_grid_layout(self):
        # 根据当前窗口宽度自适应列数，并保持单元格尺寸一致，避免异步回填时重叠。
        vp = self.list_widget.viewport()
        if vp is None:
            return
        w = max(1, vp.width())
        spacing = max(0, self.list_widget.spacing())
        min_cell_w = 220
        max_cols = 10
        cols = max(1, min(max_cols, w // min_cell_w))
        cell_w = max(180, (w - spacing * (cols + 1)) // cols)
        icon_w = max(120, cell_w - 20)
        icon_h = max(84, int(icon_w * 0.7))
        cell_h = icon_h + 66
        self.list_widget.setIconSize(QSize(icon_w, icon_h))
        self.list_widget.setGridSize(QSize(cell_w, cell_h))
        gs = self.list_widget.gridSize()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item is not None:
                item.setSizeHint(gs)

    def _recreate_executors(self):
        try:
            self._search_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            self._preview_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        self._search_executor = ThreadPoolExecutor(max_workers=1)
        self._preview_executor = ThreadPoolExecutor(max_workers=8)

    def _on_search_timeout(self):
        if self._closing or self.search_btn.isEnabled():
            return
        # 使当前回调失效，避免超时后旧任务回来覆盖界面。
        self._search_seq += 1
        self._active_search_id = self._search_seq
        self.search_btn.setEnabled(True)
        self.status_label.setText("搜索超时，已重置后台任务，请重试。")
        self._recreate_executors()

    def _cleanup_async_tasks(self):
        if self._cleanup_done:
            return
        self._closing = True
        self._cleanup_done = True
        self._pending_preview_items.clear()
        for fut in list(self._futures):
            try:
                fut.cancel()
            except Exception:
                pass
        self._futures.clear()
        self._future_search_ids.clear()
        self._search_timeout_timer.stop()
        try:
            self._signals.preview_ready.disconnect(self._on_preview_ready)
        except Exception:
            pass
        try:
            self._signals.search_done.disconnect(self._on_search_done_main)
        except Exception:
            pass
        try:
            self._search_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            self._preview_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def done(self, r: int):
        self._cleanup_async_tasks()
        super().done(r)

    def closeEvent(self, event):
        self._cleanup_async_tasks()
        super().closeEvent(event)

    def _start_search(self):
        if self._closing:
            return
        kw = self.keyword_edit.text().strip()
        if not kw:
            QMessageBox.warning(self, "提示", "请输入搜索关键词。")
            return
        self.search_btn.setEnabled(False)
        self.status_label.setText("正在搜索，请稍候...")
        # 新搜索开始时取消旧任务，避免旧回调叠加到新界面。
        for fut in list(self._futures):
            try:
                fut.cancel()
            except Exception:
                pass
        self._futures.clear()
        self._future_search_ids.clear()
        # 与“重开窗口再搜”等效：每次搜索重建线程池，清理潜在卡死任务。
        self._recreate_executors()
        self.list_widget.clear()
        self._results.clear()
        self._selected = None
        self._next_index = 0
        self._pending_preview_items.clear()
        self._search_seq += 1
        self._active_search_id = self._search_seq

        future = self._search_executor.submit(_fetch_google_image_results, kw, 240)
        self._futures.add(future)
        self._future_search_ids[future] = self._active_search_id
        future.add_done_callback(self._on_search_done)
        self._search_timeout_timer.start()

    def _on_search_done(self, future: Future):
        self._futures.discard(future)
        search_id = self._future_search_ids.pop(future, 0)
        if self._closing:
            return
        try:
            results = future.result()
            self._signals.search_done.emit(search_id, results, "")
        except Exception as exc:
            self._signals.search_done.emit(search_id, [], str(exc))

    def _on_search_done_main(self, search_id: int, results: object, err: str):
        if self._closing:
            return
        if search_id != self._active_search_id:
            return
        self._search_timeout_timer.stop()
        self.search_btn.setEnabled(True)
        if err:
            QMessageBox.critical(self, "搜索失败", f"无法获取搜索结果：{err}")
            self.status_label.setText("搜索失败。")
            return
        self._results = list(results) if isinstance(results, list) else []
        self._next_index = 0
        if not self._results:
            self.status_label.setText("未找到可用图片结果。")
            return
        self.status_label.setText(f"已抓取 {len(self._results)} 条候选，正在加载预览...")
        self._load_next_page()

    def _load_next_page(self):
        if self._closing:
            return
        if self._next_index >= len(self._results):
            return
        end = min(self._next_index + self._page_size, len(self._results))
        for idx in range(self._next_index, end):
            one = self._results[idx]
            display_size = one.get("original_size", "").strip() or "未知原图尺寸"
            item = QListWidgetItem(f"{display_size}\n#{idx + 1}")
            item.setData(Qt.UserRole, one)
            item.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
            item.setSizeHint(self.list_widget.gridSize())
            # 占位 icon
            icon_sz = self.list_widget.iconSize()
            placeholder = QPixmap(max(1, icon_sz.width()), max(1, icon_sz.height()))
            placeholder.fill(Qt.lightGray)
            item.setIcon(QIcon(placeholder))
            self.list_widget.addItem(item)
            self._pending_preview_items[idx] = item
            fut = self._preview_executor.submit(
                self._load_preview_for_index,
                self._active_search_id,
                idx,
                one.get("thumb_url", "") or one.get("full_url", ""),
            )
            self._futures.add(fut)
            self._future_search_ids[fut] = self._active_search_id
            fut.add_done_callback(lambda f, s=self: (s._futures.discard(f), s._future_search_ids.pop(f, None)))
        self._next_index = end
        if self._next_index < len(self._results):
            self.status_label.setText(f"已加载 {self._next_index}/{len(self._results)}，下滑继续加载...")
            # 首批结果在大窗口里可能还不足以产生滚动条，自动补载到可滚动为止。
            sb = self.list_widget.verticalScrollBar()
            if sb.maximum() <= 0:
                QTimer.singleShot(0, self._load_next_page)
        else:
            self.status_label.setText(f"已全部加载 {len(self._results)}。")

    def _load_preview_for_index(self, search_id: int, idx: int, preview_url: str):
        if self._closing:
            return
        blob = _download_image_preview_bytes(preview_url, timeout_sec=3, max_bytes=260 * 1024)
        if not blob:
            return
        image = QImage.fromData(blob)
        if image.isNull():
            return
        pix = QPixmap.fromImage(image).scaled(200, 140, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if not self._closing:
            self._signals.preview_ready.emit(search_id, idx, pix)

    def _on_preview_ready(self, search_id: int, idx: int, pixmap: QPixmap):
        if self._closing:
            return
        if search_id != self._active_search_id:
            return
        item = self._pending_preview_items.get(idx)
        if item is None:
            return
        item.setIcon(QIcon(pixmap))

    def _on_selection_changed(self):
        items = self.list_widget.selectedItems()
        if not items:
            self._selected = None
            return
        self._selected = items[0].data(Qt.UserRole)

    def _on_scroll_changed(self, _value: int):
        sb = self.list_widget.verticalScrollBar()
        if sb.maximum() <= 0:
            return
        if sb.value() >= int(sb.maximum() * 0.92):
            self._load_next_page()

    def _apply_selected(self, *_args):
        if not self._selected:
            QMessageBox.warning(self, "提示", "请先选中一张图片。")
            return
        full_url = self._selected.get("full_url", "")
        label = self._selected.get("label", "")
        source_page_url = self._selected.get("source_page_url", "")
        if label == "仅缩略图候选" and source_page_url:
            resolved = _resolve_original_image_from_source_page(source_page_url)
            if resolved:
                full_url = resolved
                self._selected["full_url"] = resolved
                self._selected["label"] = "来源页原图候选"
        if not full_url:
            QMessageBox.warning(self, "提示", "无法确定可下载图片链接。")
            return
        if self._selected.get("label", "") == "仅缩略图候选":
            ok = QMessageBox.question(
                self,
                "提示",
                "当前仅有缩略图候选，且未解析到原图。是否仍继续下载并应用？",
            )
            if ok != QMessageBox.StandardButton.Yes:
                return
        self.status_label.setText("正在下载并应用，请稍候...")
        self.repaint()
        path = self._downloader(full_url)
        if path is None:
            self.status_label.setText("下载失败，请换一张图。")
            return
        self.selected_path = path
        self.accept()
