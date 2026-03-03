import sys


def _install_qt_log_filter():
    """过滤 Windows 下 Qt 的已知字体噪音日志，避免控制台被刷屏。"""
    try:
        from PySide6.QtCore import qInstallMessageHandler
    except Exception:
        return

    def _handler(_mode, _context, message):
        msg = str(message or "")
        if "QFont::setPointSize: Point size <= 0" in msg:
            return
        if "qt.qpa.fonts: DirectWrite: CreateFontFaceFromHDC() failed" in msg:
            return
        try:
            sys.__stderr__.write(msg + "\n")
        except Exception:
            pass

    qInstallMessageHandler(_handler)


def main() -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except Exception as exc:
        print("缺少 PySide6，请先安装: pip install pyside6")
        print(f"导入错误: {exc}")
        return 1

    _install_qt_log_filter()

    from jellyfin_nfo_qt_window import JellyfinNfoQtWindow

    app = QApplication(sys.argv)
    # 兜底：确保应用字体字号有效，避免 pointSize=-1 触发重复警告。
    f = app.font()
    if f.pointSizeF() <= 0:
        f.setPointSizeF(10.0)
        app.setFont(f)
    win = JellyfinNfoQtWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
