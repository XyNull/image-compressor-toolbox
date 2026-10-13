"""合并流图: 目标节点常驻右列, 单击目标展开/收起其源视频链路 (思维导图式)。

交互设计 (2026-09-06 改版):
  - 平时仅显示右侧目标视频节点 (两行文本: 文件名 + 所在目录末级目录名,
    用于区分不同目录下的同名 V*.mp4; 同一目录的目标天然相邻排列);
  - 左键单击目标节点 → 左列展开该目标的源视频链路 (同一时间只展开一个,
    打开新的自动关闭旧的); 再次单击同一目标或单击空白处收起;
  - 节点左侧小三角指示展开状态: ▸ 收起 / ▼ 已展开;
  - "时长异常" 目标节点左侧显示红色感叹号徽标 (合并成功但成片时长与
    源视频总时长偏差过大), 悬停浮窗给出期望/实际时长;
  - 悬停节点显示富文本浮窗 (QToolTip); 右键菜单: 源节点 [移除选中 /
    打开所在文件夹], 目标节点 [打开所在文件夹] (由主窗口 on_video_node_menu
    处理);
  - 连线样式: 待合并=红虚线 / 合并中=红虚线流动 / 已合并=绿实线 /
    跳过·超限=灰虚线 / 失败=红实线 (时长异常仍为绿实线, 警告由节点徽标表达)。

数据来源直接读取主窗口的 src_model / tgt_model / _video_links,
regroup() 等状态变化后由主窗口调用 refresh()。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, \
    QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from compressor import fmt_saving_pct, is_larger
from .models import VideoItem, TargetItem, _fmt_size
from .style import C_ERR, C_OK, C_RUN, C_WARN
from video_merge import fmt_duration

_GRAY = QColor("#8a8b90")
_MUTED = QColor("#9a9b9f")

_FLOW_INTERVAL_MS = 33
_FLOW_STEP = 1.6

_NODE_H = 36.0            # 源节点高
_TGT_H = 44.0             # 目标节点高 (两行文本)
_SRC_W = 190.0
_TGT_W = 200.0
_PAD_X = 22.0
_PAD_Y = 22.0
_GAP_Y = 12.0

# 状态 → 节点边框/连线颜色
_STATUS_COLOR = {
    "待合并": QColor(C_ERR), "合并中": QColor(C_RUN),
    "已合并": QColor(C_OK), "跳过": QColor(C_WARN),
    "失败": QColor(C_ERR), "时长异常": QColor(C_ERR),
}


def _grayish(status: str) -> bool:
    return status in ("待探测", "已探测", "组数超限", "单组超限", "已合并产物")


def _color_of(status: str) -> QColor:
    if _grayish(status):
        return _GRAY
    return _STATUS_COLOR.get(status, QColor(C_ERR))


def _line_pen(status: str, phase: float) -> QPen:
    """连线样式: 待合并=红虚线 / 合并中=红虚线流动 / 已合并=绿实线 /
    跳过·超限=灰虚线 / 失败=红实线。"""
    if status in ("已合并", "时长异常"):   # 合并成功; 警告由节点徽标表达
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


def _elide(text: str, fm: QFontMetrics, width: float) -> str:
    if fm.horizontalAdvance(text) <= width:
        return text
    out = text
    while out and fm.horizontalAdvance(out + "…") > width:
        out = out[:-1]
    return out + "…"


class MergeFlowView(QWidget):
    """合并流图画布。持主窗口引用以读取模型与链接数据。"""

    def __init__(self, main) -> None:
        super().__init__()
        self._main = main
        self.setObjectName("mergeFlow")
        self.setAttribute(Qt.WA_StyledBackground, True)   # QSS 背景
        self.setMouseTracking(True)                        # 悬停浮窗
        self._phase = 0.0
        self._expanded_key: Optional[Tuple[object, str]] = None  # (目录, 文件名)
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

    def _find_link(self, key: Tuple[object, str]
                   ) -> Optional[Tuple[List[int], int]]:
        """按 (目录, 文件名) 查找目标对应的链接; 不存在返回 None。"""
        for src_rows, tgt_row in self._links():
            items = self._tgt()
            if 0 <= tgt_row < len(items):
                t = items[tgt_row]
                if (t.directory, t.name) == key:
                    return src_rows, tgt_row
        return None

    def _expanded(self) -> Optional[Tuple[TargetItem, List[int], int]]:
        """当前展开的目标 (item, 源模型行列表, 目标行号); 未展开为 None。"""
        if not self._expanded_key:
            return None
        link = self._find_link(self._expanded_key)
        if link is None:                      # regroup 后目标已消失
            self._expanded_key = None
            return None
        src_rows, tgt_row = link
        return self._tgt()[tgt_row], src_rows, tgt_row

    def toggle_expand(self, t: TargetItem) -> None:
        """左键单击目标节点: 展开其来源链路 (互斥) 或收起。"""
        key = (t.directory, t.name)
        self._expanded_key = None if self._expanded_key == key else key
        self._relayout()

    def collapse(self) -> None:
        if self._expanded_key is not None:
            self._expanded_key = None
            self._relayout()

    def auto_follow(self, directory, name: str) -> None:
        """合并进行中自动跟随 (修复: 连线仅在展开的链路上绘制, 合并开始后
        若无展开则流动动画不可见)。

        规则: 当前无展开、或展开的目标不在"合并中"状态时, 切换展开该任务
        的链路; 用户正盯着某个"合并中"链路时不抢焦点 (点击目标可"钉住")。
        """
        exp = self._expanded()
        if exp is not None and exp[0].status == "合并中":
            return
        for t in self._tgt():
            if t.directory == directory and t.name == name:
                key = (t.directory, t.name)
                if self._expanded_key != key:
                    self._expanded_key = key
                    self.refresh()          # 含动画计时器启停 + 重绘
                return

    def _relayout(self) -> None:
        self._update_min_height()
        self.update()

    def refresh(self) -> None:
        """模型/链接变化后调用: 自适应高度, 启停流动动画, 重绘。"""
        self._expanded()          # 顺手清掉失效的展开键
        self._update_min_height()
        active = any(t.status == "合并中" for t in self._tgt())
        if active and not self._timer.isActive():
            self._timer.start()
        elif not active and self._timer.isActive():
            self._timer.stop()
        self.update()

    def _update_min_height(self) -> None:
        n_t = len(self._tgt())
        exp = self._expanded()
        n_s = len(exp[1]) if exp else 0
        need = max(n_t * (_TGT_H + _GAP_Y),
                   n_s * (_NODE_H + _GAP_Y) + _TGT_H)
        self.setMinimumHeight(int(max(170, _PAD_Y * 2 + need)))

    def _tick(self) -> None:
        self._phase -= _FLOW_STEP     # 递减 → 虚线图案沿路径向前流动
        self.update()

    # ---------------------------------------------------------------- #
    #  几何
    # ---------------------------------------------------------------- #
    def _spread(self, n: int, height: float, node_h: float) -> List[float]:
        """n 个节点在高度内垂直均匀分布, 返回中心 y。"""
        if n <= 0:
            return []
        if n == 1:
            return [height / 2.0]
        top = _PAD_Y + node_h / 2.0
        bottom = height - _PAD_Y - node_h / 2.0
        step = (bottom - top) / (n - 1)
        return [top + i * step for i in range(n)]

    def _src_ys(self, n: int, center_y: float, height: float) -> List[float]:
        """展开的源节点: 以 center_y 为中心聚拢排列, 越界时夹回画布。"""
        if n <= 0:
            return []
        block = n * _NODE_H + (n - 1) * _GAP_Y
        top = center_y - block / 2.0
        min_top = _PAD_Y
        max_top = max(min_top, height - _PAD_Y - block)
        top = min(max(top, min_top), max_top)
        return [top + _NODE_H / 2.0 + i * (_NODE_H + _GAP_Y)
                for i in range(n)]

    def _rects(self) -> Tuple[List[QRectF], List[int], List[QRectF]]:
        """返回 (源节点矩形, 源节点对应的模型行, 目标节点矩形)。

        源节点仅在展开目标链路时存在, 顺序与该目标的 src_rows 一致。
        """
        h = float(self.height())
        w = float(self.width())
        tgt_items = self._tgt()
        yt = self._spread(len(tgt_items), h, _TGT_H)
        tgt = [QRectF(w - _PAD_X - _TGT_W, y - _TGT_H / 2, _TGT_W, _TGT_H)
               for y in yt]
        src: List[QRectF] = []
        src_rows: List[int] = []
        exp = self._expanded()
        if exp is not None:
            _, rows, tgt_row = exp
            src_rows = list(rows)
            ys = self._src_ys(len(src_rows), yt[tgt_row], h)
            src = [QRectF(_PAD_X, y - _NODE_H / 2, _SRC_W, _NODE_H)
                   for y in ys]
        return src, src_rows, tgt

    def _node_at(self, pos: QPointF) -> Optional[Tuple[bool, int]]:
        """命中检测。源节点返回 (True, 模型行号), 目标节点返回 (False, 行号)。"""
        src, src_rows, tgt = self._rects()
        for r, row in zip(src, src_rows):
            if r.contains(pos):
                return True, row
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
        src, src_rows, tgt = self._rects()
        src_items, tgt_items = self._src(), self._tgt()
        exp = self._expanded()

        if not tgt_items:
            painter.setPen(QColor("#8a8b90"))
            painter.drawText(self.rect(), Qt.AlignCenter,
                             "拖入包含视频的文件夹后，此处显示合并流图\n"
                             "(单击目标视频节点可展开其来源链路)")
            painter.end()
            return

        # --- 连线 (仅展开的目标; 先画线, 节点覆盖其上) --- #
        if exp is not None:
            t_item, rows, tgt_row = exp
            if 0 <= tgt_row < len(tgt):
                status = t_item.status
                r_t = tgt[tgt_row]
                x0 = _PAD_X + _SRC_W
                x1 = r_t.left()
                y1 = r_t.center().y()
                for rect, sr in zip(src, rows):
                    if not (0 <= sr < len(src_items)):
                        continue
                    y0 = rect.center().y()
                    painter.setPen(_line_pen(status, self._phase))
                    painter.setBrush(Qt.NoBrush)
                    path = QPainterPath(QPointF(x0, y0))
                    mid = (x1 - x0) * 0.5
                    path.cubicTo(QPointF(x0 + mid, y0),
                                 QPointF(x1 - mid, y1),
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

        # --- 收起时的左列提示 --- #
        if exp is None:
            hint_rect = QRectF(_PAD_X, 0,
                               (tgt[0].left() - _PAD_X * 2
                                if tgt else self.width() - _PAD_X * 2),
                               self.height())
            if hint_rect.width() > 80:
                painter.setPen(_MUTED)
                painter.drawText(hint_rect,
                                 Qt.AlignVCenter | Qt.AlignHCenter,
                                 "单击目标视频节点\n展开其来源链路")

        # --- 源节点 (仅展开时) --- #
        for rect, row in zip(src, src_rows):
            if 0 <= row < len(src_items):
                it = src_items[row]
                self._draw_node(painter, rect, it.path.name, it.status,
                                align_right=False)

        # --- 目标节点 (常驻, 两行: 文件名 + 末级目录名) --- #
        for i, r in enumerate(tgt):
            if i < len(tgt_items):
                self._draw_target(painter, r, tgt_items[i],
                                  expanded=(exp is not None
                                            and exp[2] == i))
        painter.end()

    def _draw_node(self, painter: QPainter, r: QRectF, name: str,
                   status: str, align_right: bool) -> None:
        """源节点 (单行文本)。"""
        color = _color_of(status)
        painter.setPen(QPen(color, 1.6))
        painter.setBrush(QColor(255, 255, 255, 14))
        painter.drawRoundedRect(r, 8.0, 8.0)
        fm = QFontMetrics(painter.font())
        avail = r.width() - 24.0
        text = _elide(name, fm, avail)
        painter.setPen(QColor("#e6e6e6"))
        painter.drawText(
            QRectF(r.left() + 12, r.top(), r.width() - 24, r.height()),
            Qt.AlignVCenter | (Qt.AlignRight if align_right else Qt.AlignLeft),
            text)

    def _draw_target(self, painter: QPainter, r: QRectF, item: TargetItem,
                     expanded: bool) -> None:
        """目标节点: 左侧展开三角/感叹号徽标, 右侧两行 (文件名/末级目录名)。"""
        color = _color_of(item.status)
        painter.setPen(QPen(color, 1.6))
        painter.setBrush(QColor(255, 255, 255, 14))
        painter.drawRoundedRect(r, 8.0, 8.0)

        # 左侧标记: 时长异常 → 红色感叹号; 否则展开状态三角
        lx = r.left() + 11.0
        cy = r.center().y()
        if item.status == "时长异常":
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(C_ERR))
            painter.drawEllipse(QPointF(lx, cy), 7.2, 7.2)
            painter.setPen(QPen(QColor("white"), 1.3))
            f = painter.font()
            f.setBold(True)
            painter.setFont(f)
            painter.drawText(QRectF(lx - 7, cy - 7, 14, 14),
                             Qt.AlignCenter, "!")
            f.setBold(False)
            painter.setFont(f)
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(_GRAY)
            if expanded:      # ▼ 再点收起
                pts = [QPointF(lx - 3.4, cy - 1.6),
                       QPointF(lx + 3.4, cy - 1.6),
                       QPointF(lx, cy + 2.6)]
            else:             # ▸ 可展开
                pts = [QPointF(lx - 1.6, cy - 3.4),
                       QPointF(lx - 1.6, cy + 3.4),
                       QPointF(lx + 2.6, cy)]
            painter.drawPolygon(QPolygonF(pts))

        # 两行文本 (右对齐)
        text_left = r.left() + 24.0
        avail = r.right() - 10.0 - text_left
        if avail <= 10:
            return
        fm = QFontMetrics(painter.font())
        painter.setPen(QColor("#e6e6e6"))
        painter.drawText(
            QRectF(text_left, r.top() + 5, avail, 17),
            Qt.AlignVCenter | Qt.AlignRight,
            _elide(item.name, fm, avail))
        f = painter.font()
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1.5))
        painter.setFont(f)
        painter.setPen(_MUTED)
        fm2 = QFontMetrics(f)
        dirname = item.directory.name or str(item.directory)
        painter.drawText(
            QRectF(text_left, r.top() + 22, avail, 17),
            Qt.AlignVCenter | Qt.AlignRight,
            _elide(dirname, fm2, avail))

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
        label = "节省率"
        saving = "—"
        if it.output_size and it.orig_total:
            saving = fmt_saving_pct(it.output_size, it.orig_total)
            if is_larger(it.output_size, it.orig_total):   # 变大改口径
                label = "体积变化"
        html = (f"<b>{it.name}</b> · {it.directory.name}<br>"
                f"视频数: {it.video_count} · 原始合计: "
                f"{_fmt_size(it.orig_total)}<br>"
                f"目标大小: {_fmt_size(it.output_size) if it.output_size else '—'}"
                f" · {label}: {saving}<br>")
        if it.duration_actual > 0:
            html += (f"时长: {fmt_duration(it.duration_actual)}"
                     f" (源合计 {fmt_duration(it.duration_expected)})<br>")
        html += f"状态: {it.status}<br>"
        if it.status == "时长异常":
            html += (f"<span style='color:{C_ERR}'><b>⚠ 成片时长与源视频"
                     f"总时长偏差过大, 可能存在卡帧/黑屏段, 请检查!</b></span><br>")
        html += f"<span style='color:#9a9b9f'>{it.directory / it.name}</span>"
        return html

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        hit = self._node_at(event.position())
        self.setCursor(Qt.PointingHandCursor
                       if hit is not None and not hit[0] else Qt.ArrowCursor)
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
        pos = event.position()
        if event.button() == Qt.RightButton:
            hit = self._node_at(pos)
            if hit is not None:
                source, idx = hit
                self._main.on_video_node_menu(
                    source, idx, event.globalPosition().toPoint())
            return
        if event.button() == Qt.LeftButton:
            hit = self._node_at(pos)
            if hit is None:
                self.collapse()            # 点击空白收起
                return
            source, idx = hit
            if not source:
                items = self._tgt()
                if 0 <= idx < len(items):
                    self.toggle_expand(items[idx])
            return
        super().mousePressEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().resizeEvent(event)
