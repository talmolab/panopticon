"""Dynamic camera grid display widget with per-camera FPS indicator and double-click zoom."""
import math
import numpy as np
from PyQt5.QtWidgets import QWidget, QGridLayout, QLabel
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QImage, QPixmap, QFont


class CameraCell(QWidget):
    """Container for one camera view."""

    def __init__(self, index: int, grid: "CameraGridWidget", parent=None):
        super().__init__(parent)
        self._index = index
        self._grid = grid
        self.setStyleSheet("background-color: #0f0f1e; border: 1px solid #333; border-radius: 4px;")

        self.label = QLabel(self)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setScaledContents(True)
        self.label.setStyleSheet("border: none;")

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
        self.label.setGeometry(0, 0, w, h)
        self.name_overlay.setGeometry(0, 0, w, 28)
        self.fps_overlay.setGeometry(0, h - 24, w, 24)


class CameraGridWidget(QWidget):
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

    def grid_aspect(self) -> float:
        if self._num_cameras == 0:
            return 2.4
        rows = math.ceil(self._num_cameras / self.COLS)
        return (self.COLS * self.CAM_ASPECT) / rows

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

        rows = math.ceil(num_cameras / self.COLS) if num_cameras > 0 else 1

        for i in range(num_cameras):
            cell = CameraCell(i, self)
            row, col = divmod(i, self.COLS)
            self._layout.addWidget(cell, row, col)
            self._cells.append(cell)

        for r in range(rows):
            self._layout.setRowStretch(r, 1)
        for c in range(self.COLS):
            self._layout.setColumnStretch(c, 1)

    def toggle_zoom(self, index: int):
        if self._zoomed_index == index:
            # Unzoom: restore the full grid from a clean stretch table.
            self._zoomed_index = -1
            self._reset_stretches()
            for i, cell in enumerate(self._cells):
                row, col = divmod(i, self.COLS)
                self._layout.addWidget(cell, row, col)
                cell.setVisible(True)
            rows = math.ceil(self._num_cameras / self.COLS)
            for r in range(rows):
                self._layout.setRowStretch(r, 1)
            for c in range(self.COLS):
                self._layout.setColumnStretch(c, 1)
        else:
            # Zoom — remove all from layout, add only the target at (0,0)
            self._zoomed_index = index
            for i, cell in enumerate(self._cells):
                self._layout.removeWidget(cell)
                cell.setVisible(i == index)
            self._layout.addWidget(self._cells[index], 0, 0)
            # Clear stretches for unused rows/cols
            rows = math.ceil(self._num_cameras / self.COLS)
            for r in range(rows):
                self._layout.setRowStretch(r, 0)
            for c in range(self.COLS):
                self._layout.setColumnStretch(c, 0)
            self._layout.setRowStretch(0, 1)
            self._layout.setColumnStretch(0, 1)

    def update_frame(self, cam_index: int, frame: np.ndarray):
        if frame is None or cam_index >= len(self._cells):
            return
        h, w = frame.shape[:2]
        qimg = QImage(frame.data, w, h, w, QImage.Format_Grayscale8)
        self._cells[cam_index].label.setPixmap(QPixmap.fromImage(qimg))

    def update_fps(self, cam_index: int, fps: float):
        if cam_index < len(self._cells):
            self._cells[cam_index].fps_overlay.setText(f"{fps:.0f} fps")
