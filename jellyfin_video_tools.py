import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TimeSegment:
    start: str
    end: str


@dataclass
class SegmentPreview:
    source: Path
    output: Path
    start: str
    end: str


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _to_seconds(ts: str) -> float:
    text = ts.strip()
    parts = text.split(":")
    if len(parts) != 3:
        raise ValueError(f"时间格式错误: {ts}")
    hh = float(parts[0])
    mm = float(parts[1])
    ss = float(parts[2])
    return hh * 3600 + mm * 60 + ss


def parse_segments(raw_text: str) -> list[TimeSegment]:
    segments: list[TimeSegment] = []
    for line in raw_text.splitlines():
        one = line.strip()
        if not one:
            continue
        if "-" not in one:
            raise ValueError(f"分段格式错误: {one}（应为 start-end）")
        start, end = one.split("-", 1)
        s = _to_seconds(start)
        e = _to_seconds(end)
        if e <= s:
            raise ValueError(f"分段结束时间必须大于开始时间: {one}")
        segments.append(TimeSegment(start=start.strip(), end=end.strip()))
    if not segments:
        raise ValueError("未提供任何分段。")

    # 重叠校验
    ordered = sorted(segments, key=lambda x: _to_seconds(x.start))
    for i in range(1, len(ordered)):
        prev = ordered[i - 1]
        cur = ordered[i]
        if _to_seconds(cur.start) < _to_seconds(prev.end):
            raise ValueError(f"分段重叠: {prev.start}-{prev.end} 与 {cur.start}-{cur.end}")
    return ordered


def build_segment_previews(video_files: list[Path], segments: list[TimeSegment], output_dir: Path) -> list[SegmentPreview]:
    previews: list[SegmentPreview] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    for src in video_files:
        for idx, seg in enumerate(segments, start=1):
            out = output_dir / f"{src.stem}-part{idx:02d}{src.suffix.lower()}"
            previews.append(SegmentPreview(source=src, output=out, start=seg.start, end=seg.end))
    return previews


def run_segment_export(
    previews: list[SegmentPreview],
    copy_stream: bool = True,
    video_filter: str | None = None,
) -> tuple[int, int, list[str]]:
    ok = 0
    failed = 0
    logs: list[str] = []
    vf = (video_filter or "").strip()
    use_filter = bool(vf)
    for job in previews:
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            job.start,
            "-to",
            job.end,
            "-i",
            str(job.source),
        ]
        if use_filter:
            # 使用滤镜时必须转码，不能 copy。
            cmd.extend(["-vf", vf, "-c:v", "libx264", "-c:a", "aac"])
        elif copy_stream:
            cmd.extend(["-c", "copy"])
        else:
            cmd.extend(["-c:v", "libx264", "-c:a", "aac"])
        cmd.append(str(job.output))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode == 0:
                ok += 1
                logs.append(f"成功: {job.source.name} -> {job.output.name}")
            else:
                failed += 1
                logs.append(f"失败: {job.source.name} -> {proc.stderr.strip()[:200]}")
        except Exception as exc:
            failed += 1
            logs.append(f"失败: {job.source.name} -> {exc}")
    return ok, failed, logs

