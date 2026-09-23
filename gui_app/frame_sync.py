"""Real-time cross-camera frame sync — the "kick out frames not seen by every
camera, before they're encoded" path.

Each grab thread submits its successfully-grabbed frames in trigger (block-ID)
order. The coordinator releases a trigger to the encoders ONLY once every camera
has reached it AND all of them captured it; triggers any camera missed are
dropped before encoding. So each camera's encoder receives a gapless stream of
frames that are identical across cameras — normal GOP encoding then yields
equal-length, trigger-aligned videos with no post-hoc re-encode.

This is pure logic (no Qt, no pylon, no frame copies of its own), which is what
lets the offline test suite prove it equivalent to the post-hoc block-ID
intersection headlessly. Keep it that way: it is now the DEFAULT capture path
(`realtime_kick: true`), so this equivalence proof is the only thing standing
between a change here and silently misaligned recordings. Frame objects are
opaque pass-through tokens.

Design notes:
- Cameras are hardware-triggered in lockstep, so confirmation lag is ~1-2
  frames. A camera that missed trigger N reveals it by delivering N+1 (a gap in
  its block-ID sequence); a camera with incomplete/underrun frames simply never
  submits that block ID.
- `max_lag` bounds how far ahead the fastest camera may get before a lagging
  camera's missing triggers are force-dropped — so one stalled camera can't
  freeze the others (it only drops triggers the post-hoc intersection would
  drop too). Set it comfortably above the resend-recovery window.
- Block IDs are unwrapped per camera (16-bit GVSP wrap at 65535) so the path
  works even if 64-bit extended IDs aren't honored.
"""
from collections import deque

#: The period of a 16-bit GigE Vision block-ID counter: it counts 1..65535 and
#: skips 0, so ID 65535 is followed by ID 1. Every module that unwraps block
#: IDs takes the period from here, because a live unwrap and a post-hoc one
#: that disagree on it place the same frame at different trigger ordinals.
BLOCKID_WRAP = 65535


class UnwrapState:
    """What `unwrap_one` remembers about one camera's block-ID stream."""

    __slots__ = ("seen", "last_raw", "offset")

    def __init__(self):
        self.seen = False     # whether any ID has been unwrapped yet
        self.last_raw = 0     # the previous raw ID
        self.offset = 0       # wraps so far, times BLOCKID_WRAP


def unwrap_one(raw: int, state: UnwrapState) -> int:
    """Unwrap the next raw block ID of one camera's stream.

    RULE: a raw ID more than half a period below the previous one is a wrap,
    and anything else is taken as it stands. REASON: every camera is
    triggered by the same board and starts at the same ID, so each one wraps
    at the same trigger, and counting its wraps from its own first ID yields
    ordinals that agree across cameras. A drop of less than half a period
    cannot be a wrap, because no camera falls 32767 triggers behind itself.

    `state` carries the stream's history and is updated in place. Callers
    keep one state per camera, fed in the order the camera delivered.
    """
    if state.seen and raw < state.last_raw - (BLOCKID_WRAP // 2):
        state.offset += BLOCKID_WRAP
    state.seen = True
    state.last_raw = raw
    return raw + state.offset


def unwrap_blockids(ids):
    """A whole recorded block-ID sequence unwrapped, as an int64 numpy array.

    The same rule as `unwrap_one` applied to every step, so a sequence read
    back from blockids.npy and the stream the coordinator unwrapped live come
    out with the same ordinals. It refuses what cannot be an ordinal: an ID
    at or below 0 raises ValueError (0 is reserved and -1 is a placeholder,
    and either one would read as a wrap), and so does a sequence that is not
    strictly increasing after the unwrap, which only corrupt or reordered
    data produces.
    """
    import numpy as np

    b = np.asarray(ids).astype(np.int64)
    bad = b <= 0
    if np.any(bad):
        raise ValueError(
            f"{int(bad.sum())} non-positive block ID(s) (0 is reserved, -1 "
            f"means the camera did not report one); first at index "
            f"{int(np.argmax(bad))}")
    if b.size < 2:
        return b
    wrap_at = np.diff(b) < -(BLOCKID_WRAP // 2)
    if wrap_at.any():
        b = b + np.concatenate(
            [[0], np.cumsum(wrap_at.astype(np.int64))]) * BLOCKID_WRAP
    if np.any(np.diff(b) <= 0):
        raise ValueError("block IDs not monotonic even after wrap-unwrap "
                         "(corrupt or reordered)")
    return b


class FrameSyncCoordinator:
    def __init__(self, n_cams: int, max_lag: int = 240, on_drop=None):
        self.n = int(n_cams)
        self.max_lag = int(max_lag)
        #: Called as on_drop(cam, frame) for each frame the coordinator
        #: discards instead of releasing, or None. RULE: every frame submit()
        #: is given leaves exactly once, in a release or through on_drop,
        #: including a late frame, a retired camera's frame and the frames a
        #: retirement clears. REASON: the router's frames are NV12 ring
        #: slots that the grab thread may reuse only once they are free, and
        #: a discarded frame nobody hands back shrinks that ring for the rest
        #: of the session.
        self._on_drop = on_drop
        self._pending = [deque() for _ in range(self.n)]  # (block_id, frame)
        self._frontier = [0] * self.n        # highest unwrapped ID seen per cam
        #: Per-camera unwrap history (see unwrap_one).
        self._unwrap_state = [UnwrapState() for _ in range(self.n)]
        self._decided_upto = 0                # highest block ID whose fate is set
        self._first_decided = None            # lowest one, once any is decided
        self._retired = [False] * self.n      # cameras dropped from the align set
        #: (cam_index, reason) for every retirement, in order. A retirement is
        #: the difference between losing one camera and losing the session, so
        #: it must reach the operator — not just stdout.
        self.retired_reasons: list = []
        # stats
        self.released = 0                     # common frames passed to encoders
        self.released_triggers = 0            # triggers released (cam-count agnostic)
        self.dropped = 0                      # frames kicked out (not common)
        self.forced = 0                       # frames dropped by max_lag forcing
        self.forced_by = [0] * self.n         # who was the laggard when forcing hit

    def active(self) -> list:
        return [c for c in range(self.n) if not self._retired[c]]

    def pending_depth(self) -> int:
        """Max frames buffered awaiting a release decision (for monitoring)."""
        return max((len(p) for p in self._pending), default=0)

    def pending_count(self, cam: int) -> int:
        """Frames of camera `cam` awaiting a release decision."""
        return len(self._pending[cam])

    @property
    def forced_triggers(self) -> int:
        """Triggers force-dropped from every camera because one lagged more
        than max_lag. `forced` counts the frames those triggers took with
        them, one per camera that had the trigger."""
        return sum(self.forced_by)

    @property
    def decided_upto(self) -> int:
        """Highest trigger whose fate (released or dropped) is final."""
        return self._decided_upto

    @property
    def decided_triggers(self) -> int:
        """Triggers from the first decided one to decided_upto, released or
        not. Counted from the first decision rather than from 1, because a
        stream's IDs start wherever its counter started (a 16-bit counter
        near its wrap, a resumed stream)."""
        if self._first_decided is None:
            return 0
        return self._decided_upto - self._first_decided + 1

    def retire(self, cam: int, reason: str = "", announce: bool = True):
        """Drop a camera from the alignment set.

        For a camera that stalled and could not be realigned. Without this its
        frozen frontier pins the watermark, every later trigger gets force-
        dropped, and the whole recording yields nothing; retiring keeps the
        remaining cameras aligned and recording.

        Returns the log line (or None if the camera was already retired).
        `announce=False` leaves printing to the caller, for a caller that holds
        a lock other threads wait on: a console write can take milliseconds.
        """
        if self._retired[cam]:
            return None
        self._retired[cam] = True
        self.retired_reasons.append((cam, reason))
        held = self._pending[cam]
        if self._on_drop is not None:
            for _bid, frame in held:
                self._on_drop(cam, frame)
        held.clear()
        if self.active():
            tail = "Remaining cameras stay aligned; this one's video ends here."
        else:
            tail = ("No camera is left in the alignment set, so every video "
                    "ends here.")
        msg = (f"[sync] cam{cam + 1} RETIRED from the alignment set: {reason}. "
               f"{tail}")
        if announce:
            print(msg, flush=True)
        return msg

    def lag_frames(self) -> list:
        """Per-camera triggers behind the leading camera; -1 for a retired one.

        Counted in triggers from the cameras' own block IDs, so unlike a
        wall-clock delivery lag it carries no host/camera oscillator drift.
        """
        act = self.active()
        if not act:
            return [-1] * self.n
        lead = max(self._frontier[c] for c in act)
        return [(lead - self._frontier[c]) if not self._retired[c] else -1
                for c in range(self.n)]

    def lag_report(self) -> str:
        """Who is behind, and who is causing the forced drops."""
        act = self.active()
        if not act:
            return "[sync] no active cameras"
        lead = max(self._frontier[c] for c in act)
        lags = " ".join(f"c{c + 1}:{lead - self._frontier[c]}" for c in act)
        blame = " ".join(f"c{c + 1}:{self.forced_by[c]}" for c in act
                         if self.forced_by[c])
        return (f"[sync] lag_behind_leader[{lags}] depth={self.pending_depth()}"
                f"/{self.max_lag} released={self.released_triggers} "
                f"forced={self.forced}" + (f" forced_by[{blame}]" if blame else ""))

    def _unwrap(self, cam: int, raw: int) -> int:
        """Camera `cam`'s next raw ID unwrapped (see unwrap_one)."""
        return unwrap_one(raw, self._unwrap_state[cam])

    def submit(self, cam: int, raw_block_id: int, frame):
        """Register a successfully-grabbed frame. Returns a list of
        (cam, block_id, frame) ready to encode now, in per-camera order.
        A frame that is refused or dropped goes to on_drop (see __init__)."""
        if self._retired[cam]:
            if self._on_drop is not None:
                self._on_drop(cam, frame)
            return []
        if raw_block_id <= 0:
            # GVSP reserves block ID 0 and no camera reports a negative one, so
            # such a value is a placeholder for "the camera did not report an
            # ordinal". Fed to the unwrap it reads as a 16-bit wrap and places
            # the camera far ahead, force-dropping every other camera. Refuse
            # it; the grab thread counts the exception as a frame error.
            if self._on_drop is not None:
                self._on_drop(cam, frame)
            raise ValueError(
                f"cam{cam + 1}: block ID {raw_block_id} is not a trigger "
                f"ordinal (0 is reserved, negative means unreported)")
        bid = unwrap_one(raw_block_id, self._unwrap_state[cam])
        if bid <= self._decided_upto:
            # Late arrival (e.g. recovered after we force-dropped its trigger);
            # its slot is already decided, so it can't be aligned — drop it.
            self.dropped += 1
            if self._on_drop is not None:
                self._on_drop(cam, frame)
            return []
        self._pending[cam].append((bid, frame))
        self._frontier[cam] = bid
        return self._advance()

    def _watermark(self, act: list) -> int:
        # All active cameras have reached at least this trigger.
        wm = min(self._frontier[c] for c in act)
        # Force progress if the fastest camera is too far ahead of a laggard.
        fastest = max(self._frontier[c] for c in act)
        if fastest - wm > self.max_lag:
            wm = fastest - self.max_lag
        return wm

    def _advance(self):
        ready = []
        act = self.active()
        if not act:
            return ready
        wm = self._watermark(act)
        while True:
            heads = [self._pending[c][0][0] for c in act if self._pending[c]]
            if not heads:
                break
            t = min(heads)
            if t > wm:
                break
            havers = [c for c in act
                      if self._pending[c] and self._pending[c][0][0] == t]
            if len(havers) == len(act):
                for c in havers:
                    _bid, frame = self._pending[c].popleft()
                    ready.append((c, t, frame))
                self.released += len(act)
                self.released_triggers += 1
            else:
                slowest = min(act, key=lambda c: self._frontier[c])
                forced = t > self._frontier[slowest]  # dropped only by max_lag
                self._discard_heads(havers)
                if forced:
                    self.forced += len(havers)
                    self.forced_by[slowest] += 1
            if self._first_decided is None:
                self._first_decided = t
            self._decided_upto = t
        return ready

    def block_rate_warnings(self, timestamps, block_ids, fps: int,
                            names=None, hints=None) -> list:
        """Run the block-ID rate check over every camera in this session.
        `hints` is passed through to `check_block_id_rate`."""
        names = names or [f"cam{i + 1}" for i in range(self.n)]
        return block_rate_warnings(
            [block_ids[i] for i in range(self.n)],
            [timestamps[i] for i in range(self.n)], fps, names, hints=hints)

    def flush(self):
        """End of recording: no more frames will arrive, so decide every
        remaining trigger. Returns the final ready list."""
        ready = []
        act = self.active()
        while act:
            heads = [self._pending[c][0][0] for c in act if self._pending[c]]
            if not heads:
                break
            t = min(heads)
            havers = [c for c in act
                      if self._pending[c] and self._pending[c][0][0] == t]
            if len(havers) == len(act):
                for c in havers:
                    _bid, frame = self._pending[c].popleft()
                    ready.append((c, t, frame))
                self.released += len(act)
                self.released_triggers += 1
            else:
                self._discard_heads(havers)
            if self._first_decided is None:
                self._first_decided = t
            self._decided_upto = t
        return ready

    def _discard_heads(self, cams) -> None:
        """Drop the head frame of each camera in `cams` (a trigger not every
        camera has), counting it and handing it to on_drop."""
        on_drop = self._on_drop
        for c in cams:
            _bid, frame = self._pending[c].popleft()
            if on_drop is not None:
                on_drop(c, frame)
        self.dropped += len(cams)


#: Fractional tolerance on the measured block-ID rate.
#:
#: Measured, not guessed: across 74 camera-sessions of real data at 30 and
#: 100 fps, including sessions that lost 24% and 43% of frames, the rate sits
#: at +220..+250 ppm of configured, every time. That is the fixed offset
#: between the trigger board's resonator and the cameras' oscillators, and it
#: is stable enough that the band is 30 ppm wide.
#:
#: 0.3% sits 12x above that worst case and still catches a camera skipping one
#: trigger in a hundred (10,000 ppm) with 3x to spare — and that is the case
#: worth catching, because nothing else in the pipeline can see it. 1% was
#: tried first and landed exactly on top of the 1-in-100 case.
BLOCK_RATE_TOL = 0.003
#: Below this many frames / seconds the span-over-duration arithmetic cannot
#: separate a skipped trigger from end-effects, so the check abstains rather
#: than cry wolf on a two-second test clip.
BLOCK_RATE_MIN_FRAMES = 300
BLOCK_RATE_MIN_SECONDS = 2.0


def check_block_id_rate(block_ids, timestamps, fps: int, name: str = "camera",
                        ceiling_hint=None, timestamp_hint=None):
    """Verify that a camera's block IDs really are trigger ordinals.

    Everything downstream takes "same block ID" to mean "same instant" —
    kick-out release, post-hoc intersection, ``stim_trace``'s
    ``t = (blockid - 1) / fps``, and the 3D solve. That identity holds only
    while the camera produces exactly one frame per trigger.

    A camera whose exposure exceeds the ceiling (``exposure + 1/limiter``
    must stay under ``1/fps``) is still busy when the next pulse arrives and
    simply *ignores* it. No frame is acquired, so no block ID is consumed, and
    from then on its block ID N is trigger N+k. Nothing else in the pipeline
    can see this: the IDs stay gapless, the frame counts stay equal across
    cameras because only common IDs are kept, and no packet or buffer counter
    moves. The videos come out looking perfect and are misaligned in time.

    The camera's device clock is a free-running hardware oscillator,
    independent of its block-ID counter, so the two together are a check: over
    any span, block IDs must advance at the trigger rate. ``timestamps`` are
    device seconds (``frametimes.npy`` row 1, or the router's per-camera list);
    only differences are used, so an origin-shifted series is fine.

    Returns None if the rate checks out or there is too little data to judge,
    otherwise a description of the discrepancy.

    The advice at the end of each message names camera settings, and those
    are vendor-specific. `ceiling_hint` is the clause that says how to keep
    exposure under the ceiling, and `timestamp_hint` the clause that says how
    to check the timestamp unit. Each defaults to the Basler wording (the
    .pfs, GevTimestampTickFrequency); a caller for another backend passes its
    own.
    """
    if ceiling_hint is None:
        ceiling_hint = (f"ExposureTime + 1/trigger_rate_limit must stay under "
                        f"1/{fps} s, so check ExposureTime in the .pfs")
    if timestamp_hint is None:
        timestamp_hint = ("check GevTimestampTickFrequency, which is 1e9 on "
                          "the Basler ace models this was built against")
    if fps <= 0 or len(block_ids) < BLOCK_RATE_MIN_FRAMES:
        return None
    if len(timestamps) < len(block_ids):
        return None
    span = float(block_ids[-1]) - float(block_ids[0])
    dur = float(timestamps[len(block_ids) - 1]) - float(timestamps[0])
    if dur < BLOCK_RATE_MIN_SECONDS or span <= 0:
        return None

    measured = span / dur
    if abs(measured - fps) <= BLOCK_RATE_TOL * fps:
        return None

    # Triggers the board fired over this span, against block IDs consumed.
    expected = fps * dur
    missed = expected - span
    drift = abs(missed) / fps

    head = (f"{name}: block IDs advanced at {measured:.2f}/s while the trigger "
            f"board runs at {fps}/s, over {dur:.1f} s of this camera's own "
            f"device clock.")
    if measured < fps:
        return (
            f"{head} That means it did NOT produce one frame per trigger — it "
            f"ignored roughly {missed:.0f} of them. Its block IDs are therefore "
            f"not trigger ordinals, so every frame it contributed is paired "
            f"with the other cameras' frames from a DIFFERENT instant, drifting "
            f"to about {drift:.1f} s by the end. Equal frame counts and gapless "
            f"block IDs do not rule this out. The usual cause is an exposure "
            f"over the ceiling: {ceiling_hint}. DO NOT use "
            f"this recording for 3D reconstruction.")
    return (
        f"{head} Block IDs cannot outrun the trigger, so this is not a capture "
        f"fault: either the recording fps ({fps}) is not what the board was "
        f"actually driving, a stream re-arm mid-recording resynchronised this "
        f"camera to the wrong ordinal, or this camera model does not report its "
        f"device timestamp in nanoseconds (grab_thread assumes it does — "
        f"{timestamp_hint}). Cross-camera alignment for {name} is unverified "
        f"until that is resolved.")


def block_rate_warnings(block_ids, timestamps, fps: int, names,
                        hints=None) -> list:
    """check_block_id_rate() over every camera, plus one cross-camera read.

    A single camera off the trigger rate is a camera fault. *Every* camera off
    it by the same amount is not — nine cameras do not independently decide to
    skip the same fraction of triggers. That pattern means the reference is
    wrong rather than the cameras: the recording fps does not match what the
    board was driving, or this camera model does not report device timestamps
    in nanoseconds. Saying so costs one comparison and stops a fleet-wide
    misconfiguration from reading as nine separate exposure problems.

    `hints` is None or a dict with `ceiling_hint` and/or `timestamp_hint`,
    passed to every per-camera check (see `check_block_id_rate`).
    """
    hints = dict(hints or {})
    msgs, rates = [], []
    for b, ts, nm in zip(block_ids, timestamps, names):
        msg = check_block_id_rate(b, ts, fps, nm, **hints)
        if msg:
            msgs.append(msg)
        if len(b) >= BLOCK_RATE_MIN_FRAMES and len(ts) >= len(b):
            dur = float(ts[len(b) - 1]) - float(ts[0])
            span = float(b[-1]) - float(b[0])
            if dur >= BLOCK_RATE_MIN_SECONDS and span > 0:
                rates.append(span / dur)

    if len(msgs) == len(rates) and len(rates) > 1:
        lo, hi = min(rates), max(rates)
        if lo > 0 and (hi - lo) <= BLOCK_RATE_TOL * lo:
            msgs.append(
                f"All {len(rates)} cameras report the same block-ID rate "
                f"({lo:.2f}/s), which is off the configured {fps}/s by the same "
                f"amount. Cameras do not fail identically, so suspect the "
                f"reference rather than the cameras: check that the profile's "
                f"frame rate matches what the trigger board is driving, and "
                f"that these cameras report device timestamps in nanoseconds. "
                f"The videos are probably aligned with each other; it is the "
                f"absolute timebase that is in question.")
    return msgs
