"""时长修复端到端验证 (真实 ffmpeg)。

场景:
  A. 干净 CFR 源 (3 x 2s): normalize_pts 开 → 成片视频流时长 ≈ 6s, 无异常。
  B. A/V 时长不匹配源 (v0 视频 2s/音频 4s): concat 会产生 2s 视频 PTS 空洞
     → normalize_pts 开: 成片视频流 ≈ 6s (重排消除空洞), 无异常;
     normalize_pts 关 + 修复关: 成片视频流 ≈ 8s → duration_issues 检出;
     normalize_pts 关 + 修复开: 自动修复回 6s。
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config  # noqa: E402
from video_merge import VideoMerger, fmt_duration  # noqa: E402

FF = "ffmpeg"
PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def make_av(d: Path, name: str, vsec: float, asec: float) -> Path:
    """生成 h264+aac 测试视频; asec>vsec 时 concat 会出现视频 PTS 空洞。"""
    p = d / name
    subprocess.run(
        [FF, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i",
         f"testsrc=duration={vsec}:size=320x240:rate=30",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={asec}",
         "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "96k",
         str(p)], check=True)
    return p


def merge_dir(d: Path, **vm_overrides):
    cfg = load_config()
    for k, v in vm_overrides.items():
        setattr(cfg.video_merge, k, v)
    m = VideoMerger(None, cfg)
    assert m.is_available(), "ffmpeg 不可用"
    print(f"  编码器: {m.encoder_display()}")
    return m.merge_directory(d)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="durtest_"))
    try:
        # --- 场景 A: 干净 CFR --- #
        print("场景 A: 3 x 2s 干净 CFR (normalize 开)")
        da = tmp / "a"
        da.mkdir()
        for i in range(3):
            make_av(da, f"src{i}.mp4", 2.0, 2.0)
        rep = merge_dir(da, normalize_pts=True, duration_repair=True)
        out = da / "V1.mp4"
        check("A: 输出生成", out.exists())
        d = VideoMerger(None, load_config()).probe_duration(out)
        check("A: 视频流时长 ≈ 6s", abs(d - 6.0) <= 1.0, f"{d:.2f}s")
        check("A: 无时长异常", not rep.duration_issues, str(rep.duration_issues))

        # --- 场景 B1: A/V 不匹配 + normalize 开 --- #
        print("场景 B1: v0 音频 4s/视频 2s (normalize 开 → 空洞被重排消除)")
        db = tmp / "b1"
        db.mkdir()
        make_av(db, "src0.mp4", 2.0, 4.0)
        make_av(db, "src1.mp4", 2.0, 2.0)
        make_av(db, "src2.mp4", 2.0, 2.0)
        rep = merge_dir(db, normalize_pts=True, duration_repair=True)
        out = db / "V1.mp4"
        check("B1: 输出生成", out.exists())
        d = VideoMerger(None, load_config()).probe_duration(out)
        check("B1: 视频流时长 ≈ 6s (时长无虚增)", abs(d - 6.0) <= 1.0,
              f"{d:.2f}s")
        check("B1: 无时长异常", not rep.duration_issues,
              str(rep.duration_issues))

        # --- 场景 B2: normalize 关 + 修复关 → 校验必须检出 --- #
        print("场景 B2: 同源 (normalize 关/修复关 → 校验应检出 8s 异常)")
        dc = tmp / "b2"
        dc.mkdir()
        make_av(dc, "src0.mp4", 2.0, 4.0)
        make_av(dc, "src1.mp4", 2.0, 2.0)
        make_av(dc, "src2.mp4", 2.0, 2.0)
        rep = merge_dir(dc, normalize_pts=False, duration_repair=False)
        out = dc / "V1.mp4"
        d = VideoMerger(None, load_config()).probe_duration(out)
        print(f"  成片视频流时长: {fmt_duration(d)}")
        check("B2: 时长异常被检出", len(rep.duration_issues) == 1,
              str(rep.duration_issues))
        check("B2: 成片时长确实虚增 (~8s)", d > 7.0, f"{d:.2f}s")

        # --- 场景 B3: normalize 关 + 修复开 → 自动修复 --- #
        print("场景 B3: 同源 (normalize 关/修复开 → 自动修复回 6s)")
        dd = tmp / "b3"
        dd.mkdir()
        make_av(dd, "src0.mp4", 2.0, 4.0)
        make_av(dd, "src1.mp4", 2.0, 2.0)
        make_av(dd, "src2.mp4", 2.0, 2.0)
        rep = merge_dir(dd, normalize_pts=False, duration_repair=True)
        out = dd / "V1.mp4"
        d = VideoMerger(None, load_config()).probe_duration(out)
        check("B3: 修复后时长 ≈ 6s", abs(d - 6.0) <= 1.0, f"{d:.2f}s")
        check("B3: 无残留时长异常", not rep.duration_issues,
              str(rep.duration_issues))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n== 结果: {PASS} 通过, {FAIL} 失败 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
