"""Animated image → MP4 (H.265/HEVC) converter via FFmpeg subprocess.

FFmpeg reads GIF / APNG animated images directly and encodes them
to MP4 with H.265.  Encoder selection follows the hardware acceleration
priority: NVIDIA NVENC → Intel QSV → CPU libx265.

**Important — animated WebP caveat:**
FFmpeg's built-in ``webp`` decoder only handles *static* WebP (VP8/VP8L
bitstream).  When fed an *animated* WebP (containing ``ANIM``/``ANMF``
RIFF chunks) it fails with ``image data not found``.  The ``--enable-libwebp``
build flag only enables the *encoder*, not an animated decoder.

To work around this, animated WebP files are decoded frame-by-frame with
Pillow (which wraps libwebp and fully supports the animation format) and
piped to FFmpeg as raw RGB video on stdin.

If FFmpeg is not available, ``is_available()`` returns False and the caller
should fall back to skipping animated images.
"""
from __future__ import annotations

import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Union

from compressor import CompressionResult
from hwaccel import HardwareAccelerator
from winproc import HIDDEN_KW
from config import Config, load_config
from logger import get_logger

# 日志 (与 main.py 同一日志文件)
_log = get_logger()

# hw_type → FFmpeg H.265 编码器的固定映射
_HW_TO_ENCODER: dict[str, str] = {
    "nvidia": "hevc_nvenc",
    "intel_qsv": "hevc_qsv",
    "cpu": "libx265",
}


class VideoConverter:
    """Convert animated images to MP4 H.265 using FFmpeg."""

    # Maximum concurrent FFmpeg video conversions.
    # Consumer NVIDIA GPUs limit simultaneous NVENC sessions; without
    # throttling, a 20-thread pool will exhaust them and FFmpeg reports
    # "No capable devices found" / "OpenEncodeSessionEx failed".
    MAX_CONCURRENT_VIDEO = 2

    def __init__(
        self,
        hw_accel: HardwareAccelerator,
        cfg: Optional[Config] = None,
    ) -> None:
        self.hw = hw_accel
        self.cfg = cfg or load_config()
        self.ffmpeg: Optional[str] = shutil.which("ffmpeg")
        # 按配置的 crf 动态生成各编码器的质量参数
        crf = self.cfg.video.crf
        self._encoder_quality_args: dict[str, list[str]] = {
            "libx265":    ["-crf", str(crf)],
            "hevc_nvenc": ["-cq", str(crf)],
            "hevc_qsv":   ["-global_quality", str(crf)],
        }
        self._encoder: Optional[str] = self._detect_encoder()
        # 限制同时运行的 FFmpeg 视频转换进程数
        self._video_sem = threading.Semaphore(self.MAX_CONCURRENT_VIDEO)

    # ------------------------------------------------------------------ #
    #  Availability & encoder detection
    # ------------------------------------------------------------------ #

    def is_available(self) -> bool:
        """True if FFmpeg and a usable H.265 encoder are both available."""
        return self.ffmpeg is not None and self._encoder is not None

    def _detect_encoder(self) -> Optional[str]:
        """按配置中的编码器优先级选取可用的最佳 H.265 编码器。"""
        if not self.ffmpeg:
            return None

        # 依据 config.video.encoder_priority 顺序尝试（未知 hw_type 跳过）
        for hw_type in self.cfg.video.encoder_priority:
            encoder = _HW_TO_ENCODER.get(hw_type)
            if encoder is None:
                continue
            if hw_type == "cpu" or self.hw.type == hw_type:
                if self._has_encoder(encoder):
                    return encoder
        return None

    def _has_encoder(self, encoder: str) -> bool:
        """Check if FFmpeg supports a specific encoder."""
        try:
            result = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-encoders"],
                capture_output=True,
                text=True,
                timeout=5,
                **HIDDEN_KW,
            )
            return encoder in result.stdout
        except (subprocess.SubprocessError, OSError):
            return False

    def _encoder_fallback_list(self) -> list[str]:
        """Return encoders to try, in priority order.

        The primary encoder (selected by config + hardware) comes first,
        followed by any other available H.265 encoders as fallbacks.
        libx265 (CPU) is always included last as the ultimate fallback.
        """
        tried: set[str] = set()
        ordered: list[str] = []

        # Primary encoder first
        if self._encoder and self._encoder not in tried:
            ordered.append(self._encoder)
            tried.add(self._encoder)

        # All encoders in config priority order that are available
        for hw_type in self.cfg.video.encoder_priority:
            enc = _HW_TO_ENCODER.get(hw_type)
            if enc and enc not in tried and self._has_encoder(enc):
                ordered.append(enc)
                tried.add(enc)

        # Guarantee libx265 is in the list as a last-resort fallback
        if "libx265" not in tried and self._has_encoder("libx265"):
            ordered.append("libx265")

        return ordered

    @staticmethod
    def _is_encoder_session_error(error_text: str) -> bool:
        """True if the FFmpeg error indicates an encoder/device failure
        that might succeed with a different encoder (e.g. NVENC sessions
        exhausted)."""
        markers = (
            "No capable devices found",
            "OpenEncodeSessionEx failed",
            "incompatible client key",
            "Failed to create Direct3D",
            "D3D11VA",
            "not available",
            "device failed",
        )
        low = error_text.lower()
        return any(m.lower() in low for m in markers)

    # ------------------------------------------------------------------ #
    #  Conversion
    # ------------------------------------------------------------------ #

    def convert(
        self,
        input_path: Union[str, Path],
        root_dir: Optional[Union[str, Path]] = None,
        original_fmt: Optional[str] = None,
        original_size: int = 0,
    ) -> CompressionResult:
        """Convert an animated image to MP4 H.265.

        Args:
            input_path:   Path to the animated image (GIF / WebP / etc).
            root_dir:     Dropped folder root (for preserving subdir structure).
            original_fmt: Format string from the detector (e.g. "gif").
            original_size: Pre-known file size in bytes (from scanning).
                          If non-zero, skips redundant stat() call.
        """
        input_path = Path(input_path)

        # Use cached size if available (avoids extra stat on SMB/network)
        if not original_size:
            original_size = input_path.stat().st_size

        _log.info(
            "[FFmpeg] 开始转换动图 %s (fmt=%s, %d bytes)",
            input_path, original_fmt, original_size,
        )

        # FFmpeg not available → skip as animated
        if not self.is_available():
            _log.warning("[FFmpeg] 不可用, 跳过动图 %s", input_path.name)
            return CompressionResult(
                input_path=input_path,
                output_path=None,
                success=True,
                original_size=original_size,
                fmt=original_fmt,
                skipped=True,
                skip_reason="animated",
            )

        output_path = self._output_path(input_path, root_dir)

        # Animated WebP cannot be decoded by FFmpeg's built-in webp decoder
        # (it reports "image data not found" on ANIM/ANMF chunks).
        # Decode frames with Pillow and pipe them as raw RGB to FFmpeg.
        if original_fmt == "webp":
            result = self._convert_animated_webp(
                input_path, output_path, original_size, original_fmt
            )
            if result is not None:
                return result
            # result is None → file turned out to be single-frame WebP;
            # fall through to the normal FFmpeg file-input path below.

        encoder = self._encoder  # type: ignore[assignment]

        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Try each available encoder in priority order.
        # If NVENC sessions are exhausted (No capable devices found),
        # fall back to the next encoder (hevc_qsv → libx265).
        encoders_to_try = self._encoder_fallback_list()
        if encoder and encoder not in encoders_to_try:
            encoders_to_try.insert(0, encoder)

        last_error = ""
        for enc in encoders_to_try:
            cmd: list[str] = [
                self.ffmpeg,  # type: ignore[list-item]
                "-y",
                "-i", str(input_path),
                "-an",
                "-c:v", enc,
            ]
            cmd += self._encoder_quality_args.get(enc, [])
            cmd += [
                "-pix_fmt", "yuv420p",
                "-tag:v", "hvc1",
                "-movflags", "+faststart",
                "-loglevel", "error",
                str(output_path),
            ]

            try:
                # Limit concurrent FFmpeg processes to avoid exhausting
                # hardware encoder sessions (e.g. NVENC on consumer GPUs).
                _log.info(
                    "[FFmpeg] 执行转换: %s → %s (编码器=%s)",
                    input_path.name, output_path.name, enc,
                )
                t0 = time.time()
                with self._video_sem:
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=self.cfg.video.timeout,
                        **HIDDEN_KW,
                    )
                _log.info(
                    "[FFmpeg] 转换返回: %s returncode=%d 耗时%.1fs stderr前120=%s",
                    input_path.name, result.returncode,
                    time.time() - t0,
                    (result.stderr.strip()[:120].replace("\n", " ") if result.stderr else ""),
                )

                if result.returncode != 0:
                    output_path.unlink(missing_ok=True)
                    err = result.stderr.strip()
                    last_error = err[:200]
                    # Retry with next encoder on device/session failures
                    if self._is_encoder_session_error(err) and enc != encoders_to_try[-1]:
                        continue
                    return CompressionResult(
                        input_path=input_path,
                        output_path=None,
                        success=False,
                        original_size=original_size,
                        fmt=original_fmt,
                        error=f"FFmpeg: {err[:200]}",
                    )

                output_size = output_path.stat().st_size

                if output_size >= original_size:
                    output_path.unlink(missing_ok=True)
                    return CompressionResult(
                        input_path=input_path,
                        output_path=None,
                        success=True,
                        original_size=original_size,
                        output_size=output_size,
                        fmt=original_fmt,
                        skipped=True,
                        skip_reason="larger",
                    )

                return CompressionResult(
                    input_path=input_path,
                    output_path=output_path,
                    success=True,
                    original_size=original_size,
                    output_size=output_size,
                    fmt=original_fmt,
                    is_video=True,
                    encoder=enc,
                )

            except subprocess.TimeoutExpired:
                output_path.unlink(missing_ok=True)
                return CompressionResult(
                    input_path=input_path,
                    output_path=None,
                    success=False,
                    original_size=original_size,
                    fmt=original_fmt,
                    error="FFmpeg 转换超时（>5分钟）",
                )
            except Exception as exc:
                output_path.unlink(missing_ok=True)
                return CompressionResult(
                    input_path=input_path,
                    output_path=None,
                    success=False,
                    original_size=original_size,
                    fmt=original_fmt,
                    error=str(exc),
                )

        # All encoders failed
        return CompressionResult(
            input_path=input_path,
            output_path=None,
            success=False,
            original_size=original_size,
            fmt=original_fmt,
            error=f"FFmpeg: 所有编码器均失败，最后错误: {last_error}",
        )

    # ------------------------------------------------------------------ #
    #  Animated WebP — Pillow frame piping
    # ------------------------------------------------------------------ #

    def _convert_animated_webp(
        self,
        input_path: Path,
        output_path: Path,
        original_size: int,
        original_fmt: Optional[str],
    ) -> Optional[CompressionResult]:
        """Convert an animated WebP by piping Pillow-decoded frames to FFmpeg.

        FFmpeg's webp decoder cannot read animated WebP files, so we decode
        each frame with Pillow (libwebp) and feed them to FFmpeg as raw RGB24
        video on stdin.

        Returns a ``CompressionResult`` on success/failure, or ``None`` if the
        file turned out to be a single-frame WebP (caller should fall back to
        the normal FFmpeg file-input path).
        """
        from PIL import Image  # local import avoids cost when unused

        _log.info("[FFmpeg] WebP 动图管道路径: %s", input_path.name)

        try:
            img = Image.open(input_path)
        except Exception as exc:
            return CompressionResult(
                input_path=input_path,
                output_path=None,
                success=False,
                original_size=original_size,
                fmt=original_fmt,
                error=f"Pillow 打开 WebP 失败: {exc}",
            )

        n_frames = getattr(img, "n_frames", 1)
        if n_frames <= 1:
            # Not actually animated — let the caller use the normal path
            img.close()
            return None

        width, height = img.size
        bg_color = tuple(self.cfg.compression.background_color)
        frame_rate = self._parse_webp_frame_rate(input_path, n_frames)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        encoders_to_try = self._encoder_fallback_list()
        last_error = ""

        try:
            for encoder in encoders_to_try:
                cmd: list[str] = [
                    self.ffmpeg,  # type: ignore[list-item]
                    "-y",
                    "-f", "rawvideo",
                    "-pixel_format", "rgb24",
                    "-video_size", f"{width}x{height}",
                    "-framerate", str(frame_rate),
                    "-i", "pipe:0",
                    "-an",
                    "-c:v", encoder,
                ]
                cmd += self._encoder_quality_args.get(encoder, [])
                cmd += [
                    "-pix_fmt", "yuv420p",
                    "-tag:v", "hvc1",
                    "-movflags", "+faststart",
                    "-loglevel", "error",
                    str(output_path),
                ]

                proc: Optional[subprocess.Popen] = None
                pipe_broken = False
                try:
                    # Limit concurrent FFmpeg processes to avoid exhausting
                    # hardware encoder sessions (e.g. NVENC on consumer GPUs).
                    with self._video_sem:
                        proc = subprocess.Popen(
                            cmd,
                            stdin=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            **HIDDEN_KW,
                        )

                        # Re-seek to first frame for each encoder attempt
                        img.seek(0)
                        assert proc.stdin is not None
                        for i in range(n_frames):
                            img.seek(i)
                            frame = img
                            # Composite transparency over bg color
                            if frame.mode in ("RGBA", "LA", "PA"):
                                bg = Image.new(
                                    "RGB", frame.size, bg_color
                                )
                                if frame.mode != "RGBA":
                                    frame = frame.convert("RGBA")
                                bg.paste(frame, mask=frame.split()[-1])
                                frame = bg
                            elif frame.mode != "RGB":
                                frame = frame.convert("RGB")
                            try:
                                proc.stdin.write(frame.tobytes())
                            except (BrokenPipeError, OSError):
                                # FFmpeg exited early (e.g. encoder init
                                # failure). On Windows this is
                                # OSError(errno=22), not BrokenPipeError.
                                pipe_broken = True
                                break

                        try:
                            proc.stdin.close()
                        except (BrokenPipeError, OSError):
                            pass

                        try:
                            _, stderr_bytes = proc.communicate(
                                timeout=self.cfg.video.timeout
                            )
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.communicate()
                            output_path.unlink(missing_ok=True)
                            return CompressionResult(
                                input_path=input_path,
                                output_path=None,
                                success=False,
                                original_size=original_size,
                                fmt=original_fmt,
                                error="FFmpeg 转换超时",
                            )

                    err = stderr_bytes.decode(
                        "utf-8", errors="replace"
                    ).strip()

                    if proc.returncode != 0:
                        output_path.unlink(missing_ok=True)
                        last_error = err[:200]
                        # Retry with next encoder on device/session failures
                        if (
                            self._is_encoder_session_error(err)
                            and encoder != encoders_to_try[-1]
                        ):
                            continue
                        return CompressionResult(
                            input_path=input_path,
                            output_path=None,
                            success=False,
                            original_size=original_size,
                            fmt=original_fmt,
                            error=f"FFmpeg: {err[:200]}",
                        )

                    output_size = output_path.stat().st_size

                    if output_size >= original_size:
                        output_path.unlink(missing_ok=True)
                        return CompressionResult(
                            input_path=input_path,
                            output_path=None,
                            success=True,
                            original_size=original_size,
                            output_size=output_size,
                            fmt=original_fmt,
                            skipped=True,
                            skip_reason="larger",
                        )

                    return CompressionResult(
                        input_path=input_path,
                        output_path=output_path,
                        success=True,
                        original_size=original_size,
                        output_size=output_size,
                        fmt=original_fmt,
                        is_video=True,
                        encoder=encoder,
                    )

                except Exception as exc:
                    if proc is not None:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    output_path.unlink(missing_ok=True)
                    return CompressionResult(
                        input_path=input_path,
                        output_path=None,
                        success=False,
                        original_size=original_size,
                        fmt=original_fmt,
                        error=str(exc),
                    )

            # All encoders failed
            return CompressionResult(
                input_path=input_path,
                output_path=None,
                success=False,
                original_size=original_size,
                fmt=original_fmt,
                error=f"FFmpeg: 所有编码器均失败，最后错误: {last_error}",
            )

        finally:
            img.close()

    @staticmethod
    def _parse_webp_frame_rate(
        input_path: Path, n_frames: int
    ) -> float:
        """Parse ANMF chunk durations from a WebP to compute average FPS.

        Falls back to 24.0 fps if the file can't be parsed or durations
        sum to zero (defensive — shouldn't happen for a valid animated file).
        """
        try:
            with open(input_path, "rb") as f:
                # Skip 12-byte RIFF header (RIFF + size + WEBP)
                f.read(12)
                total_ms = 0
                count = 0
                while count < n_frames + 4:  # safety cap
                    hdr = f.read(8)
                    if len(hdr) < 8:
                        break
                    cid = hdr[:4]
                    csz = struct.unpack("<I", hdr[4:8])[0]
                    if cid == b"ANMF":
                        # ANMF header: x(3) y(3) w(3) h(3) duration(3) flags(1)
                        hdr_data = f.read(16)
                        if len(hdr_data) < 16:
                            break
                        dur = struct.unpack(
                            "<I", hdr_data[12:15] + b"\x00"
                        )[0]
                        total_ms += dur
                        count += 1
                        # Skip remaining chunk payload + padding
                        rest = csz - 16
                        f.read(rest + (csz % 2))
                    else:
                        f.read(csz + (csz % 2))
            if total_ms > 0 and count > 0:
                return round(count * 1000.0 / total_ms, 3)
        except (OSError, struct.error):
            pass
        return 24.0

    # ------------------------------------------------------------------ #
    #  Output path
    # ------------------------------------------------------------------ #

    def _output_path(
        self,
        input_path: Path,
        root_dir: Optional[Union[str, Path]],
    ) -> Path:
        """Compute output path with the configured video extension."""
        ext = self.cfg.video.extension
        subdir = "compressed"  # 视频与图片共用输出子目录
        if root_dir is not None:
            root = Path(root_dir)
            try:
                rel = input_path.relative_to(root)
            except ValueError:
                return input_path.parent / subdir / (input_path.stem + "." + ext)
            return root / subdir / rel.parent / (rel.stem + "." + ext)
        return input_path.parent / subdir / (input_path.stem + "." + ext)
