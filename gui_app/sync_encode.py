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
"""
import gc
import os
import threading
import time
from pathlib import Path

import numpy as np

#: _O_BINARY only exists on Windows; on POSIX the flag is meaningless
#: and referencing it is an AttributeError at import. Zero is the correct
#: no-op there, so this is all that stands between these modules and Linux.
_O_BINARY = getattr(os, "O_BINARY", 0)

from gui_app import encoders
from gui_app.frame_sync import FrameSyncCoordinator
from gui_app.grab_thread import (_EncoderThread, write_split_point,
                                 DRAIN_SENTINEL_TIMEOUT_S, DRAIN_JOIN_TIMEOUT_S)

#: Bound on abandon() as a whole. An encoder thread that has not left
#: Encode() in this long is wedged in the driver and is leaked, not waited on.
ABANDON_TIMEOUT_S = 5.0


def _release_loose_encoder(enc) -> None:
    """End and drop an encoder no _EncoderThread owns.

    The session is freed by the object's destructor, so the last reference
    must go here, after EndEncode() and Close() where the encoder has one.
    """
    try:
        enc.EndEncode()
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
                 enc_pcores: bool = False, encoder_factory=None):
        self._n = len(raw_paths)
        self._w, self._h, self._q = width, height, quality
        self._fps = int(fps)   # stop() checks block-ID rate against it
        self.max_lag = max_lag  # grab threads read this to size their NV12 ring
        self._coord = FrameSyncCoordinator(self._n, max_lag=max_lag)
        self._lock = threading.Lock()
        self._encoders = []
        self._fds = []
        self._h264_paths = []
        self.timestamps = [[] for _ in range(self._n)]
        self.block_ids = [[] for _ in range(self._n)]
        self.available = False
        #: Reason `available` is False, for a caller that refuses to start.
        self.unavailable_reason = ""
        self.dropped_full = 0  # frames lost to a wedged encoder queue (should be 0)
        self._dropped_full_by = [0] * self._n
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
                _release_loose_encoder(pending)
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
            try:
                self._encoders[cam].queue.put_nowait(buf)
                self.block_ids[cam].append(bid)
                self.timestamps[cam].append(ts)
            except Exception:
                # Encoder queue full => encoder wedged (GPU stall). Dropping
                # here desyncs this camera's video length from the others; it
                # should never happen because the encoder drains faster than
                # inflow. The block ID is NOT recorded (the frame was not
                # persisted), and stop() turns the count into a warning.
                self.dropped_full += 1
                self._dropped_full_by[cam] += 1

    def submit(self, cam: int, block_id: int, ts: float, buf: np.ndarray):
        report = None
        with self._lock:
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

        `timeout_s` bounds the WHOLE call, not each thread: the threads are
        wedged concurrently if at all, and a per-thread bound would make the
        caller wait n times longer at nine cameras than at one.

        A stream.h264 left at zero bytes is removed once its fd is closed. An
        abandoned session never recorded a frame into it, and an empty stream
        left behind makes the next start into that directory ask to overwrite
        a session that never existed and makes the post-hoc encoder remux an
        empty file instead of raw.bin.
        """
        deadline = time.monotonic() + max(0.0, timeout_s)
        for i, et in enumerate(self._encoders):
            exited = True
            try:
                exited = et.abandon(timeout=max(0.05, deadline - time.monotonic()))
            except Exception:
                exited = False
            if exited:
                try:
                    et.release_encoder()
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
        # encoded + spilled is the true persisted count, and both are in arrival
        # order (FIFO queue, appended in the same order), so truncating to it is
        # the correct repair rather than a guess.
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
            persisted = et.encoded + et.spilled
            claimed = len(self.block_ids[i])
            if persisted != claimed:
                msg = (f"cam{i+1}: block-ID bookkeeping claimed {claimed} frames but "
                       f"only {persisted} were persisted (encoded={et.encoded} "
                       f"spilled={et.spilled}, encoder_failed={et.failed}); "
                       f"truncated to {persisted} so frame indices still map to the "
                       f"correct triggers")
                self._warn(i, msg)
                del self.block_ids[i][persisted:]
                del self.timestamps[i][persisted:]
            if et.spilled > 0:
                # The split point between stream.h264 and raw_tail.bin, for
                # the post-hoc encoder: if the tail cannot be merged it
                # truncates the metadata to `encoded` rather than over-claim.
                write_split_point(self._dirs[i], et.encoded, et.spilled)
                self._warn(i, f"cam{i+1}: the encoder failed after {et.encoded} "
                              f"frames; {et.spilled} frames were spilled raw to "
                              f"raw_tail.bin and are merged at encode time")

        # A frame dropped because an encoder queue was full is a frame every
        # camera captured and every OTHER camera encoded, so this camera's
        # video is shorter than the rest and frame i no longer means the same
        # trigger across cameras — the property kick mode promises. blockids.npy
        # is still right for this camera; the operator must still be told.
        for i, n in enumerate(self._dropped_full_by):
            if n:
                self._warn(i, f"cam{i+1}: {n} released frames were dropped because "
                              f"its encoder queue was full (encoder wedged). Its "
                              f"video is {n} frames shorter than the others; frame "
                              f"indices are NOT aligned across cameras for this "
                              f"recording, use blockids.npy to align.")

        # Does each camera's block-ID counter actually keep step with the
        # trigger board? Every other loss mode leaves a gap in blockids.npy;
        # a camera that IGNORES triggers (exposure over the ceiling) does not,
        # and the release rule here matches on block ID alone. So this is the
        # one failure that survives everything above while corrupting exactly
        # the invariant the whole path rests on. Cheap to check: the device
        # clock is independent of the block-ID counter.
        for msg in self._coord.block_rate_warnings(self.timestamps,
                                                   self.block_ids, self._fps):
            print(f"[sync] WARNING: {msg}", flush=True)
            self.warnings.append(msg)

        # Retirements are session-shaping and must not be stdout-only: a
        # retired camera's video simply ends early, so the recording is no
        # longer equal-length by construction and downstream alignment has to
        # know. Fold them in beside the reconciliation warnings.
        for cam, reason in self._coord.retired_reasons:
            self.warnings.append(
                f"cam{cam+1} was RETIRED mid-recording ({reason}). Its video "
                f"ends at that point; the other cameras continued and stay "
                f"aligned with each other.")

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

    def _warn(self, cam: int, msg: str) -> None:
        print(f"[sync] WARNING: {msg}", flush=True)
        self.warnings.append(msg)
        self._cam_warnings[cam].append(msg)
