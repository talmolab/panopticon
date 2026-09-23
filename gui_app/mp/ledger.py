"""Cross-process kick-out ledger (version 2).

Kick-out releases trigger T only once every active camera holds it, and
force-drops T once the fastest camera is `max_lag` ahead of the slowest. With
the cameras split across processes, that decision has one authority: the
parent's `Coordinator`, which drives the real `FrameSyncCoordinator` with
`frame=None`. Workers only announce what they grabbed and read decisions back.
Two authorities deciding from slightly different views would force-drop at
different triggers, and the videos would drift apart with equal frame counts
and gapless block IDs. Frames never cross the boundary; each worker encodes
its own.

Layout (`Layout` computes the offsets; every region starts 8-byte aligned):

    header      16 x int64: magic, version, n_cams, max_lag, ring_bits,
                decided_upto, epoch, coord_heartbeat_ns, flushed, state,
                reason_bytes, size, deciding_upto, spare x 3
    per camera  int64 arrays: frontier, announces, eos, retire_req,
                retired, retired_at, entering
    presence    n_cams x ring_bytes  "camera c holds trigger T" (worker writes)
    decision    ring_bytes           "trigger T was released" (coordinator writes)
    reasons     n_cams x REASON_BYTES  UTF-8 retirement reason (worker writes)

Every int64 goes through an aligned numpy view (`gui_app.mp.shm`), one element
per access, so no read can tear.

Publication orders. Each is a store order the writer keeps and a load order the
reader keeps; x86-64 preserves both, which is why `shm` refuses other CPUs.

- Announce: `entering`, presence bit, then `frontier`. The coordinator reads
  `frontier` first and scans bits up to it, so it never reads past a bit not
  yet set. It reads `entering` after the scan (see the ring, below).
- Decide: `deciding_upto`, decision bits, then `decided_upto`. A worker reads
  `decided_upto` first, then bits at or below it, then `deciding_upto` (see
  the ring, below).
- Retire: `retired_at`, then `retired`. `retired_at` is the `decided_upto`
  already published, so every trigger at or below it was decided with the
  camera still in the set. A worker reads `decided_upto`, then `retired`, then
  `retired_at`. If it sees `retired`, it keeps its frames up to `retired_at`
  (by their bits) and drops every later one. If it does not, the
  `decided_upto` it read predates the retirement.
- Retire request: reason text, then `retire_req`.
- End of stream: a worker's final announce, then `eos`. `flush` checks `eos`
  before its last poll, so that poll sees every announce.
- Flush: bits, `decided_upto`, then `flushed`. A worker that sees `flushed`
  knows no later trigger will be decided, and drops what it still holds.

A worker's harvest therefore loads `flushed`, `decided_upto`, `retired`,
`retired_at`, the decision bits, and `deciding_upto`, in that order.

The ring. Bits are indexed `T % ring_bits`, so trigger T shares its bit with
T + ring_bits. These rules tie every bit a reader keeps to one trigger:

- A worker keeps its pending triggers within `ring_bits` of the newest.
  Announce refuses a trigger that would alias an older bit still set, and a
  worker clears a bit once its trigger is decided.
- The coordinator scans only the last `ring_bits` triggers below a camera's
  frontier. The bit of any older trigger was cleared before that frontier was
  stored.
- A worker can announce past that frontier while the coordinator scans. The
  new bit then sits where the scan expects a trigger `ring_bits` older.
  Announce stores `entering` before the bit, and the coordinator loads it
  after the scan and discards every scanned trigger at or below
  `entering - ring_bits`. A trigger the camera still holds is never that far
  below, because announce refuses it. A trigger the camera grabbed and has
  since harvested was decided before this poll, and the core would drop it as
  late anyway.
- A publish can rewrite a decision bit while a worker reads it. The publish
  stores `deciding_upto` before its first bit, and the harvest loads it after
  its last. A bit a later trigger already took therefore shows up as a
  `deciding_upto` at least `ring_bits` past the trigger read. Harvest then
  stops, returns nothing and sets `lag_error`, and the caller retires the
  camera.

While its camera is stalled the coordinator keeps deciding at the trigger
rate (forcing), so a worker harvests on its grab loop's timeouts too, not only
when a frame arrives: a harvest must come at least every `ring_bits / fps`
seconds.

Workers read `retire_req`, `retired` and the decision bits of their own
cameras only, and each presence bitmap has one writer at a time: the camera's
grab thread while it runs, then the worker's control thread after `eos`.
"""
from __future__ import annotations

import enum
import threading
from collections import deque
from dataclasses import dataclass

import numpy as np

from gui_app.frame_sync import FrameSyncCoordinator
from gui_app.mp.shm import (SegmentMismatch, SharedSegment, int64_view,
                            new_epoch, now_ns, segment_name, uint8_view)

MAGIC = 0x50414E4F          # 'PANO'
VERSION = 2

HEADER_SLOTS = 16
HEADER_BYTES = HEADER_SLOTS * 8
(H_MAGIC, H_VERSION, H_NCAMS, H_MAXLAG, H_RINGBITS, H_DECIDED, H_EPOCH,
 H_COORD_HB, H_FLUSHED, H_STATE, H_REASON_BYTES, H_SIZE,
 H_DECIDING) = range(13)

#: Bytes of UTF-8 retirement reason per camera, NUL-padded.
REASON_BYTES = 256
#: Per-camera int64 arrays, in layout order.
CAMERA_ARRAYS = ("frontier", "announces", "eos", "retire_req", "retired",
                 "retired_at", "entering")


class LedgerState(enum.IntEnum):
    INIT = 0
    OPEN = 1
    FLUSHED = 2
    ABANDONED = 3


class LedgerError(RuntimeError):
    """The ledger was used out of protocol (for example, flushed before every
    active camera reached end of stream)."""


def ring_bits_for(max_lag: int) -> int:
    """Ring size in bits: the larger of 4096 and 4 x max_lag, in whole 64-bit words.

    4096 bits (512 bytes per bitmap) covers every max_lag up to 1024, and the
    4x margin keeps a lagging worker well clear of a reused bit.
    """
    return max(4096, (int(max_lag) * 4 + 63) // 64 * 64)


@dataclass(frozen=True)
class Layout:
    n_cams: int
    max_lag: int
    ring_bits: int

    @property
    def ring_bytes(self) -> int:
        return self.ring_bits // 8

    def off_array(self, name: str) -> int:
        return HEADER_BYTES + CAMERA_ARRAYS.index(name) * self.n_cams * 8

    @property
    def off_presence(self) -> int:
        return HEADER_BYTES + len(CAMERA_ARRAYS) * self.n_cams * 8

    @property
    def off_decision(self) -> int:
        return self.off_presence + self.n_cams * self.ring_bytes

    @property
    def off_reasons(self) -> int:
        return self.off_decision + self.ring_bytes

    @property
    def size(self) -> int:
        return self.off_reasons + self.n_cams * REASON_BYTES


def make_layout(n_cams: int, max_lag: int) -> Layout:
    n_cams, max_lag = int(n_cams), int(max_lag)
    if n_cams < 1:
        raise ValueError(f"the ledger needs at least one camera, got {n_cams}")
    if max_lag < 1:
        raise ValueError(f"max_lag must be at least 1, got {max_lag}")
    return Layout(n_cams, max_lag, ring_bits_for(max_lag))


class _Views:
    """Aligned numpy views over one ledger buffer."""

    def __init__(self, buf, layout: Layout):
        n = layout.n_cams
        self.hdr = int64_view(buf, 0, HEADER_SLOTS)
        for name in CAMERA_ARRAYS:
            setattr(self, name, int64_view(buf, layout.off_array(name), n))
        self.presence = uint8_view(buf, layout.off_presence,
                                   n * layout.ring_bytes).reshape(n, layout.ring_bytes)
        self.decision = uint8_view(buf, layout.off_decision, layout.ring_bytes)
        self.reasons = uint8_view(buf, layout.off_reasons,
                                  n * REASON_BYTES).reshape(n, REASON_BYTES)


def init_segment(buf, n_cams: int, max_lag: int, *, epoch: int) -> Layout:
    """Zero a fresh ledger and stamp its header. Coordinator side only.

    The magic goes last, so a worker that attaches to a half-written ledger
    is refused rather than reading zeros as decisions.
    """
    epoch = int(epoch)
    if epoch <= 0:
        raise ValueError(f"epoch must be a positive int64, got {epoch}")
    layout = make_layout(n_cams, max_lag)
    if len(buf) < layout.size:
        raise ValueError(f"ledger buffer is {len(buf)} bytes, the layout needs "
                         f"{layout.size}")
    whole = uint8_view(buf, 0, layout.size)
    whole[:] = 0
    del whole
    hdr = int64_view(buf, 0, HEADER_SLOTS)
    hdr[H_VERSION] = VERSION
    hdr[H_NCAMS] = layout.n_cams
    hdr[H_MAXLAG] = layout.max_lag
    hdr[H_RINGBITS] = layout.ring_bits
    hdr[H_DECIDED] = 0
    hdr[H_DECIDING] = 0
    hdr[H_EPOCH] = epoch
    hdr[H_STATE] = LedgerState.OPEN
    hdr[H_REASON_BYTES] = REASON_BYTES
    hdr[H_SIZE] = layout.size
    hdr[H_MAGIC] = MAGIC
    return layout


def attach(buf, *, epoch: int) -> Layout:
    """Validate a ledger written by `init_segment` and return its layout.

    Every field is checked rather than trusted. A foreign buffer, a
    half-initialised ledger, another version, or another acquisition's
    ledger (a different epoch, same magic and version) is refused with
    `SegmentMismatch`, because reading one as current would mis-decide
    every trigger.
    """
    want = int(epoch)
    if want <= 0:
        raise SegmentMismatch(f"attach needs this acquisition's epoch, got {epoch!r}")
    if len(buf) < HEADER_BYTES:
        raise SegmentMismatch(f"buffer of {len(buf)} bytes is smaller than a "
                              f"ledger header")
    hdr = int64_view(buf, 0, HEADER_SLOTS)
    magic, version = int(hdr[H_MAGIC]), int(hdr[H_VERSION])
    n_cams, max_lag = int(hdr[H_NCAMS]), int(hdr[H_MAXLAG])
    ring_bits, got = int(hdr[H_RINGBITS]), int(hdr[H_EPOCH])
    reason_bytes, size = int(hdr[H_REASON_BYTES]), int(hdr[H_SIZE])
    del hdr
    if magic != MAGIC:
        raise SegmentMismatch(f"not a Panopticon ledger segment (magic {magic:#x})")
    if version != VERSION:
        raise SegmentMismatch(f"ledger version {version}, expected {VERSION}")
    if got != want:
        raise SegmentMismatch(
            f"ledger epoch {got:#x} does not match this acquisition's {want:#x}: "
            f"the segment belongs to another acquisition")
    if n_cams < 1 or max_lag < 1 or ring_bits != ring_bits_for(max_lag):
        raise SegmentMismatch(f"ledger header is inconsistent: n_cams={n_cams} "
                              f"max_lag={max_lag} ring_bits={ring_bits}")
    layout = Layout(n_cams, max_lag, ring_bits)
    if reason_bytes != REASON_BYTES or size != layout.size:
        raise SegmentMismatch(f"ledger layout differs: {size} bytes and "
                              f"{reason_bytes}-byte reasons, expected "
                              f"{layout.size} and {REASON_BYTES}")
    if len(buf) < layout.size:
        raise SegmentMismatch(f"ledger buffer is {len(buf)} bytes, the header "
                              f"describes {layout.size}")
    return layout


def _bit(row: np.ndarray, idx: int) -> bool:
    return bool(int(row[idx >> 3]) & (1 << (idx & 7)))


def _set_bit(row: np.ndarray, idx: int, on: bool) -> None:
    byte, mask = idx >> 3, 1 << (idx & 7)
    cur = int(row[byte])
    row[byte] = (cur | mask) if on else (cur & (0xFF ^ mask))


class Coordinator:
    """The single authority: reads frontiers and presence, writes decisions.

    Drives the real `FrameSyncCoordinator`, so the properties
    `test_frame_sync.py` proves for the in-process path hold here. `poll`,
    `retire` and `flush` may be called from different parent threads (the
    coordinator loop, the supervisor, the stop path); one lock serialises
    them. `lag_frames` and `lag_report` read without it, as the in-process
    router's do.
    """

    def __init__(self, buf, n_cams: int, max_lag: int, *, epoch: int):
        self.epoch = int(epoch)
        self.layout = init_segment(buf, n_cams, max_lag, epoch=self.epoch)
        self._v = _Views(buf, self.layout)
        self.n = self.layout.n_cams
        self._core = FrameSyncCoordinator(self.n, max_lag=self.layout.max_lag)
        self._lock = threading.Lock()
        #: Highest trigger fed to the core per camera. Only ever raised.
        self._submitted = [0] * self.n
        self._published = 0
        self._retired = [False] * self.n
        self._closed_state = False
        start = now_ns()
        self._progress_ns = [start] * self.n
        self.released_triggers = 0
        self.dropped_triggers = 0

    # -- read-only accessors --------------------------------------------------

    @property
    def core(self) -> FrameSyncCoordinator:
        return self._core

    @property
    def decided_upto(self) -> int:
        return self._published

    @property
    def retired_reasons(self) -> list:
        return self._core.retired_reasons

    @property
    def state(self) -> LedgerState:
        return LedgerState(int(self._v.hdr[H_STATE]))

    def lag_frames(self) -> list:
        return self._core.lag_frames()

    def lag_report(self) -> str:
        return self._core.lag_report()

    def eos_missing(self) -> list:
        """Active cameras that have not reached end of stream."""
        return [c for c in range(self.n)
                if not self._retired[c] and not int(self._v.eos[c])]

    def all_eos(self) -> bool:
        return not self.eos_missing()

    def progress_age_s(self, cam: int, t_ns: int | None = None) -> float:
        """Seconds since this camera's frontier last advanced in a poll (or
        since the ledger was created)."""
        return ((now_ns() if t_ns is None else int(t_ns))
                - self._progress_ns[cam]) / 1e9

    # -- decisions ------------------------------------------------------------

    def poll(self, t_ns: int | None = None) -> int:
        """Feed newly announced triggers to the core and publish its decisions.

        Returns the number of triggers decided by this call. Retirement
        requests are honoured first. New triggers are fed in trigger-major
        order, which is the order the hardware produces them, so a poll that
        comes late (a stalled parent) submits the same sequence an on-time
        one would and forces nothing extra.
        """
        with self._lock:
            n_new, msgs = self._poll_locked(now_ns() if t_ns is None else int(t_ns))
        for m in msgs:
            print(m, flush=True)
        return n_new

    def retire(self, cam: int, reason: str = "", announce: bool = True):
        """Drop a camera from the alignment set. Returns the log line, or None
        if it was already retired."""
        with self._lock:
            msg = self._retire_locked(cam, reason)
        if msg and announce:
            print(msg, flush=True)
        return msg

    def flush(self, require_eos: bool = True) -> int:
        """End of acquisition: poll once more, then decide every trigger held.

        Refuses (LedgerError) while an active camera has not reached end of
        stream, because announces it makes after the flush are never decided.
        Retire such a camera first, or pass require_eos=False knowingly.
        """
        with self._lock:
            missing = [c for c in range(self.n)
                       if not self._retired[c] and not int(self._v.eos[c])]
            if missing and require_eos:
                names = ", ".join(f"cam{c + 1}" for c in missing)
                raise LedgerError(f"flush refused: {names} has not reached end "
                                  f"of stream; retire it or wait for eos")
            n_new, msgs = self._poll_locked(now_ns())
            released_now = {bid for (_c, bid, _f) in self._core.flush()}
            n_new += self._publish_locked(released_now)
            hdr = self._v.hdr
            hdr[H_FLUSHED] = 1
            if int(hdr[H_STATE]) != LedgerState.ABANDONED:
                hdr[H_STATE] = LedgerState.FLUSHED
            self._closed_state = True
        for m in msgs:
            print(m, flush=True)
        return n_new

    def abandon(self) -> None:
        """Mark the acquisition abandoned. Workers read it from `state`."""
        with self._lock:
            self._v.hdr[H_STATE] = LedgerState.ABANDONED
            self._closed_state = True

    def close(self) -> None:
        """Drop the views over the buffer, so its segment can be closed."""
        with self._lock:
            self._v = None

    # -- internals (lock held) --------------------------------------------------

    def _reason(self, cam: int) -> str:
        raw = bytes(self._v.reasons[cam])
        text = raw.split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()
        return text or "its capture process asked to retire it"

    def _retire_locked(self, cam: int, reason: str):
        if self._retired[cam]:
            return None
        v = self._v
        # Every decision the core has made is published between calls, so
        # _published is the core's decided_upto here.
        v.retired_at[cam] = self._published
        v.retired[cam] = 1
        self._retired[cam] = True
        return self._core.retire(cam, reason, announce=False)

    def _poll_locked(self, t_ns: int):
        v = self._v
        v.hdr[H_COORD_HB] = t_ns
        msgs = []
        if self._closed_state:
            return 0, msgs
        for cam in range(self.n):
            if not self._retired[cam] and int(v.retire_req[cam]):
                msg = self._retire_locked(cam, self._reason(cam))
                if msg:
                    msgs.append(msg)
        rb = self.layout.ring_bits
        items = []
        for cam in range(self.n):
            if self._retired[cam]:
                continue
            front = int(v.frontier[cam])
            start = self._submitted[cam] + 1
            if front < start:
                # Nothing new, or a frontier read below one already fed: never
                # re-submit, the core would take a duplicate as a new frame.
                continue
            row = v.presence[cam]
            # Only the last ring_bits triggers can have a bit set.
            held = [t for t in range(max(start, front - rb + 1), front + 1)
                    if _bit(row, t % rb)]
            # Loaded after the bits. A bit set by an announce past `front`
            # sits where the scan expects a trigger ring_bits older, and that
            # announce stored `entering` first. No trigger the camera still
            # holds is ring_bits below `entering`: announce refuses that.
            floor = int(v.entering[cam]) - rb
            items.extend((t, cam) for t in held if t > floor)
            self._submitted[cam] = front
            self._progress_ns[cam] = t_ns
        items.sort()
        released_now = set()
        for t, cam in items:
            for (_c, bid, _f) in self._core.submit(cam, t, None):
                released_now.add(bid)
        return self._publish_locked(released_now), msgs

    def _publish_locked(self, released_now: set) -> int:
        decided = self._core.decided_upto
        prev = self._published
        if decided < prev:
            raise LedgerError(f"the core's decided_upto moved backwards "
                              f"({prev} -> {decided}); decisions already "
                              f"published cannot be taken back")
        if decided == prev:
            return 0
        rb = self.layout.ring_bits
        dec = self._v.decision
        # The bound goes out before the first bit, so a worker that reads a
        # bit this publish rewrote also reads a bound that condemns the read.
        self._v.hdr[H_DECIDING] = decided
        for t in range(prev + 1, decided + 1):
            released = t in released_now
            _set_bit(dec, t % rb, released)
            if released:
                self.released_triggers += 1
            else:
                self.dropped_triggers += 1
        self._v.hdr[H_DECIDED] = decided
        self._published = decided
        return decided - prev


class WorkerLedger:
    """One camera's side of the ledger, in the worker that grabs it.

    Not thread-safe: one thread at a time uses it (the grab thread while it
    runs, then the control thread for the final harvest).
    """

    def __init__(self, buf, cam: int, *, epoch: int):
        self.layout = attach(buf, epoch=epoch)
        if not 0 <= int(cam) < self.layout.n_cams:
            raise ValueError(f"camera index {cam} is outside this ledger's "
                             f"{self.layout.n_cams} cameras")
        self.epoch = int(epoch)
        self.cam = int(cam)
        self._v = _Views(buf, self.layout)
        self._row = self._v.presence[self.cam]
        self._pending: deque = deque()   # bit set, fate not yet read
        self._ready: list = []           # fates read by announce, not yet returned
        self._last = 0
        self._announces = 0
        self._retire_requested = False
        #: Why harvest stopped: the coordinator ran ring_bits past a trigger
        #: this worker still holds. The caller retires the camera.
        self.lag_error: str | None = None
        #: Triggers announce refused because an older bit within ring_bits
        #: was still undecided (the coordinator is not polling).
        self.refused_span = 0

    # -- state read from the coordinator ---------------------------------------

    @property
    def decided_upto(self) -> int:
        return int(self._v.hdr[H_DECIDED])

    @property
    def flushed(self) -> bool:
        return bool(int(self._v.hdr[H_FLUSHED]))

    @property
    def state(self) -> LedgerState:
        return LedgerState(int(self._v.hdr[H_STATE]))

    @property
    def retired(self) -> bool:
        return bool(int(self._v.retired[self.cam]))

    @property
    def retired_at(self) -> int | None:
        return int(self._v.retired_at[self.cam]) if self.retired else None

    @property
    def retire_requested(self) -> bool:
        return self._retire_requested

    @property
    def pending(self) -> int:
        return len(self._pending)

    def coordinator_age_s(self, t_ns: int | None = None) -> float | None:
        """Seconds since the coordinator last polled; None if it never has."""
        hb = int(self._v.hdr[H_COORD_HB])
        if hb == 0:
            return None
        return ((now_ns() if t_ns is None else int(t_ns)) - hb) / 1e9

    # -- announcing -------------------------------------------------------------

    def announce(self, trigger: int) -> bool:
        """Record that this camera grabbed `trigger` (unwrapped, increasing).

        Returns False when the frame is not entered and counts as dropped now:
        the camera is retired or its retirement was requested, or entering it
        would alias an older trigger still undecided (see the module
        docstring). The caller frees its ring slot at once. A trigger at or
        below the last one announced raises ValueError, because a frontier
        must never move backwards.
        """
        t = int(trigger)
        if t <= self._last:
            raise ValueError(
                f"cam{self.cam + 1}: trigger {t} does not advance past "
                f"{self._last}; the ledger takes unwrapped, increasing triggers")
        if self._retire_requested or int(self._v.retired[self.cam]):
            return False
        rb = self.layout.ring_bits
        if self._pending and t - self._pending[0] >= rb:
            self._collect(self._ready)
            if self._pending and t - self._pending[0] >= rb:
                self.refused_span += 1
                return False
        # Before the bit: a coordinator that reads this bit as an older
        # trigger's then reads an `entering` that tells it otherwise.
        self._v.entering[self.cam] = t
        _set_bit(self._row, t % rb, True)
        self._v.frontier[self.cam] = t
        self._announces += 1
        self._v.announces[self.cam] = self._announces
        self._last = t
        self._pending.append(t)
        return True

    def request_retire(self, reason: str = "") -> None:
        """Ask the coordinator to retire this camera (a grab thread gave up).

        The reason text goes in before the flag. Only the first request
        writes, so the coordinator never reads a reason being overwritten.
        Frames announced after this call are refused.
        """
        if self._retire_requested:
            return
        self._retire_requested = True
        raw = str(reason).encode("utf-8")[:REASON_BYTES - 1]
        raw = raw.decode("utf-8", errors="ignore").encode("utf-8")
        area = self._v.reasons[self.cam]
        area[:] = 0
        if raw:
            area[:len(raw)] = np.frombuffer(raw, dtype=np.uint8)
        self._v.retire_req[self.cam] = 1

    def set_eos(self) -> None:
        """This camera will announce nothing more. Call after the final announce."""
        self._v.eos[self.cam] = 1

    # -- reading decisions ------------------------------------------------------

    def check_lag(self, trigger: int) -> bool:
        """False if the coordinator is deciding ring_bits or more past
        `trigger`, so its bit may already hold a later trigger's decision. The
        caller retires the camera.

        Reads `deciding_upto`, not `decided_upto`: a publish rewrites bits
        before it stores `decided_upto`, so only the bound it stores first
        covers a publish still in progress."""
        behind = int(self._v.hdr[H_DECIDING]) - int(trigger)
        if behind >= self.layout.ring_bits:
            self.lag_error = (f"cam{self.cam + 1} is {behind} triggers behind the "
                              f"coordinator; the ring holds {self.layout.ring_bits}")
            return False
        return True

    def harvest(self) -> list:
        """Triggers whose fate is now known, as (trigger, released), in
        increasing order. Each is returned once, and its presence bit is
        cleared here by the worker that set it."""
        out, self._ready = self._ready, []
        self._collect(out)
        return out

    def drop_pending(self) -> list:
        """Like harvest(), but every trigger still undecided is returned as
        (trigger, False) and its bit cleared. For a camera that stops after a
        lag error, or on abandon. Fates already read keep their value."""
        rb = self.layout.ring_bits
        out, self._ready = self._ready, []
        while self._pending:
            t = self._pending.popleft()
            _set_bit(self._row, t % rb, False)
            out.append((t, False))
        return out

    def close(self) -> None:
        """Drop the views over the buffer, so its segment can be closed."""
        self._v = None
        self._row = None

    def _collect(self, out: list) -> None:
        pend = self._pending
        if not pend:
            return
        v = self._v
        hdr = v.hdr
        rb = self.layout.ring_bits
        # Load order: flushed, decided_upto, retired, retired_at (module docstring).
        flushed = int(hdr[H_FLUSHED])
        decided = int(hdr[H_DECIDED])
        retired = int(v.retired[self.cam])
        limit = int(v.retired_at[self.cam]) if retired else decided
        got = []
        for t in pend:
            if t > limit:
                break
            got.append((t, _bit(v.decision, t % rb)))
        if got:
            # A publish stores deciding_upto before it rewrites any bit, so a
            # bit read here that a later trigger already took implies a
            # deciding_upto at least ring_bits past the oldest trigger read.
            # Loaded after the bits, it catches a publish still in progress.
            after = int(hdr[H_DECIDING])
            behind = after - got[0][0]
            if behind >= rb:
                self.lag_error = (
                    f"cam{self.cam + 1}: trigger {got[0][0]} is {behind} behind "
                    f"the coordinator's decisions and the ring holds {rb}, so "
                    f"its decision bit may belong to a later trigger")
                return
        for item in got:
            t = pend.popleft()
            _set_bit(self._row, t % rb, False)
            out.append(item)
        if retired or flushed:
            # Nothing above the limit will be decided with this camera in the set.
            while pend:
                t = pend.popleft()
                _set_bit(self._row, t % rb, False)
                out.append((t, False))


class RingGuard:
    """Occupancy of one camera's NV12 ring in a capture worker.

    In process, the coordinator's forcing bounds how many frames wait for a
    decision. Across processes that bound holds only while the coordinator
    polls, so the worker checks the slot it is about to overwrite: if that
    slot's frame still waits for a decision or for the encoder, the new
    frame is dropped before it is copied or announced, and the loss is a gap
    in blockids.npy rather than newer pixels under an older block ID.

    Slots are written in ring order. A slot is busy from `claim` until its
    frame is dropped (`drop`) or handed to the encoder (`hand_off`) and then
    consumed. The encoder frees slots in hand-off order, so the guard needs
    only the encoder's `consumed` count, read through `consumed_fn`. A count
    of outstanding frames alone is not enough: a frame waiting in the encoder
    queue behind a stall keeps its slot busy while later frames are dropped
    around it.

    One thread at a time mutates the guard (the grab thread, then the
    control thread for the final harvest); `consumed_fn` is read from the
    encoder thread's counter.
    """

    def __init__(self, n_slots: int, consumed_fn=None):
        if int(n_slots) < 1:
            raise ValueError("a ring needs at least one slot")
        self.n_slots = int(n_slots)
        self._busy = bytearray(self.n_slots)
        self._next = 0
        self._handed: deque = deque()
        self._consumed_fn = consumed_fn
        self._consumed_local = 0
        self._consumed_seen = 0
        self.copied = 0
        self.dropped = 0
        self.refused = 0

    @property
    def next_slot(self) -> int:
        return self._next

    @property
    def consumed(self) -> int:
        return int(self._consumed_fn()) if self._consumed_fn else self._consumed_local

    @property
    def outstanding(self) -> int:
        """Frames copied and not yet dropped or consumed."""
        return self.copied - self.dropped - self.consumed

    def _sync(self) -> None:
        consumed = self.consumed
        while self._consumed_seen < consumed and self._handed:
            self._busy[self._handed.popleft()] = 0
            self._consumed_seen += 1

    def slot_free(self) -> bool:
        """True if the next slot may be written."""
        if not self._busy[self._next]:
            return True
        self._sync()
        return not self._busy[self._next]

    def claim(self) -> int:
        """Take the next slot for a new frame. Call only after slot_free()."""
        if not self.slot_free():
            raise RuntimeError(f"ring slot {self._next} still holds a frame "
                               f"awaiting a decision or the encoder")
        slot = self._next
        self._busy[slot] = 1
        self._next = (slot + 1) % self.n_slots
        self.copied += 1
        return slot

    def refuse(self) -> None:
        """Count a new frame dropped because slot_free() was False."""
        self.refused += 1

    def drop(self, slot: int) -> None:
        """The frame in `slot` was dropped (by decision, or not entered)."""
        if not self._busy[slot]:
            raise RuntimeError(f"ring slot {slot} is not held")
        self._busy[slot] = 0
        self.dropped += 1

    def hand_off(self, slot: int) -> None:
        """The frame in `slot` was queued to the encoder. Call after the queue
        accepted it (a refused put is a `drop`), in queue order: the guard
        matches the encoder's consumed count to hand-offs one for one."""
        if not self._busy[slot]:
            raise RuntimeError(f"ring slot {slot} is not held")
        self._handed.append(slot)

    def mark_consumed(self, n: int = 1) -> None:
        """Without a consumed_fn: the encoder finished `n` more queued frames."""
        if self._consumed_fn is not None:
            raise RuntimeError("this guard reads the encoder's own counter")
        self._consumed_local += int(n)


# -- named segments ---------------------------------------------------------------

def create_shared(n_cams: int, max_lag: int, *, epoch: int | None = None,
                  parent_pid: int | None = None) -> tuple:
    """Create one acquisition's ledger segment. Returns (segment, Coordinator).

    Close the Coordinator before the segment."""
    epoch = new_epoch() if epoch is None else int(epoch)
    layout = make_layout(n_cams, max_lag)
    seg = SharedSegment.create(segment_name("ledger", parent_pid=parent_pid,
                                            epoch=epoch), layout.size)
    try:
        coord = Coordinator(seg.buf, n_cams, max_lag, epoch=epoch)
    except BaseException:
        seg.close()
        seg.unlink()
        raise
    return seg, coord


def attach_shared(name: str, cams, *, epoch: int) -> tuple:
    """Attach a worker to a ledger segment. Returns (segment, [WorkerLedger]),
    one ledger per camera index in `cams`. Close the ledgers before the segment."""
    seg = SharedSegment.attach(name, min_size=HEADER_BYTES)
    ledgers = []
    try:
        for cam in cams:
            ledgers.append(WorkerLedger(seg.buf, cam, epoch=epoch))
    except BaseException:
        for led in ledgers:
            led.close()
        seg.close()
        raise
    return seg, ledgers
