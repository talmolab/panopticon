"""The Focus window: one live counter per camera for focusing lenses by hand.

The operator lays the ChArUco board still in the arena, clicks a camera and
turns its focus ring while watching that camera's count of decoded markers.
The decimated preview cannot show a soft lens; the count can, because blur
stops the marker codes decoding long before the board looks blurred at
preview scale (see ``gui_app/focus_worker.py``).

Each tile shows the markers decoded of the board's total, the ChArUco corners,
the best count since the last reset, a bar, and the last 30 s of counts. Once
the count stops rising, the sharpness score and the median marker confidence
find the peak. A camera's best sharpness is kept only at its best marker
count: the score is measured over the decoded markers' hull, and a hull that
grows with the count would move the score for a reason other than focus.

Beside the tiles, the selected camera's whole view, scaled to fit, shows a
box around the markers it decoded, so the operator sees where the camera finds
the board without the picture moving.
"""
import math
import time
from collections import deque

from PyQt5.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPen
from PyQt5.QtWidgets import (QDialog, QFrame, QGridLayout, QHBoxLayout,
                             QLabel, QPushButton, QSizePolicy, QVBoxLayout,
                             QWidget)

from gui_app.focus_worker import FocusWorker

#: Seconds of marker counts each tile's trend line spans.
TREND_S = 30.0
#: Seconds without a reading after which a tile greys out.
STALE_S = 3.0
#: Seconds the last board box stays up, dashed, after readings that decode
#: no marker.
BOX_HOLD_S = 1.0
#: Share of the board's markers at or above which the count shows green, and
#: at or above which it shows amber; below, red.
GOOD_SHARE = 0.75
FAIR_SHARE = 0.40

GOOD = "#5fd35f"
FAIR = "#e0b040"
POOR = "#e05555"
DIM = "#666"

GUIDANCE = (
    "Lay the board still near the centre of the arena, where the animal will "
    "be. Click a camera, then turn its focus ring slowly and watch its count. "
    "Stop where the count is highest. When every marker decodes, turn for the "
    "highest sharpness. Lock the ring, then do the next camera.")


def share_colour(markers: int, total: int) -> str:
    """The counter's colour for ``markers`` decoded of ``total``."""
    if total <= 0:
        return DIM
    share = markers / total
    if share >= GOOD_SHARE:
        return GOOD
    if share >= FAIR_SHARE:
        return FAIR
    return POOR


class TrendLine(QWidget):
    """The last ``TREND_S`` seconds of one camera's marker counts."""

    def __init__(self, total: int, parent=None):
        super().__init__(parent)
        self._total = max(int(total), 1)
        self._points = deque()
        self.setMinimumHeight(28)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def add(self, t: float, markers: int):
        self._points.append((t, markers))
        while self._points and t - self._points[0][0] > TREND_S:
            self._points.popleft()
        self.update()

    def clear(self):
        self._points.clear()
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(QPen(QColor("#2a2a44"), 1))
        p.drawLine(0, h - 1, w, h - 1)
        if len(self._points) < 2:
            return
        t_end = self._points[-1][0]
        pen = QPen(QColor("#88aadd"), 1.5)
        p.setPen(pen)
        prev = None
        for t, m in self._points:
            x = w - 1 - (t_end - t) / TREND_S * (w - 1)
            y = h - 2 - m / self._total * (h - 4)
            if prev is not None:
                p.drawLine(int(prev[0]), int(prev[1]), int(x), int(y))
            prev = (x, y)


class FocusTile(QFrame):
    """One camera's counters. Click to show its view."""

    clicked = pyqtSignal(int)

    def __init__(self, index: int, name: str, total_markers: int,
                 total_corners: int, parent=None):
        super().__init__(parent)
        self.index = index
        self.name = name
        self._total = total_markers
        self._total_corners = total_corners
        self._selected = False
        self._last_t = None
        self.best_markers = 0
        self.best_corners = 0
        self.best_sharpness = None
        self.best_confidence = None
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(190)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(2)
        top = QHBoxLayout()
        self._name = QLabel(name)
        self._name.setStyleSheet("color: #aab; font-size: 12px; border: none;")
        top.addWidget(self._name)
        top.addStretch(1)
        self._best = QLabel("")
        self._best.setStyleSheet("color: #889; font-size: 11px; border: none;")
        top.addWidget(self._best)
        lay.addLayout(top)

        row = QHBoxLayout()
        self._count = QLabel("–")
        f = QFont()
        f.setPointSize(26)
        f.setBold(True)
        self._count.setFont(f)
        self._count.setStyleSheet(f"color: {DIM}; border: none;")
        row.addWidget(self._count)
        self._of = QLabel(f"/ {total_markers} markers")
        self._of.setStyleSheet("color: #889; font-size: 11px; border: none;")
        row.addWidget(self._of, 0, Qt.AlignBottom)
        row.addStretch(1)
        lay.addLayout(row)

        self._detail = QLabel("waiting for a frame")
        self._detail.setStyleSheet("color: #aab; font-size: 11px; border: none;")
        lay.addWidget(self._detail)
        self._fine = QLabel("")
        self._fine.setStyleSheet("color: #889; font-size: 11px; border: none;")
        lay.addWidget(self._fine)
        self._conf = QLabel("")
        self._conf.setStyleSheet("color: #889; font-size: 11px; border: none;")
        lay.addWidget(self._conf)
        lay.addStretch(1)
        self.trend = TrendLine(total_markers)
        lay.addWidget(self.trend)
        self._restyle()

    def _restyle(self):
        border = "#5078c8" if self._selected else "#333"
        # The labels draw no background of their own, so the tile's colour
        # shows through instead of the window's.
        self.setStyleSheet(
            f"FocusTile {{ background: #1a1a2e; border: 2px solid {border}; "
            "border-radius: 4px; } QLabel { background: transparent; }")

    def set_selected(self, selected: bool):
        self._selected = selected
        self._restyle()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.index)
        super().mousePressEvent(event)

    def reset_best(self):
        self.best_markers = 0
        self.best_corners = 0
        self.best_sharpness = None
        self.best_confidence = None
        self.trend.clear()
        self._best.setText("")
        self._fine.setText("")

    def update_stats(self, stats, now: float):
        """Show one reading: a FocusStats, or None for no frame yet."""
        self._last_t = now
        if stats is None:
            self._count.setText("–")
            self._count.setStyleSheet(f"color: {DIM}; border: none;")
            self._detail.setText("waiting for a frame")
            self._fine.setText("")
            self._conf.setText("")
            return
        m = stats.markers
        if m > self.best_markers:
            # A larger hull: the sharpness and confidence bests restart, so
            # they are only ever compared over the same markers.
            self.best_markers = m
            self.best_sharpness = stats.sharpness
            self.best_confidence = stats.confidence
        elif m == self.best_markers and m > 0:
            if stats.sharpness is not None and (
                    self.best_sharpness is None
                    or stats.sharpness > self.best_sharpness):
                self.best_sharpness = stats.sharpness
            if stats.confidence is not None and (
                    self.best_confidence is None
                    or stats.confidence > self.best_confidence):
                self.best_confidence = stats.confidence
        self.best_corners = max(self.best_corners, stats.corners)
        self._count.setText(str(m))
        self._count.setStyleSheet(
            f"color: {share_colour(m, self._total)}; border: none;")
        self._detail.setText(
            f"{stats.corners} / {self._total_corners} corners"
            if m else "no marker decoded")
        self._best.setText(f"best {self.best_markers}")
        # The bests are kept at the best count only, so they are shown only
        # there: beside a lower count they belong to another hull.
        at_best = m == self.best_markers
        if stats.sharpness is None:
            self._fine.setText("")
        elif at_best and self.best_sharpness is not None:
            self._fine.setText(f"sharpness {stats.sharpness:.0f}, "
                               f"best {self.best_sharpness:.0f}")
        else:
            self._fine.setText(f"sharpness {stats.sharpness:.0f}")
        self._conf.setText("" if stats.confidence is None
                           else f"confidence {stats.confidence:.2f}")
        self.trend.add(now, m)

    def mark_stale(self, now: float):
        """Grey the counter when no reading came for ``STALE_S`` seconds."""
        if self._last_t is not None and now - self._last_t > STALE_S:
            self._count.setStyleSheet(f"color: {DIM}; border: none;")


class CameraView(QWidget):
    """The selected camera's whole view, scaled to fit, with a box around the
    markers it decoded.

    The framing never follows the detections: a view that cropped to the
    board jumped from tick to tick when only a few markers decoded, and
    flickered. The box is the bounding box of the decoded markers, in the
    colour of the count. After a reading with no marker the last box stays,
    dashed, for ``BOX_HOLD_S``, so sparse detections do not blink it on and
    off.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._view = None       # the numpy array the QImage reads
        self._image = None
        self._name = ""
        self._caption = "Click a camera to see its view."
        self._box = None        # (x0, y0, x1, y1) in view pixels
        self._box_t = None
        self._box_fresh = False
        self._colour = DIM
        self._label = ""
        self.setMinimumSize(420, 280)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    @property
    def box(self):
        """The box drawn now, in view pixels, or None."""
        return self._box

    @property
    def box_fresh(self) -> bool:
        """Whether the box drawn now comes from the latest reading."""
        return self._box is not None and self._box_fresh

    def set_view(self, view, scale: float, name: str, stats, total: int,
                 now: float):
        """Show ``view`` (a ``FocusTick`` view) with the box from ``stats``
        (its ``roi`` is in full-frame pixels, ``scale`` maps it on)."""
        h, w = view.shape[:2]
        fmt = (QImage.Format_RGB888 if view.ndim == 3
               else QImage.Format_Grayscale8)
        # The QImage reads the array's memory, so the array is kept with it.
        self._view = view
        self._image = QImage(view.data, w, h, view.strides[0], fmt)
        self._name = name
        if stats is not None and stats.roi is not None:
            x0, y0, x1, y1 = stats.roi
            self._box = (x0 * scale, y0 * scale, x1 * scale, y1 * scale)
            self._box_t = now
            self._box_fresh = True
            self._colour = share_colour(stats.markers, total)
            self._label = f"{stats.markers} of {total} markers"
        else:
            if self._box is not None and now - self._box_t > BOX_HOLD_S:
                self._box = None
            self._box_fresh = False
            self._label = ("no marker decoded" if stats is not None
                           else "waiting for a frame")
        self.update()

    def clear(self, caption: str):
        self._view = None
        self._image = None
        self._box = None
        self._label = ""
        self._caption = caption
        self.update()

    def image_rect(self) -> QRectF:
        """Where the view is drawn: the largest rect of its aspect that fits,
        centred."""
        if self._image is None:
            return QRectF()
        iw, ih = self._image.width(), self._image.height()
        s = min(self.width() / iw, self.height() / max(ih, 1))
        w, h = iw * s, ih * s
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0e0e1c"))
        if self._image is None:
            p.setPen(QColor("#889"))
            p.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap,
                       self._caption)
            return
        target = self.image_rect()
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawImage(target, self._image)
        p.setPen(QColor("#dcdcdc"))
        p.drawText(QPointF(target.x() + 8, target.y() + 18), self._name)
        if self._label:
            p.drawText(QPointF(target.x() + 8, target.bottom() - 8),
                       self._label)
        if self._box is None:
            return
        k = target.width() / self._image.width()
        x0, y0, x1, y1 = self._box
        pen = QPen(QColor(self._colour), 2)
        if not self._box_fresh:
            pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(target.x() + x0 * k, target.y() + y0 * k,
                          (x1 - x0) * k, (y1 - y0) * k))


class FocusWindow(QDialog):
    """Live focus counters for every camera, and the selected one's view.

    It owns its ``FocusWorker``: the worker starts with the window and is
    joined in ``done`` before ``closed`` is emitted, so the owner can turn
    full frames off and drop the window as soon as it hears ``closed``.
    """

    closed = pyqtSignal()

    def __init__(self, camera_names: list, engine, camera_mgr, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Focus")
        self.setMinimumSize(980, 640)
        # Room for the camera view beside the counters.
        self.resize(1400, 820)
        self.setStyleSheet("background: #141428; color: #dcdcdc;")
        self._names = list(camera_names)
        self._total = engine.total_markers
        self._selected = None
        self._shut = False
        self._closed_sent = False

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)
        guide = QLabel(GUIDANCE)
        guide.setWordWrap(True)
        guide.setStyleSheet("color: #aab; font-size: 12px;")
        root.addWidget(guide)

        body = QHBoxLayout()
        grid = QGridLayout()
        grid.setSpacing(8)
        n = len(self._names)
        cols = max(1, math.ceil(math.sqrt(n)))
        self.tiles = []
        for i, name in enumerate(self._names):
            tile = FocusTile(i, name, engine.total_markers,
                             engine.total_corners)
            tile.clicked.connect(self.select)
            grid.addWidget(tile, i // cols, i % cols)
            self.tiles.append(tile)
        grid_box = QWidget()
        grid_box.setLayout(grid)
        body.addWidget(grid_box, 1)
        self.view = CameraView()
        body.addWidget(self.view, 1)
        root.addLayout(body, 1)

        bottom = QHBoxLayout()
        reset = QPushButton("Reset best")
        reset.setToolTip("Forget every camera's best count, sharpness and "
                         "confidence, and its trend line")
        reset.setStyleSheet(
            "QPushButton { background: #2a2a4a; color: #88aadd; "
            "border: 1px solid #444; border-radius: 3px; padding: 5px 10px; }"
            "QPushButton:hover { background: #333366; border-color: #5078c8; }")
        reset.clicked.connect(self.reset_best)
        bottom.addWidget(reset)
        bottom.addStretch(1)
        self._rate = QLabel("")
        self._rate.setStyleSheet("color: #889; font-size: 11px;")
        bottom.addWidget(self._rate)
        root.addLayout(bottom)

        self._stale_timer = QTimer(self)
        self._stale_timer.setInterval(1000)
        self._stale_timer.timeout.connect(self._mark_stale)
        self._stale_timer.start()

        self.worker = FocusWorker(engine, camera_mgr, n)
        self.worker.updated.connect(self.on_tick)
        self.worker.start()

    def select(self, index: int):
        """Make ``index`` the camera the worker detects every tick."""
        self._selected = index
        for tile in self.tiles:
            tile.set_selected(tile.index == index)
        self.worker.set_selected(index)
        self.view.clear(f"Waiting for {self._names[index]}…")

    def reset_best(self):
        for tile in self.tiles:
            tile.reset_best()

    def on_tick(self, tick):
        now = time.monotonic()
        for cam, stats in tick.results.items():
            if 0 <= cam < len(self.tiles):
                self.tiles[cam].update_stats(stats, now)
        cam = tick.view_cam
        if tick.view is not None and cam == self._selected:
            self.view.set_view(tick.view, tick.view_scale, self._names[cam],
                               tick.results.get(cam), self._total, now)
        if tick.ticks_per_s > 0:
            per_cam = (tick.ticks_per_s if self._selected is not None
                       else 2 * tick.ticks_per_s / max(len(self.tiles), 1))
            self._rate.setText(
                "{} updates {:.1f} times a second".format(
                    self._names[self._selected] if self._selected is not None
                    else "each camera", per_cam))

    def _mark_stale(self):
        now = time.monotonic()
        for tile in self.tiles:
            tile.mark_stale(now)

    def shutdown(self):
        """Stop and join the worker. Idempotent. The join is bounded by one
        tick: two full-resolution detections."""
        if self._shut:
            return
        self._shut = True
        self._stale_timer.stop()
        self.worker.stop()
        self.worker.wait()

    def done(self, result):
        """Every way the window ends runs through here: the close button
        (QDialog's closeEvent rejects), Escape (reject) and the owner's
        ``done(0)``. A hide alone would leave the worker running."""
        self.shutdown()
        super().done(result)
        if not self._closed_sent:
            self._closed_sent = True
            self.closed.emit()
