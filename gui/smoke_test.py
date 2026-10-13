"""GUI 冒烟测试 (offscreen): 覆盖 扫描→压缩→覆盖→动图复查→探测→分组→合并→设置。

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

from PySide6.QtCore import Qt, QThread, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import config  # noqa: E402
import gui.workers as W  # noqa: E402
from compressor import fmt_saving_pct  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402
from gui.models import (  # noqa: E402
    ImageQueueModel, ImgStatus, QueueItem, TargetItem, VideoTargetModel,
    saving_color,
)
from gui.settings_dialog import save_config_values  # noqa: E402
from gui.style import C_OK, C_WARN  # noqa: E402
from gui.workers import release_thread  # noqa: E402

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
    """specs: [(文件名, WxH)] 或 [(文件名, WxH, 编码器)]。

    指定编码器 → 用于构造"组内编码不一致"(混编) 场景; hevc 编码器不可用时
    自动退回默认 h264 (该组就不是混编组, 后面的断言仍成立)。
    """
    import subprocess
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from winproc import HIDDEN_KW
    for spec in specs:
        name, wh = spec[0], spec[1]
        codec = spec[2] if len(spec) > 2 else None
        base = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                "-i", f"testsrc=duration=1:size={wh}:rate=10"]
        if codec:
            try:
                subprocess.run(base + ["-c:v", codec, "-pix_fmt", "yuv420p",
                                       str(d / name)],
                               check=True, **HIDDEN_KW)
                continue
            except subprocess.CalledProcessError:
                print(f"  [warn] {codec} 不可用, {name} 退回默认编码")
        subprocess.run(base + [str(d / name)], check=True, **HIDDEN_KW)


def make_test_gif(d: Path, name: str, size=(640, 480), frames: int = 10) -> Path:
    """多帧 GIF (随机噪点 → GIF 体积大、转 MP4 后显著更小, 保证不被"更大跳过")。"""
    from PIL import Image
    import numpy as np
    rng = np.random.default_rng(7)
    imgs = [Image.fromarray(rng.integers(0, 255, (size[1], size[0], 3),
                                        dtype=np.uint8))
            for _ in range(frames)]
    p = d / name
    imgs[0].save(p, "GIF", save_all=True, append_images=imgs[1:],
                 duration=100, loop=0)
    return p


def main() -> int:
    print("== GUI 冒烟测试 ==")
    tmp = Path(tempfile.mkdtemp(prefix="gui_smoke_"))
    img_dir = tmp / "imgs"
    vid_dir = tmp / "vids"
    img_dir.mkdir()
    vid_dir.mkdir()
    make_test_images(img_dir, 3)
    try:
        # b.mp4 用 hevc: 与 a.mp4(h264) 同分辨率 → 构成"组内编码不一致"混编组
        make_test_videos(vid_dir, [("a.mp4", "320x240"), ("b.mp4", "320x240",
                                                          "hevc_nvenc"),
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

    # --- 1.5 体积变化列符号: 变小 -x% / 变大 +x% (2026-09-23 修复) --- #
    check("节省额 → -5%", fmt_saving_pct(95, 100) == "-5%",
          fmt_saving_pct(95, 100))
    check("变大 → +5% (不再 --5%)", fmt_saving_pct(105, 100) == "+5%",
          fmt_saving_pct(105, 100))
    check("零变化 → 0%", fmt_saving_pct(100, 100) == "0%",
          fmt_saving_pct(100, 100))
    check("缺值 → —", fmt_saving_pct(0, 100) == "—")
    _m = ImageQueueModel()
    _m.reset_items([QueueItem(
        path=Path("bigger.png"), size=100, output_size=105,
        status=ImgStatus.SKIPPED, skip_reason="larger")])
    _txt = _m.data(_m.index(0, 5))
    _fg = _m.data(_m.index(0, 5), Qt.ForegroundRole)
    check("表格列渲染 +5% 且无双负号", _txt == "+5%" and "--" not in _txt, _txt)
    check("变大用警告色 (非绿色)",
          _fg.name().lower() == C_WARN.lower()          # QColor.name() 为小写
          and saving_color(105, 100) == C_WARN
          and saving_color(95, 100) == C_OK,
          _fg.name())
    _vm = VideoTargetModel()
    _vm.reset_items([TargetItem(directory=Path("d"), name="V1.mp4",
                                output_size=210, orig_total=200)])
    _vt = _vm.data(_vm.index(0, 5))
    check("视频目标列同一口径 (+5%)", _vt == "+5%",
          _vt)
    check("视频目标列变大也是警告色",
          _vm.data(_vm.index(0, 5), Qt.ForegroundRole).name().lower()
          == C_WARN.lower())

    # --- 1.6 QThread 释放安全 (2026-09-25 崩溃修复) --- #
    # 复现: 线程在 run() 末尾 emit 终局信号时仍 isRunning(); 槽里直接丢引用
    # 会让 PySide6 立即析构运行中的 QThread → 工作线程写已释放内存 → 进程硬崩
    # (实测 exit=127, 无 traceback)。release_thread 必须先确认线程结束。
    class _TailThread(QThread):
        tail = Signal()

        def run(self) -> None:
            self.tail.emit()
            time.sleep(1.0)        # 模拟 emit 之后的收尾窗口

    fired: list = []
    holder: list = []

    def _start_tail() -> None:
        t = _TailThread()
        t.tail.connect(lambda: fired.append(1))
        holder.append(t)           # 唯一引用在 holder 里
        t.start()

    _start_tail()
    check("尾部线程已发出终局信号", wait_until(lambda: fired, 5, app=app))

    _old_ms = W._THREAD_JOIN_MS
    W._THREAD_JOIN_MS = 50         # 缩短等待窗口, 以便覆盖"等不到"分支
    _held = release_thread(holder[0])
    check("终局信号时线程仍在运行 (竞态窗口真实存在)",
          bool(_held is not None and _held.isRunning()))
    check("等不到结束就继续持有 — 绝不析构运行中的线程", _held is holder[0])

    W._THREAD_JOIN_MS = _old_ms   # 恢复 3s: 应能等到线程真正结束
    check("线程结束后才允许丢弃引用",
          release_thread(_held) is None and not _held.isRunning())
    holder.clear()                 # 已确认结束 → 安全丢引用 (继续跑就是证明)
    app.processEvents()

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

        # --- 3.55 auto_follow (合并动画修复): 跟随任务展开链路 --- #
        t0, t1 = tgts[0], tgts[1]
        flow.auto_follow(t0.directory, t0.name)
        check("auto_follow 展开任务链路",
              flow._expanded_key == (t0.directory, t0.name))
        t1.status = "合并中"
        flow.auto_follow(t1.directory, t1.name)
        check("auto_follow 切到合并中目标",
              flow._expanded_key == (t1.directory, t1.name))
        check("合并中动画计时器启动", flow._timer.isActive())
        flow.auto_follow(t0.directory, t0.name)
        check("合并中链路不被抢占",
              flow._expanded_key == (t1.directory, t1.name))
        t1.status = "待合并"
        flow.refresh()
        check("动画计时器随状态停止", not flow._timer.isActive())
        flow.collapse()
        app.processEvents()

        win2.on_merge_only()
        ok = wait_until(lambda: not win2._busy, timeout=300, app=app)
        check("合并批次完成", ok)
        v1 = vid_dir / "V1.mp4"
        v2 = vid_dir / "V2.mp4"
        check("V1.mp4 生成", v1.exists())
        check("V2.mp4 生成", v2.exists())

        # --- 3.65 混编组 (h264+hevc 同分辨率) 不再跳过, 统一转 AV1 后合并 --- #
        from video_merge import VideoMerger as _VM
        _mv = _VM(None, cfg2)
        _src_codecs = {_mv.probe(vid_dir / "a.mp4").vcodec,
                       _mv.probe(vid_dir / "b.mp4").vcodec}
        if _src_codecs == {"h264", "hevc"}:
            check("源组内编码确实不一致 (h264 + hevc)", True)
            check("混编组未被跳过 → V2.mp4 落盘", v2.exists(),
                  f"files={sorted(p.name for p in vid_dir.iterdir())}")
            check("混编成片编码为 AV1",
                  v2.exists() and _mv.probe(v2).vcodec == "av1",
                  _mv.probe(v2).vcodec if v2.exists() else "无输出")
            _dv2 = _mv.probe_duration(v2) if v2.exists() else 0.0
            check("混编成片时长 ≈ 2s (两源之和)", 1.2 < _dv2 <= 3.5,
                  f"{_dv2:.2f}s")
        else:
            print("  [skip] 混编场景 (源编码未构成本地可用的 h264+hevc)")

        # --- 3.66 前期合并产物: GUI 不再把它列为合并对象 (2026-10-07) --- #
        solo = tmp / "solo"
        solo.mkdir()
        shutil.copyfile(v2, solo / "V1.mp4")     # 只放一个已合并成品 (AV1)
        win5 = MainWindow(config.load_config(), [])
        win5.add_paths([solo])
        _ok5 = wait_until(
            lambda: any(v.status == "已合并产物" for v in win5.src_model.items),
            timeout=30, app=app)
        check("成品目录: 识别为「已合并产物」", _ok5,
              str([(v.path.name, v.status) for v in win5.src_model.items]))
        check("成品不生成合并目标", win5.tgt_model.rowCount() == 0,
              str([t.name for t in win5.tgt_model.items]))
        win5.on_merge_only()
        check("成品目录: 合并阶段空跑完成",
              wait_until(lambda: not win5._busy, timeout=120, app=app))
        check("成品文件仍在且未被重写", (solo / "V1.mp4").exists())
        win5.close()
        app.processEvents()

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

        # --- 3.7 动图转码产物必须进入合并流图 (2026-09-23 修复) --- #
        # 场景: 覆盖模式下 GIF → MP4 并移回原目录, 合并阶段会实际合并它;
        # 而 src_model 是"拖入时扫描"的快照 → 修复前流图里看不到它。
        anim_dir = tmp / "anim"
        anim_dir.mkdir()
        gif = make_test_gif(anim_dir, "clip.gif")
        cfg4 = config.load_config()
        win4 = MainWindow(cfg4, [])
        win4.mode = "overwrite"
        win4.add_paths([anim_dir])
        ok = wait_until(lambda: len(win4.img_model.items) == 1, timeout=30,
                        app=app)
        check("动图入图片队列", ok)
        check("拖入时无视频 (GIF 不是视频扩展名)",
              win4.src_model.rowCount() == 0)
        win4.on_start_all()
        ok = wait_until(lambda: not win4._busy, timeout=300, app=app)
        check("动图批次 (压缩+合并) 完成", ok)
        anim_mp4 = anim_dir / "clip.mp4"
        check("GIF 已转 MP4 并移回原目录", anim_mp4.exists(),
              f"dir={sorted(p.name for p in anim_dir.iterdir())}")
        src_paths = [v.path for v in win4.src_model.items]
        check("合并流图已含动图转码视频 (修复点)",
              any(p.name == "clip.mp4" for p in src_paths), str(src_paths))
        check("转码视频已探测 (>0 分辨率)",
              all(v.probe_ok and v.width > 0 for v in win4.src_model.items))
        check("已生成合并目标 V1.mp4",
              any(t.name == "V1.mp4" for t in win4.tgt_model.items)
              and win4._video_links, str([t.name for t in win4.tgt_model.items]))
        check("连线覆盖动图转码视频",
              sum(len(s) for s, _ in win4._video_links) == len(src_paths),
              f"src={len(src_paths)}")
        check("动图视频已实际合并 (V1.mp4 落盘)", (anim_dir / "V1.mp4").exists())
        t_anim = next((t for t in win4.tgt_model.items
                       if t.name == "V1.mp4"), None)
        check("流图目标标记已合并",
              bool(t_anim and t_anim.status == "已合并"),
              t_anim.status if t_anim else "?")
        win4.close()
        app.processEvents()
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
