"""同目录视频合并 + AV1 转码 (按 v 触发)。

流程（每目录独立处理）:
  1. 扫描目录顶层视频文件（不递归、跳过 compressed/ 等子目录）。
  2. ffprobe 探测每个视频的分辨率 / 帧率 / 视频编码。
  3. 按分辨率分组（容差 tolerance_px: 1918x1080 ≈ 1920x1080）。
  4. 限制检查:
     - 组内视频数 > max_videos_per_group (10) → 该组跳过并提示;
     - 分辨率组数(目标文件数) > max_output_files (4) → 整个目录跳过并提示。
  5. 复制流合并: ffmpeg -f concat -safe 0 -c copy（组内编码不一致 → 提示跳过）。
  6. AV1 转码: 首选 av1_qsv (HW:QSV)，回退 libsvtav1 / libaom-av1。
     设置: 质量 quality(26)、关键帧 keyframe_interval(2s)、音频 AAC LC 128kbps
     声道同原版、无字幕、不改变分辨率。
  7. 输出 V1.mp4 / V2.mp4 ...（按分辨率从大到小编号）。

说明: 本功能针对真实视频文件, 无需考虑由多帧动图转成的视频(动画转换产物
在 compressed/ 子目录, 不在合并范围内)。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from winproc import HIDDEN_KW

from config import Config, load_config
from hwaccel import HardwareAccelerator
from logger import get_logger

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


@dataclass
class VideoProbe:
    """ffprobe 探测结果。"""
    path: Path
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    ok: bool = False
    error: str = ""


@dataclass
class MergeGroup:
    """同一分辨率的一组视频。"""
    width: int = 0
    height: int = 0
    videos: List[Path] = field(default_factory=list)


@dataclass
class MergeReport:
    """单个目录的合并结果。"""
    directory: Path
    total_videos: int = 0
    groups: int = 0
    merged: int = 0          # 成功生成的组数
    skipped: int = 0         # 跳过的组数
    outputs: List[Path] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)  # 提示/原因


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
        """ffprobe 探测单个视频: 分辨率 / 帧率 / 视频编码。"""
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
                    "stream=width,height,r_frame_rate,codec_name",
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
            probe.ok = probe.width > 0 and probe.height > 0
            if not probe.ok:
                probe.error = "未探测到视频流/分辨率"
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError, ValueError) as exc:
            probe.error = f"{type(exc).__name__}: {exc}"
        return probe

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
    def _scan_videos(directory: Path) -> List[Path]:
        """扫描目录顶层视频文件（不递归；跳过隐藏文件与 compressed/）。"""
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
    #  合并 + 转码
    # ------------------------------------------------------------------ #

    def merge_directory(
        self,
        directory: Path,
        log_cb: Optional[Callable[[str], None]] = None,
        exclude: Optional[set] = None,
    ) -> MergeReport:
        """对目录执行 扫描→分组→合并→转码。返回报告（含提示信息）。

        log_cb: 过程日志回调（主线程安全投递 UI）；同时写文件日志。
        exclude: 需要排除的视频路径集合（GUI 移除选中的文件不再参与合并）。
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
            return report

        vm = self.cfg.video_merge
        say(f"[cyan]扫描目录: {directory}[/]")

        videos = self._scan_videos(directory)
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
            return report
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
            return report

        # 分组
        groups = self._group_videos(probes)
        report.groups = len(groups)
        say(f"  按分辨率分组: [cyan]{len(groups)}[/] 组")
        for g in groups:
            say(f"    {g.width}x{g.height}: {len(g.videos)} 个")

        # 限制 1: 目标文件数（分辨率组数）上限
        if len(groups) > vm.max_output_files:
            msg = (f"[yellow]⚠ 分辨率组数 {len(groups)} 超过上限 "
                   f"{vm.max_output_files}（目标文件将超过 {vm.max_output_files} 个），"
                   f"本目录不合并。[/]")
            say(msg)
            report.messages.append(
                f"分组数 {len(groups)} > {vm.max_output_files}，不合并"
            )
            return report

        # 限制 2: 单组视频数上限
        for g in groups:
            if len(g.videos) > vm.max_videos_per_group:
                msg = (f"[yellow]⚠ 组 {g.width}x{g.height} 有 {len(g.videos)} 个视频，"
                       f"超过上限 {vm.max_videos_per_group}，跳过该组。[/]")
                say(msg)
                report.messages.append(
                    f"{g.width}x{g.height} 组 {len(g.videos)} > "
                    f"{vm.max_videos_per_group}，跳过"
                )
                g.videos = []  # 标记跳过

        # 编码器
        encoder = self.pick_encoder()
        if not encoder:
            say("[red]✗ 无可用 AV1 编码器（av1_qsv / libsvtav1 / libaom-av1 均不可用），跳过。[/]")
            report.messages.append("无可用 AV1 编码器")
            return report
        say(f"  编码器: [cyan]{self.encoder_display()}[/]，"
            f"质量 {vm.quality}，关键帧 {vm.keyframe_interval}s，"
            f"音频 {vm.audio_codec} {vm.audio_profile} {vm.audio_bitrate}")

        # 逐组处理
        out_index = 0
        for g in groups:
            if not g.videos:
                report.skipped += 1
                continue
            out_index += 1
            out_name = f"{vm.output_prefix}{out_index}.{vm.output_extension}"
            out_path = directory / out_name

            if out_path.exists() and not vm.overwrite_existing:
                say(f"[yellow]⚠ 目标文件已存在，跳过: {out_name}[/]")
                report.skipped += 1
                report.messages.append(f"{out_name} 已存在，跳过")
                continue

            ok = self._merge_and_transcode(
                g, out_path, encoder, vm, report, say,
            )
            if ok:
                report.merged += 1
                report.outputs.append(out_path)
            else:
                report.skipped += 1

        # 目录汇总（日志末尾提示）
        say("")
        if report.merged:
            say(f"[bold green]✓ 目录合并完成: 成功 {report.merged} 组"
                + (f"，跳过 {report.skipped} 组" if report.skipped else "")
                + f"，输出 {', '.join(p.name for p in report.outputs)}[/]")
        else:
            say(f"[yellow]目录合并未产生输出（跳过 {report.skipped} 组）。[/]")
        return report

    def _merge_and_transcode(
        self,
        group: MergeGroup,
        out_path: Path,
        encoder: str,
        vm,
        report: MergeReport,
        say: Callable[[str], None],
    ) -> bool:
        """复制流合并 + AV1 转码。成功返回 True。"""
        assert self.ffmpeg is not None
        tmpdir = Path(tempfile.mkdtemp(prefix="vmerge_"))
        merged_tmp = tmpdir / "merged.mp4"
        try:
            # --- 检查组内视频编码一致性（concat copy 需要一致）---
            vcodes: set[str] = set()
            for v in group.videos:
                p = self.probe(v)
                if p.ok and p.vcodec:
                    vcodes.add(p.vcodec)
            if len(vcodes) > 1:
                say(f"[yellow]⚠ 组 {group.width}x{group.height} 视频编码不一致 "
                    f"({', '.join(sorted(v for v in vcodes if v))})，跳过合并。[/]")
                report.messages.append(
                    f"{group.width}x{group.height} 编码不一致，跳过"
                )
                return False

            # --- 1) 复制流合并 ---
            list_file = tmpdir / "concat.txt"
            entries = []
            for v in group.videos:
                # concat demuxer 需要正斜杠路径；' 需转义
                p = str(v.resolve()).replace("\\", "/").replace("'", "'\\''")
                entries.append(f"file '{p}'")
            list_file.write_text("\n".join(entries) + "\n", encoding="utf-8")
            say(f"  [cyan]合并组 {group.width}x{group.height}[/] "
                f"({len(group.videos)} 个视频, 复制流)…")

            cmd = [
                self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(list_file),
                "-c", "copy", str(merged_tmp),
            ]
            ok, err = self._run(cmd, vm.timeout)
            if not ok:
                say(f"[red]✗ 复制流合并失败: {err[:200]}[/]")
                say("[dim]    提示: 组内编码/参数不一致时无法直接拼接，跳过该组。[/]")
                report.messages.append(
                    f"{group.width}x{group.height} 复制流合并失败"
                )
                return False
            say(f"  [green]✓[/] 复制流合并完成 → {merged_tmp.name}")

            # --- 2) AV1 转码 ---
            say("  转码 AV1 …")
            cmd = self._build_transcode_cmd(merged_tmp, out_path, encoder, vm)
            ok, err = self._run(cmd, vm.timeout)
            if not ok:
                # 硬件编码失败 → 尝试 CPU 回退
                fallback = "libaom-av1" if encoder == "libsvtav1" else "libsvtav1"
                if fallback and fallback != encoder and self._has_encoder(fallback):
                    say(f"[yellow]⚠ {encoder} 转码失败，回退 {fallback}…[/]")
                    cmd = self._build_transcode_cmd(merged_tmp, out_path, fallback, vm)
                    ok, err = self._run(cmd, vm.timeout)
                    if ok:
                        encoder = fallback
            if not ok:
                say(f"[red]✗ 转码失败: {err[:200]}[/]")
                out_path.unlink(missing_ok=True)
                report.messages.append(
                    f"{group.width}x{group.height} 转码失败"
                )
                return False

            size_mb = out_path.stat().st_size / (1024 * 1024) if out_path.exists() else 0
            say(f"  [bold green]✓ 完成 → {out_path.name} "
                f"({group.width}x{group.height}, {size_mb:.1f} MB, {encoder})[/]")
            _log.info(
                "[视频合并] 组 %dx%d 完成 → %s (%.1fMB, %s)",
                group.width, group.height, out_path.name, size_mb, encoder,
            )
            return True

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _build_transcode_cmd(
        self, src: Path, dst: Path, encoder: str, vm,
    ) -> List[str]:
        """构造 AV1 转码命令（关键帧/音频/字幕按配置）。"""
        quality_arg = _QUALITY_ARG.get(encoder, "-crf")
        cmd = [
            self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-c:v", encoder,
            quality_arg, str(vm.quality),
        ]
        # 关键帧间隔（秒）→ 帧数: 探测合并后文件帧率
        fps = self.probe(src).fps
        if fps <= 0:
            fps = 30.0
        g = max(1, round(fps * max(1, vm.keyframe_interval)))
        cmd += ["-g", str(g), "-keyint_min", str(g)]

        # av1_qsv: 参考 ShanaEncoder QSVAV1 预设 —— veryfast
        # (MFX_TARGETUSAGE_BEST_SPEED) 压缩效率低, 同质量下文件更大、
        # 细节保留更多; 不启用 look_ahead (ICQ 下会进一步压小体积,
        # 导致"体积过小、质量损失过多")。旧参数: -preset 0 -look_ahead 1
        if encoder == "av1_qsv":
            cmd += ["-preset", vm.qsv_preset or "veryfast"]
            if getattr(vm, "qsv_profile", ""):
                cmd += ["-profile:v", vm.qsv_profile]

        # 音频: AAC LC 128k，声道保持原版
        cmd += [
            "-c:a", vm.audio_codec,
            "-profile:a", vm.audio_profile,
            "-b:a", vm.audio_bitrate,
        ]
        if not vm.keep_channels:
            cmd += ["-ac", "2"]
        if vm.remove_subtitles:
            cmd += ["-sn"]

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
