"""时长修复 + 混编组统一转码 端到端验证 (真实 ffmpeg)。

场景:
  A. 干净 CFR 源 (3 x 2s): normalize_pts 开 → 成片视频流时长 ≈ 6s, 无异常。
  B. A/V 时长不匹配源 (v0 视频 2s/音频 4s): concat 会产生 2s 视频 PTS 空洞
     → normalize_pts 开: 成片视频流 ≈ 6s (重排消除空洞), 无异常;
     normalize_pts 关 + 修复关: 成片视频流 ≈ 8s → duration_issues 检出;
     normalize_pts 关 + 修复开: 自动修复回 6s。
  C. 混编组 (编码不一致 → 统一规格后拼接转 AV1, 2026-10-07 新增):
     C1 h264 + hevc + 无音轨 同分辨率 → 不再跳过, 输出 AV1, 时长 = 源之和,
        无音轨源按等长静音补齐 (输出仍带音轨);
     C2 全组无音轨 → 输出无音轨;
     C3 分辨率在容差内但不同 (320x240 + 316x236) → 缩放补边后拼接, 时长正确;
     C4 帧率不同 (30 + 25) → 统一到组内帧率, 时长正确;
     C5 mixed_codec_merge=false → 退回旧行为 (跳过该组)。
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


def make_clip(d: Path, name: str, vcodec: str = "libx264",
              size: str = "320x240", rate: int = 30, sec: float = 2.0,
              audio: bool = True, freq: int = 440) -> Path:
    """生成指定编码/分辨率/帧率的测试片段 (混编场景用)。"""
    p = d / name
    args = [FF, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i",
            f"testsrc=duration={sec}:size={size}:rate={rate}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency={freq}:duration={sec}",
                 "-map", "0:v", "-map", "1:a",
                 "-c:a", "aac", "-b:a", "96k"]
    args += ["-c:v", vcodec]
    if vcodec == "libx264":
        args += ["-preset", "ultrafast"]
    args += ["-pix_fmt", "yuv420p", str(p)]
    subprocess.run(args, check=True)
    return p


def stream_duration(path: Path, kind: str = "a") -> float:
    """ffprobe 读指定流的时长 (测试里校验音画是否等长)。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", f"{kind}:0",
             "-show_entries", "stream=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out)
    except (ValueError, subprocess.SubprocessError, OSError):
        return 0.0


def make_long_audio(d: Path, name: str, vcodec: str, vsec: float,
                    asec: float) -> Path:
    """指定编码 + 视频/音频时长不同的片段 (C6 音画等长校验用)。"""
    p = d / name
    args = [FF, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i",
            f"testsrc=duration={vsec}:size=320x240:rate=30",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={asec}",
            "-map", "0:v", "-map", "1:a",
            "-c:v", vcodec]
    if vcodec == "libx264":
        args += ["-preset", "ultrafast"]
    args += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", str(p)]
    subprocess.run(args, check=True)
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

        # --- 场景 C1: 混编 h264+hevc (+无音轨源) → 统一转 AV1 --- #
        print("场景 C1: h264 + hevc + 无音轨 同分辨率 (旧行为: 跳过)")
        dc1 = tmp / "c1"
        dc1.mkdir()
        make_clip(dc1, "a.mp4", "libx264", audio=True)
        make_clip(dc1, "b.mp4", "hevc_nvenc", audio=True)
        make_clip(dc1, "c.mp4", "libx264", audio=False)
        rep = merge_dir(dc1, mixed_codec_merge=True)
        out = dc1 / "V1.mp4"
        m2 = VideoMerger(None, load_config())
        check("C1: 输出生成 (不再跳过)", out.exists(),
              f"messages={rep.messages}")
        check("C1: 报告标记混编组", rep.mixed_groups == 1 and rep.merged == 1,
              f"mixed={rep.mixed_groups} merged={rep.merged}")
        if out.exists():
            pr = m2.probe(out)
            d = m2.probe_duration(out)
            au = m2._probe_audio(out)
            check("C1: 成片编码为 AV1", pr.vcodec == "av1", pr.vcodec)
            check("C1: 时长 ≈ 6s (三源之和)", abs(d - 6.0) <= 1.0, f"{d:.2f}s")
            check("C1: 无音轨源已按静音补齐 (成片带音轨)", au.has_audio,
                  f"audio={au.has_audio}")
            check("C1: 无时长异常", not rep.duration_issues,
                  str(rep.duration_issues))

        # --- 场景 C2: 全组无音轨 → 成片不带音轨 --- #
        print("场景 C2: 混编但全部无音轨")
        dc2 = tmp / "c2"
        dc2.mkdir()
        make_clip(dc2, "a.mp4", "libx264", audio=False)
        make_clip(dc2, "b.mp4", "hevc_nvenc", audio=False)
        rep = merge_dir(dc2, mixed_codec_merge=True)
        out = dc2 / "V1.mp4"
        check("C2: 输出生成", out.exists(), f"messages={rep.messages}")
        if out.exists():
            au = m2._probe_audio(out)
            d = m2.probe_duration(out)
            check("C2: 成片无音轨", not au.has_audio)
            check("C2: 时长 ≈ 4s", abs(d - 4.0) <= 1.0, f"{d:.2f}s")

        # --- 场景 C3: 分辨率在容差内但不同 → 缩放补边 --- #
        print("场景 C3: 320x240 + 316x236 (容差内不同尺寸, 混编)")
        dc3 = tmp / "c3"
        dc3.mkdir()
        make_clip(dc3, "a.mp4", "libx264", size="320x240")
        make_clip(dc3, "b.mp4", "hevc_nvenc", size="316x236")
        rep = merge_dir(dc3, mixed_codec_merge=True, tolerance_px=8)
        out = dc3 / "V1.mp4"
        check("C3: 输出生成", out.exists(), f"messages={rep.messages}")
        if out.exists():
            pr = m2.probe(out)
            d = m2.probe_duration(out)
            check("C3: 成片为组代表分辨率 320x240",
                  (pr.width, pr.height) == (320, 240),
                  f"{pr.width}x{pr.height}")
            check("C3: 时长 ≈ 4s", abs(d - 4.0) <= 1.0, f"{d:.2f}s")

        # --- 场景 C4: 帧率不同 → 统一到组内帧率 --- #
        print("场景 C4: 30fps + 25fps 混编")
        dc4 = tmp / "c4"
        dc4.mkdir()
        make_clip(dc4, "a.mp4", "libx264", rate=30)
        make_clip(dc4, "b.mp4", "hevc_nvenc", rate=25)
        rep = merge_dir(dc4, mixed_codec_merge=True)
        out = dc4 / "V1.mp4"
        check("C4: 输出生成", out.exists(), f"messages={rep.messages}")
        if out.exists():
            pr = m2.probe(out)
            d = m2.probe_duration(out)
            check("C4: 成片帧率统一为组内帧率 30", abs(pr.fps - 30.0) < 0.5,
                  f"{pr.fps}")
            check("C4: 时长 ≈ 4s", abs(d - 4.0) <= 1.0, f"{d:.2f}s")

        # --- 场景 C5: 关掉 mixed_codec_merge → 旧行为 (跳过) --- #
        print("场景 C5: mixed_codec_merge=false → 跳过该组")
        dc5 = tmp / "c5"
        dc5.mkdir()
        make_clip(dc5, "a.mp4", "libx264")
        make_clip(dc5, "b.mp4", "hevc_nvenc")
        rep = merge_dir(dc5, mixed_codec_merge=False)
        check("C5: 输出未生成", not (dc5 / "V1.mp4").exists())
        check("C5: 记入跳过", rep.skipped == 1 and rep.merged == 0,
              f"skipped={rep.skipped} merged={rep.merged}")

        # --- 场景 C6: 混编 + v0 音频长于视频 → 音画必须等长 --- #
        print("场景 C6: 混编 + 首源音频 4s/视频 2s (音画等长对齐)")
        dc6 = tmp / "c6"
        dc6.mkdir()
        make_long_audio(dc6, "a.mp4", "libx264", vsec=2.0, asec=4.0)
        make_long_audio(dc6, "b.mp4", "hevc_nvenc", vsec=2.0, asec=2.0)
        rep = merge_dir(dc6, mixed_codec_merge=True)
        out = dc6 / "V1.mp4"
        check("C6: 输出生成", out.exists(), f"messages={rep.messages}")
        if out.exists():
            vd = m2.probe_duration(out)
            ad = stream_duration(out, "a")
            check("C6: 视频流时长 ≈ 4s (源视频之和)", abs(vd - 4.0) <= 1.0,
                  f"{vd:.2f}s")
            check("C6: 音频流与视频流等长 (无漂移)", abs(ad - vd) <= 0.5,
                  f"audio={ad:.2f}s video={vd:.2f}s")
            check("C6: 无时长异常", not rep.duration_issues,
                  str(rep.duration_issues))

        # --- 场景 D: 前期合并产物不再重复合并/转码 (2026-10-07) --- #
        print("场景 D1: 单独一个 V1.mp4 (已 AV1 成品) → 不再处理")
        dd1 = tmp / "d1"
        dd1.mkdir()
        make_clip(dd1, "src0.mp4", "libx264")
        rep = merge_dir(dd1)                       # 先正常合并出 V1.mp4
        prod = dd1 / "V1.mp4"
        src0 = dd1 / "src0.mp4"
        src0.unlink()                              # 只留成品, 模拟二次运行
        before = (prod.stat().st_size, prod.stat().st_mtime_ns)
        check("D1: 成品就位且为 AV1",
              prod.exists() and m2.probe(prod).vcodec == "av1")
        rep = merge_dir(dd1)
        after = (prod.stat().st_size, prod.stat().st_mtime_ns)
        check("D1: 识别为前期合并产物", rep.already_merged == 1,
              f"already_merged={rep.already_merged} merged={rep.merged}")
        check("D1: 未再合并/转码 (文件未被重写)", before == after,
              f"{before} → {after}")
        check("D1: 无新增输出组", rep.merged == 0,
              f"merged={rep.merged}")

        print("场景 D2: V2.mp4(AV1) + 同分辨率新视频 → 照常参与合并 (不算成品)")
        dd2 = tmp / "d2"
        dd2.mkdir()
        make_clip(dd2, "src0.mp4", "libx264", audio=False)
        dd2_src = dd2 / "src0.mp4"
        merge_dir(dd2)
        (dd2 / "V1.mp4").rename(dd2 / "V2.mp4")     # 造一个"名字对但同分辨率有伴"的成品
        dd2_src.unlink()
        make_clip(dd2, "new.mp4", "hevc_nvenc", audio=False)   # 同分辨率新视频
        rep = merge_dir(dd2)
        check("D2: 不判为成品 (同分辨率还有可合并对象)", rep.already_merged == 0,
              f"already_merged={rep.already_merged}")
        check("D2: 照常产出 V1.mp4", (dd2 / "V1.mp4").exists(),
              f"messages={rep.messages}")

        print("场景 D3: V3.mp4 但不是 AV1 (h264) → 不算成品")
        dd3 = tmp / "d3"
        dd3.mkdir()
        make_clip(dd3, "V3.mp4", "libx264")
        rep = merge_dir(dd3)
        check("D3: 不判为成品 (编码非 AV1)", rep.already_merged == 0,
              f"already_merged={rep.already_merged}")
        check("D3: 照常合并出 V1.mp4", (dd3 / "V1.mp4").exists(),
              f"messages={rep.messages}")

        print("场景 D4: 名字不含 V+序号 的 AV1 文件 → 不算成品")
        dd4 = tmp / "d4"
        dd4.mkdir()
        make_clip(dd4, "src0.mp4", "libx264")
        merge_dir(dd4)
        (dd4 / "V1.mp4").rename(dd4 / "movie.mp4")   # 名字不符合输出规则
        (dd4 / "src0.mp4").unlink()
        rep = merge_dir(dd4)
        check("D4: 不判为成品 (命名不匹配)", rep.already_merged == 0,
              f"already_merged={rep.already_merged}")
        check("D4: 照常合并出 V1.mp4", (dd4 / "V1.mp4").exists(),
              f"messages={rep.messages}")

        print("场景 D5: 成品 + 待合并组共存 → 只处理待合并组")
        dd5 = tmp / "d5"
        dd5.mkdir()
        make_clip(dd5, "a.mp4", "libx264", size="320x240", audio=False)
        merge_dir(dd5)                                # 产出 V1.mp4 (320x240)
        (dd5 / "V1.mp4").rename(dd5 / "V9.mp4")
        (dd5 / "a.mp4").unlink()
        for i in range(2):
            make_clip(dd5, f"new{i}.mp4", "hevc_nvenc", size="640x480",
                      audio=False)
        rep = merge_dir(dd5)
        check("D5: 识别 1 个成品", rep.already_merged == 1,
              f"already_merged={rep.already_merged}")
        check("D5: 仍合并出新组", rep.merged == 1 and (dd5 / "V1.mp4").exists(),
              f"merged={rep.merged} messages={rep.messages}")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n== 结果: {PASS} 通过, {FAIL} 失败 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
