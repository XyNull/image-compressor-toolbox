"""数据模型: 图片队列表 / 视频原始表 / 视频目标表 (QAbstractTableModel)。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from .style import C_ERR, C_OK, C_RUN, C_WARN


# --------------------------------------------------------------------- #
#  图片队列
# --------------------------------------------------------------------- #

class ImgStatus(Enum):
    PENDING = "待处理"
    COMPRESSING = "压缩中"
    DONE = "成功"
    SKIPPED = "跳过"
    FAILED = "失败"


STATUS_COLOR = {
    ImgStatus.PENDING: None,          # 灰 (muted)
    ImgStatus.COMPRESSING: C_RUN,
    ImgStatus.DONE: C_OK,
    ImgStatus.SKIPPED: C_WARN,
    ImgStatus.FAILED: C_ERR,
}

MUTED = "#9a9b9f"


@dataclass
class QueueItem:
    path: Path
    root: Optional[Path] = None
    fmt: Optional[str] = None
    size: int = 0
    status: ImgStatus = ImgStatus.PENDING
    output_size: int = 0
    output_path: Optional[Path] = None
    error: str = ""
    scale_count: int = 0
    skip_reason: str = ""
    encoder: str = ""


def _fmt_size(n: int) -> str:
    if n <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


class ImageQueueModel(QAbstractTableModel):
    """列: # / 文件名 / 格式 / 原始 / 压缩后 / 节省 / 状态。"""

    HEADERS = ["#", "文件名", "格式", "原始", "压缩后", "节省", "状态"]

    def __init__(self) -> None:
        super().__init__()
        self.items: List[QueueItem] = []

    # --- Qt plumbing -------------------------------------------------- #
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, sec, orient, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orient == Qt.Horizontal:
            return self.HEADERS[sec]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        it = self.items[index.row()]
        c = index.column()
        if role == Qt.DisplayRole:
            if c == 0:
                return str(index.row() + 1)
            if c == 1:
                return it.path.name
            if c == 2:
                return (it.fmt or "?").upper()
            if c == 3:
                return _fmt_size(it.size)
            if c == 4:
                return _fmt_size(it.output_size) if it.output_size else "—"
            if c == 5:
                if it.output_size and it.size:
                    pct = (1 - it.output_size / it.size) * 100
                    return f"-{pct:.0f}%"
                return "—"
            if c == 6:
                s = it.status.value
                if it.status is ImgStatus.DONE and it.encoder:
                    s = f"→{it.encoder}"
                elif it.status is ImgStatus.SKIPPED and it.skip_reason == "larger":
                    s = "跳过(更大)"
                elif it.status is ImgStatus.SKIPPED and it.skip_reason == "animated":
                    s = "跳过(动图)"
                elif it.status is ImgStatus.FAILED and it.error:
                    s = "失败"
                return s
        if role == Qt.ForegroundRole:
            if c == 5 and it.output_size and it.size:
                return QColor(C_OK)
            if c == 6:
                col = STATUS_COLOR.get(it.status)
                return QColor(col) if col else QColor(MUTED)
        if role == Qt.ToolTipRole:
            tip = f"<b>{it.path}</b><br>"
            if it.status is ImgStatus.DONE and it.scale_count:
                tip += f"缩放 {it.scale_count} 次<br>"
            if it.error:
                tip += f"<span style='color:{C_ERR}'>{it.error}</span><br>"
            tip += f"{_fmt_size(it.size)} → {_fmt_size(it.output_size)}"
            return tip
        return None

    # --- 编辑操作 ----------------------------------------------------- #
    def reset_items(self, items: List[QueueItem]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def update_row(self, row: int) -> None:
        if 0 <= row < len(self.items):
            self.dataChanged.emit(
                self.index(row, 0), self.index(row, len(self.HEADERS) - 1))

    def refresh_all(self) -> None:
        if self.items:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self.items) - 1, len(self.HEADERS) - 1))

    def remove_rows(self, rows: List[int]) -> None:
        for r in sorted(set(rows), reverse=True):
            if 0 <= r < len(self.items):
                self.beginRemoveRows(QModelIndex(), r, r)
                del self.items[r]
                self.endRemoveRows()

    def find_row(self, path: Path) -> int:
        for i, it in enumerate(self.items):
            if it.path == path:
                return i
        return -1


# --------------------------------------------------------------------- #
#  视频: 原始表 (左) / 目标表 (右)
# --------------------------------------------------------------------- #

@dataclass
class VideoItem:
    """左侧原始视频行。"""
    path: Path
    size: int = 0
    vcodec: str = ""          # 探测: 编码 (h264/hevc/...)
    width: int = 0
    height: int = 0
    probe_ok: bool = False
    probe_error: str = ""
    target: str = ""          # "V1.mp4" (分组后)
    status: str = "待探测"     # 待探测/待合并/合并中/已合并/跳过/失败


@dataclass
class TargetItem:
    """右侧目标文件行 (一组原始视频 → 一个输出文件)。"""
    directory: Path
    name: str                 # V1.mp4
    video_count: int = 0
    orig_total: int = 0       # 组内原始大小之和 (节省率分母)
    output_size: int = 0      # 合并后实际大小
    output_path: Optional[Path] = None
    status: str = "待合并"


class VideoSourceModel(QAbstractTableModel):
    """列: # / 文件名 / 格式 / 编码 / 分辨率 / 原始大小 / 目标文件 / 状态。"""

    HEADERS = ["#", "文件名", "格式", "编码", "分辨率", "原始大小", "目标文件", "状态"]

    def __init__(self) -> None:
        super().__init__()
        self.items: List[VideoItem] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, sec, orient, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orient == Qt.Horizontal:
            return self.HEADERS[sec]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        it = self.items[index.row()]
        c = index.column()
        if role == Qt.DisplayRole:
            if c == 0:
                return str(index.row() + 1)
            if c == 1:
                return it.path.name
            if c == 2:
                return it.path.suffix.lstrip(".").upper() or "?"
            if c == 3:
                return it.vcodec or ("?" if not it.probe_ok else "—")
            if c == 4:
                if it.probe_ok and it.width:
                    return f"{it.width}×{it.height}"
                return "—" if it.probe_ok else "?"
            if c == 5:
                return _fmt_size(it.size)
            if c == 6:
                return it.target or "—"
            if c == 7:
                return it.status
        if role == Qt.ForegroundRole:
            if c == 7:
                color = {"待探测": MUTED, "待合并": None, "合并中": C_RUN,
                         "已合并": C_OK, "跳过": C_WARN, "失败": C_ERR,
                         "已探测": MUTED}.get(it.status)
                return QColor(color) if color else None
        if role == Qt.ToolTipRole:
            return str(it.path)
        return None

    def reset_items(self, items: List[VideoItem]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def update_row(self, row: int) -> None:
        if 0 <= row < len(self.items):
            self.dataChanged.emit(
                self.index(row, 0), self.index(row, len(self.HEADERS) - 1))

    def refresh_all(self) -> None:
        if self.items:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self.items) - 1, len(self.HEADERS) - 1))

    def find_row(self, path: Path) -> int:
        for i, it in enumerate(self.items):
            if it.path == path:
                return i
        return -1

    def remove_rows(self, rows: List[int]) -> None:
        """从合并队列移除视频行 (仅移出列表, 不删除文件)。"""
        for r in sorted(set(rows), reverse=True):
            if 0 <= r < len(self.items):
                self.beginRemoveRows(QModelIndex(), r, r)
                del self.items[r]
                self.endRemoveRows()


class VideoTargetModel(QAbstractTableModel):
    """列: # / 目标文件 / 视频数 / 原始合计 / 目标大小 / 节省率 / 状态。"""

    HEADERS = ["#", "目标文件", "视频数", "原始合计", "目标大小", "节省率", "状态"]

    def __init__(self) -> None:
        super().__init__()
        self.items: List[TargetItem] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, sec, orient, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orient == Qt.Horizontal:
            return self.HEADERS[sec]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        it = self.items[index.row()]
        c = index.column()
        if role == Qt.DisplayRole:
            if c == 0:
                return str(index.row() + 1)
            if c == 1:
                return it.name
            if c == 2:
                return str(it.video_count)
            if c == 3:
                return _fmt_size(it.orig_total)
            if c == 4:
                return _fmt_size(it.output_size) if it.output_size else "—"
            if c == 5:
                # 节省率 = 1 - 目标大小 / 该组原始大小之和
                if it.output_size and it.orig_total:
                    pct = (1 - it.output_size / it.orig_total) * 100
                    return f"-{pct:.0f}%"
                return "—"
            if c == 6:
                return it.status
        if role == Qt.ForegroundRole:
            if c == 5 and it.output_size and it.orig_total:
                return QColor(C_OK)
            if c == 6:
                color = {"待合并": None, "合并中": C_RUN, "已合并": C_OK,
                         "跳过": C_WARN, "失败": C_ERR}.get(it.status)
                return QColor(color) if color else None
        if role == Qt.ToolTipRole:
            return str(it.directory / it.name)
        return None

    def reset_items(self, items: List[TargetItem]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def refresh_all(self) -> None:
        if self.items:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self.items) - 1, len(self.HEADERS) - 1))

    def find_row(self, directory: Path, name: str) -> int:
        for i, it in enumerate(self.items):
            if it.directory == directory and it.name == name:
                return i
        return -1
