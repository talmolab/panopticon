"""The Zone window: draw the closed loop's zone on the tracked camera, and
watch the live tracker find the mice.

The view is the tracked camera's preview, with the zone's polygons over it
and each mouse the tracker found: amber inside the zone, cyan outside. Draw
adds a polygon one click per corner; a double click, Enter or Finish closes
it, a right click takes back the last corner and Escape drops the polygon.
Any polygon counts as the zone. Save and Load keep a zone in a file, because
nothing restores one at launch: the zone decides what reaches the board.

While a recording runs the zone cannot be edited; the window only watches.
"""
import time

import numpy as np
from PyQt5.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPen, QPolygonF
from PyQt5.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel,
                             QMessageBox, QPushButton, QVBoxLayout, QWidget)

from gui_app.closed_loop import Zone

#: Milliseconds between refreshes of the view and the status line.
REFRESH_MS = 100
#: Seconds after which the last tracked frame's marks are drawn dashed.
STALE_S = 1.0

IN_COLOUR = QColor("#f0b040")
OUT_COLOUR = QColor("#40c8e8")
ZONE_FILL = QColor(240, 176, 64, 50)
ZONE_EDGE = QColor("#f0b040")
DRAFT = QColor("#ffffff")

GUIDANCE = (
    "Draw the zone over the arena: Draw, then click each corner, and double "
    "click (or Enter) to close it. A right click takes back a corner. Any "
    "polygon counts. Check the marks follow the mice before you record: "
    "amber in the zone, cyan outside.")


def _btn(text: str, tip: str = "") -> QPushButton:
    b = QPushButton(text)
    b.setToolTip(tip)
    b.setStyleSheet(
        "QPushButton { background: #2a2a4a; color: #88aadd; "
        "border: 1px solid #444; border-radius: 3px; padding: 5px 10px; }"
        "QPushButton:hover { background: #333366; border-color: #5078c8; }"
        "QPushButton:disabled { color: #555; border-color: #333; }"
        "QPushButton:checked { background: #4a3a1a; color: #f0c060; "
        "border-color: #a08030; }")
    return b


class ZoneView(QWidget):
    """The camera's view scaled to fit, the zone, the draft polygon and the
    tracked mice. Coordinates are kept in the camera's full-resolution
    pixels; the view only scales them."""

    def __init__(self, width: int, height: int, parent=None):
        super().__init__(parent)
        self.full_w, self.full_h = int(width), int(height)
        self.setMinimumSize(640, 400)
        self.setMouseTracking(True)
        self._image: QImage | None = None
        self._caption = "Waiting for the camera…"
        self.polygons: list = []
        self.draft: list | None = None
        self._dets: list = []
        self._inside: list = []
        self._stale = True
        self._banner = ""
        self.on_finish = None

    # ------------------------------------------------------------- content
    def set_frame(self, frame) -> None:
        if frame is None:
            return
        f = np.ascontiguousarray(frame)
        h, w = f.shape[:2]
        self._image = QImage(f.data, w, h, w, QImage.Format_Grayscale8).copy()
        self.update()

    def set_marks(self, dets, inside, stale: bool, banner: str) -> None:
        self._dets, self._inside = list(dets), list(inside)
        self._stale, self._banner = stale, banner
        self.update()

    def image_rect(self) -> QRectF:
        s = min(self.width() / self.full_w, self.height() / self.full_h)
        w, h = self.full_w * s, self.full_h * s
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _to_view(self, x: float, y: float) -> QPointF:
        r = self.image_rect()
        k = r.width() / self.full_w
        return QPointF(r.x() + x * k, r.y() + y * k)

    def _to_full(self, pos) -> tuple | None:
        r = self.image_rect()
        if not r.contains(pos):
            return None
        k = self.full_w / r.width()
        return ((pos.x() - r.x()) * k, (pos.y() - r.y()) * k)

    # ------------------------------------------------------------- drawing
    def mousePressEvent(self, event):
        if self.draft is None:
            return
        if event.button() == Qt.RightButton:
            if self.draft:
                self.draft.pop()
                self.update()
            return
        pt = self._to_full(event.pos())
        if pt is not None and event.button() == Qt.LeftButton:
            self.draft.append(pt)
            self.update()

    def mouseDoubleClickEvent(self, event):
        # The double click's first press already added this corner.
        if self.draft is not None and self.on_finish is not None:
            self.on_finish()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor("#0e0e1c"))
        target = self.image_rect()
        if self._image is not None:
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawImage(target, self._image)
        else:
            p.setPen(QColor("#889"))
            p.drawText(self.rect(), Qt.AlignCenter, self._caption)
        for poly in self.polygons:
            if len(poly) < 3:
                continue
            p.setPen(QPen(ZONE_EDGE, 2))
            p.setBrush(QBrush(ZONE_FILL))
            p.drawPolygon(QPolygonF([self._to_view(x, y) for x, y in poly]))
        if self.draft:
            p.setPen(QPen(DRAFT, 1.5, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            pts = [self._to_view(x, y) for x, y in self.draft]
            for a, b in zip(pts, pts[1:]):
                p.drawLine(a, b)
            for q in pts:
                p.drawEllipse(q, 3, 3)
        for (x, y, c), inside in zip(self._dets, self._inside):
            colour = IN_COLOUR if inside else OUT_COLOUR
            pen = QPen(colour, 2)
            if self._stale:
                pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            q = self._to_view(x, y)
            p.drawEllipse(q, 9, 9)
            p.drawText(QPointF(q.x() + 11, q.y() - 6), f"{c:.2f}")
        if self._banner:
            p.setFont(QFont("Segoe UI", 11, QFont.Bold))
            p.setPen(QColor("#ffffff"))
            p.drawText(QPointF(target.x() + 10, target.y() + 22), self._banner)


class ZoneWindow(QDialog):
    """The closed loop's zone and live tracking for one camera.

    ``zone_changed`` carries the new Zone (None for none) after every edit,
    load or clear; the owner hands it to the closed loop. ``closed`` is sent
    once, whichever way the window ends.
    """

    zone_changed = pyqtSignal(object)
    closed = pyqtSignal()

    def __init__(self, spec, camera_mgr, loop, zone: Zone | None,
                 width: int, height: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Zone · {spec.camera}")
        self.setMinimumSize(760, 560)
        self.resize(1100, 780)
        self.setStyleSheet("background: #141428; color: #dcdcdc;")
        self._spec = spec
        self._cam = int(spec.camera[3:]) - 1
        self._mgr = camera_mgr
        self._loop = loop
        self._w, self._h = int(width), int(height)
        self._locked = False
        self._closed_sent = False

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)
        guide = QLabel(GUIDANCE)
        guide.setWordWrap(True)
        guide.setStyleSheet("color: #aab; font-size: 12px;")
        root.addWidget(guide)
        self.view = ZoneView(width, height)
        self.view.polygons = [list(p) for p in (zone.polygons if zone else [])]
        self.view.on_finish = self._finish
        root.addWidget(self.view, 1)

        row = QHBoxLayout()
        self._draw = _btn("Draw", "Add a polygon to the zone, one click per "
                          "corner")
        self._draw.setCheckable(True)
        self._draw.toggled.connect(self._on_draw)
        self._finish_btn = _btn("Finish", "Close the polygon being drawn")
        self._finish_btn.clicked.connect(self._finish)
        self._clear = _btn("Clear", "Remove every polygon")
        self._clear.clicked.connect(self._on_clear)
        self._save = _btn("Save…", "Keep this zone in a file")
        self._save.clicked.connect(self._on_save)
        self._load = _btn("Load…", "Use a zone saved earlier for this camera")
        self._load.clicked.connect(self._on_load)
        for b in (self._draw, self._finish_btn, self._clear, self._save,
                  self._load):
            row.addWidget(b)
        row.addStretch(1)
        self._status = QLabel("")
        self._status.setStyleSheet("color: #aab; font-size: 11px;")
        row.addWidget(self._status)
        root.addLayout(row)
        for b in self.findChildren(QPushButton):
            b.setAutoDefault(False)
            b.setDefault(False)
        self._sync_buttons()

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    # ------------------------------------------------------------ the zone
    def zone(self) -> Zone | None:
        polys = [p for p in self.view.polygons if len(p) >= 3]
        return Zone(self._spec.camera, self._w, self._h, polys) if polys else None

    def _emit(self) -> None:
        self.view.update()
        self.zone_changed.emit(self.zone())

    def set_locked(self, locked: bool) -> None:
        """Lock editing while a recording runs, so the zone the recording
        started with is the zone it records."""
        self._locked = bool(locked)
        if locked and self.view.draft is not None:
            self._draw.setChecked(False)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        drawing = self.view.draft is not None
        for b in (self._draw, self._clear, self._load):
            b.setEnabled(not self._locked)
        self._finish_btn.setEnabled(drawing and not self._locked)
        self._save.setEnabled(bool(self.zone()))

    def _on_draw(self, on: bool) -> None:
        self.view.draft = [] if on else None
        self.view.setCursor(Qt.CrossCursor if on else Qt.ArrowCursor)
        self.view.update()
        self._sync_buttons()

    def _finish(self) -> None:
        draft = self.view.draft
        if draft is None:
            return
        # A double click lands its corner twice; keep one.
        pts = [pt for i, pt in enumerate(draft) if i == 0 or pt != draft[i - 1]]
        if len(pts) >= 3:
            self.view.polygons.append(pts)
            self._emit()
        self._draw.setChecked(False)

    def keyPressEvent(self, event):
        if self.view.draft is not None:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                self._finish()
                return
            if event.key() == Qt.Key_Escape:
                self._draw.setChecked(False)
                return
        super().keyPressEvent(event)

    def _on_clear(self) -> None:
        self.view.polygons = []
        self._emit()
        self._sync_buttons()

    def _on_save(self) -> None:
        zone = self.zone()
        if zone is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save zone", f"zone_{self._spec.camera}.json",
            "Zone (*.json)")
        if path:
            try:
                zone.save(path)
            except OSError as e:
                QMessageBox.warning(self, "Zone not saved", str(e))

    def _on_load(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load zone", "",
                                              "Zone (*.json)")
        if not path:
            return
        try:
            zone = Zone.load(path, self._spec.camera, self._w, self._h)
        except (OSError, ValueError, KeyError, TypeError) as e:
            QMessageBox.warning(self, "Zone not loaded",
                                f"{path} cannot be used:\n\n{e}")
            return
        self.view.polygons = [list(p) for p in zone.polygons]
        self._emit()
        self._sync_buttons()

    # ------------------------------------------------------------- the view
    def _refresh(self) -> None:
        frames = []
        try:
            frames = self._mgr.latest_frames
        except Exception:
            pass
        if self._cam < len(frames):
            self.view.set_frame(frames[self._cam])
        snap = self._loop.snapshot() if self._loop is not None else {}
        status = snap.get("status", "off")
        if status == "error":
            banner = "Tracker stopped"
            text = f"Tracker error: {snap.get('error')}"
        elif status == "starting":
            banner = ""
            text = "Starting the tracker…"
        elif status == "running":
            dets = snap.get("dets", [])
            state = snap.get("state", False)
            if self.zone() is None:
                banner = "No zone: draw one"
            else:
                banner = "Mouse in zone" if state else "Zone empty"
            if snap.get("armed"):
                banner += " · sent to the board"
            text = (f"{self._spec.camera} tracked {snap.get('hz', 0):.1f} "
                    f"times a second · {snap.get('ms', 0):.1f} ms a frame · "
                    f"{len(dets)} mice above {self._spec.min_confidence:g}")
        else:
            banner, text = "", "The tracker is not running"
        stale = time.perf_counter() - snap.get("t", 0.0) > STALE_S
        self.view.set_marks(snap.get("dets", []), snap.get("inside", []),
                            stale, banner)
        self._status.setText(text)
        # A recording's zone is the one it started with: armed means a
        # recording is sending, so editing waits for its end.
        armed = bool(snap.get("armed"))
        if armed != self._locked:
            self.set_locked(armed)
        self._sync_buttons()

    # ------------------------------------------------------------ lifecycle
    def done(self, result):
        """Every way the window ends runs through here, so ``closed`` is
        sent once. The closed loop belongs to the owner and keeps running."""
        self._timer.stop()
        super().done(result)
        if not self._closed_sent:
            self._closed_sent = True
            self.closed.emit()
