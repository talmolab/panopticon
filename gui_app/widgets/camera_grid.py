"""Dynamic camera grid display widget with per-camera FPS indicator and double-click zoom."""
import math
import numpy as np
from PyQt5.QtWidgets import QWidget, QGridLayout, QLabel
from PyQt5.QtCore import Qt, QRect
from PyQt5.QtGui import QImage, QFont, QPainter


class PreviewPane(QWidget):
    """Paints one camera's latest frame, letterboxed, straight from a QImage.

    The frame is wrapped as a Grayscale8 QImage and drawn in paintEvent with
    one drawImage call that scales and blits together. This avoids the
    QLabel+QPixmap route, where QPixmap.fromImage expands every 8-bit frame to
    a 32-bit pixmap on the GUI thread on every tick, a per-pane cost that
    scales with the camera count and competes with the grab loop for the
    interpreter.

    The QImage does not own its pixels, so the numpy frame is kept alongside
    it for exactly as long as the image lives.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frame = None
        self._image = None

    def set_frame(self, frame: np.ndarray):
        if frame.ndim == 2:
            fmt = QImage.Format_Grayscale8
        elif frame.ndim == 3 and frame.shape[2] == 3:
            fmt = QImage.Format_RGB888
        else:
            return
        # QImage reads rows at a fixed stride, so the buffer must be a
        # contiguous uint8 block; a view with other strides would shear.
        if frame.dtype != np.uint8 or not frame.flags.c_contiguous:
            frame = np.ascontiguousarray(frame, dtype=np.uint8)
        h, w = frame.shape[:2]
        self._frame = frame
        self._image = QImage(frame.data, w, h, frame.strides[0], fmt)
        self.update()

    def image(self):
        """The current QImage, or None before the first frame."""
        return self._image

    def target_rect(self) -> QRect:
        """The largest rect of the image's aspect that fits this pane, centred.

        The preview is what the operator judges aim, focus and framing by, so
        it must keep the sensor's aspect whatever shape the cell has: a zoomed
        pane and a resized window would otherwise stretch it.
        """
        if self._image is None:
            return QRect()
        w, h = self.width(), self.height()
        iw, ih = self._image.width(), self._image.height()
        if iw <= 0 or ih <= 0 or w <= 0 or h <= 0:
            return QRect()
        scale = min(w / iw, h / ih)
        tw, th = max(1, int(iw * scale)), max(1, int(ih * scale))
        return QRect((w - tw) // 2, (h - th) // 2, tw, th)

    def paintEvent(self, event):
        if self._image is None:
            return
        p = QPainter(self)
        # Nearest-neighbour scaling: the preview is decimated already and a
        # smooth transform would cost a filtered pass per pane per tick.
        p.setRenderHint(QPainter.SmoothPixmapTransform, False)
        p.drawImage(self.target_rect(), self._image)
        p.end()


class CameraCell(QWidget):
    """Container for one camera view."""

    def __init__(self, index: int, grid: "CameraGridWidget", parent=None):
        super().__init__(parent)
        self._index = index
        self._grid = grid
        self.setStyleSheet("background-color: #0f0f1e; border: 1px solid #333; border-radius: 4px;")

        self.view = PreviewPane(self)

        self.name_overlay = QLabel(f"cam{index+1}", self)
        self.name_overlay.setFont(QFont("Segoe UI", 9))
        self.name_overlay.setStyleSheet("color: rgba(255,255,255,150); background: transparent; border: none; padding: 4px;")
        self.name_overlay.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        self.fps_overlay = QLabel("", self)
        self.fps_overlay.setFont(QFont("Segoe UI", 9))
        self.fps_overlay.setStyleSheet("color: rgba(100,255,100,180); background: transparent; border: none; padding: 4px;")
        self.fps_overlay.setAlignment(Qt.AlignLeft | Qt.AlignBottom)

    def mouseDoubleClickEvent(self, event):
        self._grid.toggle_zoom(self._index)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w, h = self.width(), self.height()
        self.view.setGeometry(0, 0, w, h)
        self.name_overlay.setGeometry(0, 0, w, 28)
        self.fps_overlay.setGeometry(0, h - 24, w, 24)


class CameraGridWidget(QWidget):
    #: Defaults for a rig that has not declared its sensor shape or column
    #: count. Both are instance settings (set_camera_aspect, set_columns) so
    #: a rig with 4:3 or square sensors, or four cameras, gets an honest
    #: window aspect instead of the Basler ace shape baked in.
    COLS = 3
    CAM_ASPECT = 1920 / 1200

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QGridLayout(self)
        self._layout.setSpacing(4)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._cells: list[CameraCell] = []
        self._zoomed_index: int = -1
        self._num_cameras = 0
        self._cols = self.COLS
        self._cam_aspect = self.CAM_ASPECT

    def set_camera_aspect(self, frame_width: int, frame_height: int):
        """Declare the sensor shape the window is sized for (profile
        frame_width/frame_height). Takes effect on the next grid_aspect."""
        if frame_width <= 0 or frame_height <= 0:
            raise ValueError(f"frame size must be positive, got {frame_width}x{frame_height}")
        self._cam_aspect = frame_width / frame_height

    def set_columns(self, cols: int):
        """Declare the column count. Takes effect on the next setup_grid."""
        if cols <= 0:
            raise ValueError(f"column count must be positive, got {cols}")
        self._cols = int(cols)

    @staticmethod
    def columns_for(num_cameras: int) -> int:
        """Columns for a grid of ``num_cameras`` panes, wider than tall.

        floor(sqrt(n)) rows and as many columns as that takes: 1 to 3 cameras
        sit in one row, 4 in a 2x2 grid, 5 and 6 in two rows of three, 7 and 8
        in two rows of four, 9 in 3x3. Any camera count gets a grid; none is
        assumed.
        """
        n = max(1, int(num_cameras))
        return math.ceil(n / math.isqrt(n))

    def grid_aspect(self) -> float:
        if self._num_cameras == 0:
            return 2.4
        rows = math.ceil(self._num_cameras / self._cols)
        return (self._cols * self._cam_aspect) / rows

    def _reset_stretches(self):
        """Zero every row and column stretch the layout has ever held.

        QGridLayout keeps a row in the distribution while its stretch is
        non-zero even when no widget occupies it, and rowCount() never shrinks
        after widgets are removed. Setting only the rows the new count needs
        therefore leaves a larger previous grid's empty rows sharing the
        height, which squashes the live panes into the top of the widget.
        """
        for r in range(self._layout.rowCount()):
            self._layout.setRowStretch(r, 0)
        for c in range(self._layout.columnCount()):
            self._layout.setColumnStretch(c, 0)

    def setup_grid(self, num_cameras: int):
        for c in self._cells:
            self._layout.removeWidget(c)
            c.deleteLater()
        self._cells.clear()
        self._zoomed_index = -1
        self._num_cameras = num_cameras
        self._reset_stretches()

        rows = math.ceil(num_cameras / self._cols) if num_cameras > 0 else 1

        for i in range(num_cameras):
            cell = CameraCell(i, self)
            row, col = divmod(i, self._cols)
            self._layout.addWidget(cell, row, col)
            self._cells.append(cell)

        for r in range(rows):
            self._layout.setRowStretch(r, 1)
        for c in range(self._cols):
            self._layout.setColumnStretch(c, 1)

    def toggle_zoom(self, index: int):
        if self._zoomed_index == index:
            # Unzoom: restore the full grid from a clean stretch table.
            self._zoomed_index = -1
            self._reset_stretches()
            for i, cell in enumerate(self._cells):
                row, col = divmod(i, self._cols)
                self._layout.addWidget(cell, row, col)
                cell.setVisible(True)
            rows = math.ceil(self._num_cameras / self._cols)
            for r in range(rows):
                self._layout.setRowStretch(r, 1)
            for c in range(self._cols):
                self._layout.setColumnStretch(c, 1)
        else:
            # Zoom — remove all from layout, add only the target at (0,0)
            self._zoomed_index = index
            for i, cell in enumerate(self._cells):
                self._layout.removeWidget(cell)
                cell.setVisible(i == index)
            self._layout.addWidget(self._cells[index], 0, 0)
            # Only (0,0) keeps a stretch; every other row and column is zeroed
            # so the zoomed pane takes the whole grid.
            self._reset_stretches()
            self._layout.setRowStretch(0, 1)
            self._layout.setColumnStretch(0, 1)

    def update_frame(self, cam_index: int, frame: np.ndarray):
        if frame is None or cam_index >= len(self._cells):
            return
        cell = self._cells[cam_index]
        # A pane hidden by zoom cannot be seen, so it is not converted: the
        # other eight panes would otherwise pay the full per-tick cost for
        # nothing while one camera is zoomed.
        if cell.isHidden():
            return
        cell.view.set_frame(frame)

    def update_fps(self, cam_index: int, fps: float):
        if cam_index < len(self._cells):
            self._cells[cam_index].fps_overlay.setText(f"{fps:.0f} fps")
