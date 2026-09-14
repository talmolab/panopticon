"""Router for the real-time frame kick-out path.

Owns one NVENC encoder thread per camera and a shared FrameSyncCoordinator.
Grab threads call submit() with each successfully-grabbed frame; the coordinator
releases a trigger only once every camera has it, and the router routes those
(and only those) frames to the per-camera encoders — so each stream.h264 holds
exactly the common, trigger-aligned frames. No post-hoc re-encode is needed.

submit() holds a short lock: the coordinator step is integer-only and routing is
put_nowait into queues the encoders drain faster than the 100 fps inflow, so the
lock is held for microseconds and never blocks on a full queue. Releases are
routed under the lock so each encoder receives its frames in trigger order.
"""
import gc
import os
import threading
from pathlib import Path

import numpy as np

#: _O_BINARY only exists on Windows; on POSIX the flag is meaningless
#: and referencing it is an AttributeError at import. Zero is the correct
#: no-op there, so this is all that stands between these modules and Linux.
_O_BINARY = getattr(os, "O_BINARY", 0)

from gui_app import nvenc
from gui_app.frame_sync import FrameSyncCoordinator
from gui_app.grab_thread import _EncoderThread


class SyncEncodeRouter:
    def __init__(self, raw_paths, width: int, height: int, quality: int,
                 fps: int = 100, max_lag: int = 240, pin_encoders: bool = False,
                 enc_pcores: bool = False):
        self._n = len(raw_paths)
        self._w, self._h, self._q = width, height, quality
        self._fps = int(fps)   # stop() checks block-ID rate against it
        self.max_lag = max_lag  # grab threads read this to size their NV12 ring
        self._coord = FrameSyncCoordinator(self._n, max_lag=max_lag)
        self._lock = threading.Lock()
        self._encoders = []
        self._fds = []
        self.timestamps = [[] for _ in range(self._n)]
        self.block_ids = [[] for _ in range(self._n)]
        self.available = False
        self.dropped_full = 0  # frames lost to a wedged encoder queue (should be 0)
        self._log_every = max(int(fps), 1) * 5 * self._n   # ~5 s of submissions
        self._since_log = 0

        # Kept so stop() can write a WARNINGS.txt beside the affected video.
        self._dirs = [Path(rp).parent for rp in raw_paths]
        #: Human-readable problems found at stop(). Empty means the recording's
        #: block-ID bookkeeping matches what was actually persisted.
        self.warnings: list[str] = []

        try:
            for i, rp in enumerate(raw_paths):
                enc = nvenc.create_h264_encoder(width, height, quality, fps=fps)
                h264_path = Path(rp).parent / "stream.h264"
                fd = os.open(str(h264_path),
                             os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY)
                et = _EncoderThread(i, enc, fd, Path(rp).parent / "raw_tail.bin",
                                    width, height)
                # Keep encoders off the P-cores the grab threads are pinned to;
                # see cpu_affinity.pin_to_efficiency_core.
                et._pin_ecore = pin_encoders
                et._enc_pcores = enc_pcores
                self._encoders.append(et)
                self._fds.append(fd)
            self.available = True
        except Exception as e:
            print(f"[sync] NVENC init failed, kick-out unavailable: {e}", flush=True)
            for fd in self._fds:
                try:
                    os.close(fd)
                except Exception:
                    pass
            # Release the sessions we DID get before reporting unavailable.
            # Closing the fds is not enough: an NVENC session is freed by the
            # encoder object's destructor, so a partial failure used to hold
            # every already-created session for an indeterminate time. The grab
            # threads then fall back to creating their own encoders — against
            # the same driver cap (12 here) — so at least one camera silently
            # degrades to raw.bin at ~129 GiB per 10 minutes with no disk guard.
            # None of these threads were started, so releasing is safe.
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
            self._coord.retire(cam, reason)

    def lag_report(self) -> str:
        return self._coord.lag_report()

    def _route(self, releases):
        for cam, bid, payload in releases:
            ts, buf = payload
            try:
                self._encoders[cam].queue.put_nowait(buf)
                self.block_ids[cam].append(bid)
                self.timestamps[cam].append(ts)
            except Exception:
                # Encoder queue full => encoder wedged (GPU stall). Dropping
                # here desyncs this camera; it should never happen because the
                # encoder drains faster than inflow. Count it loudly.
                self.dropped_full += 1
                if self.dropped_full in (1, 100):
                    print(f"[sync] cam{cam+1} encoder queue full, dropped a "
                          f"released frame ({self.dropped_full})", flush=True)

    def submit(self, cam: int, block_id: int, ts: float, buf: np.ndarray):
        with self._lock:
            releases = self._coord.submit(cam, block_id, (ts, buf))
            if releases:
                self._route(releases)
            # Periodic lag report. Cross-camera skew beyond max_lag is what
            # force-drops frames every camera actually captured (43% of a
            # 23-min session on 2026-07-27), and nothing in the old logs said
            # which camera was behind.
            self._since_log += 1
            if self._since_log >= self._log_every:
                self._since_log = 0
                print(self._coord.lag_report(), flush=True)

    def abandon(self):
        """Tear down WITHOUT draining (app quit mid-recording): close the output
        fds so stream.h264 unlocks and can be deleted. Encoder threads are
        daemon and die on process exit."""
        for fd in self._fds:
            try:
                os.close(fd)
            except Exception:
                pass

    def stop(self):
        """Flush the coordinator, drain + join encoders, return per-camera
        (count, timestamps, block_ids)."""
        with self._lock:
            self._route(self._coord.flush())
        for i, et in enumerate(self._encoders):
            try:
                et.queue.put(None, timeout=30)
            except Exception as e:
                # Not silent: a wedged queue means this camera never gets its
                # sentinel, so it will not finish and everything below has to
                # treat it as unfinished.
                print(f"[sync] cam{i+1}: could not deliver the drain sentinel "
                      f"({type(e).__name__}) — encoder will not finish cleanly",
                      flush=True)
        for et in self._encoders:
            et.join(timeout=60)

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
                      f"leaking its fd and NVENC session rather than pulling "
                      f"them out from under a live writer", flush=True)
                continue
            try:
                os.close(fd)
            except Exception:
                pass
        # Hand the NVENC sessions back now rather than whenever the router
        # happens to become unreachable: the next acquisition needs them and the
        # driver cap (12 here) leaves no slack at 9 cameras.
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
                print(f"[sync] WARNING: {msg}", flush=True)
                self.warnings.append(msg)
                try:
                    (self._dirs[i] / "WARNINGS.txt").write_text(msg + "\n")
                except Exception:
                    pass
                continue
            persisted = et.encoded + et.spilled
            claimed = len(self.block_ids[i])
            if persisted == claimed:
                continue
            msg = (f"cam{i+1}: block-ID bookkeeping claimed {claimed} frames but "
                   f"only {persisted} were persisted (encoded={et.encoded} "
                   f"spilled={et.spilled}, encoder_failed={et.failed}); "
                   f"truncated to {persisted} so frame indices still map to the "
                   f"correct triggers")
            print(f"[sync] WARNING: {msg}", flush=True)
            self.warnings.append(msg)
            del self.block_ids[i][persisted:]
            del self.timestamps[i][persisted:]
            try:
                (self._dirs[i] / "WARNINGS.txt").write_text(msg + "\n")
            except Exception as e:
                print(f"[sync] could not write WARNINGS.txt for cam{i+1}: {e}",
                      flush=True)

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

        return [(len(self.block_ids[i]), self.timestamps[i], self.block_ids[i])
                for i in range(self._n)]
