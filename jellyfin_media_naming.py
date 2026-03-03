from dataclasses import dataclass
from pathlib import Path


MEDIA_TYPES = {"movie", "movie_or_video_item", "tvshow", "season", "episode", "artist", "album"}
IMAGE_KINDS = {"primary", "backdrop", "banner", "logo", "thumb"}

# 严格按 Jellyfin 文档给出的常见命名族
PRIMARY_CANDIDATES = ("folder", "poster", "cover", "default")
BACKDROP_CANDIDATES = ("fanart", "backdrop", "background", "art")
LOGO_CANDIDATES = ("logo", "clearlogo")
THUMB_CANDIDATES = ("thumb", "landscape")


@dataclass(frozen=True)
class NamingTarget:
    filename: str
    description: str


def preferred_primary_basename(media_type: str) -> str:
    if media_type in {"tvshow", "season", "episode", "artist", "album"}:
        return "folder"
    return "poster"


def preferred_basename(media_type: str, image_kind: str) -> str:
    if image_kind == "primary":
        return preferred_primary_basename(media_type)
    if image_kind == "backdrop":
        return "fanart"
    if image_kind == "banner":
        return "banner"
    if image_kind == "logo":
        return "logo"
    if image_kind == "thumb":
        return "thumb"
    return preferred_primary_basename(media_type)


def build_image_filename(media_type: str, image_kind: str, extension: str) -> str:
    ext = extension.lower().strip()
    if not ext.startswith("."):
        ext = f".{ext}"
    base = preferred_basename(media_type, image_kind)
    return f"{base}{ext}"


def episode_thumb_filename(video_file: Path, extension: str) -> str:
    ext = extension.lower().strip()
    if not ext.startswith("."):
        ext = f".{ext}"
    return f"{video_file.stem}-thumb{ext}"


def preview_image_targets(media_type: str, extension: str) -> list[NamingTarget]:
    ext = extension.lower() if extension.startswith(".") else f".{extension.lower()}"
    return [
        NamingTarget(filename=f"{preferred_primary_basename(media_type)}{ext}", description="Primary"),
        NamingTarget(filename=f"fanart{ext}", description="Backdrop"),
        NamingTarget(filename=f"banner{ext}", description="Banner"),
        NamingTarget(filename=f"logo{ext}", description="Logo"),
        NamingTarget(filename=f"thumb{ext}", description="Thumb"),
    ]

