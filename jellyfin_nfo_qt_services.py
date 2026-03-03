from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import Request, urlopen

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from jellyfin_extras_rules import AUDIO_EXTS, VIDEO_EXTS
from jellyfin_nfo_core import NfoItem, parse_nfo_fields
from jellyfin_nfo_qt_image_search import ImageSearchDialog
from jellyfin_video_tools import ffmpeg_available
from season_renamer_ui import (
    append_history_batch,
    build_rename_ops,
    collect_video_files_from_input,
    execute_renames,
    group_by_season,
    validate_conflicts,
)

SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}

def _download_image_from_url(
    self,
    raw_url: str,
    target_name: str,
    silent: bool = False,
    timeout_sec: int = 40,
    max_bytes: int | None = None,
    show_dialog: bool = True,
) -> Path | None:
    url = raw_url.strip()
    if not url:
        if (not silent) and show_dialog:
            QMessageBox.warning(self, "提示", "链接为空。")
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        if (not silent) and show_dialog:
            QMessageBox.critical(self, "链接错误", "图片链接必须是 http 或 https。")
        return None
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=timeout_sec) as resp:
            if max_bytes is not None:
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    if (not silent) and show_dialog:
                        QMessageBox.critical(self, "下载失败", "图片体积超出限制。")
                    return None
            else:
                data = resp.read()
            content_type = (resp.headers.get("Content-Type") or "").lower()
    except Exception as exc:
        if (not silent) and show_dialog:
            QMessageBox.critical(self, "下载失败", f"无法下载图片：{exc}")
        return None
    ext = Path(parsed.path).suffix.lower()
    if ext not in SUPPORTED_IMAGE_EXTS:
        if "jpeg" in content_type or "jpg" in content_type:
            ext = ".jpg"
        elif "png" in content_type:
            ext = ".png"
        elif "webp" in content_type:
            ext = ".webp"
        elif "gif" in content_type:
            ext = ".gif"
        elif "bmp" in content_type:
            ext = ".bmp"
        elif "tiff" in content_type:
            ext = ".tiff"
        elif "avif" in content_type:
            ext = ".avif"
        else:
            if not silent:
                if not show_dialog:
                    return None
                QMessageBox.critical(self, "格式错误", "链接不是受支持图片格式。")
            return None
    cache_dir = Path(__file__).with_name(".nfo_image_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{target_name}_downloaded{ext}"
    target.write_bytes(data)
    return target

def _download_binary_from_url(self, raw_url: str, target_name: str, kind: str, show_dialog: bool = True) -> Path | None:
    url = raw_url.strip()
    if not url:
        if show_dialog:
            QMessageBox.warning(self, "提示", f"请先填写 {target_name} 链接。")
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        if show_dialog:
            QMessageBox.critical(self, "链接错误", "链接必须是 http 或 https。")
        return None
    if kind == "video":
        # 视频链接统一优先走 yt-dlp：支持 YouTube/Twitter(X) 等页面链接；
        # 若 yt-dlp 失败，再回退到直链下载，兼容直接 mp4/webm 地址。
        ytdlp_path = self._download_video_by_ytdlp(url, target_name)
        if ytdlp_path is not None:
            return ytdlp_path
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=60) as resp:
            data = resp.read()
            content_type = (resp.headers.get("Content-Type") or "").lower()
    except Exception as exc:
        if show_dialog:
            QMessageBox.critical(self, "下载失败", f"无法下载文件：{exc}")
        return None
    ext = Path(parsed.path).suffix.lower()
    if kind == "video":
        if ext not in VIDEO_EXTS:
            if "mp4" in content_type:
                ext = ".mp4"
            elif "webm" in content_type:
                ext = ".webm"
            elif "mpeg" in content_type:
                ext = ".mpeg"
            else:
                if show_dialog:
                    QMessageBox.critical(self, "格式错误", "链接不是受支持视频格式。")
                return None
        cache_dir = Path(__file__).with_name(".nfo_video_cache")
    else:
        if ext not in AUDIO_EXTS:
            ext = ext or ".bin"
        cache_dir = Path(__file__).with_name(".nfo_extra_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{target_name}_downloaded{ext}"
    target.write_bytes(data)
    return target

def _is_youtube_url(self, url: str) -> bool:
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    if host.startswith("www."):
        host = host[4:]
    return host in {"youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}


def _is_twitter_url(self, url: str) -> bool:
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    if host.startswith("www."):
        host = host[4:]
    return host in {"x.com", "twitter.com", "mobile.twitter.com"}


def _normalize_video_download_url(self, url: str) -> str:
    """对页面视频链接做轻量规范化，提升 yt-dlp 命中率。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    if not self._is_twitter_url(raw):
        return raw
    try:
        p = urlparse(raw)
        m = re.match(r"^/(?:i/web/)?([^/]+)/status/(\d+)(?:/.*)?$", p.path or "", flags=re.IGNORECASE)
        if not m:
            return raw
        user = m.group(1)
        sid = m.group(2)
        clean = p._replace(path=f"/{user}/status/{sid}", params="", query="", fragment="")
        return clean.geturl()
    except Exception:
        return raw


def _is_ytdlp_need_cookie_error(self, text: str) -> bool:
    t = (text or "").lower()
    return (
        ("sign in to confirm your age" in t)
        or ("this video may be inappropriate" in t)
        or ("cookies-from-browser" in t)
        or ("failed to decrypt with dpapi" in t)
        or ("could not find chromium cookies database" in t)
        or ("cookies database" in t and "chromium" in t)
        or ("authentication" in t and "cookie" in t)
    )


def _is_cookie_decrypt_error(self, text: str) -> bool:
    t = (text or "").lower()
    return (
        ("failed to decrypt with dpapi" in t)
        or ("decrypt" in t and "dpapi" in t)
        or ("cookies" in t and "decrypt" in t)
    )


def _is_twitter_need_cookie_error(self, text: str) -> bool:
    t = (text or "").lower()
    return (
        ("video #1 is unavailable" in t)
        or ("no video could be found in this tweet" in t)
        or ("downloading guest token" in t)
        or ("downloading graphql json" in t)
        or ("sensitive content" in t)
        or ("requires authentication" in t)
        or ("not authorized" in t)
        or ("http error 401" in t)
        or ("http error 403" in t)
        or (("login" in t or "sign in" in t) and "twitter" in t)
    )

def _chromium_cookie_profile_name(self) -> str:
    return "CursorYtDlpProfile"

def _chromium_user_data_dir(self) -> Path:
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app:
        return Path(local_app) / "Chromium" / "User Data"
    return Path(__file__).parent / ".chromium_user_data"

def _chromium_cookie_source(self) -> str:
    # 使用独立 Chromium 配置目录，避免与系统 Edge/Chrome 运行中的锁冲突。
    return f"chromium:{self._chromium_cookie_profile_name()}"

def _chromium_confirm_marker_prefix(self) -> str:
    return "https://cursor-yt-confirm.local/ok"

def _chromium_extension_dir(self) -> Path:
    return Path(__file__).with_name(".chromium_confirm_extension")

def _chromium_profile_dir(self) -> Path:
    return self._chromium_user_data_dir() / self._chromium_cookie_profile_name()

def _chromium_cookie_db_paths(self) -> list[Path]:
    profile_dir = self._chromium_profile_dir()
    return [
        profile_dir / "Cookies",
        profile_dir / "Network" / "Cookies",
    ]

def _clear_chromium_restore_session_files(self):
    profile_dir = self._chromium_profile_dir()
    targets = [
        profile_dir / "Last Session",
        profile_dir / "Last Tabs",
        profile_dir / "Current Session",
        profile_dir / "Current Tabs",
    ]
    sessions_dir = profile_dir / "Sessions"
    try:
        if sessions_dir.exists():
            for p in sessions_dir.glob("*"):
                if p.is_file():
                    try:
                        p.unlink()
                    except Exception:
                        pass
    except Exception as exc:
        self._log(f"[Chromium] 清理 Sessions 目录失败: {exc}")
    for p in targets:
        try:
            if p.exists() and p.is_file():
                p.unlink()
        except Exception:
            pass

def _wait_for_chromium_cookie_db(self, timeout_sec: float = 8.0) -> bool:
    end_at = time.time() + max(0.5, timeout_sec)
    while time.time() < end_at:
        for p in self._chromium_cookie_db_paths():
            if p.exists():
                return True
        time.sleep(0.25)
    return any(p.exists() for p in self._chromium_cookie_db_paths())


def _wait_for_chromium_cookie_db_copyable(self, timeout_sec: float = 10.0) -> bool:
    """等待 cookie 数据库可被 yt-dlp 复制（避免刚关闭 WebView2 时文件锁）。"""
    end_at = time.time() + max(0.5, timeout_sec)
    while time.time() < end_at:
        for p in self._chromium_cookie_db_paths():
            if not p.exists():
                continue
            probe = p.with_name(p.name + ".copy_probe")
            try:
                shutil.copy2(p, probe)
                try:
                    probe.unlink()
                except Exception:
                    pass
                return True
            except Exception:
                # 文件还在被占用，继续等待。
                pass
        time.sleep(0.25)
    return False

def _opened_youtube_watch_page_since(self, since_unix_ts: float) -> bool:
    profile_dir = self._chromium_profile_dir()
    history_db = profile_dir / "History"
    if not history_db.exists():
        return False
    # Chromium History uses WebKit timestamp: microseconds since 1601-01-01.
    since_webkit = int((since_unix_ts + 11644473600) * 1_000_000)
    sql = (
        "SELECT 1 FROM urls "
        "WHERE url LIKE 'https://www.youtube.com/watch%' "
        "AND last_visit_time >= ? "
        "LIMIT 1"
    )
    try:
        con = sqlite3.connect(str(history_db))
        try:
            row = con.execute(sql, (since_webkit,)).fetchone()
            return row is not None
        finally:
            con.close()
    except Exception as exc:
        self._log(f"[Chromium] 读取 History 失败: {exc}")
        return False

def _has_chromium_confirm_marker_since(self, since_unix_ts: float) -> bool:
    profile_dir = self._chromium_profile_dir()
    history_db = profile_dir / "History"
    if not history_db.exists():
        return False
    since_webkit = int((since_unix_ts + 11644473600) * 1_000_000)
    sql = (
        "SELECT 1 FROM urls "
        "WHERE url LIKE ? "
        "AND last_visit_time >= ? "
        "LIMIT 1"
    )
    try:
        con = sqlite3.connect(str(history_db))
        try:
            row = con.execute(sql, (f"{self._chromium_confirm_marker_prefix()}%", since_webkit)).fetchone()
            return row is not None
        finally:
            con.close()
    except Exception as exc:
        self._log(f"[Chromium] 读取确认标记失败: {exc}")
        return False

def _latest_confirmed_video_url_since(self, since_unix_ts: float) -> str:
    profile_dir = self._chromium_profile_dir()
    history_db = profile_dir / "History"
    if not history_db.exists():
        return ""
    since_webkit = int((since_unix_ts + 11644473600) * 1_000_000)
    sql = (
        "SELECT url FROM urls "
        "WHERE url LIKE ? "
        "AND last_visit_time >= ? "
        "ORDER BY last_visit_time DESC "
        "LIMIT 1"
    )
    try:
        con = sqlite3.connect(str(history_db))
        try:
            row = con.execute(sql, (f"{self._chromium_confirm_marker_prefix()}%", since_webkit)).fetchone()
        finally:
            con.close()
    except Exception as exc:
        self._log(f"[Chromium] 读取确认URL失败: {exc}")
        return ""
    if not row or not row[0]:
        return ""
    marker_url = str(row[0]).strip()
    try:
        q = parse_qs(urlparse(marker_url).query)
        raw_from = (q.get("from") or [""])[0]
        src = unquote(raw_from).strip()
    except Exception:
        src = ""
    if not self._is_youtube_url(src):
        return ""
    return src

def _close_chromium_process(self, proc: subprocess.Popen | None):
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    except Exception:
        pass

def _ytdlp_js_runtime_args(self) -> list[str]:
    # YouTube n challenge 需要可用 JS runtime；优先使用 Node.js。
    node_bin = shutil.which("node")
    if not node_bin:
        win_default = Path(r"C:\Program Files\nodejs\node.exe")
        if win_default.exists():
            node_bin = str(win_default)
    if node_bin:
        # yt-dlp --js-runtimes 只接受运行时名称，不接受绝对路径。
        return ["--js-runtimes", "node"]
    return []

def _ytdlp_impersonate_args(self) -> list[str]:
    # 借助 curl_cffi 的浏览器指纹，降低 YouTube 反爬挑战影响。
    return ["--impersonate", "chrome"]

def _ytdlp_cmd_prefix(self) -> list[str]:
    if self._cached_ytdlp_cmd_prefix is not None:
        return list(self._cached_ytdlp_cmd_prefix)
    # 优先使用 pip 安装的 yt-dlp（通过 Python 模块调用），
    # 因为独立 exe 的 yt-dlp 无法使用系统 Python 的 yt_dlp_ejs 插件来解 n challenge。
    try:
        rc = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True, text=True, check=False,
        )
        if rc.returncode == 0 and (rc.stdout or "").strip():
            self._log(f"[yt-dlp] 使用 pip 模块版本: {(rc.stdout or '').strip()}")
            self._cached_ytdlp_cmd_prefix = [sys.executable, "-m", "yt_dlp"]
            return list(self._cached_ytdlp_cmd_prefix)
    except Exception:
        pass
    ytdlp_bin = shutil.which("yt-dlp")
    if ytdlp_bin:
        self._cached_ytdlp_cmd_prefix = [ytdlp_bin]
        return list(self._cached_ytdlp_cmd_prefix)
    return []

def _ytdlp_subprocess_env(self) -> dict[str, str]:
    env = dict(os.environ)
    node_bin = shutil.which("node")
    if not node_bin:
        win_default = Path(r"C:\Program Files\nodejs\node.exe")
        if win_default.exists():
            node_bin = str(win_default)
    if node_bin:
        node_dir = str(Path(node_bin).parent)
        path_val = env.get("PATH", "")
        if node_dir.lower() not in path_val.lower():
            env["PATH"] = f"{node_dir}{os.pathsep}{path_val}" if path_val else node_dir
    return env

def _pick_best_ytdlp_format(self, cmd_prefix: list[str], url: str, cookie_source: str | None = None) -> str:
    if not cmd_prefix:
        return ""

    def _is_none(v):
        return (str(v or "").strip().lower() in {"", "none"})

    def _score_video(f):
        return (
            int(f.get("height") or 0),
            float(f.get("fps") or 0.0),
            float(f.get("tbr") or 0.0),
            float(f.get("vbr") or 0.0),
        )

    def _score_audio(f):
        return (
            float(f.get("abr") or 0.0),
            float(f.get("tbr") or 0.0),
        )

    def _score_muxed(f):
        return (
            int(f.get("height") or 0),
            float(f.get("fps") or 0.0),
            float(f.get("tbr") or 0.0),
            float(f.get("abr") or 0.0),
        )

    def _query_formats(clients: str | None, use_cookie: bool) -> tuple[list[dict], bool, str]:
        """返回 (formats_list, is_age_restricted, stderr_text)"""
        cmd = [
            *cmd_prefix,
            "--no-playlist",
            "--skip-download",
            "-J",
        ]
        if clients:
            cmd.extend(["--extractor-args", f"youtube:player_client={clients}"])
        cmd.extend(self._ytdlp_js_runtime_args())
        cmd.extend(self._ytdlp_impersonate_args())
        if use_cookie and cookie_source:
            cmd.extend(["--cookies-from-browser", cookie_source])
        cmd.append(url)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=self._ytdlp_subprocess_env())
        except Exception as exc:
            self._log(f"[yt-dlp] 获取格式列表失败: {exc}")
            return ([], False, "")
        stderr_text = (proc.stderr or "").strip()
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            err = (stderr_text or proc.stdout or "").strip()
            if err:
                self._log(f"[yt-dlp] 获取格式列表失败 (clients={clients}): {err[:220]}")
            return ([], False, stderr_text)
        try:
            data = json.loads(proc.stdout)
        except Exception:
            self._log("[yt-dlp] 解析格式列表失败，回退默认格式策略。")
            return ([], False, stderr_text)
        fmts = data.get("formats")
        if not isinstance(fmts, list):
            fmts = []
        age_restricted = (int(data.get("age_limit") or 0) >= 18)
        stderr_low = stderr_text.lower()
        if "age-restricted" in stderr_low or "sign in to confirm your age" in stderr_low:
            age_restricted = True
        return (fmts, age_restricted, stderr_text)

    def _select_best(formats: list[dict]) -> tuple[str, int]:
        """从格式列表中选最高质量，返回 (format_spec, best_height)"""
        video_only = [f for f in formats if (not _is_none(f.get("vcodec"))) and _is_none(f.get("acodec")) and f.get("format_id")]
        audio_only = [f for f in formats if _is_none(f.get("vcodec")) and (not _is_none(f.get("acodec"))) and f.get("format_id")]
        muxed = [f for f in formats if (not _is_none(f.get("vcodec"))) and (not _is_none(f.get("acodec"))) and f.get("format_id")]

        best_video = max(video_only, key=_score_video, default=None)
        best_audio = max(audio_only, key=_score_audio, default=None)
        if best_video is not None and best_audio is not None:
            vf = str(best_video.get("format_id"))
            af = str(best_audio.get("format_id"))
            h = int(best_video.get("height") or 0)
            return (f"{vf}+{af}", h)

        best_muxed = max(muxed, key=_score_muxed, default=None)
        if best_muxed is not None:
            picked = str(best_muxed.get("format_id") or "")
            h = int(best_muxed.get("height") or 0)
            if picked:
                return (picked, h)
        return ("", 0)

    # 不指定 player_client，让 yt-dlp 使用默认客户端组合
    # (当前为 tv_downgraded,web_safari) 以获取最佳兼容性。
    formats, age_restricted, stderr = _query_formats(None, use_cookie=True)
    picked, best_h = _select_best(formats)

    if age_restricted:
        self._last_ytdlp_age_restricted = True
        self._log("[yt-dlp] 检测到此视频有年龄限制（age-restricted）。")
        if best_h <= 360:
            self._log(
                "[yt-dlp] ⚠ 年龄限制视频需要登录 YouTube 才能获取高画质。"
            )

    if picked:
        self._log(f"[yt-dlp] 自动选定格式: {picked} ({best_h}p)")
    return picked

def _download_video_by_ytdlp(
    self,
    url: str,
    target_name: str,
    cookie_source: str | None = None,
    allow_cookie_decrypt_fallback: bool = True,
) -> Path | None:
    self._last_ytdlp_error = ""
    self._last_ytdlp_need_cookie = False
    self._last_ytdlp_age_restricted = False
    cmd_prefix = self._ytdlp_cmd_prefix()
    if not cmd_prefix:
        self._last_ytdlp_error = "未找到 yt-dlp，请先安装并加入 PATH。"
        self._log(f"[yt-dlp] {self._last_ytdlp_error}")
        return None
    cache_dir = Path(__file__).with_name(".nfo_video_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_tpl = str((cache_dir / f"{target_name}_ytdlp_{ts}.%(ext)s").resolve())
    has_ffmpeg = ffmpeg_available()
    if not has_ffmpeg:
        self._log("[yt-dlp] 未检测到 ffmpeg：最高画质可能不可用（常见回退到 360p/720p 单文件流）。")
    base_cmd = [
        *cmd_prefix,
        "--no-playlist",
        "--print",
        "after_move:filepath",
        "--quiet",
        "--force-overwrites",
        "--retries",
        "8",
        "--fragment-retries",
        "8",
        "--concurrent-fragments",
        "8",
        "--throttled-rate",
        "250K",
        "-o",
        out_tpl,
    ]
    aria2_bin = shutil.which("aria2c")
    if aria2_bin:
        # aria2 并发连接通常能显著改善 YouTube 慢速链路。
        base_cmd.extend([
            "--downloader",
            "aria2c",
            "--downloader-args",
            "aria2c:-x16 -s16 -k1M --file-allocation=none",
        ])
        self._log("[yt-dlp] 检测到 aria2c，启用并发下载加速。")
    else:
        # 内置下载器也启用分块请求，尽量缓解限速。
        base_cmd.extend(["--http-chunk-size", "10M"])
    if has_ffmpeg:
        # 下载后自动合并/封装到 mp4
        base_cmd.extend(["--merge-output-format", "mp4", "--remux-video", "mp4"])
    base_cmd.extend(self._ytdlp_js_runtime_args())
    base_cmd.extend(self._ytdlp_impersonate_args())
    norm_url = self._normalize_video_download_url(url)
    if norm_url and norm_url != url:
        self._log(f"[yt-dlp] 链接规范化: {url} -> {norm_url}")
    use_url = norm_url or url
    self._log(f"[yt-dlp] 开始下载: {use_url}")
    def _resolve_output_path(stdout_text: str) -> Path | None:
        lines = [x.strip() for x in (stdout_text or "").splitlines() if x.strip()]
        if lines:
            p = Path(lines[-1])
            if p.exists():
                return p.resolve()
        candidates = sorted(
            list(cache_dir.glob(f"{target_name}_ytdlp_*.*")) + list(cache_dir.glob(f"{target_name}_ytdlp.*")),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
        for one in candidates:
            if one.is_file():
                return one.resolve()
        return None

    def _run(one_cmd: list[str]) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                one_cmd,
                capture_output=True,
                text=True,
                check=False,
                env=self._ytdlp_subprocess_env(),
                timeout=900,
            )
            return (proc.returncode, proc.stdout or "", proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            err = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            return (-2, out, (err + "\nyt-dlp 下载超时(900s)，已中止。").strip())
        except Exception as exc:
            return (-1, "", str(exc))

    auto_fmt = ""
    if self._is_youtube_url(use_url):
        auto_fmt = self._pick_best_ytdlp_format(cmd_prefix, use_url, cookie_source=cookie_source).strip()

    def _build_cmd(cookie: str | None, fmt: str) -> list[str]:
        one = list(base_cmd)
        if fmt.strip():
            one.extend(["-f", fmt])
        if cookie:
            one.extend(["--cookies-from-browser", cookie])
        one.append(use_url)
        return one

    def _is_benign_success_warning(text: str) -> bool:
        low = (text or "").lower()
        if ("failed to extract initial attestation from the webpage" in low):
            return True
        if ("failed to download m3u8 information" in low) and ("connection timed out" in low):
            return True
        return False
    # 年龄限制视频没有 video+audio 分离格式时（只有 muxed 低画质如 360p），
    # 直接返回失败并触发登录重试流程，不浪费时间下载低画质。
    if self._last_ytdlp_age_restricted and "+" not in (auto_fmt or "x"):
        self._last_ytdlp_need_cookie = True
        self._last_ytdlp_error = (
            "此视频有年龄限制（age-restricted），当前仅能获取低画质（360p）。\n"
            "请在 Chromium 窗口中登录 YouTube 账号后重试，即可获取高画质。"
        )
        self._log(f"[yt-dlp] {self._last_ytdlp_error}")
        return None

    format_candidates: list[str] = []
    if self._is_twitter_url(use_url):
        # X/Twitter 只做一次默认尝试；失败直接按“需登录”路径处理。
        format_candidates = [""]
    elif self._is_youtube_url(use_url):
        # YouTube 只做一次：优先自动选定格式；无自动格式则默认自动。
        format_candidates = [auto_fmt] if auto_fmt else [""]
    else:
        if auto_fmt:
            format_candidates.append(auto_fmt)
        if has_ffmpeg:
            format_candidates.extend([
                "bestvideo*+bestaudio/best",
                "bv*+ba/b",
            ])
        format_candidates.extend(["", "best", "b"])
    seen: set[str] = set()
    format_candidates = [f for f in format_candidates if (f not in seen and not seen.add(f))]
    last_err = ""
    for idx, fmt in enumerate(format_candidates):
        if idx == 0 and fmt.strip():
            self._log(f"[yt-dlp] 先尝试自动选定格式: {fmt}")
        elif idx == 0:
            self._log("[yt-dlp] 先尝试默认自动格式。")
        else:
            self._log(f"[yt-dlp] 尝试格式回退: {fmt or 'auto'}")
        rc, out, err = _run(_build_cmd(cookie_source, fmt))
        brief = (err or out or "").strip()
        if rc != 0:
            self._log(f"[yt-dlp] 本次尝试返回码: {rc}")
            if brief:
                self._log(f"[yt-dlp] 本次尝试输出: {brief[:500]}")
        elif brief and (not _is_benign_success_warning(brief)):
            self._log(f"[yt-dlp] 本次尝试提示: {brief[:500]}")
        if rc == 0:
            out_path = _resolve_output_path(out)
            if out_path is not None:
                self._log(f"[yt-dlp] 下载完成: {out_path}")
                return out_path
            last_err = "yt-dlp 返回成功，但未找到输出文件。"
            continue
        one_err = (err or out or "").strip()
        if one_err:
            last_err = one_err

    # 对年龄限制/需登录的视频：不读取本机浏览器 cookies，交给专用 Chromium 登录流程处理。
    err_text = (last_err or "").strip()
    err_low = err_text.lower()
    if ("ffmpeg" in err_low) and ("not found" in err_low or "not installed" in err_low):
        err_text = (
            f"{err_text}\n"
            "未检测到 ffmpeg。根据 yt-dlp FAQ，YouTube 高画质通常是分离音视频流，"
            "需要 ffmpeg 才能合并为高画质成品。"
        ).strip()
    if ("n challenge solving failed" in err_low) and (not shutil.which("node")):
        err_text = (
            f"{err_text}\n"
            "检测到 YouTube n challenge，且未找到 Node.js。请安装 Node.js(LTS) 并确保 node 在 PATH，"
            "同时更新 yt-dlp 到最新版本后重试。"
        ).strip()

    # 某些环境 cookies-from-browser 会触发 DPAPI 解密错误；自动退回“无 cookies”再试一次。
    if allow_cookie_decrypt_fallback and cookie_source and self._is_cookie_decrypt_error(err_text):
        self._log("[yt-dlp] 检测到 Cookie 解密失败(DPAPI)，自动回退为无 Cookie 重试。")
        return self._download_video_by_ytdlp(
            use_url,
            target_name,
            cookie_source=None,
            allow_cookie_decrypt_fallback=False,
        )

    need_cookie_retry = self._is_ytdlp_need_cookie_error(err_text)
    if (not need_cookie_retry) and self._is_twitter_url(use_url):
        # X/Twitter 常见“Video unavailable”也可能是未登录导致，允许走一次 WebView2 登录重试。
        need_cookie_retry = self._is_twitter_need_cookie_error(err_text)
    self._last_ytdlp_need_cookie = need_cookie_retry

    self._last_ytdlp_error = (err_text or "未知错误")[:800]
    self._log(f"[yt-dlp] 下载失败: {self._last_ytdlp_error}")
    return None

def _ensure_chromium_browser(self) -> tuple[str, str] | None:
    project_dir = Path(__file__).parent
    # 优先使用本地路径下的 Chromium（便携放置）。
    local_candidates = [
        project_dir / ".chromium" / "chrome.exe",
        project_dir / ".chromium" / "chrome-win" / "chrome.exe",
        project_dir / ".chromium" / "chrome-linux" / "chrome",
        project_dir / ".chromium" / "chrome-mac" / "Chromium.app" / "Contents" / "MacOS" / "Chromium",
    ]
    for p in local_candidates:
        if p.exists():
            return (str(p), self._chromium_cookie_source())

    # 复用已安装的 Playwright Chromium，避免每次重复安装。
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app:
        pw_root = Path(local_app) / "ms-playwright"
        if pw_root.exists():
            candidates = sorted(
                [p for p in pw_root.glob("chromium-*/*/chrome.exe") if p.is_file()],
                key=lambda x: x.stat().st_mtime,
                reverse=True,
            )
            if candidates:
                picked = str(candidates[0])
                self._log(f"[Chromium] 复用已安装 Playwright Chromium: {picked}")
                return (picked, self._chromium_cookie_source())

    # 系统里已有 chromium 时直接使用。
    for exe_name in ("chromium", "chromium-browser"):
        p = shutil.which(exe_name)
        if p:
            return (p, self._chromium_cookie_source())

    self._log("[Chromium] 未找到可执行文件，开始自动安装 Playwright Chromium...")

    def _run_cmd(cmd: list[str]) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            return (proc.returncode, proc.stdout or "", proc.stderr or "")
        except Exception as exc:
            return (-1, "", str(exc))

    rc1, _o1, e1 = _run_cmd([sys.executable, "-m", "pip", "install", "-U", "playwright"])
    if rc1 != 0:
        self._log(f"[Chromium] 安装 playwright 失败: {(e1 or 'unknown')[:240]}")
        return None
    rc2, _o2, e2 = _run_cmd([sys.executable, "-m", "playwright", "install", "chromium"])
    if rc2 != 0:
        self._log(f"[Chromium] 下载 chromium 失败: {(e2 or 'unknown')[:240]}")
        return None
    rc3, out3, e3 = _run_cmd(
        [
            sys.executable,
            "-c",
            "from playwright.sync_api import sync_playwright\n"
            "p=sync_playwright().start()\n"
            "print(p.chromium.executable_path)\n"
            "p.stop()\n",
        ]
    )
    if rc3 != 0:
        self._log(f"[Chromium] 获取 chromium 路径失败: {(e3 or 'unknown')[:240]}")
        return None
    exe = (out3 or "").strip().splitlines()[-1].strip() if (out3 or "").strip() else ""
    if not exe or not Path(exe).exists():
        self._log("[Chromium] 自动安装完成但未找到可执行路径。")
        return None
    self._log(f"[Chromium] 自动安装成功: {exe}")
    return (exe, self._chromium_cookie_source())

def _prompt_chromium_login_cookie(self, open_url: str, message: str) -> str | None:
    found = self._ensure_chromium_browser()
    if found is None:
        self._log("[yt-dlp] 未准备好 Chromium，无法执行登录重试。")
        return None
    browser_path, cookie_source = found
    user_data_dir = self._chromium_user_data_dir()
    user_data_dir.mkdir(parents=True, exist_ok=True)
    args = [
        browser_path,
        "--new-window",
        f"--user-data-dir={str(user_data_dir)}",
        f"--profile-directory={self._chromium_cookie_profile_name()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-features=SigninIntercept,SignInPromo,Sync",
        open_url,
    ]
    proc = None
    try:
        proc = subprocess.Popen(args)
    except Exception as exc:
        self._log(f"[yt-dlp] 启动浏览器登录失败: {exc}")
        return None
    msg_box = QMessageBox(self)
    msg_box.setIcon(QMessageBox.Icon.Question)
    msg_box.setWindowTitle("需要登录 YouTube")
    msg_box.setText(message)
    continue_btn = msg_box.addButton("继续", QMessageBox.ButtonRole.AcceptRole)
    msg_box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
    msg_box.exec()
    if msg_box.clickedButton() is not continue_btn:
        self._log("[yt-dlp] 用户取消登录重试。")
        return None
    self._log("[yt-dlp] 用户点击应用，开始关闭 Chromium 并等待 Cookie 落盘。")
    self._close_chromium_process(proc)
    if not self._wait_for_chromium_cookie_db(timeout_sec=10.0):
        self._log("[yt-dlp] 未检测到 Chromium Cookies 数据库，稍后重试。")
        QMessageBox.warning(self, "Cookie 未就绪", "未检测到 Chromium cookie 数据库，请确认已登录后重试。")
        return None
    return cookie_source

def _retry_ytdlp_with_browser_login(self, video_url: str, target_key: str) -> Path | None:
    cookie_source = self._prompt_chromium_login_cookie(
        video_url,
        "请在已打开的 Chromium 窗口完成登录。\n登录完成后点击“应用”继续下载（将自动使用 Chromium Cookies）。",
    )
    if not cookie_source:
        return None
    return self._download_video_by_ytdlp(video_url, target_key, cookie_source=cookie_source)

def _prompt_chromium_login_for_search(self, keyword: str) -> str | None:
    search_url = f"https://www.youtube.com/results?search_query={quote_plus(keyword)}"
    return self._prompt_chromium_login_cookie(
        search_url,
        "搜索需要登录。请在已打开的 WebView2 窗口完成登录。\n登录完成后点击“确认并继续”（将自动使用登录 Cookies）。",
    )

def _prompt_chromium_login_cookie_async(self, open_url: str, message: str, on_done, require_watch_page: bool = False):
    self._last_confirmed_video_url = ""
    cookie_source = self._chromium_cookie_source()
    helper = Path(__file__).with_name("jellyfin_nfo_qt_webview2_helper.py")
    if not helper.exists():
        QMessageBox.warning(self, "缺少组件", f"未找到 WebView2 helper：{helper.name}")
        on_done(None)
        return
    profile_dir = self._chromium_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    self._log("[WebView2] 启动集成确认按钮的登录窗口。")

    def _job():
        cmd = [sys.executable, str(helper), open_url, str(profile_dir)]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            return ("error", (err or out or "WebView2 helper 启动失败").strip())
        last = ""
        if out:
            last = out.splitlines()[-1].strip()
        if not last:
            return ("cancel", "")
        try:
            payload = json.loads(last)
        except Exception:
            return ("error", f"WebView2 helper 输出无法解析: {last[:240]}")
        ok = bool(payload.get("ok"))
        confirmed = str(payload.get("url") or "").strip()
        if not ok:
            return ("cancel", "")
        return ("ok", confirmed)

    def _done(result_obj, err):
        if err:
            self._log(f"[WebView2] helper 异常: {err}")
            QMessageBox.warning(self, "WebView2 异常", str(err))
            on_done(None)
            return
        status = ""
        confirmed_url = ""
        if isinstance(result_obj, tuple) and len(result_obj) >= 2:
            status = str(result_obj[0] or "")
            confirmed_url = str(result_obj[1] or "")
        if status == "cancel":
            self._log("[WebView2] 用户取消登录流程。")
            on_done(None)
            return
        if status == "error":
            self._log(f"[WebView2] helper 失败: {confirmed_url}")
            QMessageBox.warning(self, "WebView2 失败", confirmed_url or "无法启动 WebView2 登录窗口。")
            on_done(None)
            return
        if not confirmed_url:
            confirmed_url = open_url
        is_watch = ("/watch" in confirmed_url) or ("youtu.be/" in confirmed_url)
        if require_watch_page and (not is_watch):
            QMessageBox.warning(self, "未打开视频页", "请在 WebView2 中点进一个具体视频页面后，再点“确认并继续”。")
            on_done(None)
            return
        if self._is_youtube_url(confirmed_url):
            self._last_confirmed_video_url = confirmed_url
        # WebView2 窗口关闭后，等待 cookies 数据库可复制，避免 yt-dlp 立刻读取失败。
        if not self._wait_for_chromium_cookie_db(timeout_sec=10.0):
            self._log("[WebView2] 未检测到 cookies 数据库。")
            QMessageBox.warning(self, "Cookie 未就绪", "未检测到登录 cookies 数据库，请重试。")
            on_done(None)
            return
        if not self._wait_for_chromium_cookie_db_copyable(timeout_sec=10.0):
            self._log("[WebView2] cookies 数据库仍被占用，无法读取。")
            QMessageBox.warning(self, "Cookie 被占用", "登录完成后 cookies 仍被占用，请稍后重试。")
            on_done(None)
            return
        on_done(cookie_source)

    self._run_async(_job, _done)

def _prompt_chromium_login_for_search_async(self, keyword: str, on_done):
    search_url = f"https://www.youtube.com/results?search_query={quote_plus(keyword)}"
    self._prompt_chromium_login_cookie_async(
        search_url,
        "搜索需要登录。请在已打开的 WebView2 窗口完成登录。\n登录完成后点击“确认并继续”（将自动使用登录 Cookies）。",
        on_done,
        require_watch_page=True,
    )

def _search_youtube_candidates(self, keyword: str, limit: int = 20, cookie_source: str | None = None) -> list[dict[str, str]]:
    cmd_prefix = self._ytdlp_cmd_prefix()
    if not cmd_prefix:
        raise RuntimeError("未找到 yt-dlp，请先安装并加入 PATH。")
    query = f"ytsearch{max(1, min(50, limit))}:{keyword}"
    cmd = [
        *cmd_prefix,
        query,
        "--skip-download",
        "--flat-playlist",
        "--ignore-errors",
        "--no-abort-on-error",
        "--dump-json",
        "--quiet",
        "--no-warnings",
        "--no-playlist",
    ]
    if cookie_source:
        cmd.extend(["--cookies-from-browser", cookie_source])
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        raise RuntimeError((proc.stderr or proc.stdout or "未知错误").strip()[:800])
    result: list[dict[str, str]] = []
    for line in (proc.stdout or "").splitlines():
        one = line.strip()
        if not one:
            continue
        try:
            item = json.loads(one)
        except Exception:
            continue
        url = str(item.get("webpage_url") or "").strip()
        if not self._is_youtube_url(url):
            continue
        title = str(item.get("title") or "").strip() or "未命名视频"
        dur = str(item.get("duration_string") or "").strip()
        if not dur:
            duration = item.get("duration")
            if isinstance(duration, (int, float)) and duration > 0:
                sec = int(duration)
                h = sec // 3600
                m = (sec % 3600) // 60
                s = sec % 60
                dur = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
            else:
                dur = "--:--"
        result.append({"title": title, "url": url, "duration": dur})
    return result


def _resolve_search_keyword(self) -> str:
    """统一使用一级 NFO 的 title；为空时回退一级节点名称。"""
    if not hasattr(self, "_selected_items"):
        return ""
    selected = self._selected_items()
    if not selected:
        return ""
    base_item = selected[0]
    try:
        all_items = getattr(self, "items", [])
        parent_of = getattr(self, "_tree_parent_of", {})
        idx_map = {str(one.path).casefold(): i for i, one in enumerate(all_items)}
        cur_idx = idx_map.get(str(base_item.path).casefold())
        while isinstance(cur_idx, int):
            p_idx = parent_of.get(cur_idx)
            if not isinstance(p_idx, int):
                break
            cur_idx = p_idx
        if isinstance(cur_idx, int) and 0 <= cur_idx < len(all_items):
            base_item = all_items[cur_idx]
    except Exception:
        pass

    try:
        fields = parse_nfo_fields(base_item.path)
        top_title = str(fields.get("title", "") or "").strip()
        if top_title:
            return top_title
    except Exception:
        pass

    return (base_item.path.parent.name.strip() or base_item.path.stem.strip() or "").strip()


def _open_video_search_dialog(self, target_key: str):
    keyword = self._resolve_search_keyword()
    if not keyword:
        QMessageBox.warning(self, "提示", "当前标题为空，无法自动生成搜索关键词。")
        return
    cookie_ref: dict[str, str] = {"value": ""}

    def _retry_download_after_login(video_url: str, tgt_key: str, new_cookie: str | None):
        if not new_cookie:
            self._log("[yt-dlp] 登录取消或未获取到 cookie。")
            return
        cookie_ref["value"] = new_cookie
        self._log(f"[yt-dlp] 登录后重试下载: {video_url}")

        def _dl():
            return self._download_video_by_ytdlp(video_url, tgt_key, cookie_source=new_cookie)

        def _done(path_obj, err):
            path = path_obj if isinstance(path_obj, Path) else None
            if err:
                self._log(f"[yt-dlp] 登录后下载异常: {err}")
            if path is None:
                self._log("[yt-dlp] 登录后下载失败。")
                return
            edit = self.extra_video_source_edits[tgt_key]
            if self._target_supports_multi(tgt_key):
                edit.append_path(str(path))
            else:
                edit.set_paths([str(path)])

        self._run_async(_dl, _done)

    def _start_search_with_cookie(cookie_source: str):
        if not cookie_source:
            return
        cookie_ref["value"] = cookie_source
        confirmed_video_url = self._last_confirmed_video_url.strip()
        if self._is_youtube_url(confirmed_video_url) and (
            ("/watch" in confirmed_video_url) or ("youtu.be/" in confirmed_video_url)
        ):
            self._log(f"[yt-dlp] 使用扩展确认的视频URL直接下载: {confirmed_video_url}")

            def _download_job():
                return self._download_video_by_ytdlp(confirmed_video_url, target_key, cookie_source=cookie_ref["value"])

            def _on_download_done(path_obj, err2):
                path = path_obj if isinstance(path_obj, Path) else None
                if err2:
                    self._log(f"[yt-dlp] 后台下载异常: {err2}")
                if path is None:
                    if self._last_ytdlp_need_cookie:
                        self._log("[yt-dlp] 需要登录 YouTube 才能获取高画质，正在打开登录流程…")
                        login_url = "https://accounts.google.com/ServiceLogin?service=youtube&continue=" + quote_plus(confirmed_video_url)
                        self._prompt_chromium_login_cookie_async(
                            login_url,
                            '此视频有年龄限制，需要登录 YouTube 才能获取高画质。\n'
                            '请在打开的 WebView2 中登录 Google 账号，\n'
                            '登录后会跳转到视频页面，再点击“确认并继续”。',
                            lambda new_cookie: _retry_download_after_login(confirmed_video_url, target_key, new_cookie),
                        )
                        return
                    self._log("[yt-dlp] 后台下载失败。")
                    return
                edit = self.extra_video_source_edits[target_key]
                if self._target_supports_multi(target_key):
                    edit.append_path(str(path))
                else:
                    edit.set_paths([str(path)])

            self._run_async(_download_job, _on_download_done)
            return
        self._log(f"[yt-dlp] 开始后台搜索 YouTube: {keyword}")

        def _search_job():
            return self._search_youtube_candidates(keyword, limit=20, cookie_source=cookie_ref["value"])

        self._run_async(_search_job, _on_search_done)

    def _on_search_done(items_obj, err):
        items = items_obj if isinstance(items_obj, list) else []
        if err:
            self._log(f"[yt-dlp] 搜索失败: {err}")
            if self._is_ytdlp_need_cookie_error(err):
                self._prompt_chromium_login_for_search_async(
                    keyword,
                    lambda retry_cookie_source: _start_search_with_cookie(retry_cookie_source or ""),
                )
                return
            QMessageBox.critical(self, "搜索失败", f"无法搜索 YouTube：{err}")
            return
        if not items:
            self._log("[yt-dlp] 搜索无结果。")
            QMessageBox.information(self, "未找到", "没有找到可用 YouTube 视频结果。")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("选择 YouTube 视频")
        dlg.resize(920, 620)
        lay = QVBoxLayout(dlg)
        listw = QListWidget()
        for idx, one in enumerate(items, start=1):
            txt = f"[{one['duration']}] {one['title']}\n{one['url']}"
            it = QListWidgetItem(txt)
            it.setData(Qt.UserRole, one["url"])
            listw.addItem(it)
            if idx == 1:
                listw.setCurrentItem(it)
        lay.addWidget(listw, 1)
        btns = QHBoxLayout()
        ok_btn = QPushButton("确认下载")
        cancel_btn = QPushButton("取消")
        btns.addWidget(ok_btn)
        btns.addWidget(cancel_btn)
        btns.addStretch(1)
        lay.addLayout(btns)

        selected_url: dict[str, str] = {"url": ""}

        def _confirm():
            cur = listw.currentItem()
            if cur is None:
                QMessageBox.warning(dlg, "提示", "请先选择一个视频。")
                return
            u = str(cur.data(Qt.UserRole) or "").strip()
            if not self._is_youtube_url(u):
                QMessageBox.warning(dlg, "提示", "选中项不是有效 YouTube 链接。")
                return
            selected_url["url"] = u
            dlg.accept()

        ok_btn.clicked.connect(_confirm)
        cancel_btn.clicked.connect(dlg.reject)
        listw.itemDoubleClicked.connect(lambda *_: _confirm())
        if dlg.exec() != QDialog.Accepted:
            return

        video_url = selected_url["url"]
        if not video_url:
            return
        self._log(f"[yt-dlp] 开始后台下载(搜索结果): {video_url}")

        def _download_job():
            return self._download_video_by_ytdlp(video_url, target_key, cookie_source=cookie_ref["value"])

        def _on_download_done(path_obj, err2):
            path = path_obj if isinstance(path_obj, Path) else None
            if err2:
                self._log(f"[yt-dlp] 后台下载异常: {err2}")
            if path is None:
                if self._last_ytdlp_need_cookie:
                    self._prompt_chromium_login_cookie_async(
                        video_url,
                        "请在已打开的 WebView2 窗口完成登录。\n登录完成后点击“确认并继续”（将自动使用登录 Cookies）。",
                        lambda retry_cookie: self._run_async(
                            lambda: self._download_video_by_ytdlp(video_url, target_key, cookie_source=retry_cookie),
                            _on_download_done,
                        )
                        if retry_cookie
                        else None,
                    )
                    return
                self._log("[yt-dlp] 后台下载失败。")
                return
            edit = self.extra_video_source_edits[target_key]
            if self._target_supports_multi(target_key):
                edit.append_path(str(path))
            else:
                edit.set_paths([str(path)])

        self._run_async(_download_job, _on_download_done)

    # 搜索入口先走登录流程，确保后续搜索/下载可复用同一套 cookie 源。
    self._prompt_chromium_login_for_search_async(
        keyword,
        lambda first_cookie: _start_search_with_cookie(first_cookie or ""),
    )


def _download_youtube_to_output_dir_by_ytdlp(
    self,
    url: str,
    output_dir: Path,
    cookie_source: str | None = None,
    allow_playlist: bool = False,
    root_title: str = "",
) -> list[Path]:
    self._last_ytdlp_error = ""
    self._last_ytdlp_need_cookie = False
    cmd_prefix = self._ytdlp_cmd_prefix()
    if not cmd_prefix:
        self._last_ytdlp_error = "未找到 yt-dlp，请先安装并加入 PATH。"
        self._log(f"[yt-dlp] {self._last_ytdlp_error}")
        return []
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    has_ffmpeg = ffmpeg_available()
    root_title_clean = re.sub(r'[\\/:*?"<>|]+', "", str(root_title or "").strip())
    if not root_title_clean:
        root_title_clean = "未命名"
    out_tpl = str((output_dir / f"[{root_title_clean}][%(upload_date>%Y-%m-%d)s] %(title).180B [%(id)s].%(ext)s").resolve())
    start_ts = time.time()
    base_cmd = [
        *cmd_prefix,
        "--print",
        "after_move:filepath",
        "--quiet",
        "--embed-metadata",
        "--embed-thumbnail",
        "--force-overwrites",
        "--retries",
        "8",
        "--fragment-retries",
        "8",
        "--concurrent-fragments",
        "8",
        "--throttled-rate",
        "250K",
        "-o",
        out_tpl,
    ]
    if allow_playlist:
        base_cmd.extend(["--yes-playlist", "--ignore-errors", "--no-abort-on-error"])
    else:
        base_cmd.append("--no-playlist")
    aria2_bin = shutil.which("aria2c")
    if aria2_bin:
        base_cmd.extend([
            "--downloader",
            "aria2c",
            "--downloader-args",
            "aria2c:-x16 -s16 -k1M --file-allocation=none",
        ])
    else:
        base_cmd.extend(["--http-chunk-size", "10M"])
    if has_ffmpeg:
        base_cmd.extend(["--merge-output-format", "mp4", "--remux-video", "mp4"])
    base_cmd.extend(self._ytdlp_js_runtime_args())
    base_cmd.extend(self._ytdlp_impersonate_args())
    use_url = self._normalize_video_download_url(url) or (url or "").strip()
    cmd = list(base_cmd)
    if cookie_source:
        cmd.extend(["--cookies-from-browser", cookie_source])
    cmd.append(use_url)
    stdout_text = ""
    stderr_text = ""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=False,
            check=False,
            env=self._ytdlp_subprocess_env(),
            timeout=None if allow_playlist else 1200,
        )
        stdout_raw = proc.stdout or b""
        stderr_raw = proc.stderr or b""
        stdout_text = stdout_raw.decode("utf-8", errors="replace")
        stderr_text = stderr_raw.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        self._last_ytdlp_error = "yt-dlp 下载超时，请稍后重试。"
        self._log(f"[yt-dlp] {self._last_ytdlp_error}")
        return []
    except Exception as exc:
        self._last_ytdlp_error = str(exc)
        self._log(f"[yt-dlp] 下载异常: {self._last_ytdlp_error}")
        return []

    downloaded: list[Path] = []
    seen: set[str] = set()
    for one_line in (stdout_text or "").splitlines():
        s = one_line.strip()
        if not s:
            continue
        p = Path(s)
        if not p.exists():
            continue
        k = str(p.resolve()).casefold()
        if k in seen:
            continue
        seen.add(k)
        downloaded.append(p.resolve())
    if not downloaded:
        candidates = sorted(
            [p for p in output_dir.glob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for one in candidates:
            if one.stat().st_mtime < (start_ts - 2.0):
                continue
            k = str(one.resolve()).casefold()
            if k in seen:
                continue
            seen.add(k)
            downloaded.append(one.resolve())
            if not allow_playlist:
                break
    if downloaded:
        downloaded.sort(key=lambda p: p.stat().st_mtime)
        return downloaded
    err_text = ((stderr_text or "") + "\n" + (stdout_text or "")).strip()
    self._last_ytdlp_need_cookie = self._is_ytdlp_need_cookie_error(err_text)
    self._last_ytdlp_error = (err_text or "未知错误")[:800]
    self._log(f"[yt-dlp] 下载失败: {self._last_ytdlp_error}")
    return []


def _download_video_to_nfo_dir_via_webview(self, nfo_path: Path):
    nfo = Path(nfo_path)
    if not str(nfo).strip():
        QMessageBox.warning(self, "提示", "当前条目路径无效。")
        return
    output_dir = nfo.parent.resolve()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        QMessageBox.warning(self, "提示", f"无法创建目标目录：{output_dir}\n{exc}")
        return
    keyword = ""
    base_item_path = nfo
    try:
        all_items = getattr(self, "items", [])
        parent_of = getattr(self, "_tree_parent_of", {})
        idx_map = {str(one.path).casefold(): i for i, one in enumerate(all_items)}
        cur_idx = idx_map.get(str(nfo).casefold())
        while isinstance(cur_idx, int):
            p_idx = parent_of.get(cur_idx)
            if not isinstance(p_idx, int):
                break
            cur_idx = p_idx
        if isinstance(cur_idx, int) and 0 <= cur_idx < len(all_items):
            base_item_path = Path(all_items[cur_idx].path)
    except Exception:
        base_item_path = nfo
    try:
        fields = parse_nfo_fields(base_item_path)
        keyword = str(fields.get("title", "") or "").strip()
    except Exception:
        keyword = ""
    if not keyword:
        keyword = (base_item_path.parent.name.strip() or base_item_path.stem.strip() or "").strip()
    open_url = (
        f"https://www.youtube.com/results?search_query={quote_plus(keyword)}"
        if keyword
        else "https://www.youtube.com/"
    )
    tip = (
        "请在弹出的 WebView2 中选择要下载的视频页面或播放列表页面。\n"
        "支持 playlist 批量下载；确认后会下载到当前 NFO 所在目录。"
    )

    def _start_download(cookie_source: str):
        confirmed = str(getattr(self, "_last_confirmed_video_url", "") or "").strip() or open_url
        if not self._is_youtube_url(confirmed):
            QMessageBox.warning(self, "链接无效", "请在 WebView2 中打开 YouTube 视频页或播放列表页后再确认。")
            return
        parsed = urlparse(confirmed)
        is_video_page = ("/watch" in parsed.path) or ("youtu.be/" in confirmed)
        q = parse_qs(parsed.query or "")
        is_playlist_page = ("/playlist" in parsed.path) or ("list" in q and bool((q.get("list") or [""])[0].strip()))
        if (not is_video_page) and (not is_playlist_page):
            QMessageBox.warning(self, "链接无效", "请在 WebView2 中打开具体视频页或 playlist 页后，再点“确认并继续”。")
            return
        self._log(f"[yt-dlp] NFO右键下载开始: {confirmed} -> {output_dir}")

        def _job():
            return self._download_youtube_to_output_dir_by_ytdlp(
                confirmed,
                output_dir,
                cookie_source=cookie_source,
                allow_playlist=True,
                root_title=keyword,
            )

        def _done(paths_obj, err):
            paths = [p for p in (paths_obj or []) if isinstance(p, Path)] if isinstance(paths_obj, list) else []
            if err:
                self._log(f"[yt-dlp] NFO右键下载异常: {err}")
            if not paths:
                if self._last_ytdlp_need_cookie:
                    self._prompt_chromium_login_cookie_async(
                        confirmed,
                        "请在 WebView2 中完成 YouTube 登录后，再点击“确认并继续”。",
                        lambda retry_cookie: self._run_async(
                            lambda: self._download_youtube_to_output_dir_by_ytdlp(
                                confirmed,
                                output_dir,
                                cookie_source=retry_cookie,
                                allow_playlist=True,
                                root_title=keyword,
                            ),
                            _done,
                        )
                        if retry_cookie
                        else None,
                    )
                    return
                QMessageBox.warning(self, "下载失败", "未下载到视频文件，请查看日志详情。")
                return
            QMessageBox.information(self, "下载完成", f"已下载 {len(paths)} 个视频到：\n{output_dir}")

        self._run_async(_job, _done)

    self._prompt_chromium_login_cookie_async(
        open_url,
        tip,
        lambda cookie_source: _start_download(cookie_source) if cookie_source else None,
        require_watch_page=False,
    )


def _open_season_renamer_for_nfo(self, nfo_path: Path):
    nfo = Path(nfo_path)
    target_dir = nfo.parent if nfo.suffix.lower() == ".nfo" else (nfo if nfo.is_dir() else nfo.parent)
    if not target_dir.exists():
        QMessageBox.warning(self, "提示", f"目标目录不存在：{target_dir}")
        return
    all_files = collect_video_files_from_input(target_dir)
    if not all_files:
        QMessageBox.warning(self, "提示", f"未发现可处理的视频文件：{target_dir}")
        return
    grouped = group_by_season(all_files)
    if not grouped:
        QMessageBox.warning(self, "提示", "未找到季度文件夹下的视频文件（例如 Season1、Season 2）。")
        return
    ops, skipped_tagged = build_rename_ops(grouped)
    valid_ops, skipped_conflicts = validate_conflicts(ops)
    skipped = skipped_tagged + skipped_conflicts
    if not valid_ops:
        QMessageBox.information(self, "结果", "没有可执行的重命名操作。")
        return
    confirm = QMessageBox.question(
        self,
        "确认重命名",
        f"目标目录：{target_dir}\n"
        f"识别视频：{len(all_files)}\n"
        f"可执行重命名：{len(valid_ops)}\n"
        f"将跳过：{len(skipped)}\n\n"
        "是否立即执行？",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.Yes,
    )
    if confirm != QMessageBox.StandardButton.Yes:
        return

    def _job():
        logs = execute_renames(valid_ops)
        append_history_batch(valid_ops)
        return {"logs": logs, "skipped": skipped, "done": len(valid_ops), "target": str(target_dir)}

    def _done(result_obj, err):
        if err:
            self._log(f"[season-renamer] 执行失败: {err}")
            QMessageBox.critical(self, "重命名失败", str(err))
            return
        result = result_obj if isinstance(result_obj, dict) else {}
        logs = result.get("logs", []) if isinstance(result.get("logs"), list) else []
        skipped_msgs = result.get("skipped", []) if isinstance(result.get("skipped"), list) else []
        done_count = int(result.get("done", 0) or 0)
        self._log("=" * 70)
        self._log(f"[season-renamer] 目录: {result.get('target', str(target_dir))}")
        self._log(f"[season-renamer] 已执行: {done_count}，跳过: {len(skipped_msgs)}")
        for msg in skipped_msgs:
            self._log(f"[season-renamer] {msg}")
        for msg in logs:
            self._log(f"[season-renamer] {msg}")
        QMessageBox.information(self, "完成", f"重命名完成，共处理 {done_count} 个文件。")

    self._run_async(_job, _done)


def _open_season_episode_offset_dialog(self, selected_items: list[NfoItem]):
    episode_items = [it for it in (selected_items or []) if str(getattr(it, "media_type", "") or "") == "episode"]
    if not episode_items:
        QMessageBox.information(self, "提示", "当前选择中没有可处理的剧集 NFO。")
        return
    dlg = QDialog(self)
    dlg.setWindowTitle("季度偏移")
    dlg.resize(980, 620)
    lay = QVBoxLayout(dlg)

    top = QHBoxLayout()
    top.addWidget(QLabel("季度偏移:", dlg))
    season_spin = QSpinBox(dlg)
    season_spin.setRange(-500, 500)
    season_spin.setValue(0)
    season_spin.setFixedWidth(120)
    top.addWidget(season_spin)
    top.addSpacing(24)
    top.addWidget(QLabel("集数偏移:", dlg))
    episode_spin = QSpinBox(dlg)
    episode_spin.setRange(-50000, 50000)
    episode_spin.setValue(0)
    episode_spin.setFixedWidth(120)
    top.addWidget(episode_spin)
    top.addStretch(1)
    lay.addLayout(top)

    trees_row = QHBoxLayout()
    old_tree = QTreeWidget(dlg)
    old_tree.setHeaderLabels(["原命名"])
    old_tree.setRootIsDecorated(True)
    old_tree.setIndentation(16)
    old_tree.setSelectionMode(QAbstractItemView.NoSelection)
    old_tree.setFocusPolicy(Qt.NoFocus)
    _svg_dir = str(Path(__file__).parent.resolve()).replace("\\", "/")
    old_tree.setStyleSheet(
        "QTreeWidget{outline:0;}"
        "QTreeWidget::item{border:none;outline:0;}"
        "QTreeWidget::item:selected{background:transparent;color:inherit;}"
        "QTreeWidget::indicator{width:14px;height:14px;}"
        f"QTreeWidget::indicator:unchecked{{image:url({_svg_dir}/qt_checkbox_unchecked.svg);}}"
        f"QTreeWidget::indicator:checked{{image:url({_svg_dir}/qt_checkbox_check.svg);}}"
        f"QTreeWidget::indicator:indeterminate{{image:url({_svg_dir}/qt_checkbox_indeterminate.svg);}}"
    )
    new_tree = QTreeWidget(dlg)
    new_tree.setHeaderLabels(["重命名"])
    new_tree.setRootIsDecorated(True)
    new_tree.setIndentation(16)
    new_tree.setSelectionMode(QAbstractItemView.NoSelection)
    new_tree.setFocusPolicy(Qt.NoFocus)
    new_tree.setStyleSheet(
        "QTreeWidget{outline:0;}"
        "QTreeWidget::item{border:none;outline:0;}"
        "QTreeWidget::item:selected{background:transparent;color:inherit;}"
    )
    trees_row.addWidget(old_tree, 1)
    trees_row.addWidget(new_tree, 1)
    lay.addLayout(trees_row, 1)

    status_lbl = QLabel("", dlg)
    status_lbl.setWordWrap(True)
    lay.addWidget(status_lbl)

    btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, Qt.Horizontal, dlg)
    lay.addWidget(btns)
    ok_btn = btns.button(QDialogButtonBox.Ok)
    cancel_btn = btns.button(QDialogButtonBox.Cancel)
    if ok_btn is not None:
        ok_btn.setText("确定")
    if cancel_btn is not None:
        cancel_btn.setText("取消")
    btns.accepted.connect(dlg.accept)
    btns.rejected.connect(dlg.reject)
    checked_row_ids: set[str] = set()
    first_build = True
    latest_plan: dict = {}
    _poll_prev: frozenset[str] | None = None
    _guard = 0
    ROLE_KIND = int(Qt.UserRole)
    ROLE_RID = int(Qt.UserRole) + 1

    def _get_kind(item: QTreeWidgetItem) -> str:
        return str(item.data(0, ROLE_KIND) or "")

    def _get_rid(item: QTreeWidgetItem) -> str:
        return str(item.data(0, ROLE_RID) or "")

    def _scan_tree():
        checked_row_ids.clear()
        for i in range(old_tree.topLevelItemCount()):
            stk: list[QTreeWidgetItem] = [old_tree.topLevelItem(i)]
            while stk:
                cur = stk.pop()
                if _get_kind(cur) == "episode":
                    rid = _get_rid(cur)
                    if rid and cur.checkState(0) == Qt.CheckState.Checked:
                        checked_row_ids.add(rid)
                for j in range(cur.childCount()):
                    stk.append(cur.child(j))

    def _sync_parents():
        for i in range(old_tree.topLevelItemCount()):
            _sync_one(old_tree.topLevelItem(i))

    def _sync_one(node: QTreeWidgetItem) -> tuple[bool, bool, bool]:
        kind = _get_kind(node)
        if kind == "episode":
            st = node.checkState(0)
            return (st == Qt.CheckState.Checked, st != Qt.CheckState.Checked, True)
        any_c = any_u = has = False
        for i in range(node.childCount()):
            c, u, h = _sync_one(node.child(i))
            any_c |= c; any_u |= u; has |= h
        if has and kind in ("show", "season"):
            if any_c and any_u:
                node.setCheckState(0, Qt.CheckState.PartiallyChecked)
            elif any_c:
                node.setCheckState(0, Qt.CheckState.Checked)
            else:
                node.setCheckState(0, Qt.CheckState.Unchecked)
        return (any_c, any_u, has)

    def _on_item_changed(item, _col):
        nonlocal _guard
        if _guard:
            return
        kind = _get_kind(item)
        if kind in ("show", "season"):
            _guard += 1
            target = item.checkState(0)
            if target == Qt.CheckState.PartiallyChecked:
                target = Qt.CheckState.Checked
            stk: list[QTreeWidgetItem] = [item]
            while stk:
                cur = stk.pop()
                for i in range(cur.childCount()):
                    c = cur.child(i)
                    ck = _get_kind(c)
                    if ck == "episode":
                        c.setCheckState(0, target)
                    elif c.childCount() > 0:
                        c.setCheckState(0, target)
                        stk.append(c)
            _guard -= 1

    old_tree.itemChanged.connect(_on_item_changed)

    def _poll_check():
        nonlocal _poll_prev, _guard
        if old_tree.topLevelItemCount() == 0:
            return
        _scan_tree()
        snap = frozenset(checked_row_ids)
        if _poll_prev is not None and snap == _poll_prev:
            return
        _poll_prev = snap
        _guard += 1
        _sync_parents()
        _guard -= 1
        _rebuild_new_tree()
        _refresh_status()

    _poll_timer = QTimer(dlg)
    _poll_timer.timeout.connect(_poll_check)
    _poll_timer.start(200)

    def _refresh_status():
        plan = latest_plan
        if not plan:
            return
        chosen = set(checked_row_ids)
        ok_rows = [r for r in (plan.get("preview_rows") or []) if r.get("row_id", "") in chosen]
        op_records = [r for r in (plan.get("rename_op_records") or []) if r.get("row_id", "") in chosen]
        invalid_chosen = [
            r for r in (plan.get("preview_rows_all") or [])
            if r.get("status") == "invalid" and r.get("row_id", "") in chosen
        ]
        total = len(plan.get("preview_rows_all") or [])
        status_parts = [
            f"总剧集: {total}",
            f"已勾选: {len(ok_rows)}",
            f"将执行文件: {len(op_records)}",
        ]
        if invalid_chosen:
            msgs = [f"{r.get('old_name', '?')}: {r.get('reason', '非法')}" for r in invalid_chosen[:3]]
            status_parts.append(f"不合法: {len(invalid_chosen)}")
            status_lbl.setText("；".join(status_parts) + "\n" + "\n".join(msgs))
            ok_btn.setEnabled(False)
            return
        status_lbl.setText("；".join(status_parts))
        ok_btn.setEnabled(len(op_records) > 0)

    def _rebuild_new_tree():
        new_tree.clear()
        plan = latest_plan
        if not plan:
            return
        chosen = set(checked_row_ids)
        new_show_nodes: dict[str, QTreeWidgetItem] = {}
        new_season_nodes: dict[tuple[str, int], QTreeWidgetItem] = {}
        for row in (plan.get("preview_rows_all") or []):
            row_id = row.get("row_id", "")
            row_status = row.get("status", "ok") or "ok"
            row_reason = (row.get("reason") or "").strip()
            show_name = row["show_name"]
            is_checked = row_id and row_id in chosen
            if is_checked and row_status == "ok":
                display_s = int(row["new_folder_season"])
                display_name = row["new_name"]
            else:
                display_s = int(row["old_folder_season"])
                display_name = row["old_name"]
            if show_name not in new_show_nodes:
                new_show_nodes[show_name] = QTreeWidgetItem([f"电视剧|{show_name}"])
                new_tree.addTopLevelItem(new_show_nodes[show_name])
            key = (show_name, display_s)
            if key not in new_season_nodes:
                new_season_nodes[key] = QTreeWidgetItem([f"季度|第 {display_s} 季"])
                new_show_nodes[show_name].addChild(new_season_nodes[key])
            label = f"剧集|{display_name}"
            if row_status != "ok" and row_reason:
                label = f"{label}  （跳过：{row_reason}）"
            new_season_nodes[key].addChild(QTreeWidgetItem([label]))
        new_tree.expandAll()

    def _refresh_preview():
        nonlocal latest_plan, first_build, _poll_prev, _guard
        _poll_timer.stop()
        if not first_build and old_tree.topLevelItemCount() > 0:
            _scan_tree()
        season_offset = int(season_spin.value())
        episode_offset = int(episode_spin.value())
        plan = self._build_season_episode_offset_plan(episode_items, season_offset, episode_offset)
        latest_plan = plan

        all_valid_ids: set[str] = set()
        for row in plan["preview_rows_all"]:
            rid = row.get("row_id", "")
            if rid and (row.get("status", "ok") or "ok") == "ok":
                all_valid_ids.add(rid)

        if first_build:
            checked_row_ids.clear()
            checked_row_ids.update(all_valid_ids)
            first_build = False
        else:
            checked_row_ids.intersection_update(all_valid_ids)

        _guard += 1
        old_tree.clear()
        old_show_nodes: dict[str, QTreeWidgetItem] = {}
        old_season_nodes: dict[tuple[str, int], QTreeWidgetItem] = {}

        for row in plan["preview_rows_all"]:
            show_name = row["show_name"]
            old_s = int(row["old_folder_season"])
            old_name = row["old_name"]
            row_status = row.get("status", "ok") or "ok"
            row_reason = (row.get("reason") or "").strip()
            row_id = row.get("row_id", "")
            if show_name not in old_show_nodes:
                n = QTreeWidgetItem([f"电视剧|{show_name}"])
                n.setFlags(n.flags() | Qt.ItemIsUserCheckable)
                n.setCheckState(0, Qt.CheckState.Unchecked)
                n.setData(0, ROLE_KIND, "show")
                old_tree.addTopLevelItem(n)
                old_show_nodes[show_name] = n
            old_key = (show_name, old_s)
            if old_key not in old_season_nodes:
                n = QTreeWidgetItem([f"季度|第 {old_s} 季"])
                n.setFlags(n.flags() | Qt.ItemIsUserCheckable)
                n.setCheckState(0, Qt.CheckState.Unchecked)
                n.setData(0, ROLE_KIND, "season")
                old_show_nodes[show_name].addChild(n)
                old_season_nodes[old_key] = n
            old_label = f"剧集|{old_name}"
            if row_status != "ok" and row_reason:
                old_label = f"{old_label}  （跳过：{row_reason}）"
            old_child = QTreeWidgetItem([old_label])
            if row_status == "ok" and row_id:
                old_child.setFlags(old_child.flags() | Qt.ItemIsUserCheckable)
                old_child.setData(0, ROLE_KIND, "episode")
                old_child.setData(0, ROLE_RID, row_id)
                chk = Qt.CheckState.Checked if row_id in checked_row_ids else Qt.CheckState.Unchecked
                old_child.setCheckState(0, chk)
            else:
                old_child.setData(0, ROLE_KIND, "skip")
            old_season_nodes[old_key].addChild(old_child)

        old_tree.expandAll()
        _sync_parents()
        _guard -= 1
        _poll_prev = frozenset(checked_row_ids)
        _poll_timer.start(200)
        _rebuild_new_tree()
        _refresh_status()

    season_spin.valueChanged.connect(lambda *_: _refresh_preview())
    episode_spin.valueChanged.connect(lambda *_: _refresh_preview())
    _refresh_preview()

    if dlg.exec() != QDialog.Accepted:
        return
    season_offset = int(season_spin.value())
    episode_offset = int(episode_spin.value())
    if season_offset == 0 and episode_offset == 0:
        QMessageBox.information(self, "提示", "偏移量均为 0，无需处理。")
        return
    self._apply_season_episode_offset(
        episode_items,
        season_offset,
        episode_offset,
        selected_row_ids=set(checked_row_ids),
        selection_mode_active=True,
    )


def _build_season_episode_offset_plan(self, episode_items: list[NfoItem], season_offset: int, episode_offset: int) -> dict:
    pat = re.compile(r"(?<!\d)S(\d+)E(\d+)(?!\d)", re.IGNORECASE)
    idx_map = {str(one.path).casefold(): i for i, one in enumerate(getattr(self, "items", []))}
    parent_of: dict[int, int | None] = getattr(self, "_tree_parent_of", {})

    def _find_season_dir(item: NfoItem) -> Path:
        idx = idx_map.get(str(item.path).casefold())
        while isinstance(idx, int):
            p_idx = parent_of.get(idx)
            if not isinstance(p_idx, int):
                break
            if 0 <= p_idx < len(self.items):
                p_item = self.items[p_idx]
                if str(getattr(p_item, "media_type", "") or "") == "season":
                    return p_item.path.parent
            idx = p_idx
        return item.path.parent

    def _find_show_name(item: NfoItem) -> str:
        idx = idx_map.get(str(item.path).casefold())
        top_item = item
        while isinstance(idx, int):
            p_idx = parent_of.get(idx)
            if not isinstance(p_idx, int):
                break
            if 0 <= p_idx < len(self.items):
                top_item = self.items[p_idx]
            idx = p_idx
        try:
            fields = parse_nfo_fields(top_item.path)
            title = str(fields.get("title", "") or "").strip()
            if title:
                return title
        except Exception:
            pass
        return top_item.path.parent.name.strip() or top_item.path.stem.strip() or "未命名"

    rename_ops: list[tuple[Path, Path]] = []
    rename_op_records: list[dict] = []
    invalid_msgs: list[str] = []
    hard_invalids: list[str] = []
    season0_hits = 0
    preview_rows: list[dict] = []
    preview_rows_all: list[dict] = []
    episode_video_seed: dict[str, Path] = {}
    seed_video_files: set[Path] = set()

    for ep_item in episode_items:
        nfo_path = Path(ep_item.path)
        row_id = str(nfo_path).casefold()
        stem = nfo_path.stem
        candidates: list[Path] = []
        try:
            for cand in nfo_path.parent.glob(f"{stem}.*"):
                if cand.is_file() and cand.suffix.lower() in VIDEO_EXTS:
                    candidates.append(cand.resolve())
        except Exception:
            candidates = []
        if not candidates:
            continue
        candidates.sort(key=lambda p: (p.suffix.lower(), p.name.casefold()))
        pick = candidates[0]
        episode_video_seed[str(nfo_path).casefold()] = pick
        seed_video_files.add(pick)

    baseline_stem_by_video: dict[str, str] = {}
    if seed_video_files:
        grouped = group_by_season(seed_video_files)
        baseline_ops, _ = build_rename_ops(grouped)
        baseline_stem_by_video = {str(op.source.resolve()).casefold(): op.target.stem for op in baseline_ops}

    for ep_item in episode_items:
        nfo_path = Path(ep_item.path)
        row_id = str(nfo_path).casefold()
        stem = nfo_path.stem
        src_season_dir = _find_season_dir(ep_item)
        m_dir = re.match(r"^season\s*(\d+)$", src_season_dir.name.strip(), flags=re.IGNORECASE)
        old_folder_season = int(m_dir.group(1)) if m_dir else 0
        base_stem = stem
        m = pat.search(base_stem)
        if m is None:
            seed_video = episode_video_seed.get(str(nfo_path).casefold())
            if seed_video is not None:
                mapped = baseline_stem_by_video.get(str(seed_video).casefold(), "").strip()
                if mapped:
                    base_stem = mapped
                else:
                    base_stem = seed_video.stem
                m = pat.search(base_stem)
        if m is None:
            new_folder_season = old_folder_season + int(season_offset)
            reason = "无 SxEx 标记（重命名规则未生成）"
            invalid_msgs.append(f"跳过（{reason}）: {nfo_path.name}")
            preview_rows_all.append(
                {
                    "show_name": _find_show_name(ep_item),
                    "old_season": old_folder_season,
                    "new_season": new_folder_season,
                    "old_folder_season": old_folder_season,
                    "new_folder_season": max(0, new_folder_season),
                    "old_episode": 0,
                    "new_episode": 0,
                    "old_name": stem,
                    "new_name": base_stem,
                    "status": "skipped",
                    "reason": reason,
                    "row_id": row_id,
                }
            )
            continue

        old_season = int(m.group(1))
        old_episode = int(m.group(2))
        old_ep_width = max(2, len(str(m.group(2))))
        new_season = old_season + int(season_offset)
        new_episode = old_episode + int(episode_offset)
        old_folder_season = int(m_dir.group(1)) if m_dir else old_season
        new_folder_season = old_folder_season + int(season_offset)
        if new_season < 0 or new_episode <= 0 or new_folder_season < 0:
            reason = "偏移后季号或集号非法"
            hard_invalids.append(
                f"{nfo_path.name}: S{old_season:02d}E{old_episode} -> S{new_season:02d}E{new_episode}，"
                f"Season {old_folder_season} -> Season {new_folder_season}"
            )
            preview_rows_all.append(
                {
                    "show_name": _find_show_name(ep_item),
                    "old_season": old_season,
                    "new_season": new_season,
                    "old_folder_season": old_folder_season,
                    "new_folder_season": max(0, new_folder_season),
                    "old_episode": old_episode,
                    "new_episode": new_episode,
                    "old_name": stem,
                    "new_name": stem,
                    "status": "invalid",
                    "reason": reason,
                    "row_id": row_id,
                }
            )
            continue
        if new_season == 0 or new_folder_season == 0:
            season0_hits += 1
        ep_width = max(2, old_ep_width, len(str(new_episode)))
        token = f"S{new_season:02d}E{new_episode:0{ep_width}d}"
        new_stem = f"{base_stem[:m.start()]}{token}{base_stem[m.end():]}"
        show_root_dir = src_season_dir.parent if src_season_dir.parent != src_season_dir else nfo_path.parent
        dst_season_dir = show_root_dir / f"Season {new_folder_season}"
        related_files: list[Path] = []
        for cand in nfo_path.parent.glob(f"{stem}.*"):
            if not cand.is_file():
                continue
            suf = cand.suffix.lower()
            if suf in VIDEO_EXTS or suf in {".nfo", ".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"}:
                related_files.append(cand)
        if not related_files:
            reason = "未找到关联视频"
            invalid_msgs.append(f"跳过（{reason}）: {nfo_path.name}")
            preview_rows_all.append(
                {
                    "show_name": _find_show_name(ep_item),
                    "old_season": old_season,
                    "new_season": new_season,
                    "old_folder_season": old_folder_season,
                    "new_folder_season": new_folder_season,
                    "old_episode": old_episode,
                    "new_episode": new_episode,
                    "old_name": stem,
                    "new_name": new_stem,
                    "status": "skipped",
                    "reason": reason,
                    "row_id": row_id,
                }
            )
            continue
        one_preview = {
            "show_name": _find_show_name(ep_item),
            "old_season": old_season,
            "new_season": new_season,
            "old_folder_season": old_folder_season,
            "new_folder_season": new_folder_season,
            "old_episode": old_episode,
            "new_episode": new_episode,
            "old_name": stem,
            "new_name": new_stem,
            "status": "ok",
            "reason": "",
            "row_id": row_id,
        }
        preview_rows.append(one_preview)
        preview_rows_all.append(one_preview)
        for src in related_files:
            dst = dst_season_dir / f"{new_stem}{src.suffix}"
            rename_ops.append((src.resolve(), dst.resolve()))
            rename_op_records.append(
                {
                    "row_id": row_id,
                    "src": src.resolve(),
                    "dst": dst.resolve(),
                }
            )

    sort_key = lambda r: (
        str(r.get("show_name", "")).casefold(),
        int(r.get("old_folder_season", 0)),
        int(r.get("old_episode", 0)),
        str(r.get("old_name", "")).casefold(),
    )
    return {
        "rename_ops": rename_ops,
        "rename_op_records": rename_op_records,
        "invalid_msgs": invalid_msgs,
        "hard_invalids": hard_invalids,
        "season0_hits": season0_hits,
        "preview_rows": sorted(preview_rows, key=sort_key),
        "preview_rows_all": sorted(preview_rows_all, key=sort_key),
    }


def _apply_season_episode_offset(
    self,
    episode_items: list[NfoItem],
    season_offset: int,
    episode_offset: int,
    selected_row_ids: set[str] | None = None,
    selection_mode_active: bool = False,
):
    plan = self._build_season_episode_offset_plan(episode_items, season_offset, episode_offset)
    chosen = {str(x) for x in (selected_row_ids or set()) if str(x)}
    if selection_mode_active:
        op_records = [r for r in (plan.get("rename_op_records", []) or []) if str(r.get("row_id", "")) in chosen]
        rename_ops = [(Path(r["src"]), Path(r["dst"])) for r in op_records]
        chosen_rows = [r for r in (plan.get("preview_rows", []) or []) if str(r.get("row_id", "")) in chosen]
        season0_hits = sum(
            1
            for r in chosen_rows
            if (int(r.get("new_season", 0)) == 0 or int(r.get("new_folder_season", 0)) == 0)
        )
        invalid_msgs = []
        hard_invalids = []
    else:
        rename_ops = list(plan["rename_ops"])
        invalid_msgs = list(plan["invalid_msgs"])
        hard_invalids = list(plan["hard_invalids"])
        season0_hits = int(plan["season0_hits"])
    if hard_invalids:
        QMessageBox.critical(
            self,
            "偏移不合法",
            "存在非法目标（Season < 0 或 Episode <= 0）：\n\n" + "\n".join(hard_invalids[:10]),
        )
        return

    if not rename_ops:
        msg = "没有可执行的偏移重命名操作。"
        if invalid_msgs:
            msg = msg + "\n\n" + "\n".join(invalid_msgs[:20])
        QMessageBox.information(self, "提示", msg)
        return

    if season0_hits > 0:
        ret = QMessageBox.warning(
            self,
            "将偏移到 Season 0",
            f"有 {season0_hits} 个条目将移动到 Season 0。\n是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

    src_set = {s for s, _ in rename_ops}
    dst_seen: set[Path] = set()
    overwrite_hits: list[str] = []
    duplicate_hits: list[str] = []
    for src, dst in rename_ops:
        if dst in dst_seen:
            duplicate_hits.append(str(dst))
            continue
        dst_seen.add(dst)
        if dst.exists() and (dst not in src_set):
            overwrite_hits.append(str(dst))
    if duplicate_hits:
        QMessageBox.critical(
            self,
            "目标冲突",
            "偏移后存在多个文件映射到同一目标，无法执行。\n\n" + "\n".join(duplicate_hits[:20]),
        )
        return
    if overwrite_hits:
        ret = QMessageBox.warning(
            self,
            "检测到覆盖风险",
            f"偏移后有 {len(overwrite_hits)} 个目标文件已存在，继续将覆盖。\n是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

    def _job():
        logs: list[str] = []
        temp_ops: list[tuple[Path, Path, Path]] = []
        for src, dst in rename_ops:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src == dst:
                continue
            temp = src.with_name(f".tmp_offset_{uuid.uuid4().hex}{src.suffix}")
            src.rename(temp)
            temp_ops.append((src, temp, dst))
            logs.append(f"[临时] {src} -> {temp.name}")
        for src, temp, dst in temp_ops:
            temp.rename(dst)
            logs.append(f"[完成] {src.name} -> {dst}")
        return {"done": len(temp_ops), "logs": logs, "skipped": invalid_msgs}

    def _done(result_obj, err):
        if err:
            self._log(f"[season-offset] 执行失败: {err}")
            QMessageBox.critical(self, "执行失败", str(err))
            return
        result = result_obj if isinstance(result_obj, dict) else {}
        done = int(result.get("done", 0) or 0)
        logs = result.get("logs", []) if isinstance(result.get("logs"), list) else []
        skipped_msgs = result.get("skipped", []) if isinstance(result.get("skipped"), list) else []
        self._log("=" * 70)
        self._log(f"[season-offset] 完成: {done} 个文件")
        for msg in skipped_msgs:
            self._log(f"[season-offset] {msg}")
        for msg in logs:
            self._log(f"[season-offset] {msg}")
        QMessageBox.information(self, "完成", f"季度偏移完成，共处理 {done} 个文件。")

    self._run_async(_job, _done)


def _open_url_dialog(self, target_key: str, kind: str, is_extra: bool):
    title = "输入链接"
    tip = "图片链接（http/https）" if kind == "image" else ("视频链接（http/https）" if kind == "video" else "音频链接（http/https）")
    url, ok = QInputDialog.getText(self, title, tip)
    if not ok or not url.strip():
        return
    self._log(f"[download] 开始后台下载: kind={kind}, key={target_key}")

    def _job():
        if kind == "image":
            return self._download_image_from_url(url, target_key, show_dialog=False)
        return self._download_binary_from_url(url, target_key, kind=kind, show_dialog=False)

    def _on_done(path_obj, err):
        path = path_obj if isinstance(path_obj, Path) else None
        if err:
            self._log(f"[download] 后台任务异常: {err}")
            QMessageBox.critical(self, "下载失败", err)
            return
        if path is None:
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
                        _on_done,
                    )
                    if retry_cookie
                    else None,
                )
                return
            self._log("[download] 后台下载失败。")
            QMessageBox.warning(self, "下载失败", "下载失败，请查看日志详情。")
            return
        if kind == "image":
            edit = self.extra_image_source_edits[target_key] if is_extra else self.image_source_edits[target_key]
        elif kind == "video":
            edit = self.extra_video_source_edits[target_key]
        else:
            edit = self.extra_audio_source_edits[target_key]
        if is_extra and self._target_supports_multi(target_key):
            edit.append_path(str(path))
        else:
            edit.set_paths([str(path)])

    self._run_async(_job, _on_done)

def _open_search_dialog(self, target_key: str, is_extra: bool):
    keyword = self._resolve_search_keyword()
    dialog = ImageSearchDialog(self, keyword, lambda u: self._download_image_from_url(u, f"{target_key}_selected"))
    if dialog.exec() != QDialog.Accepted or dialog.selected_path is None:
        return
    edit = self.extra_image_source_edits[target_key] if is_extra else self.image_source_edits[target_key]
    if is_extra and self._target_supports_multi(target_key):
        edit.append_path(str(dialog.selected_path))
    else:
        edit.set_paths([str(dialog.selected_path)])

def bind_network_services_methods(cls):
    cls._download_image_from_url = _download_image_from_url
    cls._download_binary_from_url = _download_binary_from_url
    cls._is_youtube_url = _is_youtube_url
    cls._is_twitter_url = _is_twitter_url
    cls._normalize_video_download_url = _normalize_video_download_url
    cls._is_ytdlp_need_cookie_error = _is_ytdlp_need_cookie_error
    cls._is_cookie_decrypt_error = _is_cookie_decrypt_error
    cls._is_twitter_need_cookie_error = _is_twitter_need_cookie_error
    cls._chromium_cookie_profile_name = _chromium_cookie_profile_name
    cls._chromium_user_data_dir = _chromium_user_data_dir
    cls._chromium_cookie_source = _chromium_cookie_source
    cls._chromium_confirm_marker_prefix = _chromium_confirm_marker_prefix
    cls._chromium_extension_dir = _chromium_extension_dir
    cls._chromium_profile_dir = _chromium_profile_dir
    cls._chromium_cookie_db_paths = _chromium_cookie_db_paths
    cls._clear_chromium_restore_session_files = _clear_chromium_restore_session_files
    cls._wait_for_chromium_cookie_db = _wait_for_chromium_cookie_db
    cls._wait_for_chromium_cookie_db_copyable = _wait_for_chromium_cookie_db_copyable
    cls._opened_youtube_watch_page_since = _opened_youtube_watch_page_since
    cls._has_chromium_confirm_marker_since = _has_chromium_confirm_marker_since
    cls._latest_confirmed_video_url_since = _latest_confirmed_video_url_since
    cls._close_chromium_process = _close_chromium_process
    cls._ytdlp_js_runtime_args = _ytdlp_js_runtime_args
    cls._ytdlp_impersonate_args = _ytdlp_impersonate_args
    cls._ytdlp_cmd_prefix = _ytdlp_cmd_prefix
    cls._ytdlp_subprocess_env = _ytdlp_subprocess_env
    cls._pick_best_ytdlp_format = _pick_best_ytdlp_format
    cls._download_video_by_ytdlp = _download_video_by_ytdlp
    cls._ensure_chromium_browser = _ensure_chromium_browser
    cls._prompt_chromium_login_cookie = _prompt_chromium_login_cookie
    cls._retry_ytdlp_with_browser_login = _retry_ytdlp_with_browser_login
    cls._prompt_chromium_login_for_search = _prompt_chromium_login_for_search
    cls._prompt_chromium_login_cookie_async = _prompt_chromium_login_cookie_async
    cls._prompt_chromium_login_for_search_async = _prompt_chromium_login_for_search_async
    cls._search_youtube_candidates = _search_youtube_candidates
    cls._resolve_search_keyword = _resolve_search_keyword
    cls._open_video_search_dialog = _open_video_search_dialog
    cls._download_youtube_to_output_dir_by_ytdlp = _download_youtube_to_output_dir_by_ytdlp
    cls._download_video_to_nfo_dir_via_webview = _download_video_to_nfo_dir_via_webview
    cls._open_season_renamer_for_nfo = _open_season_renamer_for_nfo
    cls._open_season_episode_offset_dialog = _open_season_episode_offset_dialog
    cls._build_season_episode_offset_plan = _build_season_episode_offset_plan
    cls._apply_season_episode_offset = _apply_season_episode_offset
    cls._open_url_dialog = _open_url_dialog
    cls._open_search_dialog = _open_search_dialog

