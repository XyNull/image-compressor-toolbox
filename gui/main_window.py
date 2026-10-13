"""主窗口: 工具栏 + 图片表 + 视频双表 + 日志 + 进度统计。"""
from __future__ import annotations

import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QFileDialog, QFrame,
    QHeaderView, QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSplitter, QTableView,
    QTextEdit, QVBoxLayout, QWidget,
)

import win_perf
from compressor import CompressionResult
from config import Config
from hwaccel import HardwareAccelerator
from hwinfo_power import read_cpu_package_watts
from video_merge import VideoMerger, VideoProbe

from .merge_flow import MergeFlowView
from .models import (
    ImgStatus, ImageQueueModel, QueueItem, TargetItem, VideoItem,
    VideoSourceModel, VideoTargetModel,
)
from .settings_dialog import SettingsDialog
from .style import qss_for
from .workers import (
    CompressThread, MergeThread, ProbeThread, RedoThread, ScanThread,
    VideoRefreshThread, release_thread,
)

STATE_FILE = Path(__file__).resolve().parent.parent / "gui_state.json"
ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "app_icon.ico"


def human_size(n: int) -> str:
    if n <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def parse_paths(text: str) -> List[Path]:
    out: List[Path] = []
    for m in re.findall(r'"([^"]+)"|(\S+)', text):
        s = (m[0] or m[1]).strip().strip('"').strip("'")
        if s:
            out.append(Path(s))
    return out


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, initial_paths: List[Path]) -> None:
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle(cfg.ui.title)
        self.resize(1080, 760)
        self.setAcceptDrops(True)
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))

        # --- 状态 --- #
        self.hw = HardwareAccelerator()
        self.img_model = ImageQueueModel()
        self.src_model = VideoSourceModel()
        self.tgt_model = VideoTargetModel()
        self._merge_dirs: List[Path] = []
        self._merge_excluded: set = set()   # 右键移除的视频, 不再参与合并
        self._video_links: List[Tuple[List[int], int]] = []  # (源行列表, 目标行)
        self._leftover: List[CompressionResult] = []
        self._leftover_seen: set = set()
        self._cancel = threading.Event()
        self._busy = False
        self._batch_dirs: List[Path] = []     # 打开输出目录用
        self._probe_pending = 0
        self._total = 0
        self._done = 0
        self._compress_thread: Optional[CompressThread] = None
        self._scan_thread: Optional[ScanThread] = None
        self._probe_thread: Optional[ProbeThread] = None
        self._refresh_thread: Optional[VideoRefreshThread] = None
        self._merge_thread: Optional[MergeThread] = None
        self._redo_thread: Optional[RedoThread] = None
        self._merge_after_compress = True   # 压缩后是否继续合并 (一键开始)
        self._probe_then_merge = False      # 合并前复查探测完成后继续合并

        self._build_ui()
        self._apply_theme(self._load_theme())
        win_perf.boost_process()

        # CPU 功耗 (HWiNFO 共享内存): 每秒刷新, 读不到时标签为空
        self._power_timer = QTimer(self)
        self._power_timer.setInterval(1000)
        self._power_timer.timeout.connect(self._update_power)
        self._power_timer.start()
        self._update_power()

        if initial_paths:
            QTimer.singleShot(50, lambda: self.add_paths(initial_paths))

    def _update_power(self) -> None:
        try:
            watts = read_cpu_package_watts()
        except Exception:
            watts = None
        self.power_label.setText(f"CPU {watts:.1f} W" if watts else "")

    # ------------------------------------------------------------------ #
    #  UI 构建
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        lay.addWidget(self._build_header())
        lay.addWidget(self._build_toolbar())
        lay.addWidget(self._build_mode_row())

        split = QSplitter(Qt.Vertical)
        split.addWidget(self._build_images_area())
        split.addWidget(self._build_video_area())
        split.addWidget(self._build_log_area())
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 3)
        split.setStretchFactor(2, 2)
        # QScrollArea 的 sizeHint 很小, 不显式分配会被压扁
        split.setSizes([420, 250, 180])
        split.setChildrenCollapsible(False)
        lay.addWidget(split, 1)

        lay.addWidget(self._build_footer())

        self.setCentralWidget(central)
        self._build_shortcuts()

    def _build_header(self) -> QWidget:
        w = QWidget()
        w.setObjectName("header")
        h = QHBoxLayout(w)
        h.setContentsMargins(14, 8, 14, 8)
        dot = QLabel("●")
        dot.setStyleSheet("color:#378ADD;font-size:11px;")
        title = QLabel(self.cfg.ui.title)
        title.setObjectName("appTitle")
        self.hw_badge = QLabel()
        self.hw_badge.setObjectName("hwBadge")
        nvenc = self.hw.type == "nvidia"
        try:
            qsv = self.hw._check_intel_qsv()
        except Exception:
            qsv = False
        self.hw_badge.setTextFormat(Qt.RichText)
        self.hw_badge.setText(
            f"NVENC <b style='color:{'#97C459' if nvenc else '#9a9b9f'}'>"
            f"{'✓' if nvenc else '✗'}</b>&nbsp;&nbsp;"
            f"QSV <b style='color:{'#97C459' if qsv else '#9a9b9f'}'>"
            f"{'✓' if qsv else '✗'}</b>")
        h.addWidget(dot)
        h.addWidget(title)
        h.addStretch(1)
        self.power_label = QLabel("")
        self.power_label.setObjectName("hwBadge")
        self.power_label.setToolTip(
            "CPU 封装功耗 (读取 HWiNFO 共享内存, 每秒刷新)\n"
            "需要运行 HWiNFO64 并在 Settings → Safety 中\n"
            "勾选 Shared Memory Support; 未运行时自动隐藏")
        h.addWidget(self.power_label)
        h.addWidget(self.hw_badge)
        return w

    def _build_toolbar(self) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(10, 8, 10, 8)
        h.setSpacing(6)

        def sep() -> None:
            line = QFrame()
            line.setFrameShape(QFrame.VLine)
            line.setObjectName("toolSep")
            line.setFixedHeight(22)
            h.addWidget(line)

        # --- 执行 --- #
        self.btn_start = QPushButton("▶ 一键开始")
        self.btn_start.setObjectName("primary")
        self.btn_start.setToolTip("压缩图片 (阶段1) + 合并视频 (阶段2)")
        self.btn_start.clicked.connect(self.on_start_all)
        self.btn_compress_only = QPushButton("仅压缩图片")
        self.btn_compress_only.setToolTip("只执行图片压缩, 不合并视频")
        self.btn_compress_only.clicked.connect(self.on_compress_only)
        self.btn_stop = QPushButton("■ 停止")
        self.btn_stop.setObjectName("stopBtn")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.on_stop)
        for b in (self.btn_start, self.btn_compress_only, self.btn_stop):
            h.addWidget(b)

        sep()
        # --- 文件 --- #
        self.btn_add_files = QPushButton("添加文件")
        self.btn_add_files.clicked.connect(self.on_add_files)
        self.btn_add_dir = QPushButton("添加文件夹")
        self.btn_add_dir.clicked.connect(self.on_add_dir)
        self.btn_remove = QPushButton("移除选中")
        self.btn_remove.clicked.connect(self.on_remove_selected)
        self.btn_clear = QPushButton("清空列表")
        self.btn_clear.setToolTip("清空图片与视频列表 (不删除文件)")
        self.btn_clear.clicked.connect(self.on_clear_all)
        for b in (self.btn_add_files, self.btn_add_dir, self.btn_remove,
                  self.btn_clear):
            h.addWidget(b)

        sep()
        # --- 视频 --- #
        self.btn_merge = QPushButton("合并视频")
        self.btn_merge.clicked.connect(self.on_merge_only)
        self.btn_redo = QPushButton("恢复覆盖")
        self.btn_redo.clicked.connect(self.on_redo)
        self.btn_redo.setEnabled(False)
        for b in (self.btn_merge, self.btn_redo):
            h.addWidget(b)

        sep()
        # --- 工具 --- #
        self.btn_open_out = QPushButton("打开输出目录")
        self.btn_open_out.clicked.connect(self.on_open_output)
        self.btn_settings = QPushButton("⚙ 设置")
        self.btn_settings.clicked.connect(self.on_settings)
        self.btn_theme = QPushButton("浅色")
        self.btn_theme.setObjectName("themeBtn")
        self.btn_theme.setToolTip("切换浅色/深色主题")
        self.btn_theme.clicked.connect(self.on_toggle_theme)
        h.addWidget(self.btn_open_out)
        h.addStretch(1)          # 设置/主题推到最右
        h.addWidget(self.btn_settings)
        h.addWidget(self.btn_theme)
        return w

    def _build_mode_row(self) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(10, 4, 10, 6)
        h.setSpacing(8)
        lab = QLabel("输出模式")
        lab.setObjectName("hint")
        self.rb_subdir = QPushButton("子目录")
        self.rb_overwrite = QPushButton("覆盖")
        for b in (self.rb_subdir, self.rb_overwrite):
            b.setCheckable(True)
            b.setStyleSheet("padding:4px 14px;")
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.rb_subdir)
        self.mode_group.addButton(self.rb_overwrite)
        self.mode_group.setExclusive(True)
        self.mode_group.idClicked.connect(self.on_mode_changed)
        default_overwrite = self.cfg.output.default_mode == "overwrite"
        (self.rb_overwrite if default_overwrite else self.rb_subdir).setChecked(
            True)
        self.mode = "overwrite" if default_overwrite else "subdir"
        hint = QLabel("拖入文件或文件夹到窗口任意位置即可加入队列")
        hint.setObjectName("hint")
        h.addWidget(lab)
        h.addWidget(self.rb_subdir)
        h.addWidget(self.rb_overwrite)
        h.addStretch(1)
        h.addWidget(hint)
        return w

    def _build_images_area(self) -> QWidget:
        self.img_table = QTableView()
        self.img_table.setModel(self.img_model)
        self.img_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.img_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.img_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.img_table.setAlternatingRowColors(True)
        self.img_table.verticalHeader().setVisible(False)
        self.img_table.setShowGrid(False)
        hdr = self.img_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setStretchLastSection(False)
        for col, w in ((0, 36), (1, 260), (2, 56), (3, 84), (4, 84), (5, 68),
                       (6, 92)):
            hdr.resizeSection(col, w)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        self.img_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.img_table.customContextMenuRequested.connect(self._table_menu)
        self.img_table.doubleClicked.connect(self._open_selected)
        return self.img_table

    def _build_video_area(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        lab = QLabel("视频合并 (单击目标视频展开来源链路 · 悬停查看详情)")
        lab.setObjectName("sectionLabel")
        v.addWidget(lab)

        # 合并流图: 左列源视频节点, 右列目标节点, 曲线连接
        self._flow = MergeFlowView(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._flow)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setObjectName("flowScroll")
        v.addWidget(scroll, 1)
        return wrap

    def _build_log_area(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        lab = QLabel("运行日志")
        lab.setObjectName("sectionLabel")
        v.addWidget(lab)
        self.log_view = QTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(3000)
        v.addWidget(self.log_view, 1)
        return wrap

    def _style_table(self, table: QTableView, widths, stretch=1) -> None:
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.setShowGrid(False)
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        for col, w in enumerate(widths):
            hdr.resizeSection(col, w)
        hdr.setSectionResizeMode(stretch, QHeaderView.Stretch)

    def _build_footer(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(10, 6, 10, 8)
        v.setSpacing(4)
        prog_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("")
        self.prog_label = QLabel("")
        self.prog_label.setObjectName("statValue")
        prog_row.addWidget(self.progress, 1)
        prog_row.addWidget(self.prog_label)
        v.addLayout(prog_row)
        stat_row = QHBoxLayout()
        self.stat_label = QLabel("")
        self.stat_label.setObjectName("statValue")
        stat_row.addWidget(self.stat_label)
        v.addLayout(stat_row)
        self._update_stats()
        return w

    def _build_shortcuts(self) -> None:
        QShortcut(QKeySequence.Paste, self).activated.connect(self.on_paste)
        QShortcut(QKeySequence("Ctrl+O"), self).activated.connect(
            self.on_add_files)

    # ------------------------------------------------------------------ #
    #  拖放 / 粘贴 / 添加
    # ------------------------------------------------------------------ #

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls()
                 if u.isLocalFile()]
        if paths:
            self.add_paths(paths)

    def on_paste(self) -> None:
        text = QApplication.clipboard().text()
        paths = parse_paths(text)
        if paths:
            self.add_paths(paths)

    def on_add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "选择图片/视频")
        if files:
            self.add_paths([Path(f) for f in files])

    def on_add_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if d:
            self.add_paths([Path(d)])

    def add_paths(self, paths: List[Path]) -> None:
        if self._busy:
            self._log("正在处理中，请等待当前批次完成。", "warn")
            return
        valid = [p for p in paths if p.exists()]
        if not valid:
            self._log("路径不存在，已忽略。", "warn")
            return
        self._log(f"正在扫描 {len(valid)} 个路径…")
        self._scan_thread = ScanThread(
            valid, existing={it.path for it in self.img_model.items},
            leftover_seen=self._leftover_seen, cfg=self.cfg)
        self._scan_thread.done.connect(self._on_scan_done)
        self._scan_thread.finished.connect(self._scan_thread.deleteLater)
        self._scan_thread.start()

    def _on_scan_done(self, images: List[QueueItem], videos: List[VideoItem],
                      merge_dirs: list, leftover: list,
                      skipped: int) -> None:
        self._scan_thread = release_thread(self._scan_thread)
        # 图片
        existing = {it.path for it in self.img_model.items}
        new_items = [it for it in images if it.path not in existing]
        if new_items:
            self.img_model.reset_items(self.img_model.items + new_items)
        # 视频 (排除用户右键移除过的)
        existing_v = {v.path for v in self.src_model.items}
        new_videos = [v for v in videos
                      if v.path not in existing_v
                      and v.path not in self._merge_excluded]
        if new_videos:
            self.src_model.reset_items(self.src_model.items + new_videos)
        # 合并目录
        for d in merge_dirs:
            if d not in self._merge_dirs:
                self._merge_dirs.append(d)
        # 残留
        if leftover:
            self._leftover.extend(leftover)
            self._leftover_seen.update(
                r.output_path for r in leftover if r.output_path)
            self.btn_redo.setEnabled(True)
            self._log(f"发现 {len(leftover)} 个未完成的覆盖"
                      f"(compressed/ 残留) — 点击\"恢复覆盖\"一键恢复", "warn")
        # 日志
        if new_items:
            msg = (f"已添加 {len(new_items)} 张图片 — "
                   f"当前列表共 {len(self.img_model.items)} 张")
            if skipped:
                msg += f"（跳过 {skipped} 个非图片文件）"
            self._log(msg, "ok")
        elif skipped:
            self._log(f"未添加新图片（跳过 {skipped} 个非图片文件）", "warn")
        else:
            self._log("未发现新图片（可能已在列表中）")
        if new_videos:
            self._log(f"发现 {len(new_videos)} 个视频（待探测分组）", "ok")
            self._flow.refresh()          # 先显示"待探测"节点
            self._start_probe(new_videos)
        elif new_items or new_videos:
            self.regroup()
        self._update_stats()

    # ------------------------------------------------------------------ #
    #  视频探测 + 分组
    # ------------------------------------------------------------------ #

    def _start_probe(self, videos: List[VideoItem]) -> None:
        self._probe_thread = ProbeThread(videos, self.cfg)
        self._probe_thread.probed.connect(self._on_probed)
        self._probe_thread.finished_all.connect(self._on_probe_all_done)
        self._probe_thread.finished.connect(self._probe_thread.deleteLater)
        self._probe_thread.start()

    def _on_probed(self, probe: VideoProbe) -> None:
        row = self.src_model.find_row(probe.path)
        if row < 0:
            return
        it = self.src_model.items[row]
        it.probe_ok = probe.ok
        it.vcodec = probe.vcodec
        it.width = probe.width
        it.height = probe.height
        it.status = "已探测" if probe.ok else "失败"
        self.src_model.update_row(row)

    def _on_probe_all_done(self) -> None:
        self._probe_thread = release_thread(self._probe_thread)
        self.regroup()
        if self._probe_then_merge:      # 合并前的复查探测: 探完继续走合并
            self._probe_then_merge = False
            self._launch_merge()

    def regroup(self) -> None:
        """按目录对已探测视频分组 → 生成 V1/V2 目标预览 (与合并器一致)。"""
        merger = VideoMerger(None, self.cfg)
        vm = self.cfg.video_merge
        by_dir: dict = {}
        for v in self.src_model.items:
            if v.probe_ok:
                by_dir.setdefault(v.path.parent, []).append(v)
        targets: List[TargetItem] = []
        links: List[Tuple[List[int], int]] = []
        done_count = 0
        for d in sorted(by_dir):
            items = by_dir[d]
            probes = [VideoProbe(path=v.path, width=v.width, height=v.height,
                                 vcodec=v.vcodec, ok=True) for v in items]
            groups = merger._group_videos(probes)
            # 前期合并产物 (已是 AV1 的 V*.mp4 且本目录同分辨率仅此一份) 不参与
            # 合并 —— 与合并器同一判据, 保证流图显示 = 实际合并范围。
            probe_map = {p.path: p for p in probes}
            done_paths = set(merger.detect_merged_outputs(groups, probe_map, vm))
            if done_paths:
                for v in items:
                    if v.path in done_paths:
                        v.target = ""
                        v.status = "已合并产物"
                        r = self.src_model.find_row(v.path)
                        if r >= 0:
                            self.src_model.update_row(r)
                done_count += len(done_paths)
                for g in groups:
                    g.videos = [p for p in g.videos if p not in done_paths]
                groups = [g for g in groups if g.videos]
            over_groups = len(groups) > vm.max_output_files
            out_index = 0
            for g in groups:
                over_count = len(g.videos) > vm.max_videos_per_group
                if not over_count:
                    out_index += 1
                name = f"{vm.output_prefix}{out_index}.{vm.output_extension}"
                total = 0
                src_rows: List[int] = []
                for p in g.videos:
                    for v in items:
                        if v.path == p:
                            total += v.size
                            v.target = name if not over_groups else "—"
                            v.status = ("组数超限" if over_groups
                                        else "单组超限" if over_count
                                        else "待合并")
                            r = self.src_model.find_row(v.path)
                            if r >= 0:
                                src_rows.append(r)
                            break
                tgt_row = len(targets)
                targets.append(TargetItem(
                    directory=d, name=name, video_count=len(g.videos),
                    orig_total=total,
                    status="组数超限" if over_groups
                    else "单组超限" if over_count else "待合并"))
                links.append((src_rows, tgt_row))
        # 保留旧状态 (已合并/时长异常/跳过)
        old = {(t.directory, t.name): t for t in self.tgt_model.items}
        for t in targets:
            prev = old.get((t.directory, t.name))
            if prev and prev.status in ("已合并", "时长异常"):
                t.status = prev.status
                t.output_size = prev.output_size
                t.output_path = prev.output_path
                t.duration_expected = prev.duration_expected
                t.duration_actual = prev.duration_actual
        self.tgt_model.reset_items(targets)
        self.src_model.refresh_all()
        self._video_links = links
        if done_count:
            self._log(f"跳过 {done_count} 个前期合并产物"
                      "（已是 AV1 成品，且本目录同分辨率无可合并对象）")
        self._flow.refresh()

    # ------------------------------------------------------------------ #
    #  批次执行
    # ------------------------------------------------------------------ #

    def on_start_all(self) -> None:
        self._start_common(do_merge=True)

    def on_compress_only(self) -> None:
        self._start_common(do_merge=False)

    def _start_common(self, do_merge: bool) -> None:
        if self._busy:
            self._log("正在处理中，请等待完成。", "warn")
            return
        pending = [it for it in self.img_model.items
                   if it.status in (ImgStatus.PENDING, ImgStatus.FAILED)]
        if not pending and (not do_merge or not self._merge_dirs):
            self._log("列表为空 — 请先拖入包含图片或视频的目录。", "warn")
            return
        if self.mode == "overwrite" and pending:
            ret = QMessageBox.question(
                self, "确认覆盖模式",
                "覆盖模式将用压缩结果覆盖原图（不可恢复）。\n"
                f"共 {len(pending)} 张图片。\n\n确定继续？",
                QMessageBox.Yes | QMessageBox.No)
            if ret != QMessageBox.Yes:
                return
        self._merge_after_compress = do_merge
        if not pending and do_merge:
            self._begin_batch([], do_merge=True)
        else:
            self._begin_batch(pending, do_merge=False)

    def on_merge_only(self) -> None:
        if self._busy:
            self._log("正在处理中，请等待完成。", "warn")
            return
        if not self._merge_dirs:
            self._log("没有可合并的目录 — 请先拖入包含视频的文件夹。", "warn")
            return
        self._merge_after_compress = True
        self._begin_batch([], do_merge=True)

    def on_clear_all(self) -> None:
        """清空图片 + 视频列表 (不删除任何文件)。"""
        if self._busy:
            self._log("正在处理中，无法清空。", "warn")
            return
        n_img = len(self.img_model.items)
        n_vid = len(self.src_model.items)
        if not n_img and not n_vid:
            self._log("列表已经是空的。")
            return
        ret = QMessageBox.question(
            self, "清空列表",
            f"将移除 {n_img} 张图片与 {n_vid} 个视频的队列记录。\n"
            "不会删除任何文件。确定清空？",
            QMessageBox.Yes | QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        self.img_model.reset_items([])
        self.src_model.reset_items([])
        self.tgt_model.reset_items([])
        self._merge_dirs.clear()
        self._merge_excluded.clear()
        self._video_links = []
        self._leftover.clear()
        self._leftover_seen.clear()
        self.btn_redo.setEnabled(False)
        self._flow.refresh()
        self._update_stats()
        self._log(f"已清空列表（图片 {n_img}、视频 {n_vid}），文件未删除。", "ok")

    def _begin_batch(self, items: List[QueueItem], do_merge: bool) -> None:
        self._set_busy(True)
        self._cancel.clear()
        self._batch_dirs = []
        if items:
            self._compress_thread = CompressThread(
                items, self.cfg, self.mode == "overwrite", self._cancel)
            self._compress_thread.item_done.connect(self._on_item_done)
            self._compress_thread.current.connect(
                lambda p: self.prog_label.setText(p))
            self._compress_thread.log.connect(self._log)
            self._compress_thread.finished_all.connect(self._on_compress_done)
            self._compress_thread.finished.connect(
                self._compress_thread.deleteLater)
            self._total = len(items)
            self._done = 0
            self.progress.setRange(0, max(1, len(items)))
            self.progress.setValue(0)
            self._compress_thread.start()
        elif do_merge:
            self._start_merge()

    def _on_item_done(self, item: QueueItem,
                      result: CompressionResult) -> None:
        row = self.img_model.find_row(item.path)
        if row >= 0:
            it = self.img_model.items[row]
            it.status = (ImgStatus.DONE if result.success and not result.skipped
                         else ImgStatus.SKIPPED if result.skipped
                         else ImgStatus.FAILED)
            it.output_size = result.output_size
            it.output_path = result.output_path
            it.error = result.error or ""
            it.scale_count = result.scale_count
            it.skip_reason = result.skip_reason or ""
            it.encoder = result.encoder or ""
            self.img_model.update_row(row)
            # 输出目录记录 (打开输出目录用)
            if it.status is ImgStatus.DONE:
                out_dir = (result.output_path.parent if not result.skipped
                           and result.output_path else item.path.parent)
                if self.mode == "overwrite":
                    out_dir = item.path.parent
                if out_dir not in self._batch_dirs:
                    self._batch_dirs.append(out_dir)
        self._done += 1
        self.progress.setValue(self._done)
        self._update_stats()

    def _on_compress_done(self, ok: bool) -> None:
        self._compress_thread = release_thread(self._compress_thread)
        if not ok:
            self._finish_batch()
            return
        if self._merge_after_compress and self._merge_dirs:
            self._start_merge()
        else:
            if not self._merge_after_compress:
                self._log("图片压缩完成（本次未合并视频）。", "ok")
            self._finish_batch()

    def _start_merge(self) -> None:
        """进入合并阶段: 先复查视频列表, 再启动合并线程。

        复查的必要性 (2026-09-23 修复): src_model 是"拖入时扫描"的快照, 而压缩
        阶段新产生的动图转码 MP4 (覆盖模式下已移回所在目录) 不在其中; 合并线程
        却是合并前重新扫描磁盘的, 于是出现"实际合并了但流图里没有"。先复查补齐
        (含探测→重新分组) 可保证流图显示 = 实际合并范围。
        """
        self.prog_label.setText("视频合并中…")
        self.progress.setRange(0, 0)  # 忙碌指示
        self._refresh_thread = VideoRefreshThread(
            list(self._merge_dirs),
            known={v.path for v in self.src_model.items},
            exclude=self._merge_excluded)
        self._refresh_thread.found.connect(self._on_videos_refreshed)
        self._refresh_thread.finished.connect(self._refresh_thread.deleteLater)
        self._refresh_thread.start()

    def _on_videos_refreshed(self, new_videos: List[VideoItem]) -> None:
        """复查结果: 有新增先探测再合并 (让分组用真实分辨率), 否则直接合并。"""
        self._refresh_thread = release_thread(self._refresh_thread)
        if not new_videos:
            self._launch_merge()
            return
        self.src_model.reset_items(self.src_model.items + new_videos)
        names = "、".join(v.path.name for v in new_videos[:3])
        more = f" 等 {len(new_videos)} 个" if len(new_videos) > 3 else ""
        self._log(f"合并前复查: 新增 {len(new_videos)} 个视频（{names}{more}）"
                  "— 多为压缩阶段动图转成的 MP4, 已加入合并列表", "ok")
        self._flow.refresh()               # 先显示"待探测"节点
        self._update_stats()
        self._probe_then_merge = True
        self._start_probe(new_videos)

    def _launch_merge(self) -> None:
        self._merge_thread = MergeThread(list(self._merge_dirs), self.cfg,
                                         self._cancel,
                                         exclude=self._merge_excluded)
        self._merge_thread.log.connect(self._log)
        self._merge_thread.dir_started.connect(self._on_merge_dir_started)
        self._merge_thread.task_started.connect(self._on_merge_task_started)
        self._merge_thread.dir_done.connect(self._on_merge_dir_done)
        self._merge_thread.finished_all.connect(self._on_merge_done)
        self._merge_thread.finished.connect(self._merge_thread.deleteLater)
        self._merge_thread.start()

    def _on_merge_task_started(self, directory: str, name: str) -> None:
        """子任务 (复制流合并/转码) 开跑 → 流图自动跟随展开其来源链路,
        保证流动动画可见; 用户正盯着某个"合并中"链路时不抢焦点。"""
        try:
            self._flow.auto_follow(Path(directory), name)
        except Exception:
            pass

    def _on_merge_dir_started(self, d: str) -> None:
        dp = Path(d)
        for i, t in enumerate(self.tgt_model.items):
            if t.directory == dp and t.status == "待合并":
                t.status = "合并中"
        self.tgt_model.refresh_all()
        for i, v in enumerate(self.src_model.items):
            if (v.path.parent == dp and v.target
                    and v.status == "待合并"):
                v.status = "合并中"
        self.src_model.refresh_all()
        self._flow.refresh()

    def _on_merge_dir_done(self, report) -> None:
        outputs = {p: p.stat().st_size for p in report.outputs
                   if p.exists()}
        # 时长异常: (输出路径, 期望时长, 实际时长); info 含全部正常输出
        issues = {p: (e, a) for p, e, a in
                  getattr(report, "duration_issues", [])}
        info = {p: (e, a) for p, e, a in
                getattr(report, "duration_info", [])}
        for i, t in enumerate(self.tgt_model.items):
            if t.directory != report.directory:
                continue
            out_path = t.directory / t.name
            if out_path in outputs:
                t.status = ("时长异常" if out_path in issues
                            else "已合并")
                t.output_size = outputs[out_path]
                t.output_path = out_path
                if out_path in issues:
                    t.duration_expected, t.duration_actual = \
                        issues[out_path]
                    self._log(
                        f"⚠ {t.name} ({t.directory.name}) 时长异常: "
                        f"实际 {t.duration_actual:.1f}s / 期望 "
                        f"{t.duration_expected:.1f}s — 请检查该文件",
                        "warn")
                elif out_path in info:
                    t.duration_expected, t.duration_actual = info[out_path]
                if out_path.parent not in self._batch_dirs:
                    self._batch_dirs.append(out_path.parent)
            elif t.status == "合并中":
                t.status = "跳过"
        for i, v in enumerate(self.src_model.items):
            if v.path.parent == report.directory and v.status == "合并中":
                v.status = "已合并" if v.target and (
                    v.path.parent / v.target) in outputs else "跳过"
        self.tgt_model.refresh_all()
        self.src_model.refresh_all()
        self._flow.refresh()
        self._update_stats()

    def _on_merge_done(self, ok: bool) -> None:
        self._merge_thread = release_thread(self._merge_thread)
        self._finish_batch()

    def _finish_batch(self) -> None:
        self._busy = False
        self._set_busy(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.prog_label.setText("")
        self._log("✓ 全部任务完成", "ok")
        self._update_stats()

    # ------------------------------------------------------------------ #
    #  停止 / 恢复覆盖 / 移除 / 打开
    # ------------------------------------------------------------------ #

    def on_stop(self) -> None:
        self._cancel.set()
        self.btn_stop.setEnabled(False)
        self.btn_stop.setText("停止中…")
        self._log("正在停止… (已开始的任务会完成)", "warn")

    def on_redo(self) -> None:
        if self._busy or not self._leftover:
            return
        ret = QMessageBox.question(
            self, "确认恢复覆盖",
            f"将把 {len(self._leftover)} 个压缩残留移回原目录覆盖原图。\n"
            "确定继续？", QMessageBox.Yes | QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        self._set_busy(True)
        self._cancel.clear()
        results = list(self._leftover)
        self._redo_thread = RedoThread(results, self.cfg)
        self._redo_thread.log.connect(self._log)
        self._redo_thread.finished_all.connect(self._on_redo_done)
        self._redo_thread.finished.connect(self._redo_thread.deleteLater)
        self._redo_thread.start()

    def _on_redo_done(self, stats: dict) -> None:
        self._redo_thread = release_thread(self._redo_thread)
        self._leftover = []
        self._leftover_seen = set()
        self.btn_redo.setEnabled(False)
        self._set_busy(False)
        self._update_stats()

    def on_remove_selected(self) -> None:
        if self._busy:
            self._log("正在处理中，无法移除。", "warn")
            return
        rows = [i.row() for i in self.img_table.selectionModel().selectedRows()]
        if not rows:
            return
        self.img_model.remove_rows(rows)
        self._update_stats()

    def _open_selected(self, index) -> None:
        row = index.row()
        if row < 0:
            return
        p = self.img_model.items[row].path
        try:
            os.startfile(str(p))  # noqa
        except OSError as e:
            self._log(f"无法打开: {e}", "warn")

    def on_open_output(self) -> None:
        if not self._batch_dirs:
            self._log("本批次还没有输出目录。", "warn")
            return
        target = self._batch_dirs[-1]
        try:
            os.startfile(str(target))  # noqa
        except OSError as e:
            self._log(f"无法打开目录: {e}", "warn")

    def _table_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("移除选中", self.on_remove_selected)
        menu.addAction("打开所在目录", self._open_containing)
        failed = [it for it in self.img_model.items
                  if it.status is ImgStatus.FAILED]
        act_retry = menu.addAction("重试全部失败项")
        act_retry.setEnabled(bool(failed) and not self._busy)
        act = menu.exec(self.img_table.viewport().mapToGlobal(pos))
        if act == act_retry and failed:
            for it in failed:
                it.status = ImgStatus.PENDING
            self.img_model.refresh_all()
            self.on_start_all()

    def _open_containing(self) -> None:
        rows = [i.row() for i in self.img_table.selectionModel().selectedRows()]
        for r in rows[:1]:
            try:
                os.startfile(str(self.img_model.items[r].path.parent))  # noqa
            except OSError:
                pass

    def on_video_node_menu(self, source: bool, idx: int,
                           global_pos) -> None:
        """合并流图节点右键菜单 (由 MergeFlowView 调用)。

        source=True 为源视频节点; idx 为模型行号。
        """
        model = self.src_model if source else self.tgt_model
        if not (0 <= idx < len(model.items)):
            return
        menu = QMenu(self)
        act_remove = menu.addAction("移除选中") if source else None
        act_open = menu.addAction("打开所在文件夹")
        act = menu.exec(global_pos)
        if act is None:
            return
        if act is act_remove:
            if self._busy:
                self._log("正在处理中，无法移除。", "warn")
                return
            path = model.items[idx].path
            self._merge_excluded.add(path)
            model.remove_rows([idx])
            self.regroup()
            self._update_stats()
            self._log(f"已从合并列表移除 {path.name}"
                      "（不会删除文件；重新扫描也不会加回，重启程序可清空排除）",
                      "warn")
        elif act is act_open:
            it = model.items[idx]
            folder = it.path.parent if source else it.directory
            try:
                os.startfile(str(folder))  # noqa
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    #  模式 / 主题 / 设置
    # ------------------------------------------------------------------ #

    def on_mode_changed(self, btn_id: int) -> None:
        new_mode = "overwrite" if self.rb_overwrite.isChecked() \
            else "subdir"
        if new_mode == self.mode:
            return
        if new_mode == "overwrite":
            ret = QMessageBox.question(
                self, "切换到覆盖模式",
                "覆盖模式将在压缩完成后用结果覆盖原图。\n确定切换？",
                QMessageBox.Yes | QMessageBox.No)
            if ret != QMessageBox.Yes:
                self.rb_subdir.setChecked(True)
                return
        self.mode = new_mode
        self._log(f"输出模式: {'覆盖' if new_mode == 'overwrite' else '子目录'}"
                  "（对下一批次生效）", "warn" if new_mode == "overwrite" else "info")

    def on_toggle_theme(self) -> None:
        self._apply_theme("light" if self._theme == "dark" else "dark")

    def _load_theme(self) -> str:
        try:
            import json
            if STATE_FILE.exists():
                return json.loads(
                    STATE_FILE.read_text("utf-8")).get("theme", "dark")
        except Exception:
            pass
        return "dark"

    def _apply_theme(self, theme: str) -> None:
        self._theme = theme
        app = QApplication.instance()
        if app:
            app.setStyleSheet(qss_for(theme))
        self.btn_theme.setText("深色" if theme == "light" else "浅色")
        try:
            import json
            STATE_FILE.write_text(
                json.dumps({"theme": theme}), encoding="utf-8")
        except Exception:
            pass

    def on_settings(self) -> None:
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            self.cfg = dlg.new_cfg
            self.setWindowTitle(self.cfg.ui.title)
            self._log("配置已保存 — 下批次生效", "ok")

    # ------------------------------------------------------------------ #
    #  杂项
    # ------------------------------------------------------------------ #

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for b in (self.btn_start, self.btn_compress_only, self.btn_add_files,
                  self.btn_add_dir, self.btn_remove, self.btn_clear,
                  self.btn_merge, self.btn_redo, self.btn_settings):
            b.setEnabled(not busy)
        self.btn_stop.setEnabled(busy)
        if not busy:
            self.btn_stop.setText("■ 停止")

    def _log(self, text: str, level: str = "info") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        color = {"ok": "#97C459", "warn": "#EF9F27", "error": "#F09595"}.get(
            level)
        body = f"<span style='color:{color}'>{text}</span>" if color else text
        self.log_view.append(
            f"<span style='color:#7a7b7f'>[{ts}]</span> {body}")

    def _update_stats(self) -> None:
        items = self.img_model.items
        done = sum(1 for i in items if i.status is ImgStatus.DONE)
        skipped = sum(1 for i in items if i.status is ImgStatus.SKIPPED)
        failed = sum(1 for i in items if i.status is ImgStatus.FAILED)
        saved = sum(max(0, i.size - i.output_size) for i in items
                    if i.status is ImgStatus.DONE and i.output_size)
        vids = self.src_model.items
        merged_groups = sum(1 for t in self.tgt_model.items
                            if t.status == "已合并")
        mode_str = "覆盖" if self.mode == "overwrite" else "子目录"
        workers = self.cfg.parallel.max_workers or (os.cpu_count() or 4)
        parts = [
            f"图片 {len(items)}",
            f"成功 {done}" if done else None,
            f"跳过 {skipped}" if skipped else None,
            f"失败 {failed}" if failed else None,
            f"节省 {human_size(saved)}" if saved else None,
            f"| 视频 {len(vids)} · 目标 {len(self.tgt_model.items)}"
            f" · 已合并 {merged_groups}" if vids else None,
            f"| 模式: {mode_str} · {workers} 线程",
        ]
        self.stat_label.setText("  ".join(p for p in parts if p))

    def closeEvent(self, event) -> None:
        if self._busy:
            ret = QMessageBox.question(
                self, "正在处理中",
                "任务仍在进行，退出将无法停止后台线程。\n确定退出？",
                QMessageBox.Yes | QMessageBox.No)
            if ret != QMessageBox.Yes:
                event.ignore()
                return
        self._cancel.set()
        # 退出前给后台线程一点收尾时间: 解释器退出时销毁"仍在运行"的 QThread
        # 会命中 Qt 禁止的 UB (见 workers.release_thread 的崩溃说明)。
        for th in (self._scan_thread, self._probe_thread, self._refresh_thread,
                   self._compress_thread, self._merge_thread,
                   self._redo_thread):
            if th is not None and th.isRunning():
                th.wait(1500)
        event.accept()
