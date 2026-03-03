from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import webview
except Exception as exc:  # pragma: no cover
    print(json.dumps({"ok": False, "error": f"pywebview unavailable: {exc}"}), flush=True)
    sys.exit(2)


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


def _inject_toolbar(window):
    js = r"""
    (function() {
      if (window.__cursorYtBarInstalled) return;
      window.__cursorYtBarInstalled = true;

      function mkBtn(text, bg) {
        const b = document.createElement('button');
        b.textContent = text;
        b.style.border = 'none';
        b.style.borderRadius = '8px';
        b.style.padding = '8px 12px';
        b.style.fontSize = '13px';
        b.style.cursor = 'pointer';
        b.style.color = '#fff';
        b.style.background = bg;
        return b;
      }

      function ensureBar() {
        if (document.getElementById('cursor-yt-confirm-bar')) return;
        const wrap = document.createElement('div');
        wrap.id = 'cursor-yt-confirm-bar';
        wrap.style.position = 'fixed';
        // 放在左上角并下移，避免遮挡 YouTube 右上角登录按钮
        wrap.style.top = '76px';
        wrap.style.left = '16px';
        wrap.style.zIndex = '2147483647';
        wrap.style.display = 'flex';
        wrap.style.gap = '8px';
        wrap.style.padding = '8px';
        wrap.style.borderRadius = '10px';
        wrap.style.background = 'rgba(17,24,39,0.78)';
        wrap.style.boxShadow = '0 6px 18px rgba(0,0,0,.3)';

        const ok = mkBtn('确认并继续', '#2563eb');
        const cancel = mkBtn('取消', '#6b7280');
        ok.onclick = function() {
          if (window.pywebview && window.pywebview.api) {
            window.pywebview.api.confirm_continue(window.location.href);
          }
        };
        cancel.onclick = function() {
          if (window.pywebview && window.pywebview.api) {
            window.pywebview.api.cancel_continue();
          }
        };
        wrap.appendChild(cancel);
        wrap.appendChild(ok);
        (document.documentElement || document.body).appendChild(wrap);
      }

      ensureBar();
      // 兼容 YouTube SPA 切页后节点被替换：定时自恢复按钮。
      window.setInterval(ensureBar, 1000);
    })();
    """
    try:
        window.evaluate_js(js)
    except Exception:
        pass


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
    window = webview.create_window(
        "YouTube 登录控制 (WebView2)",
        open_url,
        js_api=api,
        width=1200,
        height=860,
        resizable=True,
    )

    # 登录页输入时避免反复处理 loaded 事件导致卡顿，仅在启动时注入一次。
    def _on_loaded():
        _inject_toolbar(window)

    try:
        window.events.loaded += _on_loaded
    except Exception:
        pass

    def _on_start(_window):
        # 某些版本 pywebview 会向回调传入 window 参数，必须接收。
        _inject_toolbar(window)

    kwargs = {"gui": "edgechromium", "private_mode": False}
    if storage_path:
        kwargs["storage_path"] = storage_path
    webview.start(_on_start, window, **kwargs)

    print(json.dumps(api.result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
