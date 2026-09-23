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
        self._encoders = []
        self._fds = []
        self._h264_paths = []
        self.timestamps = [[] for _ in range(self._n)]
        self.block_ids = [[] for _ in range(self._n)]
        self.available = False
        #: Reason `available` is False, for a caller that refuses to start.
        self.unavailable_reason = ""
        #: Released frames that never reached their encoder (should be 0),
        #: in total and per camera. Their block IDs are not recorded.
        self.dropped_full = 0
        self._dropped_full_by = [0] * self._n
        #: Of those, per camera, the frames whose grab thread found no free
        #: ring slot (submitted without pixels); the rest were still waiting
        #: when the drain at stop ran out of time.
        self._no_slot_by = [0] * self._n
        #: Per camera, the deque of free NV12 ring slots its grab thread
        #: takes from (attach_ring), or None for a submitter that owns its
        #: buffers.
        self._free_slots = [None] * self._n
        #: Released frames waiting for room in their encoder queue, per
        #: camera, as (block_id, ts, buf) in trigger order; and their total,
        #: so a submit with nothing waiting pays one integer test. Each one
        #: owns a ring slot, so the ring bounds the backlog.
        self._backlog = [deque() for _ in range(self._n)]
        self._n_backlog = 0
        #: Largest backlog any camera reached, for the log.
        self.backlog_peak = 0
        #: "camN: reason" for each encoder that failed during the recording,
        #: filled by stop(); the manager reports them so the NVENC session
        #: count the preflight cached is probed again.
        self.encoder_failures: list[str] = []
        self._log_every = max(int(fps), 1) * 5 * self._n   # ~5 s of submissions
        self._since_log = 0

        # Kept so stop() can write a WARNINGS.txt beside the affected video.
        self._dirs = [Path(rp).parent for rp in raw_paths]
        #: Human-readable problems found at stop(). Empty means the recording's
        #: block-ID bookkeeping matches what was actually persisted.
        self.warnings: list[str] = []
        #: Per-camera warnings destined for that camera's WARNINGS.txt.
        self._cam_warnings: list[list[str]] = [[] for _ in range(self._n)]

        factory = encoder_factory or encoders.get_default_factory()
        #: An encoder created but not yet owned by an _EncoderThread. Every
        #: session must be reachable from the except block below: one bound
        #: only to a loop local is freed by refcount without EndEncode/Close,
        #: after the gc.collect() meant to hand the sessions back.
        pending = None
        try:
            for i, rp in enumerate(raw_paths):
                # The fd is opened BEFORE the encoder is created, so the only
                # step that can fail after a session exists is the thread
                # constructor, and `pending` covers that one.
                h264_path = Path(rp).parent / "stream.h264"
                fd = os.open(str(h264_path),
                             os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY)
                self._h264_paths.append(h264_path)
                self._fds.append(fd)
                notes: list = []
                pending = factory(width, height, quality, fps, notes)
                for note in notes:
                    # A degraded encoder config is a property of the recording,
                    # not of the console: it reaches WARNINGS.txt at stop.
                    self._warn(i, f"cam{i+1}: {note}")
                et = _EncoderThread(i, pending, fd,
                                    Path(rp).parent / "raw_tail.bin", height)
                pending = None          # the thread owns it now
                # Keep encoders off the P-cores the grab threads are pinned to;
                # see cpu_affinity.pin_to_efficiency_core.
                et._pin_ecore = pin_encoders
                et._enc_pcores = enc_pcores
                self._encoders.append(et)
            self.available = True
        except Exception as e:
            self.unavailable_reason = f"{type(e).__name__}: {e}"
            print(f"[sync] encoder init failed, kick-out unavailable: {e}", flush=True)
            if pending is not None:
                # Bounded by the same budget abandon() uses: this path has no
                # deadline of its own, and it must not become one.
                _release_loose_encoder(pending, timeout_s=ABANDON_TIMEOUT_S)
                pending = None
            for fd in self._fds:
                try:
                    os.close(fd)
                except Exception:
                    pass
            # The files this constructor created are empty and must not
            # survive: the post-hoc encoder prefers an existing stream.h264 and
            # would remux a zero-byte file, reporting the camera as failed with
            # its raw.bin never touched.
            for p in self._h264_paths:
                try:
                    p.unlink()
                except Exception:
                    pass
            self._fds = []
            self._h264_paths = []
            # Release the sessions that WERE created before reporting
            # unavailable. Closing the fds is not enough: an NVENC session is
            # freed by the encoder object's destructor, so a partial failure
            # that does not `del` its encoders holds every already-created
            # session for an indeterminate time and the grab threads' fallback
            # encoders then fail against the cap, degrading at least one camera
            # to raw.bin at ~129 GiB per 10 minutes with no disk guard. None of
            # these threads were started, so releasing is safe.
            for et in self._encoders:
                try:
                    et.release_encoder()
                except Exception:
                    pass
            self._encoders = []
            gc.collect()

    def start(self):
        for et in self._encoders:
            et.start()

    def pending(self) -> int:
        return self._coord.pending_depth()

    def backlog_len(self, cam: int) -> int:
        """Released frames of camera `cam` waiting for encoder queue room."""
        return len(self._backlog[cam])

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
        free = deque(slots)
        self._free_slots[cam] = free
        if cam < len(self._encoders):
            self._encoders[cam].recycle = free.append
        return free

    def _give_back(self, cam: int, buf) -> None:
        """Return a dropped frame's ring slot to its camera's free list."""
        free = self._free_slots[cam]
        if free is not None and buf is not None:
            free.append(buf)

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
        for cam, bid, payload in releases:
            ts, buf = payload
            if buf is None:
                # Its grab thread had no free ring slot: the frame took part
                # in the alignment but has no pixels, so it is dropped from
                # this camera's video alone. The block ID is NOT recorded,
                # and stop() turns the count into a warning.
                self.dropped_full += 1
                self._dropped_full_by[cam] += 1
                self._no_slot_by[cam] += 1
                continue
            backlog = self._backlog[cam]
            if not backlog:
                try:
                    self._encoders[cam].queue.put_nowait(buf)
                    self.block_ids[cam].append(bid)
                    self.timestamps[cam].append(ts)
                    continue
                except queue.Full:
                    pass
            # Queue full, or earlier frames of this camera already waiting
            # (which keeps the encoder's input in trigger order).
            backlog.append((bid, ts, buf))
            self._n_backlog += 1
            if len(backlog) > self.backlog_peak:
                self.backlog_peak = len(backlog)

    def _pump(self, deadline=None) -> None:
        """Move backlogged frames into their encoder queues, oldest first.

        Without a deadline it takes only what fits now (the submit path,
        under the lock, never blocks). With one it waits for room until the
        deadline and drops what is left then: stop() uses that after the
        last submit, when the encoders are draining toward their sentinels.
        """
        for cam, backlog in enumerate(self._backlog):
            q = self._encoders[cam].queue
            while backlog:
                bid, ts, buf = backlog[0]
                try:
                    if deadline is None:
                        q.put_nowait(buf)
                    else:
                        q.put(buf, timeout=max(0.0, deadline - time.monotonic()))
                except queue.Full:
                    if deadline is None:
                        break
                    # Out of time: the encoder is wedged, so the rest of this
                    # camera's backlog is lost, and counted like any other
                    # frame that never reached the encoder.
                    self.dropped_full += len(backlog)
                    self._dropped_full_by[cam] += len(backlog)
                    self._n_backlog -= len(backlog)
                    for _bid, _ts, dropped in backlog:
                        self._give_back(cam, dropped)
                    backlog.clear()
                    break
                backlog.popleft()
                self._n_backlog -= 1
                self.block_ids[cam].append(bid)
                self.timestamps[cam].append(ts)

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
        bound would make the caller wait n times longer at nine cameras than at
        one. The remaining budget is passed down into release_encoder() too,
        because the flush inside EndEncode() waits as long as the join does —
        bounding only the join leaves the per-camera multiple in place.

        Best effort, because the deadline does not reach every wait. Once
        CpuEncoder.EndEncode's deadline expires it calls kill(), whose waits
        are fixed (proc.wait 5 s plus two reader joins of 1 s), and EndEncode
        then joins its stderr reader for another 1 s. A child wedged hard
        enough to need killing can therefore overrun the shared deadline by
        several seconds, once per such camera — so at nine cameras the
        per-camera multiple is reduced, not eliminated. Removing the residual
        needs kill()'s waits to share EndEncode's deadline, in cpu_encode.

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
            for backlog in self._backlog:
                backlog.clear()
            self._n_backlog = 0
        for i, et in enumerate(self._encoders):
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
                if i < len(self._fds):
                    try:
                        os.close(self._fds[i])
                    except Exception:
                        pass
                    self._unlink_if_empty(i)
            else:
                print(f"[sync] cam{i+1}: encoder thread still running at "
                      f"abandon; its fd and encoder session are leaked until "
                      f"the application exits", flush=True)
        self._fds = []
        gc.collect()

    def _unlink_if_empty(self, i: int) -> None:
        """Remove camera i's stream.h264 when it holds no bytes (fd closed)."""
        if i >= len(self._h264_paths):
            return
        p = self._h264_paths[i]
        try:
            if p.exists() and p.stat().st_size == 0:
                p.unlink()
        except Exception:
            pass

    def live_encoders(self) -> int:
        """Encoder threads still running (0 after a clean stop or abandon)."""
        return sum(1 for et in self._encoders if et.is_alive())

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
                self._pump(deadline=time.monotonic() + DRAIN_SENTINEL_TIMEOUT_S)
        for i, et in enumerate(self._encoders):
            try:
                et.queue.put(None, timeout=DRAIN_SENTINEL_TIMEOUT_S)
            except Exception as e:
                # Not silent: a wedged queue means this camera never gets its
                # sentinel, so it will not finish and everything below has to
                # treat it as unfinished.
                print(f"[sync] cam{i+1}: could not deliver the drain sentinel "
                      f"({type(e).__name__}) — encoder will not finish cleanly",
                      flush=True)
        for et in self._encoders:
            et.join(timeout=DRAIN_JOIN_TIMEOUT_S)

        # Establish ONCE who actually finished, then gate everything on it.
        # A thread can outlive its join, and every step below is unsafe against
        # a live one: closing its fd makes the next os.write raise EBADF, which
        # run() reads as "the encoder died" and sends it spilling raw planes into
        # a session being finalised; and its encoded/spilled counters are still
        # moving, so reconciling against them truncates block_ids to a snapshot
        # of a moving target — corrupting a recording that was still finishing
        # correctly.
        alive = [et.is_alive() for et in self._encoders]
        for i, (fd, is_alive) in enumerate(zip(self._fds, alive)):
            if is_alive:
                print(f"[sync] cam{i+1}: encoder still running after join; "
                      f"leaking its fd and encoder session rather than pulling "
                      f"them out from under a live writer", flush=True)
                continue
            try:
                os.close(fd)
            except Exception:
                pass
        # What each encoder actually CODED, taken here because release_encoder()
        # below drops the encoder the count lives on and the reconciliation
        # further down needs it. A live thread's entry is unused (that camera
        # is reported unverified instead).
        coded = [et.coded_frames() if not is_alive else et.encoded
                 for et, is_alive in zip(self._encoders, alive)]
        # Hand the encoder sessions back now rather than whenever the router
        # happens to become unreachable: the next acquisition needs them and the
        # NVENC driver cap leaves no slack at 9 cameras.
        for et, is_alive in zip(self._encoders, alive):
            if is_alive:
                continue
            try:
                et.release_encoder()
            except Exception as e:
                print(f"[sync] encoder release failed: {e}", flush=True)
        gc.collect()
        print(f"[sync] released={self._coord.released} dropped={self._coord.dropped} "
              f"forced={self._coord.forced} queue_full_drops={self.dropped_full}",
              flush=True)
        if self.backlog_peak:
            print(f"[sync] released frames waited for encoder queue room: "
                  f"largest backlog {self.backlog_peak} frames", flush=True)
        # An encoder that failed mid-recording (it spilled, or died with its
        # spill) is reported to the manager: the NVENC session count the
        # preflight cached may no longer be true.
        for i, et in enumerate(self._encoders):
            if et.failed:
                self.encoder_failures.append(
                    f"cam{i+1}: the real-time encoder failed during the "
                    f"recording")

        # Reconcile bookkeeping against what was actually PERSISTED.
        #
        # _route() records a block ID as soon as queue.put_nowait() succeeds —
        # but that only means the QUEUE accepted the frame, not that it was
        # encoded. If an encoder thread dies, it silently accepts up to
        # ENCODE_QUEUE_DEPTH more frames and encodes none of them, while their
        # block IDs are already in the list. blockids.npy then claims frames
        # stream.h264 does not contain, so **frame i of the mp4 maps to the
        # wrong trigger** — and every downstream consumer (alignment.py,
        # stim_trace, the 3D solve) takes block-ID identity as given, so nothing
        # detects it. stim_trace's own cross-check cannot either: in kick mode
        # the two arrays are identical by construction, so it passes while the
        # videos disagree.
        #
        # coded + spilled is the true persisted count, and both are in arrival
        # order (FIFO queue, appended in the same order), so reconciling against
        # it is the correct repair rather than a guess. Coded, not fed: Encode()
        # accepting a frame is not the encoder emitting it, and where the
        # encoder died the flush that would have emitted the last frame never
        # arrives, so the fed count over-claims by one frame permanently.
        #
        # RULE: the (encoded - coded) frames the encoder never emitted are
        # SPLICED OUT at index `coded`, not truncated from the end. REASON:
        # stream.h264 holds fed frames [0, coded) and raw_tail.bin holds
        # [encoded, encoded + spilled), and encode_worker concatenates the tail
        # onto the stream — so the frames that reached neither file sit in the
        # MIDDLE of the arrival order. Deleting from the end instead would leave
        # a contiguous run of block IDs whose count matches the mp4 exactly
        # while every frame from index `coded` onward carried a trigger
        # (encoded - coded) too small: a misaligned recording that presents as a
        # perfect one, which is the block-ID-axiom failure class.
        for i, et in enumerate(self._encoders):
            if alive[i]:
                # Counters are still moving; any repair would be based on a
                # snapshot of a moving target. Say the mapping is unverified
                # rather than silently truncating a recording that may be fine.
                msg = (f"cam{i+1}: encoder did not finish draining, so the "
                       f"frame-to-trigger mapping is UNVERIFIED. Do not trust "
                       f"this camera's alignment without checking the mp4 frame "
                       f"count against blockids.npy.")
                self._warn(i, msg)
                continue
            persisted = coded[i] + et.spilled
            claimed = len(self.block_ids[i])
            lost = max(0, et.encoded - coded[i])
            if persisted != claimed:
                msg = (f"cam{i+1}: block-ID bookkeeping claimed {claimed} frames but "
                       f"only {persisted} were persisted (coded={coded[i]} of "
                       f"{et.encoded} fed, spilled={et.spilled}, "
                       f"encoder_failed={et.failed}); "
                       f"{lost} uncoded frame(s) spliced out at index {coded[i]} "
                       f"and the rest truncated to {persisted}, so frame indices "
                       f"still map to the correct triggers")
                self._warn(i, msg)
                if lost:
                    del self.block_ids[i][coded[i]:coded[i] + lost]
                    del self.timestamps[i][coded[i]:coded[i] + lost]
                del self.block_ids[i][persisted:]
                del self.timestamps[i][persisted:]
            if et.spilled > 0:
                # The split point between stream.h264 and raw_tail.bin, for
                # the post-hoc encoder: if the tail cannot be merged it
                # truncates the metadata to the coded count rather than
                # over-claim. That count, not the fed one, is where the
                # elementary stream really ends.
                write_split_point(self._dirs[i], coded[i], et.spilled)
                self._warn(i, f"cam{i+1}: the encoder failed after {coded[i]} "
                              f"frames; {et.spilled} frames were spilled raw to "
                              f"raw_tail.bin and are merged at encode time")

        # A released frame that never reached its encoder is a frame every
        # camera captured and every OTHER camera encoded, so this camera's
        # video is shorter than the rest and frame i no longer means the same
        # trigger across cameras — the property kick mode promises. blockids.npy
        # is still right for this camera; the operator must still be told.
        for i, n in enumerate(self._dropped_full_by):
            if n:
                self._warn(i, self._dropped_text(i, n))

        # Forced drops remove the same triggers from every camera, so the
        # videos stay equal length and aligned, and nothing else in the
        # session would say that part of it is missing from all of them.
        coord = self._coord
        forced_triggers = coord.forced_triggers
        if forced_triggers:
            who = ", ".join(f"cam{c+1} ({n} triggers)"
                            for c, n in enumerate(coord.forced_by) if n)
            msg = (f"{forced_triggers} triggers ({self._seconds(forced_triggers)}"
                   f" of recording) are missing from every camera's video: "
                   f"they were force-dropped because a camera fell more than "
                   f"kick_max_lag ({self.max_lag}) triggers behind the leader. "
                   f"Laggard: {who}. The videos stay aligned with each other.")
            print(f"[sync] WARNING: {msg}", flush=True)
            self.warnings.append(msg)
        # Ordinary kick-outs: a trigger one camera missed is dropped from all
        # of them. A few are normal transport loss; above
        # KICKOUT_WARN_FRACTION the operator has to know how much is gone.
        decided = coord.decided_triggers
        kicked = decided - coord.released_triggers - forced_triggers
        if decided > 0 and kicked > KICKOUT_WARN_FRACTION * decided:
            msg = (f"{kicked} of {decided} triggers "
                   f"({100.0 * kicked / decided:.2f}%, "
                   f"{self._seconds(kicked)}) are missing from every camera's "
                   f"video because at least one camera did not deliver them. "
                   f"Each camera's own losses are in its log (failed grabs, "
                   f"stream stats); the videos stay aligned with each other.")
            print(f"[sync] WARNING: {msg}", flush=True)
            self.warnings.append(msg)

        # Does each camera's block-ID counter actually keep step with the
        # trigger board? Every other loss mode leaves a gap in blockids.npy;
        # a camera that IGNORES triggers (exposure over the ceiling) does not,
        # and the release rule here matches on block ID alone. So this is the
        # one failure that survives everything above while corrupting exactly
        # the invariant the whole path rests on. Cheap to check: the device
        # clock is independent of the block-ID counter.
        for msg in self._coord.block_rate_warnings(self.timestamps,
                                                   self.block_ids, self._fps,
                                                   hints=self._rate_hints):
            print(f"[sync] WARNING: {msg}", flush=True)
            self.warnings.append(msg)

        # Retirements are session-shaping and must not be stdout-only: a
        # retired camera's video simply ends early, so the recording is no
        # longer equal-length by construction and downstream alignment has to
        # know. Fold them in beside the reconciliation warnings.
        survivors = bool(self._coord.active())
        for cam, reason in self._coord.retired_reasons:
            if survivors:
                tail = ("the other cameras continued and stay aligned with "
                        "each other.")
            else:
                tail = ("no camera was still recording when the session "
                        "ended, so every video ends at its camera's "
                        "retirement.")
            self.warnings.append(
                f"cam{cam+1} was RETIRED mid-recording ({reason}). Its video "
                f"ends at that point; {tail}")

        # One WARNINGS.txt per affected camera, holding every warning about it
        # (a second write_text would overwrite the first).
        for i, msgs in enumerate(self._cam_warnings):
            if not msgs:
                continue
            try:
                (self._dirs[i] / "WARNINGS.txt").write_text("\n".join(msgs) + "\n")
            except Exception as e:
                print(f"[sync] could not write WARNINGS.txt for cam{i+1}: {e}",
                      flush=True)

        return [(len(self.block_ids[i]), self.timestamps[i], self.block_ids[i])
                for i in range(self._n)]

    def _dropped_text(self, i: int, n: int) -> str:
        """The warning for camera i's `n` released frames that never
        reached its encoder, naming each cause."""
        no_slot = self._no_slot_by[i]
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

    def _seconds(self, triggers: int) -> str:
        """`triggers` as recording time at this session's frame rate."""
        if self._fps <= 0:
            return f"{triggers} trigger periods"
        return f"{triggers / self._fps:.1f} s"

    def _warn(self, cam: int, msg: str) -> None:
        print(f"[sync] WARNING: {msg}", flush=True)
        self.warnings.append(msg)
        self._cam_warnings[cam].append(msg)
