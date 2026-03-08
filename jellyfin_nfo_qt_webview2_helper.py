from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

_wv2_extra = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
if "--disable-quic" not in _wv2_extra:
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--disable-quic {_wv2_extra}".strip()

try:
    import webview
except Exception as exc:  # pragma: no cover
    print(json.dumps({"ok": False, "error": f"pywebview unavailable: {exc}"}), flush=True)
    sys.exit(2)


# ── Win32 原生浮动工具栏 (仅 Windows) ────────────────────────────────
#
# 核心思路:
#   pywebview 的 _on_start 回调在后台线程运行, 而窗口属于主线程 (.NET
#   Application.Run).  SetWindowSubclass 不能跨线程使用, 所以之前的子类
#   回调从未触发.
#
#   新方案: 在专用后台线程上创建一个 WS_POPUP 弹出窗口 (owner = 主窗口),
#   所有控件都是这个弹出窗口的子窗口, 消息循环也在同一线程上运行.
#   用 WM_TIMER 每 60ms 同步位置/大小, 并把 WebView2 推到工具栏下方.

if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as _wt

    _u32 = ctypes.windll.user32
    _k32 = ctypes.windll.kernel32
    _gdi = ctypes.windll.gdi32
    _cc32 = ctypes.windll.comctl32

    _X64 = ctypes.sizeof(ctypes.c_void_p) == 8
    _LRESULT = ctypes.c_longlong if _X64 else ctypes.c_long
    _UINT_PTR = ctypes.c_ulonglong if _X64 else ctypes.c_ulong
    _DWORD_PTR = ctypes.c_ulonglong if _X64 else ctypes.c_ulong

    _WNDPROC_T = ctypes.WINFUNCTYPE(
        _LRESULT, _wt.HWND, _wt.UINT, _wt.WPARAM, _wt.LPARAM,
    )
    _SUBCLASSPROC = ctypes.WINFUNCTYPE(
        _LRESULT, _wt.HWND, _wt.UINT, _wt.WPARAM, _wt.LPARAM,
        _UINT_PTR, _DWORD_PTR,
    )
    _WNDENUMPROC = ctypes.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
    _HOOKPROC = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, _wt.WPARAM, _wt.LPARAM)

    class _CWPSTRUCT(ctypes.Structure):
        _fields_ = [
            ("lParam", _wt.LPARAM),
            ("wParam", _wt.WPARAM),
            ("message", _wt.UINT),
            ("hwnd", _wt.HWND),
        ]

    class _WINDOWPOS(ctypes.Structure):
        _fields_ = [
            ("hwnd", _wt.HWND),
            ("hwndInsertAfter", _wt.HWND),
            ("x", ctypes.c_int),
            ("y", ctypes.c_int),
            ("cx", ctypes.c_int),
            ("cy", ctypes.c_int),
            ("flags", _wt.UINT),
        ]

    class _WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", _wt.UINT),
            ("style", _wt.UINT),
            ("lpfnWndProc", _WNDPROC_T),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", _wt.HINSTANCE),
            ("hIcon", _wt.HANDLE),
            ("hCursor", _wt.HANDLE),
            ("hbrBackground", _wt.HANDLE),
            ("lpszMenuName", _wt.LPCWSTR),
            ("lpszClassName", _wt.LPCWSTR),
            ("hIconSm", _wt.HANDLE),
        ]

    # ── argtypes / restypes ──────────────────────────────────────
    _u32.FindWindowW.restype = _wt.HWND
    _u32.FindWindowW.argtypes = [_wt.LPCWSTR, _wt.LPCWSTR]
    _u32.CreateWindowExW.restype = _wt.HWND
    _u32.CreateWindowExW.argtypes = [
        _wt.DWORD, _wt.LPCWSTR, _wt.LPCWSTR, _wt.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        _wt.HWND, ctypes.c_void_p, _wt.HINSTANCE, ctypes.c_void_p,
    ]
    _u32.MoveWindow.argtypes = [
        _wt.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, _wt.BOOL,
    ]
    _u32.GetClientRect.argtypes = [_wt.HWND, ctypes.POINTER(_wt.RECT)]
    _u32.SetWindowTextW.argtypes = [_wt.HWND, _wt.LPCWSTR]
    _u32.GetWindowTextW.argtypes = [_wt.HWND, _wt.LPWSTR, ctypes.c_int]
    _u32.GetWindowTextLengthW.argtypes = [_wt.HWND]
    _u32.GetWindowTextLengthW.restype = ctypes.c_int
    _u32.SendMessageW.argtypes = [_wt.HWND, _wt.UINT, _wt.WPARAM, _wt.LPARAM]
    _u32.SendMessageW.restype = _LRESULT
    _u32.EnumChildWindows.argtypes = [_wt.HWND, _WNDENUMPROC, _wt.LPARAM]
    _u32.EnumWindows.argtypes = [_WNDENUMPROC, _wt.LPARAM]
    _u32.GetWindowRect.argtypes = [_wt.HWND, ctypes.POINTER(_wt.RECT)]
    _u32.GetParent.argtypes = [_wt.HWND]
    _u32.GetParent.restype = _wt.HWND
    _u32.IsWindowVisible.argtypes = [_wt.HWND]
    _u32.IsWindowVisible.restype = _wt.BOOL
    _u32.GetWindowThreadProcessId.argtypes = [_wt.HWND, ctypes.POINTER(_wt.DWORD)]
    _u32.GetWindowThreadProcessId.restype = _wt.DWORD
    _u32.RegisterClassExW.restype = _wt.ATOM
    _u32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
    _u32.DefWindowProcW.restype = _LRESULT
    _u32.DefWindowProcW.argtypes = [_wt.HWND, _wt.UINT, _wt.WPARAM, _wt.LPARAM]
    _u32.GetMessageW.restype = _wt.BOOL
    _u32.GetMessageW.argtypes = [ctypes.POINTER(_wt.MSG), _wt.HWND, _wt.UINT, _wt.UINT]
    _u32.TranslateMessage.restype = _wt.BOOL
    _u32.TranslateMessage.argtypes = [ctypes.POINTER(_wt.MSG)]
    _u32.DispatchMessageW.restype = _LRESULT
    _u32.DispatchMessageW.argtypes = [ctypes.POINTER(_wt.MSG)]
    _u32.SetTimer.restype = _UINT_PTR
    _u32.SetTimer.argtypes = [_wt.HWND, _UINT_PTR, _wt.UINT, ctypes.c_void_p]
    _u32.PostQuitMessage.argtypes = [ctypes.c_int]
    _u32.ClientToScreen.argtypes = [_wt.HWND, ctypes.POINTER(_wt.POINT)]
    _u32.ClientToScreen.restype = _wt.BOOL
    _u32.IsWindow.argtypes = [_wt.HWND]
    _u32.IsWindow.restype = _wt.BOOL
    _u32.IsIconic.argtypes = [_wt.HWND]
    _u32.IsIconic.restype = _wt.BOOL
    _u32.ShowWindow.argtypes = [_wt.HWND, ctypes.c_int]
    _u32.ShowWindow.restype = _wt.BOOL
    _u32.LoadCursorW.restype = _wt.HANDLE
    _u32.LoadCursorW.argtypes = [_wt.HINSTANCE, ctypes.c_void_p]
    _u32.DestroyWindow.argtypes = [_wt.HWND]
    _u32.DestroyWindow.restype = _wt.BOOL
    _u32.ScreenToClient.argtypes = [_wt.HWND, ctypes.POINTER(_wt.POINT)]
    _u32.ScreenToClient.restype = _wt.BOOL
    _u32.SetWindowsHookExW.restype = _wt.HANDLE
    _u32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, _wt.HINSTANCE, _wt.DWORD]
    _u32.CallNextHookEx.restype = _LRESULT
    _u32.CallNextHookEx.argtypes = [_wt.HANDLE, ctypes.c_int, _wt.WPARAM, _wt.LPARAM]
    _u32.UnhookWindowsHookEx.restype = _wt.BOOL
    _u32.UnhookWindowsHookEx.argtypes = [_wt.HANDLE]

    _cc32.SetWindowSubclass.restype = _wt.BOOL
    _cc32.SetWindowSubclass.argtypes = [
        _wt.HWND, _SUBCLASSPROC, _UINT_PTR, _DWORD_PTR,
    ]
    _cc32.DefSubclassProc.restype = _LRESULT
    _cc32.DefSubclassProc.argtypes = [_wt.HWND, _wt.UINT, _wt.WPARAM, _wt.LPARAM]

    _gdi.CreateFontW.restype = _wt.HFONT
    _gdi.CreateFontW.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        _wt.DWORD, _wt.DWORD, _wt.DWORD, _wt.DWORD, _wt.DWORD,
        _wt.DWORD, _wt.DWORD, _wt.DWORD, _wt.LPCWSTR,
    ]

    try:
        _u32.GetDpiForWindow.restype = _wt.UINT
        _u32.GetDpiForWindow.argtypes = [_wt.HWND]
        _HAS_DPI = True
    except Exception:
        _HAS_DPI = False

    # ── 常量 ──
    _WS_CHILD = 0x40000000
    _WS_VISIBLE = 0x10000000
    _WS_POPUP = 0x80000000
    _WS_CLIPCHILDREN = 0x02000000
    _WS_TABSTOP = 0x00010000
    _WS_EX_CLIENTEDGE = 0x00000200
    _WS_EX_TOOLWINDOW = 0x00000080
    _ES_AUTOHSCROLL = 0x0080
    _BS_PUSHBUTTON = 0x00000000
    _WM_DESTROY = 0x0002
    _WM_COMMAND = 0x0111
    _WM_TIMER = 0x0113
    _WM_SETFONT = 0x0030
    _WM_KEYDOWN = 0x0100
    _WM_SETFOCUS_MSG = 0x0007
    _WM_KILLFOCUS_MSG = 0x0008
    _WM_WINDOWPOSCHANGING = 0x0046
    _VK_RETURN = 0x0D
    _BN_CLICKED = 0
    _WH_CALLWNDPROC = 4
    _SWP_NOMOVE = 0x0002
    _SWP_NOSIZE = 0x0001

    _ID_ADDR = 3001
    _ID_GO = 3002
    _ID_OK = 3003
    _ID_CANCEL = 3004

    _BAR_H = 42
    _PAD = 6
    _BTN_H = 30
    _EDIT_H = 28
    _TIMER_ID = 1
    _TIMER_MS = 60

    _gc_prevent: list = []

    # ── 辅助函数 ──

    def _dpi_of(hwnd) -> int:
        if _HAS_DPI:
            try:
                d = _u32.GetDpiForWindow(hwnd)
                if d and d > 0:
                    return int(d)
            except Exception:
                pass
        return 96

    def _s(base: int, dpi: int) -> int:
        return max(1, int(base * dpi / 96))

    def _dbg(msg: str):
        try:
            sys.stderr.write(f"[toolbar] {msg}\n")
            sys.stderr.flush()
        except Exception:
            pass

    # ── 模块级 WndProc 与窗口类注册 ──

    _tb_inst: "_NativeToolbar | None" = None

    @_WNDPROC_T
    def _tb_wndproc(hwnd, msg, wp, lp):
        tb = _tb_inst
        if tb is not None:
            try:
                return tb._on_msg(hwnd, msg, wp, lp)
            except Exception as exc:
                _dbg(f"wndproc error: {exc}")
        return _u32.DefWindowProcW(hwnd, msg, wp, lp)

    _gc_prevent.append(_tb_wndproc)

    _tb_class_ok = False
    _TB_CLS = "PyWV2ToolbarHost"

    def _ensure_tb_class() -> bool:
        global _tb_class_ok
        if _tb_class_ok:
            return True
        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(wc)
        wc.style = 0x0003  # CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = _tb_wndproc
        wc.hInstance = _k32.GetModuleHandleW(None)
        wc.hCursor = _u32.LoadCursorW(None, 32512)  # IDC_ARROW
        wc.hbrBackground = 16  # (HBRUSH)(COLOR_BTNFACE + 1)
        wc.lpszClassName = _TB_CLS
        atom = _u32.RegisterClassExW(ctypes.byref(wc))
        if atom or _k32.GetLastError() == 1410:  # ERROR_CLASS_ALREADY_EXISTS
            _tb_class_ok = True
            return True
        _dbg(f"RegisterClassExW failed err={_k32.GetLastError()}")
        return False

    # ── _NativeToolbar ──

    class _NativeToolbar:
        def __init__(self, title: str, on_nav, on_ok, on_cancel):
            self._title = title
            self._on_nav = on_nav
            self._on_ok = on_ok
            self._on_cancel = on_cancel
            self._hwnd: int = 0
            self._wv: int = 0
            self._popup: int = 0
            self._edit: int = 0
            self._btn_go: int = 0
            self._btn_ok: int = 0
            self._btn_cancel: int = 0
            self._font: int = 0
            self._dpi: int = 96
            self._edit_focused = False
            self._pending_url = ""
            self._hook = None

        # ── public ──

        def install(self, timeout: float = 10.0) -> bool:
            t0 = time.time()
            self._hwnd = self._find_hwnd(timeout)
            if not self._hwnd:
                _dbg("HWND not found")
                return False
            _dbg(f"HWND={self._hwnd:#x} (took {time.time()-t0:.2f}s)")
            self._dpi = _dpi_of(self._hwnd)

            deadline = time.time() + max(1.0, timeout / 2)
            while time.time() < deadline:
                self._wv = self._find_wv_child()
                if self._wv:
                    break
                time.sleep(0.1)
            _dbg(f"WebView2 child={self._wv:#x}")

            ready = threading.Event()
            threading.Thread(
                target=self._toolbar_thread, args=(ready,), daemon=True,
            ).start()
            ready.wait(timeout=5.0)

            if not self._popup:
                _dbg("popup creation failed")
                return False
            _dbg(f"popup={self._popup:#x} edit={self._edit:#x}")
            return True

        def set_url(self, url: str):
            if self._edit:
                _u32.SetWindowTextW(_wt.HWND(self._edit), url or "")
            else:
                self._pending_url = url or ""

        def get_url(self) -> str:
            if not self._edit:
                return self._pending_url
            n = _u32.GetWindowTextLengthW(_wt.HWND(self._edit))
            if n <= 0:
                return ""
            buf = ctypes.create_unicode_buffer(n + 2)
            _u32.GetWindowTextW(_wt.HWND(self._edit), buf, n + 2)
            return buf.value or ""

        # ── toolbar thread ──

        def _toolbar_thread(self, ready: threading.Event):
            global _tb_inst
            _tb_inst = self
            try:
                if not _ensure_tb_class():
                    _dbg("class registration failed")
                    ready.set()
                    return
                self._create_popup()
                if not self._popup:
                    ready.set()
                    return
                self._create_controls()
                self._subclass_edit()
                self._layout()
                _u32.SetTimer(_wt.HWND(self._popup), _TIMER_ID, _TIMER_MS, None)
                _dbg("toolbar ready, entering message loop")
                ready.set()
                msg = _wt.MSG()
                while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    _u32.TranslateMessage(ctypes.byref(msg))
                    _u32.DispatchMessageW(ctypes.byref(msg))
                _dbg("message loop exited")
            except Exception as exc:
                _dbg(f"toolbar_thread error: {exc}")
                import traceback
                traceback.print_exc(file=sys.stderr)
                ready.set()

        # ── popup & controls ──

        def _create_popup(self):
            hi = _k32.GetModuleHandleW(None)
            h = _u32.CreateWindowExW(
                _WS_EX_TOOLWINDOW,
                _TB_CLS, "",
                _WS_POPUP | _WS_VISIBLE | _WS_CLIPCHILDREN,
                0, 0, 10, 10,
                _wt.HWND(self._hwnd),
                None, hi, None,
            )
            self._popup = int(h or 0)
            if not self._popup:
                _dbg(f"CreateWindowExW popup failed err={_k32.GetLastError()}")

        def _create_controls(self):
            if not self._popup:
                return
            hi = _k32.GetModuleHandleW(None)
            pp = _wt.HWND(self._popup)
            d = self._dpi
            fh = -_s(14, d)
            self._font = _gdi.CreateFontW(
                fh, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 4, 0, "Segoe UI",
            )
            self._edit = int(_u32.CreateWindowExW(
                _WS_EX_CLIENTEDGE, "EDIT", "",
                _WS_CHILD | _WS_VISIBLE | _WS_TABSTOP | _ES_AUTOHSCROLL,
                0, 0, 10, 10, pp, ctypes.c_void_p(_ID_ADDR), hi, None,
            ) or 0)
            self._btn_go = int(_u32.CreateWindowExW(
                0, "BUTTON", "\u8bbf\u95ee",
                _WS_CHILD | _WS_VISIBLE | _WS_TABSTOP | _BS_PUSHBUTTON,
                0, 0, 10, 10, pp, ctypes.c_void_p(_ID_GO), hi, None,
            ) or 0)
            self._btn_cancel = int(_u32.CreateWindowExW(
                0, "BUTTON", "\u53d6\u6d88",
                _WS_CHILD | _WS_VISIBLE | _WS_TABSTOP | _BS_PUSHBUTTON,
                0, 0, 10, 10, pp, ctypes.c_void_p(_ID_CANCEL), hi, None,
            ) or 0)
            self._btn_ok = int(_u32.CreateWindowExW(
                0, "BUTTON", "\u786e\u8ba4\u5e76\u7ee7\u7eed",
                _WS_CHILD | _WS_VISIBLE | _WS_TABSTOP | _BS_PUSHBUTTON,
                0, 0, 10, 10, pp, ctypes.c_void_p(_ID_OK), hi, None,
            ) or 0)
            _dbg(
                f"controls edit={self._edit:#x} go={self._btn_go:#x} "
                f"cancel={self._btn_cancel:#x} ok={self._btn_ok:#x}"
            )
            for h in (self._edit, self._btn_go, self._btn_ok, self._btn_cancel):
                if h:
                    _u32.SendMessageW(
                        _wt.HWND(h), _WM_SETFONT,
                        _wt.WPARAM(self._font or 0), _wt.LPARAM(1),
                    )
            if self._pending_url and self._edit:
                _u32.SetWindowTextW(_wt.HWND(self._edit), self._pending_url)
                self._pending_url = ""

        # ── subclass edit (同线程，SetWindowSubclass 可用) ──

        def _subclass_edit(self):
            if not self._edit:
                return
            tb = self

            @_SUBCLASSPROC
            def proc(hwnd, msg, wp, lp, uid, ref):
                if msg == _WM_KEYDOWN and int(wp) == _VK_RETURN:
                    tb._fire_nav()
                    return 0
                if msg == _WM_SETFOCUS_MSG:
                    tb._edit_focused = True
                if msg == _WM_KILLFOCUS_MSG:
                    tb._edit_focused = False
                return _cc32.DefSubclassProc(hwnd, msg, wp, lp)

            _gc_prevent.append(proc)
            _cc32.SetWindowSubclass(
                _wt.HWND(self._edit), proc, _UINT_PTR(2), _DWORD_PTR(0),
            )

        # ── WH_CALLWNDPROC 钩子: 拦截 .NET 对 WebView2 的位置重置 ──

        def _install_wv_hook(self):
            """在主线程上安装 WH_CALLWNDPROC 钩子，拦截 WM_WINDOWPOSCHANGING
            并强制 WebView2 始终留在工具栏下方，从根源消除抖动。"""
            if not self._wv or not self._hwnd:
                return
            gui_tid = _u32.GetWindowThreadProcessId(
                _wt.HWND(self._hwnd), None,
            )
            if not gui_tid:
                _dbg("failed to get GUI thread ID for hook")
                return
            tb = self

            @_HOOKPROC
            def hook(nCode, wParam, lParam):
                if nCode >= 0:
                    try:
                        cwp = ctypes.cast(lParam, ctypes.POINTER(_CWPSTRUCT))
                        h = int(cwp.contents.hwnd or 0)
                        if h == tb._wv and cwp.contents.message == _WM_WINDOWPOSCHANGING:
                            pos = ctypes.cast(
                                cwp.contents.lParam,
                                ctypes.POINTER(_WINDOWPOS),
                            )
                            flags = int(pos.contents.flags)
                            cr = _wt.RECT()
                            _u32.GetClientRect(
                                _wt.HWND(tb._hwnd), ctypes.byref(cr),
                            )
                            bar = _s(_BAR_H, tb._dpi)
                            if not (flags & _SWP_NOMOVE):
                                pos.contents.x = 0
                                pos.contents.y = bar
                            if not (flags & _SWP_NOSIZE):
                                pos.contents.cx = cr.right
                                pos.contents.cy = max(1, cr.bottom - bar)
                    except Exception:
                        pass
                return _u32.CallNextHookEx(None, nCode, wParam, lParam)

            _gc_prevent.append(hook)
            self._hook = _u32.SetWindowsHookExW(
                _WH_CALLWNDPROC, hook, None, gui_tid,
            )
            if self._hook:
                _dbg(f"WH_CALLWNDPROC hook installed on tid={gui_tid}")
            else:
                _dbg(f"SetWindowsHookExW failed err={_k32.GetLastError()}")

        # ── layout ──

        @staticmethod
        def _get_rect(hwnd_int: int) -> tuple[int, int, int, int]:
            r = _wt.RECT()
            _u32.GetWindowRect(_wt.HWND(hwnd_int), ctypes.byref(r))
            return (r.left, r.top, r.right - r.left, r.bottom - r.top)

        @staticmethod
        def _move_if_needed(hwnd_int: int, x: int, y: int, w: int, h: int):
            """只在窗口当前位置/大小与目标不同时才 MoveWindow"""
            cur = _NativeToolbar._get_rect(hwnd_int)
            if cur == (x, y, w, h):
                return
            _u32.MoveWindow(_wt.HWND(hwnd_int), x, y, w, h, True)

        def _child_pos(self, child_hwnd: int) -> tuple[int, int, int, int]:
            """返回子窗口相对于其父窗口客户区的 (x, y, w, h)"""
            r = _wt.RECT()
            _u32.GetWindowRect(_wt.HWND(child_hwnd), ctypes.byref(r))
            pt = _wt.POINT(r.left, r.top)
            parent = int(_u32.GetParent(_wt.HWND(child_hwnd)) or 0)
            if parent:
                _u32.ScreenToClient(_wt.HWND(parent), ctypes.byref(pt))
            return (pt.x, pt.y, r.right - r.left, r.bottom - r.top)

        def _move_child_if_needed(self, hwnd_int: int, x: int, y: int, w: int, h: int):
            if self._child_pos(hwnd_int) == (x, y, w, h):
                return
            _u32.MoveWindow(_wt.HWND(hwnd_int), x, y, w, h, True)

        def _layout(self):
            if not self._popup or not self._hwnd:
                return
            if not _u32.IsWindow(_wt.HWND(self._hwnd)):
                _u32.DestroyWindow(_wt.HWND(self._popup))
                return

            d = self._dpi
            bar = _s(_BAR_H, d)

            if _u32.IsIconic(_wt.HWND(self._hwnd)):
                if _u32.IsWindowVisible(_wt.HWND(self._popup)):
                    _u32.ShowWindow(_wt.HWND(self._popup), 0)
                return
            if not _u32.IsWindowVisible(_wt.HWND(self._popup)):
                _u32.ShowWindow(_wt.HWND(self._popup), 5)

            # 把工具栏定位在主窗口标题栏的正上方,
            # 完全不占用客户区 → WebView2 由 .NET 自然填满, 不抖动.
            wr = _wt.RECT()
            _u32.GetWindowRect(_wt.HWND(self._hwnd), ctypes.byref(wr))
            w = wr.right - wr.left

            self._move_if_needed(self._popup, wr.left, wr.top - bar, w, bar)

            pad = _s(_PAD, d)
            btn_h = _s(_BTN_H, d)
            edit_h = _s(_EDIT_H, d)
            go_w = _s(58, d)
            cancel_w = _s(58, d)
            ok_w = _s(100, d)
            btns_w = go_w + cancel_w + ok_w + pad * 3
            edit_w = max(_s(100, d), w - btns_w - pad * 2)
            y_e = (bar - edit_h) // 2
            y_b = (bar - btn_h) // 2
            x = pad
            if self._edit:
                self._move_child_if_needed(self._edit, x, y_e, edit_w, edit_h)
            x += edit_w + pad
            if self._btn_go:
                self._move_child_if_needed(self._btn_go, x, y_b, go_w, btn_h)
            x += go_w + pad
            if self._btn_cancel:
                self._move_child_if_needed(self._btn_cancel, x, y_b, cancel_w, btn_h)
            x += cancel_w + pad
            if self._btn_ok:
                self._move_child_if_needed(self._btn_ok, x, y_b, ok_w, btn_h)

        # ── WndProc 消息处理 ──

        def _on_msg(self, hwnd, msg, wp, lp):
            if msg == _WM_COMMAND:
                cid = int(wp) & 0xFFFF
                notif = (int(wp) >> 16) & 0xFFFF
                if notif == _BN_CLICKED:
                    if cid == _ID_GO:
                        self._fire_nav()
                        return 0
                    if cid == _ID_OK and self._on_ok:
                        threading.Thread(target=self._on_ok, daemon=True).start()
                        return 0
                    if cid == _ID_CANCEL and self._on_cancel:
                        threading.Thread(target=self._on_cancel, daemon=True).start()
                        return 0
            if msg == _WM_TIMER and int(wp) == _TIMER_ID:
                self._layout()
                return 0
            if msg == _WM_DESTROY:
                _u32.PostQuitMessage(0)
                return 0
            return _u32.DefWindowProcW(hwnd, msg, wp, lp)

        # ── navigation ──

        def _fire_nav(self):
            url = self.get_url().strip()
            if url and self._on_nav:
                self._edit_focused = False
                threading.Thread(
                    target=self._on_nav, args=(url,), daemon=True,
                ).start()

        # ── HWND 查找 ──

        def _find_hwnd(self, timeout: float) -> int:
            deadline = time.time() + timeout
            while time.time() < deadline:
                h = _u32.FindWindowW(None, self._title)
                if h:
                    return int(h)
                time.sleep(0.05)
            _dbg("FindWindowW failed, fallback EnumWindows+PID")
            pid = os.getpid()
            result: list[int] = [0]

            @_WNDENUMPROC
            def cb(h, _lp):
                proc_id = _wt.DWORD()
                _u32.GetWindowThreadProcessId(h, ctypes.byref(proc_id))
                if int(proc_id.value) == pid and _u32.IsWindowVisible(h):
                    n = _u32.GetWindowTextLengthW(h)
                    if n > 5:
                        result[0] = int(h)
                        return False
                return True

            _gc_prevent.append(cb)
            _u32.EnumWindows(cb, _wt.LPARAM(0))
            if result[0]:
                _dbg(f"EnumWindows found {result[0]:#x}")
            return result[0]

        def _find_wv_child(self) -> int:
            children: list[int] = []

            @_WNDENUMPROC
            def cb(h, _lp):
                children.append(int(h))
                return True

            _u32.EnumChildWindows(
                _wt.HWND(self._hwnd), cb, _wt.LPARAM(0),
            )
            best, best_a = 0, 0
            for c in children:
                parent = int(_u32.GetParent(_wt.HWND(c)))
                if parent != self._hwnd:
                    continue
                r = _wt.RECT()
                _u32.GetWindowRect(_wt.HWND(c), ctypes.byref(r))
                a = (r.right - r.left) * (r.bottom - r.top)
                if a > best_a:
                    best_a = a
                    best = c
            direct = len([
                c for c in children
                if int(_u32.GetParent(_wt.HWND(c))) == self._hwnd
            ])
            _dbg(f"direct children={direct}, total={len(children)}")
            return best

else:
    _NativeToolbar = None  # type: ignore[assignment,misc]


# ── pywebview Bridge API ────────────────────────────────────────────


class _BridgeApi:
    def __init__(self):
        self.result = {"ok": False, "url": ""}

    @staticmethod
    def _close_current_window():
        try:
            if webview.windows:
                webview.windows[0].destroy()
        except Exception:
            pass

    def confirm_continue(self, current_url: str = ""):
        self.result = {"ok": True, "url": str(current_url or "")}
        self._close_current_window()
        return True

    def cancel_continue(self):
        self.result = {"ok": False, "url": ""}
        self._close_current_window()
        return True


def _normalize_url(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", s):
        s = "https://" + s
    return s


# ── main ────────────────────────────────────────────────────────────


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "missing url"}), flush=True)
        return 1
    open_url = str(sys.argv[1]).strip()
    storage_path = ""
    if len(sys.argv) >= 3:
        storage_path = str(sys.argv[2]).strip()
        if storage_path:
            Path(storage_path).mkdir(parents=True, exist_ok=True)

    api = _BridgeApi()
    title = "YouTube \u767b\u5f55\u63a7\u5236 (WebView2)"

    window = webview.create_window(
        title,
        open_url,
        js_api=api,
        width=1100,
        height=780,
        resizable=True,
    )

    toolbar = None

    def _on_navigate(url: str):
        try:
            target = _normalize_url(url)
            if target:
                window.load_url(target)
        except Exception as exc:
            sys.stderr.write(f"[toolbar] navigate error: {exc}\n")
            sys.stderr.flush()

    def _on_confirm():
        try:
            cur = window.evaluate_js("window.location.href") or ""
        except Exception:
            cur = ""
        api.result = {"ok": True, "url": str(cur)}
        api._close_current_window()

    def _on_cancel():
        api.result = {"ok": False, "url": ""}
        api._close_current_window()

    def _url_sync():
        last_synced = ""
        while True:
            time.sleep(0.6)
            tb = toolbar
            if tb is None:
                continue
            try:
                if not webview.windows:
                    break
                if tb._edit_focused:
                    continue
                url = window.evaluate_js("window.location.href")
                if not url:
                    continue
                current_text = tb.get_url()
                if current_text != last_synced and current_text != url:
                    continue
                if url != last_synced:
                    tb.set_url(url)
                    last_synced = url
            except Exception:
                pass

    def _on_start(_win):
        nonlocal toolbar
        if _NativeToolbar is not None:
            tb = _NativeToolbar(title, _on_navigate, _on_confirm, _on_cancel)
            if tb.install(timeout=10.0):
                tb.set_url(open_url)
                toolbar = tb
            else:
                _dbg("install failed")
        threading.Thread(target=_url_sync, daemon=True).start()

    kwargs: dict = {"gui": "edgechromium", "private_mode": False}
    if storage_path:
        kwargs["storage_path"] = storage_path
    webview.start(_on_start, window, **kwargs)

    print(json.dumps(api.result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
