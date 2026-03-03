from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path, PureWindowsPath

from jellyfin_nfo_core import NfoItem


def _session_base_dir(self) -> Path:
    return Path(__file__).resolve().parent


def _build_device_session_key() -> str:
    host = str(os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "").strip().lower()
    user = str(os.environ.get("USERNAME") or os.environ.get("USER") or "").strip().lower()
    if not host:
        host = "unknown-host"
    if not user:
        user = "unknown-user"
    return f"default@{host}@{user}"


def _to_session_rel_path_text(self, raw_path) -> str | None:
    txt = str(raw_path or "").strip()
    if not txt:
        return None
    p = Path(txt)
    if not p.is_absolute():
        return p.as_posix()
    wp = PureWindowsPath(txt)
    drive = (wp.drive or "").strip()
    if drive.startswith("\\\\"):
        unc_head = drive.lstrip("\\")
        unc_parts = [x for x in unc_head.split("\\") if x]
        if len(unc_parts) >= 2:
            server, share = unc_parts[0], unc_parts[1]
            anchor = wp.anchor or ""
            tail = str(wp)[len(anchor):].lstrip("\\/")
            tail_norm = tail.replace("\\", "/")
            return f"unc/{server}/{share}" + (f"/{tail_norm}" if tail_norm else "")
    if len(drive) == 2 and drive[1] == ":":
        letter = drive[0].lower()
        anchor = wp.anchor or ""
        tail = str(wp)[len(anchor):].lstrip("\\/")
        tail_norm = tail.replace("\\", "/")
        return f"drive/{letter}" + (f"/{tail_norm}" if tail_norm else "")
    return None


def _from_session_rel_path_text(self, raw_path) -> Path | None:
    txt = str(raw_path or "").strip()
    if not txt:
        return None
    p = Path(txt)
    # 会话数据库仅允许相对路径；旧绝对路径一律视为无效。
    if p.is_absolute():
        return None
    norm = txt.replace("\\", "/").strip().strip("/")
    if not norm:
        return None
    parts = [x for x in norm.split("/") if x]
    if len(parts) >= 2 and parts[0].lower() == "drive":
        letter = parts[1].strip()
        if len(letter) == 1 and letter.isalpha():
            rest = "\\".join(parts[2:]).strip("\\")
            return Path(f"{letter.upper()}:\\{rest}" if rest else f"{letter.upper()}:\\")
    if len(parts) >= 3 and parts[0].lower() == "unc":
        server = parts[1].strip()
        share = parts[2].strip()
        if server and share:
            rest = "\\".join(parts[3:]).strip("\\")
            base = f"\\\\{server}\\{share}"
            return Path(f"{base}\\{rest}" if rest else base)
    return self._session_base_dir() / Path(norm)


def _is_absolute_path_text(raw_path) -> bool:
    txt = str(raw_path or "").strip()
    if not txt:
        return False
    try:
        return Path(txt).is_absolute()
    except Exception:
        return False


_path_exists_cache: dict[str, bool] = {}


def _path_exists_for_restore(path_obj: Path | None) -> bool:
    """路径存在检测（带缓存），避免对同一 NAS 路径多次 I/O。"""
    if not isinstance(path_obj, Path):
        return False
    key = str(path_obj).casefold()
    cached = _path_exists_cache.get(key)
    if cached is not None:
        return cached
    try:
        result = path_obj.exists()
    except Exception:
        result = False
    _path_exists_cache[key] = result
    return result


def _clear_session_records(self, cur):
    cur.execute("DELETE FROM session_kv WHERE session_key=?", (self._session_pg_key,))
    cur.execute("DELETE FROM session_items WHERE session_key=?", (self._session_pg_key,))


def _ensure_pg_driver(self):
    # 兼容主窗口已有字段，SQLite 方案下不需要外部驱动。
    return ("sqlite3", sqlite3)


def _auto_configure_pg_session(self):
    # 使用 Python 内置 sqlite3，会话库文件放在程序目录，便于放在服务器共享。
    db_path = Path(__file__).with_name(".jellyfin_qt_session.sqlite3")
    self._session_pg_dsn = str(db_path)
    self._session_pg_key = _build_device_session_key()
    self._session_pg_ready = True
    self._session_pg_error_logged = False
    self._log(f"已启用会话数据库（SQLite）: {db_path} | 会话键: {self._session_pg_key}")


def _shutdown_embedded_pg(self):
    # SQLite 无独立进程，无需停止。
    return


def _pg_connect(self):
    if not self._session_pg_ready:
        return None
    try:
        con = sqlite3.connect(self._session_pg_dsn, timeout=8, check_same_thread=False)
        # 共享目录/多设备场景下，WAL 常出现锁竞争与额外开销；改为 DELETE 更稳。
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=8000")
        con.execute("PRAGMA temp_store=MEMORY")
        return con
    except Exception as exc:
        if not self._session_pg_error_logged:
            self._log(f"会话数据库（SQLite）不可用: {exc}")
            self._session_pg_error_logged = True
        self._session_pg_ready = False
        return None


def _pg_ensure_tables(self, cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS session_kv (
            session_key TEXT NOT NULL,
            key TEXT NOT NULL,
            value_text TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(session_key, key)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS session_items (
            session_key TEXT NOT NULL,
            path TEXT NOT NULL,
            media_type TEXT NOT NULL,
            display TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(session_key, path)
        )
        """
    )


def _ensure_pg_session_schema(self):
    if not self._session_pg_ready:
        return
    con = self._pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        self._pg_ensure_tables(cur)
        con.commit()
    except Exception as exc:
        self._log(f"会话数据库（SQLite）自动建表失败: {exc}")
    finally:
        con.close()


def _pg_set_kv(self, cur, key: str, value_obj):
    cur.execute(
        """
        INSERT INTO session_kv(session_key, key, value_text, updated_at)
        VALUES(?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(session_key, key)
        DO UPDATE SET value_text=excluded.value_text, updated_at=CURRENT_TIMESTAMP
        """,
        (self._session_pg_key, key, json.dumps(value_obj, ensure_ascii=False)),
    )


def _pg_get_kv(self, cur, key: str, default):
    cur.execute(
        "SELECT value_text FROM session_kv WHERE session_key=? AND key=?",
        (self._session_pg_key, key),
    )
    row = cur.fetchone()
    if not row:
        return default
    try:
        return json.loads(str(row[0]))
    except Exception:
        return default


def _pg_get_kv_by_session_key(cur, session_key: str, key: str, default):
    cur.execute(
        "SELECT value_text FROM session_kv WHERE session_key=? AND key=?",
        (session_key, key),
    )
    row = cur.fetchone()
    if not row:
        return default
    try:
        return json.loads(str(row[0]))
    except Exception:
        return default


def _save_ui_session(self):
    if not self._session_pg_ready:
        return
    try:
        selected_paths = []
        for one in self._selected_items():
            rel = self._to_session_rel_path_text(one.path)
            if rel:
                selected_paths.append(rel)

        valid_paths = []
        for one in sorted(self.paths, key=lambda x: str(x).casefold()):
            rel = self._to_session_rel_path_text(one)
            if rel:
                valid_paths.append(rel)

        lazy_loaded_dirs = []
        for one in sorted(self._lazy_loaded_dirs):
            rel = self._to_session_rel_path_text(one)
            if rel:
                lazy_loaded_dirs.append(rel)
        cover_hint_mode = str(getattr(self, "_cover_gallery_hint_mode", "auto") or "auto").strip().lower()
        if cover_hint_mode not in {"auto", "portrait", "landscape"}:
            cover_hint_mode = "auto"
        left_view_mode = "list"
        if hasattr(self, "nfo_left_stack"):
            try:
                left_view_mode = "list" if int(self.nfo_left_stack.currentIndex()) == 0 else "cover"
            except Exception:
                left_view_mode = "list"
        cover_icon_h_portrait = int(getattr(self, "_cover_icon_h_portrait", 267) or 267)
        cover_icon_h_landscape = int(getattr(self, "_cover_icon_h_landscape", 84) or 84)
        cover_kind_cache_raw = getattr(self, "_cover_kind_cache", {})
        cover_kind_cache: dict[str, str] = {}
        if isinstance(cover_kind_cache_raw, dict):
            for raw_root, raw_kind in cover_kind_cache_raw.items():
                rel_root = self._to_session_rel_path_text(raw_root)
                kind = str(raw_kind or "").strip().lower()
                if rel_root and kind in {"portrait", "landscape"}:
                    cover_kind_cache[rel_root] = kind

        session_items_rows: list[tuple[str, str, str, str]] = []
        for one in self.items:
            rel = self._to_session_rel_path_text(one.path)
            if not rel:
                continue
            session_items_rows.append((self._session_pg_key, rel, one.media_type, one.display))

        # 跨设备挂载失效后，本次可能是“空会话启动”；避免回写把已缓存条目清空。
        if bool(getattr(self, "_session_skip_empty_save_once", False)) and (not valid_paths) and (not session_items_rows):
            self._log("检测到空会话回写保护：保留既有会话缓存，不覆盖数据库。")
            return

        con = self._pg_connect()
        if con is None:
            return
        try:
            cur = con.cursor()
            self._pg_ensure_tables(cur)
            self._pg_set_kv(cur, "paths", valid_paths)
            self._pg_set_kv(cur, "selected_nfos", selected_paths)
            self._pg_set_kv(cur, "lazy_loaded_dirs", lazy_loaded_dirs)
            self._pg_set_kv(cur, "left_view_mode", left_view_mode)
            self._pg_set_kv(cur, "cover_gallery_hint_mode", cover_hint_mode)
            self._pg_set_kv(cur, "cover_icon_h_portrait", cover_icon_h_portrait)
            self._pg_set_kv(cur, "cover_icon_h_landscape", cover_icon_h_landscape)
            self._pg_set_kv(cur, "cover_kind_cache", cover_kind_cache)
            cur.execute("DELETE FROM session_items WHERE session_key=?", (self._session_pg_key,))
            if session_items_rows:
                cur.executemany(
                    "INSERT INTO session_items(session_key, path, media_type, display, updated_at) VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    session_items_rows,
                )
            con.commit()
            self._session_skip_empty_save_once = False
        finally:
            con.close()
    except Exception as exc:
        self._log(f"保存会话失败: {exc}")


def _restore_ui_session(self):
    if not self._session_pg_ready:
        return
    raw_paths = []
    raw_selected = []
    raw_lazy_dirs = []
    left_view_mode = "list"
    cover_hint_mode = "auto"
    cover_icon_h_portrait = 267
    cover_icon_h_landscape = 84
    cover_kind_cache = {}
    cached_items: list[NfoItem] = []
    try:
        con = self._pg_connect()
        if con is None:
            return
        try:
            cur = con.cursor()
            self._pg_ensure_tables(cur)
            active_key = str(getattr(self, "_session_pg_key", "default") or "default")
            candidate_keys = [active_key]
            if active_key != "default":
                candidate_keys.append("default")
            rows = []
            used_key = active_key
            for one_key in candidate_keys:
                cand_paths = _pg_get_kv_by_session_key(cur, one_key, "paths", [])
                cand_selected = _pg_get_kv_by_session_key(cur, one_key, "selected_nfos", [])
                cand_lazy = _pg_get_kv_by_session_key(cur, one_key, "lazy_loaded_dirs", [])
                cand_left_mode = _pg_get_kv_by_session_key(cur, one_key, "left_view_mode", "list")
                cand_hint_mode = _pg_get_kv_by_session_key(cur, one_key, "cover_gallery_hint_mode", "auto")
                cand_icon_h_portrait = _pg_get_kv_by_session_key(cur, one_key, "cover_icon_h_portrait", 267)
                cand_icon_h_landscape = _pg_get_kv_by_session_key(cur, one_key, "cover_icon_h_landscape", 84)
                cand_kind_cache = _pg_get_kv_by_session_key(cur, one_key, "cover_kind_cache", {})
                cur.execute("SELECT path, media_type, display FROM session_items WHERE session_key=?", (one_key,))
                cand_rows = cur.fetchall()
                has_payload = bool(cand_rows) or bool(cand_paths) or bool(cand_selected) or bool(cand_lazy)
                if not has_payload:
                    continue
                used_key = one_key
                raw_paths = cand_paths
                raw_selected = cand_selected
                raw_lazy_dirs = cand_lazy
                left_view_mode = cand_left_mode
                cover_hint_mode = cand_hint_mode
                cover_icon_h_portrait = cand_icon_h_portrait
                cover_icon_h_landscape = cand_icon_h_landscape
                cover_kind_cache = cand_kind_cache
                rows = cand_rows
                break
            if used_key != active_key:
                self._log(f"会话恢复使用兼容键: {used_key}")
            has_legacy_abs = any(_is_absolute_path_text(x) for x in raw_paths)
            has_legacy_abs = has_legacy_abs or any(_is_absolute_path_text(x) for x in raw_selected)
            has_legacy_abs = has_legacy_abs or any(_is_absolute_path_text(x) for x in raw_lazy_dirs)
            has_legacy_abs = has_legacy_abs or any(_is_absolute_path_text((r[0] if len(r) > 0 else "")) for r in rows)
            if has_legacy_abs:
                self._session_skip_empty_save_once = True
                self._log("检测到旧版绝对路径会话数据，已忽略本次会话恢复（保留缓存条目）。")
                return
            decoded_roots = []
            for raw in raw_paths:
                p = self._from_session_rel_path_text(raw)
                if p is not None:
                    decoded_roots.append(p)
            valid_roots = [p for p in decoded_roots if _path_exists_for_restore(p)]
            # NAS 映射盘符变化时，会话里可能只剩无效路径；直接清库避免残留数据污染界面。
            if decoded_roots and (not valid_roots):
                self._session_skip_empty_save_once = True
                self._log("检测到会话路径全部失效，已清空当前恢复状态（保留缓存条目供后续复用）。")
                return
            for row in rows:
                try:
                    p = self._from_session_rel_path_text(row[0])
                    mt = str(row[1] or "").strip()
                    disp = str(row[2] or "").strip()
                except Exception:
                    continue
                if p is None:
                    continue
                if not mt:
                    continue
                if not disp:
                    disp = f"[{mt}] {p.parent.name}/{p.name}"
                cached_items.append(NfoItem(path=p, media_type=mt, display=disp))
            con.commit()
        finally:
            con.close()
    except Exception as exc:
        self._log(f"读取会话失败: {exc}")
        return

    if not isinstance(raw_paths, list):
        raw_paths = []
    if not isinstance(raw_selected, list):
        raw_selected = []
    if not isinstance(raw_lazy_dirs, list):
        raw_lazy_dirs = []
    left_view_mode_txt = str(left_view_mode or "list").strip().lower()
    if left_view_mode_txt not in {"list", "cover"}:
        left_view_mode_txt = "list"
    cover_hint_mode_txt = str(cover_hint_mode or "auto").strip().lower()
    if cover_hint_mode_txt not in {"auto", "portrait", "landscape"}:
        cover_hint_mode_txt = "auto"
    self._cover_gallery_hint_mode = cover_hint_mode_txt
    try:
        self._cover_icon_h_portrait = max(56, int(cover_icon_h_portrait))
    except Exception:
        self._cover_icon_h_portrait = 267
    try:
        self._cover_icon_h_landscape = max(56, int(cover_icon_h_landscape))
    except Exception:
        self._cover_icon_h_landscape = 84
    resolved_kind_cache: dict[str, str] = {}
    if isinstance(cover_kind_cache, dict):
        for raw_root, raw_kind in cover_kind_cache.items():
            p = self._from_session_rel_path_text(raw_root)
            kind = str(raw_kind or "").strip().lower()
            if p is None or kind not in {"portrait", "landscape"}:
                continue
            if not _path_exists_for_restore(p):
                continue
            resolved_kind_cache[str(p).casefold()] = kind
    self._cover_kind_cache = resolved_kind_cache
    for raw in raw_paths:
        p = self._from_session_rel_path_text(raw)
        if p is None:
            continue
        if not _path_exists_for_restore(p):
            continue
        self.paths.add(p)
    self._pending_restore_selected_paths = {
        str(p).casefold()
        for p in (self._from_session_rel_path_text(x) for x in raw_selected)
        if p is not None and _path_exists_for_restore(p)
    }
    self._pending_restore_lazy_dirs = {
        str(p).casefold()
        for p in (self._from_session_rel_path_text(x) for x in raw_lazy_dirs)
        if p is not None and _path_exists_for_restore(p)
    }
    if self.paths and cached_items:
        valid_roots = list(self.paths)
        self.items = []
        seen: set[str] = set()
        for item in cached_items:
            one_path = item.path
            in_roots = any((one_path == root) or (root in one_path.parents) for root in valid_roots)
            if not in_roots:
                continue
            key = str(one_path).casefold()
            if key in seen:
                continue
            seen.add(key)
            self.items.append(NfoItem(path=one_path, media_type=item.media_type, display=item.display))
        self._build_item_tree()
        self._update_scan_stats_label()
        # 由会话缓存恢复时，self.items 已包含已加载节点，避免再次触发延迟加载导致启动变慢。
        self._restore_scan_tree_state(restore_lazy_dirs=False)
        self._log(f"已从会话缓存恢复 NFO 树：{len(self.items)} 条。")
    elif self.paths:
        self.refresh_items()

    # 恢复退出前左侧模式（列表/图表）。
    if hasattr(self, "_switch_left_nfo_view"):
        try:
            self._switch_left_nfo_view(left_view_mode_txt)
        except Exception:
            pass


def bind_session_pg_methods(cls):
    cls._session_base_dir = _session_base_dir
    cls._build_device_session_key = staticmethod(_build_device_session_key)
    cls._to_session_rel_path_text = _to_session_rel_path_text
    cls._from_session_rel_path_text = _from_session_rel_path_text
    cls._clear_session_records = _clear_session_records
    cls._ensure_pg_driver = _ensure_pg_driver
    cls._auto_configure_pg_session = _auto_configure_pg_session
    cls._pg_connect = _pg_connect
    cls._pg_ensure_tables = _pg_ensure_tables
    cls._ensure_pg_session_schema = _ensure_pg_session_schema
    cls._pg_set_kv = _pg_set_kv
    cls._pg_get_kv = _pg_get_kv
    cls._pg_get_kv_by_session_key = staticmethod(_pg_get_kv_by_session_key)
    cls._save_ui_session = _save_ui_session
    cls._restore_ui_session = _restore_ui_session
    cls._shutdown_embedded_pg = _shutdown_embedded_pg
