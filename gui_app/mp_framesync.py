"""Cross-process frame synchronisation for the multi-process acquisition split.

WHY. Splitting acquisition across processes is worth ~2.8x on grab-loop headroom,
because nine grab threads plus nine encoder threads in one
interpreter contend for one GIL. The blocker is that kick-out needs a GLOBAL
decision: trigger T is released only once every active camera has it, and
force-dropped once the fastest camera is `max_lag` ahead of the slowest. Split
the cameras across processes and that decision has nowhere to live.

THE ONE RULE THAT MAKES THIS SAFE. The forcing decision must have a **single
authority**. If two workers decided independently from slightly stale views they
would force-drop at different triggers, and the cameras would diverge SILENTLY —
equal frame counts, gapless block IDs, videos that drift apart in time. That is
the precise failure the whole pipeline exists to prevent. So: one coordinator
writes decisions, every worker only ever *reads* them.

WHAT CROSSES THE BOUNDARY: nothing large. Frame data never leaves its worker —
each encodes locally. The coordinator needs only which triggers each camera
holds, which is a bitmap. At nine cameras and a 480 ring this whole segment is
under 8 KB.

    header      magic, version, n_cams, max_lag, ring_bits, decided_upto, epoch
    frontier[]  int64 per camera — highest unwrapped block ID submitted
    heartbeat[] int64 per camera — bumped every loop, so a dead worker is visible
    retired[]   int64 per camera — nonzero once the coordinator drops it
    presence[]  n_cams x ring_bits — "camera c holds trigger T"  (worker writes)
    decision[]  ring_bits — "trigger T was released" (coordinator writes)

DECISIONS ARE ONLY MEANINGFUL FOR T <= decided_upto. A worker reads decided_upto
FIRST, then the bit; the coordinator writes the bit BEFORE advancing
decided_upto. That ordering is what makes the channel safe without a lock, and
reversing it is a silent correctness bug.

The ring is indexed by `T % ring_bits`, so a worker that falls more than
ring_bits behind would read a wrapped bit and mis-decide. `ring_bits` is
therefore sized well above `max_lag` (the coordinator never lets a camera lag
further than that), and `TriggerLedger.check_lag` fails loudly rather than
guessing if it ever happens.

EQUIVALENCE. The coordinator does NOT reimplement the release rule. It drives
the real `FrameSyncCoordinator` from `gui_app.frame_sync`, submitting block IDs
with `frame=None`, so the six properties `test_frame_sync.py` proves about the
in-process path hold here by construction. `test_mp_framesync.py` checks that
end to end over randomised drop patterns, with no cameras.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from gui_app.frame_sync import FrameSyncCoordinator

MAGIC = 0x50414E4F          # 'PANO'
VERSION = 1

# Header field offsets (int64 each, little-endian)
_H_MAGIC, _H_VERSION, _H_NCAMS, _H_MAXLAG, _H_RINGBITS, _H_DECIDED, _H_EPOCH = range(7)
_HEADER_SLOTS = 8           # one spare
_HEADER_BYTES = _HEADER_SLOTS * 8


def ring_bits_for(max_lag: int) -> int:
    """Ring size in bits. Must comfortably exceed max_lag.

    The larger of 4096 and 4x max_lag, rounded to a byte boundary: 4096 bits
    (512 bytes per bitmap) at every max_lag up to 1024, so the whole segment
    stays trivial and there is at least a 4x margin before a wrapped bit could
    be misread by a lagging worker.
    """
    return max(4096, int(max_lag) * 4 + 7 & ~7)


@dataclass(frozen=True)
class Layout:
    n_cams: int
    max_lag: int
    ring_bits: int

    @property
    def ring_bytes(self) -> int:
        return self.ring_bits // 8

    @property
    def off_frontier(self) -> int:
        return _HEADER_BYTES

    @property
    def off_heartbeat(self) -> int:
        return self.off_frontier + self.n_cams * 8

    @property
    def off_retired(self) -> int:
        return self.off_heartbeat + self.n_cams * 8

    @property
    def off_presence(self) -> int:
        return self.off_retired + self.n_cams * 8

    @property
    def off_decision(self) -> int:
        return self.off_presence + self.n_cams * self.ring_bytes

    @property
    def size(self) -> int:
        return self.off_decision + self.ring_bytes


def make_layout(n_cams: int, max_lag: int) -> Layout:
    return Layout(int(n_cams), int(max_lag), ring_bits_for(max_lag))


class _Seg:
    """Typed accessors over a buffer. Works on shared memory or a bytearray,
    which is what lets the whole protocol be tested without spawning anything."""

    def __init__(self, buf, layout: Layout):
        self.buf = buf
        self.L = layout

    # -- header ----------------------------------------------------------
    def _h_get(self, slot: int) -> int:
        return struct.unpack_from("<q", self.buf, slot * 8)[0]

    def _h_set(self, slot: int, v: int) -> None:
        struct.pack_into("<q", self.buf, slot * 8, int(v))

    @property
    def decided_upto(self) -> int:
        return self._h_get(_H_DECIDED)

    @decided_upto.setter
    def decided_upto(self, v: int) -> None:
        self._h_set(_H_DECIDED, v)

    # -- per-camera int64 arrays ------------------------------------------
    def _arr_get(self, off: int, cam: int) -> int:
        return struct.unpack_from("<q", self.buf, off + cam * 8)[0]

    def _arr_set(self, off: int, cam: int, v: int) -> None:
        struct.pack_into("<q", self.buf, off + cam * 8, int(v))

    def frontier(self, cam: int) -> int:
        return self._arr_get(self.L.off_frontier, cam)

    def set_frontier(self, cam: int, v: int) -> None:
        self._arr_set(self.L.off_frontier, cam, v)

    def heartbeat(self, cam: int) -> int:
        return self._arr_get(self.L.off_heartbeat, cam)

    def bump_heartbeat(self, cam: int) -> None:
        self._arr_set(self.L.off_heartbeat, cam,
                      self._arr_get(self.L.off_heartbeat, cam) + 1)

    def retired(self, cam: int) -> bool:
        return self._arr_get(self.L.off_retired, cam) != 0

    def set_retired(self, cam: int) -> None:
        self._arr_set(self.L.off_retired, cam, 1)

    # -- bitmaps -----------------------------------------------------------
    def _bit(self, base: int, idx: int) -> bool:
        return bool(self.buf[base + (idx >> 3)] & (1 << (idx & 7)))

    def _set_bit(self, base: int, idx: int, on: bool) -> None:
        byte = base + (idx >> 3)
        mask = 1 << (idx & 7)
        cur = self.buf[byte]
        self.buf[byte] = (cur | mask) if on else (cur & ~mask & 0xFF)

    def presence(self, cam: int, trigger: int) -> bool:
        base = self.L.off_presence + cam * self.L.ring_bytes
        return self._bit(base, trigger % self.L.ring_bits)

    def set_presence(self, cam: int, trigger: int, on: bool = True) -> None:
        base = self.L.off_presence + cam * self.L.ring_bytes
        self._set_bit(base, trigger % self.L.ring_bits, on)

    def released(self, trigger: int) -> bool:
        return self._bit(self.L.off_decision, trigger % self.L.ring_bits)

    def set_released(self, trigger: int, on: bool) -> None:
        self._set_bit(self.L.off_decision, trigger % self.L.ring_bits, on)


def init_segment(buf, n_cams: int, max_lag: int) -> Layout:
    """Zero a fresh segment and stamp its header. Coordinator side only."""
    layout = make_layout(n_cams, max_lag)
    for i in range(layout.size):
        buf[i] = 0
    seg = _Seg(buf, layout)
    seg._h_set(_H_MAGIC, MAGIC)
    seg._h_set(_H_VERSION, VERSION)
    seg._h_set(_H_NCAMS, n_cams)
    seg._h_set(_H_MAXLAG, max_lag)
    seg._h_set(_H_RINGBITS, layout.ring_bits)
    seg.decided_upto = 0
    return layout


def attach(buf) -> tuple[Layout, "_Seg"]:
    """Read a segment written by init_segment. Worker side.

    Validates the header rather than trusting it: a stale segment left by a
    crashed run has the right size and the wrong contents, and reading one as
    if it were current would mis-decide every trigger.
    """
    magic, version, n_cams, max_lag, ring_bits = struct.unpack_from("<5q", buf, 0)
    if magic != MAGIC:
        raise ValueError(f"not a Panopticon frame-sync segment (magic {magic:#x})")
    if version != VERSION:
        raise ValueError(f"segment version {version}, expected {VERSION}")
    layout = Layout(int(n_cams), int(max_lag), int(ring_bits))
    return layout, _Seg(buf, layout)


class Coordinator:
    """The single authority. Reads frontiers and presence, writes decisions.

    Drives the real FrameSyncCoordinator rather than reimplementing the release
    rule, so the equivalence properties proven in test_frame_sync.py carry over
    unchanged. Frames are submitted as None: the coordinator only decides, the
    frames themselves never leave the worker that grabbed them.
    """

    def __init__(self, buf, n_cams: int, max_lag: int):
        self.layout = init_segment(buf, n_cams, max_lag)
        self.seg = _Seg(buf, self.layout)
        self.n = n_cams
        self._core = FrameSyncCoordinator(n_cams, max_lag=max_lag)
        self._submitted = [0] * n_cams   # highest trigger fed to the core
        self.released_triggers = 0
        self.dropped_triggers = 0

    def retire(self, cam: int, reason: str = "") -> None:
        self.seg.set_retired(cam)
        self._core.retire(cam, reason)

    def poll(self) -> int:
        """Feed any newly-present triggers to the core and publish decisions.

        Returns how many triggers were decided this call. Call it often (~1 kHz);
        it is pure integer work over a few KB.
        """
        seg = self.seg
        # Submit everything each camera has gained since the last poll, in
        # order. submit() RETURNS the triggers it just released, which is the
        # only place that information exists — the core keeps no released set.
        released_now: set[int] = set()
        for cam in range(self.n):
            if seg.retired(cam):
                continue
            front = seg.frontier(cam)
            t = self._submitted[cam] + 1
            while t <= front:
                if seg.presence(cam, t):
                    # frame=None: the coordinator only decides. The frame stays
                    # in the worker that grabbed it and never crosses over.
                    for (_c, bid, _f) in self._core.submit(cam, t, None):
                        released_now.add(bid)
                t += 1
            self._submitted[cam] = front

        decided = self._core.decided_upto
        n_new = 0
        prev = seg.decided_upto
        if decided > prev:
            for t in range(prev + 1, decided + 1):
                was_released = t in released_now
                # ORDER MATTERS: write the bit, THEN advance decided_upto. A
                # worker reads decided_upto first, so it can never observe a
                # bit that has not been written yet.
                seg.set_released(t, was_released)
                if was_released:
                    self.released_triggers += 1
                else:
                    self.dropped_triggers += 1
                n_new += 1
            seg.decided_upto = decided
        return n_new


    def flush(self) -> int:
        """End of recording: decide every remaining trigger and publish it.

        Without this the tail of every session stays undecided — the core only
        advances to the watermark, so the last frames each camera grabbed are
        held forever and never reach an encoder. Caught by test 7, which asserts
        a worker learns the fate of every trigger it announced.
        """
        seg = self.seg
        released_now = {bid for (_c, bid, _f) in self._core.flush()}
        decided = self._core.decided_upto
        prev = seg.decided_upto
        n_new = 0
        for t in range(prev + 1, decided + 1):
            was_released = t in released_now
            seg.set_released(t, was_released)
            if was_released:
                self.released_triggers += 1
            else:
                self.dropped_triggers += 1
            n_new += 1
        if decided > prev:
            seg.decided_upto = decided
        return n_new


class WorkerLedger:
    """Worker side. Announces what it has; asks what to do with it."""

    def __init__(self, buf, cam: int):
        self.layout, self.seg = attach(buf)
        self.cam = cam
        self._pending: list[int] = []     # triggers grabbed, not yet decided
        self.lag_error: str | None = None

    def announce(self, trigger: int) -> None:
        """This camera has grabbed `trigger`. Presence bit BEFORE frontier:
        the coordinator reads the frontier to decide how far to scan, so a
        frontier visible ahead of its presence bit would make it miss one."""
        self.seg.set_presence(self.cam, trigger, True)
        self.seg.set_frontier(self.cam, trigger)
        self._pending.append(trigger)
        self.seg.bump_heartbeat(self.cam)

    def check_lag(self, trigger: int) -> bool:
        """False if this worker has fallen so far behind that the ring wrapped.

        Reading a wrapped bit would silently mis-decide, so callers must treat
        False as fatal for this camera and retire it rather than continue.
        """
        behind = self.seg.decided_upto - trigger
        if behind >= self.layout.ring_bits:
            self.lag_error = (f"cam{self.cam} is {behind} triggers behind the "
                              f"coordinator; ring holds {self.layout.ring_bits}")
            return False
        return True

    def harvest(self):
        """Triggers now decided, as (trigger, released) in increasing order.

        Read decided_upto FIRST, then the bits — the coordinator writes bits
        before advancing it, so this ordering can never read an unwritten bit.

        Each decided trigger's presence bit is cleared HERE, by the worker that
        set it. The ring is indexed by T % ring_bits, so a bit left set would
        alias to trigger T + ring_bits and make the coordinator submit a
        trigger this camera never announced: the other cameras would encode it
        and this one would not, which is the equal-count drifting misalignment
        the module exists to prevent. The worker is the only writer of its own
        bitmap (a second writer across processes would be a lost-update race on
        the shared byte), and a late announce the coordinator then skips would
        have been dropped as late anyway, so the outcome is unchanged.
        """
        decided = self.seg.decided_upto
        out = []
        keep = []
        for t in self._pending:
            if t > decided:
                keep.append(t)
                continue
            if not self.check_lag(t):
                keep.append(t)
                continue
            out.append((t, self.seg.released(t)))
            self.seg.set_presence(self.cam, t, False)
        self._pending = keep
        return out
