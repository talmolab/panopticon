"""Per-camera grab thread — grabs frames, hands them to disk or the encoder.

Real-time (online) encode is DECOUPLED from the grab loop: the grab thread only
does retrieve -> copy -> queue.put -> Release (raw-to-disk weight, proven at
100 fps), while a per-camera _EncoderThread drains the queue through the
encoder. Encoding inline in the grab loop blows the 10 ms/frame budget under
contention, exhausts the pylon buffer pool and drops ~28% of frames as GigE
"buffer incompletely grabbed" errors, so no work goes back on the loop's
critical path.
"""
import gc
import inspect
import json
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

from gui_app import encoders
from gui_app.frame_sync import BLOCK_RATE_MIN_FRAMES

# The hot loop uses the NATIVE grab-result object rather than a wrapper — see
# gui_app/backends/__init__.py for the contract it must satisfy and why it is
# deliberately not abstracted per-field. Everything else this file needs from
# the vendor SDK comes through the backend INSTANCE handed to GrabThread by
# CameraManager, so this module imports no SDK: one backend object serves the
# whole application and a second backend needs no edit here.

# Frames of slack per camera between grab and encode: 200 = 2 s at 100 fps. What
# is queued is an NV12 ring slot (3.46 MB each at 1920x1200), owned by the ring
# rather than by the queue. The pylon buffer pool upstream adds more slack; its
# depth comes from the profile (max_num_buffer) and must be >= kick_max_lag, or
# the coordinator can hold frames the driver has already had to overwrite.
ENCODE_QUEUE_DEPTH = 200
#: Ring slots beyond the queue depth. In kick mode a frame is held by the
#: coordinator (up to max_lag) and then queued (up to ENCODE_QUEUE_DEPTH), so the
#: slack only has to cover the frame in Encode() plus the one being written;
#: 64 leaves room for the router's own bookkeeping to lag a little.
KICK_RING_SLACK = 64
#: Decoupled mode holds at most ENCODE_QUEUE_DEPTH queued + 1 in Encode() + 1
#: being written, so 4 spare slots suffice as long as a slot is only consumed
#: by a successful put (see _put_frame).
DECOUPLED_RING_SLACK = 4
# How long the grab thread may block on a full queue before dropping the frame.
# Backpressure this long means the encoder is wedged (GPU stall) rather than
# merely behind — a 200-frame queue drains in ~2 s at 100 fps. On timeout the
# frame is dropped and counted in self.drops, and NO block ID or timestamp is
# recorded for it (see _put_frame's caller), so the loss shows up as a GAP in
# blockids.npy — detectable and re-alignable — instead of shifting every later
# frame onto the wrong trigger. Blocking indefinitely would only push the
# backlog upstream until the pylon pool ran dry too.
PUT_TIMEOUT_S = 2.0
#: Frames between the per-thread timing summaries in the log.
STATS_EVERY = 1000
#: Bounds on the decoupled-encoder drain at stop: a full queue is ~1 s of
#: encoding, so a sentinel that cannot be delivered in 30 s or a thread that
#: has not exited 60 s later is wedged, and its bookkeeping is reported as
#: unverified rather than waited on forever.
DRAIN_SENTINEL_TIMEOUT_S = 30.0
DRAIN_JOIN_TIMEOUT_S = 60.0
#: Frames over which the host/device clock offset is taken as the MINIMUM of
#: (host - device) rather than the first sample: the first frames after the
#: ready barrier arrive at the true rate, so their smallest offset is the
#: honest zero for delivery_lag_s, and a single late first frame does not
#: hide a backlog of the same size for the whole run.
CLOCK_BASELINE_FRAMES = 200
#: Seconds of pre-trigger silence tolerated before the stall ladder runs when
#: the caller has NOT signalled that the board started and no frame has
#: arrived. The signal is the honest source (see signal_triggers_started);
#: this bound exists so a caller that never signals still gets a dead camera
#: retired instead of the coordinator force-dropping every trigger for every
#: camera for the rest of the session. It has to cover the whole startup gap
#: with margin: a sketch flash (~30 s) + the readiness barrier (30 s bound) +
#: the serial handshake with a forced reset (~10 s) is ~70 s.
PRE_TRIGGER_GRACE_S = 90.0


def ring_slots(max_lag, kick: bool) -> int:
    """NV12 ring depth per camera for the given mode.

    The capacity preflight budgets RAM from this same function, so the ring the
    grab thread allocates and the ring the preflight refused or permitted are
    always the same size. `max_lag` is ignored when `kick` is False.
    """
    if kick:
        return int(max_lag) + ENCODE_QUEUE_DEPTH + KICK_RING_SLACK
    return ENCODE_QUEUE_DEPTH + DECOUPLED_RING_SLACK


def _end_encode(enc, timeout_s=None) -> None:
    """End an encoder's bitstream, under the caller's deadline where it takes one.

    RULE: a teardown deadline is passed down to EndEncode() whenever the
    encoder accepts one, and omitted otherwise. REASON: EndEncode is the one
    step of a teardown that waits — the CPU encoder flushes and reaps a child
    process there — so a caller bounding the WHOLE teardown (abandon()) must
    hand its remaining budget down, or the bound becomes per camera and nine
    cameras cost nine times what was promised. NVENC's EndEncode takes no
    argument, so the signature is inspected rather than assumed.
    """
    if timeout_s is None:
        enc.EndEncode()
        return
    try:
        params = inspect.signature(enc.EndEncode).parameters
        takes = ("timeout_s" in params
                 or any(p.kind is p.VAR_KEYWORD for p in params.values()))
    except (TypeError, ValueError):
        # A builtin or C-extension method with no introspectable signature.
        takes = False
    if takes:
        enc.EndEncode(timeout_s=timeout_s)
    else:
        enc.EndEncode()


class _EncoderThread(threading.Thread):
    """Drains ready-made NV12 frames from a queue into an H.264 stream.

    The grab thread copies each gray frame straight into a preallocated NV12
    ring buffer (one memcpy, ~0.08 ms into an already-faulted buffer, and numpy
    releases the GIL for it) and queues the buffer; this thread then only calls
    Encode() (releases the GIL) and os.write (ditto) — so the encoder side holds
    the GIL for ~zero time per frame. All cameras' encoder threads run truly
    concurrently.

    If the encoder dies mid-recording, the thread switches to writing the
    remaining queued frames' Y planes (the gray data) raw to ``raw_tail.bin``
    in arrival order, so the recording stays gapless: stream.h264 holds frames
    [0..encoded) and the tail holds [encoded..end). encode_worker encodes the
    tail and concatenates it onto the stream at stop.

    The sentinel contract: run() consumes the queue until it receives None, no
    matter what fails inside, so a caller's `queue.put(None)` always ends the
    thread. The one exception is `abandon()`, which ends it without a sentinel.
    """

    def __init__(self, cam_index: int, enc, h264_fd: int, spill_path: Path,
                 height: int):
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
        #: Set by abandon(): run() exits at its next queue.get() without
        #: flushing, so a caller that cannot deliver a sentinel to a full
        #: queue can still end the thread.
        self._abort = False

    def coded_frames(self) -> int:
        """Frames the encoder actually emitted, not the frames fed to it.

        RULE: bookkeeping that decides which frame maps to which trigger uses
        this count, read after this thread's join and before release_encoder()
        drops the encoder. REASON: blockids.npy records only frames that were
        persisted, and `encoded` counts frames ACCEPTED by Encode() — on the
        encoder-death path the final flush never arrives, so the last frame
        fed is never coded and the fed count over-claims permanently. An
        over-claim of even one frame maps every later frame of this camera to
        the wrong trigger while leaving NO gap in blockids.npy, so it presents
        as a perfect recording. An encoder that returns each frame's bytes
        from Encode() (NVENC) exposes no `frames_out` and needs none: its fed
        count IS its coded count.
        """
        enc = self._enc
        if enc is None:
            return self.encoded
        return min(self.encoded, getattr(enc, "frames_out", self.encoded))

    def release_encoder(self, timeout_s=None):
        """Free this thread's encoder session.

        `EndEncode()` ends the bitstream; the SESSION is released by the encoder
        object's *destructor* (and by `Close()` where the encoder has one), so
        the reference has to be dropped as well. NVENC concurrent sessions are
        capped by the driver, and at 9 cameras the budget is tight enough that
        one leaked session can push a camera onto the raw fallback at ~129 GiB
        per 10 minutes.

        `timeout_s` is the caller's remaining teardown budget, passed on to an
        encoder whose EndEncode takes one; None means the encoder's own
        default. A caller that reads coded_frames() must do so BEFORE this
        call, which drops the encoder.

        ONLY call this once the thread is no longer running — before start() or
        after join(). run() dereferences self._enc per frame.
        """
        enc, self._enc = self._enc, None
        if enc is None:
            return
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

    def abandon(self, timeout: float = 5.0) -> bool:
        """End this thread WITHOUT draining and report whether it exited.

        The queue may be full, so a sentinel cannot be relied on: set the abort
        flag first (run() checks it before touching a frame), then empty the
        queue so a put_nowait(None) lands, then join briefly. Returns True when
        the thread has exited, in which case the caller may release the encoder
        and close the fd; a thread still inside Encode() after `timeout` is
        left alone, because pulling its fd or encoder out from under a live
        native call is worse than leaking them until process exit.
        """
        self._abort = True
        try:
            while True:
                self.queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        if self.is_alive():
            self.join(timeout=timeout)
        return not self.is_alive()

    def _open_spill(self, nv12) -> int:
        """Open raw_tail.bin and write the first spilled plane.

        Returns the fd, or -1 when the spill cannot be opened or written (the
        realistic cause is the same full disk that killed the encoder). -1 is
        the "spill is dead, drop frames" sentinel run() already honours, so a
        failure here degrades to dropping instead of killing the drain thread
        and wedging the queue for the rest of the session.
        """
        fd = None
        try:
            fd = os.open(str(self._spill_path),
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY)
            plane = nv12[:self._height]
            n = os.write(fd, plane)
            if n != plane.nbytes:
                raise OSError(f"short write: {n} of {plane.nbytes} bytes "
                              f"(disk full?)")
            self.spilled += 1
            return fd
        except Exception as e:
            print(f"[enc{self._cam_index}] RAW SPILL could not be started: {e}. "
                  f"Frames from here on are LOST.", flush=True)
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
            return -1

    def run(self):
        # Encoders go on E-cores. They are not latency-critical (Encode() and
        # os.write() both release the GIL; the work is on the GPU) but there is
        # one per camera, so unpinned they compete with the grab threads for
        # the eight P-cores -- which would undo the grab-thread pinning
        # entirely. No priority bump: the point is to yield to capture.
        if getattr(self, "_pin_ecore", False):
            try:
                from gui_app.cpu_affinity import pin_to_efficiency_core
                r = pin_to_efficiency_core(self._cam_index)
                print(f"[enc{self._cam_index}] affinity cpu={r['cpu']} "
                      f"pinned={r['pinned']} (of {r['n_ecores']} E-cores)",
                      flush=True)
            except Exception as e:
                print(f"[enc{self._cam_index}] affinity failed: {e}", flush=True)
        elif getattr(self, "_enc_pcores", False):
            # Confined to the P-core set, deliberately NOT one core each: one
            # encoder per E-core measured catastrophic (worst lag 321), and
            # leaving them unpinned lets Windows exile one to an E-core, which
            # fills the encode queue and stalls every camera through the shared
            # ring. Confinement keeps them on fast cores while the scheduler
            # still balances them around the pinned grab threads.
            try:
                from gui_app.cpu_affinity import restrict_to_performance_cores
                r = restrict_to_performance_cores()
                print(f"[enc{self._cam_index}] affinity cpu={r['cpu']} "
                      f"pinned={r['pinned']} (P-core set)", flush=True)
            except Exception as e:
                print(f"[enc{self._cam_index}] affinity failed: {e}", flush=True)
        spill_fd = None
        try:
            while True:
                nv12 = self.queue.get()
                if self._abort:
                    return
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
                    if spill_fd == -1:
                        continue                 # spill failed; nothing to do but drop
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
                    spill_fd = self._open_spill(nv12)
        except Exception as e:
            # Nothing above is supposed to raise, but the sentinel contract
            # must hold regardless: keep consuming (and discarding) until the
            # sentinel arrives, so the grab thread's drain never blocks on a
            # queue nobody empties. The frames discarded here are accounted
            # for by the encoded+spilled reconciliation at stop.
            print(f"[enc{self._cam_index}] FATAL in encoder thread: "
                  f"{type(e).__name__}: {e}; discarding frames until the "
                  f"sentinel", flush=True)
            self.failed = True
            while not self._abort:
                if self.queue.get() is None:
                    break
        finally:
            # -1 is the "spill died and was already closed" sentinel.
            if spill_fd is not None and spill_fd != -1:
                try:
                    os.close(spill_fd)
                except Exception:
                    pass


class GrabThread(QThread):
    def __init__(self, cam_index: int, camera, backend,
                 raw_path: Path = None, display_every: int = 1,
                 downsample: int = 3, realtime: bool = False,
                 width: int = 0, height: int = 0, quality: int = 21,
                 fps: int = 100, router=None, encoder_factory=None):
        super().__init__()
        self._cam_index = cam_index
        # Set by CameraManager from the profile. See cpu_affinity.py for why
        # this exists at all: on a hybrid CPU the scheduler puts most of our
        # threads on E-cores and picks differently every launch.
        self._pin_cpu = False
        self._pin_ecore = False
        #: Confine the encoder to the P-core SET (not one core each, and not
        #: the E-cores). See _EncoderThread.run for why this exists.
        self._enc_pcores = False
        self.pin_result = None
        self._camera = camera
        # The vendor layer, shared with CameraManager. Only three things are
        # read from it on the hot path, and they are bound once here so the
        # loop body pays no attribute lookup on the backend per frame.
        self._backend = backend
        self._TimeoutException = backend.TimeoutException
        self._grab_strategy = backend.GRAB_STRATEGY
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
        #: Makes the decoupled path's encoder. None means the process default
        #: (gui_app.encoders); a test or a CPU-encode build installs its own.
        self._encoder_factory = encoder_factory
        self._running = False
        self._triggers_stopped = False
        #: Set by signal_triggers_started() once the trigger board has
        #: acknowledged its start command. Until then a retrieve timeout is
        #: expected silence, not a stall: the board is started only after every
        #: grab thread is ready, and that gap (a sketch swap, the readiness
        #: barrier, the serial handshake) can exceed the stall bound. Re-arming
        #: inside it restarts the block-ID counter with no frame history to
        #: re-base it against, which retires the camera before its first
        #: trigger. Not reset by run(): the signal may land before run() is
        #: scheduled.
        self._triggers_started = False
        self._abandoned = False
        self._kick = False
        self.frame_count = 0
        self.timestamps = []
        self.block_ids = []
        self.drops = 0
        #: Problems this thread found with ITS OWN recording (decoupled and raw
        #: modes: a failed resync, block-ID truncation, an encoder that spilled).
        #: In kick mode the router carries the equivalent list; CameraManager
        #: folds whichever applies into last_warnings at stop.
        self.warnings: list = []
        self.latest_frame = None
        self.current_fps = 0.0
        self._fps_times = deque(maxlen=10)
        self._snapshot_requested = False
        self.snapshot_frame = None
        self._keep_full = False
        #: Set once this thread has allocated its ring and called
        #: StartGrabbing, i.e. once it can actually keep up with triggers.
        #: start_acquisition waits on these before the board is started; see
        #: CameraManager.wait_until_ready.
        self.ready = threading.Event()
        self.latest_full_frame = None
        # Stall recovery state (see _rearm_stream / _resync_offset).
        self._last_ts = None          # device timestamp of the last good frame
        self._last_bid_eff = -1       # its globally-consistent block ID
        self._first_ts = None         # device timestamp of the first good frame
        self._first_bid_eff = -1      # its block ID; with the pair above, the
        self._rate_frames = 0         # camera's own measured block-ID rate
        self.rearms = 0               # stream restarts this run
        #: Seconds this camera is behind real time, in every recording mode.
        #: ~0 is healthy; sustained growth means the grab loop is losing to the
        #: trigger. Measured against the camera's device clock with a fixed
        #: offset, so host/camera oscillator drift (~200 ppm, i.e. under a
        #: second per hour) accumulates in it slowly; a real backlog grows two
        #: orders of magnitude faster. The drift-free signal is the
        #: coordinator's cross-camera lag (CameraManager.frontier_lags).
        self.delivery_lag_s = 0.0
        self.desynced = False         # stalled and could not be realigned
        #: True once the retrieve loop has exited and run() is in its drain
        #: and teardown. A thread in this state is not receiving frames, so a
        #: caller escalating a stop must not read its still-moving frame_count
        #: (the decoupled encoder's last pool frames) as a board that ignored
        #: the stop command.
        self.retrieve_loop_exited = False

    def _rearm_stream(self, attempt: int) -> bool:
        """Restart this camera's stream after a stall.

        A GigE Vision stream stall wedges the driver pipeline and never
        self-recovers: the grab loop would time out for the rest of the
        session. Restarting the grab is the only way back without restarting
        the GUI.
        """
        print(f"[grab{self._cam_index}] STALLED — re-arming stream "
              f"(attempt {attempt})", flush=True)
        try:
            self._camera.StopGrabbing()
            self._camera.StartGrabbing(self._grab_strategy)
            return True
        except Exception as e:
            print(f"[grab{self._cam_index}] re-arm failed: "
                  f"{type(e).__name__}: {e}", flush=True)
            return False

    def _block_rate(self) -> float:
        """Block IDs per device-second for THIS camera.

        Measured from the camera's own pre-stall history once it holds
        BLOCK_RATE_MIN_FRAMES frames, otherwise the nominal profile rate. The
        camera oscillators sit a few hundred ppm off the trigger board, and
        that systematic error is what a resync across a gap of tens of seconds
        would otherwise spend its tolerance on: at +250 ppm a 40 s gap is off by
        a whole period against the nominal rate and lands on the wrong ordinal.
        """
        if (self._first_ts is not None and self._last_ts is not None
                and self._rate_frames >= BLOCK_RATE_MIN_FRAMES):
            dur = self._last_ts - self._first_ts
            span = self._last_bid_eff - self._first_bid_eff
            if dur > 0 and span > 0:
                return span / dur
        return float(self._fps)

    def _note_block_id(self, bid: int, dev_ts: float) -> None:
        """Record a good frame's ordinal and device time for stall recovery."""
        if self._first_ts is None:
            self._first_ts = dev_ts
            self._first_bid_eff = bid
        self._last_bid_eff = bid
        self._last_ts = dev_ts
        self._rate_frames += 1

    def _resync_offset(self, raw_bid: int, ts: float):
        """Block-ID offset that keeps trigger ordinals globally consistent.

        StartGrabbing restarts the camera's block-ID counter, so a re-armed
        camera looks like it jumped tens of thousands of triggers backwards —
        which the coordinator's 16-bit unwrap would misread as a wrap, place it
        far AHEAD, and force-drop every other camera. Post-hoc alignment reads
        the same restart the same way.

        The device timestamp is a free-running hardware clock that survives the
        restart, and the triggers are hardware-timed, so the number of missed
        periods is measurable rather than guessed. Returns None when the gap
        doesn't land cleanly on a period boundary — better to declare desync
        than to publish frames under the wrong trigger ordinal.
        """
        if self._last_ts is None or self._fps <= 0:
            return None
        periods = (ts - self._last_ts) * self._block_rate()
        k = round(periods)
        resid = abs(periods - k)
        if k < 1 or resid > 0.25:
            print(f"[grab{self._cam_index}] cannot resync: {periods:.2f} trigger "
                  f"periods elapsed, {resid:.2f} off a boundary", flush=True)
            return None
        return (self._last_bid_eff + k) - raw_bid

    def _put_frame(self, enc_thread: _EncoderThread, img) -> bool:
        """Copy the gray frame into the next NV12 ring buffer and queue it.

        A ring slot is consumed only by a SUCCESSFUL put: a dropped frame is
        overwritten in the same slot by the next one. The ring is only
        ENCODE_QUEUE_DEPTH + DECOUPLED_RING_SLACK deep, so advancing on a drop
        would, after a few drops against a wedged encoder, write into the slot
        Encode() is reading and then into slots whose block IDs are already
        recorded, i.e. newer pixels under older ordinals once the encoder
        recovers. The timed put is deliberate and stays: it waits for a merely
        slow encoder and only drops when it is wedged.

        Copying straight into NV12 here (instead of img.copy() + a second copy
        in the encoder) halves the memcpy work per frame."""
        buf = self._nv12_ring[self._ring_i]
        buf[:self._height, :] = img  # Y plane = gray; UV stays 128
        try:
            enc_thread.queue.put(buf, timeout=PUT_TIMEOUT_S)
        except queue.Full:
            self.drops += 1
            if self.drops in (1, 10, 100) or self.drops % 1000 == 0:
                print(f"[grab{self._cam_index}] ENCODER BACKPRESSURE: encoder "
                      f"not draining, dropped {self.drops} frames so far", flush=True)
            return False
        self._ring_i = (self._ring_i + 1) % len(self._nv12_ring)
        return True

    def _log_stream_stats(self):
        """Dump the backend's per-stream counters — distinguishes network loss
        (Failed_Buffer/Resend_Request) from pool exhaustion (Buffer_Underrun).
        Statistic_Failed_Packet_Count is deliberately not among them; see
        `backends.basler.stream_stats`."""
        try:
            stats = self._backend.stream_stats(self._camera)
        except Exception as e:
            stats = {"error": f"{type(e).__name__}: {e}"}
        if stats.get("error"):
            print(f"[grab{self._cam_index}] stream stats unavailable: "
                  f"{stats['error']}", flush=True)
        else:
            print(f"[grab{self._cam_index}] stream stats: {stats}", flush=True)

    def _give_up(self, reason: str) -> None:
        """Retire this camera for the rest of the run, in every recording mode.

        In kick mode the coordinator must be told, or it waits for a camera
        that will never publish and force-drops every trigger for all of them;
        the router folds the retirement into the session warnings. In the
        decoupled and raw modes nobody else knows, so the warning is recorded
        here for CameraManager to surface. Calling this twice is harmless.
        """
        if self.desynced:
            return
        self.desynced = True
        if self._kick and self._router is not None:
            self._router.retire(self._cam_index, reason)
        else:
            msg = (f"cam{self._cam_index + 1}: {reason}. Its recording ends at "
                   f"that point; frames after it were not recorded.")
            print(f"[grab{self._cam_index}] WARNING: {msg}", flush=True)
            self.warnings.append(msg)

    def _release_orphan_encoder(self, enc, enc_thread) -> None:
        """Drop an encoder that never got to run, freeing its session NOW.

        The run() frame otherwise keeps `enc` referenced for the whole raw.bin
        fallback recording, and the session is freed only by the destructor.
        """
        if enc_thread is not None:
            try:
                enc_thread.release_encoder()
            except Exception:
                pass
        elif enc is not None:
            try:
                enc.EndEncode()
            except Exception:
                pass
            del enc
        gc.collect()

    @staticmethod
    def _unlink_if_empty(path: Path) -> None:
        """Remove a zero-byte stream file left by a failed encoder start, so
        the post-hoc encoder falls through to raw.bin instead of remuxing an
        empty file and reporting the camera as failed with raw.bin unused."""
        try:
            if path.exists() and path.stat().st_size == 0:
                path.unlink()
        except Exception:
            pass

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
                import gui_app.cpu_affinity as _ca
                from gui_app.cpu_affinity import (
                    place_capture_thread, restrict_to_performance_cores)
                THREAD_PRIORITY_HIGHEST = _ca.GRAB_THREAD_PRIORITY
                if self._pin_cpu == "set":
                    r = restrict_to_performance_cores(
                        priority=THREAD_PRIORITY_HIGHEST)
                else:
                    # Not pin_to_performance_core: its round-robin doubles two
                    # cameras onto one core once there are more cameras than
                    # P-cores. See place_capture_thread.
                    r = place_capture_thread(
                        self._cam_index, priority=THREAD_PRIORITY_HIGHEST)
                self.pin_result = r
                print(f"[grab{self._cam_index}] affinity cpu={r['cpu']} "
                      f"pinned={r['pinned']} prio={r['priority']} "
                      f"(of {r['n_pcores']} P-cores)", flush=True)
            except Exception as e:
                print(f"[grab{self._cam_index}] affinity failed: {e}",
                      flush=True)
        self._running = True
        self._triggers_stopped = False
        self._abandoned = False
        self.retrieve_loop_exited = False
        self.frame_count = 0
        self.timestamps = []
        self.block_ids = []
        self.warnings = []
        self.drops = 0
        recording = self._raw_path is not None
        fd = None              # raw.bin descriptor (raw-to-disk mode / fallback)
        h264_fd = None         # H.264 elementary-stream descriptor
        h264_path = None
        enc = None             # decoupled encoder, until its thread owns it
        enc_thread = None      # decoupled encoder drain thread (real-time mode)
        kick = recording and self._realtime and self._router is not None
        self._kick = kick

        if kick:
            # Frames go to the shared router; this thread keeps no encoder. The
            # ring must outlast a frame's whole journey (held by the coordinator
            # up to max_lag, then queued at the encoder) before its slot reuses.
            ring_n = ring_slots(self._router.max_lag, kick=True)
            try:
                self._nv12_ring = [
                    np.full((self._height * 3 // 2, self._width), 128, np.uint8)
                    for _ in range(ring_n)]
            except MemoryError as e:
                # Reachable, not theoretical: the ring is 2.39 GiB per camera at
                # max_lag=480 (744 buffers x 3.456 MB), so 9 cameras is ~21.5 GiB
                # of ring on top of the pylon buffer pool. Unprotected, a
                # MemoryError here escapes run() and takes the GUI down — and in
                # kick mode a camera that never publishes makes the coordinator
                # force-drop EVERY trigger for EVERY camera, so the session
                # yields empty videos from all of them. Retire so the others
                # record aligned.
                gib = ring_n * self._width * (self._height * 3 // 2) / 2**30
                print(f"[grab{self._cam_index}] FATAL: could not allocate the "
                      f"{ring_n}-buffer NV12 ring ({gib:.2f} GiB): {e}", flush=True)
                self._give_up("could not allocate its NV12 ring")
                self.ready.set()        # never hold the barrier open
                return
            self._ring_i = 0
            print(f"[grab{self._cam_index}] real-time kick-out -> shared router "
                  f"(ring={ring_n})", flush=True)
        elif recording and self._realtime:
            try:
                factory = self._encoder_factory or encoders.get_default_factory()
                notes: list = []
                enc = factory(self._width, self._height, self._quality,
                              self._fps, notes)
                self.warnings.extend(f"cam{self._cam_index + 1}: {n}"
                                     for n in notes)
                h264_path = self._raw_path.parent / "stream.h264"
                h264_fd = os.open(str(h264_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY)
                enc_thread = _EncoderThread(
                    self._cam_index, enc, h264_fd,
                    self._raw_path.parent / "raw_tail.bin",
                    self._height)
                enc = None          # the thread owns it now
                enc_thread._pin_ecore = self._pin_ecore
                enc_thread._enc_pcores = self._enc_pcores
                # NV12 ring: grab copies gray directly into these (UV preset to
                # 128, which also pre-faults every page). Slack over the queue
                # capacity covers the frame in Encode() and the one being
                # written; a slot is consumed only by a successful put.
                self._nv12_ring = [
                    np.full((self._height * 3 // 2, self._width), 128, np.uint8)
                    for _ in range(ring_slots(None, kick=False))]
                self._ring_i = 0
                enc_thread.start()
                print(f"[grab{self._cam_index}] real-time encode (decoupled) -> {h264_path.name}", flush=True)
            except Exception as e:
                # Encoder unavailable: fall back to raw-to-disk so no data is
                # lost. Anything created so far is undone: the session goes
                # back (the next acquisition needs it), and an empty
                # stream.h264 is removed so the post-hoc encoder picks raw.bin.
                print(f"[grab{self._cam_index}] encoder init failed, falling back to raw.bin: {e}", flush=True)
                self._release_orphan_encoder(enc, enc_thread)
                enc = None
                enc_thread = None
                if h264_fd is not None:
                    os.close(h264_fd); h264_fd = None
                if h264_path is not None:
                    self._unlink_if_empty(h264_path)
                    h264_path = None

        if recording and not kick and enc_thread is None:
            fd = os.open(str(self._raw_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY)

        print(f"[grab{self._cam_index}] StartGrabbing (recording={recording})", flush=True)
        try:
            self._camera.StartGrabbing(self._grab_strategy)
            # Ring allocated, stream started: this thread can now keep up with
            # the trigger rate. Announce it BEFORE the retrieve loop so the
            # board is not started against a thread still writing 2.57 GiB of
            # ring. Set on the failure paths too -- a thread that will never be
            # ready must not hold the barrier until it times out.
            self.ready.set()
        except Exception as e:
            self.ready.set()
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
            if recording:
                self._give_up("camera did not start grabbing")
            if fd is not None:
                os.close(fd)
            if enc_thread is not None:
                enc_thread.queue.put(None)
                enc_thread.join(timeout=10)
                if not enc_thread.is_alive():
                    enc_thread.release_encoder()
            if h264_fd is not None:
                os.close(h264_fd)
                self._unlink_if_empty(h264_path)
            self._write_warnings(recording and not kick)
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
        t_wait = 0.0   # cumulative s blocked in RetrieveResult (per STATS_EVERY frames)
        t_proc = 0.0   # cumulative s spent processing a frame (per STATS_EVERY frames)
        # --- lag diagnostics -------------------------------------------------
        # Delivery lag = how stale a frame is when we finally retrieve it,
        # measured against the camera's own clock so host scheduling can't skew
        # it. The pool is deep (max_num_buffer, profile) and GrabStrategy_OneByOne
        # hands frames over oldest-first, so a thread that stalls briefly and
        # then only just keeps up carries that backlog for the rest of the run.
        # deliv_lag is the live regression watch for the grab loop losing to the
        # trigger: it must stay flat (~0 s); a GIL-held copy in the loop drives
        # it to +10 s over 20k frames.
        clock_off = None          # min(host - device) over the first frames
        deliv_lag = 0.0           # seconds of accumulated delivery delay
        t_copy = t_submit = t_disp = 0.0   # where the per-frame budget goes
        # t_wait + t_proc do NOT cover the whole iteration: Release(), the fps
        # bookkeeping and the loop edge sit outside both, and a laggard can show
        # LOWER proc than its peers while losing 1.5% per cycle in that gap.
        # t_cycle closes it and must read the trigger period (10.00 ms).
        # Anything above the period is the regression.
        t_rel = t_cycle = 0.0
        t_prev = None
        stats_line = None       # printed AFTER Release(), never inside the with
        t_loop_start = time.perf_counter()   # anchors PRE_TRIGGER_GRACE_S

        try:
            while self._running and self._camera.IsGrabbing():
                try:
                    timeout = 200 if recording else 2000
                    t0 = time.perf_counter()
                    result = self._backend.retrieve(self._camera, timeout)
                    t1 = time.perf_counter()
                    t_wait += t1 - t0

                    # Everything from here to the matching `finally` runs with
                    # the driver buffer held, and Release() runs on EVERY exit
                    # from it: success, `continue`, `break`, or an exception on
                    # its way to the handler below. The pool must get its buffer
                    # back once per result by construction, not by the accident
                    # of the variable being rebound on the next retrieve. The
                    # failed-grab branch is inside for the same reason: reading
                    # ErrorCode/ErrorDescription off a failed result can itself
                    # raise, and that path must release too.
                    stats_line = None
                    try:
                        if not result.GrabSucceeded():
                            print(f"[grab{self._cam_index}] grab failed: "
                                  f"{result.ErrorCode} {result.ErrorDescription}",
                                  flush=True)
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
                            # Drop this camera from the alignment set so the others
                            # keep recording aligned instead of the coordinator
                            # starving on a camera that will never publish.
                            self._give_up("camera reports row padding")
                            break
                        if not zc_verified:
                            zc_verified = True
                            print(f"[grab{self._cam_index}] zero-copy view OK "
                                  f"(PaddingX=0 PaddingY=0)", flush=True)

                        with result.GetArrayZeroCopy() as img:
                            if self._snapshot_requested:
                                self.snapshot_frame = img.copy()  # full-resolution still
                                self._snapshot_requested = False

                            # A retrieved frame means the stream is not stalled;
                            # the error bound resets only once the frame has been
                            # processed, below, so a per-frame failure inside this
                            # block counts toward it.
                            consec_timeouts = 0
                            if recording:
                                # Deliberately unguarded: a BlockID or TimeStamp the
                                # result cannot supply is a per-frame error like any
                                # other and goes to the handler below, which retires
                                # the camera after MAX_CONSEC_ERRORS. A placeholder
                                # ordinal (-1) would instead starve or force-drop
                                # every camera through the coordinator and poison
                                # post-hoc alignment, silently.
                                raw_bid = result.BlockID
                                dev_ts = result.TimeStamp * 1e-9
                                # How far behind the camera's own clock we are now.
                                if clock_off is None or (
                                        frame_n < CLOCK_BASELINE_FRAMES
                                        and t1 - dev_ts < clock_off):
                                    clock_off = t1 - dev_ts
                                deliv_lag = (t1 - dev_ts) - clock_off
                                # Published live, in every recording mode. This is
                                # the honest health signal: how far behind real
                                # time this camera is RIGHT NOW. The failure it
                                # catches is silent by construction — the buffer
                                # pool absorbs a per-frame deficit for minutes
                                # before anything errors, so by the time frames are
                                # lost the session is already spoiled.
                                self.delivery_lag_s = deliv_lag
                                if awaiting_resync:
                                    awaiting_resync = False
                                    off = self._resync_offset(raw_bid, dev_ts)
                                    if off is None:
                                        # No ordinal for this camera's frames from
                                        # here on, in ANY mode: post-hoc alignment
                                        # would read the restarted counter as a
                                        # wrap, so stop recording it rather than
                                        # keep grabbing 2.3 MB/frame of data that
                                        # cannot be placed in time.
                                        self._give_up("stream stalled and block IDs "
                                                      "could not be realigned")
                                        break
                                    bid_offset = off
                                    print(f"[grab{self._cam_index}] resynced after "
                                          f"re-arm, block-ID offset {off}", flush=True)
                                bid = raw_bid + bid_offset
                                if bid <= 0:
                                    # GVSP reserves 0 and nothing below it is an
                                    # ordinal. Recording one would be read as a
                                    # 16-bit wrap by every consumer; raising here
                                    # counts it as a frame error instead.
                                    raise ValueError(
                                        f"block ID {bid} is not a trigger ordinal")
                                self._note_block_id(bid, dev_ts)
                                if kick:
                                    # Copy gray into the next ring slot and submit to the
                                    # router; it records metadata for frames it RELEASES
                                    # (the common set), so this thread records none.
                                    buf = self._nv12_ring[self._ring_i]
                                    self._ring_i = (self._ring_i + 1) % len(self._nv12_ring)
                                    tc0 = time.perf_counter()
                                    buf[:self._height, :] = img
                                    t_copy += time.perf_counter() - tc0
                                    ts0 = time.perf_counter()
                                    self._router.submit(self._cam_index, bid,
                                                        dev_ts, buf)
                                    t_submit += time.perf_counter() - ts0
                                    self.frame_count += 1  # grabbed count (for logging)
                                elif enc_thread is not None:
                                    if self._put_frame(enc_thread, img):
                                        # Recorded only for frames the queue took:
                                        # a dropped frame is a GAP in blockids.npy,
                                        # never a shifted index. The queue taking a
                                        # frame is still not the same as the encoder
                                        # persisting it; the finally block
                                        # reconciles against encoded+spilled.
                                        self.frame_count += 1
                                        self.timestamps.append(dev_ts)
                                        self.block_ids.append(bid)
                                else:
                                    os.write(fd, img)
                                    self.frame_count += 1
                                    self.timestamps.append(dev_ts)
                                    self.block_ids.append(bid)

                            consec_errors = 0
                            frame_n += 1
                            if recording and not first_frame_logged:
                                first_frame_logged = True
                                stats_line = f"[grab{self._cam_index}] first frame received"
                            t_proc += time.perf_counter() - t1
                            if recording and frame_n % STATS_EVERY == 0:
                                # wait >> proc and ~10 ms/frame -> loop keeps up (waits
                                # for triggers); proc-bound or wait ~0 -> loop is the
                                # bottleneck and a pool backlog is building. Per-frame
                                # milliseconds, averaged over STATS_EVERY frames.
                                qd = enc_thread.queue.qsize() if enc_thread else (
                                    self._router.pending() if kick else 0)
                                ms = 1e3 / STATS_EVERY
                                stats_line = (
                                    f"[grab{self._cam_index}] frames={frame_n} timeouts={timeout_n} "
                                    f"avg_wait={t_wait * ms:.2f}ms avg_proc={t_proc * ms:.2f}ms "
                                    f"qsize={qd} | deliv_lag={deliv_lag:+.3f}s "
                                    f"copy={t_copy * ms:.2f} submit={t_submit * ms:.2f} "
                                    f"disp={t_disp * ms:.2f} rel={t_rel * ms:.2f} "
                                    f"cycle={t_cycle * ms:.2f}ms")
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
                    finally:
                        tr0 = time.perf_counter()
                        result.Release()
                        t_rel += time.perf_counter() - tr0
                    if t_prev is not None:
                        t_cycle += tr0 - t_prev   # start-of-iteration to start
                    t_prev = tr0
                    if stats_line is not None:
                        # Console writes can take milliseconds; they happen with
                        # the driver buffer already returned.
                        print(stats_line, flush=True)

                except self._TimeoutException:
                    timeout_n += 1
                    consec_timeouts += 1
                    if recording and timeout_n in (1, 5, 20):
                        print(f"[grab{self._cam_index}] TIMEOUT #{timeout_n} (no frame in {timeout}ms, triggers_stopped={self._triggers_stopped})", flush=True)
                    if recording and self._triggers_stopped:
                        break
                    if not self._running:
                        break
                    # A stall is silence while triggers are known to be
                    # running: either the caller has signalled that the board
                    # acknowledged its start, or a frame has already arrived
                    # (which also gives _resync_offset the history it needs).
                    # Before both, the board has not been started and the
                    # timeouts are expected; re-arming here would restart the
                    # block-ID counter with nothing to re-base it against and
                    # retire the camera on its first real frame. The grace is
                    # bounded so a caller that never signals cannot leave a
                    # dead camera un-retired for the whole session. Two
                    # attribute reads, on the timeout path only.
                    if (recording and not self._triggers_started
                            and self._last_ts is None
                            and t0 - t_loop_start < PRE_TRIGGER_GRACE_S):
                        consec_timeouts = 0
                        continue
                    # Stalled: the stream has gone quiet while triggers are still
                    # running. Restart it rather than time out for the rest of
                    # the session.
                    if (consec_timeouts >= STALL_TIMEOUTS and not self.desynced
                            and self.rearms < MAX_REARMS):
                        self.rearms += 1
                        consec_timeouts = 0
                        if self._rearm_stream(self.rearms):
                            # The restarted counter must be re-based in EVERY
                            # recording mode: post-hoc alignment reads a restart
                            # as a 16-bit wrap exactly as the coordinator would.
                            awaiting_resync = recording
                        elif recording:
                            self._give_up("stream stalled, re-arm failed")
                            break
                    elif (consec_timeouts >= STALL_TIMEOUTS and not self.desynced
                            and recording):
                        # Re-arms exhausted. Without this the condition above just
                        # stays false forever: the thread keeps timing out in
                        # silence, its frontier frozen, and the coordinator
                        # force-drops every trigger for EVERY camera — the whole
                        # session comes back empty with nothing but timeout lines
                        # to show for it.
                        self._give_up(f"stream dead after {MAX_REARMS} re-arms")
                        # Must break. Retiring sets desynced, which makes this
                        # branch false forever after, so without the break the
                        # thread would sit timing out for the rest of the session
                        # — the exact silence this fix exists to end. (Caught by
                        # test_rearm_exhaustion_retires hanging.)
                        break
                except Exception as e:
                    print(f"[grab{self._cam_index}] exception: {type(e).__name__}: {e}", flush=True)
                    # The with-target may still be bound to a view over a buffer
                    # that Release() has already returned; drop it.
                    img = None
                    if not self._running:
                        break
                    # This handler must count toward consec_errors so the camera
                    # is written off: a print-sleep-loop here lets a camera
                    # consume 100 fps, discard everything and starve every other
                    # camera through the coordinator.
                    consec_errors += 1
                    if consec_errors >= MAX_CONSEC_ERRORS:
                        print(f"[grab{self._cam_index}] FATAL: {consec_errors} "
                              f"consecutive frame errors, giving up on this camera",
                              flush=True)
                        if recording:
                            self._give_up(f"repeated frame-processing errors: "
                                          f"{type(e).__name__}: {e}")
                        break
                    time.sleep(0.001)
        finally:
            # First, before any drain can block: from here on this thread
            # receives no frames, whatever frame_count still does.
            self.retrieve_loop_exited = True
            print(f"[grab{self._cam_index}] exiting: frames={frame_n} "
                  f"timeouts={timeout_n} drops={self.drops} rearms={self.rearms}"
                  + (" DESYNCED" if self.desynced else ""), flush=True)
            # Catch-all. Any exit that is NOT a normal stop must retire this
            # camera, or the coordinator waits forever for a thread that is gone
            # and force-drops every trigger for every other camera. The explicit
            # retires above cover the paths we know about; this covers the ones
            # we don't — notably `IsGrabbing()` going False under us, which just
            # falls out of the while loop with no error at all. A stop the
            # operator asked for (triggers stopped, or the session abandoned)
            # is a normal exit and must not read as a retirement.
            if (recording and not self._triggers_stopped
                    and not self._abandoned and not self.desynced):
                self._give_up("grab thread exited before the recording was stopped")
            if recording:
                self._log_stream_stats()  # before StopGrabbing resets counters
            enc_exited = True
            if enc_thread is not None:
                enc_exited = self._finish_encoder(enc_thread)
            if h264_fd is not None:
                if enc_exited:
                    os.close(h264_fd)
                else:
                    # A live encoder thread still writes to this fd. Closing it
                    # would make its next os.write fail as "encoder died" and
                    # spill into a directory being finalised or deleted, and a
                    # reused fd number could receive H.264 bytes meant for the
                    # stream. Leaking it until process exit is the lesser harm.
                    print(f"[grab{self._cam_index}] leaking stream fd under a "
                          f"live encoder thread", flush=True)
            if fd is not None:
                os.close(fd)
            self._write_warnings(recording and not kick)
            try:
                self._camera.StopGrabbing()
            except Exception:
                pass

    def _finish_encoder(self, enc_thread: _EncoderThread) -> bool:
        """End the decoupled encoder and reconcile this camera's bookkeeping.
        Returns whether the encoder thread exited (False means its fd and
        session must be left alone).

        blockids.npy must only record frames that were actually persisted. The
        list here grew by successful queue.put, which means the queue accepted
        the frame, not that it was encoded: a dead encoder whose spill also died
        accepts and discards, so the list is truncated to encoded + spilled
        (both in arrival order) and the repair is written to WARNINGS.txt. The
        same rule SyncEncodeRouter.stop() enforces for kick mode.

        An abandoned session skips the drain entirely: the encoder is aborted,
        released if it exits, and nothing is reconciled because nothing will be
        kept.
        """
        cam = f"cam{self._cam_index + 1}"
        if self._abandoned:
            if enc_thread.abandon(timeout=2.0):
                try:
                    enc_thread.release_encoder()
                except Exception:
                    pass
                return True
            print(f"[grab{self._cam_index}] encoder thread still inside "
                  f"Encode at abandon; leaving its session to process exit",
                  flush=True)
            return False
        # Drain: sentinel, then wait for the backlog (~1 s at full queue).
        try:
            enc_thread.queue.put(None, timeout=DRAIN_SENTINEL_TIMEOUT_S)
        except queue.Full:
            print(f"[grab{self._cam_index}] encoder queue wedged at stop, abandoning drain", flush=True)
        enc_thread.join(timeout=DRAIN_JOIN_TIMEOUT_S)
        if enc_thread.is_alive():
            # Counters still moving: any repair would be a snapshot of a moving
            # target. Say the mapping is unverified rather than guess. The
            # session is leaked on purpose: release_encoder() nulls self._enc
            # while a live run() still dereferences it per frame.
            msg = (f"{cam}: encoder did not finish draining, so the "
                   f"frame-to-trigger mapping is UNVERIFIED. Do not trust this "
                   f"camera's alignment without checking the mp4 frame count "
                   f"against blockids.npy.")
            print(f"[grab{self._cam_index}] WARNING: {msg}", flush=True)
            self.warnings.append(msg)
            return False
        # The coded count is taken here, between the join and the release: the
        # encoder must still exist to be asked, and its counters must have
        # stopped moving.
        coded = enc_thread.coded_frames()
        print(f"[grab{self._cam_index}] encoded={enc_thread.encoded} "
              f"coded={coded} spilled={enc_thread.spilled}", flush=True)
        # Hand the session back explicitly and NOW, rather than whenever this
        # thread object happens to become garbage: the next acquisition needs
        # it, and this non-kick path is itself the fallback used when sessions
        # are scarce.
        try:
            enc_thread.release_encoder()
        except Exception as e:
            print(f"[grab{self._cam_index}] encoder release failed: {e}", flush=True)
        persisted = coded + enc_thread.spilled
        claimed = len(self.block_ids)
        if persisted != claimed:
            msg = (f"{cam}: block-ID bookkeeping claimed {claimed} frames but "
                   f"only {persisted} were persisted (coded={coded} of "
                   f"{enc_thread.encoded} fed, spilled={enc_thread.spilled}, "
                   f"encoder_failed={enc_thread.failed}); "
                   f"truncated to {persisted} so frame indices still map to the "
                   f"correct triggers")
            print(f"[grab{self._cam_index}] WARNING: {msg}", flush=True)
            self.warnings.append(msg)
            del self.block_ids[persisted:]
            del self.timestamps[persisted:]
            self.frame_count = min(self.frame_count, persisted)
        if enc_thread.spilled > 0:
            # The split point between stream.h264 and raw_tail.bin, for the
            # post-hoc encoder: if the tail cannot be merged it truncates the
            # metadata to the coded count instead of over-claiming. The coded
            # count, not the fed one, is where the stream really ends.
            write_split_point(self._raw_path.parent, coded, enc_thread.spilled)
            msg = (f"{cam}: the encoder failed after {coded} frames; "
                   f"{enc_thread.spilled} frames were spilled raw to raw_tail.bin "
                   f"and are merged at encode time")
            self.warnings.append(msg)
        return True

    def _write_warnings(self, own_dir: bool) -> None:
        """Write this camera's warnings beside its video (decoupled/raw modes).

        Kick mode's WARNINGS.txt is the router's; this thread's list still
        reaches the operator through CameraManager.last_warnings.
        """
        if not own_dir or not self.warnings or self._raw_path is None:
            return
        try:
            (self._raw_path.parent / "WARNINGS.txt").write_text(
                "\n".join(self.warnings) + "\n")
        except Exception as e:
            print(f"[grab{self._cam_index}] could not write WARNINGS.txt: {e}",
                  flush=True)

    def signal_triggers_started(self):
        """Arm the stall detector: the trigger board has acknowledged its start.

        Called once start_triggers() returns True. From here a run of retrieve
        timeouts means the stream has stalled and is re-armed; before it the
        same silence is the board not yet running and is ignored, for up to
        PRE_TRIGGER_GRACE_S. A frame arriving arms the detector as well, so a
        caller that never signals still gets stall recovery once the recording
        is under way, and a camera that never delivers is still retired once
        the grace runs out.
        """
        self._triggers_started = True

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

    def abandon(self):
        """Stop WITHOUT draining: the session is being thrown away.

        The loop exits at its next retrieve or timeout; the finally block then
        aborts the decoupled encoder instead of waiting up to 90 s for it to
        drain, closes the stream fds at once so the directory can be deleted,
        and does not record the exit as a retirement.
        """
        self._abandoned = True
        self._running = False


def write_split_point(cam_dir: Path, encoded: int, spilled: int) -> None:
    """Persist how many frames stream.h264 holds before raw_tail.bin begins.

    Written as encoded.json beside the stream whenever frames were spilled.
    The post-hoc encoder reads it: if merging the raw tail fails, it truncates
    blockids.npy and frametimes.npy to `encoded` and keeps stream.h264 rather
    than claiming frames the mp4 does not contain.
    """
    try:
        (Path(cam_dir) / "encoded.json").write_text(json.dumps({
            "encoded": int(encoded),
            "spilled": int(spilled),
            "persisted": int(encoded) + int(spilled),
        }))
    except Exception as e:
        print(f"[grab] could not write encoded.json in {cam_dir}: {e}",
              flush=True)
