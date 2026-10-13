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
#  线程释放 (全项目统一入口)
# --------------------------------------------------------------------- #

# 丢引用前等待线程真正结束的上限 (ms)。终局信号都是 run() 末尾 emit 的,
# 排队到主线程时线程通常只差几条收尾指令, 实测 wait 立即返回。
_THREAD_JOIN_MS = 3000


def release_thread(th: Optional[QThread]) -> Optional[QThread]:
    """安全丢弃 QThread 引用: 先确认线程真的结束了, 否则继续持有。

    ⚠️ 为什么必须这样 (2026-09-25 崩溃修复, 有实测复现):
    PySide6 中 QThread 的**最后一份 Python 引用**被丢掉时会立即 `delete` C++ 对象。
    而本项目各线程的终局信号 (`done` / `finished_all` / `found`) 都是在 `run()`
    末尾 `emit` 的 —— 信号排队到主线程执行时, 工作线程往往还差几条指令才真正退出
    (`isRunning()` 实测仍为 True, 因为 QThread 只在私有 `finish()` 里才把
    running 置 false 并补发 `finished`)。此刻析构 = Qt 明令禁止的
    "destroyed while thread is still running": 工作线程继续写已释放内存 →
    **进程硬崩, 没有任何 Python traceback**, 只在 `logs/gui_console.log` 留一句
    `QThread: Destroyed while thread '' is still running`。
    实测: 丢引用那一刻进程即死 (`exit=127`, 连 print 都来不及输出)。

    用法: `self._x_thread = release_thread(self._x_thread)`
      - 返回 None  → 线程已结束, 引用可以安全丢弃 (析构安全)
      - 返回原对象 → 线程仍在运行, 调用方**继续持有**(宁可多留一个线程对象,
        也绝不析构运行中的线程; 下个批次替换引用时它早已结束)

    等待不会死锁: 本项目信号一律非阻塞队列投递, 工作线程不会反向阻塞主线程。
    """
    if th is None:
        return None
    try:
        running = th.isRunning()
    except RuntimeError:
        # C++ 对象已被 deleteLater 回收 (包装器失效) → 已无需持有
        return None
    if running and not th.wait(_THREAD_JOIN_MS):
        return th
    return None


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
        # 视频收集与 VideoMerger.scan_videos 对齐: 只收 merge_dirs 顶层视频
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
#  合并前视频复查线程
# --------------------------------------------------------------------- #

class VideoRefreshThread(QThread):
    """合并前复查合并目录: 找出"盘子上的视频"里列表还没有的那些。

    为什么需要 (2026-09-23 修复): 压缩阶段会把多帧动图转成 MP4, 覆盖模式下
    立即移回所在目录 —— 这些 MP4 在拖入扫描时并不存在, 所以不在 src_model 里;
    而合并阶段 (VideoMerger.plan_directory) 是**合并前重新扫描磁盘**的, 会把
    它们一并合并。结果就是"文件确实被合并了, 但流图里看不到、分组预览也偏小"。

    这里用与合并器完全相同的扫描规则 (VideoMerger.scan_videos) 复查一遍,
    只扫合并目录顶层 (与合并范围一致), 不递归、不重扫整棵目录树, 因此开销
    与目录数成正比、与目录体积无关。

    用户右键"移除选中"过的路径 (exclude) 保持排除, 不会被复查加回。
    """

    found = Signal(list)   # List[VideoItem] 列表里缺失的视频

    def __init__(self, dirs: List[Path], known: set,
                 exclude: Optional[set] = None) -> None:
        super().__init__()
        self.dirs = list(dirs)
        self.known = set(known)
        self.exclude = set(exclude or ())

    def run(self) -> None:
        seen = set(self.known)
        new_items: List[VideoItem] = []
        for d in self.dirs:
            for p in VideoMerger.scan_videos(Path(d)):
                if p in seen or p in self.exclude:
                    continue
                seen.add(p)
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0            # 体积仅用于统计, 拿不到不阻断
                new_items.append(VideoItem(path=p, size=size))
        self.found.emit(new_items)


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
    """视频合并转码 — 批量规划所有目录后走全局两级并发流水线
    (阶段1 复制流合并 concat_workers 并发 → 阶段2 AV1 转码
    transcode_workers 并发, 跨目录混排)。"""

    log = Signal(str, str)
    dir_started = Signal(str)               # 目录路径 (规划完成, 进入待合并)
    task_started = Signal(str, str)         # (目录, 输出文件名) 子任务开跑
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
        cw = max(1, int(getattr(vm_cfg, "concat_workers", 1) or 1))
        tw = max(1, int(getattr(vm_cfg, "transcode_workers", 1) or 1))
        self.log.emit(f"开始视频合并转码 — {len(dirs)} 个目录"
                      f"（合并并发 {cw} / 转码并发 {tw}）…", "ok")
        _log.info("GUI 视频合并开始 — 目录 %d 个 (concat=%d, transcode=%d)",
                  len(dirs), cw, tw)

        merger = VideoMerger(None, self.cfg)
        if not merger.is_available():
            self.log.emit("✗ FFmpeg/ffprobe 不可用，无法合并视频。", "error")
            return
        self.log.emit(f"编码器 {merger.encoder_display()} | 质量 {vm_cfg.quality} "
                      f"| 关键帧 {vm_cfg.keyframe_interval}s | 音频 "
                      f"{vm_cfg.audio_codec} {vm_cfg.audio_bitrate}", "info")

        # 阶段 A: 逐目录规划 (串行: 扫描/探测/分组/限制)
        plans: List[tuple] = []
        for d in dirs:
            if self.cancel.is_set():
                self.log.emit("已停止 — 剩余目录未规划", "warn")
                break
            self.dir_started.emit(str(d))
            report, tasks = merger.plan_directory(
                d, log_cb=lambda t: self.log.emit(t),
                exclude=self.exclude)
            plans.append((report, tasks))

        # 阶段 B: 全局两级并发流水线 (concat 池 → 转码池)
        def on_start(task, phase: str) -> None:
            self.task_started.emit(str(task.directory),
                                   task.out_path.name)

        def say(t: str, lv: str = "info") -> None:
            self.log.emit(t, lv)

        merger.run_pipeline(plans, vm_cfg, say=say,
                            cancelled=self.cancel, on_start=on_start)

        # 阶段 C: 汇总 + 逐目录报告
        total_merged = 0
        total_skipped = 0
        total_mixed = 0
        total_done = 0
        all_outputs: List[Path] = []
        for report, tasks in plans:
            merger.finalize_report(report, tasks)
            total_merged += report.merged
            total_skipped += report.skipped
            total_mixed += getattr(report, "mixed_groups", 0)
            total_done += getattr(report, "already_merged", 0)
            all_outputs.extend(report.outputs)
            self.dir_done.emit(report)

        self.log.emit(f"视频合并结束: 成功 {total_merged} 组, 跳过 {total_skipped} 组"
                      + (f"（其中 {total_mixed} 组编码不一致已统一转码）"
                         if total_mixed else "")
                      + (f"，{total_done} 个已合并产物未重复处理"
                         if total_done else "")
                      + (f"，输出 {len(all_outputs)} 个文件" if all_outputs else ""),
                      "ok" if total_merged else "warn")
        _log.info("GUI 视频合并结束 — 成功 %d, 跳过 %d, 混编统一 %d, 已有产物 %d, "
                  "输出 %s", total_merged, total_skipped, total_mixed,
                  total_done, [str(o) for o in all_outputs])


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
