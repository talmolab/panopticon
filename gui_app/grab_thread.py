"""Per-camera grab thread — grabs frames, hands them to disk or the encoder.

Real-time (online) encode is DECOUPLED from the grab loop: the grab thread only
does retrieve -> copy -> queue.put -> Release (raw-to-disk weight, proven at
100 fps), while a per-camera _EncoderThread drains the queue through NVENC.
Encoding inline in the grab loop (pre-2026-06-12) blew the 10 ms/frame budget
under 6-thread contention, exhausted the pylon buffer pool, and dropped ~28%
of frames as GigE "buffer incompletely grabbed" errors.
"""
import os
import queue
import threading
import time
import numpy as np

#: _O_BINARY only exists on Windows; on POSIX the flag is meaningless
#: and referencing it is an AttributeError at import. Zero is the correct
#: no-op there, so this is all that stands between these modules and Linux.
_O_BINARY = getattr(os, "O_BINARY", 0)
from collections import deque
from pathlib import Path
from PyQt5.QtCore import QThread

# The hot loop uses the NATIVE grab-result object rather than a wrapper — see
# gui_app/backends/__init__.py for the contract it must satisfy and why it is
# deliberately not abstracted per-field. Everything else this file needs from
# the vendor SDK comes through the backend, which is what keeps the coupling
# down to that one documented contract.
from gui_app.backends import load_backend

_BACKEND = load_backend("basler")
TimeoutException = _BACKEND.TimeoutException
GRAB_STRATEGY = _BACKEND.GRAB_STRATEGY

# Frames of slack per camera between grab and encode: 200 = 2 s at 100 fps. What
# is queued is an NV12 ring slot (3.46 MB each at 1920x1200), owned by the ring
# rather than by the queue. The pylon buffer pool upstream adds ~10 s more
# (camera_manager.MAX_NUM_BUFFER = 1000).
ENCODE_QUEUE_DEPTH = 200
# How long the grab thread may block on a full queue before dropping the frame.
# Backpressure this long means the encoder is wedged (GPU stall) rather than
# merely behind — a 200-frame queue drains in ~2 s at 100 fps. On timeout the
# frame is dropped and counted in self.drops, and NO block ID or timestamp is
# recorded for it (see _put_frame's caller), so the loss shows up as a GAP in
# blockids.npy — detectable and re-alignable — instead of shifting every later
# frame onto the wrong trigger. Blocking indefinitely would only push the
# backlog upstream until the pylon pool ran dry too.
PUT_TIMEOUT_S = 2.0


class _EncoderThread(threading.Thread):
    """Drains ready-made NV12 frames from a queue into an NVENC H.264 stream.

    The grab thread copies each gray frame straight into a preallocated NV12
    ring buffer (one memcpy, ~0.08 ms into an already-faulted buffer, and numpy
    releases the GIL for it — docs/PERF_EXPERIMENTS.md E1/E1a) and queues the
    buffer; this thread then only calls Encode() (releases the GIL) and os.write
    (ditto) — so the encoder side holds the GIL for ~zero time per frame. All
    cameras' encoder threads run truly concurrently (PoC: ~1400 fps aggregate).

    If the encoder dies mid-recording, the thread switches to writing the
    remaining queued frames' Y planes (the gray data) raw to ``raw_tail.bin``
    in arrival order, so the recording stays gapless: stream.h264 holds frames
    [0..encoded) and the tail holds [encoded..end). encode_worker encodes the
    tail and concatenates it onto the stream at stop.
    """

    def __init__(self, cam_index: int, enc, h264_fd: int, spill_path: Path,
                 width: int, height: int):
        super().__init__(daemon=True, name=f"encoder{cam_index}")
        self._cam_index = cam_index
        self._enc = enc
        self._fd = h264_fd
        self._spill_path = spill_path
        self._height = height
        self.queue = queue.Queue(maxsize=ENCODE_QUEUE_DEPTH)
        self.encoded = 0
        self.spilled = 0
        self.failed = False

    def release_encoder(self):
        """Free this thread's NVENC session.

        `EndEncode()` ends the bitstream; the SESSION is released by the encoder
        object's *destructor*, so the reference has to be dropped as well.
        Concurrent sessions are capped by the driver (measured: 12 on this rig),
        and at 9 cameras the budget is tight enough that one leaked session can
        push a camera onto the raw fallback at ~129 GiB per 10 minutes.

        ONLY call this once the thread is no longer running — before start() or
        after join(). run() dereferences self._enc per frame.
        """
        enc, self._enc = self._enc, None
        if enc is None:
            return
        try:
            enc.EndEncode()
        except Exception:
            pass
        del enc

    def run(self):
        spill_fd = None
        try:
            while True:
                nv12 = self.queue.get()
                if nv12 is None:  # sentinel: flush + exit
                    if spill_fd is None:
                        try:
                            bs = self._enc.EndEncode()
                            if bs:
                                os.write(self._fd, bs)
                        except Exception as e:
                            print(f"[enc{self._cam_index}] EndEncode failed: {e}", flush=True)
                    return
                if spill_fd is not None:
                    # Guarded, unlike a normal write: this is the ALREADY
                    # degraded path, and it is the one place where a partial
                    # write corrupts everything after it. raw_tail.bin is read
                    # back as fixed-size w*h frames, so a short write shears
                    # every subsequent frame while `spilled` keeps counting them
                    # as good. Disk-full is the realistic trigger — the capacity
                    # preflight budgets ~4.6 KB/frame for H.264, and this path
                    # writes the full 2.3 MB plane.
                    try:
                        plane = nv12[:self._height]
                        n = os.write(spill_fd, plane)
                        if n != plane.nbytes:
                            raise OSError(
                                f"short write: {n} of {plane.nbytes} bytes "
                                f"(disk full?)")
                        self.spilled += 1
                    except Exception as e:
                        print(f"[enc{self._cam_index}] RAW SPILL WRITE FAILED "
                              f"after {self.spilled} frames: {e}. Stopping the "
                              f"spill rather than writing a sheared tail — "
                              f"frames from here on are LOST.", flush=True)
                        try:
                            os.close(spill_fd)
                        except Exception:
                            pass
                        spill_fd = -1        # sentinel: spill is dead, drop frames
                    continue
                if spill_fd == -1:
                    continue                 # spill failed; nothing to do but drop
                try:
                    bs = self._enc.Encode(nv12)
                    if bs:
                        os.write(self._fd, bs)
                    self.encoded += 1
                except Exception as e:
                    # Encoder died: flush what it already accepted, then write
                    # this and all later frames raw so nothing is lost.
                    print(f"[enc{self._cam_index}] encoder FAILED after "
                          f"{self.encoded} frames, spilling raw to "
                          f"{self._spill_path.name}: {e}", flush=True)
                    self.failed = True
                    try:
                        bs = self._enc.EndEncode()
                        if bs:
                            os.write(self._fd, bs)
                    except Exception:
                        pass
                    spill_fd = os.open(str(self._spill_path),
                                       os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY)
                    os.write(spill_fd, nv12[:self._height])
                    self.spilled += 1
        finally:
            # -1 is the "spill died and was already closed" sentinel.
            if spill_fd is not None and spill_fd != -1:
                try:
                    os.close(spill_fd)
                except Exception:
                    pass


class GrabThread(QThread):
    def __init__(self, cam_index: int, camera,
                 raw_path: Path = None, display_every: int = 1,
                 downsample: int = 3, realtime: bool = False,
                 width: int = 0, height: int = 0, quality: int = 21,
                 fps: int = 100, router=None):
        super().__init__()
        self._cam_index = cam_index
        # Set by CameraManager from the profile. See cpu_affinity.py for why
        # this exists at all: on a hybrid CPU the scheduler puts most of our
        # threads on E-cores and picks differently every launch.
        self._pin_cpu = False
        self._camera = camera
        self._raw_path = raw_path
        self._display_every = display_every
        self._downsample = downsample
        self._realtime = realtime
        self._width = width
        self._height = height
        self._quality = quality
        self._fps = fps
        # Real-time kick-out: when set, frames go to this shared router (which
        # gates them through the cross-camera coordinator) instead of a private
        # encoder. The router owns the encoders + the recorded metadata.
        self._router = router
        self._running = False
        self._triggers_stopped = False
        self.frame_count = 0
        self.timestamps = []
        self.block_ids = []
        self.drops = 0
        self.latest_frame = None
        self.current_fps = 0.0
        self._fps_times = deque(maxlen=10)
        self._snapshot_requested = False
        self.snapshot_frame = None
        self._keep_full = False
        self.latest_full_frame = None
        # Stall recovery state (see _rearm_stream / _resync_offset).
        self._last_ts = None          # device timestamp of the last good frame
        self._last_bid_eff = -1       # its globally-consistent block ID
        self.rearms = 0               # stream restarts this run
        #: Seconds this camera is behind real time (kick mode). ~0 is healthy;
        #: sustained growth means the grab loop is losing to the trigger.
        self.delivery_lag_s = 0.0
        self.desynced = False         # stalled and could not be realigned

    def _rearm_stream(self, attempt: int) -> bool:
        """Restart this camera's stream after a stall.

        A GigE Vision stream stall wedges the driver pipeline and never
        self-recovers — the grab loop just times out for the rest of the session
        (cam6 2026-06-10, cam1 2026-07-27). Restarting the grab is the only way
        back without restarting the GUI.
        """
        print(f"[grab{self._cam_index}] STALLED — re-arming stream "
              f"(attempt {attempt})", flush=True)
        try:
            self._camera.StopGrabbing()
            self._camera.StartGrabbing(GRAB_STRATEGY)
            return True
        except Exception as e:
            print(f"[grab{self._cam_index}] re-arm failed: "
                  f"{type(e).__name__}: {e}", flush=True)
            return False

    def _resync_offset(self, raw_bid: int, ts: float):
        """Block-ID offset that keeps trigger ordinals globally consistent.

        StartGrabbing restarts the camera's block-ID counter, so a re-armed
        camera looks like it jumped tens of thousands of triggers backwards —
        which the coordinator's 16-bit unwrap would misread as a wrap, place it
        far AHEAD, and force-drop every other camera.

        The device timestamp is a free-running hardware clock that survives the
        restart, and the triggers are hardware-timed, so the number of missed
        periods is measurable rather than guessed. Returns None when the gap
        doesn't land cleanly on a period boundary — better to declare desync
        than to publish frames under the wrong trigger ordinal.
        """
        if self._last_ts is None or self._fps <= 0:
            return None
        periods = (ts - self._last_ts) * self._fps
        k = round(periods)
        resid = abs(periods - k)
        if k < 1 or resid > 0.25:
            print(f"[grab{self._cam_index}] cannot resync: {periods:.2f} trigger "
                  f"periods elapsed, {resid:.2f} off a boundary", flush=True)
            return None
        return (self._last_bid_eff + k) - raw_bid

    def _put_frame(self, enc_thread: _EncoderThread, img) -> bool:
        """Copy the gray frame into the next NV12 ring buffer and queue it.

        The ring has queue-capacity + slack buffers, so a buffer can only be
        reused after the encoder has long since consumed it. Copying straight
        into NV12 here (instead of img.copy() + a second copy in the encoder)
        halves the memcpy work per frame."""
        buf = self._nv12_ring[self._ring_i]
        self._ring_i = (self._ring_i + 1) % len(self._nv12_ring)
        buf[:self._height, :] = img  # Y plane = gray; UV stays 128
        try:
            enc_thread.queue.put(buf, timeout=PUT_TIMEOUT_S)
            return True
        except queue.Full:
            self.drops += 1
            if self.drops in (1, 10, 100) or self.drops % 1000 == 0:
                print(f"[grab{self._cam_index}] ENCODER BACKPRESSURE: encoder "
                      f"not draining, dropped {self.drops} frames so far", flush=True)
            return False

    def _log_stream_stats(self):
        """Dump the backend's per-stream counters — distinguishes network loss
        (Failed_Buffer/Resend_Request) from pool exhaustion (Buffer_Underrun).
        Statistic_Failed_Packet_Count is deliberately not among them; see
        `backends.basler.stream_stats`."""
        stats = _BACKEND.stream_stats(self._camera)
        if stats.get("error"):
            print(f"[grab{self._cam_index}] stream stats unavailable: "
                  f"{stats['error']}", flush=True)
        else:
            print(f"[grab{self._cam_index}] stream stats: {stats}", flush=True)

    def run(self):
        # Hybrid-CPU placement. 9 grab + 9 encoder + Qt is ~19 busy threads on
        # 8 P-cores, so Windows must put most of them on E-cores and picks
        # differently every launch. A grab thread on an E-core runs a few
        # percent slow, and a few percent is unrecoverable here: the loop
        # retrieves at exactly the rate frames arrive, so it never catches up.
        # That is the shape of the rotating laggard. Failure to pin is logged
        # and ignored -- it is a performance regression, never a correctness one.
        if self._pin_cpu:
            try:
                from gui_app.cpu_affinity import (
                    pin_to_performance_core, THREAD_PRIORITY_HIGHEST)
                r = pin_to_performance_core(self._cam_index,
                                            priority=THREAD_PRIORITY_HIGHEST)
                print(f"[grab{self._cam_index}] affinity cpu={r['cpu']} "
                      f"pinned={r['pinned']} prio={r['priority']} "
                      f"(of {r['n_pcores']} P-cores)", flush=True)
            except Exception as e:
                print(f"[grab{self._cam_index}] affinity failed: {e}",
                      flush=True)
        self._running = True
        self._triggers_stopped = False
        self.frame_count = 0
        self.timestamps = []
        self.block_ids = []
        self.drops = 0
        recording = self._raw_path is not None
        fd = None              # raw.bin descriptor (raw-to-disk mode / fallback)
        h264_fd = None         # H.264 elementary-stream descriptor
        enc_thread = None      # decoupled NVENC drain thread (real-time mode)
        kick = recording and self._realtime and self._router is not None

        if kick:
            # Frames go to the shared router; this thread keeps no encoder. The
            # ring must outlast a frame's whole journey (held by the coordinator
            # up to max_lag, then queued at the encoder) before its slot reuses.
            ring_n = self._router.max_lag + ENCODE_QUEUE_DEPTH + 64
            try:
                self._nv12_ring = [
                    np.full((self._height * 3 // 2, self._width), 128, np.uint8)
                    for _ in range(ring_n)]
            except MemoryError as e:
                # Reachable, not theoretical: the ring is 2.39 GiB per camera at
                # max_lag=480 (744 buffers x 3.456 MB), so 9 cameras is ~21.5 GiB
                # of ring on top of ~19.3 GiB of pylon buffer pool (9 x 1000 x
                # 2.304 MB). Unprotected, a MemoryError here escapes run() and
                # takes the GUI down — and in kick mode a camera that never
                # publishes makes the coordinator force-drop EVERY trigger for
                # EVERY camera, so the session yields empty videos from all of
                # them. Retire so the others record aligned.
                gib = ring_n * self._width * (self._height * 3 // 2) / 2**30
                print(f"[grab{self._cam_index}] FATAL: could not allocate the "
                      f"{ring_n}-buffer NV12 ring ({gib:.2f} GiB): {e}", flush=True)
                self._router.retire(self._cam_index,
                                    "could not allocate its NV12 ring")
                return
            self._ring_i = 0
            print(f"[grab{self._cam_index}] real-time kick-out -> shared router "
                  f"(ring={ring_n})", flush=True)
        elif recording and self._realtime:
            try:
                from gui_app import nvenc
                enc = nvenc.create_h264_encoder(self._width, self._height, self._quality, fps=self._fps)
                h264_path = self._raw_path.parent / "stream.h264"
                h264_fd = os.open(str(h264_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY)
                enc_thread = _EncoderThread(
                    self._cam_index, enc, h264_fd,
                    self._raw_path.parent / "raw_tail.bin",
                    self._width, self._height)
                # NV12 ring: grab copies gray directly into these (UV preset to
                # 128); +4 slack over queue capacity so reuse can't catch up.
                self._nv12_ring = [
                    np.full((self._height * 3 // 2, self._width), 128, np.uint8)
                    for _ in range(ENCODE_QUEUE_DEPTH + 4)]
                self._ring_i = 0
                enc_thread.start()
                print(f"[grab{self._cam_index}] real-time NVENC encode (decoupled) -> {h264_path.name}", flush=True)
            except Exception as e:
                # Encoder unavailable: fall back to raw-to-disk so no data is lost.
                print(f"[grab{self._cam_index}] NVENC init failed, falling back to raw.bin: {e}", flush=True)
                enc_thread = None
                if h264_fd is not None:
                    os.close(h264_fd); h264_fd = None

        if recording and not kick and enc_thread is None:
            fd = os.open(str(self._raw_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY)

        print(f"[grab{self._cam_index}] StartGrabbing (recording={recording})", flush=True)
        try:
            self._camera.StartGrabbing(GRAB_STRATEGY)
        except Exception as e:
            # Camera offline / in a bad transport state: exit this thread cleanly
            # rather than letting the exception escape run() and abort Qt.
            print(f"[grab{self._cam_index}] StartGrabbing failed (camera offline?): {e}", flush=True)
            # THE highest-damage path in this file. In kick mode the coordinator
            # only releases trigger N once every camera has delivered N, so a
            # camera that never arms holds the frontier at 0 and force-drops
            # every trigger for EVERY camera: one dead camera silently produces
            # an empty recording from all of them. Retiring drops it from the
            # alignment set so the survivors record aligned, which is the
            # difference between losing one camera and losing the session.
            if kick and self._router is not None:
                self._router.retire(self._cam_index,
                                    "camera did not start grabbing")
            if fd is not None:
                os.close(fd)
            if enc_thread is not None:
                enc_thread.queue.put(None)
                enc_thread.join(timeout=10)
            if h264_fd is not None:
                os.close(h264_fd)
            return
        print(f"[grab{self._cam_index}] grabbing={self._camera.IsGrabbing()}", flush=True)
        frame_n = 0
        timeout_n = 0
        consec_timeouts = 0    # reset by every successful grab; stall detector
        bid_offset = 0         # added to raw block IDs after a re-arm
        awaiting_resync = False
        # ~5 s of silence at the 200 ms recording timeout. Long enough that a
        # burst of GigE resends can't trip it, short enough to lose seconds
        # rather than the rest of the session.
        STALL_TIMEOUTS = 25
        MAX_REARMS = 5         # stop thrashing if the link is genuinely dead
        # Consecutive per-frame exceptions before this camera is written off. A
        # camera raising every frame is not recoverable by retrying, and in kick
        # mode it starves every OTHER camera, so failing fast beats spinning.
        MAX_CONSEC_ERRORS = 10
        consec_errors = 0      # reset by every successful frame
        first_frame_logged = False
        # Gates the one-time "padding OK" log only. The padding CHECK itself runs
        # on every frame, below — it is one attribute read and padding could in
        # principle be turned on mid-stream.
        zc_verified = False
        t_wait = 0.0   # cumulative s blocked in RetrieveResult (per 1000 frames)
        t_proc = 0.0   # cumulative s spent processing a frame (per 1000 frames)
        # --- lag diagnostics -------------------------------------------------
        # Delivery lag = how stale a frame is when we finally retrieve it,
        # measured against the camera's own clock so host scheduling can't skew
        # it. MaxNumBuffer is 1000 (10 s at 100 fps) and GrabStrategy_OneByOne
        # hands frames over oldest-first, so a thread that stalls briefly and
        # then only just keeps up carries that backlog for the rest of the run.
        # That is exactly what the coordinator used to see as "lag": E3 measured
        # +10.7 s here at 20k frames before the GetArrayZeroCopy fix and -0.002
        # to -0.037 s after. Kept as the live regression watch — it should stay
        # flat, and growth means the grab loop is losing to the trigger again.
        clock_off = None          # (host - device) at the first frame
        deliv_lag = 0.0           # seconds of accumulated delivery delay
        t_copy = t_submit = t_disp = 0.0   # where the per-frame budget goes
        # t_wait + t_proc does NOT cover the whole iteration: Release(), the fps
        # bookkeeping and the loop edge sit outside both. On 2026-08-11 the
        # laggard camera had LOWER proc than its peers, so the ~1.5% deficit that
        # walked it into the cap had to live in that gap; t_cycle closes it. The
        # deficit was GIL wait from `result.Array` (E3), and cycle is the cleanest
        # signal that it is gone: it now reads 10.00 ms, the trigger period
        # exactly. Anything above the period is the regression.
        t_rel = t_cycle = 0.0
        t_prev = None

        try:
            while self._running and self._camera.IsGrabbing():
                try:
                    timeout = 200 if recording else 2000
                    t0 = time.perf_counter()
                    result = _BACKEND.retrieve(self._camera, timeout)
                    t1 = time.perf_counter()
                    t_wait += t1 - t0
                    if not result.GrabSucceeded():
                        print(f"[grab{self._cam_index}] grab failed: {result.ErrorCode} {result.ErrorDescription}", flush=True)
                        result.Release()
                        continue

                    # Zero-copy view over the driver buffer. `result.Array`
                    # (GetArray) ALLOCATES a fresh 2.3 MB array and memcpys into
                    # it with the GIL HELD -- measured 0.837 ms/frame of GIL-held
                    # work vs 0.157 ms here (5.33x, probe_zerocopy.py on a real
                    # camera). That is ~0.68 ms per camera per frame, and it is the
                    # term that scales with camera count: 9 cams x 0.68 = 6.1 ms of
                    # a 10 ms window. E2 showed <=300 us/thread/frame is safe even
                    # at 17 threads while ~1000 us blows the budget at 11, so this
                    # single change is what makes 9 cameras arithmetically possible.
                    #
                    # The view MUST NOT outlive this block. Every consumer below
                    # copies out of it (snapshot, NV12 ring, os.write, preview
                    # decimate, full-res HUD copy). pypylon's exit guard catches
                    # EXTRA references, but structurally cannot catch the
                    # with-target itself — that binding is inside its budget — so
                    # `del img` below is what actually enforces this. Do NOT hoist
                    # `img` out of the with. np.frombuffer(GetBuffer()) is NOT a
                    # substitute: measured 0.902 ms, no better than .Array.
                    #
                    # Row padding is checked BEFORE the with, not inside it.
                    # GetArrayZeroCopy reshapes the raw buffer to (H, W) and
                    # ignores padding, and with padding present the memoryview
                    # .cast() raises TypeError — so an inside-the-with check can
                    # never run in the very case it exists for.
                    if result.PaddingX or result.PaddingY:
                        # Not a hypothetical guard: GetArray() itself reads
                        # PaddingX to build its strides, so the code this replaced
                        # was already consulting it 100x/s. Unpadded rows are the
                        # precondition for the (H, W) reshape being the image.
                        print(f"[grab{self._cam_index}] FATAL: PaddingX="
                              f"{result.PaddingX} PaddingY={result.PaddingY} — rows "
                              f"would shear. Refusing to record.", flush=True)
                        self.desynced = True
                        if kick and self._router is not None:
                            # Drop this camera from the alignment set so the others
                            # keep recording aligned instead of the coordinator
                            # starving on a camera that will never publish.
                            self._router.retire(self._cam_index,
                                                "camera reports row padding")
                        result.Release()
                        break
                    if not zc_verified:
                        zc_verified = True
                        print(f"[grab{self._cam_index}] zero-copy view OK "
                              f"(PaddingX=0 PaddingY=0)", flush=True)

                    with result.GetArrayZeroCopy() as img:
                        if self._snapshot_requested:
                            self.snapshot_frame = img.copy()  # full-resolution still
                            self._snapshot_requested = False

                        consec_timeouts = 0
                        consec_errors = 0
                        if recording:
                            if kick:
                                # Copy gray into the next ring slot and submit to the
                                # router; it records metadata for frames it RELEASES
                                # (the common set), so this thread records none.
                                buf = self._nv12_ring[self._ring_i]
                                self._ring_i = (self._ring_i + 1) % len(self._nv12_ring)
                                tc0 = time.perf_counter()
                                buf[:self._height, :] = img
                                t_copy += time.perf_counter() - tc0
                                try:
                                    raw_bid = result.BlockID
                                except Exception:
                                    raw_bid = -1
                                dev_ts = result.TimeStamp * 1e-9
                                # How far behind the camera's own clock we are now.
                                if clock_off is None:
                                    clock_off = t1 - dev_ts
                                deliv_lag = (t1 - dev_ts) - clock_off
                                # Published live. This is the honest health
                                # signal: how far behind real time this camera
                                # is RIGHT NOW. The failure it catches is silent
                                # by construction — the buffer pool absorbs a
                                # per-frame deficit for minutes before anything
                                # errors, so by the time frames are lost the
                                # session is already spoiled.
                                self.delivery_lag_s = deliv_lag
                                if awaiting_resync:
                                    awaiting_resync = False
                                    off = self._resync_offset(raw_bid, dev_ts)
                                    if off is None:
                                        self.desynced = True
                                        self._router.retire(
                                            self._cam_index,
                                            "stream stalled and block IDs could not "
                                            "be realigned")
                                    else:
                                        bid_offset = off
                                        print(f"[grab{self._cam_index}] resynced after "
                                              f"re-arm, block-ID offset {off}", flush=True)
                                if not self.desynced:
                                    bid = raw_bid + bid_offset
                                    self._last_bid_eff = bid
                                    self._last_ts = dev_ts
                                    ts0 = time.perf_counter()
                                    self._router.submit(self._cam_index, bid,
                                                        dev_ts, buf)
                                    t_submit += time.perf_counter() - ts0
                                self.frame_count += 1  # grabbed count (for logging)
                            elif enc_thread is not None:
                                persisted = self._put_frame(enc_thread, img)
                                if persisted:
                                    self.frame_count += 1
                                    self.timestamps.append(result.TimeStamp * 1e-9)
                                    # GigE Vision block ID = trigger ordinal: makes a
                                    # dropped frame a detectable, re-alignable gap
                                    # instead of a silent cross-camera desync.
                                    try:
                                        self.block_ids.append(result.BlockID)
                                    except Exception:
                                        self.block_ids.append(-1)
                            else:
                                os.write(fd, img)
                                self.frame_count += 1
                                self.timestamps.append(result.TimeStamp * 1e-9)
                                try:
                                    self.block_ids.append(result.BlockID)
                                except Exception:
                                    self.block_ids.append(-1)

                        frame_n += 1
                        if recording and not first_frame_logged:
                            print(f"[grab{self._cam_index}] first frame received", flush=True)
                            first_frame_logged = True
                        t_proc += time.perf_counter() - t1
                        if recording and frame_n % 1000 == 0:
                            # wait >> proc and ~10 ms/frame -> loop keeps up (waits
                            # for triggers); proc-bound or wait ~0 -> loop is the
                            # bottleneck and a pool backlog is building.
                            qd = enc_thread.queue.qsize() if enc_thread else (
                                self._router.pending() if kick else 0)
                            print(f"[grab{self._cam_index}] frames={frame_n} timeouts={timeout_n} "
                                  f"avg_wait={t_wait:.2f}ms avg_proc={t_proc:.2f}ms "
                                  f"qsize={qd} | deliv_lag={deliv_lag:+.3f}s "
                                  f"copy={t_copy:.2f} submit={t_submit:.2f} "
                                  f"disp={t_disp:.2f} rel={t_rel:.2f} "
                                  f"cycle={t_cycle:.2f}ms", flush=True)
                            t_wait = t_proc = 0.0
                            t_copy = t_submit = t_disp = t_rel = t_cycle = 0.0
                        now = time.perf_counter()
                        self._fps_times.append(now)
                        if len(self._fps_times) >= 2:
                            dt = self._fps_times[-1] - self._fps_times[0]
                            if dt > 0:
                                self.current_fps = (len(self._fps_times) - 1) / dt

                        if frame_n % self._display_every == 0:
                            td0 = time.perf_counter()
                            d = self._downsample
                            self.latest_frame = img[::d, ::d].copy()
                            # Full-res copy for the coverage HUD detector (calibration
                            # only) — oblique cams (1/4) need full res to resolve the
                            # board, same as the post-hoc calibration.
                            if self._keep_full:
                                self.latest_full_frame = img.copy()
                            t_disp += time.perf_counter() - td0

                    # Python does not unbind a with-target when the block ends, so
                    # `img` would otherwise keep pointing at the driver buffer past
                    # Release() — a dangling view one careless edit away from a
                    # use-after-free. Unbind it explicitly.
                    del img

                    tr0 = time.perf_counter()
                    result.Release()
                    t_rel += time.perf_counter() - tr0
                    if t_prev is not None:
                        t_cycle += tr0 - t_prev   # start-of-iteration to start
                    t_prev = tr0

                except TimeoutException:
                    timeout_n += 1
                    consec_timeouts += 1
                    if recording and timeout_n in (1, 5, 20):
                        print(f"[grab{self._cam_index}] TIMEOUT #{timeout_n} (no frame in {timeout}ms, triggers_stopped={self._triggers_stopped})", flush=True)
                    if recording and self._triggers_stopped:
                        break
                    if not self._running:
                        break
                    # Stalled: the stream has gone quiet while triggers are still
                    # running. Restart it rather than time out for the rest of
                    # the session.
                    if (consec_timeouts >= STALL_TIMEOUTS and not self.desynced
                            and self.rearms < MAX_REARMS):
                        self.rearms += 1
                        consec_timeouts = 0
                        if self._rearm_stream(self.rearms):
                            awaiting_resync = recording and kick
                        elif recording and kick:
                            self.desynced = True
                            self._router.retire(self._cam_index,
                                                "stream stalled, re-arm failed")
                    elif (consec_timeouts >= STALL_TIMEOUTS and not self.desynced
                            and recording and kick):
                        # Re-arms exhausted. Without this the condition above just
                        # stays false forever: the thread keeps timing out in
                        # silence, its frontier frozen, and the coordinator
                        # force-drops every trigger for EVERY camera — the whole
                        # session comes back empty with nothing but timeout lines
                        # to show for it.
                        self.desynced = True
                        self._router.retire(
                            self._cam_index,
                            f"stream dead after {MAX_REARMS} re-arms")
                        # Must break. Retiring sets desynced, which makes this
                        # branch false forever after, so without the break the
                        # thread would sit timing out for the rest of the session
                        # — the exact silence this fix exists to end. (Caught by
                        # test_rearm_exhaustion_retires hanging.)
                        break
                except Exception as e:
                    print(f"[grab{self._cam_index}] exception: {type(e).__name__}: {e}", flush=True)
                    if not self._running:
                        break
                    # This handler used to print, sleep and loop forever. It never
                    # touched consec_timeouts, so the stall detector above could
                    # not arm, and `while self._running and IsGrabbing()` stayed
                    # true — so a camera raising every frame consumed 100 fps,
                    # discarded all of it, and starved the coordinator into
                    # force-dropping every trigger for every camera. Bound it.
                    consec_errors += 1
                    if consec_errors >= MAX_CONSEC_ERRORS:
                        print(f"[grab{self._cam_index}] FATAL: {consec_errors} "
                              f"consecutive frame errors, giving up on this camera",
                              flush=True)
                        if recording and kick and self._router is not None:
                            self.desynced = True
                            self._router.retire(
                                self._cam_index,
                                f"repeated frame-processing errors: "
                                f"{type(e).__name__}: {e}")
                        break
                    time.sleep(0.001)
        finally:
            print(f"[grab{self._cam_index}] exiting: frames={frame_n} "
                  f"timeouts={timeout_n} drops={self.drops} rearms={self.rearms}"
                  + (" DESYNCED" if self.desynced else ""), flush=True)
            # Catch-all. Any exit that is NOT a normal stop must retire this
            # camera, or the coordinator waits forever for a thread that is gone
            # and force-drops every trigger for every other camera. The explicit
            # retires above cover the paths we know about; this covers the ones
            # we don't — notably `IsGrabbing()` going False under us, which just
            # falls out of the while loop with no error at all.
            if (recording and kick and self._router is not None
                    and not self._triggers_stopped and not self.desynced):
                self.desynced = True
                self._router.retire(
                    self._cam_index,
                    "grab thread exited before the recording was stopped")
            if recording:
                self._log_stream_stats()  # before StopGrabbing resets counters
            if enc_thread is not None:
                # Drain: sentinel, then wait for the backlog (~1 s at full queue).
                try:
                    enc_thread.queue.put(None, timeout=30)
                except queue.Full:
                    print(f"[grab{self._cam_index}] encoder queue wedged at stop, abandoning drain", flush=True)
                enc_thread.join(timeout=60)
                if enc_thread.is_alive():
                    print(f"[grab{self._cam_index}] encoder thread did not exit (GPU stall?)", flush=True)
                else:
                    print(f"[grab{self._cam_index}] encoded={enc_thread.encoded} spilled={enc_thread.spilled}", flush=True)
                    # Hand the NVENC session back explicitly and NOW, rather than
                    # whenever this thread object happens to become garbage: the
                    # next acquisition needs the session, and this non-kick path
                    # is itself the fallback used when sessions are scarce.
                    # Deliberately inside the "thread exited" branch — the
                    # is_alive() case above leaks the session on purpose, because
                    # release_encoder() nulls self._enc while a live run() is
                    # still dereferencing it per frame (see its docstring). Same
                    # rule as SyncEncodeRouter.stop().
                    try:
                        enc_thread.release_encoder()
                    except Exception as e:
                        print(f"[grab{self._cam_index}] encoder release failed: "
                              f"{e}", flush=True)
            if h264_fd is not None:
                os.close(h264_fd)
            if fd is not None:
                os.close(fd)
            try:
                self._camera.StopGrabbing()
            except Exception:
                pass

    def signal_triggers_stopped(self):
        self._triggers_stopped = True

    def request_snapshot(self):
        """Ask the grab loop to stash the next full-resolution frame."""
        self.snapshot_frame = None
        self._snapshot_requested = True

    def set_keep_full(self, flag: bool):
        """Keep a full-resolution copy of each display-cadence frame for the
        coverage HUD detector. Off by default to avoid recording-loop overhead."""
        self._keep_full = flag
        if not flag:
            self.latest_full_frame = None

    def stop(self):
        self._running = False
