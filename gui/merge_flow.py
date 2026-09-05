"""合并流图: 源视频节点 → 曲线 → 目标文件节点 (思维导图式)。

取代原左右双表布局:
  - 左列 = 源视频节点, 右列 = 目标文件节点, 每个视频一个圆角卡片节点;
  - 节点间 S 形曲线连接, 状态样式: 待合并=红虚线 / 合并中=红虚线流动 /
    已合并=绿实线 / 跳过·超限=灰虚线 / 失败=红实线;
  - 原表格信息 (格式/编码/分辨率/大小/目标/状态…) 平时不展示,
    鼠标悬停节点时以富文本浮窗 (QToolTip) 显示;
  - 节点右键菜单: 源节点 [移除选中 / 打开所在文件夹], 目标节点 [打开所在文件夹]
    (由主窗口 on_video_node_menu 处理)。

数据来源直接读取主窗口的 src_model / tgt_model / _video_links,
regroup() 等状态变化后由主窗口调用 refresh()。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, \
    QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from .models import VideoItem, TargetItem, _fmt_size
from .style import C_ERR, C_OK, C_RUN, C_WARN

_GRAY = QColor("#8a8b90")

_FLOW_INTERVAL_MS = 33
_FLOW_STEP = 1.6

_NODE_H = 36.0
_SRC_W = 190.0
_TGT_W = 150.0
_PAD_X = 22.0
_PAD_Y = 22.0
_GAP_Y = 12.0

# 状态 → 节点边框/连线颜色
_STATUS_COLOR = {
    "待合并": QColor(C_ERR), "合并中": QColor(C_RUN),
    "已合并": QColor(C_OK), "跳过": QColor(C_WARN),
    "失败": QColor(C_ERR),
}


def _grayish(status: str) -> bool:
    return status in ("待探测", "已探测", "组数超限", "单组超限")


def _color_of(status: str) -> QColor:
    if _grayish(status):
        return _GRAY
    return _STATUS_COLOR.get(status, QColor(C_ERR))


def _line_pen(status: str, phase: float) -> QPen:
    """连线样式: 待合并=红虚线 / 合并中=红虚线流动 / 已合并=绿实线 /
    跳过·超限=灰虚线 / 失败=红实线。"""
    if status == "已合并":
        return QPen(QColor(C_OK), 2.2)
    if status == "失败":
        return QPen(QColor(C_ERR), 1.8)
    if _grayish(status) or status == "跳过":
        pen = QPen(_GRAY, 1.4)
        pen.setDashPattern([4.0, 3.5])
        return pen
    pen = QPen(QColor(C_ERR), 1.8)          # 待合并 / 合并中: 红虚线
    pen.setDashPattern([4.0, 3.5])
    if status == "合并中":
        pen.setDashOffset(phase)             # 自源向目标流动
    return pen


class MergeFlowView(QWidget):
    """合并流图画布。持主窗口引用以读取模型与链接数据。"""

    def __init__(self, main) -> None:
        super().__init__()
        self._main = main
        self.setObjectName("mergeFlow")
        self.setAttribute(Qt.WA_StyledBackground, True)   # QSS 背景
        self.setMouseTracking(True)                        # 悬停浮窗
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(_FLOW_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    # ---------------------------------------------------------------- #
    #  数据
    # ---------------------------------------------------------------- #
    def _src(self) -> List[VideoItem]:
        return self._main.src_model.items

    def _tgt(self) -> List[TargetItem]:
        return self._main.tgt_model.items

    def _links(self) -> List[Tuple[List[int], int]]:
        return getattr(self._main, "_video_links", [])

    def refresh(self) -> None:
        """模型/链接变化后调用: 自适应高度, 启停流动动画, 重绘。"""
        n = max(len(self._src()), len(self._tgt()))
        self.setMinimumHeight(int(max(170, _PAD_Y * 2
                                      + n * (_NODE_H + _GAP_Y))))
        active = any(t.status == "合并中" for t in self._tgt())
        if active and not self._timer.isActive():
            self._timer.start()
        elif not active and self._timer.isActive():
            self._timer.stop()
        self.update()

    def _tick(self) -> None:
        self._phase -= _FLOW_STEP     # 递减 → 虚线图案沿路径向前流动
        self.update()

    # ---------------------------------------------------------------- #
    #  几何
    # ---------------------------------------------------------------- #
    def _spread(self, n: int, height: float) -> List[float]:
        """n 个节点在高度内垂直均匀分布, 返回中心 y。"""
        if n <= 0:
            return []
        if n == 1:
            return [height / 2.0]
        top = _PAD_Y + _NODE_H / 2.0
        bottom = height - _PAD_Y - _NODE_H / 2.0
        step = (bottom - top) / (n - 1)
        return [top + i * step for i in range(n)]

    def _rects(self) -> Tuple[List[QRectF], List[QRectF]]:
        h = float(self.height())
        w = float(self.width())
        ys = self._spread(len(self._src()), h)
        yt = self._spread(len(self._tgt()), h)
        src = [QRectF(_PAD_X, y - _NODE_H / 2, _SRC_W, _NODE_H) for y in ys]
        tgt = [QRectF(w - _PAD_X - _TGT_W, y - _NODE_H / 2, _TGT_W, _NODE_H)
               for y in yt]
        return src, tgt

    def _node_at(self, pos: QPointF) -> Optional[Tuple[bool, int]]:
        """返回 (是否源节点, 行号); 未命中返回 None。"""
        src, tgt = self._rects()
        for i, r in enumerate(src):
            if r.contains(pos):
                return True, i
        for i, r in enumerate(tgt):
            if r.contains(pos):
                return False, i
        return None

    # ---------------------------------------------------------------- #
    #  绘制
    # ---------------------------------------------------------------- #
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        src, tgt = self._rects()
        src_items, tgt_items = self._src(), self._tgt()

        if not src_items and not tgt_items:
            painter.setPen(QColor("#8a8b90"))
            painter.drawText(self.rect(), Qt.AlignCenter,
                             "拖入包含视频的文件夹后，此处显示合并流图\n"
                             "(源视频 → 目标文件)")
            painter.end()
            return

        # --- 连线 (先画线, 节点覆盖其上) --- #
        for src_rows, tgt_row in self._links():
            if not (0 <= tgt_row < len(tgt)):
                continue
            status = tgt_items[tgt_row].status if tgt_row < len(tgt_items) \
                else ""
            r_t = tgt[tgt_row]
            x0 = _PAD_X + _SRC_W
            x1 = r_t.left()
            y1 = r_t.center().y()
            for sr in src_rows:
                if not (0 <= sr < len(src)):
                    continue
                y0 = src[sr].center().y()
                painter.setPen(_line_pen(status, self._phase))
                painter.setBrush(Qt.NoBrush)
                path = QPainterPath(QPointF(x0, y0))
                mid = (x1 - x0) * 0.5
                path.cubicTo(QPointF(x0 + mid, y0), QPointF(x1 - mid, y1),
                             QPointF(x1 - 5.0, y1))
                painter.drawPath(path)
                # 目标端箭头
                painter.setBrush(_color_of(status) if status != "跳过"
                                 else _GRAY)
                painter.setPen(Qt.NoPen)
                painter.drawPolygon(QPolygonF([
                    QPointF(x1 - 1.0, y1),
                    QPointF(x1 - 7.0, y1 - 3.4),
                    QPointF(x1 - 7.0, y1 + 3.4)]))
                # 源端小圆点
                painter.setBrush(painter.pen().brush())
                painter.setPen(Qt.NoPen)
                painter.drawEllipse(QPointF(x0 + 1.5, y0), 2.6, 2.6)

        # --- 源节点 --- #
        for i, r in enumerate(src):
            self._draw_node(painter, r, src_items[i].path.name,
                            src_items[i].status, align_right=False)

        # --- 目标节点 --- #
        for i, r in enumerate(tgt):
            if i < len(tgt_items):
                self._draw_node(painter, r, tgt_items[i].name,
                                tgt_items[i].status, align_right=True)

        painter.end()

    def _draw_node(self, painter: QPainter, r: QRectF, name: str,
                   status: str, align_right: bool) -> None:
        color = _color_of(status)
        painter.setPen(QPen(color, 1.6))
        painter.setBrush(QColor(255, 255, 255, 14))
        painter.drawRoundedRect(r, 8.0, 8.0)
        # 文件名 (超出省略); 状态由边框颜色表达, 不再画状态圆点
        text = name
        avail = r.width() - 24.0
        fm = QFontMetrics(painter.font())
        if fm.horizontalAdvance(text) > avail:
            while text and fm.horizontalAdvance(text + "…") > avail:
                text = text[:-1]
            text += "…"
        painter.setPen(QColor("#e6e6e6"))
        painter.drawText(
            QRectF(r.left() + 12, r.top(), r.width() - 24, r.height()),
            Qt.AlignVCenter | (Qt.AlignRight if align_right else Qt.AlignLeft),
            text)

    # ---------------------------------------------------------------- #
    #  交互
    # ---------------------------------------------------------------- #
    def _tooltip_html(self, source: bool, idx: int) -> str:
        if source:
            it = self._src()[idx]
            fmt = it.path.suffix.lstrip(".").upper() or "?"
            res = (f"{it.width}×{it.height}" if it.probe_ok and it.width
                   else ("—" if it.probe_ok else "未探测"))
            codec = it.vcodec or ("—" if it.probe_ok else "未探测")
            return (f"<b>{it.path.name}</b><br>"
                    f"格式: {fmt} · 编码: {codec}<br>"
                    f"分辨率: {res} · 大小: {_fmt_size(it.size)}<br>"
                    f"目标: {it.target or '—'} · 状态: {it.status}<br>"
                    f"<span style='color:#9a9b9f'>{it.path}</span>")
        it = self._tgt()[idx]
        saving = "—"
        if it.output_size and it.orig_total:
            saving = f"-{(1 - it.output_size / it.orig_total) * 100:.0f}%"
        return (f"<b>{it.name}</b><br>"
                f"视频数: {it.video_count} · 原始合计: "
                f"{_fmt_size(it.orig_total)}<br>"
                f"目标大小: {_fmt_size(it.output_size) if it.output_size else '—'}"
                f" · 节省率: {saving}<br>"
                f"状态: {it.status}<br>"
                f"<span style='color:#9a9b9f'>{it.directory / it.name}</span>")

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        hit = self._node_at(event.position())
        if hit is None:
            self.setToolTip("")
            event.ignore()
            return
        source, idx = hit
        items = self._src() if source else self._tgt()
        if not (0 <= idx < len(items)):
            return
        from PySide6.QtWidgets import QToolTip
        QToolTip.showText(event.globalPosition().toPoint(),
                          self._tooltip_html(source, idx), self)

    def leaveEvent(self, event) -> None:  # noqa: N802
        from PySide6.QtWidgets import QToolTip
        QToolTip.hideText()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.RightButton:
            hit = self._node_at(event.position())
            if hit is not None:
                source, idx = hit
                self._main.on_video_node_menu(
                    source, idx, event.globalPosition().toPoint())
                return
        super().mousePressEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().resizeEvent(event)
