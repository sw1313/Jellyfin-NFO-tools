from pathlib import Path

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QPropertyAnimation, QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QMenu,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QListView,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTextEdit,
    QToolButton,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

from jellyfin_nfo_core import FIELD_DEFINITIONS, MULTI_VALUE_TAGS


class AdaptiveFieldLineEdit(QLineEdit):
    """单行输入保持布局不变，长文本走弹窗编辑。"""

    def __init__(self, tag: str, desc: str, parent=None):
        super().__init__(parent)
        self._tag = tag
        self._desc = desc
        self._long_threshold = 36 if ("id" in tag.lower()) else 72
        self._is_long_text = False
        self.setClearButtonEnabled(True)
        self.textChanged.connect(self._on_text_changed)
        self._on_text_changed(self.text())

    def _on_text_changed(self, value: str):
        txt = (value or "").strip()
        self._is_long_text = len(txt) >= self._long_threshold
        self.setProperty("longText", self._is_long_text)
        # 切换动态属性后触发重绘，让样式表立即生效。
        self.style().unpolish(self)
        self.style().polish(self)
        if txt:
            if self._is_long_text:
                self.setToolTip(f"{self._desc}（双击可展开编辑）\n\n{txt}")
            else:
                self.setToolTip(txt)
        else:
            self.setToolTip(f"{self._desc}（双击可展开编辑）")

    def mouseDoubleClickEvent(self, event):
        if self._is_long_text:
            self._open_expand_dialog()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        menu = self.createStandardContextMenu()
        menu.addSeparator()
        act = menu.addAction("展开编辑...")
        act.triggered.connect(self._open_expand_dialog)
        menu.exec(event.globalPos())

    def _open_expand_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle(f"编辑：{self._desc} ({self._tag})")
        dlg.resize(760, 260)
        lay = QVBoxLayout(dlg)
        editor = QPlainTextEdit(dlg)
        editor.setPlainText(self.text())
        lay.addWidget(editor, 1)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, Qt.Horizontal, dlg)
        lay.addWidget(btns)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        if dlg.exec() == QDialog.Accepted:
            self.setText(editor.toPlainText().strip())


class SmoothListWidget(QListWidget):
    """为滚轮提供平滑过渡动画。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scroll_anim = QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self._scroll_anim.setEasingCurve(QEasingCurve.OutQuint)
        self._scroll_anim.setDuration(190)
        self._wheel_gain = 9
        self._pixel_gain = 1.8
        self._min_duration = 160
        self._max_duration = 320

    def wheelEvent(self, event):
        bar = self.verticalScrollBar()
        if bar is None:
            super().wheelEvent(event)
            return
        pixel = event.pixelDelta().y()
        if pixel != 0:
            delta_px = int(-pixel * self._pixel_gain)
        else:
            angle = event.angleDelta().y()
            if angle == 0:
                super().wheelEvent(event)
                return
            base_step = max(14, bar.singleStep())
            delta_px = int(-(angle / 120.0) * base_step * self._wheel_gain)
        if self._scroll_anim.state() == QAbstractAnimation.Running:
            base_target = int(self._scroll_anim.endValue())
        else:
            base_target = bar.value()
        start = bar.value()
        end = max(bar.minimum(), min(bar.maximum(), base_target + delta_px))
        if end == start:
            event.accept()
            return
        travel = abs(end - start)
        duration = max(self._min_duration, min(self._max_duration, 110 + int(travel * 0.22)))
        self._scroll_anim.stop()
        self._scroll_anim.setStartValue(start)
        self._scroll_anim.setEndValue(end)
        self._scroll_anim.setDuration(duration)
        self._scroll_anim.start()
        event.accept()


def build_ui(window):
    root = QWidget()
    root.setObjectName("Root")
    window.setCentralWidget(root)
    window.setStyleSheet(
        """
        QWidget {
            color: #1f2937;
            font-family: "Microsoft YaHei UI";
            font-size: 13px;
        }
        QWidget#Root {
            background: #f5f7fb;
        }
        QGroupBox {
            border: 1px solid #d5deea;
            border-radius: 10px;
            margin-top: 12px;
            padding-top: 8px;
            background: #ffffff;
            font-weight: 600;
            color: #0f172a;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 6px;
            background: #ffffff;
        }
        QPushButton, QToolButton {
            border: 1px solid #d1dae6;
            border-radius: 8px;
            padding: 6px 12px;
            background: #edf2f8;
            color: #1f2937;
        }
        QPushButton:hover, QToolButton:hover {
            background: #e4ebf5;
        }
        QPushButton:pressed, QToolButton:pressed {
            background: #dbe4f1;
        }
        QPushButton[role="primary"] {
            background: #2463eb;
            border: 1px solid #1f56cc;
            color: #ffffff;
            font-weight: 700;
        }
        QPushButton[role="primary"]:hover {
            background: #1f56cc;
        }
        QPushButton[role="secondary"], QToolButton[role="secondary"] {
            background: #ffffff;
            border: 1px solid #b9c7dc;
            color: #1f3a5f;
            font-weight: 600;
        }
        QPushButton[tone="danger"], QToolButton[tone="danger"] {
            color: #c62828;
            border: 1px solid #efcaca;
            background: #fff5f5;
        }
        QPushButton[role="mini"], QToolButton[role="mini"] {
            min-width: 28px;
            max-width: 28px;
            min-height: 24px;
            max-height: 24px;
            padding: 0;
            border-radius: 6px;
            background: #f4f7fb;
        }
        QLabel[role="hint"] {
            color: #475569;
        }
        QLineEdit, QTextEdit, QPlainTextEdit, QComboBox {
            border: 1px solid #ccd7e6;
            border-radius: 8px;
            background: #fbfdff;
            padding: 6px 8px;
            selection-background-color: #2463eb;
        }
        QLineEdit[longText="true"] {
            border: 1px solid #5b8def;
            background: #f2f7ff;
        }
        QTreeWidget {
            border: 1px solid #d1dae6;
            border-radius: 8px;
            background: #ffffff;
            alternate-background-color: #f8fbff;
        }
        QTreeWidget::item {
            padding: 4px;
        }
        QTreeWidget::item:selected,
        QTreeWidget::item:selected:active,
        QTreeWidget::item:selected:!active {
            background: #dbeafe;
            color: #0f2f66;
            border: none;
            outline: 0;
        }
        QTreeView::item:focus,
        QListView::item:focus {
            border: none;
            outline: 0;
        }
        QScrollArea {
            border: none;
            background: transparent;
        }
        """
    )
    layout = QVBoxLayout(root)
    layout.setContentsMargins(10, 8, 10, 10)
    layout.setSpacing(8)

    top = QHBoxLayout()
    window.nfo_sidebar_toggle_btn = QToolButton()
    window.nfo_sidebar_toggle_btn.setProperty("role", "secondary")
    window.nfo_sidebar_toggle_btn.setToolTip("折叠/展开左侧菜单")
    window.nfo_sidebar_toggle_btn.setText("菜单")
    dehaze_svg = Path(__file__).with_name("dehaze_24dp_1F1F1F_FILL0_wght400_GRAD0_opsz24.svg")
    if dehaze_svg.exists():
        window.nfo_sidebar_toggle_btn.setIcon(QIcon(str(dehaze_svg)))
    top.addWidget(window.nfo_sidebar_toggle_btn)
    for text, fn in [
        ("添加文件夹", window.add_folder),
        ("添加 NFO 文件", window.add_files),
        ("刷新扫描", window.refresh_items),
        ("清空", window.clear_all),
    ]:
        btn = QPushButton(text)
        btn.setProperty("role", "secondary")
        btn.setMinimumHeight(30)
        btn.setMaximumHeight(30)
        top.addWidget(btn)
        btn.clicked.connect(fn)
    top.addStretch(1)
    write_btn = QPushButton("写入选中")
    write_btn.setProperty("role", "primary")
    write_btn.clicked.connect(window.apply_selected_metadata)
    read_btn = QPushButton("读取选中")
    read_btn.setProperty("role", "secondary")
    read_btn.clicked.connect(lambda: window.load_selected_metadata(force_reload=True))
    log_btn = QToolButton()
    log_btn.setProperty("role", "secondary")
    log_btn.setText("日志")
    log_btn.setPopupMode(QToolButton.InstantPopup)
    log_menu = QMenu(log_btn)
    act_show_log = log_menu.addAction("查看日志")
    act_clear_log = log_menu.addAction("清空日志")
    act_show_log.triggered.connect(window._show_log_dialog)
    act_clear_log.triggered.connect(window._clear_logs)
    log_btn.setMenu(log_menu)
    top.addWidget(log_btn)
    top.addWidget(write_btn)
    top.addWidget(read_btn)
    layout.addLayout(top)

    body_splitter = QSplitter(Qt.Horizontal)
    body_splitter.setChildrenCollapsible(False)
    layout.addWidget(body_splitter, 1)

    sidebar = QWidget()
    window.nfo_sidebar_container = sidebar
    sidebar_layout = QVBoxLayout(sidebar)
    sidebar_layout.setContentsMargins(0, 0, 0, 0)
    sidebar_layout.setSpacing(8)

    left_group = QGroupBox("检测到的 NFO 文件")
    window.nfo_left_group = left_group
    left_layout = QVBoxLayout(left_group)
    left_toolbar = QHBoxLayout()
    window.nfo_cover_back_btn = QPushButton("返回")
    window.nfo_cover_back_btn.setProperty("role", "secondary")
    left_toolbar.addWidget(window.nfo_cover_back_btn)
    window.nfo_title_filter_edit = QLineEdit()
    window.nfo_title_filter_edit.setClearButtonEnabled(True)
    window.nfo_title_filter_edit.setMinimumWidth(40)
    window.nfo_title_filter_edit.setMaximumWidth(16777215)
    window.nfo_title_filter_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    left_toolbar.addWidget(window.nfo_title_filter_edit, 1)
    window.nfo_view_list_btn = QToolButton()
    window.nfo_view_list_btn.setProperty("role", "secondary")
    window.nfo_view_list_btn.setCheckable(True)
    window.nfo_view_list_btn.setChecked(True)
    window.nfo_view_list_btn.setToolTip("列表视图")
    window.nfo_view_list_btn.setText("列表")
    list_icon_svg = Path(__file__).with_name("view_list_24dp_1F1F1F_FILL0_wght400_GRAD0_opsz24.svg")
    list_icon_png = Path(__file__).with_name("view_list_24dp_1F1F1F_FILL0_wght400_GRAD0_opsz24.png")
    if list_icon_svg.exists():
        window.nfo_view_list_btn.setIcon(QIcon(str(list_icon_svg)))
    elif list_icon_png.exists():
        window.nfo_view_list_btn.setIcon(QIcon(str(list_icon_png)))
    else:
        window.nfo_view_list_btn.setIcon(window.style().standardIcon(QStyle.SP_FileDialogDetailedView))
    window.nfo_view_cover_btn = QToolButton()
    window.nfo_view_cover_btn.setProperty("role", "secondary")
    window.nfo_view_cover_btn.setCheckable(True)
    window.nfo_view_cover_btn.setToolTip("图片视图")
    window.nfo_view_cover_btn.setText("图片")
    grid_icon_svg = Path(__file__).with_name("grid_view_24dp_1F1F1F_FILL0_wght400_GRAD0_opsz24.svg")
    grid_icon_png = Path(__file__).with_name("grid_view_24dp_1F1F1F_FILL0_wght400_GRAD0_opsz24.png")
    if grid_icon_svg.exists():
        window.nfo_view_cover_btn.setIcon(QIcon(str(grid_icon_svg)))
    elif grid_icon_png.exists():
        window.nfo_view_cover_btn.setIcon(QIcon(str(grid_icon_png)))
    else:
        window.nfo_view_cover_btn.setIcon(window.style().standardIcon(QStyle.SP_FileDialogInfoView))
    left_toolbar.addWidget(window.nfo_view_list_btn)
    left_toolbar.addWidget(window.nfo_view_cover_btn)
    left_layout.addLayout(left_toolbar)
    window.nfo_left_stack = QStackedWidget()
    window.item_list = window._create_item_tree_widget()
    window.item_list.setFocusPolicy(Qt.NoFocus)
    window.nfo_left_stack.addWidget(window.item_list)
    window.nfo_cover_gallery = SmoothListWidget()
    window.nfo_cover_gallery.setViewMode(QListWidget.IconMode)
    window.nfo_cover_gallery.setResizeMode(QListWidget.Adjust)
    window.nfo_cover_gallery.setLayoutMode(QListView.SinglePass)
    window.nfo_cover_gallery.setMovement(QListWidget.Static)
    window.nfo_cover_gallery.setWrapping(True)
    window.nfo_cover_gallery.setUniformItemSizes(False)
    window.nfo_cover_gallery.setSpacing(10)
    window.nfo_cover_gallery.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    window.nfo_cover_gallery.verticalScrollBar().setSingleStep(20)
    window.nfo_cover_gallery.setWordWrap(True)
    window.nfo_cover_gallery.setTextElideMode(Qt.ElideNone)
    window.nfo_cover_gallery.setIconSize(QSize())
    window.nfo_cover_gallery.setGridSize(QSize())
    window.nfo_cover_gallery.setSelectionMode(QAbstractItemView.SingleSelection)
    window.nfo_cover_gallery.setFocusPolicy(Qt.NoFocus)
    window.nfo_cover_gallery.setStyleSheet(
        "QListWidget::item{padding:4px;border:none;outline:0;}"
        "QListWidget::item:selected,QListWidget::item:selected:active,QListWidget::item:selected:!active{"
        "background:#dbeafe;color:#0f2f66;border:none;outline:0;border-radius:8px;}"
    )
    window.nfo_left_stack.addWidget(window.nfo_cover_gallery)
    detail_page = QWidget()
    detail_lay = QVBoxLayout(detail_page)
    detail_lay.setContentsMargins(2, 2, 2, 2)
    detail_lay.setSpacing(8)
    window.nfo_cover_preview = QLabel("暂无封面")
    window.nfo_cover_preview.setAlignment(Qt.AlignCenter)
    window.nfo_cover_preview.setMinimumHeight(180)
    window.nfo_cover_preview.setStyleSheet("border:1px solid #d1dae6;border-radius:8px;background:#f8fbff;")
    detail_lay.addWidget(window.nfo_cover_preview, 4)
    window.nfo_cover_title = QLabel("")
    window.nfo_cover_title.setAlignment(Qt.AlignCenter)
    window.nfo_cover_title.setWordWrap(True)
    detail_lay.addWidget(window.nfo_cover_title)
    window.nfo_cover_meta_list = QTreeWidget()
    window.nfo_cover_meta_list.setHeaderHidden(True)
    window.nfo_cover_meta_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
    window.nfo_cover_meta_list.setRootIsDecorated(True)
    window.nfo_cover_meta_list.setIndentation(16)
    window.nfo_cover_meta_list.setMinimumHeight(120)
    window.nfo_cover_meta_list.setFocusPolicy(Qt.ClickFocus)
    window.nfo_cover_meta_list.setContextMenuPolicy(Qt.CustomContextMenu)
    window.nfo_cover_meta_list.setStyleSheet(
        "QTreeWidget{outline:0;border:1px solid #d1dae6;border-radius:6px;}"
        "QTreeWidget::item{border:none;outline:0;}"
        "QTreeWidget::item:selected,QTreeWidget::item:selected:active,QTreeWidget::item:selected:!active{"
        "background:#dbeafe;color:#0f2f66;border:none;outline:0;}"
        "QTreeWidget::item:focus,QTreeWidget::item:focus:selected{"
        "border:none;outline:0;}"
    )
    detail_lay.addWidget(window.nfo_cover_meta_list, 1)
    window.nfo_left_stack.addWidget(detail_page)
    window.nfo_view_list_btn.clicked.connect(lambda: window._switch_left_nfo_view("list"))
    window.nfo_view_cover_btn.clicked.connect(lambda: window._switch_left_nfo_view("cover"))
    window.nfo_cover_back_btn.clicked.connect(window._back_to_cover_gallery)
    window.nfo_title_filter_edit.textChanged.connect(window._on_left_title_filter_changed)
    window.nfo_cover_gallery.setContextMenuPolicy(Qt.CustomContextMenu)
    window.nfo_cover_gallery.customContextMenuRequested.connect(window._on_cover_gallery_context_menu)
    window.nfo_cover_gallery.itemPressed.connect(window._on_cover_gallery_item_pressed)
    window.nfo_cover_gallery.itemClicked.connect(window._on_cover_gallery_item_clicked)
    window.nfo_cover_gallery.itemDoubleClicked.connect(window._open_cover_detail)
    window.nfo_cover_meta_list.itemSelectionChanged.connect(window._on_cover_meta_selection_changed)
    window.nfo_cover_meta_list.customContextMenuRequested.connect(window._on_cover_meta_list_context_menu)
    window.nfo_cover_meta_list.itemClicked.connect(lambda node, col: window._on_tree_clicked_for_rename(window.nfo_cover_meta_list, node, col))
    window.nfo_cover_meta_list.itemChanged.connect(lambda node, col: window._on_tree_item_changed_for_rename(node, col))
    window.nfo_cover_gallery.verticalScrollBar().valueChanged.connect(window._on_cover_gallery_scrolled)
    left_layout.addWidget(window.nfo_left_stack, 1)
    window.scan_stats_label = QLabel("统计：共 0 部电视剧，0 部电影，0 张专辑。")
    window.scan_stats_label.setWordWrap(True)
    left_layout.addWidget(window.scan_stats_label)
    sidebar_layout.addWidget(left_group, 1)
    body_splitter.addWidget(sidebar)

    right_splitter = QSplitter(Qt.Vertical)
    body_splitter.addWidget(right_splitter)
    sidebar.setMinimumWidth(340)
    body_splitter.setSizes([420, 900])

    def _toggle_sidebar():
        showing = bool(getattr(window, "_nfo_sidebar_visible", True))
        if showing:
            sidebar.hide()
            window._nfo_sidebar_visible = False
            return
        sidebar.show()
        window._nfo_sidebar_visible = True
        try:
            body_splitter.setSizes([420, max(900, body_splitter.width() - 420)])
        except Exception:
            pass

    window._nfo_sidebar_visible = True
    window.nfo_sidebar_toggle_btn.clicked.connect(_toggle_sidebar)

    nfo_group = QGroupBox("可编辑 Jellyfin NFO 字段")
    nfo_layout = QVBoxLayout(nfo_group)
    nfo_scroll = QScrollArea()
    nfo_scroll.setWidgetResizable(True)
    nfo_container = QWidget()
    nfo_grid = QGridLayout(nfo_container)
    nfo_grid.setColumnStretch(0, 0)
    nfo_grid.setColumnStretch(1, 1)
    nfo_grid.setColumnStretch(2, 0)
    nfo_grid.setColumnStretch(3, 1)
    nfo_grid.setHorizontalSpacing(10)
    nfo_grid.setVerticalSpacing(8)
    row = 0
    slot = 0  # 0: 左列，1: 右列
    for tag, desc in FIELD_DEFINITIONS:
        lbl = QLabel(f"{desc} ({tag})")
        lbl.setWordWrap(True)
        is_wide = (tag == "plot") or (tag in MULTI_VALUE_TAGS)
        if is_wide and slot == 1:
            row += 1
            slot = 0
        if is_wide:
            nfo_grid.addWidget(lbl, row, 0)
        else:
            base_col = 0 if slot == 0 else 2
            nfo_grid.addWidget(lbl, row, base_col)

        if tag == "plot":
            edit = QTextEdit()
            edit.setMinimumHeight(120)
            window.plot_edit = edit
            nfo_grid.addWidget(edit, row, 1, 1, 3)
        elif tag == "title":
            le = AdaptiveFieldLineEdit(tag, desc)
            window.field_edits[tag] = le
            row_wrap = QWidget()
            row_lay = QHBoxLayout(row_wrap)
            row_lay.setContentsMargins(0, 0, 0, 0)
            row_lay.setSpacing(6)
            row_lay.addWidget(le, 1)
            fix_btn = QPushButton("修复")
            fix_btn.setToolTip("将所选层级下真实视频同名 NFO 的 title 修复为视频文件名")
            fix_btn.clicked.connect(window.fix_titles_from_video_name_for_selected_level)
            row_lay.addWidget(fix_btn, 0)
            nfo_grid.addWidget(row_wrap, row, 1, 1, 3)
        elif tag in MULTI_VALUE_TAGS:
            mv = window._create_multi_value_editor()
            window.multi_value_editors[tag] = mv
            nfo_grid.addWidget(mv, row, 1, 1, 3)
        else:
            le = AdaptiveFieldLineEdit(tag, desc)
            window.field_edits[tag] = le
            base_col = 0 if slot == 0 else 2
            nfo_grid.addWidget(le, row, base_col + 1)

        if is_wide or tag == "title":
            row += 1
            slot = 0
        else:
            if slot == 0:
                slot = 1
            else:
                slot = 0
                row += 1
    nfo_scroll.setWidget(nfo_container)
    nfo_layout.addWidget(nfo_scroll)
    right_splitter.addWidget(nfo_group)

    media_group = QGroupBox("媒体资源上传（含额外数据，全量支持）")
    media_layout = QVBoxLayout(media_group)
    media_scroll = QScrollArea()
    media_scroll.setWidgetResizable(True)
    media_container = QWidget()
    media_grid = QGridLayout(media_container)
    media_grid.setColumnStretch(0, 1)
    media_grid.setColumnStretch(1, 1)
    media_grid.setHorizontalSpacing(10)
    media_grid.setVerticalSpacing(10)
    card_specs: list[tuple[str, str, str, object, bool, bool]] = []
    for key, label in [
        ("primary", "封面图 (Primary)"),
        ("backdrop", "背景图 (Backdrop)"),
        ("banner", "横幅图 (Banner)"),
        ("logo", "徽标 (Logo)"),
        ("thumb", "缩略图 (Thumb)"),
    ]:
        card_specs.append((label, key, "image", window.image_source_edits[key], False, True))
    for key, label in window.EXTRA_IMAGE_ROWS:
        card_specs.append((label, key, "image", window.extra_image_source_edits[key], True, True))
    for key, label in window.EXTRA_VIDEO_ROWS:
        card_specs.append((label, key, "video", window.extra_video_source_edits[key], True, False))
    for key, label in window.EXTRA_AUDIO_ROWS:
        card_specs.append((label, key, "audio", window.extra_audio_source_edits[key], True, False))

    for idx, (label, key, kind, edit, is_extra, has_search) in enumerate(card_specs):
        window._build_media_target_card(media_grid, idx, label, key, kind, edit, is_extra, has_search)

    r = (len(card_specs) + 1) // 2

    provider_box = QGroupBox("附加 Provider IDs（<providernameid>）")
    pb = QVBoxLayout(provider_box)
    provider_tip = QLabel("格式示例: anidbid=12345/tmdbid=67890")
    provider_tip.setProperty("role", "hint")
    pb.addWidget(provider_tip)
    window.provider_ids_edit.setClearButtonEnabled(True)
    window.provider_ids_edit.setPlaceholderText("例如：anidbid=12345/tmdbid=67890")
    pb.addWidget(window.provider_ids_edit)
    media_grid.addWidget(provider_box, r, 0, 1, 2)
    r += 1

    note = QLabel("说明：多值字段支持 / , ; 换行 分隔；空值不覆盖。图片/视频/音频行支持本地选择与 URL 下载；图片行支持搜索。")
    note.setProperty("role", "hint")
    note.setWordWrap(True)
    media_grid.addWidget(note, r, 0, 1, 2)
    media_scroll.setWidget(media_container)
    media_layout.addWidget(media_scroll)
    right_splitter.addWidget(media_group)
    right_splitter.setSizes([360, 520])
    window._refresh_media_target_visibility([])
    window._switch_left_nfo_view("list")
    window._log("Qt 界面已启用。")


def build_media_target_card(window, grid: QGridLayout, index: int, label: str, key: str, kind: str, edit, is_extra: bool, has_search: bool):
    card = QGroupBox(label)
    lay = QVBoxLayout(card)
    lay.setContentsMargins(4, 3, 4, 3)
    lay.setSpacing(2)
    lay.addWidget(edit)
    btn_wrap = QWidget()
    btn_wrap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    btn_wrap.setFixedHeight(26)
    btns = QHBoxLayout(btn_wrap)
    btns.setContentsMargins(0, 0, 0, 0)
    btns.setSpacing(2)
    if kind == "image":
        pick_btn = QPushButton("📁")
        pick_btn.setProperty("role", "mini")
        pick_btn.setToolTip("选择图片或视频")
        pick_btn.clicked.connect(lambda: window._pick_media_for_image_target(key, is_extra))
        url_btn = QPushButton("🌐")
        url_btn.setProperty("role", "mini")
        url_btn.setToolTip("下载图片或视频链接")
        url_btn.clicked.connect(lambda: window._open_media_url_for_image_target(key, is_extra))
        btns.addWidget(pick_btn)
        btns.addWidget(url_btn)
        if has_search:
            search_btn = QPushButton("🔍")
            search_btn.setProperty("role", "mini")
            search_btn.clicked.connect(lambda: window._open_search_dialog(key, is_extra))
            btns.addWidget(search_btn)
        crop_btn = QPushButton("✂")
        crop_btn.setProperty("role", "mini")
        crop_btn.setToolTip("编辑（自动识别图片/视频）")
        crop_btn.clicked.connect(lambda: window._open_media_editor_for_image_target(key, is_extra))
        btns.addWidget(crop_btn)
    else:
        pick_tip = "选择视频" if kind == "video" else "选择音频"
        pick_btn = QPushButton("📁")
        pick_btn.setProperty("role", "mini")
        pick_btn.setToolTip(pick_tip)
        pick_btn.clicked.connect(lambda: window._pick_file_for_target(key, kind))
        url_btn = QPushButton("🌐")
        url_btn.setProperty("role", "mini")
        url_btn.clicked.connect(lambda: window._open_url_dialog(key, kind=kind, is_extra=True))
        btns.addWidget(pick_btn)
        btns.addWidget(url_btn)
        if kind == "video":
            search_btn = QPushButton("🔍")
            search_btn.setProperty("role", "mini")
            search_btn.setToolTip("搜索并下载 YouTube 视频")
            search_btn.clicked.connect(lambda: window._open_video_search_dialog(key))
            btns.addWidget(search_btn)
            cut_btn = QPushButton("✂")
            cut_btn.setProperty("role", "mini")
            cut_btn.setToolTip("视频分段裁切")
            cut_btn.clicked.connect(lambda: window._open_video_editor_for_target(key))
            btns.addWidget(cut_btn)
    clear_btn = QPushButton("🗑")
    clear_btn.setProperty("role", "mini")
    clear_btn.setProperty("tone", "danger")
    clear_btn.setToolTip("清空")
    clear_btn.clicked.connect(lambda: edit.set_paths([]))
    btns.addWidget(clear_btn)
    btns.addStretch(1)
    lay.addWidget(btn_wrap)
    row = index // 2
    col = index % 2
    grid.addWidget(card, row, col)
    window._media_target_cards[key] = card
