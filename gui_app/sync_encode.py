"""Router for the real-time frame kick-out path.

Owns one encoder thread per camera and a shared FrameSyncCoordinator.
Grab threads call submit() with each successfully-grabbed frame; the coordinator
releases a trigger only once every camera has it, and the router routes those
(and only those) frames to the per-camera encoders — so each stream.h264 holds
exactly the common, trigger-aligned frames. No post-hoc re-encode is needed.

submit() holds a short lock: the coordinator step is integer-only and routing is
put_nowait into queues the encoders drain faster than the 100 fps inflow, so the
lock is held for microseconds and never blocks on a full queue. Releases are
routed under the lock so each encoder receives its frames in trigger order.
Nothing prints under the lock: a console write can take milliseconds, and
every other grab thread would spend them blocked while holding a driver buffer.

A release that finds its encoder queue full waits in a per-camera backlog
instead of being dropped, and later submits move it on as the encoder drains.
That covers the one burst the coordinator produces: retiring a camera
releases every trigger the survivors held for it, up to max_lag at once,
into queues ENCODE_QUEUE_DEPTH deep.

A grab thread's frames are slots of its NV12 ring, and the router owns each
slot from submit() until the frame is dropped or encoded (attach_ring). The
backlog therefore needs no bound of its own: it can hold at most the slots
the ring has, and a camera whose encoder falls further behind finds no free
slot and loses frames of its own video, never the pixels of a queued one.

Everything the path keeps per camera (the encoder thread, the stream file,
the ring's free list, the backlog, the recorded block IDs and their
reconciliation at stop) is one `_CameraSink`. The router is a coordinator
plus one sink per camera. A capture worker process (gui_app.mp.worker) drives
the sinks of its own cameras from the cross-process ledger instead, so
blockids.npy is reconciled against what was persisted by this one code path
in both modes.
"""
import gc
import os
import queue
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

#: _O_BINARY only exists on Windows; on POSIX the flag is meaningless
#: and referencing it is an AttributeError at import. Zero is the correct
#: no-op there, so this is all that stands between these modules and Linux.
_O_BINARY = getattr(os, "O_BINARY", 0)

from gui_app import encoders
from gui_app.frame_sync import FrameSyncCoordinator
from gui_app.grab_thread import (_EncoderThread, _end_encode, write_split_point,
                                 DRAIN_SENTINEL_TIMEOUT_S, DRAIN_JOIN_TIMEOUT_S)

#: Bound on abandon() as a whole. An encoder thread that has not left
#: Encode() in this long is wedged in the driver and is leaked, not waited on.
ABANDON_TIMEOUT_S = 5.0
#: Ordinary kick-outs (triggers some camera missed, dropped from all of them)
#: above this fraction of the decided triggers add a session warning at stop.
KICKOUT_WARN_FRACTION = 0.005


def _release_loose_encoder(enc, timeout_s=None) -> None:
    """End and drop an encoder no _EncoderThread owns.

    The session is freed by the object's destructor, so the last reference
    must go here, after EndEncode() and Close() where the encoder has one.

    `timeout_s` bounds the flush; None means the encoder's own default. RULE:
    every caller passes a bound. REASON: this runs on a failure path with no
    recording to save, and a CPU encoder's EndEncode waits on a child process
    that may already be the reason the failure happened — an unbounded flush
    would hang the constructor that is trying to report the encoders
    unavailable.
    """
    try:
        _end_encode(enc, timeout_s)
    except Exception:
        pass
    close = getattr(enc, "Close", None)
    if close is not None:
        try:
            close()
        except Exception:
            pass
    del enc


def _seconds(triggers: int, fps: int) -> str:
    """`triggers` as recording time at the session's frame rate."""
    if fps <= 0:
        return f"{triggers} trigger periods"
    return f"{triggers / fps:.1f} s"


class _CameraSink:
    """One camera's half of the kick-out path, after the release decision.

    It holds the camera's encoder thread and stream file, the free list of
    its NV12 ring, the released frames waiting for encoder queue room, and
    the block IDs and timestamps of the frames that reached the encoder.
    `cam` is the index the camera is named by (camN, encN, WARNINGS.txt); in
    a capture worker that is the camera's index in the whole rig.

    `session_warnings` is the list every warning also goes to, in the order
    the warnings are found. `block_ids` and `timestamps` are the lists the
    sink appends to and reconciles in place; a caller that already holds one
    list per camera passes them so its own references stay current.

    Not thread-safe: the caller serialises route(), pump() and the stop
    steps, as SyncEncodeRouter does under its submit lock.
    """

    def __init__(self, cam: int, raw_path, height: int, session_warnings: list,
                 block_ids: list | None = None, timestamps: list | None = None):
        self.cam = int(cam)
        self.dir = Path(raw_path).parent
        self._height = height
        self._session = session_warnings
        #: stream.h264 and its descriptor, set by open_stream().
        self.h264_path = None
        self.fd = None
        #: The camera's _EncoderThread once one owns its encoder.
        self.et = None
        self.block_ids = [] if block_ids is None else block_ids
        self.timestamps = [] if timestamps is None else timestamps
        #: Released frames that never reached the encoder (should be 0);
        #: their block IDs are not recorded.
        self.dropped_full = 0
        #: Of those, the frames whose grab thread found no free ring slot
        #: (submitted without pixels); the rest were still waiting when the
        #: drain at stop ran out of time.
        self.no_slot = 0
        #: The deque of free NV12 ring slots the grab thread takes from
        #: (attach_ring), or None for a submitter that owns its buffers.
        self.free_slots = None
        #: Released frames waiting for room in the encoder queue, as
        #: (block_id, ts, buf) in trigger order. Each one owns a ring slot, so
        #: the ring bounds the backlog.
        self.backlog = deque()
        #: This camera's lines of its WARNINGS.txt.
        self.warnings: list = []
        #: Set by settle(): whether the encoder thread outlived its join, and
        #: the frames it coded.
        self.alive = False
        self.coded = 0

    # -- construction --------------------------------------------------------

    def open_stream(self) -> None:
        """Create stream.h264. Opened before the encoder is created, so the
        only step that can fail after a session exists is the thread
        constructor."""
        h264_path = self.dir / "stream.h264"
        fd = os.open(str(h264_path),
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY)
        self.h264_path = h264_path
        self.fd = fd

    # -- the ring --------------------------------------------------------------

    def attach_ring(self, slots) -> deque:
        """Take ownership of the camera's NV12 ring; return its free list.

        See SyncEncodeRouter.attach_ring for the ownership rule.
        """
        free = deque(slots)
        self.free_slots = free
        if self.et is not None:
            self.et.recycle = free.append
        return free

    def give_back(self, buf) -> None:
        """Return a dropped frame's ring slot to the free list."""
        free = self.free_slots
        if free is not None and buf is not None:
            free.append(buf)

    # -- routing ---------------------------------------------------------------

    def route(self, bid, ts, buf) -> bool:
        """Hand one released frame to the encoder, or to the backlog.

        Returns True when the frame went to the backlog, so the caller can
        keep its own count of waiting frames.
        """
        if buf is None:
            # Its grab thread had no free ring slot: the frame took part
            # in the alignment but has no pixels, so it is dropped from
            # this camera's video alone. The block ID is NOT recorded,
            # and stop() turns the count into a warning.
            self.dropped_full += 1
            self.no_slot += 1
            return False
        backlog = self.backlog
        if not backlog:
            try:
                self.et.queue.put_nowait(buf)
                self.block_ids.append(bid)
                self.timestamps.append(ts)
                return False
            except queue.Full:
                pass
        # Queue full, or earlier frames of this camera already waiting
        # (which keeps the encoder's input in trigger order).
        backlog.append((bid, ts, buf))
        return True

    def pump(self) -> int:
        """Move backlogged frames into the encoder queue, oldest first, as far
        as the queue has room now. Never blocks. Returns how many moved."""
        moved = 0
        backlog = self.backlog
        if not backlog:
            return 0
        q = self.et.queue
        while backlog:
            bid, ts, buf = backlog[0]
            try:
                q.put_nowait(buf)
            except queue.Full:
                break
            backlog.popleft()
            moved += 1
            self.block_ids.append(bid)
            self.timestamps.append(ts)
        return moved

    def drop_backlog(self) -> None:
        """Drop what still waits for queue room: the encoder is wedged, so
        these frames are counted like any other frame that never reached
        the encoder."""
        backlog = self.backlog
        if not backlog:
            return
        self.dropped_full += len(backlog)
        for _bid, _ts, buf in backlog:
            self.give_back(buf)
        backlog.clear()

    # -- stop ------------------------------------------------------------------

    def send_sentinel(self) -> None:
        try:
            self.et.queue.put(None, timeout=DRAIN_SENTINEL_TIMEOUT_S)
        except Exception as e:
            # Not silent: a wedged queue means this camera never gets its
            # sentinel, so it will not finish and everything below has to
            # treat it as unfinished.
            print(f"[sync] cam{self.cam+1}: could not deliver the drain sentinel "
                  f"({type(e).__name__}) — encoder will not finish cleanly",
                  flush=True)

    def close_if_finished(self) -> None:
        """Record whether the encoder thread outlived its join and, if it
        did not, close the stream and take the coded count.

        A thread can outlive its join, and every step after it is unsafe
        against a live one: closing its fd makes the next os.write raise
        EBADF, which run() reads as "the encoder died" and sends it spilling
        raw planes into a session being finalised; and its encoded/spilled
        counters are still moving, so reconciling against them truncates
        block_ids to a snapshot of a moving target.
        """
        self.alive = self.et.is_alive()
        if self.alive:
            print(f"[sync] cam{self.cam+1}: encoder still running after join; "
                  f"leaking its fd and encoder session rather than pulling "
                  f"them out from under a live writer", flush=True)
        else:
            try:
                os.close(self.fd)
            except Exception:
                pass
        # What the encoder actually CODED, taken here because release_encoder()
        # drops the encoder the count lives on and the reconciliation needs
        # it. A live thread's entry is unused (that camera is reported
        # unverified instead).
        self.coded = (self.et.coded_frames() if not self.alive
                      else self.et.encoded)

    def release(self) -> None:
        """Hand the encoder session back now: the next acquisition needs it,
        and the NVENC driver cap leaves no slack at a full camera count."""
        if self.alive:
            return
        try:
            self.et.release_encoder()
        except Exception as e:
            print(f"[sync] encoder release failed: {e}", flush=True)

    def failure(self) -> str | None:
        """The encoder-failure line for the manager, or None."""
        if self.et.failed:
            return (f"cam{self.cam+1}: the real-time encoder failed during the "
                    f"recording")
        return None

    def reconcile(self) -> None:
        """Make block_ids and timestamps describe only persisted frames.

        route() records a block ID as soon as queue.put_nowait() succeeds,
        but that only means the QUEUE accepted the frame, not that it was
        encoded. If an encoder thread dies, it silently accepts up to
        ENCODE_QUEUE_DEPTH more frames and encodes none of them, while their
        block IDs are already in the list. blockids.npy then claims frames
        stream.h264 does not contain, so frame i of the mp4 maps to the
        wrong trigger, and every downstream consumer (alignment.py,
        stim_trace, the 3D solve) takes block-ID identity as given, so
        nothing detects it. stim_trace's own cross-check cannot either: in
        kick mode the arrays are identical by construction, so it passes
        while the videos disagree.

        coded + spilled is the true persisted count, and both are in arrival
        order (FIFO queue, appended in the same order), so reconciling against
        it is the correct repair rather than a guess. Coded, not fed: Encode()
        accepting a frame is not the encoder emitting it, and where the
        encoder died the flush that would have emitted the last frame never
        arrives, so the fed count over-claims by one frame permanently.

        RULE: the (encoded - coded) frames the encoder never emitted are
        SPLICED OUT at index `coded`, not truncated from the end. REASON:
        stream.h264 holds fed frames [0, coded) and raw_tail.bin holds
        [encoded, encoded + spilled), and encode_worker concatenates the tail
        onto the stream, so the frames that reached neither file sit in the
        MIDDLE of the arrival order. Deleting from the end instead would leave
        a contiguous run of block IDs whose count matches the mp4 exactly
        while every frame from index `coded` onward carried a trigger
        (encoded - coded) too small: a misaligned recording that presents as
        a perfect one, which is the block-ID-axiom failure class.
        """
        et = self.et
        i = self.cam
        if self.alive:
            # Counters are still moving; any repair would be based on a
            # snapshot of a moving target. Say the mapping is unverified
            # rather than silently truncating a recording that may be fine.
            msg = (f"cam{i+1}: encoder did not finish draining, so the "
                   f"frame-to-trigger mapping is UNVERIFIED. Do not trust "
                   f"this camera's alignment without checking the mp4 frame "
                   f"count against blockids.npy.")
            self.warn(msg)
            return
        coded = self.coded
        persisted = coded + et.spilled
        claimed = len(self.block_ids)
        lost = max(0, et.encoded - coded)
        if persisted != claimed:
            msg = (f"cam{i+1}: block-ID bookkeeping claimed {claimed} frames but "
                   f"only {persisted} were persisted (coded={coded} of "
                   f"{et.encoded} fed, spilled={et.spilled}, "
                   f"encoder_failed={et.failed}); "
                   f"{lost} uncoded frame(s) spliced out at index {coded} "
                   f"and the rest truncated to {persisted}, so frame indices "
                   f"still map to the correct triggers")
            self.warn(msg)
            if lost:
                del self.block_ids[coded:coded + lost]
                del self.timestamps[coded:coded + lost]
            del self.block_ids[persisted:]
            del self.timestamps[persisted:]
        if et.spilled > 0:
            # The split point between stream.h264 and raw_tail.bin, for
            # the post-hoc encoder: if the tail cannot be merged it
            # truncates the metadata to the coded count rather than
            # over-claim. That count, not the fed one, is where the
            # elementary stream really ends.
            write_split_point(self.dir, coded, et.spilled)
            self.warn(f"cam{i+1}: the encoder failed after {coded} "
                      f"frames; {et.spilled} frames were spilled raw to "
                      f"raw_tail.bin and are merged at encode time")

    def warn_dropped(self) -> None:
        """Warn when released frames never reached the encoder.

        Such a frame is one every camera captured and every OTHER camera
        encoded, so this camera's video is shorter than the rest and frame i
        no longer means the same trigger across cameras, the property kick
        mode promises. blockids.npy is still right for this camera; the
        operator must still be told.
        """
        if self.dropped_full:
            self.warn(self.dropped_text())

    def dropped_text(self) -> str:
        """The warning for this camera's released frames that never
        reached its encoder, naming each cause."""
        i, n = self.cam, self.dropped_full
        no_slot = self.no_slot
        causes = []
        if no_slot:
            causes.append(f"{no_slot} found no free NV12 ring slot because "
                          f"its encoder ran behind the trigger rate")
        if n - no_slot:
            causes.append(f"{n - no_slot} were still waiting for the encoder "
                          f"when the drain at stop ran out of time (encoder "
                          f"wedged)")
        return (f"cam{i+1}: {n} released frames were dropped from this "
                f"camera's video ({'; '.join(causes)}). Its video is {n} "
                f"frames shorter than the others; frame indices are NOT "
                f"aligned across cameras for this recording, use blockids.npy "
                f"to align.")

    def write_warnings_file(self) -> None:
        """One WARNINGS.txt holding every warning about this camera (a second
        write_text would overwrite the first)."""
        if not self.warnings:
            return
        try:
            (self.dir / "WARNINGS.txt").write_text("\n".join(self.warnings) + "\n")
        except Exception as e:
            print(f"[sync] could not write WARNINGS.txt for cam{self.cam+1}: {e}",
                  flush=True)

    # -- abandon ---------------------------------------------------------------

    def abandon(self, deadline: float) -> None:
        """Stop the encoder thread without draining, then release its encoder
        and close the stream if it exited (see SyncEncodeRouter.abandon)."""
        et = self.et
        exited = True
        try:
            exited = et.abandon(timeout=max(0.05, deadline - time.monotonic()))
        except Exception:
            exited = False
        if exited:
            try:
                et.release_encoder(
                    timeout_s=max(0.05, deadline - time.monotonic()))
            except Exception:
                pass
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except Exception:
                    pass
                self.unlink_if_empty()
        else:
            print(f"[sync] cam{self.cam+1}: encoder thread still running at "
                  f"abandon; its fd and encoder session are leaked until "
                  f"the application exits", flush=True)
        self.fd = None

    def unlink_if_empty(self) -> None:
        """Remove stream.h264 when it holds no bytes (fd closed)."""
        p = self.h264_path
        if p is None:
            return
        try:
            if p.exists() and p.stat().st_size == 0:
                p.unlink()
        except Exception:
            pass

    # -- warnings ----------------------------------------------------------------

    def warn(self, msg: str) -> None:
        print(f"[sync] WARNING: {msg}", flush=True)
        self._session.append(msg)
        self.warnings.append(msg)


def open_sinks(cams, raw_paths, width: int, height: int, quality: int,
               fps: int, factory, session_warnings: list,
               pin_encoders: bool = False, enc_pcores: bool = False,
               block_ids=None, timestamps=None) -> tuple:
    """One _CameraSink per camera, each with its stream open and its encoder
    owned by an _EncoderThread (not started). Returns (sinks, "") or, when
    any encoder cannot be created, ([], reason) with everything this call
    created undone.

    `cams` are the indices the cameras are named by, one per raw path.
    `block_ids` and `timestamps`, when given, hold one list per camera for
    the sinks to use.
    """
    sinks = []
    #: An encoder created but not yet owned by an _EncoderThread. Every
    #: session must be reachable from the except block below: one bound
    #: only to a loop local is freed by refcount without EndEncode/Close,
    #: after the gc.collect() meant to hand the sessions back.
    pending = None
    try:
        for k, (cam, rp) in enumerate(zip(cams, raw_paths)):
            sink = _CameraSink(
                cam, rp, height, session_warnings,
                block_ids=None if block_ids is None else block_ids[k],
                timestamps=None if timestamps is None else timestamps[k])
            sinks.append(sink)
            sink.open_stream()
            notes: list = []
            pending = factory(width, height, quality, fps, notes)
            for note in notes:
                # A degraded encoder config is a property of the recording,
                # not of the console: it reaches WARNINGS.txt at stop.
                sink.warn(f"cam{cam+1}: {note}")
            et = _EncoderThread(cam, pending, sink.fd,
                                sink.dir / "raw_tail.bin", height)
            pending = None          # the thread owns it now
            # Keep encoders off the P-cores the grab threads are pinned to;
            # see cpu_affinity.pin_to_efficiency_core.
            et._pin_ecore = pin_encoders
            et._enc_pcores = enc_pcores
            sink.et = et
        return sinks, ""
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        print(f"[sync] encoder init failed, kick-out unavailable: {e}", flush=True)
        if pending is not None:
            # Bounded by the same budget abandon() uses: this path has no
            # deadline of its own, and it must not become one.
            _release_loose_encoder(pending, timeout_s=ABANDON_TIMEOUT_S)
            pending = None
        for sink in sinks:
            if sink.fd is not None:
                try:
                    os.close(sink.fd)
                except Exception:
                    pass
        # The files this call created are empty and must not survive: the
        # post-hoc encoder prefers an existing stream.h264 and would remux a
        # zero-byte file, reporting the camera as failed with its raw.bin
        # never touched.
        for sink in sinks:
            if sink.h264_path is not None:
                try:
                    sink.h264_path.unlink()
                except Exception:
                    pass
        # Release the sessions that WERE created before reporting
        # unavailable. Closing the fds is not enough: an NVENC session is
        # freed by the encoder object's destructor, so a partial failure
        # that does not `del` its encoders holds every already-created
        # session for an indeterminate time and the grab threads' fallback
        # encoders then fail against the cap, degrading at least one camera
        # to raw.bin (width x height bytes per frame) with no disk guard.
        # None of these threads were started, so releasing is safe.
        for sink in sinks:
            if sink.et is not None:
                try:
                    sink.et.release_encoder()
                except Exception:
                    pass
        gc.collect()
        return [], reason


def drain_backlogs(sinks, deadline: float) -> None:
    """Pump every sink's backlog until it is empty or `deadline`
    (time.monotonic) passes, then drop what is left.

    RULE: each pass takes what fits in every camera's queue, and the
    passes repeat under the one deadline. REASON: a wedged encoder's
    queue never makes room, so waiting on one camera at a time would
    spend the whole deadline there and leave every camera after it only
    the room its queue had at that moment.
    """
    while any(s.backlog for s in sinks):
        if sum(s.pump() for s in sinks):
            continue
        if time.monotonic() >= deadline:
            break
        time.sleep(0.001)
    for s in sinks:
        s.drop_backlog()


def finish_encoders(sinks) -> None:
    """Drain every sink's encoder and hand its session back.

    Sentinels go to every camera first, then every thread is joined, so the
    encoders drain concurrently. Who actually finished is established ONCE
    (close_if_finished) and every later step is gated on it.
    """
    for s in sinks:
        s.send_sentinel()
    for s in sinks:
        s.et.join(timeout=DRAIN_JOIN_TIMEOUT_S)
    for s in sinks:
        s.close_if_finished()
    for s in sinks:
        s.release()
    gc.collect()


def reconcile_sinks(sinks) -> None:
    """Per camera, blockids.npy against what was persisted, then the frames
    that never reached an encoder, each written into that camera's
    warnings."""
    for s in sinks:
        s.reconcile()
    for s in sinks:
        s.warn_dropped()


def session_warnings(coord, timestamps, block_ids, fps: int, max_lag: int,
                     rate_hints=None) -> list:
    """The warnings about a recording that need every camera: forced drops,
    ordinary kick-outs, the block-ID rate check and retirements.

    `coord` is the FrameSyncCoordinator that decided the recording, and
    `timestamps` / `block_ids` hold one list per camera, reconciled. Lines
    are printed as they are found, in the order they are returned.
    """
    out = []
    # Forced drops remove the same triggers from every camera, so the
    # videos stay equal length and aligned, and nothing else in the
    # session would say that part of it is missing from all of them.
    forced_triggers = coord.forced_triggers
    if forced_triggers:
        who = ", ".join(f"cam{c+1} ({n} triggers)"
                        for c, n in enumerate(coord.forced_by) if n)
        msg = (f"{forced_triggers} triggers ({_seconds(forced_triggers, fps)}"
               f" of recording) are missing from every camera's video: "
               f"they were force-dropped because a camera fell more than "
               f"kick_max_lag ({max_lag}) triggers behind the leader. "
               f"Laggard: {who}. The videos stay aligned with each other.")
        print(f"[sync] WARNING: {msg}", flush=True)
        out.append(msg)
    # Ordinary kick-outs: a trigger one camera missed is dropped from all
    # of them. A few are normal transport loss; above
    # KICKOUT_WARN_FRACTION the operator has to know how much is gone.
    decided = coord.decided_triggers
    kicked = decided - coord.released_triggers - forced_triggers
    if decided > 0 and kicked > KICKOUT_WARN_FRACTION * decided:
        msg = (f"{kicked} of {decided} triggers "
               f"({100.0 * kicked / decided:.2f}%, "
               f"{_seconds(kicked, fps)}) are missing from every camera's "
               f"video because at least one camera did not deliver them. "
               f"Each camera's own losses are in its log (failed grabs, "
               f"stream stats); the videos stay aligned with each other.")
        print(f"[sync] WARNING: {msg}", flush=True)
        out.append(msg)

    # Does each camera's block-ID counter actually keep step with the
    # trigger board? Every other loss mode leaves a gap in blockids.npy;
    # a camera that IGNORES triggers (exposure over the ceiling) does not,
    # and the release rule here matches on block ID alone. So this is the
    # one failure that survives everything above while corrupting exactly
    # the invariant the whole path rests on. Cheap to check: the device
    # clock is independent of the block-ID counter.
    for msg in coord.block_rate_warnings(timestamps, block_ids, fps,
                                         hints=rate_hints):
        print(f"[sync] WARNING: {msg}", flush=True)
        out.append(msg)

    # Retirements are session-shaping and must not be stdout-only: a
    # retired camera's video simply ends early, so the recording is no
    # longer equal-length by construction and downstream alignment has to
    # know. Fold them in beside the reconciliation warnings.
    survivors = bool(coord.active())
    for cam, reason in coord.retired_reasons:
        if survivors:
            tail = ("the other cameras continued and stay aligned with "
                    "each other.")
        else:
            tail = ("no camera was still recording when the session "
                    "ended, so every video ends at its camera's "
                    "retirement.")
        out.append(
            f"cam{cam+1} was RETIRED mid-recording ({reason}). Its video "
            f"ends at that point; {tail}")
    return out


class SyncEncodeRouter:
    def __init__(self, raw_paths, width: int, height: int, quality: int,
                 fps: int = 100, max_lag: int = 240, pin_encoders: bool = False,
                 enc_pcores: bool = False, encoder_factory=None,
                 rate_hints=None):
        self._n = len(raw_paths)
        self._w, self._h, self._q = width, height, quality
        self._fps = int(fps)   # stop() checks block-ID rate against it
        self.max_lag = max_lag  # grab threads read this to size their NV12 ring
        #: Backend wording for the block-rate warning's advice (see
        #: frame_sync.check_block_id_rate), or None for the default.
        self._rate_hints = dict(rate_hints) if rate_hints else None
        self._coord = FrameSyncCoordinator(self._n, max_lag=max_lag,
                                           on_drop=self._coord_dropped)
        self._lock = threading.Lock()
        #: One _CameraSink per camera, in camera order; empty when the
        #: encoders could not be created.
        self._sinks: list = []
        self.timestamps = [[] for _ in range(self._n)]
        self.block_ids = [[] for _ in range(self._n)]
        self.available = False
        #: Reason `available` is False, for a caller that refuses to start.
        self.unavailable_reason = ""
        #: Released frames waiting for encoder queue room, over every camera,
        #: so a submit with nothing waiting pays one integer test.
        self._n_backlog = 0
        #: Largest backlog any camera reached, for the log.
        self.backlog_peak = 0
        #: "camN: reason" for each encoder that failed during the recording,
        #: filled by stop(); the manager reports them so the NVENC session
        #: count the preflight cached is probed again.
        self.encoder_failures: list[str] = []
        self._log_every = max(int(fps), 1) * 5 * self._n   # ~5 s of submissions
        self._since_log = 0
        #: Human-readable problems found at stop(). Empty means the recording's
        #: block-ID bookkeeping matches what was actually persisted.
        self.warnings: list[str] = []

        factory = encoder_factory or encoders.get_default_factory()
        sinks, reason = open_sinks(
            range(self._n), raw_paths, width, height, quality, fps, factory,
            self.warnings, pin_encoders=pin_encoders, enc_pcores=enc_pcores,
            block_ids=self.block_ids, timestamps=self.timestamps)
        if reason:
            self.unavailable_reason = reason
        else:
            self._sinks = sinks
            self.available = True

    # -- per-camera state, read through the sinks ------------------------------

    @property
    def _encoders(self) -> list:
        return [s.et for s in self._sinks]

    @property
    def _fds(self) -> list:
        return [s.fd for s in self._sinks if s.fd is not None]

    @property
    def _h264_paths(self) -> list:
        return [s.h264_path for s in self._sinks]

    @property
    def _dirs(self) -> list:
        return [s.dir for s in self._sinks]

    @property
    def _backlog(self) -> list:
        return [s.backlog for s in self._sinks]

    @property
    def _free_slots(self) -> list:
        return [s.free_slots for s in self._sinks]

    @property
    def _cam_warnings(self) -> list:
        return [s.warnings for s in self._sinks]

    @property
    def dropped_full(self) -> int:
        """Released frames that never reached their encoder (should be 0).
        Their block IDs are not recorded."""
        return sum(s.dropped_full for s in self._sinks)

    @property
    def _dropped_full_by(self) -> list:
        return [s.dropped_full for s in self._sinks]

    @property
    def _no_slot_by(self) -> list:
        return [s.no_slot for s in self._sinks]

    def start(self):
        for s in self._sinks:
            s.et.start()

    def pending(self) -> int:
        return self._coord.pending_depth()

    def backlog_len(self, cam: int) -> int:
        """Released frames of camera `cam` waiting for encoder queue room."""
        return len(self._sinks[cam].backlog)

    def attach_ring(self, cam: int, slots) -> deque:
        """Take ownership of camera `cam`'s NV12 ring; return its free list.

        The grab thread takes a slot with `free.popleft()` for each frame
        and submits it. From then on the slot belongs to the router: it goes
        back on the list when the coordinator or the router drops the frame,
        or once the camera's encoder has finished reading it. A grab thread
        that finds the list empty submits the frame with buf=None instead
        (the router counts it in dropped_full and records no block ID).

        RULE: a slot is written only while it is on this list. REASON: a
        released frame can wait behind a slow encoder for longer than the
        ring takes to cycle, and a ring written in turn would put a later
        trigger's pixels under the block ID already recorded for that frame,
        which no count, gap or rate check can detect afterward.

        Called by the grab thread before it starts grabbing. Deque appends
        and pops are atomic, so the grab thread, the encoder thread and the
        router share the list without the submit lock.
        """
        return self._sinks[cam].attach_ring(slots)

    def _give_back(self, cam: int, buf) -> None:
        """Return a dropped frame's ring slot to its camera's free list."""
        self._sinks[cam].give_back(buf)

    def _coord_dropped(self, cam: int, payload) -> None:
        """The coordinator's on_drop: a frame it will never release."""
        self._give_back(cam, payload[1])

    @property
    def retired_reasons(self) -> list:
        """(camera index, reason) for every retirement, in order."""
        return list(self._coord.retired_reasons)

    def retire(self, cam: int, reason: str = ""):
        """Drop a camera from the alignment set (stalled and unrecoverable)."""
        with self._lock:
            msg = self._coord.retire(cam, reason, announce=False)
        if msg:
            print(msg, flush=True)

    def lag_report(self) -> str:
        return self._coord.lag_report()

    def lag_frames(self) -> list:
        """Per-camera triggers behind the leader (drift-free health signal).

        Read WITHOUT the submit lock, exactly as lag_report() is: the values
        are a snapshot for a health display. The frontier entries are monotonic
        ints and the retired flags bools, so a read torn across a concurrent
        submit is off by at most one trigger, whereas taking the lock from the
        GUI thread would make every grab thread's submit() wait on it while
        holding a driver buffer.
        """
        return self._coord.lag_frames()

    def _route(self, releases):
        sinks = self._sinks
        for cam, bid, payload in releases:
            ts, buf = payload
            sink = sinks[cam]
            if sink.route(bid, ts, buf):
                self._n_backlog += 1
                if len(sink.backlog) > self.backlog_peak:
                    self.backlog_peak = len(sink.backlog)

    def _pump(self) -> int:
        """Move backlogged frames into their encoder queues, oldest first,
        as far as the queues have room now. Never blocks: it runs under the
        submit lock. Returns how many frames moved."""
        moved = 0
        for sink in self._sinks:
            moved += sink.pump()
        self._n_backlog -= moved
        return moved

    def _drain_backlogs(self, deadline: float) -> None:
        """Pump every camera's backlog until it is empty or `deadline`
        (time.monotonic) passes, then drop what is left (drain_backlogs)."""
        drain_backlogs(self._sinks, deadline)
        self._n_backlog = 0

    def submit(self, cam: int, block_id: int, ts: float, buf: np.ndarray):
        """Hand the router one grabbed frame of camera `cam`.

        `buf` is the frame's ring slot, which the router owns from here
        (attach_ring), or None when the grab thread had no free slot: the
        trigger still counts for alignment, and the frame is dropped from
        this camera's video.
        """
        report = None
        with self._lock:
            if self._n_backlog:
                self._pump()
            releases = self._coord.submit(cam, block_id, (ts, buf))
            if releases:
                self._route(releases)
            # Periodic lag report: cross-camera skew beyond max_lag force-drops
            # frames every camera captured (up to 43% of a session), and only
            # this line names the camera that is behind. Built under the lock,
            # printed outside it.
            self._since_log += 1
            if self._since_log >= self._log_every:
                self._since_log = 0
                report = self._coord.lag_report()
        if report is not None:
            print(report, flush=True)

    def abandon(self, timeout_s: float = ABANDON_TIMEOUT_S):
        """Tear down WITHOUT draining (app quit, or a finalize that failed).

        Order matters. Each encoder thread is stopped FIRST (abort flag, queue
        emptied, sentinel, short join) and its encoder released if it exited;
        only then are the output fds closed. Closing an fd under a live writer
        makes its next os.write fail as "encoder died", which spills raw planes
        into a directory about to be deleted, and a reused fd number could
        receive H.264 bytes meant for the stream. A thread still inside
        Encode() after the join keeps its fd and session until process exit;
        the process does not always exit here (the failed-finalize path returns
        to IDLE), so a leaked session is reported rather than assumed harmless.

        `timeout_s` is a BEST-EFFORT bound on the whole call, not a bound per
        thread: the threads are wedged concurrently if at all, and a per-thread
        bound would make the caller wait n times longer with n cameras than
        with one. The remaining budget is passed down into release_encoder()
        too, because the flush inside EndEncode() waits as long as the join
        does; bounding only the join leaves the per-camera multiple in place.

        Best effort, because the deadline does not reach every wait. Once
        CpuEncoder.EndEncode's deadline expires it calls kill(), whose waits
        are fixed (proc.wait 5 s plus two reader joins of 1 s), and EndEncode
        then joins its stderr reader for another 1 s. A child wedged hard
        enough to need killing can therefore overrun the shared deadline by
        several seconds, once per such camera, so the per-camera multiple is
        reduced, not eliminated. Removing the residual needs kill()'s waits to
        share EndEncode's deadline, in cpu_encode.

        A stream.h264 left at zero bytes is removed once its fd is closed. An
        abandoned session never recorded a frame into it, and an empty stream
        left behind makes the next start into that directory ask to overwrite
        a session that never existed and makes the post-hoc encoder remux an
        empty file instead of raw.bin.
        """
        deadline = time.monotonic() + max(0.0, timeout_s)
        # Frames waiting for queue room are part of a session being thrown
        # away: nothing will encode them.
        with self._lock:
            for sink in self._sinks:
                sink.backlog.clear()
            self._n_backlog = 0
        for sink in self._sinks:
            sink.abandon(deadline)
        gc.collect()

    def _unlink_if_empty(self, i: int) -> None:
        """Remove camera i's stream.h264 when it holds no bytes (fd closed)."""
        if i < len(self._sinks):
            self._sinks[i].unlink_if_empty()

    def live_encoders(self) -> int:
        """Encoder threads still running (0 after a clean stop or abandon)."""
        return sum(1 for s in self._sinks if s.et.is_alive())

    def stop(self):
        """Flush the coordinator, drain + join encoders, return per-camera
        (count, timestamps, block_ids)."""
        with self._lock:
            self._route(self._coord.flush())
            # Everything still waiting for queue room goes in before the
            # sentinels, under one shared deadline for all cameras: the
            # encoders are draining, so a healthy one makes room within
            # moments, and a wedged one loses the rest of its backlog.
            if self._n_backlog:
                self._drain_backlogs(time.monotonic() + DRAIN_SENTINEL_TIMEOUT_S)
        finish_encoders(self._sinks)
        print(f"[sync] released={self._coord.released} dropped={self._coord.dropped} "
              f"forced={self._coord.forced} queue_full_drops={self.dropped_full}",
              flush=True)
        if self.backlog_peak:
            print(f"[sync] released frames waited for encoder queue room: "
                  f"largest backlog {self.backlog_peak} frames", flush=True)
        # An encoder that failed mid-recording (it spilled, or died with its
        # spill) is reported to the manager: the NVENC session count the
        # preflight cached may no longer be true.
        for sink in self._sinks:
            failure = sink.failure()
            if failure:
                self.encoder_failures.append(failure)
        # blockids.npy against what was actually PERSISTED, per camera
        # (_CameraSink.reconcile), then the released frames that never
        # reached their encoder.
        reconcile_sinks(self._sinks)
        self.warnings.extend(session_warnings(
            self._coord, self.timestamps, self.block_ids, self._fps,
            self.max_lag, self._rate_hints))
        for sink in self._sinks:
            sink.write_warnings_file()
        return [(len(self.block_ids[i]), self.timestamps[i], self.block_ids[i])
                for i in range(self._n)]

    def _dropped_text(self, i: int, n: int) -> str:
        """The warning for camera i's released frames that never reached its
        encoder, naming each cause (`n` is its dropped count)."""
        return self._sinks[i].dropped_text()

    def _seconds(self, triggers: int) -> str:
        """`triggers` as recording time at this session's frame rate."""
        return _seconds(triggers, self._fps)

    def _warn(self, cam: int, msg: str) -> None:
        self._sinks[cam].warn(msg)
