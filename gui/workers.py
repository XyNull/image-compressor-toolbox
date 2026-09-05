"""后台线程: 扫描 / 探测 / 压缩 / 合并 — 全部 QThread, 结果经 Signal 回主线程。

对齐 TUI 的经验: 跨线程 UI 更新一律走信号(非阻塞), 绝不阻塞等待主线程。
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Signal, QThread

import win_perf
from compressor import CompressionResult, ImageCompressor
from detector import SUPPORTED_FORMATS, detect_format
from video_merge import VIDEO_EXTS, VideoMerger, VideoProbe

from .models import QueueItem, VideoItem
from .overwrite import detect_leftover, execute_overwrite


# --------------------------------------------------------------------- #
#  扫描线程
# --------------------------------------------------------------------- #

class ScanThread(QThread):
    """扫描拖入路径: 图片入队 / 视频入视频表 / 目录记入合并候选 / 残留检测。"""

    done = Signal(object, object, list, list, int)  # images, videos, merge_dirs, leftover, skipped
    log = Signal(str, str)

    def __init__(self, paths: List[Path], existing: set,
                 leftover_seen: set, cfg) -> None:
        super().__init__()
        self.paths = paths
        self.existing = set(existing)
        self.leftover_seen = set(leftover_seen)
        self.cfg = cfg
        self.cancelled = False

    # --- 目录递归 (os.scandir 缓存 stat, SMB 友好) --------------------- #
    @staticmethod
    def _scan_directory(root: Path) -> List[tuple]:
        results: List[tuple] = []
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
                                        follow_symlinks=False).st_size
                                except OSError:
                                    size = 0
                                results.append((Path(entry.path), size))
                        except OSError:
                            pass
            except OSError:
                pass
        return results

    def run(self) -> None:
        cfg = self.cfg
        images: List[QueueItem] = []
        videos: List[VideoItem] = []
        merge_dirs: List[Path] = []
        skipped_non_image = 0

        for path in self.paths:
            try:
                if path.is_dir() and path not in merge_dirs:
                    merge_dirs.append(path)
                elif (path.is_file() and path.suffix.lower() in VIDEO_EXTS
                        and path.parent not in merge_dirs):
                    merge_dirs.append(path.parent)
            except OSError:
                pass

        # Phase 1: 收集候选 (path, size)
        candidates: List[tuple] = []
        for path in self.paths:
            try:
                if path.is_file():
                    try:
                        size = path.stat().st_size
                    except OSError:
                        size = 0
                    candidates.append((path, size))
                elif path.is_dir():
                    candidates.extend(self._scan_directory(path))
            except OSError:
                pass

        # Phase 2: 并行格式检测
        scan_workers = max(1, cfg.io.scan_workers)

        def _detect_one(item):
            p, s = item
            return p, s, detect_format(p)

        detected: List[tuple] = []
        if len(candidates) <= 4:
            for item in candidates:
                detected.append(_detect_one(item))
        else:
            with ThreadPoolExecutor(
                max_workers=scan_workers,
                initializer=win_perf.boost_current_thread,
            ) as pool:
                detected = list(pool.map(_detect_one, candidates))

        # Phase 3: 分类入列
        # 视频收集与 VideoMerger._scan_videos 对齐: 只收 merge_dirs 顶层视频
        merge_dir_set = set(merge_dirs)
        video_seen: set = set()
        for p, size, fmt in detected:
            if fmt and fmt in SUPPORTED_FORMATS:
                if p not in self.existing:
                    root = None
                    for input_path in self.paths:
                        if input_path.is_dir():
                            try:
                                p.relative_to(input_path)
                                root = input_path
                                break
                            except ValueError:
                                pass
                    images.append(QueueItem(path=p, root=root, fmt=fmt,
                                            size=size))
                    self.existing.add(p)
            elif p.suffix.lower() in VIDEO_EXTS and p.parent in merge_dir_set:
                if p not in video_seen:
                    video_seen.add(p)
                    videos.append(VideoItem(path=p, size=size))
            else:
                skipped_non_image += 1

        leftover = detect_leftover(self.paths, self.leftover_seen)
        self.done.emit(images, videos, merge_dirs, leftover, skipped_non_image)


# --------------------------------------------------------------------- #
#  视频探测线程
# --------------------------------------------------------------------- #

class ProbeThread(QThread):
    """用 VideoMerger.probe 逐个探测视频 (串行, ffprobe 子进程)。"""

    probed = Signal(object)   # VideoProbe
    finished_all = Signal()

    def __init__(self, videos: List[VideoItem], cfg) -> None:
        super().__init__()
        self.videos = videos
        self.cfg = cfg
        self.cancelled = threading.Event()

    def run(self) -> None:
        merger = VideoMerger(None, self.cfg)
        if not merger.ffprobe:
            self.finished_all.emit()
            return
        for item in self.videos:
            if self.cancelled.is_set():
                break
            probe = merger.probe(item.path)
            self.probed.emit(probe)
        self.finished_all.emit()


# --------------------------------------------------------------------- #
#  压缩线程
# --------------------------------------------------------------------- #

class CompressThread(QThread):
    """图片压缩 (线程池) + 覆盖阶段 (可选) — 复用 ImageCompressor。"""

    item_done = Signal(object, object)      # QueueItem, CompressionResult
    current = Signal(str)                   # 当前文件路径
    log = Signal(str, str)
    stage = Signal(str)                     # 阶段提示
    finished_all = Signal(bool)             # 正常完成?

    def __init__(self, items: List[QueueItem], cfg, overwrite_mode: bool,
                 cancel: threading.Event) -> None:
        super().__init__()
        self.items = items
        self.cfg = cfg
        self.overwrite_mode = overwrite_mode
        self.cancel = cancel

    def run(self) -> None:
        ok = True
        try:
            self._run()
        except Exception as e:
            import traceback
            self.log.emit(f"✗ 压缩阶段异常: {type(e).__name__}: {e} "
                          f"(详情见日志)", "error")
            from logger import get_logger
            get_logger(self.cfg.logging.log_dir).error(
                "GUI 压缩异常 %s: %s\n%s", type(e).__name__, e,
                traceback.format_exc())
            ok = False
        self.finished_all.emit(ok)

    def _run(self) -> None:
        from logger import get_logger
        _log = get_logger(self.cfg.logging.log_dir)
        cfg = self.cfg
        images = self.items
        mode_str = "覆盖模式" if self.overwrite_mode else "子目录模式"
        self.stage.emit(f"压缩 {len(images)} 张图片（{mode_str}）")
        self.log.emit(f"开始压缩 {len(images)} 张图片（{mode_str}）…", "ok")
        _log.info("GUI 批次开始 — %d 张图片, 模式=%s", len(images), mode_str)

        prio = win_perf.restore_priority()
        if prio:
            _log.warning(prio)

        compressor = ImageCompressor(cfg=cfg)
        max_workers = cfg.parallel.max_workers or (os.cpu_count() or 4)
        all_results: List[CompressionResult] = []

        pending = list(images)
        with ThreadPoolExecutor(
            max_workers=max_workers,
            initializer=win_perf.boost_current_thread,
        ) as pool:
            futures = {}
            for img in pending:
                if self.cancel.is_set():
                    break
                futures[pool.submit(
                    compressor.compress, img.path, img.root,
                    img.fmt, img.size)] = img

            done = 0
            for future in as_completed(futures):
                img = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = CompressionResult(
                        input_path=img.path, output_path=None,
                        success=False, error=str(exc))
                done += 1
                all_results.append(result)
                self.item_done.emit(img, result)
                if done % 50 == 0 or done == len(futures):
                    _log.info("GUI 压缩进度 %d/%d", done, len(futures))

            if self.cancel.is_set():
                skipped = len(pending) - len(futures)
                if skipped > 0:
                    self.log.emit(f"已停止 — 剩余 {skipped} 张未处理", "warn")

        self.log.emit(f"压缩阶段完成: 成功 "
                      f"{sum(1 for r in all_results if r.success and not r.skipped)}, "
                      f"跳过 {sum(1 for r in all_results if r.skipped)}, "
                      f"失败 {sum(1 for r in all_results if not r.success)}",
                      "info")

        # 覆盖模式: 移回并覆盖原图 (被"停止"截断的半批不执行, 防部分覆盖)
        partial = self.cancel.is_set() and len(all_results) < len(pending)
        if self.overwrite_mode and all_results and not partial:
            import gc
            _log.info("GUI 开始 GC (释放残留句柄)")
            gc.collect()
            execute_overwrite(all_results, cfg, log_cb=self.log.emit)


# --------------------------------------------------------------------- #
#  合并线程
# --------------------------------------------------------------------- #

class MergeThread(QThread):
    """视频合并转码 — 对每个候选目录串行调用 VideoMerger.merge_directory。"""

    log = Signal(str, str)
    dir_started = Signal(str)               # 目录路径
    dir_done = Signal(object)               # MergeReport
    finished_all = Signal(bool)

    def __init__(self, dirs: List[Path], cfg,
                 cancel: threading.Event,
                 exclude: Optional[set] = None) -> None:
        super().__init__()
        self.dirs = dirs
        self.cfg = cfg
        self.cancel = cancel
        self.exclude = exclude  # 用户在视频列表移除的路径, 不参与合并

    def run(self) -> None:
        ok = True
        try:
            self._run()
        except Exception as e:
            import traceback
            self.log.emit(f"✗ 视频合并异常: {type(e).__name__}: {e}", "error")
            from logger import get_logger
            get_logger(self.cfg.logging.log_dir).error(
                "GUI 合并异常 %s: %s\n%s", type(e).__name__, e,
                traceback.format_exc())
            ok = False
        self.finished_all.emit(ok)

    def _run(self) -> None:
        from logger import get_logger
        _log = get_logger(self.cfg.logging.log_dir)
        dirs = [d for d in self.dirs if not self.cancel.is_set()]
        if not dirs:
            return
        vm_cfg = self.cfg.video_merge
        self.log.emit(f"开始视频合并转码 — {len(dirs)} 个目录…", "ok")
        _log.info("GUI 视频合并开始 — 目录 %d 个", len(dirs))

        merger = VideoMerger(None, self.cfg)
        if not merger.is_available():
            self.log.emit("✗ FFmpeg/ffprobe 不可用，无法合并视频。", "error")
            return
        self.log.emit(f"编码器 {merger.encoder_display()} | 质量 {vm_cfg.quality} "
                      f"| 关键帧 {vm_cfg.keyframe_interval}s | 音频 "
                      f"{vm_cfg.audio_codec} {vm_cfg.audio_bitrate}", "info")

        total_merged = 0
        total_skipped = 0
        all_outputs: List[Path] = []
        for d in dirs:
            if self.cancel.is_set():
                self.log.emit("已停止 — 剩余目录未合并", "warn")
                break
            self.dir_started.emit(str(d))
            report = merger.merge_directory(
                d, log_cb=lambda t, lv="info": self.log.emit(t, lv),
                exclude=self.exclude)
            total_merged += report.merged
            total_skipped += report.skipped
            all_outputs.extend(report.outputs)
            self.dir_done.emit(report)

        self.log.emit(f"视频合并结束: 成功 {total_merged} 组, 跳过 {total_skipped} 组"
                      + (f"，输出 {len(all_outputs)} 个文件" if all_outputs else ""),
                      "ok" if total_merged else "warn")
        _log.info("GUI 视频合并结束 — 成功 %d, 跳过 %d, 输出 %s",
                  total_merged, total_skipped, [str(o) for o in all_outputs])


# --------------------------------------------------------------------- #
#  恢复覆盖线程
# --------------------------------------------------------------------- #

class RedoThread(QThread):
    """把 compressed/ 残留移回覆盖原图 (恢复覆盖, 复用 execute_overwrite)。"""

    log = Signal(str, str)
    finished_all = Signal(dict)  # {"moved","deleted","failed"}

    def __init__(self, results: List[CompressionResult], cfg) -> None:
        super().__init__()
        self.results = results
        self.cfg = cfg

    def run(self) -> None:
        stats = execute_overwrite(self.results, self.cfg,
                                  log_cb=self.log.emit)
        self.finished_all.emit(stats)
