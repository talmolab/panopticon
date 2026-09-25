"""Live ChArUco coverage graph for the calibration HUD.

Numbered camera nodes on a ring, with an edge for every pair. A node pulses
(glows cyan) when that camera currently sees the board; each edge's width and
whiteness scale with how many ticks the pair co-detected the board, maxing out
at the detector's ``optimal_shared``. When the coverage graph is connected and
every camera is sufficiently covered, the whole graph freezes solid white.
"""
import math
import time

import numpy as np
from PyQt5.QtWidgets import QSizePolicy, QWidget
from PyQt5.QtCore import Qt, QPointF, QRectF
from PyQt5.QtGui import QPainter, QPen, QColor, QFont, QBrush


class CoverageGraphWidget(QWidget):
    #: The graph's size at the narrowest sidebar. Nodes, badges and text scale
    #: with its smaller side beyond that, up to MAX_SCALE.
    BASE_EXTENT = 228.0
    MAX_SCALE = 2.2
    #: How much wider than tall the ring may stretch to use a wide sidebar.
    MAX_ASPECT = 1.35

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(210)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet("background: transparent; border: none;")
        self._n = 0
        self._glow = None
        self._shared = None
        self._per_cam = None
        self._optimal = 50
        self._target = 40
        self._ready = False
        self._grid_covered = None
        self._grid_cells_hit = None
        self._min_grid_cells = 3
        self._components: list[list[int]] = []
        self._start_time = None
        self._elapsed_s = 0.0

    def setup(self, n_cams: int):
        self._n = int(n_cams)
        self._glow = np.zeros(self._n)
        self._shared = np.zeros((self._n, self._n), dtype=int)
        self._per_cam = np.zeros(self._n, dtype=int)
        self._grid_covered = None
        self._grid_cells_hit = np.zeros(self._n, dtype=int)
        # Reset with the rest of the snapshot so a previous session's group
        # list cannot paint before this session's first tick.
        self._components = []
        self._ready = False
        self._start_time = time.monotonic()
        self._elapsed_s = 0.0
        self.update()

    def update_from(self, det):
        """Snapshot a BoardDetector's state and repaint.

        Every attribute is read directly: BoardDetector defines all of them
        unconditionally, so a fallback default would hide a renamed attribute
        behind a plausible number instead of failing loudly.
        """
        self._n = det.n
        self._glow = np.asarray(det.glow, dtype=float).copy()
        self._shared = np.asarray(det.shared, dtype=int).copy()
        # Show the counter READY actually tests, or the caption lies.
        self._per_cam = np.asarray(det.per_cam_frames, dtype=int).copy()
        self._optimal = det.optimal_shared or 1
        self._target = det.min_per_cam_shared
        self._grid_covered = np.asarray(det.grid_covered, dtype=bool).copy()
        self._grid_cells_hit = np.asarray(det.grid_cells_hit, dtype=int).copy()
        self._min_grid_cells = det.MIN_GRID_CELLS
        self._components = [list(c) for c in det.components]
        ready_now = bool(det.ready)
        if self._start_time is not None and not ready_now:
            self._elapsed_s = time.monotonic() - self._start_time
        self._ready = ready_now
        self.update()

    @staticmethod
    def _fitting_font(p, size, weight, width, sample):
        """The largest Segoe UI size at most ``size`` whose ``sample`` fits in
        ``width`` pixels, so the caption is never clipped at the narrowest
        sidebar."""
        pt = max(6, int(round(size)))
        font = QFont("Segoe UI", pt, weight)
        p.setFont(font)
        while pt > 6 and p.fontMetrics().horizontalAdvance(sample) > width:
            pt -= 1
            font = QFont("Segoe UI", pt, weight)
            p.setFont(font)
        return font

    def _node_positions(self, w, h, rx, ry):
        """Nodes evenly spaced by angle on an ellipse centred in (w, h). The
        ellipse fills the width, so a sidebar dragged wider spreads the
        nodes and edges apart even though its height does not change."""
        cx, cy = w / 2.0, h / 2.0
        pos = []
        for i in range(self._n):
            ang = -math.pi / 2 + 2 * math.pi * i / max(self._n, 1)
            pos.append(QPointF(cx + rx * math.cos(ang), cy + ry * math.sin(ang)))
        return pos

    def paintEvent(self, event):
        if not self._n:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        s = max(1.0, min(self.MAX_SCALE, min(w, h) / self.BASE_EXTENT))
        text_s = min(s, 1.6)
        caption_h = 2 * 16 * text_s + 4
        # Room from the centre to the edge, less a node and its badge, so no
        # node is ever drawn past the widget's edge.
        margin = 22 * s
        avail_x = max(12.0, w / 2.0 - margin)
        avail_y = max(12.0, (h - caption_h) / 2.0 - margin)
        # Round, sized by the tighter side; given spare width, the ring
        # stretches up to MAX_ASPECT so the extra width spreads the nodes.
        ry = min(avail_x, avail_y)
        rx = min(avail_x, ry * self.MAX_ASPECT)
        pos = self._node_positions(w, h - caption_h + 4, rx, ry)
        ready = self._ready

        # --- edges ---
        for i in range(self._n):
            for j in range(i + 1, self._n):
                c = int(self._shared[i, j]) if self._shared is not None else 0
                strength = min(c / float(self._optimal), 1.0)
                if ready:
                    col, width = QColor(255, 255, 255), 4.0
                elif c <= 0:
                    col, width = QColor(55, 55, 75), 1.0
                else:
                    v = int(70 + 185 * strength)
                    col, width = QColor(v, v, v), 1.0 + 5.0 * strength
                pen = QPen(col)
                pen.setWidthF(width * s)
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                p.drawLine(pos[i], pos[j])

        # --- nodes with camera number + spatial grid badge ---
        node_r = 15.0 * s
        grid_rows, grid_cols = 2, 2
        for i in range(self._n):
            glow = float(self._glow[i]) if self._glow is not None else 0.0
            if ready:
                fill, border, txt = QColor(255, 255, 255), QColor(255, 255, 255), QColor(20, 20, 30)
            else:
                fill = QColor(int(50 + 40 * glow), int(80 + 120 * glow), int(120 + 135 * glow))
                border = QColor(130, 170, 235) if glow > 0.05 else QColor(70, 90, 130)
                txt = QColor(235, 235, 245)
            p.setBrush(QBrush(fill))
            pen = QPen(border)
            pen.setWidthF(2.0 * s)
            p.setPen(pen)
            p.drawEllipse(pos[i], node_r, node_r)

            # Camera number always visible
            p.setPen(QPen(txt))
            p.setFont(QFont("Segoe UI", int(round(10 * s)), QFont.Bold))
            p.drawText(QRectF(pos[i].x() - node_r, pos[i].y() - node_r,
                              2 * node_r, 2 * node_r),
                       Qt.AlignCenter, str(i + 1))

            # 2x2 grid badge in bottom-right of node
            if self._grid_covered is not None and not ready:
                gs = 10.0 * s
                gx = pos[i].x() + node_r * 0.3
                gy = pos[i].y() + node_r * 0.3
                cw, ch = gs / grid_cols, gs / grid_rows
                for gr in range(grid_rows):
                    for gc in range(grid_cols):
                        if self._grid_covered[i, gr, gc]:
                            cell_color = QColor(100, 220, 140, 200)
                        else:
                            cell_color = QColor(40, 40, 60, 140)
                        p.setBrush(QBrush(cell_color))
                        p.setPen(QPen(QColor(60, 60, 80, 100), 0.5))
                        p.drawRect(QRectF(gx + gc * cw, gy + gr * ch, cw, ch))

        # --- caption with timer ---
        elapsed = self._elapsed_s
        mins, secs = int(elapsed) // 60, int(elapsed) % 60
        timer_str = f"{mins}:{secs:02d}"

        line_h = 16 * text_s
        p.setFont(self._fitting_font(
            p, 9 * text_s, QFont.Bold, w - 4,
            "0:00  paired 00/00  grid 0/0  groups 0/1"))
        if ready:
            p.setPen(QPen(QColor(255, 255, 255)))
            p.drawText(QRectF(0, h - line_h - 2, w, line_h), Qt.AlignCenter,
                       f"READY — {timer_str}")
        else:
            mn = int(self._per_cam.min()) if (self._per_cam is not None and self._n) else 0
            min_grid = int(self._grid_cells_hit.min()) if (self._grid_cells_hit is not None and self._n) else 0
            comps = self._components
            p.setPen(QPen(QColor(150, 150, 170)))
            p.drawText(QRectF(0, h - line_h - 2, w, line_h), Qt.AlignCenter,
                       f"{timer_str}  paired {mn}/{self._target}  "
                       f"grid {min_grid}/{self._min_grid_cells}  "
                       f"groups {len(comps)}/1")

            # Connectivity is the one blocker the other two numbers cannot show,
            # and the one that decides which cameras survive the solve: it kept
            # a 9-camera session at 4 usable cameras while every per-camera
            # figure read as satisfied. Say which groups exist and which pair is
            # closest to joining them, so it reads as an instruction.
            if len(comps) > 1:
                # The group list stays -- it is what caught a nine-camera
                # session solving only four cameras while every per-camera
                # number read as satisfied. There is no per-pair "show board to
                # camX + camY together" instruction here: it repaints every tick
                # and names a pair the operator is often not working on. `groups
                # N/1` in the line below carries the same information without
                # telling the operator what to do.
                groups = "  ".join(
                    "{" + ",".join(str(i + 1) for i in sorted(g)) + "}"
                    for g in comps[:4])
                p.setPen(QPen(QColor(235, 170, 90)))
                p.setFont(QFont("Segoe UI", int(round(8 * text_s))))
                p.drawText(QRectF(0, h - 2 * line_h - 2, w, line_h),
                           Qt.AlignCenter, groups)
        p.end()
