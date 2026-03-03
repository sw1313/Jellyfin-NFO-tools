import json
import re
import uuid
import sys
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox


VIDEO_EXTENSIONS = {
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
LINUX_FILENAME_MAX_BYTES = 255
EPISODE_TAG_PATTERN = re.compile(r"(?<!\d)S(\d+)E(\d+)(?!\d)", re.IGNORECASE)
HISTORY_FILE = Path(__file__).with_name(".season_renamer_history.json")


@dataclass
class RenameOp:
    source: Path
    target: Path
    temp: Path | None = None


def is_video_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS


def parse_season_num(name: str) -> int | None:
    match = re.match(r"^season\s*(\d+)$", name.strip(), re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def natural_key(text: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def build_stem_with_limit(stem: str, suffix: str, extension: str, max_bytes: int = LINUX_FILENAME_MAX_BYTES) -> str:
    # 优先保留 suffix，先截断原始 stem 的末尾
    candidate = f"{stem}{suffix}"
    while len(f"{candidate}{extension}".encode("utf-8")) > max_bytes and stem:
        stem = stem[:-1]
        candidate = f"{stem}{suffix}"

    # 极端情况下，如果 suffix + extension 本身也超限，则继续截断 suffix
    while len(f"{candidate}{extension}".encode("utf-8")) > max_bytes and suffix:
        suffix = suffix[:-1]
        candidate = f"{stem}{suffix}"

    if not candidate:
        candidate = "renamed"
    return candidate


def collect_video_files_from_input(path: Path) -> set[Path]:
    files: set[Path] = set()
    if path.is_dir():
        for file in path.rglob("*"):
            if is_video_file(file):
                files.add(file)
    elif is_video_file(path):
        files.add(path)
    return files


def group_by_season(files: set[Path]) -> dict[int, list[Path]]:
    grouped: dict[int, list[Path]] = {}
    for file in files:
        season_num = parse_season_num(file.parent.name)
        if season_num is None:
            continue
        grouped.setdefault(season_num, []).append(file)

    for season_num in grouped:
        grouped[season_num].sort(key=lambda p: natural_key(p.stem))
    return grouped


def build_rename_ops(grouped: dict[int, list[Path]]) -> tuple[list[RenameOp], list[str]]:
    ops: list[RenameOp] = []
    skipped_msgs: list[str] = []
    for season_num, files in grouped.items():
        max_existing_ep = 0
        pending_files: list[Path] = []
        has_existing_tag = False

        for src in files:
            matches = list(EPISODE_TAG_PATTERN.finditer(src.stem))
            if matches:
                has_existing_tag = True
                skipped_msgs.append(f"跳过（已包含集数标记）: {src.name}")

                season_matches = [m for m in matches if int(m.group(1)) == season_num]
                ref_matches = season_matches if season_matches else matches
                max_existing_ep = max(max_existing_ep, max(int(m.group(2)) for m in ref_matches))
                continue

            pending_files.append(src)

        next_ep = max_existing_ep + 1
        # 若该季完全未编号，则按总集数统一位数：>=100 用 3 位，>=1000 用 4 位，以此类推。
        fixed_ep_width = max(2, len(str(len(pending_files)))) if not has_existing_tag else None
        for src in pending_files:
            ep_width = fixed_ep_width if fixed_ep_width is not None else max(2, len(str(next_ep)))
            suffix = f" S{season_num:02d}E{next_ep:0{ep_width}d}"
            new_stem = build_stem_with_limit(src.stem, suffix, src.suffix)
            dst = src.with_name(f"{new_stem}{src.suffix}")
            ops.append(RenameOp(source=src, target=dst))
            next_ep += 1
    return ops, skipped_msgs


def validate_conflicts(ops: list[RenameOp]) -> tuple[list[RenameOp], list[str]]:
    valid_ops: list[RenameOp] = []
    skipped_msgs: list[str] = []

    source_set = {op.source.resolve() for op in ops}
    target_map: dict[Path, Path] = {}

    for op in ops:
        src = op.source.resolve()
        dst = op.target.resolve()
        if src == dst:
            skipped_msgs.append(f"跳过（无需改名）: {op.source.name}")
            continue

        if dst in target_map and target_map[dst] != src:
            skipped_msgs.append(f"跳过（目标重名）: {op.source.name} -> {op.target.name}")
            continue

        if dst.exists() and dst not in source_set:
            skipped_msgs.append(f"跳过（目标已存在）: {op.source.name} -> {op.target.name}")
            continue

        target_map[dst] = src
        valid_ops.append(op)

    return valid_ops, skipped_msgs


def load_history() -> dict:
    if not HISTORY_FILE.exists():
        return {"batches": []}
    try:
        raw = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {"batches": []}
        batches = raw.get("batches", [])
        if not isinstance(batches, list):
            batches = []
        return {"batches": batches}
    except Exception:
        return {"batches": []}


def save_history(history: dict):
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def append_history_batch(ops: list[RenameOp]):
    history = load_history()
    batches = history.setdefault("batches", [])
    moves = [{"source": str(op.source), "target": str(op.target)} for op in ops]
    batches.append(
        {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "moves": moves,
        }
    )
    save_history(history)


def get_last_history_batch() -> dict | None:
    history = load_history()
    batches = history.get("batches", [])
    if not batches:
        return None
    return batches[-1]


def drop_last_history_batch():
    history = load_history()
    batches = history.get("batches", [])
    if not batches:
        return
    batches.pop()
    history["batches"] = batches
    save_history(history)


def execute_renames(ops: list[RenameOp]) -> list[str]:
    logs: list[str] = []
    if not ops:
        return logs

    # 两段式重命名：先改临时名，避免循环/重名冲突
    for op in ops:
        temp_name = f".tmp_rename_{uuid.uuid4().hex}{op.source.suffix}"
        op.temp = op.source.with_name(temp_name)
        op.source.rename(op.temp)
        logs.append(f"[临时] {op.source.name} -> {op.temp.name}")

    for op in ops:
        if op.temp is None:
            continue
        op.temp.rename(op.target)
        logs.append(f"[完成] {op.source.name} -> {op.target.name}")

    return logs


def build_undo_ops_from_batch(batch: dict) -> tuple[list[RenameOp], list[str]]:
    ops: list[RenameOp] = []
    skipped: list[str] = []
    moves = batch.get("moves", [])
    for move in moves:
        if not isinstance(move, dict):
            continue
        src_text = move.get("source")
        dst_text = move.get("target")
        if not isinstance(src_text, str) or not src_text.strip():
            continue
        if not isinstance(dst_text, str) or not dst_text.strip():
            continue
        src = Path(src_text)
        dst = Path(dst_text)

        # 撤销时：当前应为 target，目标应为 source
        undo_source = dst
        undo_target = src
        if not undo_source.exists():
            skipped.append(f"还原跳过（文件不存在）: {undo_source}")
            continue

        ops.append(RenameOp(source=undo_source, target=undo_target))

    valid_ops, skipped_conflicts = validate_conflicts(ops)
    skipped.extend([f"还原跳过（冲突）: {msg}" for msg in skipped_conflicts])
    return valid_ops, skipped


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("季度批量重命名工具")
        self.geometry("860x560")
        self.paths: set[Path] = set()

        self._build_ui()
        self._enable_drag_drop_if_possible()

    def _build_ui(self):
        top_frame = tk.Frame(self)
        top_frame.pack(fill=tk.X, padx=12, pady=8)

        tk.Button(top_frame, text="添加文件夹", command=self.add_folder).pack(side=tk.LEFT, padx=4)
        tk.Button(top_frame, text="添加视频文件", command=self.add_files).pack(side=tk.LEFT, padx=4)
        tk.Button(top_frame, text="清空列表", command=self.clear_paths).pack(side=tk.LEFT, padx=4)
        tk.Button(top_frame, text="还原上次重命名", command=self.undo_last_rename, bg="#aa6f39", fg="white").pack(
            side=tk.RIGHT, padx=4
        )
        tk.Button(top_frame, text="开始重命名", command=self.start_rename, bg="#3e8e41", fg="white").pack(
            side=tk.RIGHT, padx=4
        )

        hint = (
            "支持输入：\n"
            "1) 根目录（例如包含 Season1、Season2）\n"
            "2) 单个季度文件夹（SeasonX）\n"
            "3) 单个或多个视频文件（需位于 SeasonX 文件夹下）\n\n"
            "拖拽提示：Windows 可安装 `pip install windnd` 以启用拖拽。"
        )
        tk.Label(self, text=hint, justify=tk.LEFT, anchor="w").pack(fill=tk.X, padx=12)

        list_frame = tk.Frame(self)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        self.path_listbox = tk.Listbox(list_frame, selectmode=tk.EXTENDED)
        self.path_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scroll = tk.Scrollbar(list_frame, command=self.path_listbox.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.path_listbox.config(yscrollcommand=scroll.set)

        log_label = tk.Label(self, text="执行日志：", anchor="w")
        log_label.pack(fill=tk.X, padx=12)

        self.log_text = tk.Text(self, height=12)
        self.log_text.pack(fill=tk.BOTH, padx=12, pady=(0, 12))
        self.log_text.config(state=tk.DISABLED)

    def _log(self, message: str):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _add_path(self, path_str: str):
        path = Path(path_str).resolve()
        if not path.exists():
            self._log(f"不存在，已跳过: {path}")
            return
        if path in self.paths:
            return
        self.paths.add(path)
        self.path_listbox.insert(tk.END, str(path))

    def add_folder(self):
        folder = filedialog.askdirectory(title="选择文件夹")
        if folder:
            self._add_path(folder)

    def add_files(self):
        files = filedialog.askopenfilenames(
            title="选择视频文件",
            filetypes=[("视频文件", "*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.m4v *.ts *.webm *.mpg *.mpeg"), ("所有文件", "*.*")],
        )
        for file in files:
            self._add_path(file)

    def clear_paths(self):
        self.paths.clear()
        self.path_listbox.delete(0, tk.END)
        self._log("已清空输入列表。")

    def _enable_drag_drop_if_possible(self):
        try:
            import windnd  # type: ignore

            def _drop(files):
                for raw in files:
                    self._add_path(raw.decode("gbk", errors="ignore"))
                self._log("已接收拖拽内容。")

            windnd.hook_dropfiles(self, func=_drop)
            self._log("拖拽已启用。")
        except Exception:
            self._log("未启用拖拽（可安装 windnd）。仍可使用“添加文件夹/文件”。")

    def start_rename(self):
        if not self.paths:
            messagebox.showwarning("提示", "请先添加文件夹或文件。")
            return

        all_files: set[Path] = set()
        for path in self.paths:
            all_files.update(collect_video_files_from_input(path))

        if not all_files:
            messagebox.showwarning("提示", "没有发现可处理的视频文件。")
            return

        grouped = group_by_season(all_files)
        if not grouped:
            messagebox.showwarning("提示", "未找到季度文件夹下的视频文件（例如 Season1、Season 2）。")
            return

        ops, skipped_tagged = build_rename_ops(grouped)
        valid_ops, skipped_conflicts = validate_conflicts(ops)
        skipped = skipped_tagged + skipped_conflicts

        self._log("=" * 70)
        self._log(f"共识别视频: {len(all_files)}，可执行重命名: {len(valid_ops)}，跳过: {len(skipped)}")
        for msg in skipped:
            self._log(msg)

        if not valid_ops:
            messagebox.showinfo("结果", "没有可执行的重命名操作。")
            return

        try:
            logs = execute_renames(valid_ops)
            append_history_batch(valid_ops)
            for msg in logs:
                self._log(msg)
            messagebox.showinfo("完成", f"重命名完成，共处理 {len(valid_ops)} 个文件。")
        except Exception as exc:
            self._log(f"执行失败: {exc}")
            messagebox.showerror("错误", f"执行失败：{exc}")

    def undo_last_rename(self):
        batch = get_last_history_batch()
        if batch is None:
            messagebox.showinfo("提示", "没有可还原的重命名记录。")
            return

        ops, skipped = build_undo_ops_from_batch(batch)
        self._log("=" * 70)
        self._log(f"准备还原：可执行 {len(ops)}，跳过 {len(skipped)}")
        for msg in skipped:
            self._log(msg)

        if not ops:
            messagebox.showinfo("结果", "没有可执行的还原操作。")
            return

        try:
            logs = execute_renames(ops)
            for msg in logs:
                self._log(f"[还原]{msg}")
            drop_last_history_batch()
            messagebox.showinfo("完成", f"还原完成，共处理 {len(ops)} 个文件。")
        except Exception as exc:
            self._log(f"还原执行失败: {exc}")
            messagebox.showerror("错误", f"还原执行失败：{exc}")


if __name__ == "__main__":
    app = App()
    for arg in sys.argv[1:]:
        raw = str(arg or "").strip().strip('"')
        if not raw:
            continue
        try:
            app._add_path(raw)
        except Exception as exc:
            app._log(f"命令行路径加载失败: {raw} -> {exc}")
    if app.paths:
        app._log(f"已从命令行载入路径: {len(app.paths)}")
    app.mainloop()
