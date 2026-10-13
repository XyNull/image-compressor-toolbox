"""设置对话框 — 图形化编辑 config.toml 全部参数。

保存时逐行替换值 (保留行尾注释与文件其余内容), 并重新加载配置。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

import config as config_mod
from config import Config

_VM_ENCODERS = ["av1_qsv", "libsvtav1", "libaom-av1"]


def fmt_toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, (list, tuple)):
        if v and isinstance(v[0], str):
            return "[" + ", ".join(f'"{x}"' for x in v) + "]"
        return "[" + ", ".join(str(x) for x in v) + "]"
    return str(v)


def save_config_values(updates: Dict[str, Dict[str, Any]]) -> List[str]:
    """把 {section: {key: value}} 写回 config.toml, 保留注释。返回警告。"""
    path = Path(config_mod.CONFIG_PATH)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    warnings: List[str] = []
    pending: Dict[str, Dict[str, Any]] = {
        s: dict(kv) for s, kv in updates.items()}
    current: Optional[str] = None

    pat = re.compile(r"^(\s*[^=#\n=]+=\s*)(.*?)(\s*#.*)?$")
    for i, line in enumerate(lines):
        stripped = line.strip()
        eol = "\n" if line.endswith("\n") else ""
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
            continue
        if current and current in pending and "=" in stripped \
                and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in pending[current]:
                val = pending[current].pop(key)
                m = pat.match(line.rstrip("\n"))
                if m:
                    lines[i] = (f"{m.group(1)}{fmt_toml_value(val)}"
                                f"{m.group(3) or ''}{eol}")

    # 缺失的 key 追加到对应 section 末尾
    for section, kvs in pending.items():
        if not kvs:
            continue
        sec_idx = None
        for i, line in enumerate(lines):
            s = line.strip()
            if s == f"[{section}]":
                sec_idx = i
        if sec_idx is None:
            warnings.append(f"未找到 [{section}] 段, {list(kvs)} 未写入")
            continue
        # 找该段最后一行 (下一个 [ 或文件尾)
        end = len(lines)
        for j in range(sec_idx + 1, len(lines)):
            if lines[j].strip().startswith("["):
                end = j
                break
        insert = "".join(f"{k} = {fmt_toml_value(v)}\n"
                         for k, v in kvs.items())
        lines.insert(end, insert)

    path.write_text("".join(lines), encoding="utf-8")
    return warnings


class _Row:
    """设置页构建辅助: 管理 section/key → 控件映射。"""

    def __init__(self, store: Dict[str, Dict[str, Any]]) -> None:
        self.store = store

    def _set(self, section: str, key: str, value: Any) -> None:
        self.store.setdefault(section, {})[key] = value


class SettingsDialog(QDialog):
    """config.toml 图形化编辑。"""

    def __init__(self, cfg: Config, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(520, 620)
        self.cfg = cfg
        self.updates: Dict[str, Dict[str, Any]] = {}
        self.row = _Row(self.updates)

        tabs = QTabWidget()
        tabs.addTab(self._page_compression(), "压缩")
        tabs.addTab(self._page_output(), "输出与并行")
        tabs.addTab(self._page_video(), "动图转MP4")
        tabs.addTab(self._page_merge(), "视频合并")
        tabs.addTab(self._page_misc(), "界面与日志")

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("保存")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(btns)

    # --- 页构建辅助 ---------------------------------------------------- #
    def _scroll(self, inner: QWidget) -> QWidget:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setWidget(inner)
        return sa

    def _get(self, section: str, key: str):
        return getattr(self.cfg, section)

    def _val(self, section: str, key: str):
        return getattr(getattr(self.cfg, section), key)

    def _add_spin(self, form, section, key, label, lo, hi, step=1,
                  suffix="", tip=""):
        cur = self._val(section, key)
        if isinstance(cur, float):
            w = QDoubleSpinBox(); w.setDecimals(2)
            w.setSingleStep(step if step >= 0.01 else 0.1)
        else:
            w = QSpinBox()
        w.setRange(lo, hi)
        w.setValue(cur)
        if suffix:
            w.setSuffix(" " + suffix)
        if tip:
            w.setToolTip(tip)
        w.valueChanged.connect(
            lambda v: self.row._set(section, key, float(v)
                                    if isinstance(cur, float) else int(v)))
        form.addRow(label, w)
        return w

    def _add_bool(self, form, section, key, label, tip=""):
        w = QCheckBox()
        w.setChecked(bool(self._val(section, key)))
        if tip:
            w.setToolTip(tip)
        w.toggled.connect(lambda v: self.row._set(section, key, bool(v)))
        form.addRow(label, w)

    def _add_str(self, form, section, key, label, tip=""):
        w = QLineEdit(str(self._val(section, key)))
        if tip:
            w.setToolTip(tip)
        w.textChanged.connect(
            lambda v: self.row._set(section, key, v))
        form.addRow(label, w)

    def _add_combo(self, form, section, key, label, opts, labels=None):
        w = QComboBox()
        labels = labels or opts
        cur = str(self._val(section, key))
        w.addItems(labels)
        if cur in opts:
            w.setCurrentIndex(opts.index(cur))
        w.currentIndexChanged.connect(
            lambda i: self.row._set(section, key, opts[i]))
        form.addRow(label, w)

    def _add_unit_spin(self, form, section, key, label, units, default_unit,
                       lo, hi, tip=""):
        """带单位下拉的数值条目: [数字输入框][单位下拉]。

        units: {显示名: 换算因子(相对 config 基准单位)}, 如
               {"毫秒": 0.001, "秒": 1, "分钟": 60}。
        界面显示值 = 基准值 / 当前单位因子; 保存时换算回基准单位写回 config。
        """
        cur = self._val(section, key)
        is_float = isinstance(cur, float)
        if is_float:
            spin = QDoubleSpinBox()
            spin.setDecimals(2)
        else:
            spin = QSpinBox()
        combo = QComboBox()
        combo.addItems(list(units))
        combo.setCurrentText(default_unit)
        state = {"factor": units[default_unit]}

        def to_base(v: float):
            val = v * state["factor"]
            return val if is_float else int(round(val))

        def apply_range() -> None:
            f = state["factor"]
            spin.setRange(lo / f, hi / f)

        def on_spin(v: float) -> None:
            self.row._set(section, key, to_base(v))

        def on_unit(text: str) -> None:
            base = to_base(spin.value())       # 记住当前基准值
            state["factor"] = units[text]
            apply_range()
            spin.setValue(base / state["factor"])

        spin.valueChanged.connect(on_spin)
        combo.currentTextChanged.connect(on_unit)
        apply_range()
        spin.setValue(cur / state["factor"])
        if tip:
            spin.setToolTip(tip)
        h = QHBoxLayout()
        h.addWidget(spin, 1)
        h.addWidget(combo)
        form.addRow(label, h)

    def _add_rgb(self, form, section, key, label):
        cur = list(self._val(section, key)) or [255, 255, 255]
        btn = QPushButton("#%02X%02X%02X" % tuple(cur))
        preview = QLabel()
        preview.setFixedSize(18, 18)
        col = QColor(*cur)
        preview.setStyleSheet(
            f"background:{col.name()};border:1px solid #888;border-radius:3px;")

        def pick():
            c = QColorDialog.getColor(col, self, "透明背景填充色")
            if c.isValid():
                rgb = [c.red(), c.green(), c.blue()]
                self.row._set(section, key, rgb)
                btn.setText("#%02X%02X%02X" % tuple(rgb))
                preview.setStyleSheet(
                    f"background:{c.name()};border:1px solid #888;"
                    "border-radius:3px;")
        btn.clicked.connect(pick)
        h = QHBoxLayout()
        h.addWidget(preview)
        h.addWidget(btn)
        h.addStretch(1)
        form.addRow(label, h)

    def _add_priority(self, form):
        cur = list(self._val("video", "encoder_priority"))
        boxes: List[QCheckBox] = []

        def sync():
            order = ["nvidia", "intel_qsv", "cpu"]
            sel = [n for i, n in enumerate(order)
                   if i < len(boxes) and boxes[i].isChecked()]
            self.row._set("video", "encoder_priority", sel)

        for name, lab in zip(["nvidia", "intel_qsv", "cpu"],
                             ["NVIDIA (NVENC)", "Intel (QSV)", "CPU (libx265)"]):
            cb = QCheckBox(lab)
            cb.setChecked(name in cur)
            cb.toggled.connect(sync)
            boxes.append(cb)
            form.addRow("", cb)

    # --- 各页 ---------------------------------------------------------- #
    def _page_compression(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setLabelAlignment(Qt.AlignRight)
        self._add_spin(form, "compression", "quality", "JPEG 质量", 0, 100,
                       tip="0-100, 越高越清晰体积越大")
        self._add_unit_spin(form, "compression", "target_size_mb",
                            "目标体积上限", {"KB": 1 / 1024, "MB": 1.0,
                                             "GB": 1024.0}, "MB",
                            0.1, 10000,
                            tip="超过此体积则循环缩小")
        self._add_combo(form, "compression", "output_format", "输出格式",
                        ["jpeg", "png", "webp"],
                        ["jpeg (完整验证)", "png (实验)", "webp (实验)"])
        self._add_rgb(form, "compression", "background_color", "透明填充底色")
        self._add_bool(form, "compression", "progressive", "渐进式 JPEG")
        self._add_bool(form, "compression", "optimize", "优化 Huffman 表")
        self._add_spin(form, "compression", "scale_step", "缩放步长",
                       0.05, 0.99,
                       tip="预算算法接管, 一般无需修改")
        self._add_spin(form, "compression", "max_scale_iterations",
                       "缩放轮数上限", 1, 50,
                       tip="预算算法下通常 1-2 次即达标")
        return self._scroll(w)

    def _page_output(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self._add_str(form, "output", "subdir_name", "输出子目录名",
                      tip="子目录模式的输出目录名")
        self._add_combo(form, "output", "default_mode", "默认模式",
                        ["subdir", "overwrite"], ["子目录", "覆盖"])
        self._add_spin(form, "parallel", "max_workers", "压缩线程数", 0, 64,
                       tip="0 = 自动 (CPU 核心数)")
        self._add_unit_spin(form, "io", "small_file_threshold_mb",
                            "小文件预读阈值", {"KB": 1 / 1024, "MB": 1.0,
                                               "GB": 1024.0}, "MB",
                            0, 1000,
                            tip="小于此体积一次读入内存, 减少 SMB 往返")
        self._add_spin(form, "io", "scan_workers", "目录扫描线程数", 1, 64,
                       tip="SMB/网络路径建议 8-16")
        self._add_spin(form, "overwrite", "retry_count", "覆盖重试次数", 0, 10,
                       tip="覆盖/删除失败后的重试次数")
        return self._scroll(w)

    def _page_video(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self._add_spin(form, "video", "crf", "H.265 质量 (CRF)", 0, 51,
                       tip="越低越好, 28 为均衡")
        self._add_priority(form)
        self._add_unit_spin(form, "video", "timeout", "单文件超时",
                            {"毫秒": 0.001, "秒": 1, "分钟": 60}, "秒",
                            30, 7200)
        self._add_str(form, "video", "extension", "输出扩展名")
        return self._scroll(w)

    def _page_merge(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self._add_combo(form, "video_merge", "encoder", "首选编码器",
                        _VM_ENCODERS,
                        ["av1_qsv (Intel 硬件)", "libsvtav1 (CPU)",
                         "libaom-av1 (CPU)"])
        self._add_combo(form, "video_merge", "fallback_encoder", "回退编码器",
                        _VM_ENCODERS,
                        ["libsvtav1 (CPU)", "libaom-av1 (CPU)",
                         "av1_qsv (Intel 硬件)"])
        self._add_spin(form, "video_merge", "quality", "AV1 质量", 0, 63,
                       tip="qsv 用 global_quality, CPU 用 crf")
        self._add_combo(form, "video_merge", "qsv_preset", "QSV 预设",
                        ["veryslow", "slower", "slow", "medium", "fast",
                         "faster", "veryfast"],
                        ["极慢 (文件最小)", "较慢", "慢", "中等",
                         "快", "较快", "极快 (文件更大·细节保留多)"])
        self._add_combo(form, "video_merge", "qsv_profile", "QSV Profile",
                        ["main", "high"], ["main", "high"])
        self._add_unit_spin(form, "video_merge", "keyframe_interval",
                            "关键帧间隔",
                            {"毫秒": 0.001, "秒": 1, "分钟": 60}, "秒",
                            1, 10,
                            tip="关键帧越密 seek 越快, 体积略增")
        self._add_str(form, "video_merge", "audio_codec", "音频编码器")
        self._add_str(form, "video_merge", "audio_profile", "音频规格")
        self._add_str(form, "video_merge", "audio_bitrate", "音频码率")
        self._add_bool(form, "video_merge", "keep_channels", "保持原声道数")
        self._add_bool(form, "video_merge", "remove_subtitles", "移除字幕")
        self._add_spin(form, "video_merge", "tolerance_px", "分辨率容差",
                       0, 500, suffix="px",
                       tip="宽高差 ≤ 此值视为同分辨率")
        self._add_spin(form, "video_merge", "max_videos_per_group",
                       "单组视频上限", 2, 1000,
                       tip="超过此值跳过该组")
        self._add_spin(form, "video_merge", "max_output_files",
                       "目标文件数上限", 1, 100,
                       tip="分组数超过此值整目录跳过")
        self._add_str(form, "video_merge", "output_prefix", "输出文件名前缀",
                      tip="V1.mp4 / V2.mp4")
        self._add_str(form, "video_merge", "output_extension", "输出扩展名")
        self._add_bool(form, "video_merge", "overwrite_existing",
                       "目标已存在时覆盖",
                       tip="否则跳过该组")
        self._add_bool(form, "video_merge", "mixed_codec_merge",
                       "编码不一致时统一转码合并",
                       tip="组内视频编码不同 (如 h264+hevc) 时: 逐源统一分辨率/"
                           "帧率后拼接并转 AV1 (只编码一次), 而不是跳过该组; "
                           "复制流拼接失败的组也退回这条路径")
        self._add_unit_spin(form, "video_merge", "timeout", "合并超时",
                            {"秒": 1, "分钟": 60}, "分钟", 1, 1440)
        self._add_spin(form, "video_merge", "concat_workers",
                       "视频合并并发数", 1, 32,
                       tip="阶段1 复制流拼接同时进行的组数 (默认 10)")
        self._add_spin(form, "video_merge", "transcode_workers",
                       "转AV1并发数", 1, 8,
                       tip="阶段2 AV1 转码同时进行的组数; "
                           "QSV 硬编建议 1-2 (默认 2)")
        form.addRow(self._hr(w))
        self._add_bool(form, "video_merge", "concat_genpts",
                       "genpts 补全时间戳",
                       tip="concat 输入加 -fflags +genpts")
        self._add_bool(form, "video_merge", "normalize_pts",
                       "转码前重排视频时间戳",
                       tip="setpts=N/fps/TB, 修复时长虚增+冻结帧"
                           "(个别 VFR 源可能轻微变速, 可关闭)")
        self._add_bool(form, "video_merge", "duration_check",
                       "合并后校验时长",
                       tip="成片时长 vs 源视频时长之和, 偏差过大标红色感叹号")
        self._add_spin(form, "video_merge", "duration_tolerance_pct",
                       "时长偏差容忍", 0.1, 50.0, suffix="%",
                       tip="相对百分比; 与下方绝对秒数取较大者")
        self._add_spin(form, "video_merge", "duration_tolerance_min_s",
                       "时长偏差容忍下限", 0.1, 60.0, suffix="秒",
                       tip="短视频用固定秒数兜底")
        self._add_bool(form, "video_merge", "duration_repair",
                       "时长异常时自动修复",
                       tip="从 concat 列表重转码(setpts 重排); 仍异常才标红")
        return self._scroll(w)

    @staticmethod
    def _hr(parent: QWidget) -> QFrame:
        line = QFrame(parent)
        line.setFrameShape(QFrame.HLine)
        line.setObjectName("toolSep")
        return line

    def _page_misc(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self._add_str(form, "ui", "title", "窗口标题")
        self._add_str(form, "logging", "log_dir", "日志目录")
        return self._scroll(w)

    # --- 保存 ---------------------------------------------------------- #
    def _save(self) -> None:
        if not self.updates:
            self.accept()
            return
        warnings = save_config_values(self.updates)
        new_cfg = config_mod.load_config()
        self.new_cfg = new_cfg
        if warnings:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "部分配置未写入", "\n".join(warnings))
        self.accept()
