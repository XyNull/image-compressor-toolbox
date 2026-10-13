"""Quick smoke test for the compression engine (non-TUI parts).

Generates test images of various formats, runs the compressor, and
verifies the output.  Run:  python test_core.py
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image

# Ensure local modules are importable
sys.path.insert(0, str(Path(__file__).parent))

from compressor import ImageCompressor, fmt_saving_pct
from detector import detect_format, SUPPORTED_FORMATS
from hwaccel import HardwareAccelerator
import config

cfg = config.load_config()
TARGET_BYTES = int(cfg.compression.target_size_mb * 1024 * 1024)


def make_test_image(width: int, height: int, mode: str = "RGB") -> Image.Image:
    """Create a noisy image that won't compress trivially (uses numpy)."""
    import numpy as np
    arr = np.random.randint(0, 256, (height, width, 3), dtype=np.uint8)
    img = Image.fromarray(arr, "RGB")
    if mode == "RGBA":
        img = img.convert("RGBA")
    elif mode == "P":
        img = img.convert("P")
    elif mode == "L":
        img = img.convert("L")
    return img


def main():
    print("=" * 60)
    print("  Image Compressor — Core Smoke Test")
    print("=" * 60)

    # --- Hardware detection ---
    hw = HardwareAccelerator()
    print(f"\n[HW] Accelerator : {hw.name}")
    print(f"[HW] Type        : {hw.type}")
    print(f"[HW] PyTorch     : {'yes' if hw._torch else 'no'}")

    # --- Format detection ---
    print(f"\n[DET] Supported  : {', '.join(sorted(SUPPORTED_FORMATS))}")

    # Create temp directory with test images
    tmpdir = Path(tempfile.mkdtemp(prefix="imgtest_"))
    print(f"\n[SETUP] Temp dir : {tmpdir}")

    test_images = []

    # 1. Small JPEG (should pass without resizing)
    img = make_test_image(800, 600, "RGB")
    p = tmpdir / "small.jpg"
    img.save(p, "JPEG", quality=95)
    test_images.append(("small JPEG", p))

    # 2. Large PNG (should need resizing — random noise JPEG > 5 MB)
    img = make_test_image(3500, 3500, "RGB")
    p = tmpdir / "large.png"
    img.save(p, "PNG")
    test_images.append(("large PNG", p))

    # 3. PNG with transparency (RGBA → white bg)
    img = make_test_image(500, 500, "RGBA")
    p = tmpdir / "alpha.png"
    img.save(p, "PNG")
    test_images.append(("RGBA PNG", p))

    # 4. GIF (single frame — should compress normally)
    img = make_test_image(400, 300, "P")
    p = tmpdir / "test.gif"
    img.save(p, "GIF")
    test_images.append(("GIF", p))

    # 4b. Animated GIF (should be converted to MP4 if FFmpeg available)
    frames = [make_test_image(400, 400, "RGB") for _ in range(5)]
    p = tmpdir / "animated.gif"
    frames[0].save(p, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    test_images.append(("Animated GIF", p))

    # 4c. Animated WebP (FFmpeg can't decode animated WebP directly;
    #     the converter must pipe frames via Pillow → FFmpeg stdin.)
    frames = [make_test_image(320, 240, "RGB") for _ in range(8)]
    p = tmpdir / "animated.webp"
    frames[0].save(
        p, "WEBP", save_all=True, append_images=frames[1:],
        duration=80, loop=0, quality=80,
    )
    test_images.append(("Animated WebP", p))

    # 5. BMP
    img = make_test_image(300, 200, "RGB")
    p = tmpdir / "test.bmp"
    img.save(p, "BMP")
    test_images.append(("BMP", p))

    # 6. WebP
    img = make_test_image(600, 400, "RGB")
    p = tmpdir / "test.webp"
    img.save(p, "WEBP", quality=80)
    test_images.append(("WebP", p))

    # Subdirectory file
    subdir = tmpdir / "sub"
    subdir.mkdir()
    img = make_test_image(500, 500, "RGB")
    p = subdir / "nested.png"
    img.save(p, "PNG")
    test_images.append(("nested PNG", p))

    # --- Test format detection ---
    print("\n[DET] Magic-byte detection:")
    all_pass = True
    for label, p in test_images:
        fmt = detect_format(p)
        expected = {
            "small JPEG": "jpeg", "large PNG": "png", "RGBA PNG": "png",
            "GIF": "gif", "Animated GIF": "gif", "BMP": "bmp", "WebP": "webp", "nested PNG": "png",
            "Animated WebP": "webp",
        }[label]
        ok = fmt == expected
        status = "✓" if ok else "✗"
        print(f"  {status} {label:15s} → detected: {fmt}  (expected: {expected})")
        if not ok:
            all_pass = False

    # --- Test compression ---
    print(f"\n[COMP] Compressing (target ≤ {cfg.compression.target_size_mb:.0f} MB, "
          f"{cfg.compression.output_format.upper()} q{cfg.compression.quality})…")
    compressor = ImageCompressor(hw, cfg)

    for label, p in test_images:
        # Determine root_dir: for the nested file, use tmpdir as root
        root = tmpdir if "nested" in label else None
        result = compressor.compress(p, root)

        name = p.name
        if result.skipped:
            if result.skip_reason == "animated":
                print(
                    f"  ⊘ {name:15s} → SKIPPED (animated, no FFmpeg)"
                )
            else:
                orig_mb = result.original_size / (1024 * 1024)
                out_mb = result.output_size / (1024 * 1024)
                print(
                    f"  ⊘ {name:15s} → SKIPPED "
                    f"(compressed {out_mb:.2f} MB ≥ original {orig_mb:.2f} MB)"
                )
        elif result.success and result.is_video:
            orig_mb = result.original_size / (1024 * 1024)
            out_mb = result.output_size / (1024 * 1024)
            delta = fmt_saving_pct(result.output_size, result.original_size)
            enc = result.encoder or "?"
            print(
                f"  🎬 {name:15s} → {out_mb:.2f} MB "
                f"(was {orig_mb:.2f} MB, {result.fmt.upper()}→MP4 [{enc}], {delta})"
            )
            if result.output_path and result.output_path.exists():
                print(f"      output: {result.output_path}")
            else:
                print(f"      ⚠ output file not found!")
                all_pass = False
        elif result.success:
            orig_mb = result.original_size / (1024 * 1024)
            out_mb = result.output_size / (1024 * 1024)
            delta = fmt_saving_pct(result.output_size, result.original_size)
            ok_size = result.output_size <= TARGET_BYTES
            status = "✓" if ok_size else "⚠"
            print(
                f"  {status} {name:15s} → "
                f"{out_mb:.2f} MB (was {orig_mb:.2f} MB, "
                f"{result.scale_count}× scale, {delta})"
            )
            # Verify output file exists
            if result.output_path and result.output_path.exists():
                print(f"      output: {result.output_path}")
            else:
                print(f"      ⚠ output file not found!")
                all_pass = False
        else:
            print(f"  ✗ {name:15s} → FAILED: {result.error}")
            all_pass = False

    # --- Verify output structure ---
    print("\n[STRUCT] Output directory structure:")
    compressed_dir = tmpdir / "compressed"
    if compressed_dir.exists():
        for root, dirs, files in os.walk(compressed_dir):
            rel = Path(root).relative_to(compressed_dir)
            for f in files:
                print(f"  {rel / f}")

    # --- Verify originals untouched ---
    print("\n[SAFE] Original files unchanged:")
    for label, p in test_images:
        orig_fmt = detect_format(p)
        print(f"  {p.name:15s} → still {orig_fmt}")

    # Cleanup
    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"\n[CLEAN] Removed temp dir")

    print("\n" + "=" * 60)
    if all_pass:
        print("  ✅ ALL TESTS PASSED")
    else:
        print("  ⚠ SOME TESTS FAILED — review above")
    print("=" * 60)


if __name__ == "__main__":
    main()
