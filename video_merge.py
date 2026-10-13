"""同目录视频合并 + AV1 转码 (按 v 触发)。

架构 (2026-09-11 起, 两级并发流水线):
  规划 plan_directory(每目录, 串行):
    1. 扫描目录顶层视频文件（不递归、跳过 compressed/ 等子目录）。
    2. ffprobe 探测每个视频的分辨率 / 帧率 / 视频编码 / 视频流时长。
    3. 按分辨率分组（容差 tolerance_px: 1918x1080 ≈ 1920x1080）。
    3b. 划出"前期合并产物"并剔除 (detect_merged_outputs): 命名符合输出规则
       (V1.mp4) + 编码已是 AV1 + 该分辨率组内仅此一份 → 二次运行时不再重复
       合并/编码 (成品已是最终形态, 且没有可拼的对象)。
    4. 限制检查: 组内视频数 > max_videos_per_group → 跳过该组;
       分辨率组数 > max_output_files → 整目录跳过; 目标已存在 → 跳过。
       组内编码不一致 → mixed_codec_merge 开 (默认): 标记为"混编组", 走
       统一规格单次编码 (见下), 不再跳过; 关: 跳过该组 (旧行为)。
  混编组 (编码不一致) 的处理:
     复制流拼接要求组内编码/参数完全一致, 混编组改用 concat 滤镜一次完成
     "逐源解码 → 统一分辨率/像素格式/帧率/时基 → 拼接 → 编码 AV1"。
     等价于"先把每条转成 AV1 再拼接", 但只编码一次 (少一次编解码, 画质与
     耗时都更优), 也避免 N 个并发硬件编码挤爆会话; 音轨按 aformat 统一
     (缺音轨的源用等长静音补齐)。
  流水线 run_pipeline(可跨目录混排):
    阶段1 复制流合并: ffmpeg -f concat -safe 0 -c copy + -fflags +genpts,
      并发 concat_workers (默认 10); 混编组没有复制流这一步, 直接进阶段2。
    阶段2 AV1 转码: 每组 concat 完成即入转码池 (流水线衔接, 不等全局栅栏),
      并发 transcode_workers (默认 2)。首选 av1_qsv (HW:QSV), 回退
      libsvtav1 / libaom-av1; 质量 quality、关键帧 keyframe_interval、
      音频 AAC LC 128k 声道同原版、无字幕、不改变分辨率。
      ⚠ 时长虚增/冻结帧对策 (normalize_pts, 默认开): 转码前 -vf
      setpts=N/fps/TB 重排视频时间戳 — concat 产物/QSV 编码器对不干净
      时间戳 (PTS 空洞/非单调) 会放大为成片时长虚增+中段无法播放, 重排后
      成片时长 = 帧数/fps (格式工厂正常而 ShanaEncoder 异常正是这条路径差异)。
  校验: 成片视频流时长 vs 源视频流时长之和, 偏差超 max(pct%, min_s) →
    从 concat 列表直接重转码修复 (duration_repair), 仍异常上报
    MergeReport.duration_issues (GUI 流图红色感叹号)。
  汇总 finalize_report: 按任务状态累计 merged/skipped 并生成目录摘要。

兼容: merge_directory(单目录) = plan + pipeline + finalize, TUI 继续使用;
GUI MergeThread 用 plan_directory × N + run_pipeline 跨目录全局并发。

合并范围 (顶层扫描, 见 scan_videos): 只取合并目录**顶层**的视频文件 (不递归,
因此 compressed/ 子目录天然不在范围内)。注意子目录模式下动图转码产物留在
compressed/ 里 → **不参与合并**; 覆盖模式下它们会被移回所在目录 → 与真实视频
**同等参与合并** (GUI 合并流图据此复查列表, 见 gui/workers.VideoRefreshThread)。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from winproc import HIDDEN_KW

from config import Config, load_config
from hwaccel import HardwareAccelerator
from logger import get_logger

try:                      # 线程池 initializer: 退出 Windows EcoQoS (可选)
    import win_perf
    _POOL_INIT = getattr(win_perf, "boost_current_thread", None)
except Exception:
    _POOL_INIT = None

_log = get_logger()

# 常见视频扩展名（小写）
VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm",
    ".m4v", ".ts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".rmvb",
    ".vob", ".mts",
}

# 编码器优先级（配置 encoder/fallback 之外的最后兜底）
_ENCODER_FALLBACK_ORDER = ["av1_qsv", "libsvtav1", "libaom-av1"]

# AV1 编码器 → 质量参数名
_QUALITY_ARG = {"av1_qsv": "-global_quality", "libsvtav1": "-crf", "libaom-av1": "-crf"}

# 任务状态: pending → concat_ok/concat_failed → done/transcode_failed;
# cancelled = 停止后未执行
_ST_PENDING = "pending"
_ST_CONCAT_OK = "concat_ok"
_ST_CONCAT_FAIL = "concat_failed"
_ST_DONE = "done"
_ST_TRANSCODE_FAIL = "transcode_failed"
_ST_CANCELLED = "cancelled"


@dataclass
class VideoProbe:
    """ffprobe 探测结果。"""
    path: Path
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    duration: float = 0.0     # 视频流时长 (秒, 容器时长兜底), 解析失败为 0
    ok: bool = False
    error: str = ""


@dataclass
class _AudioInfo:
    """ffprobe 音频流探测结果 (混编组对齐音轨用)。"""
    has_audio: bool = False
    codec: str = ""
    channels: int = 0
    layout: str = ""          # channel_layout (stereo / 5.1 ...)
    sample_rate: int = 0


@dataclass
class MergeGroup:
    """同一分辨率的一组视频。"""
    width: int = 0
    height: int = 0
    videos: List[Path] = field(default_factory=list)
    duration_sum: float = 0.0   # 组内源视频时长之和 (秒), 时长校验期望值


@dataclass
class MergeReport:
    """单个目录的合并结果。"""
    directory: Path
    total_videos: int = 0
    groups: int = 0
    merged: int = 0          # 成功生成的组数
    skipped: int = 0         # 跳过的组数
    mixed_groups: int = 0    # 其中"编码不一致 → 统一转码后合并"的组数
    already_merged: int = 0  # 识别为"前期合并产物"而不再处理的视频数
    outputs: List[Path] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)  # 提示/原因
    # 时长异常: (输出文件, 期望时长s, 实际时长s) — 输出保留, 由 UI 标红提醒
    duration_issues: List[Tuple[Path, float, float]] = field(
        default_factory=list)
    # 时长校验数据: (输出文件, 期望s, 实际s) — 含全部正常输出 (tooltip 展示)
    duration_info: List[Tuple[Path, float, float]] = field(
        default_factory=list)


class _GroupTask:
    """一个待合并组 (跨目录流水线的调度单元)。"""

    __slots__ = ("directory", "group", "out_path", "report",
                 "probe_by_path", "fps", "tmpdir", "list_file",
                 "merged_tmp", "state", "mixed")

    def __init__(self, directory: Path, group: MergeGroup, out_path: Path,
                 report: MergeReport, probe_by_path: dict, fps: float,
                 mixed: bool = False) -> None:
        self.directory = directory
        self.group = group
        self.out_path = out_path
        self.report = report
        self.probe_by_path = probe_by_path
        self.fps = fps
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vmerge_"))
        self.list_file = self.tmpdir / "concat.txt"
        self.merged_tmp = self.tmpdir / "merged.mp4"
        self.state = _ST_PENDING
        # 混编组 (组内视频编码不一致): 跳过复制流拼接, 走统一规格单次编码
        self.mixed = mixed


def fmt_duration(sec: float) -> str:
    """秒 → 'M:SS.s' / 'H:MM:SS.s' 展示文本; 非正数返回 '—'。"""
    if sec <= 0:
        return "—"
    s = float(sec)
    h = int(s // 3600)
    s -= h * 3600
    m = int(s // 60)
    s -= m * 60
    if h:
        return f"{h}:{m:02d}:{s:04.1f}"
    return f"{m}:{s:04.1f}"


def _parse_duration(raw) -> float:
    """ffprobe 时长字段解析: 'N/A'/None/异常 → 0。"""
    if raw is None:
        return 0.0
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return 0.0


class VideoMerger:
    """合并同目录同分辨率视频并转码为 AV1。"""

    def __init__(
        self,
        hw_accel: Optional[HardwareAccelerator] = None,
        cfg: Optional[Config] = None,
    ) -> None:
        self.hw = hw_accel or HardwareAccelerator()
        self.cfg = cfg or load_config()
        self.ffmpeg: Optional[str] = shutil.which("ffmpeg")
        self.ffprobe: Optional[str] = shutil.which("ffprobe")
        self._encoder: Optional[str] = None
        self._encoder_checked = False

    # ------------------------------------------------------------------ #
    #  可用性 & 编码器选择
    # ------------------------------------------------------------------ #

    def is_available(self) -> bool:
        return self.ffmpeg is not None and self.ffprobe is not None

    def pick_encoder(self) -> Optional[str]:
        """按配置选择可用的 AV1 编码器: 首选 → 回退 → 最后兜底。

        缓存探测结果（-encoders 只查一次）。
        """
        if self._encoder_checked:
            return self._encoder
        self._encoder_checked = True
        if not self.ffmpeg:
            return None
        try:
            result = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=10,
                **HIDDEN_KW,
            )
            available = set()
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    available.add(parts[1])
        except (subprocess.SubprocessError, OSError):
            return None

        order: List[str] = []
        vm = self.cfg.video_merge
        if vm.encoder and vm.encoder not in order:
            order.append(vm.encoder)
        if vm.fallback_encoder and vm.fallback_encoder not in order:
            order.append(vm.fallback_encoder)
        for enc in _ENCODER_FALLBACK_ORDER:
            if enc not in order:
                order.append(enc)

        for enc in order:
            if enc in available:
                self._encoder = enc
                return enc
        return None

    def encoder_display(self) -> str:
        """UI/日志展示用: 实际选中的编码器。"""
        enc = self.pick_encoder()
        if not enc:
            return "无可用 AV1 编码器"
        if enc == "av1_qsv":
            return "av1_qsv (Intel QSV 硬件)"
        return f"{enc} (CPU)"

    # ------------------------------------------------------------------ #
    #  探测
    # ------------------------------------------------------------------ #

    def probe(self, path: Path) -> VideoProbe:
        """ffprobe 探测单个视频: 分辨率 / 帧率 / 视频编码 / 视频流时长。"""
        probe = VideoProbe(path=path)
        if not self.ffprobe:
            probe.error = "ffprobe 不可用"
            return probe
        try:
            result = subprocess.run(
                [
                    self.ffprobe, "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries",
                    "stream=width,height,r_frame_rate,codec_name,duration:"
                    "format=duration",
                    "-of", "json",
                    str(path),
                ],
                capture_output=True, text=True, timeout=30,
                **HIDDEN_KW,
            )
            if result.returncode != 0:
                probe.error = (result.stderr or result.stdout).strip()[:200]
                return probe
            data = json.loads(result.stdout)
            stream = (data.get("streams") or [{}])[0]
            probe.width = int(stream.get("width") or 0)
            probe.height = int(stream.get("height") or 0)
            probe.vcodec = str(stream.get("codec_name") or "")
            probe.fps = self._parse_fps(str(stream.get("r_frame_rate") or ""))
            # 视频流时长优先 (虚增/冻结发生在视频流上; 音频更长时不被拉高),
            # 流时长缺失 (如 TS) 回退容器时长
            probe.duration = _parse_duration(stream.get("duration"))
            if probe.duration <= 0:
                probe.duration = _parse_duration(
                    data.get("format", {}).get("duration"))
            probe.ok = probe.width > 0 and probe.height > 0
            if not probe.ok:
                probe.error = "未探测到视频流/分辨率"
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError, ValueError) as exc:
            probe.error = f"{type(exc).__name__}: {exc}"
        return probe

    def probe_duration(self, path: Path) -> float:
        """ffprobe 读取视频流时长 (秒, 容器时长兜底); 失败返回 0。"""
        if not self.ffprobe:
            return 0.0
        try:
            result = subprocess.run(
                [
                    self.ffprobe, "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True, text=True, timeout=30,
                **HIDDEN_KW,
            )
            dur = _parse_duration(result.stdout.strip())
            if dur > 0:
                return dur
            result = subprocess.run(
                [
                    self.ffprobe, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True, text=True, timeout=30,
                **HIDDEN_KW,
            )
            return _parse_duration(result.stdout.strip())
        except (subprocess.SubprocessError, OSError, ValueError):
            return 0.0

    @staticmethod
    def _parse_fps(r_frame_rate: str) -> float:
        """'30000/1001' → 29.97; '25/1' → 25; 异常 → 0。"""
        try:
            num, _, den = r_frame_rate.partition("/")
            num, den = float(num), float(den or 1)
            return round(num / den, 3) if den > 0 and num > 0 else 0.0
        except (ValueError, ZeroDivisionError):
            return 0.0

    # ------------------------------------------------------------------ #
    #  扫描 & 分组
    # ------------------------------------------------------------------ #

    @staticmethod
    def scan_videos(directory: Path) -> List[Path]:
        """扫描目录顶层视频文件 — **合并范围的唯一定义**。

        只取顶层文件 (不递归): compressed/ 等子目录里的文件天然不入选;
        隐藏文件 (以 . 开头) 跳过。GUI 合并前复查列表复用本方法, 保证
        "流图显示的" 与 "实际合并的" 是同一批文件。
        """
        videos: List[Path] = []
        try:
            with __import__("os").scandir(directory) as it:
                for entry in it:
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if entry.name.startswith("."):
                            continue
                        if entry.name.lower().endswith(tuple(VIDEO_EXTS)):
                            videos.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError as exc:
            _log.warning("[视频合并] 扫描目录失败 %s: %s", directory, exc)
        return sorted(videos)

    def _is_output_name(self, name: str, vm) -> bool:
        """文件名是否符合输出命名规则 (output_prefix + 序号 + output_extension)。

        默认即 `V1.mp4` / `V2.mp4`; 大小写不敏感 (V1.MP4 也算)。
        """
        prefix = vm.output_prefix if vm.output_prefix is not None else "V"
        ext = (vm.output_extension or "mp4").lstrip(".")
        return re.match(
            rf"^{re.escape(prefix)}\d+\.{re.escape(ext)}$", name,
            re.IGNORECASE,
        ) is not None

    def detect_merged_outputs(
        self,
        groups: List[MergeGroup],
        probe_by_path: dict,
        vm=None,
    ) -> List[Path]:
        """划出**前期合并产物** —— 二次运行时不必再合并/转码的视频。

        判据 (三条同时满足, 2026-10-07 用户需求):
          1. 文件名符合输出命名 (output_prefix + 序号 + output_extension, 如 V1.mp4);
          2. 视频编码已经是 AV1 (即已经走过本工具的合并转码);
          3. 在本目录内它的分辨率组**只有它自己** —— 没有别的视频能跟它合并
             (能合并就该照常参与, 否则会白白丢掉可合并的机会)。

        只按 1+2 判断会误伤: 用户完全可能拿上一次的成品当素材再合一次; 加上第 3 条
        "组内独苗"才等价于"这个成品没人可拼, 再跑一遍只会重复编码"。
        """
        vm = vm or self.cfg.video_merge
        merged: List[Path] = []
        for g in groups:
            if len(g.videos) != 1:          # 组内有别的视频 → 还能合并, 不当成品
                continue
            p = g.videos[0]
            pr = probe_by_path.get(p)
            if pr is None or not pr.ok:
                continue
            if not self._is_output_name(p.name, vm):
                continue
            if not (pr.vcodec or "").lower().startswith("av1"):
                continue
            merged.append(p)
        return merged

    def _group_videos(self, probes: List[VideoProbe]) -> List[MergeGroup]:
        """按分辨率分组，容差 tolerance_px（宽高差 ≤ 容差视为同分辨率）。

        组代表分辨率取组内第一个视频；为确定性，先按面积降序排列。
        """
        tol = max(0, int(self.cfg.video_merge.tolerance_px))
        groups: List[MergeGroup] = []

        def find_group(w: int, h: int) -> Optional[MergeGroup]:
            for g in groups:
                if abs(g.width - w) <= tol and abs(g.height - h) <= tol:
                    return g
            return None

        for p in sorted(probes, key=lambda x: x.width * x.height, reverse=True):
            g = find_group(p.width, p.height)
            if g is None:
                g = MergeGroup(width=p.width, height=p.height)
                groups.append(g)
            g.videos.append(p.path)
        return groups

    # ------------------------------------------------------------------ #
    #  规划 (每目录: 扫描/探测/分组/限制 → 任务列表)
    # ------------------------------------------------------------------ #

    def plan_directory(
        self,
        directory: Path,
        log_cb: Optional[Callable[[str], None]] = None,
        exclude: Optional[set] = None,
    ) -> Tuple[MergeReport, List[_GroupTask]]:
        """扫描→探测→分组→限制检查, 返回 (报告, 待合并任务列表)。

        log_cb: 过程日志回调; exclude: 排除的视频路径集合。
        """
        report = MergeReport(directory=Path(directory))

        def say(msg: str) -> None:
            if log_cb:
                try:
                    log_cb(msg)
                except Exception:
                    pass
            _log.info("[视频合并] %s", msg)

        if not self.is_available():
            say("[red]✗ FFmpeg/ffprobe 不可用，无法执行视频合并。[/]")
            report.messages.append("FFmpeg/ffprobe 不可用")
            return report, []

        vm = self.cfg.video_merge
        say(f"[cyan]扫描目录: {directory}[/]")

        videos = self.scan_videos(directory)
        if exclude:
            excluded = [v for v in videos if v in exclude]
            if excluded:
                say(f"  [dim]按用户选择排除 {len(excluded)} 个视频"
                    f"（如: {excluded[0].name}）[/]")
            videos = [v for v in videos if v not in exclude]
        report.total_videos = len(videos)
        if not videos:
            say("[dim]  未发现视频文件，跳过。[/]")
            report.messages.append("无视频文件")
            return report, []
        say(f"  发现 [cyan]{len(videos)}[/] 个视频文件")

        # 探测
        probes: List[VideoProbe] = []
        for v in videos:
            p = self.probe(v)
            if p.ok:
                probes.append(p)
            else:
                say(f"[yellow]⚠ 探测失败，跳过: {v.name} ({p.error})[/]")
                report.skipped += 1
                report.messages.append(f"探测失败: {v.name}")
        if not probes:
            say("[yellow]所有视频均无法探测，跳过。[/]")
            return report, []

        # 分组
        groups = self._group_videos(probes)
        probe_by_path = {p.path: p for p in probes}

        # 前期合并产物 (已是 AV1 的 V1.mp4 等, 且本目录同分辨率仅此一份):
        # 不再当原料重复合并/编码 —— 必须在"组数上限"判断之前剔除, 否则成品
        # 会虚增组数 (放 5 个 V*.mp4 就让整个目录超限不合并)。
        done_outputs = self.detect_merged_outputs(groups, probe_by_path, vm)
        if done_outputs:
            done_set = set(done_outputs)
            for g in groups:
                g.videos = [v for v in g.videos if v not in done_set]
            groups = [g for g in groups if g.videos]
            report.already_merged = len(done_outputs)
            names = "、".join(p.name for p in done_outputs[:3])
            more = f" 等 {len(done_outputs)} 个" if len(done_outputs) > 3 else ""
            say(f"  [dim]跳过 {len(done_outputs)} 个前期合并产物"
                f"（已是 AV1 成品且无可合并对象）: {names}{more}[/]")
            report.messages.append(
                f"跳过 {len(done_outputs)} 个已合并产物")
            if not groups:
                say("[dim]  其余无可合并的组，跳过。[/]")
                report.messages.append("无待合并组")
                return report, []

        report.groups = len(groups)
        say(f"  按分辨率分组: [cyan]{len(groups)}[/] 组")
        for g in groups:
            g.duration_sum = sum(probe_by_path[v].duration for v in g.videos)
            say(f"    {g.width}x{g.height}: {len(g.videos)} 个"
                f" · 共 {fmt_duration(g.duration_sum)}")

        # 限制 1: 目标文件数（分辨率组数）上限
        if len(groups) > vm.max_output_files:
            say(f"[yellow]⚠ 分辨率组数 {len(groups)} 超过上限 "
                f"{vm.max_output_files}（目标文件将超过 {vm.max_output_files} 个），"
                f"本目录不合并。[/]")
            report.messages.append(
                f"分组数 {len(groups)} > {vm.max_output_files}，不合并"
            )
            return report, []

        # 限制 2: 单组视频数上限 (计数一次; 下方任务构建直接跳过空组)
        for g in groups:
            if len(g.videos) > vm.max_videos_per_group:
                say(f"[yellow]⚠ 组 {g.width}x{g.height} 有 {len(g.videos)} 个视频，"
                    f"超过上限 {vm.max_videos_per_group}，跳过该组。[/]")
                report.messages.append(
                    f"{g.width}x{g.height} 组 {len(g.videos)} > "
                    f"{vm.max_videos_per_group}，跳过"
                )
                report.skipped += 1
                g.videos = []

        # 构建任务 (目标已存在 / 编码不一致 → 跳过)
        tasks: List[_GroupTask] = []
        out_index = 0
        for g in groups:
            if not g.videos:
                continue                     # 限制跳过, 已计数
            out_index += 1
            out_name = f"{vm.output_prefix}{out_index}.{vm.output_extension}"
            out_path = directory / out_name

            if out_path.exists() and not vm.overwrite_existing:
                say(f"[yellow]⚠ 目标文件已存在，跳过: {out_name}[/]")
                report.skipped += 1
                report.messages.append(f"{out_name} 已存在，跳过")
                continue

            # 组内编码一致性 (concat demuxer 要求)
            vcodes: set[str] = set()
            for v in g.videos:
                p = probe_by_path.get(v)
                if p is not None and p.vcodec:
                    vcodes.add(p.vcodec)
            mixed = len(vcodes) > 1
            if mixed:
                codec_str = ", ".join(sorted(v for v in vcodes if v))
                if not getattr(vm, "mixed_codec_merge", True):
                    say(f"[yellow]⚠ 组 {g.width}x{g.height} 视频编码不一致 "
                        f"({codec_str})，跳过合并。[/]")
                    report.messages.append(
                        f"{g.width}x{g.height} 编码不一致，跳过"
                    )
                    report.skipped += 1
                    continue
                # 混编组: 不走复制流拼接, 改为"统一规格 → 拼接 → 一次编码 AV1"
                say(f"[cyan]组 {g.width}x{g.height} 视频编码不一致 "
                    f"({codec_str}) → 逐源统一规格后拼接并转 AV1[/]")
                report.messages.append(
                    f"{g.width}x{g.height} 编码不一致 ({codec_str}) "
                    f"→ 统一转码后合并"
                )
                report.mixed_groups += 1

            # 组内帧率 (关键帧间隔换算 / setpts 重排用)
            fps = next((probe_by_path[v].fps for v in g.videos
                        if probe_by_path.get(v) and probe_by_path[v].fps > 0),
                       0.0)
            tasks.append(_GroupTask(
                directory=Path(directory), group=g, out_path=out_path,
                report=report, probe_by_path=probe_by_path, fps=fps,
                mixed=mixed))
        return report, tasks

    # ------------------------------------------------------------------ #
    #  两级并发流水线 (concat 池 → 转码池, 可跨目录混排)
    # ------------------------------------------------------------------ #

    def run_pipeline(
        self,
        plans: List[Tuple[MergeReport, List[_GroupTask]]],
        vm,
        say: Callable[[str], None],
        cancelled=None,
        on_start: Optional[Callable[[_GroupTask, str], None]] = None,
    ) -> None:
        """执行全部任务: 阶段1 复制流合并 (concat_workers 并发) →
        阶段2 AV1 转码 (transcode_workers 并发, 各组 concat 完成即入池)。

        plans: [(目录报告, 任务列表), ...]; cancelled: threading.Event 协作取消
        (已开始的子任务会完成, 未提交的标记 cancelled); on_start(task, 阶段)
        在每个子任务开跑时回调 (GUI 跟随动画用)。
        """
        items = [(t, rep) for rep, tasks in plans for t in tasks]
        if not items:
            return
        encoder = self.pick_encoder()
        if not encoder:
            say("[red]✗ 无可用 AV1 编码器（av1_qsv / libsvtav1 / "
                "libaom-av1 均不可用）。[/]")
            for task, rep in items:
                task.state = _ST_TRANSCODE_FAIL
                rep.messages.append("无可用 AV1 编码器")
            return

        cw = max(1, int(getattr(vm, "concat_workers", 1) or 1))
        tw = max(1, int(getattr(vm, "transcode_workers", 1) or 1))
        say(f"  编码器: [cyan]{self.encoder_display()}[/]，"
            f"质量 {vm.quality}，关键帧 {vm.keyframe_interval}s，"
            f"音频 {vm.audio_codec} {vm.audio_profile} {vm.audio_bitrate}，"
            f"并发: 合并 [cyan]{cw}[/] / 转码 [cyan]{tw}[/]")

        enc_futs: dict = {}

        def _mark(task: _GroupTask, rep: MergeReport, state: str,
                 msg: str = "") -> None:
            task.state = state
            if msg:
                rep.messages.append(msg)

        try:
            with ThreadPoolExecutor(
                max_workers=cw, thread_name_prefix="vmerge-cat",
                initializer=_POOL_INIT,
            ) as cat_pool:
                concat_futs: dict = {}
                for task, rep in items:
                    if cancelled is not None and cancelled.is_set():
                        _mark(task, rep, _ST_CANCELLED, "已停止，未处理")
                        continue
                    if task.mixed:
                        continue      # 混编组没有复制流这一步, 下面直接进转码池
                    if on_start:
                        on_start(task, "concat")
                    concat_futs[cat_pool.submit(
                        self._concat_task, task, vm, say)] = (task, rep)

                with ThreadPoolExecutor(
                    max_workers=tw, thread_name_prefix="vmerge-enc",
                    initializer=_POOL_INIT,
                ) as enc_pool:
                    # 混编组直接交给转码池 (内部一次 ffmpeg 完成统一规格+拼接
                    # +AV1 编码), 编码并发仍受 transcode_workers 约束。
                    for task, rep in items:
                        if not task.mixed or task.state != _ST_PENDING:
                            continue
                        if cancelled is not None and cancelled.is_set():
                            _mark(task, rep, _ST_CANCELLED, "已停止，未处理")
                            continue
                        if on_start:
                            on_start(task, "transcode")
                        enc_futs[enc_pool.submit(
                            self._transcode_task, task, encoder,
                            vm, say)] = (task, rep)

                    for fut in as_completed(concat_futs):
                        task, rep = concat_futs[fut]
                        try:
                            ok = bool(fut.result())
                        except Exception as exc:      # 防御: 任务内部已捕获
                            ok = False
                            say(f"[red]✗ [{task.out_path.name}] "
                                f"合并异常: {exc}[/]")
                        if (not ok and getattr(vm, "mixed_codec_merge", True)
                                and not (cancelled is not None
                                         and cancelled.is_set())):
                            # 复制流拼接失败的另一种成因: 编码相同但参数/时基/
                            # SAR 不一致 (含容差内的 1918x1080 vs 1920x1080)。
                            # 同样退回"统一规格重编码", 不再直接放弃该组。
                            say(f"[yellow]↩ [{task.out_path.name}] 复制流拼接失败 "
                                f"→ 退回统一规格重编码[/]")
                            task.mixed = True
                            if on_start:
                                on_start(task, "transcode")
                            enc_futs[enc_pool.submit(
                                self._transcode_task, task, encoder,
                                vm, say)] = (task, rep)
                            continue
                        if ok and not (cancelled is not None
                                       and cancelled.is_set()):
                            if on_start:
                                on_start(task, "transcode")
                            enc_futs[enc_pool.submit(
                                self._transcode_task, task, encoder,
                                vm, say)] = (task, rep)
                        elif task.state == _ST_CONCAT_OK:
                            # concat 成功但因取消未提交转码
                            _mark(task, rep, _ST_CANCELLED, "已停止，未转码")

                    for fut in as_completed(enc_futs):
                        try:
                            fut.result()
                        except Exception as exc:
                            t, _ = enc_futs[fut]
                            say(f"[red]✗ [{t.out_path.name}] "
                                f"转码异常: {exc}[/]")
        finally:
            for task, _ in items:
                shutil.rmtree(task.tmpdir, ignore_errors=True)

    def _concat_task(self, task: _GroupTask, vm, say) -> bool:
        """阶段1: 复制流合并 (任务线程内执行)。成功返回 True。"""
        g = task.group
        try:
            # concat demuxer 需要正斜杠路径；' 需转义
            entries = []
            for v in g.videos:
                p = str(v.resolve()).replace("\\", "/").replace("'", "'\\''")
                entries.append(f"file '{p}'")
            task.list_file.write_text("\n".join(entries) + "\n",
                                      encoding="utf-8")
            say(f"  [cyan][{task.out_path.name}][/] 复制流合并 "
                f"({g.width}x{g.height}, {len(g.videos)} 个视频)…")

            cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
            if vm.concat_genpts:
                cmd += ["-fflags", "+genpts"]
            cmd += ["-f", "concat", "-safe", "0", "-i", str(task.list_file),
                    "-c", "copy", str(task.merged_tmp)]
            ok, err = self._run(cmd, vm.timeout)
            if not ok:
                say(f"[red]✗ [{task.out_path.name}] 复制流合并失败: "
                    f"{err[:200]}[/]")
                say("[dim]    提示: 组内编码/参数不一致时无法直接拼接, "
                    "将退回统一规格重编码。[/]")
                task.state = _ST_CONCAT_FAIL
                task.report.messages.append(
                    f"{task.out_path.name} 复制流合并失败")
                return False
            task.state = _ST_CONCAT_OK
            return True
        except Exception as exc:
            task.state = _ST_CONCAT_FAIL
            say(f"[red]✗ [{task.out_path.name}] 合并异常: "
                f"{type(exc).__name__}: {exc}[/]")
            return False

    def _transcode_task(self, task: _GroupTask, encoder: str, vm,
                        say) -> bool:
        """阶段2: AV1 转码 + 时长校验/修复 (任务线程内执行)。

        混编组 (task.mixed) 走"统一规格拼接 + 编码"命令 —— 同样只有一次编码,
        因此与普通组共用同一套收尾校验/修复逻辑。
        """
        out_path = task.out_path
        try:
            if task.mixed:
                say(f"  [cyan][{out_path.name}][/] 组内编码不一致 "
                    f"({self._task_codecs(task)}) → 逐源统一规格后拼接转 AV1 …")
            else:
                say(f"  [{out_path.name}] 转码 AV1 …")
            ok, err = self._run(self._encode_cmd(task, encoder, vm, out_path,
                                                 say), vm.timeout)
            if not ok:
                # 硬件编码失败 → 尝试 CPU 回退
                fallback = ("libaom-av1" if encoder == "libsvtav1"
                            else "libsvtav1")
                if fallback != encoder and self._has_encoder(fallback):
                    say(f"[yellow]⚠ [{out_path.name}] {encoder} 转码失败，"
                        f"回退 {fallback}…[/]")
                    encoder = fallback
                    ok, err = self._run(
                        self._encode_cmd(task, encoder, vm, out_path, say),
                        vm.timeout)
            if not ok:
                say(f"[red]✗ [{out_path.name}] 转码失败: {err[:200]}[/]")
                out_path.unlink(missing_ok=True)
                task.report.messages.append(f"{out_path.name} 转码失败")
                task.state = _ST_TRANSCODE_FAIL
                return False
            return self._verify_and_finish(task, encoder, vm, say)
        except Exception as exc:
            task.state = _ST_TRANSCODE_FAIL
            out_path.unlink(missing_ok=True)
            say(f"[red]✗ [{out_path.name}] 转码异常: "
                f"{type(exc).__name__}: {exc}[/]")
            return False

    def _encode_cmd(self, task: _GroupTask, encoder: str, vm, dst: Path,
                    say) -> List[str]:
        """按任务类型选编码命令: 普通组=复制流产物转码; 混编组=多输入统一规格。"""
        if task.mixed:
            return self._build_mixed_cmd(task, encoder, vm, dst, say)
        return self._build_transcode_cmd(task.merged_tmp, dst, encoder, vm,
                                        task.fps)

    @staticmethod
    def _task_codecs(task: _GroupTask) -> str:
        """组内视频编码名列表 (日志展示用)。"""
        names = sorted({task.probe_by_path[v].vcodec
                        for v in task.group.videos
                        if task.probe_by_path.get(v)
                        and task.probe_by_path[v].vcodec})
        return ", ".join(names) or "?"

    def _verify_and_finish(self, task: _GroupTask, encoder: str, vm,
                           say) -> bool:
        """输出后的公共收尾: 时长校验 → (必要时) 修复 → 成功日志与状态。"""
        g = task.group
        out_path = task.out_path
        report = task.report
        # --- 时长校验: 成片 vs 源视频时长之和 ---
        expected = g.duration_sum
        actual = (self.probe_duration(out_path)
                  if vm.duration_check else 0.0)
        if vm.duration_check and expected > 0 and actual > 0:
            report.duration_info.append((out_path, expected, actual))
            tol = max(vm.duration_tolerance_min_s,
                      expected * vm.duration_tolerance_pct / 100.0)
            if abs(actual - expected) > tol:
                say(f"[yellow]⚠ [{out_path.name}] 时长异常: 期望 "
                    f"{fmt_duration(expected)} / 实际 "
                    f"{fmt_duration(actual)}（偏差 "
                    f"{abs(actual - expected):.1f}s > 容差 "
                    f"{tol:.1f}s）[/]")
                repaired = False
                if vm.duration_repair:
                    say(f"  [{out_path.name}] 尝试 setpts 重排时间戳修复…")
                    repaired, actual = self._repair_timestamps(
                        task, encoder, vm, say, expected)
                if not repaired:
                    say(f"[red]✗ {out_path.name} 时长异常未修复 "
                        f"(期望 {fmt_duration(expected)} / 实际 "
                        f"{fmt_duration(actual)}) — 请检查该文件[/]")
                    report.duration_issues.append(
                        (out_path, expected, actual))
                    report.messages.append(
                        f"{out_path.name} 时长异常: "
                        f"{fmt_duration(actual)} / 期望 "
                        f"{fmt_duration(expected)}")
        elif vm.duration_check:
            say(f"  [{out_path.name}] 时长校验: {fmt_duration(actual)} "
                f"(期望 {fmt_duration(expected)})")

        size_mb = (out_path.stat().st_size / (1024 * 1024)
                   if out_path.exists() else 0)
        warn = (" ⚠时长异常"
                if any(o == out_path
                       for o, _, _ in report.duration_issues) else "")
        say(f"  [bold green]✓ 完成 → {out_path.name} "
            f"({g.width}x{g.height}, {size_mb:.1f} MB, "
            f"{encoder}{warn})[/]")
        _log.info(
            "[视频合并] 组 %dx%d 完成 → %s (%.1fMB, %s%s)",
            g.width, g.height, out_path.name, size_mb, encoder,
            ", 混编统一转码" if task.mixed else "",
        )
        task.state = _ST_DONE
        return True

    def finalize_report(self, report: MergeReport,
                        tasks: List[_GroupTask]) -> str:
        """按任务状态累计 merged/skipped, 生成目录摘要 (含富文本标记)。"""
        for t in tasks:
            if t.state == _ST_DONE:
                report.merged += 1
                report.outputs.append(t.out_path)
            elif t.state == _ST_PENDING:
                # 管线未覆盖到的任务 (理论不应发生), 按未处理计
                report.skipped += 1
                report.messages.append(f"{t.out_path.name} 未处理")
            elif t.state == _ST_CANCELLED:
                report.skipped += 1
            elif t.state == _ST_TRANSCODE_FAIL:
                report.skipped += 1
            elif t.state == _ST_CONCAT_FAIL:
                report.skipped += 1
        if report.merged:
            extra = (f"，其中 {report.mixed_groups} 组编码不一致已统一转码"
                     if report.mixed_groups else "")
            done = (f"，{report.already_merged} 个已合并产物未重复处理"
                    if report.already_merged else "")
            summary = (f"[bold green]✓ 目录合并完成: 成功 {report.merged} 组"
                       + (f"，跳过 {report.skipped} 组" if report.skipped
                          else "")
                       + extra + done
                       + f"，输出 {', '.join(p.name for p in report.outputs)}"
                         "[/]")
        else:
            done = (f"（{report.already_merged} 个已合并产物未重复处理）"
                    if report.already_merged else "")
            summary = (f"[yellow]目录合并未产生输出"
                       f"（跳过 {report.skipped} 组）{done}。[/]")
        plain = (summary.replace("[bold green]", "").replace("[/]", "")
                 .replace("[yellow]", ""))
        report.messages.append(plain)
        return summary

    # ------------------------------------------------------------------ #
    #  兼容入口: 单目录 (TUI 使用)
    # ------------------------------------------------------------------ #

    def merge_directory(
        self,
        directory: Path,
        log_cb: Optional[Callable[[str], None]] = None,
        exclude: Optional[set] = None,
    ) -> MergeReport:
        """对单个目录 规划→两级并发流水线→汇总。返回报告。"""
        def say(msg: str) -> None:
            if log_cb:
                try:
                    log_cb(msg)
                except Exception:
                    pass
            _log.info("[视频合并] %s", msg)

        report, tasks = self.plan_directory(directory, log_cb=log_cb,
                                            exclude=exclude)
        if tasks:
            self.run_pipeline([(report, tasks)], self.cfg.video_merge, say)
        summary = self.finalize_report(report, tasks)
        if report.groups:
            say("")
            say(summary)
        return report

    # ------------------------------------------------------------------ #
    #  转码命令 / 修复 / 工具
    # ------------------------------------------------------------------ #

    def _repair_timestamps(
        self,
        task: _GroupTask,
        encoder: str,
        vm,
        say: Callable[[str], None],
        expected: float,
    ) -> Tuple[bool, float]:
        """时长异常修复: 跳过中间产物, 直接从源重转码并重排视频时间戳
        (消除 PTS 空洞/冻结段)。

        - 普通组: 从 concat 列表 (task.list_file) 重转码 + setpts 重排;
        - 混编组: 重跑"统一规格拼接"命令 (其本身已含逐源 setpts 归一化)。

        修复成功且时长达标 → 替换 out_path, 返回 (True, 新时长);
        否则保留原输出, 返回 (False, 原时长)。
        """
        out_path = task.out_path
        repair_tmp = task.tmpdir / "repair.mp4"
        if task.mixed:
            cmd = self._build_mixed_cmd(task, encoder, vm, repair_tmp, say)
        else:
            cmd = [
                self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            ]
            if vm.concat_genpts:
                cmd += ["-fflags", "+genpts"]
            cmd += [
                "-f", "concat", "-safe", "0", "-i", str(task.list_file),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", self._setpts_filter(task.fps),
            ]
            cmd += self._encode_args(encoder, vm, task.fps)
            cmd += ["-movflags", "+faststart", str(repair_tmp)]
        ok, err = self._run(cmd, vm.timeout)
        if not ok:
            say(f"  [dim]修复转码失败: {err[:160]} — 保留原输出。[/]")
            return False, self.probe_duration(out_path)
        actual = self.probe_duration(repair_tmp)
        tol = max(vm.duration_tolerance_min_s,
                  expected * vm.duration_tolerance_pct / 100.0)
        if actual <= 0 or abs(actual - expected) > tol:
            say(f"  [dim]修复后时长仍异常 ({fmt_duration(actual)}), "
                f"保留原输出。[/]")
            return False, self.probe_duration(out_path)
        try:
            out_path.unlink(missing_ok=True)
            shutil.move(str(repair_tmp), str(out_path))
        except OSError as exc:
            say(f"  [dim]修复文件替换失败: {exc} — 保留原输出。[/]")
            return False, self.probe_duration(out_path)
        say(f"  [green]✓ 修复成功: {fmt_duration(actual)} "
            f"(期望 {fmt_duration(expected)})[/]")
        return True, actual

    @staticmethod
    def _setpts_filter(fps: float) -> str:
        """视频时间戳重排滤镜: 帧号重编 → 时长 = 帧数/fps。"""
        if fps > 0:
            return f"setpts=N/{fps:.5f}/TB"
        return "setpts=N/FRAME_RATE/TB"      # 帧率未知时用输入元数据

    def _encode_args(self, encoder: str, vm, fps: float) -> List[str]:
        """编码器/音频公共参数 (转码与修复共用)。"""
        quality_arg = _QUALITY_ARG.get(encoder, "-crf")
        args = [
            "-c:v", encoder,
            quality_arg, str(vm.quality),
        ]
        # 关键帧间隔（秒）→ 帧数
        if fps <= 0:
            fps = 30.0
        g = max(1, round(fps * max(1, vm.keyframe_interval)))
        args += ["-g", str(g), "-keyint_min", str(g)]

        # av1_qsv: 参考 ShanaEncoder QSVAV1 预设 —— veryfast
        # (MFX_TARGETUSAGE_BEST_SPEED) 压缩效率低, 同质量下文件更大、
        # 细节保留更多; 不启用 look_ahead (ICQ 下会进一步压小体积,
        # 导致"体积过小、质量损失过多")。旧参数: -preset 0 -look_ahead 1
        if encoder == "av1_qsv":
            args += ["-preset", vm.qsv_preset or "veryfast"]
            if getattr(vm, "qsv_profile", ""):
                args += ["-profile:v", vm.qsv_profile]

        # 音频: AAC LC 128k，声道保持原版
        args += [
            "-c:a", vm.audio_codec,
            "-profile:a", vm.audio_profile,
            "-b:a", vm.audio_bitrate,
        ]
        if not vm.keep_channels:
            args += ["-ac", "2"]
        if vm.remove_subtitles:
            args += ["-sn"]
        return args

    # ------------------------------------------------------------------ #
    #  混编组 (组内编码不一致 → 统一规格后拼接并一次编码)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _even_size(w: int, h: int) -> Tuple[int, int]:
        """yuv420p 要求宽高为偶数 (容差分组可能落在 1918 这类奇数上)。"""
        return max(2, w - (w % 2)), max(2, h - (h % 2))

    def _probe_audio(self, path: Path) -> _AudioInfo:
        """探测首个音频流 (有无 / 声道布局 / 采样率); 失败按"无音轨"处理。"""
        info = _AudioInfo()
        if not self.ffprobe:
            return info
        try:
            result = subprocess.run(
                [
                    self.ffprobe, "-v", "error",
                    "-select_streams", "a:0",
                    "-show_entries",
                    "stream=codec_name,channels,channel_layout,sample_rate",
                    "-of", "json",
                    str(path),
                ],
                capture_output=True, text=True, timeout=30,
                **HIDDEN_KW,
            )
            if result.returncode != 0:
                return info
            streams = (json.loads(result.stdout) or {}).get("streams") or []
            if not streams:
                return info
            s = streams[0]
            info.has_audio = True
            info.codec = str(s.get("codec_name") or "")
            info.channels = int(s.get("channels") or 0)
            info.layout = str(s.get("channel_layout") or "")
            info.sample_rate = int(s.get("sample_rate") or 0)
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError,
                ValueError):
            pass
        return info

    def _mixed_audio_plan(
        self,
        audio: List[_AudioInfo],
        probe_by_path: dict,
        paths: List[Path],
        vm,
    ) -> Tuple[bool, str, int, dict]:
        """混编组音轨对齐方案 → (是否带音轨, 声道布局, 采样率, {源下标: 静音秒数})。

        concat 滤镜要求各段音频规格一致: 统一按 aformat 归到同一布局/采样率;
        个别源没有音轨时补等长静音 (时长取自探测), 时长未知则整组放弃音轨
        (宁可不带音轨, 也不要静音长度不对导致音画不同步)。
        """
        with_audio = [i for i, a in enumerate(audio) if a.has_audio]
        if not with_audio:
            return False, "", 0, {}
        if not getattr(vm, "keep_channels", True):
            layout, sr = "stereo", 48000
        else:
            first = audio[with_audio[0]]
            layout = first.layout or ""
            if not layout:                     # 布局缺失 → 按声道数兜底
                layout = "stereo" if first.channels <= 2 else "5.1"
            sr = first.sample_rate or 48000
        silences: dict = {}
        for i, p in enumerate(paths):
            if audio[i].has_audio:
                continue
            pr = probe_by_path.get(p)
            dur = pr.duration if pr is not None else 0.0
            if dur <= 0:
                return False, "", 0, {}
            silences[i] = dur
        return True, layout, sr, silences

    def _build_mixed_cmd(self, task: _GroupTask, encoder: str, vm, dst: Path,
                         say: Callable[[str], None]) -> List[str]:
        """混编组命令: 一次 ffmpeg 完成 逐源统一规格 → 拼接 → AV1 编码。

        为什么不按字面"先把每条转成 AV1 中间片再 concat 复制流":
        那需要 N 次全质量编码 + 1 次拼接, 且 N 个并发编码会挤爆硬件编码器会话;
        本方案只解码+编码各一次 (画质与耗时都更优), 产物语义完全一致 ——
        concat 滤镜先解码各源, 统一到同一分辨率/像素格式/帧率/时基再拼成一条流。
        各段的 setpts=PTS-STARTPTS 是 concat 的必要条件 (每段必须从 0 起算),
        故不受 normalize_pts 开关影响。
        """
        g = task.group
        paths = list(g.videos)
        n = len(paths)
        w, h = self._even_size(g.width, g.height)
        fps = task.fps if task.fps > 0 else 30.0
        audio = [self._probe_audio(p) for p in paths]
        use_audio, layout, sr, silences = self._mixed_audio_plan(
            audio, task.probe_by_path, paths, vm)
        if silences:
            say(f"  [dim][{task.out_path.name}] {len(silences)} 条源无音轨, "
                f"按等长静音补齐[/]")
        elif not use_audio and any(a.has_audio for a in audio):
            say(f"  [yellow]⚠ [{task.out_path.name}] 无音轨源时长不可知, "
                f"本组不带音轨[/]")

        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        if getattr(vm, "concat_genpts", True):
            cmd += ["-fflags", "+genpts"]
        for p in paths:
            cmd += ["-i", str(p)]
        # 静音源接在视频输入之后 (下标 = n + k)
        silent_in: dict = {}
        for k, (i, dur) in enumerate(sorted(silences.items())):
            silent_in[i] = n + k
            cmd += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i",
                    f"anullsrc=channel_layout={layout}:sample_rate={sr}"]

        parts: List[str] = []
        for i in range(n):
            parts.append(
                f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                f"fps={fps:g},format=yuv420p,setpts=PTS-STARTPTS[v{i}]")
        parts.append("".join(f"[v{i}]" for i in range(n))
                     + f"concat=n={n}:v=1:a=0[vout]")
        if use_audio:
            for i in range(n):
                src = f"{silent_in.get(i, i)}:a"
                pr = task.probe_by_path.get(paths[i])
                vdur = pr.duration if pr is not None else 0.0
                chain = (f"[{src}]aformat=sample_fmts=fltp:sample_rates={sr}"
                         f":channel_layouts={layout}")
                if vdur > 0 and i not in silent_in:
                    # 音频长于视频 (源的时间戳空洞) 会把后续各段整体推后 ——
                    # 按视频流时长裁长补短, 保证拼接后音画不漂移。
                    chain += f",atrim=0:{vdur:.3f},apad,atrim=0:{vdur:.3f}"
                chain += f",asetpts=PTS-STARTPTS[a{i}]"
                parts.append(chain)
            parts.append("".join(f"[a{i}]" for i in range(n))
                         + f"concat=n={n}:v=0:a=1[aout]")
        cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]"]
        if use_audio:
            cmd += ["-map", "[aout]"]
        cmd += self._encode_args(encoder, vm, fps)
        cmd += ["-movflags", "+faststart", str(dst)]
        return cmd

    def _build_transcode_cmd(
        self, src: Path, dst: Path, encoder: str, vm, fps: float = 0.0,
    ) -> List[str]:
        """构造 AV1 转码命令（关键帧/音频/字幕按配置）。

        normalize_pts (默认开): -vf setpts=N/fps/TB 重排视频时间戳 —
        concat 产物/QSV 编码器对 PTS 空洞/非单调时间戳会放大为成片
        时长虚增+中段无法播放; 重排后成片时长 = 帧数/fps。
        """
        cmd = [
            self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
        ]
        if getattr(vm, "normalize_pts", True):
            cmd += ["-vf", self._setpts_filter(fps)]
        cmd += self._encode_args(encoder, vm, fps)
        cmd += ["-movflags", "+faststart", str(dst)]
        return cmd

    def _has_encoder(self, encoder: str) -> bool:
        if not self.ffmpeg:
            return False
        try:
            result = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=10,
                **HIDDEN_KW,
            )
            return any(
                parts[1] == encoder
                for line in result.stdout.splitlines()
                for parts in [line.split()] if len(parts) >= 2
            )
        except (subprocess.SubprocessError, OSError):
            return False

    @staticmethod
    def _run(cmd: List[str], timeout: int) -> Tuple[bool, str]:
        """执行 ffmpeg，返回 (成功, 错误信息)。"""
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                **HIDDEN_KW,
            )
            if result.returncode == 0:
                return True, ""
            return False, (result.stderr or result.stdout).strip()[-400:]
        except subprocess.TimeoutExpired:
            return False, f"超时（>{timeout}s）"
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"{type(exc).__name__}: {exc}"
