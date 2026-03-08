import os
import re
import shutil
import xml.etree.ElementTree as ET
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path

from jellyfin_media_naming import build_image_filename
from jellyfin_extras_rules import EXTRAS_FOLDERS

KNOWN_NFO_BASENAMES = {
    "tvshow.nfo": "tvshow",
    "season.nfo": "season",
    "movie.nfo": "movie",
    "video_ts.nfo": "movie",
    "artist.nfo": "artist",
    "album.nfo": "album",
}

# 兼容常见季目录命名：Season 1 / S01 / S01_xxx / 第1季
SEASON_DIR_RE = re.compile(r"^(season\s*\d+|s\d{1,2}|第\s*\d+\s*季)(?:\b.*)?$", re.IGNORECASE)
DISCOVERY_VIDEO_EXTS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".m4v",
    ".webm",
    ".mpeg",
    ".mpg",
    ".ts",
}
SCAN_CACHE_VERSION = 6
SCAN_CACHE_FILE = Path(__file__).with_name(".nfo_scan_index.json")
SESSION_SQLITE_FILE = Path(__file__).with_name(".jellyfin_qt_session.sqlite3")
SCAN_CACHE_TABLE = "scan_cache_roots"

FIELD_DEFINITIONS: list[tuple[str, str]] = [
    ("title", "标题"),
    ("plot", "概述"),
    ("id", "标识（常用于 IMDb/TVDb）"),
    ("originaltitle", "原始标题"),
    ("sorttitle", "排序标题"),
    ("seasonnumber", "季序号（仅 season）"),
    ("showtitle", "剧集标题（仅 episode）"),
    ("episode", "集序号（仅 episode）"),
    ("season", "季序号（仅 episode）"),
    ("aired", "播出日期（仅 episode）"),
    ("airsafter_season", "在第几季后播出（仅 episode）"),
    ("airsbefore_episode", "在第几集前播出（仅 episode）"),
    ("airsbefore_season", "在第几季前播出（仅 episode）"),
    ("displayepisode", "显示集序号（仅 episode）"),
    ("displayseason", "显示季序号（仅 episode）"),
    ("director", "导演（多值）"),
    ("writer", "编剧（多值）"),
    ("credits", "演职人员（多值）"),
    ("trailer", "预告片链接"),
    ("rating", "评分"),
    ("year", "年份"),
    ("mpaa", "分级"),
    ("aspectratio", "纵横比"),
    ("dateadded", "添加日期"),
    ("collectionnumber", "系列编号（TMDb）"),
    ("set", "系列（仅 movie）"),
    ("imdb_id", "IMDb 标识（仅 tvshow）"),
    ("imdbid", "IMDb 标识（非 tvshow）"),
    ("tvdbid", "TVDb 标识"),
    ("tmdbid", "TMDb 标识"),
    ("premiered", "首映日期"),
    ("releasedate", "发布日期"),
    ("enddate", "结束日期"),
    ("language", "语言"),
    ("criticrating", "评论家评分"),
    ("runtime", "时长"),
    ("countrycode", "国家/地区代码"),
    ("zap2itid", "Zap2it 标识"),
    ("tvrageid", "TVRage 标识"),
    ("formed", "成立日期（音乐人）"),
    ("disbanded", "解散日期（音乐人）"),
    ("audiodbartistid", "AudioDB 艺术家标识"),
    ("audiodbalbumid", "AudioDB 专辑标识"),
    ("musicbrainzartistid", "MusicBrainz 艺术家标识"),
    ("musicbrainzalbumartistid", "MusicBrainz 专辑艺术家标识"),
    ("musicbrainzalbumid", "MusicBrainz 专辑标识"),
    ("musicbrainzreleasegroupid", "MusicBrainz 发行组标识"),
    ("tag", "标签（可多值）"),
    ("genre", "类型（可多值）"),
    ("studio", "工作室（可多值）"),
    ("country", "国家/地区（可多值）"),
]

MULTI_VALUE_TAGS = {
    "tag",
    "genre",
    "studio",
    "director",
    "writer",
    "credits",
    "country",
}

ALL_TAGS = [item[0] for item in FIELD_DEFINITIONS]
DATE_ONLY_FIELDS = {"premiered", "releasedate", "enddate", "formed", "disbanded", "aired"}
DATE_UTC_FIELDS = {"dateadded"}
RICH_TEXT_TAGS = {"plot"}

COMMON_WRITABLE = {
    "title",
    "plot",
    "id",
    "originaltitle",
    "director",
    "writer",
    "credits",
    "trailer",
    "rating",
    "year",
    "sorttitle",
    "mpaa",
    "aspectratio",
    "dateadded",
    "tvdbid",
    "tmdbid",
    "language",
    "countrycode",
    "premiered",
    "enddate",
    "releasedate",
    "criticrating",
    "runtime",
    "country",
    "genre",
    "studio",
    "tvrageid",
}

WRITABLE_BY_MEDIA_TYPE: dict[str, set[str]] = {
    "movie": COMMON_WRITABLE | {"set", "collectionnumber", "imdbid"},
    "movie_or_video_item": COMMON_WRITABLE | {"set", "collectionnumber", "imdbid"},
    "tvshow": COMMON_WRITABLE | {"imdb_id", "zap2itid"},
    "season": COMMON_WRITABLE | {"seasonnumber"},
    "episode": COMMON_WRITABLE
    | {
        "showtitle",
        "episode",
        "season",
        "aired",
        "airsafter_season",
        "airsbefore_episode",
        "airsbefore_season",
        "displayepisode",
        "displayseason",
        "imdbid",
    },
    "artist": COMMON_WRITABLE
    | {
        "formed",
        "disbanded",
        "audiodbartistid",
        "musicbrainzartistid",
    },
    "album": COMMON_WRITABLE
    | {
        "audiodbalbumid",
        "musicbrainzalbumid",
        "musicbrainzalbumartistid",
        "musicbrainzreleasegroupid",
    },
}


@dataclass
class NfoItem:
    path: Path
    media_type: str
    display: str


def split_multi_values(raw: str) -> list[str]:
    parts = re.split(r"[,\n;/|，；、]+", raw.strip())
    values: list[str] = []
    seen: set[str] = set()
    for part in parts:
        value = part.strip()
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        values.append(value)
    return values


def natural_key(text: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def has_tvshow_ancestor(file: Path) -> bool:
    cur = file.parent
    for _ in range(10):
        if (cur / "tvshow.nfo").exists():
            return True
        if cur.parent == cur:
            break
        cur = cur.parent
    return False


def has_season_ancestor(file: Path) -> bool:
    cur = file.parent
    for _ in range(10):
        if SEASON_DIR_RE.match(cur.name or ""):
            return True
        if cur.parent == cur:
            break
        cur = cur.parent
    return False


def classify_nfo(path: Path) -> str:
    base = path.name.lower()
    if base in KNOWN_NFO_BASENAMES:
        return KNOWN_NFO_BASENAMES[base]
    if has_tvshow_ancestor(path) or has_season_ancestor(path):
        return "episode"
    return "movie_or_video_item"


def _episode_nfo_has_same_stem_video(path: Path) -> bool:
    """仅保留与同名视频共存的 episode nfo，过滤重命名残留。"""
    try:
        nfo = path.resolve()
    except Exception:
        nfo = path
    if nfo.suffix.lower() != ".nfo":
        return False
    parent = nfo.parent
    if not parent.exists():
        return False
    stem_cf = nfo.stem.casefold()
    try:
        with os.scandir(parent) as it:
            for ent in it:
                try:
                    if not ent.is_file():
                        continue
                except Exception:
                    continue
                p = Path(ent.name)
                if p.suffix.lower() not in DISCOVERY_VIDEO_EXTS:
                    continue
                if p.stem.casefold() == stem_cf:
                    return True
    except Exception:
        return False
    return False


def _should_keep_nfo_item(path: Path, media_type: str) -> bool:
    base = path.name.lower()
    if base in KNOWN_NFO_BASENAMES:
        return True
    if media_type not in {"episode", "movie_or_video_item"}:
        return True
    return _episode_nfo_has_same_stem_video(path)


def collect_nfo_items(paths: set[Path], progress_cb=None, quick_scan: bool = False, max_depth: int | None = None) -> list[NfoItem]:
    extras_folders_lower = {x.lower() for x in EXTRAS_FOLDERS}

    def _is_extras_dir(dir_path: Path) -> bool:
        return (dir_path.name or "").strip().lower() in extras_folders_lower

    def _classify_with_virtual(path: Path, tvshow_dirs: set[Path], season_dirs: set[Path]) -> str:
        base = path.name.lower()
        if base in KNOWN_NFO_BASENAMES:
            return KNOWN_NFO_BASENAMES[base]
        cur = path.parent
        for _ in range(10):
            if cur in season_dirs or cur in tvshow_dirs:
                return "episode"
            if SEASON_DIR_RE.match(cur.name or ""):
                return "episode"
            if cur.parent == cur:
                break
            cur = cur.parent
        return "movie_or_video_item"

    def _connect_scan_cache_db():
        con = sqlite3.connect(str(SESSION_SQLITE_FILE), timeout=8, check_same_thread=False)
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=8000")
        con.execute("PRAGMA temp_store=MEMORY")
        return con

    def _ensure_scan_cache_table(cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCAN_CACHE_TABLE} (
                root_key TEXT NOT NULL,
                sig TEXT NOT NULL,
                items_json TEXT NOT NULL,
                cache_version INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(root_key)
            )
            """
        )

    def _migrate_legacy_scan_cache_if_needed(cur):
        if not SCAN_CACHE_FILE.exists():
            return
        try:
            legacy = json.loads(SCAN_CACHE_FILE.read_text(encoding="utf-8"))
            roots = legacy.get("roots", {}) if isinstance(legacy, dict) else {}
            if not isinstance(roots, dict):
                roots = {}
            for root_key, entry in roots.items():
                if not isinstance(entry, dict):
                    continue
                sig = str(entry.get("sig") or "")
                items = entry.get("items", [])
                if not isinstance(items, list):
                    items = []
                cur.execute(
                    f"""
                    INSERT INTO {SCAN_CACHE_TABLE}(root_key, sig, items_json, cache_version, updated_at)
                    VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(root_key)
                    DO UPDATE SET
                        sig=excluded.sig,
                        items_json=excluded.items_json,
                        cache_version=excluded.cache_version,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (str(root_key), sig, json.dumps(items, ensure_ascii=False), SCAN_CACHE_VERSION),
                )
            # 迁移成功后删除旧 JSON，避免后续双轨状态不一致。
            SCAN_CACHE_FILE.unlink(missing_ok=True)
        except Exception:
            # 迁移失败时保留旧文件，保证可回退。
            return

    def _load_scan_cache() -> dict:
        try:
            con = _connect_scan_cache_db()
            try:
                cur = con.cursor()
                _ensure_scan_cache_table(cur)
                _migrate_legacy_scan_cache_if_needed(cur)
                con.commit()
                cur.execute(
                    f"SELECT root_key, sig, items_json FROM {SCAN_CACHE_TABLE} WHERE cache_version=?",
                    (SCAN_CACHE_VERSION,),
                )
                rows = cur.fetchall()
            finally:
                con.close()
            roots: dict[str, dict] = {}
            for root_key, sig, items_json in rows:
                try:
                    items = json.loads(str(items_json))
                    if not isinstance(items, list):
                        items = []
                except Exception:
                    items = []
                roots[str(root_key)] = {"sig": str(sig or ""), "items": items}
            return {"version": SCAN_CACHE_VERSION, "roots": roots}
        except Exception:
            return {"version": SCAN_CACHE_VERSION, "roots": {}}

    def _save_scan_cache(cache_data: dict):
        try:
            roots = cache_data.get("roots", {}) if isinstance(cache_data, dict) else {}
            if not isinstance(roots, dict):
                roots = {}
            con = _connect_scan_cache_db()
            try:
                cur = con.cursor()
                _ensure_scan_cache_table(cur)
                for root_key, entry in roots.items():
                    if not isinstance(entry, dict):
                        continue
                    sig = str(entry.get("sig") or "")
                    items = entry.get("items", [])
                    if not isinstance(items, list):
                        items = []
                    cur.execute(
                        f"""
                        INSERT INTO {SCAN_CACHE_TABLE}(root_key, sig, items_json, cache_version, updated_at)
                        VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(root_key)
                        DO UPDATE SET
                            sig=excluded.sig,
                            items_json=excluded.items_json,
                            cache_version=excluded.cache_version,
                            updated_at=CURRENT_TIMESTAMP
                        """,
                        (str(root_key), sig, json.dumps(items, ensure_ascii=False), SCAN_CACHE_VERSION),
                    )
                con.commit()
            finally:
                con.close()
        except Exception:
            pass

    def _fast_root_signature(root: Path) -> str:
        parts: list[str] = []
        try:
            st = root.stat()
            parts.append(f"r:{st.st_mtime_ns}:{st.st_size}")
        except Exception:
            parts.append("r:na")
        try:
            sampled: list[str] = []
            with os.scandir(root) as it:
                for idx, ent in enumerate(it):
                    if idx >= 256:
                        break
                    try:
                        est = ent.stat(follow_symlinks=False)
                        sampled.append(f"{ent.name.lower()}:{int(ent.is_dir(follow_symlinks=False))}:{est.st_mtime_ns}:{est.st_size}")
                    except Exception:
                        sampled.append(f"{ent.name.lower()}:na")
            sampled.sort()
            parts.extend(sampled)
        except Exception:
            parts.append("scan:na")
        return "|".join(parts)

    def _scan_dir_shallow(cur_dir: Path, root: Path, is_path_ignored) -> set[Path]:
        local_found: set[Path] = set()
        lower_files: set[str] = set()
        has_video_in_dir = False
        video_stems: set[str] = set()
        child_dir_names: list[str] = []
        scanned_entries = 0
        probe_limit = 320
        try:
            with os.scandir(cur_dir) as it:
                for ent in it:
                    scanned_entries += 1
                    if scanned_entries > probe_limit:
                        break
                    name = ent.name
                    low = name.lower()
                    try:
                        if ent.is_dir(follow_symlinks=False):
                            child_dir_names.append(name)
                            continue
                    except Exception:
                        continue
                    if low.endswith(".nfo"):
                        # 极速浅扫阶段：不做 ignore 规则判断，避免网络盘多余 I/O
                        nfo_path = cur_dir / name
                        local_found.add(nfo_path)
                        lower_files.add(low)
                        continue
                    lower_files.add(low)
                    if low.endswith(tuple(DISCOVERY_VIDEO_EXTS)):
                        has_video_in_dir = True
                        video_stems.add(Path(name).stem.casefold())
        except Exception:
            return local_found

        has_season_children = any(SEASON_DIR_RE.match(d or "") for d in child_dir_names)
        is_season_dir = SEASON_DIR_RE.match(cur_dir.name or "") is not None

        if has_season_children and "tvshow.nfo" not in lower_files:
            local_found.add(cur_dir / "tvshow.nfo")
        if is_season_dir and "season.nfo" not in lower_files:
            local_found.add(cur_dir / "season.nfo")
        if (
            has_video_in_dir
            and (not has_season_children)
            and (not is_season_dir)
            and ("movie.nfo" not in lower_files)
            and (not _is_extras_dir(cur_dir))
        ):
            local_found.add(cur_dir / "movie.nfo")
        # 目录存在视频但缺少同名 nfo 时，补充“同名虚拟 nfo”便于逐视频编辑。
        if has_video_in_dir and (not has_season_children) and (not _is_extras_dir(cur_dir)):
            for stem in sorted(video_stems):
                nfo_name = f"{stem}.nfo"
                if nfo_name not in lower_files:
                    local_found.add(cur_dir / nfo_name)
        return local_found

    def _scan_one(input_path: Path) -> set[Path]:
        local_found: set[Path] = set()
        if input_path.is_dir():
            root = input_path.resolve()
            try:
                from jellyfin_extras_rules import is_path_ignored
            except Exception:
                is_path_ignored = None

            # 快速浅扫（深度 1）走并行：root + 一级子目录并发，适合网络盘大目录。
            if max_depth == 1:
                dirs_to_scan: list[Path] = [root]
                try:
                    with os.scandir(root) as it:
                        for ent in it:
                            try:
                                if ent.is_dir(follow_symlinks=False):
                                    dirs_to_scan.append(root / ent.name)
                            except Exception:
                                continue
                except Exception:
                    pass

                total_dirs = len(dirs_to_scan)
                if total_dirs <= 0:
                    return local_found

                done = 0
                # 网络盘高并发会抖动，适当收敛并发通常更快
                max_workers = min(6, max(1, total_dirs))
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {executor.submit(_scan_dir_shallow, d, root, is_path_ignored): d for d in dirs_to_scan}
                    for future in as_completed(futures):
                        done += 1
                        cur_dir = futures[future]
                        if callable(progress_cb):
                            try:
                                progress_cb(str(cur_dir), done, total_dirs)
                            except Exception:
                                pass
                        try:
                            local_found.update(future.result())
                        except Exception:
                            continue
                return local_found

            # 仅收集需要深度的目录，避免在大目录里全量递归导致慢。
            if max_depth is None:
                walk_entries = list(os.walk(root))
            else:
                walk_entries = []
                for cur, dirs, files in os.walk(root):
                    cur_dir = Path(cur)
                    rel_depth = len(cur_dir.relative_to(root).parts)
                    if rel_depth >= max_depth:
                        dirs[:] = []
                    walk_entries.append((cur, list(dirs), list(files)))
            total_dirs = len(walk_entries)
            scanned_dirs = 0
            for cur, dirs, files in walk_entries:
                cur_dir = Path(cur)
                scanned_dirs += 1
                if callable(progress_cb):
                    try:
                        progress_cb(str(cur_dir), scanned_dirs, total_dirs)
                    except Exception:
                        pass

                lower_files = {f.lower() for f in files}
                has_season_children = any(SEASON_DIR_RE.match(d or "") for d in dirs)
                is_season_dir = SEASON_DIR_RE.match(cur_dir.name or "") is not None
                video_stems = {Path(name).stem.casefold() for name in files if name.lower().endswith(tuple(DISCOVERY_VIDEO_EXTS))}
                has_video_in_dir = bool(video_stems)

                # 轻量推断目录级“应有 nfo”（不创建文件，仅用于 UI 可编辑项）
                if has_season_children and "tvshow.nfo" not in lower_files:
                    local_found.add((cur_dir / "tvshow.nfo").resolve())
                if is_season_dir and "season.nfo" not in lower_files:
                    local_found.add((cur_dir / "season.nfo").resolve())
                if (
                    has_video_in_dir
                    and (not has_season_children)
                    and (not is_season_dir)
                    and ("movie.nfo" not in lower_files)
                    and (not _is_extras_dir(cur_dir))
                ):
                    local_found.add((cur_dir / "movie.nfo").resolve())
                # 目录存在视频但缺少同名 nfo 时，补充“同名虚拟 nfo”便于逐视频编辑。
                if has_video_in_dir and (not has_season_children) and (not _is_extras_dir(cur_dir)):
                    for stem in sorted(video_stems):
                        nfo_name = f"{stem}.nfo"
                        if nfo_name not in lower_files:
                            local_found.add((cur_dir / nfo_name).resolve())

                for name in files:
                    if not name.lower().endswith(".nfo"):
                        continue
                    nfo_path = (cur_dir / name).resolve()
                    if is_path_ignored is not None:
                        try:
                            if is_path_ignored(nfo_path, root):
                                continue
                        except Exception:
                            pass
                    local_found.add(nfo_path)
        elif input_path.is_file() and input_path.suffix.lower() == ".nfo":
            local_found.add(input_path.resolve())
        return local_found

    cache = _load_scan_cache()
    roots_cache = cache.setdefault("roots", {})

    items: list[NfoItem] = []
    scan_roots: list[Path] = []
    for p in paths:
        if p.is_file() and p.suffix.lower() == ".nfo":
            media_type = classify_nfo(p)
            if not _should_keep_nfo_item(p, media_type):
                continue
            display = f"[{media_type}] {p.parent.name}/{p.name}"
            items.append(NfoItem(path=p.resolve(), media_type=media_type, display=display))
            continue
        if p.is_dir():
            root = p.resolve()
            sig = _fast_root_signature(root)
            key = str(root).casefold()
            entry = roots_cache.get(key)
            if isinstance(entry, dict) and entry.get("sig") == sig:
                cached_items = entry.get("items", [])
                if isinstance(cached_items, list):
                    for one in cached_items:
                        try:
                            pp = Path(one["path"])
                            mt = str(one["media_type"])
                            dp = str(one["display"])
                            if not _should_keep_nfo_item(pp, mt):
                                continue
                            items.append(NfoItem(path=pp, media_type=mt, display=dp))
                        except Exception:
                            continue
                    continue
            scan_roots.append(root)

    for root in scan_roots:
        found = _scan_one(root)
        tvshow_dirs = {p.parent.resolve() for p in found if p.name.lower() == "tvshow.nfo"}
        season_dirs = {p.parent.resolve() for p in found if p.name.lower() == "season.nfo"}
        root_items: list[NfoItem] = []
        for nfo in found:
            media_type = _classify_with_virtual(nfo, tvshow_dirs, season_dirs)
            if not _should_keep_nfo_item(nfo, media_type):
                continue
            display = f"[{media_type}] {nfo.parent.name}/{nfo.name}"
            root_items.append(NfoItem(path=nfo, media_type=media_type, display=display))
        items.extend(root_items)
        roots_cache[str(root).casefold()] = {
            "sig": _fast_root_signature(root),
            "items": [{"path": str(x.path), "media_type": x.media_type, "display": x.display} for x in root_items],
        }

    _save_scan_cache(cache)
    uniq: dict[str, NfoItem] = {}
    for one in items:
        uniq[str(one.path).casefold()] = one
    out = list(uniq.values())
    out.sort(key=lambda i: (i.media_type, natural_key(str(i.path).lower())))
    return out


def load_cached_nfo_items(root: Path) -> list[NfoItem] | None:
    """从 SQLite 扫描缓存直接读取指定目录的 NfoItem 列表，**不做磁盘签名校验**。

    适用于延迟加载场景，先秒级呈现缓存数据，再由后台线程校验/刷新。
    若缓存中没有该 root 的数据，返回 ``None``。
    """
    key = str(root).casefold()
    try:
        con = sqlite3.connect(str(SESSION_SQLITE_FILE), timeout=5, check_same_thread=False)
        try:
            con.execute("PRAGMA journal_mode=DELETE")
            con.execute("PRAGMA busy_timeout=5000")
            cur = con.cursor()
            cur.execute(
                f"SELECT items_json FROM {SCAN_CACHE_TABLE} WHERE root_key=? AND cache_version=?",
                (key, SCAN_CACHE_VERSION),
            )
            row = cur.fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not row:
        return None
    try:
        raw_items = json.loads(str(row[0]))
        if not isinstance(raw_items, list):
            return None
    except Exception:
        return None
    items: list[NfoItem] = []
    for one in raw_items:
        try:
            pp = Path(one["path"])
            mt = str(one["media_type"])
            dp = str(one["display"])
            if not _should_keep_nfo_item(pp, mt):
                continue
            items.append(NfoItem(path=pp, media_type=mt, display=dp))
        except Exception:
            continue
    return items if items else None


def validate_and_rescan_root(root: Path) -> list[NfoItem] | None:
    """后台线程调用：校验签名，若不一致则重新扫描并更新缓存，返回最新条目列表。

    若签名一致（无变化）返回 ``None`` 表示缓存仍有效无需刷新。
    """
    root = root.resolve()
    key = str(root).casefold()
    # ---------- 读取缓存签名 ----------
    cached_sig = ""
    try:
        con = sqlite3.connect(str(SESSION_SQLITE_FILE), timeout=5, check_same_thread=False)
        try:
            con.execute("PRAGMA journal_mode=DELETE")
            con.execute("PRAGMA busy_timeout=5000")
            cur = con.cursor()
            cur.execute(
                f"SELECT sig FROM {SCAN_CACHE_TABLE} WHERE root_key=? AND cache_version=?",
                (key, SCAN_CACHE_VERSION),
            )
            row = cur.fetchone()
            if row:
                cached_sig = str(row[0])
        finally:
            con.close()
    except Exception:
        pass
    # ---------- 计算当前磁盘签名 ----------
    parts: list[str] = []
    try:
        st = root.stat()
        parts.append(f"r:{st.st_mtime_ns}:{st.st_size}")
    except Exception:
        parts.append("r:na")
    try:
        sampled: list[str] = []
        with os.scandir(root) as it:
            for idx, ent in enumerate(it):
                if idx >= 256:
                    break
                try:
                    est = ent.stat(follow_symlinks=False)
                    sampled.append(f"{ent.name.lower()}:{int(ent.is_dir(follow_symlinks=False))}:{est.st_mtime_ns}:{est.st_size}")
                except Exception:
                    sampled.append(f"{ent.name.lower()}:na")
        sampled.sort()
        parts.extend(sampled)
    except Exception:
        parts.append("scan:na")
    current_sig = "|".join(parts)
    if cached_sig and cached_sig == current_sig:
        return None  # 缓存仍有效
    # ---------- 重新扫描 ----------
    new_items = collect_nfo_items({root}, quick_scan=True, max_depth=1)
    return new_items


def parse_nfo_fields(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    tree = ET.parse(path)
    root = tree.getroot()
    data: dict[str, str] = {}
    for tag in ALL_TAGS:
        nodes = root.findall(tag)
        if not nodes:
            continue
        if tag in MULTI_VALUE_TAGS:
            values = [((node.text or "").strip()) for node in nodes if (node.text or "").strip()]
            if values:
                data[tag] = "/".join(values)
        else:
            node = root.find(tag)
            if node is None:
                data[tag] = ""
            elif tag in RICH_TEXT_TAGS:
                data[tag] = _read_inner_xml(node).strip()
            else:
                data[tag] = (node.text or "").strip()

    alias_title = ""
    alias_plot = ""
    alias_rating = ""
    for child in list(root):
        tag = child.tag.lower().strip()
        text = (_read_inner_xml(child) if tag in RICH_TEXT_TAGS else (child.text or "")).strip()
        if not text:
            continue
        if tag in {"name", "title", "localtitle"}:
            alias_title = text
        if tag in {"plot", "biography", "review"}:
            alias_plot = text
        if tag in {"rating", "customrating"}:
            alias_rating = text
    if alias_title:
        data["title"] = alias_title
    if alias_plot:
        data["plot"] = alias_plot
    if alias_rating:
        data["rating"] = alias_rating
    return data


def upsert_single(root: ET.Element, tag: str, value: str):
    nodes = root.findall(tag)
    if nodes:
        nodes[0].text = value
        for node in nodes[1:]:
            root.remove(node)
    else:
        node = ET.SubElement(root, tag)
        node.text = value


def _read_inner_xml(node: ET.Element) -> str:
    parts: list[str] = []
    if node.text:
        parts.append(node.text)
    for child in list(node):
        parts.append(ET.tostring(child, encoding="unicode", method="xml"))
    return "".join(parts)


def upsert_single_rich(root: ET.Element, tag: str, value: str):
    nodes = root.findall(tag)
    if nodes:
        node = nodes[0]
        for extra in nodes[1:]:
            root.remove(extra)
    else:
        node = ET.SubElement(root, tag)

    for child in list(node):
        node.remove(child)
    node.text = None

    rich = value.strip()
    if not rich:
        return

    # 通过临时容器解析 inner XML，解析失败回退为纯文本。
    try:
        wrapper = ET.fromstring(f"<wrapper>{rich}</wrapper>")
    except ET.ParseError:
        node.text = rich
        return

    node.text = wrapper.text
    for child in list(wrapper):
        wrapper.remove(child)
        node.append(child)


def replace_multi(root: ET.Element, tag: str, values: list[str]):
    for node in root.findall(tag):
        root.remove(node)
    for value in values:
        node = ET.SubElement(root, tag)
        node.text = value


def write_nfo_fields(path: Path, fields: dict[str, str]):
    if path.exists():
        tree = ET.parse(path)
        root = tree.getroot()
    else:
        media_type = classify_nfo(path)
        root_tag = {
            "tvshow": "tvshow",
            "season": "season",
            "movie": "movie",
            "artist": "artist",
            "album": "album",
            "episode": "episodedetails",
        }.get(media_type, "movie")
        root = ET.Element(root_tag)
        tree = ET.ElementTree(root)
        path.parent.mkdir(parents=True, exist_ok=True)
    for tag, value in fields.items():
        clean_value = value.strip()
        if not clean_value:
            continue
        if tag in MULTI_VALUE_TAGS:
            replace_multi(root, tag, split_multi_values(clean_value))
        elif tag in RICH_TEXT_TAGS:
            upsert_single_rich(root, tag, clean_value)
        else:
            upsert_single(root, tag, clean_value)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def upsert_art_child(root: ET.Element, child_name: str, value: str):
    art = root.find("art")
    if art is None:
        art = ET.SubElement(root, "art")
    child = art.find(child_name)
    if child is None:
        child = ET.SubElement(art, child_name)
    child.text = value


def _art_child_for_kind(image_kind: str) -> str:
    mapping = {
        "primary": "poster",
        "backdrop": "fanart",
        "banner": "banner",
        "logo": "logo",
        "thumb": "thumb",
    }
    return mapping.get(image_kind, "poster")


def apply_artwork_files(
    nfo_path: Path,
    thumb_src: Path | None,
    fanart_src: Path | None,
    thumb_kind: str = "primary",
    fanart_kind: str = "backdrop",
):
    if thumb_src is None and fanart_src is None:
        return

    if nfo_path.exists():
        tree = ET.parse(nfo_path)
        root = tree.getroot()
    else:
        media_type = classify_nfo(nfo_path)
        root_tag = {
            "tvshow": "tvshow",
            "season": "season",
            "movie": "movie",
            "artist": "artist",
            "album": "album",
            "episode": "episodedetails",
        }.get(media_type, "movie")
        root = ET.Element(root_tag)
        tree = ET.ElementTree(root)
        nfo_path.parent.mkdir(parents=True, exist_ok=True)
    target_dir = nfo_path.parent
    media_type = classify_nfo(nfo_path)

    if thumb_src is not None:
        thumb_name = build_image_filename(media_type, thumb_kind, thumb_src.suffix)
        thumb_target = target_dir / thumb_name
        shutil.copy2(thumb_src, thumb_target)
        if thumb_kind in {"primary", "thumb"}:
            upsert_single(root, "thumb", thumb_name)
        if thumb_kind == "backdrop":
            upsert_single(root, "fanart", thumb_name)
        upsert_art_child(root, _art_child_for_kind(thumb_kind), thumb_name)

    if fanart_src is not None:
        fanart_name = build_image_filename(media_type, fanart_kind, fanart_src.suffix)
        fanart_target = target_dir / fanart_name
        shutil.copy2(fanart_src, fanart_target)
        if fanart_kind in {"primary", "thumb"}:
            upsert_single(root, "thumb", fanart_name)
        if fanart_kind == "backdrop":
            upsert_single(root, "fanart", fanart_name)
        upsert_art_child(root, _art_child_for_kind(fanart_kind), fanart_name)

    ET.indent(tree, space="  ")
    tree.write(nfo_path, encoding="utf-8", xml_declaration=True)


def validate_edit_values(edits: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for tag in DATE_ONLY_FIELDS:
        value = edits.get(tag, "").strip()
        if not value:
            continue
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            errors.append(f"{tag} 需为 YYYY-MM-DD 格式")
            continue
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            errors.append(f"{tag} 不是有效日期")

    for tag in DATE_UTC_FIELDS:
        value = edits.get(tag, "").strip()
        if not value:
            continue
        if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            continue
        if re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?$", value):
            continue
        errors.append(f"{tag} 日期格式不支持，允许 YYYY-MM-DD、YYYY-MM-DD HH:MM:SS、YYYY-MM-DDTHH:MM:SS(含可选时区)")
    return errors
