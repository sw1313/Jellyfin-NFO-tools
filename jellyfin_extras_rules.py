import fnmatch
from dataclasses import dataclass
from pathlib import Path


EXTRAS_FOLDERS = {
    "behind the scenes",
    "deleted scenes",
    "interviews",
    "scenes",
    "samples",
    "shorts",
    "featurettes",
    "clips",
    "other",
    "extras",
    "trailers",
    "theme-music",
    "backdrops",
}

EXTRAS_SUFFIXES = {
    "-trailer",
    ".trailer",
    "_trailer",
    " trailer",
    "-sample",
    ".sample",
    "_sample",
    " sample",
    "-scene",
    "-clip",
    "-interview",
    "-behindthescenes",
    "-deleted",
    "-deletedscene",
    "-featurette",
    "-short",
    "-other",
    "-extra",
}

VIDEO_EXTS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".m4v",
    ".ts",
    ".webm",
    ".mpg",
    ".mpeg",
}

AUDIO_EXTS = {
    ".mp3",
    ".flac",
    ".m4a",
    ".aac",
    ".wav",
    ".ogg",
    ".opus",
    ".mka",
}

IMAGE_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".avif",
}

# 来自 Jellyfin 文档 Metadata Images 表
IMAGE_NAME_ALIASES = {
    "primary": {"poster", "folder", "cover", "default", "movie", "show", "jacket"},
    "backdrop": {"backdrop", "fanart", "background", "art"},
    "banner": {"banner"},
    "logo": {"logo", "clearlogo"},
    "thumb": {"landscape", "thumb"},
    "disc": {"disc", "cdart", "discart"},
    "clearart": {"clearart"},
}


@dataclass
class ExtraSuggestion:
    source: Path
    suggested_name: str
    reason: str


@dataclass
class ExtraResource:
    path: Path
    kind: str
    detail: str


SUPPORTED_EXTRA_UPLOAD_TARGETS = [
    ("poster", "主图 poster"),
    ("folder", "主图 folder"),
    ("cover", "主图 cover"),
    ("default", "主图 default"),
    ("fanart", "背景图 fanart"),
    ("backdrop", "背景图 backdrop"),
    ("banner", "横幅 banner"),
    ("logo", "标识 logo"),
    ("clearlogo", "透明标识 clearlogo"),
    ("landscape", "缩略图 landscape"),
    ("thumb", "缩略图 thumb"),
    ("disc", "光盘图 disc"),
    ("cdart", "光盘图 cdart"),
    ("discart", "光盘图 discart"),
    ("clearart", "图像 clearart"),
    ("extras_folder_trailers", "额外内容文件夹 trailers"),
    ("extras_folder_samples", "额外内容文件夹 samples"),
    ("extras_folder_interviews", "额外内容文件夹 interviews"),
    ("extras_folder_behind the scenes", "额外内容文件夹 behind the scenes"),
    ("extras_folder_deleted scenes", "额外内容文件夹 deleted scenes"),
    ("extras_folder_scenes", "额外内容文件夹 scenes"),
    ("extras_folder_shorts", "额外内容文件夹 shorts"),
    ("extras_folder_featurettes", "额外内容文件夹 featurettes"),
    ("extras_folder_clips", "额外内容文件夹 clips"),
    ("extras_folder_other", "额外内容文件夹 other"),
    ("extras_folder_extras", "额外内容文件夹 extras"),
    ("extras_folder_theme-music", "额外内容文件夹 theme-music"),
    ("extras_folder_backdrops", "额外内容文件夹 backdrops"),
    ("suffix_trailer", "后缀型 trailer"),
    ("suffix_sample", "后缀型 sample"),
    ("suffix_scene", "后缀型 scene"),
    ("suffix_clip", "后缀型 clip"),
    ("suffix_interview", "后缀型 interview"),
    ("suffix_behindthescenes", "后缀型 behindthescenes"),
    ("suffix_deleted", "后缀型 deleted"),
    ("suffix_deletedscene", "后缀型 deletedscene"),
    ("suffix_featurette", "后缀型 featurette"),
    ("suffix_short", "后缀型 short"),
    ("suffix_other", "后缀型 other"),
    ("suffix_extra", "后缀型 extra"),
]

# 依据 Jellyfin 文档语义：
# - 明确文件名类型（poster/fanart/logo/...）通常是单项资源（单文件）
# - extras 文件夹与后缀型 extras 允许存在多个资源
MULTI_FILE_EXTRA_UPLOAD_TARGETS = {
    "extras_folder_trailers",
    "extras_folder_samples",
    "extras_folder_interviews",
    "extras_folder_behind the scenes",
    "extras_folder_deleted scenes",
    "extras_folder_scenes",
    "extras_folder_shorts",
    "extras_folder_featurettes",
    "extras_folder_clips",
    "extras_folder_other",
    "extras_folder_extras",
    "extras_folder_theme-music",
    "extras_folder_backdrops",
    "suffix_trailer",
    "suffix_sample",
    "suffix_scene",
    "suffix_clip",
    "suffix_interview",
    "suffix_behindthescenes",
    "suffix_deleted",
    "suffix_deletedscene",
    "suffix_featurette",
    "suffix_short",
    "suffix_other",
    "suffix_extra",
}


def target_supports_multiple(upload_target: str) -> bool:
    return upload_target in MULTI_FILE_EXTRA_UPLOAD_TARGETS


def detect_extra_suffix(stem: str) -> str | None:
    s = stem.lower()
    for suffix in EXTRAS_SUFFIXES:
        if s.endswith(suffix):
            return suffix
    return None


def parse_ignore_file(ignore_path: Path) -> tuple[bool, list[str]]:
    if not ignore_path.exists():
        return False, []
    try:
        content = ignore_path.read_text(encoding="utf-8").strip()
    except Exception:
        return False, []
    if not content:
        return True, []
    patterns = []
    for line in content.splitlines():
        one = line.strip()
        if not one or one.startswith("#"):
            continue
        patterns.append(one)
    return False, patterns


def is_path_ignored(path: Path, library_root: Path) -> bool:
    path = path.resolve()
    library_root = library_root.resolve()

    for parent in [path] + list(path.parents):
        if parent == library_root.parent:
            break
        ignore_path = parent / ".ignore"
        ignore_all, _ = parse_ignore_file(ignore_path)
        if ignore_all and parent != path:
            try:
                path.relative_to(parent)
                return True
            except Exception:
                pass

    for parent in [library_root] + list(library_root.rglob("*")):
        if not parent.is_dir():
            continue
        ignore_path = parent / ".ignore"
        ignore_all, patterns = parse_ignore_file(ignore_path)
        if ignore_all:
            continue
        if not patterns:
            continue
        try:
            rel = path.relative_to(parent).as_posix()
        except Exception:
            continue
        for pattern in patterns:
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(path.name, pattern):
                return True
    return False


def collect_media_files(paths: set[Path]) -> list[Path]:
    files: set[Path] = set()
    for p in paths:
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            files.add(p.resolve())
            continue
        if p.is_dir():
            root = p.resolve()
            for f in root.rglob("*"):
                if not f.is_file():
                    continue
                if f.suffix.lower() not in VIDEO_EXTS:
                    continue
                if is_path_ignored(f, root):
                    continue
                files.add(f.resolve())
    return sorted(files, key=lambda x: str(x).lower())


def suggest_extras_normalization(media_files: list[Path]) -> list[ExtraSuggestion]:
    suggestions: list[ExtraSuggestion] = []
    for f in media_files:
        suffix = detect_extra_suffix(f.stem)
        if suffix is None:
            continue
        base = f.stem[: -len(suffix)].rstrip(" .-_")
        normalized = f"{base}{suffix}{f.suffix.lower()}"
        if normalized != f.name:
            suggestions.append(
                ExtraSuggestion(
                    source=f,
                    suggested_name=normalized,
                    reason=f"后缀规范化为 {suffix}",
                )
            )
    return suggestions


def scan_extra_resources(paths: set[Path]) -> list[ExtraResource]:
    resources: list[ExtraResource] = []
    for p in paths:
        if not p.exists():
            continue
        if p.is_file():
            roots = [p.parent]
        else:
            roots = [p]
        for root in roots:
            root = root.resolve()
            for f in root.rglob("*"):
                if not f.is_file():
                    continue
                if is_path_ignored(f, root):
                    continue

                ext = f.suffix.lower()
                stem = f.stem.lower()
                name_l = f.name.lower()

                # extras 文件夹类型
                for folder_name in EXTRAS_FOLDERS:
                    folder_l = folder_name.lower()
                    if folder_l in [x.lower() for x in f.parts]:
                        resources.append(ExtraResource(path=f.resolve(), kind="extras_folder", detail=folder_name))
                        break

                # 图片命名族
                if ext in IMAGE_EXTS:
                    for kind, aliases in IMAGE_NAME_ALIASES.items():
                        if stem in aliases:
                            resources.append(ExtraResource(path=f.resolve(), kind=f"image_{kind}", detail=stem))
                            break
                    # 允许前缀式命名 movie-logo / show-poster
                    for kind, aliases in IMAGE_NAME_ALIASES.items():
                        if any(name_l.startswith(f"{a}-") or name_l.startswith(f"{a}_") for a in aliases):
                            resources.append(ExtraResource(path=f.resolve(), kind=f"image_{kind}", detail="prefix-style"))
                            break

                # 后缀型 extras
                if ext in VIDEO_EXTS | AUDIO_EXTS:
                    suf = detect_extra_suffix(stem)
                    if suf is not None:
                        resources.append(ExtraResource(path=f.resolve(), kind="extras_suffix", detail=suf))

                # 特殊文件名 extras
                if stem in {"trailer", "sample", "theme"}:
                    resources.append(ExtraResource(path=f.resolve(), kind="extras_special_filename", detail=stem))

                # extrafanart 文件夹
                if "extrafanart" in [x.lower() for x in f.parts]:
                    resources.append(ExtraResource(path=f.resolve(), kind="image_backdrop", detail="extrafanart"))
    return sorted(resources, key=lambda x: str(x.path).lower())


def build_extra_target_name(upload_target: str, source_file: Path) -> tuple[str, str]:
    """
    return: (relative_dir, filename)
    """
    ext = source_file.suffix.lower()
    if upload_target.startswith("extras_folder_"):
        folder = upload_target.replace("extras_folder_", "", 1)
        return folder, source_file.name
    if upload_target.startswith("suffix_"):
        suffix = upload_target.replace("suffix_", "", 1)
        stem = source_file.stem
        return "", f"{stem}-{suffix}{ext}"
    return "", f"{upload_target}{ext}"

