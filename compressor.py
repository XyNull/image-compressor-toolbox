"""Image compression engine.

Pipeline per image:
  1. Detect format via magic bytes (detector.py)
  2. Decode to PIL Image (pixel array)
  3. Convert to RGB (JPEG has no alpha channel)
  4. Encode as JPEG quality=90
  5. If output > 5 MB → scale down 10 % → re-encode → goto 4
  6. If compressed size ≥ original size → skip (do not save)
  7. Write to ``compressed/`` subdirectory (original untouched)

Output path strategy:
  - Single file dropped:  parent/compressed/stem.jpg
  - Folder dropped:       root/compressed/<relative-path>/stem.jpg
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional, Union

from PIL import Image

# Register HEIC / AVIF support with Pillow (optional dependency)
try:
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except ImportError:
    HEIF_AVAILABLE = False

from detector import detect_format, detect_format_from_bytes, SUPPORTED_FORMATS
from hwaccel import HardwareAccelerator
from config import Config, load_config


# --- Result dataclass ------------------------------------------------------ #

@dataclass
class CompressionResult:
    """Outcome of compressing a single image."""
    input_path: Path
    output_path: Optional[Path]
    success: bool
    original_size: int = 0
    output_size: int = 0
    scale_count: int = 0
    error: Optional[str] = None
    fmt: Optional[str] = None          # 原始格式
    skipped: bool = False              # 已跳过不保存
    skip_reason: Optional[str] = None  # "animated" | "larger"
    is_video: bool = False             # 是否为视频转换结果
    encoder: Optional[str] = None      # FFmpeg 编码器 (hevc_nvenc / libx265 等)


# --- Compressor ------------------------------------------------------------ #

class ImageCompressor:
    """Compresses images to JPEG ≤ 5 MB with automatic downscaling."""

    def __init__(
        self,
        hw_accel: Optional[HardwareAccelerator] = None,
        cfg: Optional[Config] = None,
    ) -> None:
        self.hw = hw_accel or HardwareAccelerator()
        self.cfg = cfg or load_config()
        # 延迟导入避免循环依赖
        from video_converter import VideoConverter
        self.video_converter = VideoConverter(self.hw, self.cfg)

    def compress(
        self,
        input_path: Union[str, Path],
        root_dir: Optional[Union[str, Path]] = None,
        known_fmt: Optional[str] = None,
        known_size: int = 0,
    ) -> CompressionResult:
        """Compress a single image file.

        Args:
            input_path: Path to the source image.
            root_dir:   If the image was found by traversing a dropped
                        folder, pass that folder here so the output
                        preserves the subdirectory structure.
            known_fmt:  Pre-detected format (from directory scanning).
                        If provided, skips redundant detect_format() call.
            known_size: Pre-known file size in bytes (from DirEntry.stat).
                        If non-zero, skips redundant stat() call.

        Returns:
            CompressionResult with details of the operation.
        """
        input_path = Path(input_path)

        # 1. Detect format — use cached result if available
        if known_fmt:
            fmt = known_fmt
        else:
            fmt = detect_format(input_path)
        if fmt is None or fmt not in SUPPORTED_FORMATS:
            return CompressionResult(
                input_path=input_path,
                output_path=None,
                success=False,
                error="Unsupported format or not an image",
            )

        # Check HEIC/AVIF decoder availability
        if fmt in ("heic", "avif") and not HEIF_AVAILABLE:
            return CompressionResult(
                input_path=input_path,
                output_path=None,
                success=False,
                fmt=fmt,
                error=f"{fmt.upper()} requires 'pillow-heif' package",
            )

        # Compute output path
        output_path = self._output_path(input_path, root_dir)

        # Use cached size if available (avoids extra stat on SMB/network)
        if known_size:
            original_size = known_size
        else:
            original_size = input_path.stat().st_size

        # For small files, read the entire file into memory once.
        # This avoids multiple SMB round-trips (detect + Pillow open + load)
        # by doing a single read and using BytesIO for both detection and
        # Pillow decoding.
        threshold = int(self.cfg.io.small_file_threshold_mb * 1024 * 1024)
        file_data: Optional[bytes] = None
        if original_size > 0 and original_size <= threshold:
            try:
                with open(input_path, "rb") as f:
                    file_data = f.read()
            except (OSError, IOError):
                file_data = None

        try:
            # 2. Decode (original file is read-only — never modified)
            if file_data is not None:
                # Decode from in-memory buffer (single SMB read)
                if not known_fmt:
                    fmt = detect_format_from_bytes(file_data[:32]) or fmt
                img = Image.open(BytesIO(file_data))
            else:
                img = Image.open(input_path)
            img.load()  # force decode to surface errors early

            # 动图: 转为 MP4 H.265 (FFmpeg 不可用时回退到跳过)
            if getattr(img, "n_frames", 1) > 1:
                img.close()
                return self.video_converter.convert(
                    input_path, root_dir, original_fmt=fmt,
                    original_size=original_size,
                )

            # 3. Convert to RGB (JPEG has no alpha)
            rgb_img = self._to_rgb(
                img, tuple(self.cfg.compression.background_color)
            )

            # 释放原图文件句柄（仅关 fp，不调 img.close() 否则 save() 会报错）
            # Windows 上不释放会导致覆盖模式时 os.replace() 报 PermissionError
            if hasattr(img, "fp") and img.fp:
                try:
                    img.fp.close()
                except Exception:
                    pass
                img.fp = None

            img = rgb_img

            # 4-5. 编码循环: 按配置的格式/质量编码，超目标体积则循环缩小
            c = self.cfg.compression
            pil_format = c.output_format.upper()  # 如 "JPEG"
            target_bytes = int(c.target_size_mb * 1024 * 1024)
            scale_count = 0
            final_size = 0
            buf = BytesIO()

            while True:
                buf.seek(0)
                buf.truncate()
                img.save(
                    buf,
                    format=pil_format,
                    quality=c.quality,
                    optimize=c.optimize,
                    progressive=c.progressive,
                )
                final_size = buf.tell()

                if final_size <= target_bytes:
                    break

                scale_count += 1
                if scale_count > c.max_scale_iterations:
                    break  # safety: 达到最大缩小轮数

                # 按超限比例直接预算目标尺寸 (替代固定 0.9 步长)。
                # JPEG 体积近似与像素数线性: 超限 ratio 倍 → 尺寸缩到
                # 1/sqrt(ratio), 一次到位; 至少缩 5% 防止极小超限不收敛。
                ratio = final_size / target_bytes
                scale = max(1.05, (ratio ** 0.5) * 0.97)
                new_w = max(1, int(img.width / scale))
                new_h = max(1, int(img.height / scale))
                img = self.hw.resize(img, new_w, new_h)

            # 6. Check: if compressed is not smaller than original, skip
            if final_size >= original_size:
                return CompressionResult(
                    input_path=input_path,
                    output_path=None,
                    success=True,
                    original_size=original_size,
                    output_size=final_size,
                    scale_count=scale_count,
                    fmt=fmt,
                    skipped=True,
                    skip_reason="larger",
                )

            # 7. Write output
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "wb") as f:
                f.write(buf.getvalue())

            return CompressionResult(
                input_path=input_path,
                output_path=output_path,
                success=True,
                original_size=original_size,
                output_size=final_size,
                scale_count=scale_count,
                fmt=fmt,
            )

        except Exception as exc:
            return CompressionResult(
                input_path=input_path,
                output_path=output_path,
                success=False,
                original_size=original_size,
                fmt=fmt,
                error=str(exc),
            )

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_rgb(
        img: Image.Image,
        bg_color: tuple = (255, 255, 255),
    ) -> Image.Image:
        """Convert to RGB, compositing transparency over bg_color.

        bg_color 默认白色，可由配置 compression.background_color 指定。
        """
        if img.mode == "RGB":
            return img

        if img.mode in ("RGBA", "LA", "PA"):
            bg = Image.new("RGB", img.size, bg_color)
            if img.mode == "PA":
                img = img.convert("RGBA")
            elif img.mode == "LA":
                img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[-1])  # alpha channel as mask
            return bg

        if img.mode == "P":
            if "transparency" in img.info:
                img = img.convert("RGBA")
                return ImageCompressor._to_rgb(img)
            return img.convert("RGB")

        # L, CMYK, YCbCr, etc.
        return img.convert("RGB")

    def _out_ext(self) -> str:
        """输出文件扩展名，由 compression.output_format 派生。"""
        of = self.cfg.compression.output_format.lower()
        return "jpg" if of == "jpeg" else of

    def _output_path(
        self,
        input_path: Path,
        root_dir: Optional[Union[str, Path]],
    ) -> Path:
        """Compute the output path under the configured subdir.

        Single file:   parent/<subdir>/stem.<ext>
        Folder drop:   root/<subdir>/<rel-dir>/stem.<ext>
        """
        subdir = self.cfg.output.subdir_name
        ext = self._out_ext()
        if root_dir is not None:
            root = Path(root_dir)
            try:
                rel = input_path.relative_to(root)
            except ValueError:
                # Fallback: treat as single file
                return input_path.parent / subdir / (input_path.stem + "." + ext)
            return root / subdir / rel.parent / (rel.stem + "." + ext)

        return input_path.parent / subdir / (input_path.stem + "." + ext)
