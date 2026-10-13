"""The sidebar's live view of a recording's stimulation paradigm.

A miniature of the Stimulation canvas, laid out as the operator drew it, with
the block each chain should be running highlighted, and a line per chain
naming that block and the time left in it.

RULE: it is fed the trigger ordinal (``set_time(t)`` with
``t = (unwrapped_blockid - 1) / fps``) and worked out by
``stim_trace.chain_progress``. REASON: that is how stim_trace.csv labels each
trigger, so the block shown is the block the trace will give the same frame. A
host clock or a frame count drifts off it.

It shows what the paradigm on the board should be doing, never a measurement.
The board reports nothing back during a recording, so nothing here knows
whether the laser fired: the LED in the cameras' view is the only witness.
"""
import math

from PyQt5.QtCore import Qt, QPointF, QRectF, QSize
from PyQt5.QtGui import QColor, QPainter, QPen, QBrush, QFont
from PyQt5.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from gui_app import stim_compiler, stim_trace
from gui_app.widgets.stimulation_window import BW, BH

TOOLTIP = ("Worked out from the trigger count, the same way as "
           "stim_trace.csv: the block the paradigm on the board should be "
           "running.\nIt is not a measurement. The board reports nothing "
           "back, so only the LED in the cameras' view shows the laser "
           "fired.")

#: Block fills by drive_mode() kind, then the highlighted block's.
_FILL = {"low": QColor("#2c2c44"), "train": QColor("#5a3a7a"),
         "constant": QColor("#7a3434"), "impossible": QColor("#7a3434")}
_FILL_NOW = {"low": QColor("#8a8aa8"), "train": QColor("#cc88ee"),
             "constant": QColor("#ff7070"), "impossible": QColor("#ff7070")}
_OUTLINE_NOW = QColor("#ffd54a")
_EDGE = QColor("#4a5a70")


def clock_text(seconds: float, up: bool = False) -> str:
    """Whole seconds as ``"42 s"`` or ``"3:07"``: rounded down for time
    gone, up for time left (``up``), so a step never reads 0 s while it
    still runs."""
    s = max(0, int(math.ceil(seconds - 1e-9) if up else math.floor(seconds)))
    m, s = divmod(s, 60)
    return f"{m}:{s:02d}" if m else f"{s} s"


def step_text(step: dict) -> str:
    """What a step drives, in the words the editor's blocks use."""
    freq, pw = float(step["freq_hz"]), float(step["pulse_width_ms"])
    kind, _duty = stim_compiler.drive_mode(freq, pw)
    if kind == "low":
        return "off"
    if kind == "train":
        return f"{freq:g} Hz, {pw:g} ms pulses"
    return "constant on"


class _Miniature(QWidget):
    """The canvas's blocks and arrows scaled into the sidebar's width.

    RULE: blocks are drawn where the operator put them on the canvas, unless
    that drawing would make a block shorter than MIN_CANVAS_BLOCK_H; then each
    chain's blocks are laid out in reading order, in rows that wrap. REASON:
    the canvas layout is what the operator recognises, but a long single row
    of blocks scaled into the sidebar leaves slivers a few pixels tall, too
    thin to see which one is lit.
    """

    MARGIN = 4
    #: Tallest the canvas drawing gets, whatever the layout's aspect ratio.
    MAX_HEIGHT = 120
    #: Below this block height the wrapped rows replace the canvas drawing.
    MIN_CANVAS_BLOCK_H = 10
    #: A block in the wrapped rows, the gap between blocks, and between chains.
    CELL_W, CELL_H, GAP, CHAIN_GAP = 26, 12, 3, 8

    def __init__(self, parent=None):
        super().__init__(parent)
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self._blocks: dict = {}      # id -> (x, y, drive kind)
        self._edges: list = []       # (src id, dst id)
        self._chains: list = []      # block ids per chain, in step order
        self._bounds = QRectF()
        self._now: set = set()       # ids of the blocks running now
        self._done: set = set()      # ids of blocks in finished chains

    def set_paradigm(self, blocks: list, edges: list, chains: list):
        self._blocks = {
            b["id"]: (float(b.get("x", 0.0)), float(b.get("y", 0.0)),
                      stim_compiler.drive_mode(b["freq"], b["pw"])[0])
            for b in blocks}
        self._edges = [(e["src"], e["dst"]) for e in edges
                       if e.get("src") in self._blocks
                       and e.get("dst") in self._blocks]
        self._chains = [[i for i in ids if i in self._blocks]
                        for ids in chains]
        bounds = QRectF()
        for x, y, _kind in self._blocks.values():
            bounds = bounds.united(QRectF(x, y, BW, BH))
        self._bounds = bounds
        self._now, self._done = set(), set()
        self.updateGeometry()
        self.update()

    def set_state(self, now: set, done: set):
        if now != self._now or done != self._done:
            self._now, self._done = set(now), set(done)
            self.update()

    # -- layout --
    def _canvas_scale(self, width: int) -> float | None:
        """The canvas drawing's scale at this width, or None when its
        blocks would be too thin and the wrapped rows are used."""
        if self._bounds.isEmpty():
            return None
        s = max(1, width - 2 * self.MARGIN) / self._bounds.width()
        if self._bounds.height() * s > self.MAX_HEIGHT:
            s = self.MAX_HEIGHT / self._bounds.height()
        return s if BH * s >= self.MIN_CANVAS_BLOCK_H else None

    def _columns(self, width: int) -> int:
        usable = max(1, width - 2 * self.MARGIN)
        return max(1, (usable + self.GAP) // (self.CELL_W + self.GAP))

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        if not self._blocks:
            return 0
        s = self._canvas_scale(width)
        if s is not None:
            return int(self._bounds.height() * s + 2 * self.MARGIN)
        cols = self._columns(width)
        rows = [math.ceil(len(ids) / cols) for ids in self._chains if ids]
        h = (sum(rows) * (self.CELL_H + self.GAP) - self.GAP
             + self.CHAIN_GAP * max(0, len(rows) - 1))
        return int(h + 2 * self.MARGIN)

    def sizeHint(self) -> QSize:
        return QSize(240, self.heightForWidth(240))

    def _rects(self) -> tuple[dict, bool]:
        """({block id: rect}, whether the arrows are drawn) for the current
        size."""
        s = self._canvas_scale(self.width())
        if s is not None:
            b = self._bounds
            ox = (self.width() - b.width() * s) / 2
            oy = (self.height() - b.height() * s) / 2
            return {bid: QRectF(ox + (x - b.left()) * s, oy + (y - b.top()) * s,
                                BW * s, BH * s)
                    for bid, (x, y, _k) in self._blocks.items()}, True
        cols = self._columns(self.width())
        out, y = {}, self.MARGIN
        for ids in self._chains:
            for k, bid in enumerate(ids):
                r, c = divmod(k, cols)
                out[bid] = QRectF(self.MARGIN + c * (self.CELL_W + self.GAP),
                                  y + r * (self.CELL_H + self.GAP),
                                  self.CELL_W, self.CELL_H)
            if ids:
                y += (math.ceil(len(ids) / cols) * (self.CELL_H + self.GAP)
                      - self.GAP + self.CHAIN_GAP)
        return out, False

    def paintEvent(self, event):
        if not self._blocks:
            return
        rects, arrows = self._rects()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if arrows:
            p.setPen(QPen(_EDGE, 1))
            for src, dst in self._edges:
                a, c = rects[src], rects[dst]
                if c.left() >= a.right():
                    p.drawLine(QPointF(a.right(), a.center().y()),
                               QPointF(c.left(), c.center().y()))
                else:
                    p.drawLine(a.center(), c.center())
        for bid, (_x, _y, kind) in self._blocks.items():
            if bid in self._now or bid not in rects:
                continue
            r = rects[bid]
            radius = min(3.0, r.height() / 3)
            p.setOpacity(0.35 if bid in self._done else 1.0)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(_FILL.get(kind, _FILL["low"])))
            p.drawRoundedRect(r, radius, radius)
        p.setOpacity(1.0)
        for bid in self._now:
            if bid not in rects:
                continue
            r = rects[bid].adjusted(-1, -1, 1, 1)
            radius = min(3.0, r.height() / 3)
            kind = self._blocks[bid][2]
            p.setPen(QPen(_OUTLINE_NOW, 2))
            p.setBrush(QBrush(_FILL_NOW.get(kind, _FILL_NOW["low"])))
            p.drawRoundedRect(r, radius, radius)
        p.end()


class StimProgressWidget(QWidget):
    """Title, miniature and one line per chain. ``set_paradigm`` takes the
    recording's stim_paradigm.json provenance, ``set_time`` the paradigm's
    time from the trigger count (None before the first trigger)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(4)
        self._title = QLabel("Expected stimulation")
        self._title.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self._title.setStyleSheet("color: #cc88ee; border: none;")
        self._mini = _Miniature()
        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        self._detail.setStyleSheet("color: #aaa; font-size: 10px; border: none;")
        for w in (self._title, self._mini, self._detail):
            w.setToolTip(TOOLTIP)
            lay.addWidget(w)
        self._chains: list = []
        self._ids: list = []

    def set_paradigm(self, provenance: dict):
        blocks = provenance.get("blocks")
        edges = provenance.get("edges")
        self._chains = stim_trace.resolve_chains(provenance)
        if isinstance(blocks, list) and isinstance(edges, list):
            self._ids = stim_compiler.chain_block_ids(blocks, edges)
            self._mini.set_paradigm(blocks, edges, self._ids)
        else:
            self._ids = [[] for _ in self._chains]
            self._mini.set_paradigm([], [], [])
        self.set_time(None)

    def set_time(self, t: float | None):
        if t is None:
            self._title.setText("Expected stimulation · waiting for the "
                                "first trigger")
            self._detail.setText("")
            self._mini.set_state(set(), set())
            return
        progress = stim_trace.chain_progress(self._chains, t)
        now, done, lines = set(), set(), []
        for c, (chain, where) in enumerate(zip(self._chains, progress)):
            steps = chain["steps"]
            ids = self._ids[c] if c < len(self._ids) else []
            i = where["step"]
            if i is None:
                done.update(ids)
                pin = steps[-1]["pin"] if steps else "?"
                lines.append(f"Pin {pin} · finished")
                continue
            if i < len(ids):
                now.add(ids[i])
            loops = " (loops)" if chain.get("loops") else ""
            lines.append(f"Pin {steps[i]['pin']} · block {i + 1} of "
                         f"{len(steps)}{loops} · {step_text(steps[i])} · "
                         f"{clock_text(where['left_s'], up=True)} left")
        over = bool(progress) and all(w["step"] is None for w in progress)
        self._title.setText("Expected stimulation · " + (
            "over" if over else clock_text(t)))
        self._detail.setText("\n".join(lines))
        self._mini.set_state(now, done)
