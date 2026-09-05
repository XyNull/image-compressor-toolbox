"""Hardware acceleration detection and GPU-accelerated image operations.

Detection priority:
  1. NVIDIA GPU (via nvidia-smi or PyTorch CUDA)
  2. Intel QuickSync (via FFmpeg -hwaccels)
  3. CPU software fallback

When an NVIDIA GPU *and* PyTorch are available, the resize step in the
compression loop is offloaded to the GPU using ``torch.nn.functional.
interpolate``.  JPEG decode/encode always uses Pillow (CPU) because NVENC
does not support MJPEG output.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional

from PIL import Image

from winproc import HIDDEN_KW


class HardwareAccelerator:
    """Detects available hardware accelerators and provides GPU resize."""

    def __init__(self) -> None:
        # 一次 nvidia-smi 同时完成 GPU 检测与名称查询 (原实现调用两次,
        # 且 PyTorch 检测在无 CUDA 环境下白加载 ~1s) — 启动速度优化
        self._gpu_name: Optional[str]
        is_nvidia: bool
        self._gpu_name, is_nvidia = self._query_nvidia()
        if is_nvidia:
            self.type = "nvidia"
        elif self._check_intel_qsv():
            self.type = "intel_qsv"
        else:
            self.type = "cpu"
        self.name: str = self._get_display_name()
        self.short_name: str = self._get_short_name()
        # PyTorch 惰性初始化: 仅在首次 GPU resize 时 import, 避免拖慢启动
        self._torch = None
        self._torch_tried = False

    # ------------------------------------------------------------------ #
    #  Detection
    # ------------------------------------------------------------------ #

    @staticmethod
    def _query_nvidia() -> tuple[Optional[str], bool]:
        """一次 nvidia-smi 调用: 返回 (GPU名称, 是否NVIDIA)。"""
        if not shutil.which("nvidia-smi"):
            return None, False
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
                **HIDDEN_KW,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().splitlines()[0], True
        except (subprocess.SubprocessError, OSError):
            pass
        return None, False

    def _check_nvidia(self) -> bool:
        """Check for an NVIDIA GPU."""
        # Method 1: nvidia-smi CLI
        if shutil.which("nvidia-smi"):
            try:
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return True
            except (subprocess.SubprocessError, OSError):
                pass

        # Method 2: PyTorch CUDA
        try:
            import torch  # type: ignore

            if torch.cuda.is_available():
                return True
        except ImportError:
            pass

        return False

    def _check_intel_qsv(self) -> bool:
        """Check for Intel QuickSync Video via FFmpeg."""
        if not shutil.which("ffmpeg"):
            return False
        try:
            result = subprocess.run(
                ["ffmpeg", "-hwaccels"],
                capture_output=True,
                text=True,
                timeout=5,
                **HIDDEN_KW,
            )
            return "qsv" in result.stdout.lower()
        except (subprocess.SubprocessError, OSError):
            return False

    def _get_display_name(self) -> str:
        """Human-readable accelerator name (full, for logs / startup panel)."""
        if self.type == "nvidia":
            if self._gpu_name:
                return f"NV {self._gpu_name}"
            return "NVIDIA GPU"

        if self.type == "intel_qsv":
            return "Intel QuickSync"

        return f"CPU ({self._cpu_name()})"

    def _get_short_name(self) -> str:
        """Compact accelerator name for the narrow status-bar cell.

        Strips redundant vendor/brand words so the model fits, e.g.
        ``NVIDIA GeForce RTX 3080 Ti`` → ``NV RTX 3080 Ti``.
        """
        if self.type == "nvidia":
            gpu = self._gpu_name or ""
            for token in ("NVIDIA ", "GeForce ", "Laptop ", "GPU "):
                gpu = gpu.replace(token, "")
            gpu = gpu.strip()
            if gpu.endswith(" GPU"):
                gpu = gpu[:-4].strip()
            short = f"NV {gpu}".strip() if gpu else "NVIDIA"
            return self._clip(short, 16)

        if self.type == "intel_qsv":
            return "Intel QSV"

        return "CPU"

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        """Truncate ``text`` to ``limit`` chars with an ellipsis if needed."""
        return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"

    @staticmethod
    def _query_nvidia_gpu_name() -> Optional[str]:
        if not shutil.which("nvidia-smi"):
            return None
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
                **HIDDEN_KW,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().splitlines()[0]
        except (subprocess.SubprocessError, OSError):
            pass
        return None

    @staticmethod
    def _cpu_name() -> str:
        try:
            if os.name == "nt":
                result = subprocess.run(
                    ["wmic", "cpu", "get", "name"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    **HIDDEN_KW,
                )
                lines = [
                    l.strip()
                    for l in result.stdout.splitlines()
                    if l.strip() and l.strip() != "Name"
                ]
                if lines:
                    return lines[0]
            else:
                with open("/proc/cpuinfo") as f:
                    for line in f:
                        if "model name" in line.lower():
                            return line.split(":", 1)[1].strip()
        except Exception:
            pass
        return "Software"

    def _get_torch(self) -> Optional[object]:
        """惰性初始化 PyTorch (首次 GPU resize 时), 结果缓存。

        启动阶段不再 import torch (节省 ~1s, 且无 CUDA 的 torch 白加载)。
        """
        if self._torch_tried:
            return self._torch
        self._torch_tried = True
        if self.type == "nvidia":
            try:
                import torch  # type: ignore

                if torch.cuda.is_available():
                    self._torch = torch
            except ImportError:
                pass
        return self._torch

    # ------------------------------------------------------------------ #
    #  GPU resize
    # ------------------------------------------------------------------ #

    def resize(self, img: Image.Image, new_w: int, new_h: int) -> Image.Image:
        """Resize an image, using GPU when available.

        Args:
            img: PIL Image (RGB mode).
            new_w: Target width in pixels.
            new_h: Target height in pixels.

        Returns:
            A new resized PIL Image.
        """
        torch = self._get_torch() if self.type == "nvidia" else None
        if torch is not None:
            try:
                return self._resize_gpu(img, new_w, new_h)
            except Exception:
                pass  # fall through to CPU on any GPU error

        # CPU fallback — Pillow LANCZOS
        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:
            resample = Image.LANCZOS  # type: ignore[attr-defined]
        return img.resize((new_w, new_h), resample)

    def _resize_gpu(self, img: Image.Image, new_w: int, new_h: int) -> Image.Image:
        """Resize on NVIDIA GPU via PyTorch ``interpolate`` (area mode)."""
        import numpy as np

        torch = self._torch
        assert torch is not None

        arr = np.asarray(img)  # (H, W, C) uint8

        tensor = torch.from_numpy(arr).float().to("cuda")
        # → (1, C, H, W) for F.interpolate
        if tensor.dim() == 3:
            tensor = tensor.permute(2, 0, 1).unsqueeze(0)
        elif tensor.dim() == 2:
            tensor = tensor.unsqueeze(0).unsqueeze(0)

        resized = torch.nn.functional.interpolate(
            tensor, size=(new_h, new_w), mode="area",
        )

        # Back to (H, W, C)
        if resized.dim() == 4:
            resized = resized.squeeze(0)
        if resized.shape[0] in (1, 3, 4):
            resized = resized.permute(1, 2, 0)

        result = resized.clamp(0, 255).byte().cpu().numpy()
        return Image.fromarray(result)
