"""The sidebar's live view of a recording's stimulation paradigm.

A miniature of the Stimulation canvas, laid out as the operator drew it, each
block labelled with its pin and parameters, the block each chain should be
running outlined, and a line per chain naming that block and the time left in
it. The running block is lit while it should be driving its pin: always for
an ordinary block, and for a zone block only while the closed loop reports a
mouse in its zone.

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
from PyQt5.QtGui import QColor, QPainter, QPen, QBrush, QFont, QFontMetrics
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
#: Label colours on an unlit block and on a lit one.
_TEXT = QColor("#dcdcf0")
_TEXT_LIT = QColor("#16162a")


def clock_text(seconds: float, up: bool = False) -> str:
    """Whole seconds as ``"42 s"`` or ``"3:07"``: rounded down for time
    gone, up for time left (``up``), so a step never reads 0 s while it
    still runs."""
    s = max(0, int(math.ceil(seconds - 1e-9) if up else math.floor(seconds)))
    m, s = divmod(s, 60)
    return f"{m}:{s:02d}" if m else f"{s} s"


def block_lines(block: dict) -> list:
    """A miniature block's label: its pin (and zone), what it drives, and its
    duration. A block with room for fewer lines drops the duration, then the
    pin, so what it drives stays."""
    freq, pw = float(block["freq"]), float(block["pw"])
    kind, _duty = stim_compiler.drive_mode(freq, pw)
    zone = block.get("zone")
    head = f"Pin {block['pin']}" + (f" · zone {zone}" if zone else "")
    drive = ("off" if kind == "low" else
             f"{freq:g} Hz · {pw:g} ms" if kind == "train" else "constant on")
    return [head, drive, f"{float(block['dur']):g} s"]


def step_text(step: dict) -> str:
    """What a step drives, in the words the editor's blocks use."""
    freq, pw = float(step["freq_hz"]), float(step["pulse_width_ms"])
    kind, _duty = stim_compiler.drive_mode(freq, pw)
    where = f" in zone {step['zone']}" if step.get("zone") else ""
    if kind == "low":
        return "off"
    if kind == "train":
        return f"{freq:g} Hz, {pw:g} ms pulses{where}"
    return f"constant on{where}"


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
    #: Below this block height the wrapped rows replace the canvas drawing:
    #: two lines of its label still fit.
    MIN_CANVAS_BLOCK_H = 24
    #: A block in the wrapped rows, the gap between blocks, and between chains.
    CELL_W, CELL_H, GAP, CHAIN_GAP = 74, 26, 3, 8
    #: A label's font size in pixels, from a block's height, within limits.
    FONT_PX_MIN, FONT_PX_MAX = 7, 11

    def __init__(self, parent=None):
        super().__init__(parent)
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self._blocks: dict = {}      # id -> (x, y, drive kind)
        self._labels: dict = {}      # id -> block_lines()
        self._edges: list = []       # (src id, dst id)
        self._chains: list = []      # block ids per chain, in step order
        self._bounds = QRectF()
        self._now: set = set()       # ids of the blocks running now
        self._lit: set = set()       # of those, the ones driving their pin
        self._done: set = set()      # ids of blocks in finished chains

    def set_paradigm(self, blocks: list, edges: list, chains: list):
        self._blocks = {
            b["id"]: (float(b.get("x", 0.0)), float(b.get("y", 0.0)),
                      stim_compiler.drive_mode(b["freq"], b["pw"])[0])
            for b in blocks}
        self._labels = {b["id"]: block_lines(b) for b in blocks}
        self._edges = [(e["src"], e["dst"]) for e in edges
                       if e.get("src") in self._blocks
                       and e.get("dst") in self._blocks]
        self._chains = [[i for i in ids if i in self._blocks]
                        for ids in chains]
        bounds = QRectF()
        for x, y, _kind in self._blocks.values():
            bounds = bounds.united(QRectF(x, y, BW, BH))
        self._bounds = bounds
        self._now, self._lit, self._done = set(), set(), set()
        self.updateGeometry()
        self.update()

    def set_state(self, now: set, done: set, lit: set | None = None):
        """``now``: the blocks running; ``lit``: those of them driving their
        pin (all of them when None); ``done``: blocks of finished chains."""
        lit = set(now) if lit is None else set(lit) & set(now)
        if now != self._now or done != self._done or lit != self._lit:
            self._now, self._done, self._lit = set(now), set(done), lit
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
            self._label(p, bid, r, lit=False)
        p.setOpacity(1.0)
        # The running blocks keep the outline; a zone block whose zone is
        # empty keeps its unlit fill, so it lights as a mouse enters.
        for bid in self._now:
            if bid not in rects:
                continue
            r = rects[bid].adjusted(-1, -1, 1, 1)
            radius = min(3.0, r.height() / 3)
            kind = self._blocks[bid][2]
            lit = bid in self._lit
            fill = _FILL_NOW if lit else _FILL
            p.setPen(QPen(_OUTLINE_NOW, 2))
            p.setBrush(QBrush(fill.get(kind, fill["low"])))
            p.drawRoundedRect(r, radius, radius)
            self._label(p, bid, rects[bid], lit=lit)
        p.end()

    def _label(self, p: QPainter, bid, r: QRectF, lit: bool):
        """The block's label, as many of block_lines() as fit, centred."""
        lines = self._labels.get(bid)
        if not lines:
            return
        font = QFont("Segoe UI")
        font.setPixelSize(max(self.FONT_PX_MIN,
                              min(self.FONT_PX_MAX, int(r.height() / 3.4))))
        fm = QFontMetrics(font)
        fit = int((r.height() - 2) // fm.lineSpacing())
        if fit < 1 or r.width() < 3 * fm.averageCharWidth():
            return
        shown = lines if fit >= 3 else lines[:2] if fit == 2 else lines[1:2]
        width = int(r.width() - 4)
        text = "\n".join(fm.elidedText(t, Qt.ElideRight, width)
                         for t in shown)
        p.setFont(font)
        p.setPen(QPen(_TEXT_LIT if lit else _TEXT))
        p.drawText(r.adjusted(2, 1, -2, -1), Qt.AlignCenter, text)


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

    def set_time(self, t: float | None, zones: dict | None = None):
        """``zones`` maps each zone's name to the closed loop's state for it,
        None when unknown; a zone step's line says its zone's."""
        if t is None:
            self._title.setText("Expected stimulation · waiting for the "
                                "first trigger")
            self._detail.setText("")
            self._mini.set_state(set(), set())
            return
        progress = stim_trace.chain_progress(self._chains, t)
        now, lit, done, lines = set(), set(), set(), []
        for c, (chain, where) in enumerate(zip(self._chains, progress)):
            steps = chain["steps"]
            ids = self._ids[c] if c < len(self._ids) else []
            i = where["step"]
            if i is None:
                done.update(ids)
                pin = steps[-1]["pin"] if steps else "?"
                lines.append(f"Pin {pin} · finished")
                continue
            z = steps[i].get("zone")
            if i < len(ids):
                now.add(ids[i])
                # A zone block drives its pin only while a mouse is in its
                # zone; unknown counts as not.
                if not z or (zones or {}).get(z):
                    lit.add(ids[i])
            loops = " (loops)" if chain.get("loops") else ""
            zone_now = ""
            if z:
                state = None if zones is None else zones.get(z)
                zone_now = (f" · zone {z} unknown" if state is None else
                            f" · mouse in zone {z}" if state
                            else f" · zone {z} empty")
            lines.append(f"Pin {steps[i]['pin']} · block {i + 1} of "
                         f"{len(steps)}{loops} · {step_text(steps[i])}"
                         f"{zone_now} · "
                         f"{clock_text(where['left_s'], up=True)} left")
        over = bool(progress) and all(w["step"] is None for w in progress)
        self._title.setText("Expected stimulation · " + (
            "over" if over else clock_text(t)))
        self._detail.setText("\n".join(lines))
        self._mini.set_state(now, done, lit)
