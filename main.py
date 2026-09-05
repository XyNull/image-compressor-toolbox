#!/usr/bin/env python3
"""TUI 图片压缩工具箱 — 拖放式终端图片压缩工具。

用法:
    python main.py                      # 启动 TUI
    python main.py /path/to/images/     # 带初始路径启动

工作流程:
    1. 拖入图片文件或文件夹 → 自动扫描并加入待压缩列表
    2. 可多次拖入，列表累积（自动去重）
    3. 点击"一键开始"或按 s 开始压缩；按 x 清空列表，按 m 切换覆盖/子目录模式
    4. 按 v 合并视频：同目录同分辨率视频 → 复制流合并 → AV1 转码
    5. 一键开始 = 先压缩图片，再合并视频（目录中两者都有时一步完成）
    6. 压缩完成后可继续拖入新文件

输出模式:
    子目录模式（默认）: 输出到 compressed/ 子目录，原文件不变
    覆盖模式: 压缩后将 compressed/ 中的文件移回原目录覆盖原文件，成功则删除子目录
"""
from __future__ import annotations

import atexit
import faulthandler
import gc
import os
import random
import re
import shutil
import stat
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widgets import (
    Button,
    Footer,
    Header,
    ProgressBar,
    RichLog,
    Static,
)

import config
from compressor import CompressionResult, ImageCompressor
from detector import SUPPORTED_FORMATS, detect_format
from hwaccel import HardwareAccelerator
from logger import get_logger, get_log_dir
from video_merge import VIDEO_EXTS, VideoMerger
import win_perf  # Windows 后台功耗节流/EcoQoS 关闭 (其他平台空操作)

# 加载配置（首次运行自动生成 config.toml）
cfg = config.load_config()

# 全局 logger（日志目录来自配置）
_log = get_logger(cfg.logging.log_dir)

# ---------------------------------------------------------------------- #
#  崩溃兜底 — 用于抓"进程直接消失"的无声崩溃 (崩溃时正常日志写不出来)
# ---------------------------------------------------------------------- #
# faulthandler: C 层崩溃(访问违规/段错误)时把 Python 调用栈写入独立文件,
# 即使进程被系统直接终止也能留下最后一帧的栈信息。
try:
    _fault_log = get_log_dir(cfg.logging.log_dir) / "faulthandler.log"
    _fault_file = open(_fault_log, "a", buffering=1)  # 行缓冲, 崩溃时尽量不丢
    faulthandler.enable(_fault_file)
    _log.info("faulthandler 已启用 — 崩溃栈写入: %s", _fault_log)
except Exception as _exc:
    _log.warning("faulthandler 启用失败: %s", _exc)


def _excepthook(exc_type, exc_value, exc_tb) -> None:
    """主线程未捕获异常 → 写入日志 (默认只打印到终端, 终端关闭就丢了)。"""
    _log.error(
        "未捕获异常(主线程) %s: %s\n%s",
        exc_type.__name__, exc_value,
        "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
    )
    try:
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    except Exception:
        pass


def _thread_excepthook(args) -> None:
    """工作线程未捕获异常 → 写入日志。"""
    _log.error(
        "未捕获异常(线程 %s) %s: %s\n%s",
        args.thread.name if args.thread else "?",
        args.exc_type.__name__, args.exc_value,
        "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback)),
    )


sys.excepthook = _excepthook
threading.excepthook = _thread_excepthook


def _on_exit() -> None:
    """进程正常退出时打点; 若日志末尾无此标记 → 说明被强杀/崩溃。"""
    _log.info("进程正常退出(atexit)")


atexit.register(_on_exit)

# 状态栏中文件名最大显示长度（来自配置）
_MAX_NAME_LEN = cfg.ui.max_name_len
# 文件列表中路径最大显示长度（来自配置）
_MAX_PATH_LEN = cfg.ui.max_path_len


# ---------------------------------------------------------------------- #
#  消息类 — work 线程 → 主线程的非阻塞 UI 更新
# ---------------------------------------------------------------------- #
# 用 post_message (线程安全, 非阻塞) 替代 call_from_thread (阻塞):
# 终端在后台时 Windows Terminal 会节流渲染, call_from_thread 会让 work
# 线程阻塞等待主线程, 导致压缩几乎停滞 (实测后台 0.3张/s vs 前台 50张/s)。

class ResultReady(Message):
    """单个文件压缩完成 (work → 主线程, 更新进度/日志)。"""
    def __init__(self, result: CompressionResult) -> None:
        super().__init__()
        self.result = result


class CurrentFileChanged(Message):
    """当前处理文件变化 (work → 主线程, 更新状态栏)。"""
    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path


class LogMessage(Message):
    """日志消息 (work → 主线程, 非阻塞写 UI 日志)。"""
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class UiTask(Message):
    """投递任意主线程 UI 任务 (work → 主线程, 非阻塞)。

    替代 call_from_thread 的通用版: 批次开始/收尾等一次性 UI 调用也改用
    非阻塞投递, 避免后台终端节流时 call_from_thread 阻塞 work 线程
    (call_from_thread 会等待主线程执行完回调才返回)。
    """
    def __init__(self, fn, *args) -> None:
        super().__init__()
        self.fn = fn
        self.args = args


class ImageCompressorApp(App):
    """Textual TUI for batch image compression."""

    TITLE = cfg.ui.title
    SUB_TITLE = "拖入图片 → 确认列表 → 开始压缩"

    def _handle_exception(self, error: Exception) -> None:
        """Textual 捕获的未处理异常 (含 work 线程异常) — 先写入文件日志再退出。

        Textual 默认将这类异常静默渲染到终端后直接退出 app (终端一关信息就丢)。
        覆写此方法: 先把异常 + traceback 写入日志文件, 再走默认退出逻辑。
        """
        try:
            # WorkerFailed 包装了 work 线程的原始异常 → 解包
            raw: BaseException = error
            if isinstance(error, Exception) and hasattr(error, "error"):
                raw = getattr(error, "error")
            _log.error(
                "Textual 未处理异常 %s: %s\n%s",
                type(raw).__name__, raw,
                "".join(traceback.format_exception(
                    type(raw), raw, raw.__traceback__)),
            )
        except Exception:
            _log.error("Textual 未处理异常: %s: %s", type(error).__name__, error)
        super()._handle_exception(error)

    CSS = """
    Screen {
        layout: vertical;
    }

    #drop-zone {
        height: 5;
        border: round $primary;
        content-align: center middle;
        text-align: center;
        margin: 1 2;
        padding: 0 2;
        color: $text-muted;
    }

    #drop-zone:focus {
        border: round $accent;
        color: $text;
    }

    #action-bar {
        height: 3;
        margin: 0 2;
        align: left middle;
    }

    #btn-start-all {
        width: 34;
    }

    #file-list-section {
        height: 12;
        margin: 0 2;
        border: round $primary;
    }

    #file-list-header {
        height: 1;
        padding: 0 1;
        background: $primary 15%;
        color: $text;
        text-align: center;
    }

    #file-list {
        height: 1fr;
        scrollbar-size: 1 1;
    }

    #status-bar {
        height: 3;
        margin: 0 2;
    }

    .stat {
        width: 1fr;
        padding: 0 1;
        content-align: center middle;
        text-align: center;
        text-overflow: ellipsis;
        border: round $primary-background;
        margin: 0 1;
    }

    /* 可变长度文本的两格给更宽空间，避免型号/文件名被裁切 */
    #stat-current { width: 2fr; }
    #stat-hw { width: 2fr; }

    #progress-section {
        height: 3;
        margin: 0 2;
    }

    #log-section {
        margin: 0 2 1 2;
        height: 1fr;
        border: round $primary;
    }

    #log {
        height: 1fr;
        scrollbar-size: 1 1;
    }
    """

    BINDINGS = [
        Binding("s", "start", "开始压缩"),
        Binding("m", "toggle_mode", "切换模式"),
        Binding("v", "merge_videos", "合并视频"),
        Binding("x", "clear_queue", "清空列表"),
        Binding("c", "clear", "清空日志"),
        Binding("r", "redo_overwrite", "恢复覆盖"),
        Binding("q", "quit", "退出"),
    ]

    # Reactive attributes
    total_count = reactive(0)
    done_count = reactive(0)
    current_file = reactive("")
    overwrite_mode = reactive(
        cfg.output.default_mode == "overwrite"
    )  # False=子目录模式, True=覆盖模式

    def __init__(self, initial_paths: List[Path] | None = None) -> None:
        super().__init__()
        self.cfg = cfg  # 实例级配置引用 (覆盖阶段等处用到 self.cfg)
        self.hw_accel = HardwareAccelerator()
        self.compressor = ImageCompressor(self.hw_accel, cfg)
        self._busy = threading.Event()
        self._initial_paths = initial_paths or []
        # 待压缩文件队列: [(file_path, root_dir, fmt, size), ...]
        # fmt/size 在扫描时缓存，压缩时直接使用，避免 SMB/网络重复 stat
        self._file_queue: List[
            tuple[Path, Path | None, Optional[str], int]
        ] = []
        # 崩溃残留的未覆盖文件 (compressed/ 里的压缩产物) — 按 r 恢复
        self._leftover_results: List[CompressionResult] = []
        # 视频合并候选目录 (拖入的目录本身 / 视频文件所在目录) — 按 v 合并
        self._merge_dirs: List[Path] = []

    # ------------------------------------------------------------------ #
    #  Layout
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "[bold]将图片文件或文件夹拖到此处[/bold]  "
            "[dim]拖入终端 / Ctrl+V 粘贴路径[/]",
            id="drop-zone",
        )
        # 一键开始: 先压缩图片, 再合并视频 (拖入的目录里可能两者都有)
        with Horizontal(id="action-bar"):
            yield Button(
                "一键开始 (图片压缩 + 视频合并)",
                id="btn-start-all",
                variant="primary",
            )
        with Vertical(id="file-list-section"):
            yield Static(
                "待压缩列表 (0 张)",
                id="file-list-header",
            )
            yield RichLog(id="file-list", highlight=True, markup=True)
        with Horizontal(id="status-bar"):
            yield Static("队列: 0", id="stat-queue", classes="stat")
            yield Static("已完成: 0", id="stat-done", classes="stat")
            yield Static("当前: -", id="stat-current", classes="stat")
            yield Static(
                f"模式: {'覆盖' if self.overwrite_mode else '子目录'}",
                id="stat-mode",
                classes="stat",
            )
            yield Static(
                f"硬件: {self.hw_accel.short_name}",
                id="stat-hw",
                classes="stat",
            )
        with Vertical(id="progress-section"):
            yield ProgressBar(total=100, id="progress")
        with Vertical(id="log-section"):
            yield RichLog(id="log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#log", RichLog)
        log.write("[bold cyan]═══ 图片压缩工具箱 ═══[/]")
        log.write(f"  硬件     : [cyan]{self.hw_accel.name}[/]")
        log.write(f"  CPU核心  : [cyan]{os.cpu_count()}[/]")
        log.write(f"  支持格式 : [dim]{', '.join(sorted(SUPPORTED_FORMATS))}[/]")
        log.write(
            f"  目标     : {cfg.compression.output_format.upper()} "
            f"q{cfg.compression.quality}, "
            f"≤ {cfg.compression.target_size_mb:.0f} MB"
        )
        # FFmpeg / 动图转 MP4 状态
        vc = self.compressor.video_converter
        if vc.is_available():
            enc = vc._encoder or "libx265"
            log.write(f"  FFmpeg   : [green]✓[/] 动图→MP4 H.265 [cyan]{enc}[/]")
        else:
            log.write(f"  FFmpeg   : [yellow]✗[/] 动图将跳过 (安装 FFmpeg 可启用转 MP4)")
        # 视频合并转码状态 (按 v)
        try:
            merger = VideoMerger(self.hw_accel, cfg)
            if merger.is_available():
                log.write(
                    f"  视频合并 : [green]✓[/] 同分辨率合并→AV1 "
                    f"[cyan]{merger.encoder_display()}[/] (按 [bold]v[/])"
                )
            else:
                log.write(
                    f"  视频合并 : [yellow]✗[/] 需 FFmpeg+ffprobe (按 [bold]v[/])"
                )
        except Exception as _me:
            log.write(f"  视频合并 : [yellow]✗[/] 初始化失败 ({_me})")
        log.write(f"  输出模式 : [cyan]子目录模式[/] (按 [bold]m[/bold] 切换为覆盖模式)")
        log.write(f"  日志     : [dim]{get_log_dir(cfg.logging.log_dir)}[/]")
        # Windows: 关闭系统对后台进程的功耗节流 (避免后台运行时被降频/降优先级)
        if os.name == "nt":
            boost_info = win_perf.boost_process()
            log.write(f"  性能     : [cyan]{boost_info}[/]")
            _log.info(boost_info)
        log.write("")
        log.write("[dim]拖入图片/视频目录后点击 [bold]一键开始[/]（图片压缩 + 视频合并）；"
                  "或按 [bold]s[/bold] 压缩、[bold]v[/bold] 合并。[/]")
        log.write("")

        # 记录启动环境到日志文件
        _log.info(
            "启动 — 硬件=%s, CPU核心=%s, FFmpeg=%s, 格式=[%s]",
            self.hw_accel.name,
            os.cpu_count(),
            (vc._encoder or "libx265") if vc.is_available() else "不可用",
            ", ".join(sorted(SUPPORTED_FORMATS)),
        )

        self._refresh_file_list()

        if self._initial_paths:
            self._add_to_queue(self._initial_paths)

    # ------------------------------------------------------------------ #
    #  Drag-and-drop / paste handling
    # ------------------------------------------------------------------ #

    def on_paste(self, event: events.Paste) -> None:
        """Terminal drag-and-drop pastes file paths as text."""
        if self._busy.is_set():
            self._ui_log("[yellow]⚠ 正在压缩中，请等待完成后再拖入。[/]")
            return

        paths = self._parse_paths(event.text)
        if paths:
            self._add_to_queue(paths)
        elif event.text.strip():
            self._ui_log(
                f"[yellow]非文件路径: {event.text.strip()[:60]}[/]"
            )

    @staticmethod
    def _parse_paths(text: str) -> List[Path]:
        """Extract valid file/folder paths from pasted text."""
        paths: List[Path] = []

        # 1. Try quoted paths first
        for match in re.findall(r'"([^"]+)"', text):
            p = Path(match)
            if p.exists():
                paths.append(p)

        # 2. Fallback: split by whitespace / newlines
        if not paths:
            for token in text.replace("\n", " ").split():
                clean = token.strip().strip("\"'")
                if clean:
                    p = Path(clean)
                    if p.exists():
                        paths.append(p)

        return paths

    # ------------------------------------------------------------------ #
    #  File queue management
    # ------------------------------------------------------------------ #

    def _add_to_queue(self, paths: List[Path]) -> None:
        """Scan paths and add images to the queue (deduplicated).

        Optimized for SMB / network paths:
        - Uses os.scandir() for cached stat() info (no extra round-trip)
        - Parallelizes detect_format() across files via a thread pool
        - Caches detected format + file size in the queue
        """
        log = self.query_one("#log", RichLog)

        existing_paths: set[Path] = {fp for fp, _, _, _ in self._file_queue}
        added = 0
        skipped_non_image = 0

        # 记录视频合并候选目录: 拖入的目录本身 / 视频文件所在目录 (去重)
        for path in paths:
            try:
                if path.is_dir():
                    if path not in self._merge_dirs:
                        self._merge_dirs.append(path)
                elif path.is_file() and path.suffix.lower() in VIDEO_EXTS:
                    parent = path.parent
                    if parent not in self._merge_dirs:
                        self._merge_dirs.append(parent)
            except OSError:
                pass

        # Phase 1: Collect candidate files with sizes (via os.scandir)
        candidates: List[tuple[Path, int]] = []  # (path, size)

        for path in paths:
            if path.is_file():
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                candidates.append((path, size))
            elif path.is_dir():
                candidates.extend(self._scan_directory(path))

        # Phase 2: Parallel format detection
        scan_workers = max(1, cfg.io.scan_workers)

        def _detect_one(item: tuple[Path, int]) -> tuple[Path, int, Optional[str]]:
            p, s = item
            return p, s, detect_format(p)

        detected: List[tuple[Path, int, Optional[str]]] = []
        if len(candidates) <= 4:
            # Small batch: sequential (avoids thread pool overhead)
            for item in candidates:
                detected.append(_detect_one(item))
        else:
            with ThreadPoolExecutor(
                max_workers=scan_workers,
                initializer=win_perf.boost_current_thread,
            ) as pool:
                detected = list(pool.map(_detect_one, candidates))

        # Phase 3: Add supported images to queue
        for p, size, fmt in detected:
            if fmt and fmt in SUPPORTED_FORMATS:
                if p not in existing_paths:
                    root = None
                    # Determine root_dir: find which input path is the parent
                    for input_path in paths:
                        if input_path.is_dir():
                            try:
                                p.relative_to(input_path)
                                root = input_path
                                break
                            except ValueError:
                                pass
                    self._file_queue.append((p, root, fmt, size))
                    existing_paths.add(p)
                    added += 1
            else:
                skipped_non_image += 1

        self._refresh_file_list()
        # 顺带检测: 拖入的目录里是否有崩溃遗留的 compressed/ 残留
        self._detect_leftover(paths)

        if added > 0:
            msg = f"[green]已添加 {added} 张图片到列表[/]"
            if skipped_non_image:
                msg += f"  [dim](跳过 {skipped_non_image} 个非图片文件)[/]"
            msg += f"  — 当前列表共 {len(self._file_queue)} 张"
            log.write(msg)
        else:
            if skipped_non_image:
                log.write(f"[yellow]未添加新图片（跳过 {skipped_non_image} 个非图片文件）[/]")
            else:
                log.write("[dim]未发现新图片（可能已在列表中）[/]")

    @staticmethod
    def _scan_directory(root: Path) -> List[tuple[Path, int]]:
        """Recursively scan a directory using os.scandir.

        Returns (file_path, size) pairs. Uses DirEntry.stat() which
        is cached from the directory listing on Windows/SMB — avoiding
        a separate stat() round-trip per file.
        """
        results: List[tuple[Path, int]] = []
        stack: List[Path] = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        if entry.name == "compressed":
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                try:
                                    size = entry.stat(
                                        follow_symlinks=False
                                    ).st_size
                                except OSError:
                                    size = 0
                                results.append((Path(entry.path), size))
                        except OSError:
                            pass
            except OSError:
                pass
        return results

    # ------------------------------------------------------------------ #
    #  崩溃残留检测 & 恢复覆盖 (按 r)
    # ------------------------------------------------------------------ #

    def _detect_leftover(self, paths: List[Path]) -> None:
        """检测拖入目录中的 compressed/ 残留 (崩溃遗留的未覆盖压缩产物)。

        检测结果存入 self._leftover_results, 用户按 r 调用 _execute_overwrite
        完成覆盖 (复用现有覆盖逻辑: 清只读 + 重试 + 详细日志)。
        """
        found: List[CompressionResult] = []
        seen_outputs = {r.output_path for r in self._leftover_results
                        if r.output_path is not None}

        for path in paths:
            if path.is_file():
                continue
            for comp_dir in self._find_compressed_dirs(path):
                for f in sorted(comp_dir.iterdir()):
                    if not f.is_file():
                        continue
                    if f in seen_outputs:
                        continue
                    seen_outputs.add(f)
                    try:
                        size = f.stat().st_size
                    except OSError:
                        size = 0
                    found.append(CompressionResult(
                        input_path=self._find_original(comp_dir, f),
                        output_path=f,
                        success=True,
                        original_size=size,
                        output_size=size,
                        fmt="jpeg",
                    ))

        if found:
            self._leftover_results.extend(found)
            log = self.query_one("#log", RichLog)
            log.write(
                f"[yellow]发现 {len(found)} 个未完成的覆盖"
                f"(compressed/ 残留) — 按 [bold]r[/] 一键恢复[/]"
            )
            _log.info(
                "检测到压缩残留 %d 个 (累计 %d 个), 按 r 恢复",
                len(found), len(self._leftover_results),
            )

    @staticmethod
    def _find_compressed_dirs(root: Path) -> List[Path]:
        """递归查找目录树中所有"含文件的 compressed/ 目录"。"""
        result: List[Path] = []
        stack: List[Path] = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        p = Path(entry.path)
                        if entry.name == "compressed":
                            try:
                                if any(e.is_file() for e in os.scandir(p)):
                                    result.append(p)
                            except OSError:
                                pass
                        else:
                            stack.append(p)
            except OSError:
                pass
        return result

    @staticmethod
    def _find_original(comp_dir: Path, comp_file: Path) -> Path:
        """推断压缩产物对应的原图路径。

        优先: 原目录中 stem 相同且后缀不同的文件 (如 残留 a.jpg → 原图 a.png);
        其次: 同名文件; 都没有则兜底为同名路径 (由 _execute_overwrite 容错)。
        """
        parent = comp_dir.parent
        stem = comp_file.stem
        ext = comp_file.suffix.lower()
        try:
            candidates = [p for p in parent.iterdir()
                          if p.is_file() and p.stem == stem]
        except OSError:
            candidates = []
        for c in candidates:
            if c.suffix.lower() != ext:
                return c
        if candidates:
            return candidates[0]
        return parent / comp_file.name

    def action_redo_overwrite(self) -> None:
        """按 r: 恢复上次崩溃未完成的覆盖 (compressed/ 残留 → 移回原目录)。"""
        if self._busy.is_set():
            self._ui_log("[yellow]⚠ 正在处理中，请稍后再试。[/]")
            return
        if not self._leftover_results:
            self._ui_log(
                "[dim]没有检测到未完成的覆盖 — 拖入原目录即可自动检测。[/]"
            )
            return
        self._start_redo()

    def action_merge_videos(self) -> None:
        """按 v: 合并已拖入目录中的视频（同分辨率分组 → 复制流合并 → AV1 转码）。

        处理单元 = 拖入的目录本身（不递归子目录）；"该功能无需考虑由多帧
        动图转成的视频文件"（动画转换产物在 compressed/ 子目录，不在范围）。
        """
        if self._busy.is_set():
            self._ui_log("[yellow]⚠ 正在处理中，请等待完成。[/]")
            return
        if not self._merge_dirs:
            self._ui_log(
                "[yellow]请先拖入包含视频的目录（或直接拖入视频文件）。[/]"
            )
            return
        self._start_merge()

    @work(thread=True, exit_on_error=False)
    def _start_merge(self) -> None:
        """后台线程: 仅执行视频合并转码（按 v）。"""
        try:
            self._busy.set()
            self.post_message(UiTask(self._ui_set_busy, True))
            self._merge_all()
        except Exception as e:
            _log.error(
                "视频合并异常 %s: %s\n%s",
                type(e).__name__, e, traceback.format_exc(),
            )
            self._safe_ui_log(
                f"[red]✗ 视频合并异常: {type(e).__name__}: {e} (详情见日志)[/]"
            )
        finally:
            self._busy.clear()
            self.post_message(UiTask(self._ui_set_busy, False))

    def _merge_all(self) -> None:
        """视频合并主体: 对每个候选目录执行合并转码（串行）。

        供 _start_merge（按 v）与 _start_all（一键开始阶段 2）复用。
        """
        dirs = list(self._merge_dirs)
        vm_cfg = self.cfg.video_merge
        self._safe_ui_log(
            f"[bold yellow]开始视频合并转码 — {len(dirs)} 个目录…[/]"
        )
        _log.info(
            "视频合并开始 — 目录 %d 个: %s",
            len(dirs), [str(d) for d in dirs],
        )

        merger = VideoMerger(self.hw_accel, self.cfg)
        if not merger.is_available():
            self._safe_ui_log("[red]✗ FFmpeg/ffprobe 不可用，无法合并视频。[/]")
            _log.error("视频合并: FFmpeg/ffprobe 不可用")
            return
        self._safe_ui_log(
            f"[dim]编码器 {merger.encoder_display()} | 质量 {vm_cfg.quality} | "
            f"关键帧 {vm_cfg.keyframe_interval}s | "
            f"音频 {vm_cfg.audio_codec} {vm_cfg.audio_profile} "
            f"{vm_cfg.audio_bitrate}[/]"
        )

        total_merged = 0
        total_skipped = 0
        all_outputs: List[Path] = []
        for d in dirs:
            self._safe_ui_log("")
            report = merger.merge_directory(d, log_cb=self._safe_ui_log)
            total_merged += report.merged
            total_skipped += report.skipped
            all_outputs.extend(report.outputs)

        # 末尾汇总提示（UI + 文件日志）
        self._safe_ui_log("")
        if total_merged:
            self._safe_ui_log(
                f"[bold green]═══ 视频合并完成: 成功 {total_merged} 组"
                + (f"，跳过 {total_skipped} 组" if total_skipped else "")
                + " ═══[/]"
            )
            self._safe_ui_log(
                f"[dim]输出: {', '.join(str(o) for o in all_outputs)}[/]"
            )
        else:
            self._safe_ui_log(
                f"[yellow]═══ 视频合并结束: 未生成文件（跳过 {total_skipped} 组）═══[/]"
            )
        _log.info(
            "视频合并结束 — 成功 %d 组, 跳过 %d 组, 输出 %s",
            total_merged, total_skipped, [str(o) for o in all_outputs],
        )

    def action_start_all(self) -> None:
        """一键开始: 先压缩图片，再合并视频（按钮触发）。"""
        if self._busy.is_set():
            self._ui_log("[yellow]⚠ 正在处理中，请等待完成。[/]")
            return
        if not self._file_queue and not self._merge_dirs:
            self._ui_log(
                "[yellow]列表为空 — 请先拖入包含图片或视频的目录。[/]"
            )
            return
        self._start_all()

    @work(thread=True, exit_on_error=False)
    def _start_all(self) -> None:
        """后台线程: 阶段1 图片压缩 → 阶段2 视频合并转码。"""
        try:
            self._busy.set()
            self.post_message(UiTask(self._ui_set_busy, True))
            n_img = len(self._file_queue)
            n_dir = len(self._merge_dirs)
            self._safe_ui_log(
                f"[bold cyan]一键开始: 图片 {n_img} 张"
                + (f" + 视频 {n_dir} 个目录" if n_dir else "")
                + "[/]"
            )
            _log.info("一键开始 — 图片 %d 张, 视频目录 %d 个", n_img, n_dir)

            # 阶段 1: 图片压缩 (队列非空时)
            if self._file_queue:
                self._run_processing()
            # 阶段 2: 视频合并转码 (有候选目录时)
            if self._merge_dirs:
                self._merge_all()

            self._safe_ui_log("")
            self._safe_ui_log(
                "[bold green]✓ 全部任务完成（图片压缩 + 视频合并）[/]"
            )
            _log.info("一键开始全部完成 — 图片 %d 张, 视频目录 %d 个", n_img, n_dir)
        except Exception as e:
            _log.error(
                "一键开始异常 %s: %s\n%s",
                type(e).__name__, e, traceback.format_exc(),
            )
            self._safe_ui_log(
                f"[red]✗ 一键开始异常: {type(e).__name__}: {e} (详情见日志)[/]"
            )
        finally:
            self._busy.clear()
            self.post_message(UiTask(self._ui_set_busy, False))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """按钮点击: 一键开始 (图片压缩 + 视频合并)。"""
        if event.button.id == "btn-start-all":
            self.action_start_all()

    def _ui_set_busy(self, busy: bool) -> None:
        """主线程: 更新"一键开始"按钮可用状态。"""
        try:
            self.query_one("#btn-start-all", Button).disabled = busy
        except Exception:
            pass

    @work(thread=True, exit_on_error=False)
    def _start_redo(self) -> None:
        """后台线程执行残留覆盖 (复用 _execute_overwrite)。"""
        try:
            self._busy.set()
            self.post_message(UiTask(self._ui_set_busy, True))
            n = len(self._leftover_results)
            self._safe_ui_log(f"[yellow]开始恢复 {n} 个未完成的覆盖…[/]")
            _log.info("恢复覆盖开始 — %d 个残留文件", n)
            self._execute_overwrite(self._leftover_results)
        except Exception as e:
            _log.error(
                "恢复覆盖异常 %s: %s\n%s",
                type(e).__name__, e, traceback.format_exc(),
            )
        finally:
            self._busy.clear()
            self._leftover_results = []

    def _refresh_file_list(self) -> None:
        """Re-render the file list widget."""
        fl = self.query_one("#file-list", RichLog)
        header = self.query_one("#file-list-header", Static)
        fl.clear()

        count = len(self._file_queue)
        if count == 0:
            header.update("待压缩列表 (0 张)")
            fl.write("[dim]列表为空 — 拖入图片文件或文件夹[/]")
            return

        # Calculate total size (from cached sizes — no stat calls)
        total_size = sum(size for _, _, _, size in self._file_queue)
        total_mb = total_size / (1024 * 1024)

        header.update(f"待压缩列表 ({count} 张, 共 {total_mb:.1f} MB)")

        for i, (fp, root_dir, _fmt, size) in enumerate(self._file_queue, 1):
            size_mb = size / (1024 * 1024)

            # Show path relative to root_dir if available
            if root_dir is not None:
                try:
                    display = str(fp.relative_to(root_dir))
                except ValueError:
                    display = str(fp)
            else:
                display = str(fp)

            if len(display) > _MAX_PATH_LEN:
                display = "…" + display[-(_MAX_PATH_LEN - 1):]

            fl.write(
                f"  [cyan]{i:4d}[/]. {display}  "
                f"[dim]{size_mb:.2f} MB[/]"
            )

    # ------------------------------------------------------------------ #
    #  Key binding actions
    # ------------------------------------------------------------------ #

    def action_start(self) -> None:
        """Start compression of the queued files."""
        if self._busy.is_set():
            self._ui_log("[yellow]⚠ 正在压缩中，请等待完成。[/]")
            return

        if not self._file_queue:
            self._ui_log("[yellow]列表为空，请先拖入图片。[/]")
            return

        self.start_processing()

    def watch_overwrite_mode(self, overwrite: bool) -> None:
        """overwrite_mode 变化时同步状态栏 (含启动时按配置初始化)。"""
        try:
            mode_name = "覆盖" if overwrite else "子目录"
            self.query_one("#stat-mode", Static).update(f"模式: {mode_name}")
        except NoMatches:
            # compose 期间 watcher 提前触发 (init=True), 此时标签尚未创建;
            # 初始值已由 compose 的动态标签正确渲染, 静默跳过即可。
            pass

    def action_toggle_mode(self) -> None:
        """Toggle between subdirectory mode and overwrite mode."""
        if self._busy.is_set():
            self._ui_log("[yellow]⚠ 正在压缩中，无法切换模式。[/]")
            return

        self.overwrite_mode = not self.overwrite_mode
        mode_name = "覆盖" if self.overwrite_mode else "子目录"
        self._ui_log(
            f"[cyan]输出模式已切换为: {mode_name}模式[/]"
        )

    def action_clear_queue(self) -> None:
        """Clear the entire file queue."""
        if self._busy.is_set():
            self._ui_log("[yellow]⚠ 正在压缩中，无法清空列表。[/]")
            return

        if not self._file_queue:
            self._ui_log("[dim]列表已为空。[/]")
            return

        count = len(self._file_queue)
        self._file_queue.clear()
        self._refresh_file_list()
        self._ui_log(f"[yellow]已清空列表（移除 {count} 张图片）。[/]")

    def action_clear(self) -> None:
        """Clear the log."""
        log = self.query_one("#log", RichLog)
        log.clear()
        log.write("[dim]日志已清空。[/]")

    # ------------------------------------------------------------------ #
    #  Background processing (runs in a worker thread)
    # ------------------------------------------------------------------ #

    @work(thread=True, exit_on_error=False)
    def start_processing(self) -> None:
        """Orchestrate parallel compression in a background thread.

        exit_on_error=False: work 线程异常不退出 app (Textual 默认退出且不写
        我们的日志), 而是被下面的 try/except 记录到文件日志, 便于定位。
        """
        try:
            self._busy.set()
            self.post_message(UiTask(self._ui_set_busy, True))
            self._run_processing()
        except Exception as e:
            _log.error(
                "start_processing 异常 %s: %s\n%s",
                type(e).__name__, e, traceback.format_exc(),
            )
            self._safe_ui_log(
                f"[red]✗ 处理异常: {type(e).__name__}: {e} "
                f"(详情见日志)[/]"
            )
        finally:
            self._busy.clear()
            self.post_message(UiTask(self._ui_set_busy, False))

    def _run_processing(self) -> None:
        """start_processing 的实际逻辑 (包一层便于异常捕获)。"""
        self._busy.set()

        # Snapshot the queue and clear the list display
        images = list(self._file_queue)
        self._file_queue.clear()
        self.post_message(UiTask(self._refresh_file_list))

        self.total_count = len(images)
        self.done_count = 0
        self.current_file = ""

        self.post_message(UiTask(self._ui_update_stats))
        self.post_message(UiTask(self._ui_set_progress, len(images), 0))

        mode_str = "覆盖模式" if self.overwrite_mode else "子目录模式"
        self.post_message(
            UiTask(
                self._ui_log,
                f"[bold green]开始压缩 {len(images)} 张图片（{mode_str}）…[/]"
            )
        )
        _log.info("批次开始 — %d 张图片, 模式=%s", len(images), mode_str)

        # 若系统已把本进程降级到低优先级类(效率模式), 恢复 NORMAL —
        # 只做检测性恢复, 不主动提权 (提权会饿死 UI 主线程导致界面卡顿)
        prio_restored = win_perf.restore_priority()
        if prio_restored:
            _log.warning(prio_restored)

        max_workers = cfg.parallel.max_workers or (os.cpu_count() or 4)
        all_results: List[CompressionResult] = []

        with ThreadPoolExecutor(
            max_workers=max_workers,
            initializer=win_perf.boost_current_thread,
        ) as pool:
            future_map = {
                pool.submit(self._compress_one, img, root, fmt, size): img
                for img, root, fmt, size in images
            }
            _log.info(
                "压缩线程池启动 — 任务 %d 个, 线程 %d", len(future_map), max_workers
            )

            done = 0
            for future in as_completed(future_map):
                img_path = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = CompressionResult(
                        input_path=img_path,
                        output_path=None,
                        success=False,
                        error=str(exc),
                    )
                done += 1
                all_results.append(result)
                # 非阻塞投递 UI 更新 — 不等待主线程渲染 (后台运行时主线程
                # 可能被 Windows Terminal 节流, call_from_thread 会卡死压缩)
                try:
                    self.post_message(ResultReady(result))
                except Exception as e:
                    _log.warning("投递 UI 消息失败: %s: %s", type(e).__name__, e)
                # 每 50 个 + 最后 1 个打点进度 (定位"处理到哪崩了")
                if done % 50 == 0 or done == len(future_map):
                    _log.info("压缩进度 %d/%d", done, len(future_map))

        _log.info(
            "压缩阶段完成 — 共 %d 个结果 (成功=%d, 跳过=%d, 失败=%d)",
            len(all_results),
            sum(1 for r in all_results if r.success and not r.skipped),
            sum(1 for r in all_results if r.skipped),
            sum(1 for r in all_results if not r.success),
        )

        # 覆盖模式: 将 compressed/ 中的文件移回原目录
        if self.overwrite_mode:
            # 强制 GC 释放残留的文件句柄，避免 Windows 上 os.replace() 失败
            _log.info("开始 GC (释放压缩阶段残留句柄)")
            gc.collect()
            _log.info("GC 完成, 进入覆盖阶段")
            self._execute_overwrite(all_results)
        else:
            _log.info("子目录模式, 跳过覆盖阶段")

        self.post_message(UiTask(self._ui_on_complete))
        _log.info("批次收尾完成 (等待 UI 更新)")

    def _compress_one(
        self,
        img_path: Path,
        root_dir: Path | None,
        known_fmt: Optional[str] = None,
        known_size: int = 0,
    ) -> CompressionResult:
        """Compress a single image — runs inside ThreadPoolExecutor.

        Passes cached format + size to the compressor to avoid redundant
        detect_format() and stat() calls (important for SMB/network paths).
        """
        try:
            # 非阻塞更新状态栏"当前文件" (避免后台时阻塞压缩)
            self.post_message(CurrentFileChanged(str(img_path)))
        except Exception:
            pass
        _log.info(
            "开始压缩 %s (fmt=%s, %d bytes)",
            img_path, known_fmt or "?", known_size,
        )
        result = self.compressor.compress(
            img_path, root_dir, known_fmt=known_fmt, known_size=known_size
        )
        if not result.success:
            _log.error(
                "压缩失败 %s | %s", img_path.name,
                f"{type(result.error)}: {result.error}" if result.error else "",
            )
        return result

    # ------------------------------------------------------------------ #
    #  Overwrite mode: move files from compressed/ back to parent
    # ------------------------------------------------------------------ #

    @staticmethod
    def _clear_readonly(path: Path) -> None:
        """清除 Windows 只读属性, 使文件可被覆盖 / 删除。

        从网盘 / 下载站获取的图片常带只读属性, 导致:
          - os.replace() 覆盖只读目标 → PermissionError [WinError 5]
          - os.unlink() 删除只读文件 → PermissionError [WinError 5]
        """
        try:
            if path.exists():
                os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            pass

    @staticmethod
    def _rmtree_onerror(func, path, exc_info) -> None:
        """shutil.rmtree 遇只读文件 / 目录失败时, 清除只读后重试。"""
        try:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
            func(path)
        except OSError:
            pass

    @staticmethod
    def _file_diagnostics(path: Path) -> str:
        """生成文件状态诊断字符串 (覆盖失败时写入日志, 用于定位根因)。

        包含: 存在性 / 大小 / POSIX 模式位 / Windows 属性 (READONLY 等)。
        """
        try:
            if not path.exists():
                return "不存在"
            st = os.stat(path)
            mode = st.st_mode
            attrs = []
            if not (mode & 0o200):
                attrs.append("无写位")
            win_attrs = ""
            try:
                import ctypes
                wa = ctypes.windll.kernel32.GetFileAttributesW(str(path))
                if wa != 0xFFFFFFFF:
                    flags = []
                    if wa & 0x1:
                        flags.append("READONLY")
                    if wa & 0x2:
                        flags.append("HIDDEN")
                    if wa & 0x4:
                        flags.append("SYSTEM")
                    if wa & 0x20:
                        flags.append("ARCHIVE")
                    if wa & 0x800:
                        flags.append("COMPRESSED")
                    win_attrs = " Win32=0x%X(%s)" % (
                        wa, ",".join(flags) if flags else "NORMAL"
                    )
            except Exception:
                pass
            return (f"{st.st_size}字节 mode={oct(mode & 0o777)}"
                    f"{win_attrs} {','.join(attrs)}")
        except Exception as e:
            return f"诊断失败: {e}"

    @staticmethod
    def _is_locked(path: Path) -> Optional[int]:
        """尝试请求 DELETE 权限打开文件, 判断覆盖/删除是否会被拒绝。

        os.replace()/os.unlink() 在 Windows 上需要文件的 DELETE 权限。
        若其他进程以"不共享删除"方式打开着该文件, 请求 DELETE 会失败。

        Returns:
            None        → 可取得 DELETE 权限 (可被覆盖/删除)
            Win32 错误码 → 被占用/无权限
                (32 = ERROR_SHARING_VIOLATION 共享冲突 = 其他进程正打开着,
                 5  = ERROR_ACCESS_DENIED 权限不足)
        """
        if not path.exists():
            return None
        try:
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            DELETE = 0x00010000
            GENERIC_READ = 0x80000000
            FILE_SHARE_READ = 0x1
            FILE_SHARE_WRITE = 0x2
            FILE_SHARE_DELETE = 0x4
            OPEN_EXISTING = 3
            FILE_ATTRIBUTE_NORMAL = 0x80
            handle = k32.CreateFileW(
                str(path),
                DELETE | GENERIC_READ,          # 请求删除权
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                None,
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                None,
            )
            if handle == -1 or handle == 0xFFFFFFFF or handle == 0:
                return int(ctypes.get_last_error())
            k32.CloseHandle(handle)
            return None
        except Exception:
            return None

    def _safe_ui_log(self, msg: str) -> None:
        """UI 日志安全版 (非阻塞): 主线程不可用/慢渲染时不中断文件移动。

        用 post_message 投递 (线程安全、非阻塞), 避免后台时 work 线程
        阻塞等渲染 (call_from_thread 会让压缩/覆盖在后台几乎停滞)。
        """
        try:
            self.post_message(LogMessage(msg))
        except Exception:
            pass

    def _execute_overwrite(self, results: List[CompressionResult]) -> None:
        """Move compressed files from compressed/ subdirs to parent dirs.

        For each compressed/ directory:
          - Move all files to the parent directory (overwrite existing)
          - Delete original files that have a different extension
          - If all moves succeed → delete compressed/
          - If any move fails → keep compressed/
        """
        self._safe_ui_log("")
        self._safe_ui_log("[yellow]正在执行覆盖移动…[/]"
        )
        _log.info("覆盖阶段开始 — 输入结果 %d 个", len(results))

        # Group output files by their compressed/ directory
        # Each entry: (src=compressed_file, dst=target_path, original=original_file)
        dir_files: Dict[Path, List[Tuple[Path, Path, Path]]] = {}

        for result in results:
            if not result.success or result.skipped:
                continue
            if result.output_path is None:
                continue

            output_path = result.output_path
            parts = output_path.parts

            if "compressed" not in parts:
                continue

            idx = parts.index("compressed")
            compressed_dir = Path(*parts[: idx + 1])
            # Target = path without the "compressed" component
            target_parts = parts[:idx] + parts[idx + 1:]
            target_path = Path(*target_parts)

            dir_files.setdefault(compressed_dir, []).append(
                (output_path, target_path, result.input_path)
            )

        if not dir_files:
            self._safe_ui_log("[dim]无可覆盖的文件。[/]"
            )
            _log.info("覆盖阶段: 无可覆盖的文件, 提前返回")
            return

        total_moved = 0
        total_deleted = 0
        total_failed = 0

        # 重试配置: 总尝试次数 = 1 + retry_count, 失败后等待随机延迟
        ow = self.cfg.overwrite
        retry_count = max(0, int(ow.retry_count))
        delay_ms = ow.retry_delay_ms or [500, 1000]
        delay_lo = float(delay_ms[0])
        delay_hi = float(delay_ms[1]) if len(delay_ms) > 1 else delay_lo

        _log.info(
            "覆盖分组完成 — %d 个 compressed 目录, %d 个文件待移动 "
            "(retry_count=%d, delay=[%.0f,%.0f]ms)",
            len(dir_files), sum(len(v) for v in dir_files.values()),
            retry_count, delay_lo, delay_hi,
        )

        for compressed_dir, file_triples in dir_files.items():
            all_ok = True
            moved = 0
            deleted = 0
            _log.info("[覆盖] 目录开始 %s (%d 个文件)", compressed_dir, len(file_triples))

            for src, dst, original in file_triples:
                # --- 第一步: 移动压缩文件覆盖原图 (核心) ---
                # 失败后: 强制 GC 释放残留句柄 + 随机等待 (缓解 Windows 上
                # 文件被短暂占用 / 只读的 PermissionError), 然后重试。
                # 注意: 先打点再操作 —— 若 os.replace 导致进程崩溃,
                # 日志里仍能看到"正要执行"的记录。
                last_err: Optional[Exception] = None
                last_tb: str = ""
                replaced = False
                for attempt in range(retry_count + 1):
                    _log.info(
                        "[覆盖] 尝试%d: replace %s → %s "
                        "(src存在=%s dst存在=%s 目录=%s)",
                        attempt + 1, src, dst, src.exists(), dst.exists(),
                        dst.parent,
                    )
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        # Windows: 目标文件若带只读属性, os.replace() 覆盖会抛
                        # PermissionError [WinError 5] → 先清除只读属性
                        self._clear_readonly(dst)
                        os.replace(src, dst)  # 原子覆盖同名文件
                        replaced = True
                        _log.info("[覆盖] replace 成功: %s → %s", src.name, dst.name)
                        break
                    except Exception as e:
                        last_err = e
                        last_tb = traceback.format_exc()
                        _log.error(
                            "[覆盖] 尝试%d失败: %s → %s: %s: %s",
                            attempt + 1, src.name, dst.name,
                            type(e).__name__, e,
                        )
                        if attempt < retry_count:
                            gc.collect()
                            time.sleep(random.uniform(delay_lo, delay_hi) / 1000.0)

                if not replaced:
                    all_ok = False
                    total_failed += 1
                    self._safe_ui_log(f"  [red]✗ 覆盖失败: {src.name} → {dst.name}: "
                        f"{type(last_err).__name__}: {last_err}[/]",
                    )
                    _log.error(
                        "覆盖失败 %s → %s | 尝试%d次 | %s: %s\n"
                        "  src      : %s (%s)\n"
                        "  dst      : %s (%s) 占用检测=%s\n"
                        "  original : %s (%s)\n"
                        "  traceback:\n%s",
                        src.name, dst.name, retry_count + 1,
                        type(last_err).__name__, last_err,
                        src, self._file_diagnostics(src),
                        dst, self._file_diagnostics(dst),
                        self._is_locked(dst),
                        original, self._file_diagnostics(original),
                        last_tb,
                    )
                    continue
                moved += 1

                # --- 第二步: 原图与压缩图后缀不同时，删除原图避免共存 ---
                if original.exists() and original.resolve() != dst.resolve():
                    unlinked = False
                    for attempt in range(retry_count + 1):
                        _log.info(
                            "[覆盖] 尝试删除原图 %s (存在=%s 与目标同名=%s)",
                            original, original.exists(),
                            original.resolve() == dst.resolve(),
                        )
                        try:
                            # 原图只读时 unlink() 同样会 PermissionError → 先清除
                            self._clear_readonly(original)
                            original.unlink()
                            unlinked = True
                            _log.info("[覆盖] 删除原图成功: %s", original.name)
                            break
                        except Exception as e:
                            last_err = e
                            last_tb = traceback.format_exc()
                            _log.error(
                                "[覆盖] 删除原图失败: %s: %s: %s",
                                original.name, type(e).__name__, e,
                            )
                            if attempt < retry_count:
                                gc.collect()
                                time.sleep(
                                    random.uniform(delay_lo, delay_hi) / 1000.0
                                )
                    if unlinked:
                        deleted += 1
                    else:
                        all_ok = False
                        total_failed += 1
                        self._safe_ui_log(f"  [yellow]⚠ 已覆盖 {dst.name}，"
                            f"但删除原图 {original.name} 失败: "
                            f"{type(last_err).__name__}: {last_err}[/]",
                        )
                        _log.warning(
                            "覆盖成功但删除原图失败 %s → %s | 尝试%d次 | "
                            "%s: %s\n"
                            "  original: %s (%s) 占用检测=%s\n"
                            "  traceback:\n%s",
                            src.name, original.name, retry_count + 1,
                            type(last_err).__name__, last_err,
                            original, self._file_diagnostics(original),
                            self._is_locked(original),
                            last_tb,
                        )

            if all_ok and moved > 0:
                # All files moved successfully → delete compressed/ directory
                _log.info(
                    "[覆盖] 目录 %s 移动完成 (%d/%d, 删原图 %d), 尝试删除目录",
                    compressed_dir, moved, len(file_triples), deleted,
                )
                try:
                    shutil.rmtree(compressed_dir, onerror=self._rmtree_onerror)
                    _log.info("[覆盖] 已删除目录 %s", compressed_dir)
                    del_info = f", 删除原图 {deleted} 个" if deleted else ""
                    self._safe_ui_log(f"  [green]✓ 已移动 {moved} 个文件{del_info}，"
                        f"已删除 compressed/ 目录[/]",
                    )
                except Exception as e:
                    self._safe_ui_log(f"  [yellow]⚠ 文件已移动，但无法删除目录 "
                        f"{compressed_dir}: {e}[/]",
                    )
            elif not all_ok:
                self._safe_ui_log(f"  [yellow]⚠ 部分失败，保留: {compressed_dir}[/]",
                )
                _log.warning("[覆盖] 目录 %s 部分失败, 保留", compressed_dir)

            total_moved += moved
            total_deleted += deleted
            _log.info("[覆盖] 目录结束 %s (moved=%d deleted=%d)", compressed_dir, moved, deleted)

        self._safe_ui_log(f"[bold]覆盖完成: 成功移动 {total_moved} 个文件"
            + (f", 删除原图 {total_deleted} 个" if total_deleted else "")
            + (f", 失败 {total_failed} 个" if total_failed else "")
            + "[/]",
        )
        _log.info(
            "覆盖完成 — 移动 %d, 删除原图 %d, 失败 %d",
            total_moved, total_deleted, total_failed,
        )
        _log.info("覆盖阶段结束")

    # ------------------------------------------------------------------ #
    #  Message handlers — work 线程投递的 UI 更新 (主线程执行, 非阻塞)
    # ------------------------------------------------------------------ #

    def on_result_ready(self, message: ResultReady) -> None:
        """主线程处理: 压缩完成一条结果 → 更新进度/日志。"""
        self.done_count += 1
        self._ui_on_result(message.result)

    def on_current_file_changed(self, message: CurrentFileChanged) -> None:
        """主线程处理: 更新状态栏"当前文件"。"""
        self._ui_set_current(message.path)

    def on_log_message(self, message: LogMessage) -> None:
        """主线程处理: 写 UI 日志 (覆盖阶段等 work 线程的日志消息)。"""
        try:
            self._ui_log(message.text)
        except Exception:
            pass

    def on_ui_task(self, message: UiTask) -> None:
        """主线程处理: 执行 work 线程投递的任意 UI 任务 (非阻塞)。"""
        try:
            message.fn(*message.args)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  UI helpers (always called on the main thread via call_from_thread)
    # ------------------------------------------------------------------ #

    def _ui_log(self, message: str) -> None:
        self.query_one("#log", RichLog).write(message)

    def _ui_set_current(self, filepath: str) -> None:
        self.current_file = filepath
        self._ui_update_stats()

    def _ui_set_progress(self, total: int, progress: int) -> None:
        bar = self.query_one("#progress", ProgressBar)
        bar.update(total=total, progress=progress)

    def _ui_update_stats(self) -> None:
        self.query_one("#stat-queue", Static).update(
            f"队列: {self.total_count}"
        )
        self.query_one("#stat-done", Static).update(
            f"已完成: {self.done_count}"
        )
        if self.current_file:
            name = Path(self.current_file).name
            if len(name) > _MAX_NAME_LEN:
                name = name[: _MAX_NAME_LEN - 3] + "…"
            self.query_one("#stat-current", Static).update(f"当前: {name}")
        else:
            self.query_one("#stat-current", Static).update("当前: -")

    def _ui_on_result(self, result: CompressionResult) -> None:
        """Log a compression result and refresh stats / progress."""
        log = self.query_one("#log", RichLog)
        name = Path(result.input_path).name

        # 格式信息
        if result.is_video:
            fmt_str = f"{result.fmt.upper() if result.fmt else ''}→MP4 H.265"
            if result.encoder:
                fmt_str += f" [{result.encoder}]"
        elif result.fmt:
            fmt_str = (
                f"{result.fmt.upper()}→"
                f"{cfg.compression.output_format.upper()} q{cfg.compression.quality}"
            )
        else:
            fmt_str = ""

        if result.skipped:
            if result.skip_reason == "animated":
                fmt_label = result.fmt.upper() if result.fmt else ""
                log.write(
                    f"[yellow]⊘[/] {name} → "
                    f"[dim]已跳过（{fmt_label} 动图，未安装 FFmpeg）[/]"
                )
            else:
                # skip_reason == "larger"
                orig_mb = result.original_size / (1024 * 1024)
                out_mb = result.output_size / (1024 * 1024)
                log.write(
                    f"[yellow]⊘[/] {name} → "
                    f"[dim]已跳过（{fmt_str}, "
                    f"压后 {out_mb:.2f} MB ≥ 原 {orig_mb:.2f} MB）[/]"
                )
        elif result.success:
            orig_mb = result.original_size / (1024 * 1024)
            out_mb = result.output_size / (1024 * 1024)
            if result.original_size > 0:
                ratio = (1 - result.output_size / result.original_size) * 100
            else:
                ratio = 0.0

            scale_str = (
                f", 缩放{result.scale_count}次" if result.scale_count else ""
            )
            color = "green" if ratio >= 0 else "red"
            log.write(
                f"[green]✓[/] {name} → "
                f"[cyan]{out_mb:.2f} MB[/] "
                f"(原 {orig_mb:.2f} MB, {fmt_str}{scale_str}, "
                f"[{color}]{ratio:+.0f}%[/])"
            )
        else:
            log.write(
                f"[red]✗[/] {name} → [red]失败:[/] {result.error}"
                + (f" [dim]({fmt_str})[/]" if fmt_str else "")
            )

        # 记录结果到日志文件
        self._log_result(result, fmt_str)

        self._ui_update_stats()
        bar = self.query_one("#progress", ProgressBar)
        bar.update(progress=self.done_count)

    @staticmethod
    def _log_result(result: CompressionResult, fmt_str: str) -> None:
        """将单张图片的压缩结果写入日志文件。"""
        name = Path(result.input_path).name
        orig_kb = result.original_size / 1024
        out_kb = result.output_size / 1024

        if result.skipped:
            if result.skip_reason == "animated":
                _log.info("跳过(动图) %s [%s]", name, result.fmt or "")
            else:
                _log.info(
                    "跳过(压后更大) %s | %s | 原%.0fKB 压后%.0fKB",
                    name, fmt_str, orig_kb, out_kb,
                )
        elif result.success:
            ratio = (
                (1 - result.output_size / result.original_size) * 100
                if result.original_size > 0 else 0.0
            )
            _log.info(
                "成功 %s | %s | 原%.0fKB → %.0fKB (%+.0f%%) 缩放%d次",
                name, fmt_str, orig_kb, out_kb, ratio, result.scale_count,
            )
        else:
            _log.error("失败 %s | %s | %s", name, fmt_str, result.error)

    def _ui_on_complete(self) -> None:
        log = self.query_one("#log", RichLog)
        log.write("")
        log.write(
            f"[bold green]✓ 全部完成！"
            f"共处理 {self.done_count} 张图片。[/]"
        )
        log.write("[dim]可继续拖入新图片进行下一批压缩。[/]")
        self.current_file = ""
        self._ui_update_stats()
        _log.info("批次完成 — 共处理 %d 张图片", self.done_count)


# --------------------------------------------------------------------------- #
#  Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    initial: List[Path] = []
    for arg in sys.argv[1:]:
        p = Path(arg)
        if p.exists():
            initial.append(p)

    app = ImageCompressorApp(initial_paths=initial)
    app.run()


if __name__ == "__main__":
    main()
