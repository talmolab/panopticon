"""Bonsai-style stimulus workflow editor for Panopticon."""
import hashlib
import json
import math
import time
import uuid
from pathlib import Path
from typing import Callable

from PyQt5.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QGraphicsView, QGraphicsScene, QGraphicsItem, QGraphicsEllipseItem,
    QGraphicsLineItem, QGraphicsRectItem, QPushButton, QLineEdit, QLabel,
    QFrame, QFileDialog, QMessageBox, QSizePolicy, QCheckBox,
)
from PyQt5.QtCore import Qt, QRectF, QPointF, pyqtSignal, QThread, pyqtSlot, QTimer
from PyQt5.QtGui import (
    QPainter, QPen, QBrush, QColor, QFont, QPainterPath, QPolygonF,
    QPainterPathStroker, QTransform,
)

from gui_app import stim_compiler
from gui_app.serial_controller import TeensyController

# ── geometry constants ────────────────────────────────────────────────────────
BW, BH, HEADER_H = 160, 76, 22
PORT_R = 6
GRID = 20
SNAP_RADIUS = 26          # scene-unit snap distance
ARROW_LEN, ARROW_HALF = 11, 5


def _pin_color(pin: int) -> QColor:
    hue = (int(pin) * 137) % 360
    return QColor.fromHsv(hue, 170, 210)


# ── ConnectorPort ─────────────────────────────────────────────────────────────
class ConnectorPort(QGraphicsEllipseItem):
    TOP    = "top"
    BOTTOM = "bottom"
    LEFT   = "left"
    RIGHT  = "right"

    # Local (cx, cy) of each port in BlockItem coords
    _POS = {
        TOP:    (BW / 2,  0),
        BOTTOM: (BW / 2,  BH),
        LEFT:   (0,       BH / 2),
        RIGHT:  (BW,      BH / 2),
    }

    def __init__(self, side: str, parent_block):
        # Rect centered at local origin so transforms scale from the center.
        super().__init__(-PORT_R, -PORT_R, PORT_R * 2, PORT_R * 2, parent_block)
        self.side = side
        cx, cy = self._POS[side]
        self.setPos(cx, cy)
        self.setTransformOriginPoint(0, 0)
        self.setZValue(3)
        self._base_color = QColor("#5090d0")  # same neutral color for all ports
        self.setBrush(QBrush(self._base_color))
        self.setPen(QPen(QColor("#cce0f0"), 1))
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.NoButton)  # view handles all clicks

    def center_scene(self) -> QPointF:
        return self.mapToScene(QPointF(0, 0))  # local origin == center

    def set_snap_highlight(self, active: bool):
        if active:
            self.setScale(1.7)
            self.setBrush(QBrush(QColor("#ffffff")))
            self.setPen(QPen(QColor("#ffffff"), 2))
        else:
            self.setScale(1.0)
            self.setBrush(QBrush(self._base_color))
            self.setPen(QPen(QColor("#cce0f0"), 1))

    def hoverEnterEvent(self, event):
        if self.scale() < 1.5:
            self.setScale(1.35)
            self.setPen(QPen(QColor("#ffffff"), 1.5))
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        if self.scale() < 1.5:
            self.setScale(1.0)
            self.setPen(QPen(QColor("#cce0f0"), 1))
        super().hoverLeaveEvent(event)


# ── BlockItem ─────────────────────────────────────────────────────────────────
class BlockItem(QGraphicsItem):
    def __init__(self, pin, freq, pw, dur, block_id=None):
        super().__init__()
        self.block_id = block_id or str(uuid.uuid4())
        self.pin  = int(pin)
        self.freq = float(freq)
        self.pw   = float(pw)
        self.dur  = float(dur)
        self.in_arrows: list = []
        self.out_arrow = None
        # User-pinned start. Needed for loops, where every block has an
        # incoming arrow and so nothing looks like a beginning.
        self.explicit_start = False
        # When this block finishes, the recording stops. At most one per canvas.
        self.explicit_end = False
        # Resolved globally by StimCanvas.refresh_starts() -- never set directly.
        self._is_start = True
        self._needs_start = False

        self.setFlags(
            QGraphicsItem.ItemIsMovable |
            QGraphicsItem.ItemIsSelectable |
            QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setZValue(1)

        # One port per cardinal direction — any can source or receive arrows.
        self._ports: dict[str, ConnectorPort] = {
            s: ConnectorPort(s, self)
            for s in (ConnectorPort.TOP, ConnectorPort.BOTTOM,
                      ConnectorPort.LEFT, ConnectorPort.RIGHT)
        }

    def all_ports(self):
        return self._ports.values()

    def port(self, side: str) -> ConnectorPort:
        return self._ports[side]

    def is_chain_start(self) -> bool:
        return self._is_start

    def mode_text(self) -> str:
        """How this block's freq/pulse-width actually drive the pin."""
        if self.freq <= 0 or self.pw <= 0:
            return "pin LOW"
        duty = self.pw * self.freq / 10.0  # pw(ms) * freq(Hz) / 1000 as a percent
        if duty >= 100:
            return "constant ON"
        return f"{duty:g}% duty"

    def boundingRect(self) -> QRectF:
        m = PORT_R + 2
        return QRectF(-m, -m, BW + 2 * m, BH + 2 * m)

    def paint(self, painter: QPainter, option, widget):
        painter.setRenderHint(QPainter.Antialiasing)
        col      = _pin_color(self.pin)
        selected = self.isSelected()
        body     = QRectF(0, 0, BW, BH)

        # Followers are greyed out so the block that actually begins the
        # sequence is the one that reads as live.
        if not (self._is_start or self._needs_start):
            painter.setOpacity(0.85 if selected else 0.5)

        # shadow
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 60)))
        painter.drawRoundedRect(body.adjusted(3, 3, 3, 3), 8, 8)

        # body
        painter.setBrush(QBrush(QColor("#1c2840")))
        painter.setPen(QPen(col if selected else QColor("#3a4a6a"),
                            2 if selected else 1))
        painter.drawRoundedRect(body, 8, 8)

        # header stripe (clipped to body)
        painter.save()
        clip = QPainterPath()
        clip.addRoundedRect(body, 8, 8)
        painter.setClipPath(clip)
        painter.setBrush(QBrush(col.darker(140)))
        painter.setPen(Qt.NoPen)
        painter.drawRect(QRectF(0, 0, BW, HEADER_H))
        painter.restore()

        # header text
        painter.setPen(QPen(Qt.white))
        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
        painter.drawText(QRectF(0, 0, BW, HEADER_H), Qt.AlignCenter,
                         f"Pin {self.pin}")

        # body text — spell out the resulting waveform, since a pulse width at or
        # above the period is a 100% duty cycle, not a malformed train.
        painter.setFont(QFont("Segoe UI", 8))
        painter.setPen(QPen(QColor("#b8cce0")))
        painter.drawText(
            QRectF(10, HEADER_H + 4, BW - 20, BH - HEADER_H - 8),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"{self.freq:g} Hz  |  {self.pw:g} ms\n{self.dur:g} s  ·  {self.mode_text()}",
        )

        # Start marker, upper-right. Filled red = this chain starts here (a white
        # ring means the user pinned it); hollow amber = a loop with no start,
        # so it will not run until "Starting" is ticked on one of its blocks.
        if self._is_start:
            painter.setPen(QPen(QColor("#ffffff"), 1.5)
                           if self.explicit_start else Qt.NoPen)
            painter.setBrush(QBrush(QColor("#ee3333")))
            painter.drawEllipse(QPointF(BW - 9, 9), 5, 5)
        elif self._needs_start:
            painter.setPen(QPen(QColor("#ffaa33"), 1.5, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(BW - 9, 9), 5, 5)

        # Black end-marker sits beside the start dot; a lone block can be both.
        if self.explicit_end:
            painter.setPen(QPen(QColor("#dddddd"), 1.5))
            painter.setBrush(QBrush(QColor("#000000")))
            painter.drawEllipse(QPointF(BW - 24, 9), 5, 5)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            if self.out_arrow:
                self.out_arrow.update_path()
            for arr in self.in_arrows:
                arr.update_path()
        return super().itemChange(change, value)

    def to_dict(self) -> dict:
        p = self.pos()
        return {"id": self.block_id, "x": p.x(), "y": p.y(),
                "pin": self.pin, "freq": self.freq,
                "pw": self.pw, "dur": self.dur,
                "start": self.explicit_start, "end": self.explicit_end}


# ── ArrowItem ─────────────────────────────────────────────────────────────────
class ArrowItem(QGraphicsItem):
    def __init__(self, src_block: BlockItem, src_port: ConnectorPort,
                 dst_block: BlockItem, dst_port: ConnectorPort):
        super().__init__()
        self.src      = src_block
        self.src_port = src_port
        self.dst      = dst_block
        self.dst_port = dst_port
        self.setZValue(0)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self._line_path = QPainterPath()
        self._arrowhead = QPolygonF()
        src_block.out_arrow = self
        dst_block.in_arrows.append(self)
        # Refresh start-dot on destination (it may lose its dot now).
        dst_block.update()
        self.update_path()

    def update_path(self):
        sp = self.src_port.center_scene()
        dp = self.dst_port.center_scene()

        path = QPainterPath(sp)
        path.lineTo(dp)
        self._line_path = path

        dx, dy = dp.x() - sp.x(), dp.y() - sp.y()
        length = math.hypot(dx, dy) or 1
        ux, uy = dx / length, dy / length
        base = QPointF(dp.x() - ux * ARROW_LEN, dp.y() - uy * ARROW_LEN)
        self._arrowhead = QPolygonF([
            dp,
            QPointF(base.x() + uy * ARROW_HALF, base.y() - ux * ARROW_HALF),
            QPointF(base.x() - uy * ARROW_HALF, base.y() + ux * ARROW_HALF),
        ])
        self.prepareGeometryChange()

    def boundingRect(self) -> QRectF:
        return self._line_path.boundingRect().adjusted(
            -ARROW_LEN - 2, -ARROW_LEN - 2, ARROW_LEN + 2, ARROW_LEN + 2)

    def shape(self) -> QPainterPath:
        # Wider hit area so the line is easy to click.
        stroker = QPainterPathStroker()
        stroker.setWidth(12)
        return stroker.createStroke(self._line_path)

    def paint(self, painter: QPainter, option, widget):
        painter.setRenderHint(QPainter.Antialiasing)
        selected = self.isSelected()
        color    = QColor("#ffffff") if selected else QColor("#7aaad0")
        width    = 3 if selected else 2
        painter.setPen(QPen(color, width))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(self._line_path)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(color))
        painter.drawPolygon(self._arrowhead)

    def detach(self):
        if self.src.out_arrow is self:
            self.src.out_arrow = None
        if self in self.dst.in_arrows:
            self.dst.in_arrows.remove(self)
        # Refresh start-dot on both ends.
        self.src.update()
        self.dst.update()


# ── StimCanvas ────────────────────────────────────────────────────────────────
class StimCanvas(QGraphicsView):
    block_selected = pyqtSignal(object)  # BlockItem or None
    starts_changed = pyqtSignal(int)     # count of blocks stuck without a start

    def __init__(self):
        scene = QGraphicsScene()
        scene.setSceneRect(-3000, -3000, 6000, 6000)
        super().__init__(scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setStyleSheet("background: #111820; border: none;")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._arrow_src: ConnectorPort | None = None
        self._rubber:    QGraphicsLineItem | None = None
        self._snap_target: ConnectorPort | None = None
        self._sel_rubber: QGraphicsRectItem | None = None
        self._sel_start:  QPointF | None = None
        self._pan_last:   QPointF | None = None
        self._clipboard:  list[dict] = []
        self._mouse_scene: QPointF = QPointF(0, 0)

        scene.selectionChanged.connect(self._on_selection_changed)

    # ── background ────────────────────────────────────────────────────────────
    def drawBackground(self, painter: QPainter, rect: QRectF):
        painter.fillRect(rect, QColor("#111820"))
        painter.setPen(QPen(QColor("#1e2a38"), 1))
        x = int(rect.left()  // GRID) * GRID
        while x <= rect.right():
            y = int(rect.top() // GRID) * GRID
            while y <= rect.bottom():
                painter.drawPoint(int(x), int(y))
                y += GRID
            x += GRID

    # ── snap helper ───────────────────────────────────────────────────────────
    def _find_snap_port(self, sp: QPointF,
                        src_block: BlockItem) -> ConnectorPort | None:
        """Return nearest port on any block except src_block within SNAP_RADIUS."""
        best, best_d = None, SNAP_RADIUS
        for item in self.scene().items():
            if isinstance(item, BlockItem) and item is not src_block:
                for port in item.all_ports():
                    c = port.center_scene()
                    d = math.hypot(c.x() - sp.x(), c.y() - sp.y())
                    if d < best_d:
                        best_d = d
                        best = port
        return best

    # ── mouse press ───────────────────────────────────────────────────────────
    def mousePressEvent(self, event):
        sp = self.mapToScene(event.pos())
        self._mouse_scene = sp

        if event.button() == Qt.MiddleButton:
            self._pan_last = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            return

        if event.button() == Qt.LeftButton:
            item = self.scene().itemAt(sp, QTransform())

            # Port with no outgoing arrow on its block → start arrow drag.
            if (isinstance(item, ConnectorPort)
                    and item.parentItem().out_arrow is None):
                self._arrow_src = item
                src_pt = item.center_scene()
                self._rubber = self.scene().addLine(
                    src_pt.x(), src_pt.y(), src_pt.x(), src_pt.y(),
                    QPen(QColor("#7aaad0"), 2, Qt.DashLine),
                )
                self._rubber.setZValue(10)
                return

            # Empty space → rubber-band selection.
            if item is None:
                if not (event.modifiers() & Qt.ShiftModifier):
                    self.scene().clearSelection()
                self._sel_start  = sp
                self._sel_rubber = self.scene().addRect(
                    QRectF(sp, sp),
                    QPen(QColor("#5078c8"), 1, Qt.DashLine),
                    QBrush(QColor(80, 120, 200, 25)),
                )
                self._sel_rubber.setZValue(100)
                return

        # Block / arrow / port-on-connected-block → Qt handles selection + move.
        super().mousePressEvent(event)

    # ── mouse move ────────────────────────────────────────────────────────────
    def mouseMoveEvent(self, event):
        sp = self.mapToScene(event.pos())
        self._mouse_scene = sp

        if self._pan_last is not None:
            d = event.pos() - self._pan_last
            self._pan_last = event.pos()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - d.x())
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - d.y())
            return

        if self._rubber is not None:
            snap = self._find_snap_port(sp, self._arrow_src.parentItem())
            if snap is not self._snap_target:
                if self._snap_target:
                    self._snap_target.set_snap_highlight(False)
                self._snap_target = snap
                if snap:
                    snap.set_snap_highlight(True)
            src_pt  = self._arrow_src.center_scene()
            end_pt  = snap.center_scene() if snap else sp
            self._rubber.setLine(
                src_pt.x(), src_pt.y(), end_pt.x(), end_pt.y())
            return

        if self._sel_rubber is not None:
            self._sel_rubber.setRect(QRectF(self._sel_start, sp).normalized())
            return

        super().mouseMoveEvent(event)

    # ── mouse release ─────────────────────────────────────────────────────────
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._pan_last = None
            self.unsetCursor()
            return

        if event.button() == Qt.LeftButton and self._rubber is not None:
            if self._snap_target:
                self._snap_target.set_snap_highlight(False)

            dst_port = self._snap_target
            if dst_port is None:
                # Fall back to exact hit-test under cursor.
                sp   = self.mapToScene(event.pos())
                item = self.scene().itemAt(sp, QTransform())
                if isinstance(item, ConnectorPort):
                    dst_port = item

            if dst_port is not None:
                dst_block = dst_port.parentItem()
                src_block = self._arrow_src.parentItem()
                if dst_block is not src_block:
                    self.scene().addItem(
                        ArrowItem(src_block, self._arrow_src, dst_block, dst_port))
                    self.refresh_starts()

            self.scene().removeItem(self._rubber)
            self._rubber      = None
            self._arrow_src   = None
            self._snap_target = None
            return

        if event.button() == Qt.LeftButton and self._sel_rubber is not None:
            sp   = self.mapToScene(event.pos())
            rect = QRectF(self._sel_start, sp).normalized()
            self.scene().removeItem(self._sel_rubber)
            self._sel_rubber = None
            self._sel_start  = None
            for item in self.scene().items(rect, Qt.IntersectsItemBoundingRect):
                if isinstance(item, BlockItem):
                    item.setSelected(True)
            return

        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            self._delete_selected()
            return
        if event.modifiers() & Qt.ControlModifier:
            if event.key() == Qt.Key_C:
                self._copy_selected()
                return
            if event.key() == Qt.Key_V:
                self._paste()
                return
        super().keyPressEvent(event)

    # ── selection signal ──────────────────────────────────────────────────────
    def _on_selection_changed(self):
        sel = [i for i in self.scene().selectedItems() if isinstance(i, BlockItem)]
        self.block_selected.emit(sel[0] if len(sel) == 1 else None)

    # ── start resolution ──────────────────────────────────────────────────────
    def blocks(self) -> list[BlockItem]:
        return [i for i in self.scene().items() if isinstance(i, BlockItem)]

    def refresh_starts(self):
        """Re-resolve which blocks begin a chain and repaint the markers.

        Delegates to the compiler so the canvas can never disagree with what
        actually gets uploaded.
        """
        blocks, edges = self.get_workflow()
        starts, needs = stim_compiler.resolve_starts(blocks, edges)
        for blk in self.blocks():
            # Two pinned starts can end up in one group when an arrow merges
            # them; the loser gives up its flag so the UI stays truthful.
            if blk.explicit_start and blk.block_id not in starts:
                blk.explicit_start = False
            s = blk.block_id in starts
            n = blk.block_id in needs
            if (s, n) != (blk._is_start, blk._needs_start):
                blk._is_start, blk._needs_start = s, n
                blk.update()
        self.starts_changed.emit(len(needs))

    def _component(self, blk: BlockItem) -> set[BlockItem]:
        """All blocks reachable from blk ignoring arrow direction."""
        seen, stack = {blk}, [blk]
        while stack:
            cur = stack.pop()
            neighbours = [a.src for a in cur.in_arrows]
            if cur.out_arrow:
                neighbours.append(cur.out_arrow.dst)
            for n in neighbours:
                if n not in seen:
                    seen.add(n)
                    stack.append(n)
        return seen

    def set_explicit_start(self, blk: BlockItem, on: bool):
        """Pin (or unpin) blk as its group's start; only one per group."""
        if on:
            for other in self._component(blk):
                other.explicit_start = other is blk
                other.update()
        else:
            blk.explicit_start = False
            blk.update()
        self.refresh_starts()

    def set_explicit_end(self, blk: BlockItem, on: bool):
        """Mark blk as the block that ends the recording. One per canvas."""
        for other in self.blocks():
            was = other.explicit_end
            other.explicit_end = on and other is blk
            if other.explicit_end != was:
                other.update()
        self.refresh_starts()

    # ── block / arrow operations ──────────────────────────────────────────────
    def add_block(self, pin, freq, pw, dur) -> BlockItem:
        existing = self.blocks()
        blk = BlockItem(pin, freq, pw, dur)
        blk.setPos(len(existing) * (BW + 30), 0)
        self.scene().addItem(blk)
        self.refresh_starts()
        return blk

    def _delete_selected(self):
        """Delete selected blocks and/or arrows cleanly with no ghost graphics."""
        to_remove: set = set()

        for item in list(self.scene().selectedItems()):
            if isinstance(item, BlockItem):
                to_remove.add(item)
                if item.out_arrow:
                    to_remove.add(item.out_arrow)
                for arr in item.in_arrows:
                    to_remove.add(arr)
            elif isinstance(item, ArrowItem):
                to_remove.add(item)

        # Detach arrows first so block references are cleaned up before removal.
        for item in to_remove:
            if isinstance(item, ArrowItem):
                item.detach()

        for item in to_remove:
            self.scene().removeItem(item)

        self.refresh_starts()
        self.block_selected.emit(None)

    def _copy_selected(self):
        sel = [i for i in self.scene().selectedItems() if isinstance(i, BlockItem)]
        if not sel:
            return
        pivot = sel[0].pos()
        self._clipboard = [
            {**b.to_dict(),
             "rx": b.pos().x() - pivot.x(),
             "ry": b.pos().y() - pivot.y()}
            for b in sel
        ]

    def _paste(self):
        if not self._clipboard:
            return
        origin = self._mouse_scene
        new_blocks = []
        for d in self._clipboard:
            blk = BlockItem(d["pin"], d["freq"], d["pw"], d["dur"])
            blk.setPos(origin.x() + d["rx"] + BW + 40, origin.y() + d["ry"])
            self.scene().addItem(blk)
            new_blocks.append(blk)
        self.scene().clearSelection()
        for b in new_blocks:
            b.setSelected(True)
        self.refresh_starts()

    # ── serialisation ─────────────────────────────────────────────────────────
    def get_workflow(self) -> tuple[list[dict], list[dict]]:
        blocks, edges = [], []
        for item in self.scene().items():
            if isinstance(item, BlockItem):
                blocks.append(item.to_dict())
            elif isinstance(item, ArrowItem):
                edges.append({
                    "src":      item.src.block_id,
                    "src_port": item.src_port.side,
                    "dst":      item.dst.block_id,
                    "dst_port": item.dst_port.side,
                })
        return blocks, edges

    def load_workflow(self, blocks: list[dict], edges: list[dict]):
        self.clear()
        by_id: dict[str, BlockItem] = {}
        for d in blocks:
            blk = BlockItem(d["pin"], d["freq"], d["pw"], d["dur"],
                            block_id=d["id"])
            blk.explicit_start = bool(d.get("start", False))
            blk.explicit_end = bool(d.get("end", False))
            blk.setPos(d["x"], d["y"])
            self.scene().addItem(blk)
            by_id[d["id"]] = blk
        for e in edges:
            src = by_id.get(e["src"])
            dst = by_id.get(e["dst"])
            if src and dst:
                # Gracefully fall back to LEFT/RIGHT for old save files.
                sp = e.get("src_port", ConnectorPort.RIGHT)
                dp = e.get("dst_port", ConnectorPort.LEFT)
                self.scene().addItem(
                    ArrowItem(src, src.port(sp), dst, dst.port(dp)))
        self.refresh_starts()

    def clear(self):
        self.scene().clear()
        self.refresh_starts()


# ── WaveformPreview ───────────────────────────────────────────────────────────
class WaveformPreview(QWidget):
    """One second of the square wave the current freq / pulse-width would emit.

    Exists because a pulse width at or above the period is easy to type by
    accident (10 Hz + 100 ms looks like a 10 ms-per-cycle train but is 100% duty)
    and impossible to spot in the numbers alone.
    """
    WINDOW_MS = 1000.0
    MAX_CYCLES = 400          # drawing cap; a denser train just reads as a band

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(214, 108)
        self._freq = 0.0
        self._pw = 0.0

    def set_params(self, freq: float, pw: float):
        if (freq, pw) != (self._freq, self._pw):
            self._freq, self._pw = freq, pw
            self.update()

    def state(self) -> tuple[str, str]:
        """(kind, caption) — kind is low | train | constant | impossible."""
        f, pw = self._freq, self._pw
        if f <= 0 or pw <= 0:
            return "low", "pin held LOW"
        period = 1000.0 / f
        if pw > period * (1 + 1e-9):
            return "impossible", f"pulse {pw:g} ms > period {period:g} ms"
        if pw >= period * (1 - 1e-9):
            return "constant", f"100% duty — constant ON, not {f:g} Hz"
        return "train", f"{f:g} Hz · {pw:g} ms · {pw / period * 100:.0f}% duty"

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        kind, caption = self.state()
        bad = kind in ("constant", "impossible")

        m = 7
        area = QRectF(m, m, self.width() - 2 * m, self.height() - 2 * m - 15)

        if bad:  # glow rings in the margin around the plot
            for i, alpha in enumerate((80, 50, 26)):
                p.setPen(QPen(QColor(255, 60, 60, alpha), 2))
                p.setBrush(Qt.NoBrush)
                off = i * 2 + 1
                p.drawRoundedRect(area.adjusted(-off, -off, off, off), 5, 5)

        p.setBrush(QBrush(QColor("#0d131b")))
        p.setPen(QPen(QColor("#ff5555") if bad else QColor("#33445a"), 1))
        p.drawRoundedRect(area, 4, 4)

        x0, x1 = area.left() + 7, area.right() - 7
        y_hi, y_lo = area.top() + 11, area.bottom() - 13
        span = x1 - x0

        p.setPen(QPen(QColor("#26333f"), 1, Qt.DotLine))
        p.drawLine(int(x0), int(y_lo), int(x1), int(y_lo))

        path = QPainterPath(QPointF(x0, y_lo))
        if kind == "low":
            path.lineTo(x1, y_lo)
        elif bad:
            # Both cases drive the pin permanently HIGH -- draw what really happens.
            path.lineTo(x0, y_hi)
            path.lineTo(x1, y_hi)
        else:
            period_px = (1000.0 / self._freq) / self.WINDOW_MS * span
            pw_px = self._pw / self.WINDOW_MS * span
            t, n = x0, 0
            while t < x1 and n < self.MAX_CYCLES:
                hi_end = min(t + pw_px, x1)
                path.lineTo(t, y_hi)
                path.lineTo(hi_end, y_hi)
                path.lineTo(hi_end, y_lo)
                t += period_px
                n += 1
                path.lineTo(min(t, x1), y_lo)

        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor("#ff6b6b") if bad else QColor("#5fd0e0"), 1.6))
        p.drawPath(path)

        p.setFont(QFont("Segoe UI", 7))
        p.setPen(QPen(QColor("#4a5a6a")))
        p.drawText(QRectF(x0, y_lo + 1, span, 11), Qt.AlignRight | Qt.AlignTop, "1 s")

        p.setFont(QFont("Segoe UI", 7, QFont.Bold if bad else QFont.Normal))
        p.setPen(QPen(QColor("#ff7777") if bad else QColor("#8fa4b8")))
        p.drawText(QRectF(0, area.bottom() + 2, self.width(), 14),
                   Qt.AlignCenter, caption)


# ── upload worker ─────────────────────────────────────────────────────────────
class _UploadWorker(QThread):
    done = pyqtSignal(bool, str)

    def __init__(self, ino: str, port: str):
        super().__init__()
        self.ino = ino          # kept so the window can record what got flashed
        self._port = port

    def run(self):
        self.done.emit(*stim_compiler.upload(self.ino, self._port))


# ── StimulationWindow ─────────────────────────────────────────────────────────
_BTN = (
    "QPushButton {{ background: {bg}; color: {fg}; border: 1px solid {bd}; "
    "border-radius: 4px; padding: 5px 12px; font-size: 11px; }}"
    "QPushButton:hover {{ background: {hv}; }}"
    "QPushButton:disabled {{ color: #555; border-color: #333; }}"
)


def _btn(text, bg="#2a2a4a", fg="#88aadd", bd="#444", hv="#333366"):
    b = QPushButton(text)
    b.setStyleSheet(_BTN.format(bg=bg, fg=fg, bd=bd, hv=hv))
    return b


def _field(placeholder=""):
    f = QLineEdit()
    f.setPlaceholderText(placeholder)
    f.setFixedWidth(72)
    f.setStyleSheet(
        "QLineEdit { background: #1a1a2e; color: #dcdcdc; border: 1px solid #444; "
        "border-radius: 3px; padding: 3px 6px; font-size: 11px; }"
        "QLineEdit:focus { border-color: #5078c8; }"
    )
    return f


def _lbl(text, color="#aaa"):
    l = QLabel(text)
    l.setStyleSheet(f"color: {color}; font-size: 11px; border: none;")
    return l


class StimulationWindow(QDialog):
    def __init__(self, get_port: Callable[[], str],
                 get_output_dir: Callable[[], str],
                 get_fps: Callable[[], int] = lambda: 100,
                 is_busy: Callable[[], bool] = lambda: False,
                 get_safe_pins: Callable[[], list] = lambda: list(
                     stim_compiler.DEFAULT_SAFE_LOW_PINS),
                 get_trigger_pins: Callable[[], list] = lambda: [],
                 get_serial: Callable[[], object] = lambda: None,
                 release_serial: Callable[[], None] = lambda: None,
                 on_applied: Callable[[str], None] = lambda ino: None,
                 parent=None):
        super().__init__(parent)
        self._get_port       = get_port
        self._get_output_dir = get_output_dir
        self._get_fps        = get_fps
        self._is_busy        = is_busy
        self._get_safe_pins  = get_safe_pins
        # Needed to refuse a stim chain on a camera trigger line, which would
        # silently break cross-camera block-ID alignment. Defaults to empty so a
        # bare editor still works, but main_window MUST pass the profile's pins.
        self._get_trigger_pins = get_trigger_pins
        self._get_serial     = get_serial
        self._release_serial = release_serial
        # Called with the .ino source after a successful Apply. The main window
        # keeps it for the session so it can put this paradigm back on the board
        # after a calibration has reflashed it away.
        self._on_applied = on_applied
        self._test_owns_serial = False
        self._selected_block: BlockItem | None   = None
        self._upload_worker:  _UploadWorker | None = None
        # The .ino this editor last successfully uploaded, so Test can tell when
        # the canvas has drifted away from it. NOT the same as "what the board
        # is running": main_window swaps the board between the recording-only
        # and recording+stim sketches per acquisition, so after a calibration
        # the board holds the stim-free sketch while this still holds the
        # paradigm. invalidate_upload() clears it when a reflash fails.
        self._uploaded_ino:   str | None = None
        #: True once an Apply has FAILED and no later Apply has succeeded, i.e.
        #: we positively know the board does not carry this canvas. Distinct
        #: from `_uploaded_ino is None`, which only means "nothing was uploaded
        #: this session" -- a paradigm Applied in an earlier session is still on
        #: the board, so that case must stay recordable.
        self._apply_failed:   bool = False
        self._test_after_upload = False
        self._test_serial: TeensyController | None = None
        self._test_timer:  QTimer | None = None
        self._test_end_at: float | None = None

        self.setWindowTitle("Stimulation Editor")
        self.setMinimumSize(860, 560)
        self.setStyleSheet("background: #141428; color: #dcdcdc;")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Vertical)
        splitter.setStyleSheet(
            "QSplitter::handle { background: #333; height: 3px; }")

        self._canvas = StimCanvas()
        self._canvas.block_selected.connect(self._on_block_selected)
        self._canvas.starts_changed.connect(self._on_starts_changed)
        splitter.addWidget(self._canvas)

        # ── bottom panel ──────────────────────────────────────────────────────
        bottom = QWidget()
        bottom.setStyleSheet("background: #1a1a2e;")
        bottom.setMinimumHeight(134)
        bottom.setMaximumHeight(170)
        outer = QHBoxLayout(bottom)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(14)

        self._wave = WaveformPreview()
        outer.addWidget(self._wave, alignment=Qt.AlignTop)

        bl = QVBoxLayout()
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(8)
        outer.addLayout(bl, stretch=1)

        self._f_pin  = _field("pin")
        self._f_freq = _field("Hz")
        self._f_pw   = _field("ms")
        self._f_dur  = _field("s")

        self._start_cb = QCheckBox("Starting")
        self._start_cb.setToolTip(
            "Pin the selected block as the start of its sequence.\n"
            "Required for loops, where every block has an incoming arrow.")
        self._start_cb.setEnabled(False)
        self._start_cb.setStyleSheet(
            "QCheckBox { color: #dd8888; font-size: 11px; border: none; }"
            "QCheckBox:disabled { color: #555; }"
            "QCheckBox::indicator { width: 12px; height: 12px; border-radius: 6px; "
            "border: 1px solid #666; background: #1a1a2e; }"
            "QCheckBox::indicator:checked { background: #ee3333; border-color: #ff8888; }"
        )
        self._start_cb.toggled.connect(self._on_start_toggled)

        self._end_cb = QCheckBox("Ending")
        self._end_cb.setToolTip(
            "When this block finishes, stop the recording.\n"
            "One per workflow; a looping chain keeps running until then.")
        self._end_cb.setEnabled(False)
        self._end_cb.setStyleSheet(
            "QCheckBox { color: #bbbbbb; font-size: 11px; border: none; }"
            "QCheckBox:disabled { color: #555; }"
            "QCheckBox::indicator { width: 12px; height: 12px; border-radius: 6px; "
            "border: 1px solid #666; background: #1a1a2e; }"
            "QCheckBox::indicator:checked { background: #000000; border-color: #dddddd; }"
        )
        self._end_cb.toggled.connect(self._on_end_toggled)

        param_row = QHBoxLayout()
        param_row.setSpacing(8)
        for w in [_lbl("Pin"), self._f_pin,
                  _lbl("Freq"), self._f_freq, _lbl("Hz"),
                  _lbl("PW"),   self._f_pw,   _lbl("ms"),
                  _lbl("Dur"), self._f_dur,  _lbl("s")]:
            param_row.addWidget(w)
        param_row.addSpacing(10)
        param_row.addWidget(self._start_cb)
        param_row.addWidget(self._end_cb)
        create_btn = _btn("Create Block", bg="#1e3a1e", fg="#88dd88",
                          bd="#3a6a3a", hv="#254a25")
        create_btn.clicked.connect(self._on_create)
        param_row.addSpacing(12)
        param_row.addWidget(create_btn)
        param_row.addStretch()
        bl.addLayout(param_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #333; max-height: 1px;")
        bl.addWidget(sep)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        load_btn  = _btn("Load");  load_btn.clicked.connect(self._on_load)
        clear_btn = _btn("Clear"); clear_btn.clicked.connect(self._on_clear)
        self._status_lbl = _lbl("", color="#888")
        self._status_lbl.setSizePolicy(QSizePolicy.Expanding,
                                       QSizePolicy.Preferred)
        save_btn = _btn("Save"); save_btn.clicked.connect(self._on_save)
        self._test_btn = _btn("Test", bg="#1e2e3a", fg="#77bbdd",
                              bd="#3a5a6a", hv="#254050")
        self._test_btn.setToolTip(
            "Run the paradigm on the Arduino with no cameras triggered.")
        self._test_btn.clicked.connect(self._on_test)
        self._apply_btn = _btn("Apply to Arduino",
                               bg="#3a1e1e", fg="#dd8888",
                               bd="#6a3a3a", hv="#4a2525")
        self._apply_btn.clicked.connect(self._on_apply)
        for w in [load_btn, clear_btn, self._status_lbl,
                  save_btn, self._test_btn, self._apply_btn]:
            btn_row.addWidget(w)
        bl.addLayout(btn_row)

        splitter.addWidget(bottom)
        splitter.setSizes([400, 140])
        root.addWidget(splitter)

        for f in (self._f_pin, self._f_freq, self._f_pw, self._f_dur):
            f.returnPressed.connect(self._on_field_enter)
        for f in (self._f_freq, self._f_pw):
            f.textChanged.connect(self._update_preview)
        self._update_preview()

        # In a QDialog every QPushButton is autoDefault, so Enter in a field was
        # also firing "Create Block" and spawning a duplicate of the block the
        # user was editing.
        for b in self.findChildren(QPushButton):
            b.setAutoDefault(False)
            b.setDefault(False)

    # ── block selection ───────────────────────────────────────────────────────
    @pyqtSlot(object)
    def _on_block_selected(self, block):
        self._selected_block = block
        # Reflect the block's flags without re-triggering the setters.
        for cb, flag in ((self._start_cb, "explicit_start"),
                         (self._end_cb, "explicit_end")):
            cb.setEnabled(block is not None)
            cb.blockSignals(True)
            cb.setChecked(bool(block is not None and getattr(block, flag)))
            cb.blockSignals(False)
        if block is not None:
            self._f_pin.setText(str(block.pin))
            self._f_freq.setText(str(block.freq))
            self._f_pw.setText(str(block.pw))
            self._f_dur.setText(str(block.dur))

    def _on_start_toggled(self, checked: bool):
        if self._selected_block is not None:
            self._canvas.set_explicit_start(self._selected_block, checked)

    def _on_end_toggled(self, checked: bool):
        if self._selected_block is not None:
            self._canvas.set_explicit_end(self._selected_block, checked)

    def _num(self, field) -> float:
        try:
            return float(field.text())
        except ValueError:
            return 0.0

    def _update_preview(self):
        self._wave.set_params(self._num(self._f_freq), self._num(self._f_pw))

    @pyqtSlot(int)
    def _on_starts_changed(self, n_stuck: int):
        if n_stuck:
            self._set_status(
                f"{n_stuck} block(s) form a loop with no start — select one "
                f"and tick 'Starting'.", error=True)
            return
        blocks, edges = self._canvas.get_workflow()
        clash = stim_compiler.pin_conflicts(blocks, edges)
        if clash:
            self._set_status(
                f"Pin {', '.join(str(p) for p in clash)} is driven by two chains "
                f"at once — they will fight.", error=True)
            return
        if any(b.get("end") for b in blocks) and \
                stim_compiler.end_time_s(blocks, edges) is None:
            self._set_status(
                "The 'Ending' block is not reachable from any start — the "
                "recording will not stop on its own.", error=True)
            return
        end_t = stim_compiler.end_time_s(blocks, edges)
        if end_t:
            self._set_status(f"Recording will stop {end_t:g} s after start.")
        elif self._status_lbl.text().startswith(("The 'Ending'", "Recording will",
                                                 "Invalid", "Pin")) \
                or "loop with no start" in self._status_lbl.text():
            self._set_status("")

    def end_time_s(self) -> float | None:
        """Seconds after record start at which the paradigm's end block finishes."""
        blocks, edges = self._canvas.get_workflow()
        return stim_compiler.end_time_s(blocks, edges)

    def _compile(self) -> str:
        blocks, edges = self._canvas.get_workflow()
        return stim_compiler.compile_ino(blocks, edges, self._get_safe_pins(),
                                         self._get_trigger_pins())

    def _blocking_problem(self) -> str | None:
        """Reason this workflow must not be uploaded or run, or None."""
        blocks, edges = self._canvas.get_workflow()
        _, needs = stim_compiler.resolve_starts(blocks, edges)
        if needs:
            return (f"{len(needs)} block(s) form a loop with no starting block, "
                    f"so they would never run.\n\nSelect one and tick 'Starting'.")
        # Refuse before compile_ino raises, so the user gets an explanation
        # rather than a traceback. This is the only known way to silently break
        # the rig's core assumption that block ID N is the same instant on every
        # camera, so it blocks Apply, Test and Record.
        bad = stim_compiler.forbidden_pin_uses(blocks, self._get_trigger_pins())
        if bad:
            return "\n\n".join(
                [f"Pin {p} cannot carry a stim waveform: {why}." for p, why in bad]
                + ["Move the block to a free pin."])
        clash = stim_compiler.pin_conflicts(blocks, edges)
        if clash:
            pins = ", ".join(str(p) for p in clash)
            return (f"Pin {pins} is driven by more than one chain.\n\nChains run "
                    f"at the same time, so they would fight over the output and "
                    f"the waveform would be neither one. Give each chain its own "
                    f"pin, or merge them into a single chain.")
        return None

    def invalidate_upload(self, reason: str = ""):
        """Forget that this canvas is on the board, because it no longer is.

        Called when something outside the editor reflashes the board. Without
        it `provenance()` would keep reporting matches_uploaded_firmware: true
        against firmware that no longer holds this paradigm, and a recording
        made without re-applying would carry a confident but false record of
        what the animal received. `None` — "unknown" — is the honest state.
        """
        self._uploaded_ino = None
        self._set_status(
            f"Board reflashed{' — ' + reason if reason else ''}. "
            f"Press Apply again before recording.", error=True)

    def is_uploading(self) -> bool:
        """True while arduino-cli is compiling/flashing the board.

        An Apply runs with the main window at IDLE and not busy, so nothing else
        knows it is happening. Quitting during the ~30 s flash would destroy a
        running QThread and can kill avrdude mid-write, leaving the Mega with no
        `allStimLow()` boot guard — a laser pin floating on the next power-up.
        """
        return (self._upload_worker is not None
                and self._upload_worker.isRunning())

    def record_blocker(self) -> str | None:
        """Reason a RECORDING must not start with this workflow, or None.

        Record does not compile anything — it runs whatever `_ensure_sketch_for`
        put on the board — so this was never gated, and only `_on_apply` and
        `_on_test` consulted _blocking_problem. But the canvas is what
        `stim_paradigm.json` and `stim_trace.csv` describe, and a graph
        containing a forbidden pin
        means the .ino on the board may be driving a camera trigger line, which
        silently breaks the block-ID identity every downstream consumer assumes.
        CLAUDE.md already claims Record warns here; this makes that true.

        A failed Apply also blocks. `stim_trace.csv` marks which frames were
        stimulated by reading the CANVAS, not the board, so after a failed
        upload it happily reports "900 frames with stimulation active" for a
        session in which the board never received the paradigm and nothing
        fired. Observed 2026-09-14: arduino-cli lost a race for the serial port,
        the upload failed, and the recording went ahead and was labelled
        stimulated. Data mislabelled as stimulated is worse than no data, so
        refuse until an Apply succeeds. Only a KNOWN failure blocks -- a
        paradigm Applied in a previous session is still on the board and stays
        recordable, which is why this is a separate flag from `_uploaded_ino`.
        """
        if self._apply_failed:
            return ("The last Apply FAILED, so the board does not carry this "
                    "paradigm.\n\nRecording now would produce a session "
                    "labelled as stimulated in which nothing was actually "
                    "driven. Press Apply again and let it succeed first.\n\n"
                    "If arduino-cli reported a port error, close anything else "
                    "using the board's serial port and retry.")
        return self._blocking_problem()

    def provenance(self) -> dict:
        """Everything needed to reconstruct what the animal actually received."""
        blocks, edges = self._canvas.get_workflow()
        # Must pass trigger_pins, same as _compile(). Otherwise the two compile
        # calls disagree: this one succeeds on a forbidden-pin graph while
        # firmware_source() raises, so stim_paradigm.json gets written and the
        # .ino beside it does not — a half-described session.
        ino = stim_compiler.compile_ino(blocks, edges, self._get_safe_pins(),
                                        self._get_trigger_pins())
        # None = nothing was uploaded this session, so the GUI cannot know what
        # the board is running (it survives app restarts).
        matches = None if self._uploaded_ino is None else (ino == self._uploaded_ino)
        return {
            "saved_by": "Panopticon Stimulation Editor",
            "safe_low_pins": list(self._get_safe_pins()),
            "end_time_s": stim_compiler.end_time_s(blocks, edges),
            "matches_uploaded_firmware": matches,
            "firmware_sha256": hashlib.sha256(ino.encode()).hexdigest(),
            "chains": stim_compiler.describe(blocks, edges),
            "blocks": blocks,
            "edges": edges,
        }

    def firmware_source(self) -> str:
        return self._compile()

    def _on_field_enter(self):
        """Enter edits the selected block, or creates one when nothing is selected."""
        if self._selected_block is None:
            self._on_create()
            return
        try:
            self._selected_block.pin  = int(self._f_pin.text())
            self._selected_block.freq = float(self._f_freq.text())
            self._selected_block.pw   = float(self._f_pw.text())
            self._selected_block.dur  = float(self._f_dur.text())
            self._selected_block.update()
            self._set_status("")
        except ValueError:
            self._set_status("Invalid parameters.", error=True)

    # ── create ────────────────────────────────────────────────────────────────
    def _on_create(self):
        try:
            # No default for the pin. Coercing a blank field to "0" silently
            # created a block on pin 0 = UART RX0, which garbles the link to the
            # trigger board; and on a rig whose trigger pins start at 2 a
            # mistyped pin is far better refused than guessed.
            if not self._f_pin.text().strip():
                self._set_status("Enter a pin number.", error=True)
                return
            pin  = int(self._f_pin.text())
            freq = float(self._f_freq.text() or "0")
            pw   = float(self._f_pw.text()   or "0")
            dur  = float(self._f_dur.text()  or "1")
        except ValueError:
            self._set_status("Invalid parameters.", error=True)
            return
        self._canvas.add_block(pin, freq, pw, dur)

    def _on_clear(self):
        if QMessageBox.question(
            self, "Clear", "Remove all blocks and connections?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) == QMessageBox.Yes:
            self._canvas.clear()

    # ── save ─────────────────────────────────────────────────────────────────
    def _on_save(self):
        default = str(Path(self._get_output_dir()) / "stim_config.json")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Stimulus Config", default, "JSON (*.json)")
        if not path:
            return
        p = Path(path)
        if p.exists():
            if QMessageBox.question(
                self, "Overwrite?", f"{p.name} already exists. Overwrite?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return
        blocks, edges = self._canvas.get_workflow()
        p.write_text(json.dumps({"blocks": blocks, "edges": edges}, indent=2))
        self._set_status(f"Saved to {p.name}")

    # ── load ─────────────────────────────────────────────────────────────────
    def _on_load(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Stimulus Config",
            str(self._get_output_dir()), "JSON (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text())
            self._canvas.load_workflow(
                data.get("blocks", []), data.get("edges", []))
            self._set_status(f"Loaded {Path(path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Load failed", str(e))

    # ── apply (upload) ────────────────────────────────────────────────────────
    def _on_apply(self):
        problem = self._blocking_problem()
        if problem:
            QMessageBox.warning(self, "Cannot upload", problem)
            return
        self._start_upload(self._compile())

    def _start_upload(self, ino: str):
        if self._is_busy():
            QMessageBox.information(
                self, "Busy",
                "Something else is using the serial port — either a running "
                "acquisition or a firmware flash.\n\nWait for it to finish, or "
                "stop the acquisition, then upload again. Two uploads to the "
                "same port at once leave the board in an unknown state.")
            self._test_after_upload = False
            return
        self._apply_btn.setEnabled(False)
        self._test_btn.setEnabled(False)
        self._set_status("Compiling + uploading… (~30 s)")
        # arduino-cli needs the serial port to itself. NOT reopened lazily —
        # _on_upload_done retakes it immediately; see the comment there.
        self._release_serial()
        self._upload_worker = _UploadWorker(ino, self._get_port())
        self._upload_worker.done.connect(self._on_upload_done)
        self._upload_worker.start()

    @pyqtSlot(bool, str)
    def _on_upload_done(self, ok: bool, msg: str):
        self._apply_btn.setEnabled(True)
        self._test_btn.setEnabled(True)
        ino = getattr(self._upload_worker, "ino", None)
        self._upload_worker = None
        if not ok:
            self._test_after_upload = False
            self._apply_failed = True
            self._set_status("Upload failed — see details.", error=True)
            QMessageBox.critical(self, "Upload failed", msg)
            return
        self._apply_failed = False
        self._uploaded_ino = ino
        # Hand the applied paradigm to the main window. It records what the
        # board now holds and keeps the source for the session, so a later
        # calibration can reflash it away and a later recording can put it back
        # without the user pressing Apply again.
        try:
            self._on_applied(ino)
        except Exception as e:
            print(f"[stim] could not register the applied paradigm: {e}",
                  flush=True)
        # Retake the port straight away. Reopening resets the board, so letting
        # the next Record do it would put that flash back into the experiment;
        # here it lands during Apply, alongside the reset avrdude already did.
        self._get_serial()
        if self._test_after_upload:
            self._test_after_upload = False
            self._begin_test()
        else:
            self._set_status("Upload successful — press Record to run paradigm.")

    # ── test run (no cameras) ────────────────────────────────────────────────
    def _on_test(self):
        if self._test_timer is not None:
            self._end_test("Test stopped.")
            return
        if self._is_busy():
            QMessageBox.information(
                self, "Busy",
                "Stop the current acquisition before running a stimulation test.")
            return
        blocks, edges = self._canvas.get_workflow()
        if not blocks:
            QMessageBox.information(self, "Nothing to test",
                                    "Create at least one block first.")
            return
        problem = self._blocking_problem()
        if problem:
            QMessageBox.warning(self, "Cannot test", problem)
            return
        ino = self._compile()
        if ino != self._uploaded_ino:
            # The sequence lives in the sketch, so an un-uploaded edit would
            # silently test the previous paradigm.
            if QMessageBox.question(
                self, "Upload first?",
                "The workflow has changed since the last upload, so the board is "
                "still running the previous paradigm.\n\nUpload and then test? "
                "(~30 s)",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            ) != QMessageBox.Yes:
                return
            self._test_after_upload = True
            self._start_upload(ino)
            return
        self._begin_test()

    def _begin_test(self):
        port = self._get_port()
        # Prefer the main window's shared link so we don't reset the board (and
        # flash the laser) just to run a test; fall back to our own connection
        # when the editor is used standalone.
        shared = self._get_serial()
        if shared is not None:
            self._test_serial, self._test_owns_serial = shared, False
        else:
            self._test_serial = TeensyController(port=port)
            self._test_owns_serial = True
            if not self._test_serial.open():
                self._test_serial = None
                self._set_status(f"Could not open {port}.", error=True)
                QMessageBox.critical(
                    self, "Serial port",
                    f"Could not open {port}.\nClose the Arduino Serial Monitor or "
                    f"any other app holding it.")
                return
        # Zero camera pins: the sketch's cam loops iterate over nothing, so the
        # paradigm runs on its own with no TTLs going out to the cameras.
        if not self._test_serial.start_triggers([], int(self._get_fps())):
            self._end_test("Board did not acknowledge the test command.")
            QMessageBox.critical(
                self, "No response",
                "The trigger board did not acknowledge the start command, so the "
                "paradigm would not run.\n\nCheck the Arduino is connected and "
                "running the Panopticon sketch.")
            return
        dur = stim_compiler.test_duration_s(*self._canvas.get_workflow())
        # Absolute deadline rather than a per-tick subtraction, so a slow UI
        # thread can't stretch the run.
        self._test_end_at = (time.monotonic() + dur) if dur else None
        self._test_btn.setText("Stop Test")
        self._apply_btn.setEnabled(False)
        self._test_timer = QTimer(self)
        self._test_timer.timeout.connect(self._tick_test)
        self._test_timer.start(250)
        self._tick_test()

    def _tick_test(self):
        if self._test_end_at is None:
            self._set_status("Testing — looping, press Stop Test to end.")
            return
        left = self._test_end_at - time.monotonic()
        if left <= 0:
            self._end_test("Test complete.")
            return
        self._set_status(f"Testing — {left:.0f} s remaining.")

    def _end_test(self, message: str):
        if self._test_timer is not None:
            self._test_timer.stop()
            self._test_timer = None
        stopped = True
        if self._test_serial is not None:
            # The most laser-exposed stop in the application: a bench Test drives
            # the stim pin with no cameras and no recording, and a looping chain
            # has no end time — this single write is the ONLY thing that stops
            # it. Reporting "Test stopped." when the write failed is worse than
            # not reporting at all.
            stopped = self._test_serial.stop_triggers([])
            if self._test_owns_serial:      # never close the main window's link
                self._test_serial.close()
            self._test_serial = None
            self._test_owns_serial = False
        self._test_end_at = None
        self._test_btn.setText("Test")
        self._apply_btn.setEnabled(True)
        if not stopped:
            self._set_status("STOP NOT CONFIRMED — stim may still be running.",
                             error=True)
            QMessageBox.critical(
                self, "Stim may still be running",
                "The trigger board did not accept the stop command.\n\n"
                "Any stim chain — including a looping one, which never ends on "
                "its own — may still be driving its pin.\n\n"
                "Power-cycle the trigger board and key off the laser before "
                "continuing.")
        else:
            self._set_status(message)

    def closeEvent(self, event):
        if self._test_timer is not None:
            self._end_test("Test stopped.")
        super().closeEvent(event)

    def _set_status(self, text: str, error: bool = False):
        color = "#dd6666" if error else "#88aabb"
        self._status_lbl.setText(text)
        self._status_lbl.setStyleSheet(
            f"color: {color}; font-size: 11px; border: none;")

    def get_workflow(self) -> tuple[list[dict], list[dict]]:
        return self._canvas.get_workflow()
