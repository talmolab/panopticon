"""The zone editor: name zones and draw them on any camera, for paradigm
blocks ticked In zone.

A zone is the area a block listens to: the block pulses while a mouse is in
any of the zone's shapes. One laser needs one zone; a second zone is for a
block that should follow a different area.

The Stimulation editor opens it when a block is created with In zone ticked,
when In zone is ticked on a selected block, and on a double click on a zone
block. On the left is the list of zones (A, B, ...), each in its own colour,
with the camera it is drawn on. On the right, the cameras page shows every
camera live, each with the zones drawn on it; clicking one opens the draw
page for that camera, on the selected zone if it is drawn there, else the
camera's first zone, else a new zone. New zone starts a zone on the camera
being drawn on, or, from the cameras page, on the camera clicked next.

On the draw page, the tools are Select, Polygon, Rectangle and Ellipse:

- Polygon: a click per corner; a double click or Enter closes it, a right
  click takes back a corner, Escape drops it.
- Rectangle and Ellipse: drag from corner to corner; Shift keeps a square or
  a circle.
- Select: click a shape of the current zone to select it, drag it to move
  it, drag one of its handles to resize it (Shift keeps the proportions) or
  move a polygon's corner. Ctrl+C and Ctrl+V copy and paste, Delete removes.

Shapes of other zones on the same camera are shown dimmed; a click on one
with Select switches to its zone. The live tracker's mice are marked in lime. Coordinates are kept in
the camera's full-resolution pixels; the views only scale them.
"""
import copy

import numpy as np
from PyQt5.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (QBrush, QColor, QFont, QIcon, QImage,
                         QKeySequence, QPainter, QPainterPath, QPen,
                         QPixmap, QPolygonF)
from PyQt5.QtWidgets import (QButtonGroup, QDialog, QGraphicsItem,
                             QGraphicsPixmapItem, QGraphicsScene,
                             QGraphicsView, QGridLayout, QHBoxLayout, QLabel,
                             QListWidget, QListWidgetItem, QMessageBox,
                             QPushButton, QStackedWidget, QVBoxLayout,
                             QWidget)

from gui_app.closed_loop import (MOUSE_COLOUR, ZONE_COLOURS, Shape, Zone)
from gui_app.stim_compiler import ZONE_NAMES

#: Milliseconds between refreshes of the camera images and the mice.
REFRESH_MS = 150
#: Screen pixels a handle is drawn and grabbed at, whatever the zoom.
HANDLE_PX = 9
#: A drawn rectangle or ellipse smaller than this, in camera pixels, is a
#: stray click rather than a shape.
MIN_SHAPE_PX = 6
#: Camera pixels a pasted shape is moved by, so it is seen beside its source.
PASTE_OFFSET_PX = 40


def zone_colour(name: str, alpha: int = 255) -> QColor:
    c = QColor(ZONE_COLOURS.get(name, "#ffb000"))
    c.setAlpha(alpha)
    return c


def _btn(text: str, tip: str = "", checkable: bool = False) -> QPushButton:
    b = QPushButton(text)
    b.setToolTip(tip)
    b.setCheckable(checkable)
    b.setAutoDefault(False)
    b.setStyleSheet(
        "QPushButton { background: #2a2a4a; color: #88aadd; "
        "border: 1px solid #444; border-radius: 3px; padding: 5px 10px; }"
        "QPushButton:hover { background: #333366; border-color: #5078c8; }"
        "QPushButton:disabled { color: #555; border-color: #333; }"
        "QPushButton:checked { background: #4a3a1a; color: #f0c060; "
        "border-color: #a08030; }")
    return b


def _gray_image(frame) -> QImage | None:
    if frame is None:
        return None
    f = np.ascontiguousarray(frame)
    if f.ndim != 2:
        return None
    h, w = f.shape
    return QImage(f.data, w, h, w, QImage.Format_Grayscale8).copy()


def shape_path(shape: Shape) -> QPainterPath:
    """The outline of a shape, in camera pixels."""
    path = QPainterPath()
    if shape.kind == "polygon":
        if shape.points:
            path.addPolygon(QPolygonF([QPointF(x, y) for x, y in shape.points]))
            path.closeSubpath()
        return path
    x0, y0, x1, y1 = shape.bounds()
    r = QRectF(x0, y0, x1 - x0, y1 - y0)
    if shape.kind == "rect":
        path.addRect(r)
    else:
        path.addEllipse(r)
    return path


def paint_zone(p: QPainter, zone: Zone, to_view, scale: float,
               filled: bool = False, dim: bool = False) -> None:
    """Draw a zone's shapes onto `p`. `to_view` maps camera pixels to the
    painter's coordinates and `scale` is view pixels per camera pixel."""
    col = zone_colour(zone.name, 110 if dim else 255)
    p.setPen(QPen(col, 2))
    p.setBrush(QBrush(zone_colour(zone.name, 90 if filled else 35)))
    for s in zone.shapes:
        if not s.usable:
            continue
        if s.kind == "polygon":
            p.drawPolygon(QPolygonF([to_view(x, y) for x, y in s.points]))
            continue
        x0, y0, x1, y1 = s.bounds()
        a, b = to_view(x0, y0), to_view(x1, y1)
        r = QRectF(a, b).normalized()
        if s.kind == "rect":
            p.drawRect(r)
        else:
            p.drawEllipse(r)
    usable = [s for s in zone.shapes if s.usable]
    if usable:
        x0, y0, _x1, _y1 = usable[0].bounds()
        q = to_view(x0, y0)
        p.setPen(QPen(col, 1))
        p.setFont(QFont("Segoe UI", 9, QFont.Bold))
        p.drawText(QPointF(q.x() + 3, q.y() - 4), zone.name)


# ------------------------------------------------------------------ shapes
class ShapeItem(QGraphicsItem):
    """One shape of a zone on the draw page. Its geometry lives in ``geom``
    (camera pixels), not ``shape``, which is QGraphicsItem's hit-test
    method; the item draws it, and its handles while it is selected."""

    def __init__(self, shape: Shape, zone: str, editable: bool):
        super().__init__()
        self.geom = shape
        self.zone = zone
        self.editable = editable
        self.view_scale = 1.0
        flags = QGraphicsItem.ItemIsSelectable if editable else 0
        self.setFlags(QGraphicsItem.GraphicsItemFlag(flags))
        self.setZValue(2 if editable else 1)
        self._drag = None

    # Geometry --------------------------------------------------------
    def _handle_half(self) -> float:
        return HANDLE_PX / 2.0 / max(self.view_scale, 1e-6)

    def handles(self) -> list:
        """(handle id, point) for every handle: a polygon's corners, a box's
        corners and edge midpoints."""
        if self.geom.kind == "polygon":
            return [(i, QPointF(x, y)) for i, (x, y)
                    in enumerate(self.geom.points)]
        x0, y0, x1, y1 = self.geom.bounds()
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        return [("tl", QPointF(x0, y0)), ("t", QPointF(mx, y0)),
                ("tr", QPointF(x1, y0)), ("r", QPointF(x1, my)),
                ("br", QPointF(x1, y1)), ("b", QPointF(mx, y1)),
                ("bl", QPointF(x0, y1)), ("l", QPointF(x0, my))]

    def boundingRect(self) -> QRectF:
        h = self._handle_half() + 2
        return shape_path(self.geom).boundingRect().adjusted(-h, -h, h, h)

    def shape(self) -> QPainterPath:
        path = QPainterPath(shape_path(self.geom))
        if self.isSelected():
            h = self._handle_half()
            for _hid, pt in self.handles():
                path.addRect(QRectF(pt.x() - h, pt.y() - h, 2 * h, 2 * h))
        return path

    def paint(self, painter: QPainter, option, widget=None):
        col = zone_colour(self.zone, 255 if self.editable else 110)
        pen = QPen(col, 2)
        pen.setCosmetic(True)
        if self.isSelected():
            pen.setWidth(3)
        painter.setPen(pen)
        painter.setBrush(QBrush(zone_colour(self.zone,
                                            60 if self.editable else 25)))
        painter.drawPath(shape_path(self.geom))
        if self.isSelected():
            h = self._handle_half()
            painter.setBrush(QBrush(col))
            painter.setPen(Qt.NoPen)
            for _hid, pt in self.handles():
                painter.drawRect(QRectF(pt.x() - h, pt.y() - h, 2 * h, 2 * h))

    # Editing ---------------------------------------------------------
    def handle_at(self, pos: QPointF):
        if not self.isSelected():
            return None
        h = self._handle_half() * 1.5
        for hid, pt in self.handles():
            if abs(pos.x() - pt.x()) <= h and abs(pos.y() - pt.y()) <= h:
                return hid
        return None

    def mousePressEvent(self, event):
        if not self.editable or event.button() != Qt.LeftButton:
            event.ignore()
            return
        hid = self.handle_at(event.pos())
        if not self.isSelected():
            if not (event.modifiers() & Qt.ControlModifier):
                self.scene().clearSelection()
            self.setSelected(True)
        self._drag = (hid, event.pos(), copy.deepcopy(self.geom.points))
        event.accept()

    def mouseMoveEvent(self, event):
        if self._drag is None:
            return
        hid, start, pts = self._drag
        dx = event.pos().x() - start.x()
        dy = event.pos().y() - start.y()
        self.prepareGeometryChange()
        if hid is None:
            self.geom.points = [(x + dx, y + dy) for x, y in pts]
        elif self.geom.kind == "polygon":
            new = list(pts)
            new[hid] = (event.pos().x(), event.pos().y())
            self.geom.points = new
        else:
            self.geom.points = _resize_box(
                pts, hid, event.pos(),
                bool(event.modifiers() & Qt.ShiftModifier))
        self.update()

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            self._drag = None
            view = self.scene().views()[0] if self.scene().views() else None
            if view is not None and hasattr(view, "edited"):
                view.edited.emit()
        event.accept()


def _resize_box(pts, hid: str, pos: QPointF, keep_ratio: bool) -> list:
    """A box's two corners after dragging handle `hid` to `pos`."""
    (ax, ay), (bx, by) = pts
    x0, y0, x1, y1 = min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)
    x, y = pos.x(), pos.y()
    if "l" in hid:
        x0 = x
    if "r" in hid:
        x1 = x
    if hid.startswith("t"):
        y0 = y
    if hid.startswith("b"):
        y1 = y
    if keep_ratio and len(hid) == 2:
        side = max(abs(x1 - x0), abs(y1 - y0))
        if "l" in hid:
            x0 = x1 - side if x0 < x1 else x1 + side
        else:
            x1 = x0 + side if x1 > x0 else x0 - side
        if hid.startswith("t"):
            y0 = y1 - side if y0 < y1 else y1 + side
        else:
            y1 = y0 + side if y1 > y0 else y0 - side
    return [(min(x0, x1), min(y0, y1)), (max(x0, x1), max(y0, y1))]


# ------------------------------------------------------------- draw page
class DrawView(QGraphicsView):
    """The draw page: one camera, its zones, the current zone's shapes
    editable, and the tools that make new ones."""

    edited = pyqtSignal()
    #: A dimmed shape clicked with Select: the zone it belongs to.
    zone_clicked = pyqtSignal(str)

    def __init__(self, width: int, height: int, parent=None):
        super().__init__(parent)
        self.full_w, self.full_h = int(width), int(height)
        self.setScene(QGraphicsScene(0, 0, self.full_w, self.full_h, self))
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self.setBackgroundBrush(QBrush(QColor("#0e0e1c")))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self._bg = QGraphicsPixmapItem()
        self._bg.setZValue(0)
        self.scene().addItem(self._bg)
        self.tool = "select"
        self.zone: str | None = None      # the zone being edited
        self._draft: list | None = None   # polygon corners, or a box start
        self._preview: QPointF | None = None
        self._mice: list = []

    # ------------------------------------------------------------ content
    def set_frame(self, frame) -> None:
        img = _gray_image(frame)
        if img is None:
            return
        self._bg.setPixmap(QPixmap.fromImage(img))
        self._bg.setScale(self.full_w / max(img.width(), 1))

    def set_mice(self, dets) -> None:
        self._mice = list(dets or [])
        self.viewport().update()

    def show_zones(self, zones: dict, camera: str, current: str | None):
        """Put every zone of `camera` on the page; `current`'s shapes are the
        editable ones."""
        for it in [i for i in self.scene().items() if isinstance(i, ShapeItem)]:
            self.scene().removeItem(it)
        self.zone = current
        for name, z in sorted(zones.items()):
            if z.camera != camera:
                continue
            for s in z.shapes:
                it = ShapeItem(s, name, editable=(name == current))
                it.view_scale = self._scale()
                self.scene().addItem(it)
        self.cancel_draft()

    def current_items(self) -> list:
        return [i for i in self.scene().items()
                if isinstance(i, ShapeItem) and i.editable]

    def selected_shapes(self) -> list:
        return [i for i in self.scene().selectedItems()
                if isinstance(i, ShapeItem) and i.editable]

    # --------------------------------------------------------------- view
    def _scale(self) -> float:
        return self.transform().m11()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fitInView(QRectF(0, 0, self.full_w, self.full_h),
                       Qt.KeepAspectRatio)
        s = self._scale()
        for it in self.scene().items():
            if isinstance(it, ShapeItem):
                it.prepareGeometryChange()
                it.view_scale = s

    def drawForeground(self, p: QPainter, rect: QRectF):
        s = max(self._scale(), 1e-6)
        r = 7.0 / s
        pen = QPen(QColor(MOUSE_COLOUR), 2)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        for x, y, _c in self._mice:
            p.drawEllipse(QPointF(x, y), r, r)
        if self._draft and self.zone:
            col = zone_colour(self.zone)
            dpen = QPen(col, 2, Qt.DashLine)
            dpen.setCosmetic(True)
            p.setPen(dpen)
            if self.tool == "polygon":
                pts = [QPointF(x, y) for x, y in self._draft]
                if self._preview is not None:
                    pts = pts + [self._preview]
                for a, b in zip(pts, pts[1:]):
                    p.drawLine(a, b)
                p.setBrush(QBrush(col))
                for q in pts[:len(self._draft)]:
                    p.drawEllipse(q, 3.0 / s, 3.0 / s)
            elif self._preview is not None:
                box = Shape(self.tool, [self._draft[0],
                                        (self._preview.x(), self._preview.y())])
                p.drawPath(shape_path(box))

    # -------------------------------------------------------------- tools
    def set_tool(self, tool: str) -> None:
        self.tool = tool
        self.cancel_draft()
        self.setCursor(Qt.ArrowCursor if tool == "select" else Qt.CrossCursor)

    def cancel_draft(self) -> None:
        self._draft = None
        self._preview = None
        self.viewport().update()

    def _clamp(self, pt: QPointF) -> QPointF:
        return QPointF(min(max(pt.x(), 0.0), self.full_w),
                       min(max(pt.y(), 0.0), self.full_h))

    def _add(self, shape: Shape) -> None:
        if not self.zone:
            return
        it = ShapeItem(shape, self.zone, editable=True)
        it.view_scale = self._scale()
        self.scene().addItem(it)
        self.scene().clearSelection()
        it.setSelected(True)
        self.edited.emit()

    def mousePressEvent(self, event):
        pos = self._clamp(self.mapToScene(event.pos()))
        if self.tool == "select" or not self.zone:
            other = self._other_zone_at(pos)
            if other is not None and event.button() == Qt.LeftButton:
                self.zone_clicked.emit(other)
                return
            super().mousePressEvent(event)
            return
        if self.tool == "polygon":
            if event.button() == Qt.RightButton:
                if self._draft:
                    self._draft.pop()
                    if not self._draft:
                        self._draft = None
                self.viewport().update()
                return
            if event.button() == Qt.LeftButton:
                self._draft = (self._draft or []) + [(pos.x(), pos.y())]
                self.viewport().update()
            return
        if event.button() == Qt.LeftButton:
            self._draft = [(pos.x(), pos.y())]
            self._preview = pos

    def _other_zone_at(self, pos: QPointF) -> str | None:
        """The zone of the dimmed shape under `pos`, or None when the top
        shape there is the current zone's, or there is none."""
        for it in self.scene().items(pos):     # topmost first
            if isinstance(it, ShapeItem):
                return None if it.editable else it.zone
        return None

    def mouseMoveEvent(self, event):
        pos = self._clamp(self.mapToScene(event.pos()))
        if self.tool in ("rect", "ellipse") and self._draft:
            if event.modifiers() & Qt.ShiftModifier:
                x0, y0 = self._draft[0]
                side = max(abs(pos.x() - x0), abs(pos.y() - y0))
                pos = QPointF(x0 + side * (1 if pos.x() >= x0 else -1),
                              y0 + side * (1 if pos.y() >= y0 else -1))
            self._preview = pos
            self.viewport().update()
            return
        if self.tool == "polygon" and self._draft:
            self._preview = pos
            self.viewport().update()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if (self.tool in ("rect", "ellipse") and self._draft
                and self._preview is not None
                and event.button() == Qt.LeftButton):
            x0, y0 = self._draft[0]
            x1, y1 = self._preview.x(), self._preview.y()
            self.cancel_draft()
            if abs(x1 - x0) >= MIN_SHAPE_PX and abs(y1 - y0) >= MIN_SHAPE_PX:
                self._add(Shape(self.tool, [(min(x0, x1), min(y0, y1)),
                                            (max(x0, x1), max(y0, y1))]))
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.tool == "polygon" and self._draft:
            self.finish_polygon()
            return
        super().mouseDoubleClickEvent(event)

    def finish_polygon(self) -> None:
        pts = self._draft or []
        # A double click lands its corner twice; keep one.
        pts = [q for i, q in enumerate(pts) if i == 0 or q != pts[i - 1]]
        self.cancel_draft()
        if len(pts) >= 3:
            self._add(Shape("polygon", pts))

    def keyPressEvent(self, event):
        if self.tool == "polygon" and self._draft:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                self.finish_polygon()
                return
            if event.key() == Qt.Key_Escape:
                self.cancel_draft()
                return
        super().keyPressEvent(event)


# ------------------------------------------------------------ cameras page
class CameraTile(QWidget):
    """One camera's live view with the zones drawn on it; a click picks it."""

    clicked = pyqtSignal(int)

    def __init__(self, index: int, width: int, height: int, parent=None):
        super().__init__(parent)
        self.index = index
        self.full_w, self.full_h = width, height
        self._image: QImage | None = None
        self._zones: list = []
        self.setMinimumSize(200, 125)
        self.setCursor(Qt.PointingHandCursor)

    def set_frame(self, frame) -> None:
        img = _gray_image(frame)
        if img is not None:
            self._image = img
            self.update()

    def set_zones(self, zones: list) -> None:
        self._zones = zones
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.index)

    def _rect(self) -> QRectF:
        s = min(self.width() / self.full_w, self.height() / self.full_h)
        w, h = self.full_w * s, self.full_h * s
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor("#0e0e1c"))
        r = self._rect()
        if self._image is not None:
            p.drawImage(r, self._image)
        k = r.width() / self.full_w
        for z in self._zones:
            paint_zone(p, z, lambda x, y: QPointF(r.x() + x * k,
                                                  r.y() + y * k), k)
        p.setPen(QColor("#dcdcdc"))
        p.setFont(QFont("Segoe UI", 10, QFont.Bold))
        p.drawText(QPointF(r.x() + 6, r.y() + 16), f"cam{self.index + 1}")


# ------------------------------------------------------------ the dialog
class ZoneEditor(QDialog):
    """Name zones, draw them on cameras, and pick the zone a block follows.

    ``zones`` is a working copy: the owner reads ``result_zones()`` and
    ``chosen()`` after ``exec_()`` returns Accepted.
    """

    def __init__(self, zones: dict, width: int, height: int, n_cameras: int,
                 get_frames, get_tracking=None, watch=None,
                 current: str | None = None, choose: bool = False,
                 locked: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Zones")
        self.setMinimumSize(980, 640)
        self.resize(1300, 820)
        self.setStyleSheet("background: #141428; color: #dcdcdc;")
        self._zones = {n: copy.deepcopy(z) for n, z in zones.items()}
        self._w, self._h = int(width), int(height)
        self._n = int(n_cameras)
        self._get_frames = get_frames
        self._get_tracking = get_tracking or (lambda: {})
        self._watch = watch or (lambda cams: None)
        self._locked = bool(locked)
        self._clipboard: list = []
        self._cam: int | None = None
        # New zone pressed on the cameras page: the next camera clicked
        # starts a zone, even one that already has zones.
        self._pending_new = False

        root = QHBoxLayout(self)
        left = QVBoxLayout()
        title = QLabel("Zones")
        title.setFont(QFont("Segoe UI", 11, QFont.Bold))
        left.addWidget(title)
        self.zone_list = QListWidget()
        self.zone_list.setMinimumWidth(240)
        self.zone_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.zone_list.setStyleSheet(
            "QListWidget { background: #1a1a30; border: 1px solid #333; }"
            "QListWidget::item:selected { background: #3a3a60; }")
        self.zone_list.currentItemChanged.connect(self._on_zone_picked)
        left.addWidget(self.zone_list, 1)
        self.new_btn = _btn("New zone", "Pick a camera, then draw the new "
                            "zone on it")
        self.new_btn.clicked.connect(self._on_new_zone)
        self.del_btn = _btn("Delete zone", "Remove the selected zone and its "
                            "shapes")
        self.del_btn.clicked.connect(self._on_delete_zone)
        left.addWidget(self.new_btn)
        left.addWidget(self.del_btn)
        about = QLabel("A zone is the area a block listens to: the block "
                       "pulses while a mouse is in any of the zone's shapes. "
                       "One laser needs one zone. Make another only for a "
                       "block that should follow a different area.")
        about.setWordWrap(True)
        about.setMaximumWidth(240)
        about.setStyleSheet("color: #8a8aa8; font-size: 11px;")
        left.addWidget(about)
        root.addLayout(left, 0)

        right = QVBoxLayout()
        self.pages = QStackedWidget()
        # Cameras page.
        cams_page = QWidget()
        cv = QVBoxLayout(cams_page)
        self.cams_hint = QLabel("Click a camera to draw a zone on it.")
        self.cams_hint.setStyleSheet("color: #aab;")
        cv.addWidget(self.cams_hint)
        grid = QGridLayout()
        cols = max(1, int(np.ceil(np.sqrt(self._n))))
        self.tiles = []
        for i in range(self._n):
            t = CameraTile(i, self._w, self._h)
            t.clicked.connect(self._on_camera_clicked)
            grid.addWidget(t, i // cols, i % cols)
            self.tiles.append(t)
        cv.addLayout(grid, 1)
        self.pages.addWidget(cams_page)
        # Draw page.
        draw_page = QWidget()
        dv = QVBoxLayout(draw_page)
        tools = QHBoxLayout()
        self.back_btn = _btn("Cameras", "Back to every camera")
        self.back_btn.clicked.connect(self.show_cameras)
        tools.addWidget(self.back_btn)
        self.cam_label = QLabel("")
        self.cam_label.setFont(QFont("Segoe UI", 10, QFont.Bold))
        tools.addWidget(self.cam_label)
        tools.addSpacing(16)
        self.tool_group = QButtonGroup(self)
        self.tool_btns = {}
        for key, text, tip in (
                ("select", "Select", "Pick, move and resize the zone's "
                 "shapes"),
                ("polygon", "Polygon", "A click per corner; double click or "
                 "Enter to close"),
                ("rect", "Rectangle", "Drag corner to corner; Shift for a "
                 "square"),
                ("ellipse", "Ellipse", "Drag corner to corner; Shift for a "
                 "circle")):
            b = _btn(text, tip, checkable=True)
            b.clicked.connect(lambda _c, k=key: self.set_tool(k))
            self.tool_group.addButton(b)
            self.tool_btns[key] = b
            tools.addWidget(b)
        tools.addSpacing(16)
        self.copy_btn = _btn("Copy", "Copy the selected shapes (Ctrl+C)")
        self.copy_btn.clicked.connect(self.copy_selected)
        self.paste_btn = _btn("Paste", "Paste into the zone (Ctrl+V)")
        self.paste_btn.clicked.connect(self.paste)
        self.delete_btn = _btn("Delete", "Remove the selected shapes "
                               "(Delete)")
        self.delete_btn.clicked.connect(self.delete_selected)
        for b in (self.copy_btn, self.paste_btn, self.delete_btn):
            tools.addWidget(b)
        tools.addStretch(1)
        dv.addLayout(tools)
        self.view = DrawView(self._w, self._h)
        self.view.edited.connect(self._sync_from_view)
        self.view.zone_clicked.connect(self._on_shape_zone_clicked)
        dv.addWidget(self.view, 1)
        self.pages.addWidget(draw_page)
        right.addWidget(self.pages, 1)

        bottom = QHBoxLayout()
        self.status = QLabel("")
        self.status.setStyleSheet("color: #aab; font-size: 11px;")
        bottom.addWidget(self.status, 1)
        self.cancel_btn = _btn("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        self.ok_btn = _btn("Done")
        self.ok_btn.clicked.connect(self._on_done)
        bottom.addWidget(self.cancel_btn)
        bottom.addWidget(self.ok_btn)
        right.addLayout(bottom)
        root.addLayout(right, 1)

        self._choose = bool(choose)
        self._fill_list(current)
        if current in self._zones:
            self._open_zone(current)
        else:
            self.show_cameras()
        self._sync_buttons()

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._refresh()

    # ---------------------------------------------------------- results
    def result_zones(self) -> dict:
        self._sync_from_view()
        return {n: z for n, z in self._zones.items()}

    def chosen(self) -> str | None:
        item = self.zone_list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    # ------------------------------------------------------------- zones
    def _fill_list(self, select: str | None = None) -> None:
        self.zone_list.blockSignals(True)
        self.zone_list.clear()
        for name in sorted(self._zones):
            z = self._zones[name]
            n = sum(1 for s in z.shapes if s.usable)
            item = QListWidgetItem(f"Zone {name}  ·  {z.camera}  ·  {n} "
                                   f"shape{'s' if n != 1 else ''}")
            pm = QPixmap(14, 14)
            pm.fill(zone_colour(name))
            item.setIcon(QIcon(pm))
            item.setData(Qt.UserRole, name)
            self.zone_list.addItem(item)
            if name == select:
                self.zone_list.setCurrentItem(item)
        self.zone_list.blockSignals(False)

    def _select_in_list(self, name: str) -> None:
        self.zone_list.blockSignals(True)
        for i in range(self.zone_list.count()):
            if self.zone_list.item(i).data(Qt.UserRole) == name:
                self.zone_list.setCurrentRow(i)
        self.zone_list.blockSignals(False)

    def _next_name(self) -> str | None:
        for c in ZONE_NAMES:
            if c not in self._zones:
                return c
        return None

    def _on_new_zone(self) -> None:
        """A new zone on the camera being drawn on, or, from the cameras
        page, on the camera clicked next."""
        if self._locked:
            return
        if self.pages.currentIndex() == 1 and self._cam is not None:
            self._new_zone_on(self._cam)
            return
        self._sync_from_view()
        self._pending_new = True
        self.zone_list.blockSignals(True)
        self.zone_list.setCurrentItem(None)
        self.zone_list.blockSignals(False)
        self.show_cameras("Click the camera to draw the new zone on.")
        self._sync_buttons()

    def _new_zone_on(self, index: int) -> None:
        self._sync_from_view()
        name = self._next_name()
        if name is None:
            QMessageBox.information(self, "No zone left",
                                    f"Every zone, {ZONE_NAMES[0]} to "
                                    f"{ZONE_NAMES[-1]}, is in use.")
            return
        self._zones[name] = Zone(name, f"cam{index + 1}", [])
        self._fill_list(name)
        self._open_zone(name)
        self._sync_buttons()

    def _on_delete_zone(self) -> None:
        name = self.chosen()
        if self._locked or name is None:
            return
        if QMessageBox.question(
                self, "Delete zone",
                f"Delete zone {name} and its shapes? Blocks that follow it "
                f"will need another zone before the paradigm can be applied.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        ) != QMessageBox.Yes:
            return
        self._zones.pop(name, None)
        self._fill_list()
        self.show_cameras()

    def _on_zone_picked(self, item, _prev=None) -> None:
        # The name first: saving the previous zone rebuilds the list, which
        # deletes this item.
        name = item.data(Qt.UserRole) if item is not None else None
        self._sync_from_view()
        if name in self._zones:
            self._open_zone(name)
        self._sync_buttons()

    def _on_camera_clicked(self, index: int) -> None:
        """A camera picked on the cameras page. After New zone it starts a
        zone there. Otherwise it opens the selected zone if that is drawn on
        this camera, else the camera's first zone, else starts a zone."""
        self._sync_from_view()
        cam = f"cam{index + 1}"
        new, self._pending_new = self._pending_new, False
        if not new:
            sel = self.chosen()
            on_cam = sorted(n for n, z in self._zones.items()
                            if z.camera == cam)
            if sel in on_cam:
                self._open_zone(sel)
                return
            if on_cam:
                self._open_zone(on_cam[0])
                return
        if self._locked:
            self._show_camera(index, None)
            return
        self._new_zone_on(index)

    def _on_shape_zone_clicked(self, name: str) -> None:
        """A dimmed shape clicked on the draw page: edit its zone."""
        self._sync_from_view()
        if name in self._zones:
            self._open_zone(name)
        self._sync_buttons()

    def _open_zone(self, name: str) -> None:
        z = self._zones[name]
        self._select_in_list(name)
        self._show_camera(z.cam_index, name)
        if not self._locked:
            self.set_tool("select" if not z.empty else "rect")

    def _show_camera(self, index: int, zone: str | None) -> None:
        self._cam = index
        self.cam_label.setText(f"cam{index + 1}" + (
            f"  ·  zone {zone}" if zone else ""))
        self.view.show_zones(self._zones, f"cam{index + 1}",
                             None if self._locked else zone)
        self.pages.setCurrentIndex(1)
        self._watch({index} | {z.cam_index for z in self._zones.values()})

    def show_cameras(self, hint: str | None = None) -> None:
        self._sync_from_view()
        self._cam = None
        # The draw page holds no zone while hidden, so no later sync writes
        # its shapes back, or reselects its zone.
        self.view.zone = None
        self.cams_hint.setText(hint or "Click a camera to draw a zone on it.")
        self.pages.setCurrentIndex(0)
        self._watch({z.cam_index for z in self._zones.values()})
        self._sync_buttons()

    def _sync_from_view(self) -> None:
        """Copy the draw page's shapes back into the current zone."""
        name = self.view.zone
        if name is None or name not in self._zones or self._cam is None:
            return
        self._zones[name].shapes = [it.geom for it in
                                    self.view.current_items()]
        sel = self.chosen()
        self._fill_list(sel if sel in self._zones else name)

    # ------------------------------------------------------------- tools
    def set_tool(self, tool: str) -> None:
        if self._locked:
            tool = "select"
        self.view.set_tool(tool)
        for k, b in self.tool_btns.items():
            b.setChecked(k == tool)

    def copy_selected(self) -> None:
        self._clipboard = [copy.deepcopy(it.geom)
                           for it in self.view.selected_shapes()]

    def paste(self) -> None:
        if self._locked or not self._clipboard or self.view.zone is None:
            return
        d = PASTE_OFFSET_PX
        for s in self._clipboard:
            pts = [(min(max(x + d, 0), self._w), min(max(y + d, 0), self._h))
                   for x, y in s.points]
            self.view._add(Shape(s.kind, pts))
        # A second paste lands beside the first.
        self._clipboard = [Shape(s.kind, [(x + d, y + d) for x, y in s.points])
                           for s in self._clipboard]
        self._sync_from_view()

    def delete_selected(self) -> None:
        if self._locked:
            return
        for it in self.view.selected_shapes():
            self.view.scene().removeItem(it)
        self._sync_from_view()

    def keyPressEvent(self, event):
        if self.pages.currentIndex() == 1:
            if event.matches(QKeySequence.Copy):
                self.copy_selected()
                return
            if event.matches(QKeySequence.Paste):
                self.paste()
                return
            if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
                self.delete_selected()
                return
            if event.key() == Qt.Key_Escape and self.view._draft:
                self.view.cancel_draft()
                return
        super().keyPressEvent(event)

    def _sync_buttons(self) -> None:
        on_draw = self.pages.currentIndex() == 1
        editable = not self._locked and self.view.zone is not None
        for b in self.tool_btns.values():
            b.setEnabled(on_draw and editable)
        for b in (self.copy_btn, self.paste_btn, self.delete_btn):
            b.setEnabled(on_draw and editable)
        self.new_btn.setEnabled(not self._locked)
        self.del_btn.setEnabled(not self._locked and self.chosen() is not None)
        name = self.chosen()
        if self._choose:
            self.ok_btn.setText(f"Use zone {name}" if name else "Done")
            self.ok_btn.setEnabled(name is not None)
        else:
            self.ok_btn.setText("Done")

    # ------------------------------------------------------------- live
    def _refresh(self) -> None:
        try:
            frames = list(self._get_frames() or [])
        except Exception:
            frames = []
        track = {}
        try:
            track = self._get_tracking() or {}
        except Exception:
            pass
        if self.pages.currentIndex() == 0:
            for t in self.tiles:
                if t.index < len(frames):
                    t.set_frame(frames[t.index])
                t.set_zones([z for z in self._zones.values()
                             if z.cam_index == t.index])
        elif self._cam is not None:
            if self._cam < len(frames):
                self.view.set_frame(frames[self._cam])
            cam = (track.get("cameras") or {}).get(f"cam{self._cam + 1}", {})
            self.view.set_mice(cam.get("dets", []))
        status = track.get("status")
        if status == "running":
            cam = (track.get("cameras") or {}).get(
                f"cam{(self._cam or 0) + 1}", {})
            self.status.setText(
                f"Tracker running · cam{(self._cam or 0) + 1} tracked "
                f"{cam.get('hz', 0):.1f} times a second, "
                f"{cam.get('ms', 0):.1f} ms a frame, "
                f"{len(cam.get('dets', []))} mice" if self._cam is not None
                else "Tracker running")
        elif status == "error":
            self.status.setText(f"Tracker error: {track.get('error')}")
        elif status == "starting":
            self.status.setText("Starting the tracker…")
        else:
            self.status.setText("The tracker is not running: the zones can "
                                "be drawn, and mice are not marked.")
        if self._locked:
            self.status.setText("A recording is sending these zones: they "
                                "cannot change until it ends.  "
                                + self.status.text())
        self._sync_buttons()

    def _on_done(self) -> None:
        self._sync_from_view()
        name = self.chosen()
        if self._choose and (name is None or self._zones[name].empty):
            QMessageBox.information(
                self, "Draw the zone first",
                "Pick a zone with at least one shape for the block to "
                "follow, or draw one.")
            return
        self.accept()

    def done(self, result):
        self._timer.stop()
        self._watch(set())
        super().done(result)
