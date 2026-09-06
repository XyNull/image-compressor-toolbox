"""GUI 冒烟测试 (offscreen): 覆盖 扫描→压缩→覆盖→探测→分组→合并→设置。

用法: QT_QPA_PLATFORM=offscreen python -m gui.smoke_test
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import config  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402
from gui.models import ImgStatus  # noqa: E402
from gui.settings_dialog import save_config_values  # noqa: E402

# offscreen 环境自动确认所有弹窗 (覆盖模式确认/恢复覆盖确认)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

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


def wait_until(cond, timeout=60, app=None):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if app:
            app.processEvents()
        if cond():
            return True
        time.sleep(0.05)
    return False


def make_test_images(d: Path, n: int) -> None:
    from PIL import Image
    import numpy as np
    rng = np.random.default_rng(42)
    for i in range(n):
        arr = rng.integers(0, 255, (900, 1200, 3), dtype=np.uint8)
        Image.fromarray(arr).save(d / f"test_{i}.png")


def make_test_videos(d: Path, specs) -> None:
    import subprocess
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from winproc import HIDDEN_KW
    for name, wh in specs:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", f"testsrc=duration=1:size={wh}:rate=10",
             str(d / name)], check=True, **HIDDEN_KW)


def main() -> int:
    print("== GUI 冒烟测试 ==")
    tmp = Path(tempfile.mkdtemp(prefix="gui_smoke_"))
    img_dir = tmp / "imgs"
    vid_dir = tmp / "vids"
    img_dir.mkdir()
    vid_dir.mkdir()
    make_test_images(img_dir, 3)
    try:
        make_test_videos(vid_dir, [("a.mp4", "320x240"), ("b.mp4", "320x240"),
                                   ("c.mp4", "640x480")])
        has_video = True
    except Exception as e:
        print(f"  [skip] 测试视频生成失败: {e}")
        has_video = False

    app = QApplication.instance() or QApplication(sys.argv)
    cfg = config.load_config()
    cfg.output.default_mode = "subdir"
    cfg.output.subdir_name = "compressed"

    win = MainWindow(cfg, [])
    win.show()
    check("窗口创建", win.isVisible())

    # --- 1. 扫描 + 压缩 (子目录模式) --- #
    win.add_paths([img_dir])
    ok = wait_until(lambda: len(win.img_model.items) == 3, app=app)
    check("图片入队 3 张", ok)
    ok = wait_until(lambda: win.src_model.rowCount() == 0, timeout=1, app=app)
    check("图片目录误收视频为 0", ok)

    win.on_start_all()
    ok = wait_until(
        lambda: all(it.status in (ImgStatus.DONE, ImgStatus.SKIPPED,
                                  ImgStatus.FAILED)
                    for it in win.img_model.items) and not win._busy,
        timeout=120, app=app)
    check("子目录模式批次完成", ok)
    done = [it for it in win.img_model.items if it.status is ImgStatus.DONE]
    check("有压缩成功项", len(done) >= 1,
          f"done={len(done)} failed={[i.error for i in win.img_model.items]}")
    comp = img_dir / "compressed"
    check("compressed/ 已生成", comp.is_dir() and any(comp.iterdir()))
    check("节省列有值", all(
        it.output_size > 0 and it.output_size < it.size for it in done))

    # --- 2. 覆盖模式 --- #
    cfg2 = config.load_config()
    win2 = MainWindow(cfg2, [])
    win2.mode = "overwrite"
    win2.add_paths([img_dir])
    wait_until(lambda: len(win2.img_model.items) >= 3, app=app)
    win2.on_start_all()
    ok = wait_until(lambda: not win2._busy, timeout=120, app=app)
    check("覆盖模式批次完成", ok)
    check("compressed/ 已清空", not comp.exists() or not any(comp.iterdir()))

    # --- 3. 视频: 探测→分组→合并 --- #
    if has_video:
        win2.add_paths([vid_dir])
        ok = wait_until(lambda: win2.src_model.rowCount() == 3, timeout=30,
                        app=app)
        check("视频入队 3 个", ok)
        ok = wait_until(
            lambda: all(v.probe_ok for v in win2.src_model.items)
            and win2.tgt_model.rowCount() >= 2, timeout=60, app=app)
        check("视频探测+分组 (≥2 组)", ok,
              f"targets={win2.tgt_model.rowCount()}")
        names = [t.name for t in win2.tgt_model.items]
        check("目标命名 V1/V2", "V1.mp4" in names and "V2.mp4" in names,
              str(names))
        check("连线数据已构建 (≥2 组)", len(win2._video_links) >= 2,
              f"links={len(win2._video_links)}")
        check("连线覆盖全部源视频", sum(len(s) for s, _ in win2._video_links)
              == win2.src_model.rowCount(),
              f"src={win2.src_model.rowCount()}")
        check("连线画布已挂载", win2._flow.width() > 0)

        # --- 3.5 流图展开交互 (默认收起/互斥展开/再点收起) --- #
        flow = win2._flow
        check("默认收起 (源链路隐藏)", flow._expanded_key is None)
        tgts = list(win2.tgt_model.items)
        flow.toggle_expand(tgts[0])
        app.processEvents()
        check("单击展开目标链路",
              flow._expanded_key == (tgts[0].directory, tgts[0].name),
              str(flow._expanded_key))
        if len(tgts) > 1:
            flow.toggle_expand(tgts[1])
            app.processEvents()
            check("互斥展开 (开新自动关旧)",
                  flow._expanded_key == (tgts[1].directory, tgts[1].name),
                  str(flow._expanded_key))
            flow.toggle_expand(tgts[1])
            app.processEvents()
        check("再次单击收起", flow._expanded_key is None)
        flow.toggle_expand(tgts[0])
        app.processEvents()
        pix = flow.grab()
        check("流图展开态可渲染", not pix.isNull() and pix.width() > 0)
        flow.collapse()
        app.processEvents()

        win2.on_merge_only()
        ok = wait_until(lambda: not win2._busy, timeout=300, app=app)
        check("合并批次完成", ok)
        v1 = vid_dir / "V1.mp4"
        v2 = vid_dir / "V2.mp4"
        check("V1.mp4 生成", v1.exists())
        check("V2.mp4 生成", v2.exists())
        t1 = next((t for t in win2.tgt_model.items if t.name == "V1.mp4"), None)
        check("目标表已更新大小/节省率", bool(t1 and t1.output_size > 0
                                             and t1.orig_total > 0))

        # --- 3.6 时长校验 (源视频各 1s; V1=单视频, V2=两视频) --- #
        if v1.exists() and v2.exists():
            from video_merge import VideoMerger
            dur_m = VideoMerger(None, cfg2)
            d1, d2 = dur_m.probe_duration(v1), dur_m.probe_duration(v2)
            check("V1 时长 ≈ 1s", 0 < d1 <= 2.5, f"{d1:.2f}s")
            check("V2 时长 ≈ 2s (时长无虚增)", 1.2 < d2 <= 3.5, f"{d2:.2f}s")
            t2 = next((t for t in win2.tgt_model.items
                       if t.name == "V2.mp4"), None)
            check("V2 状态已合并 (无时长异常)",
                  bool(t2 and t2.status == "已合并"),
                  t2.status if t2 else "?")
            check("时长校验数据已记录",
                  bool(t2 and t2.duration_actual > 0
                       and abs(t2.duration_actual - d2) < 0.5),
                  f"actual={t2.duration_actual if t2 else '?'}")
    else:
        print("  [skip] 视频链路 (无 ffmpeg)")

    # --- 4. 移除选中 / 统计 / 主题 --- #
    if win2.img_model.rowCount():
        win2.img_table.selectAll()
        win2.on_remove_selected()
        check("移除选中后清空", win2.img_model.rowCount() == 0)
    win2._apply_theme("light")
    check("浅色主题切换", win2._theme == "light")
    win2._apply_theme("dark")

    # --- 5. 设置保存 (保留注释) --- #
    cfg_path = Path(config.CONFIG_PATH)
    backup = cfg_path.read_text("utf-8")
    try:
        save_config_values({"compression": {"quality": 80}})
        text = cfg_path.read_text("utf-8")
        check("设置写回 quality=80", "quality = 80" in text)
        check("注释保留", "# JPEG 质量" in text)
        cfg3 = config.load_config()
        check("重新加载生效", cfg3.compression.quality == 80)
    finally:
        cfg_path.write_text(backup, encoding="utf-8")

    win.close()
    win2.close()
    app.processEvents()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n== 结果: {PASS} 通过, {FAIL} 失败 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
