"""PyNvVideoCodec loader shim + encoder factory for real-time GPU H.264 encoding.

The package only adds the CUDA runtime to the DLL search path when CUDA_PATH is
set; on this rig the CUDA toolkit isn't installed and we rely on the pip-provided
`nvidia-cuda-runtime-cu12` wheel (cudart64_12.dll — the NVENC-13 build links the
CUDA *12* runtime). So we add that directory ourselves before importing.

Everything is guarded: if PyNvVideoCodec or the runtime is missing, available()
returns False and the caller falls back to raw-to-disk. The GUI never breaks just
because the GPU encode path is unavailable.
"""
from pathlib import Path
import gc
import os
import re
import sysconfig
import threading

import numpy as np

from gui_app import cuda_driver

_nvc = None
_load_error = None
_loaded = False
_lock = threading.Lock()

# NVENCSTATUS codes that are NOT configuration problems. Descending the kwarg
# ladder on these is wrong: it cannot help, and if a session happens to free up
# mid-ladder a LATER rung succeeds with a REDUCED config — which is how a
# recording silently ends up with NVENC's driver-default GOP (CLAUDE.md: one IDR
# for an 898 s / 415 MB file, unseekable in LUC3D, unwalkable by ffprobe).
_NVENC_FATAL = {
    1: "no encode-capable device",
    2: "unsupported device",
    4: "invalid encoder device",
    5: "invalid device",
    10: "out of memory",
    21: "maximum concurrent NVENC sessions reached",
}


def _nvenc_status(err):
    """Extract the NVENCSTATUS integer from a PyNvVideoCodec exception, or None.

    The library surfaces failures as `... Error code : N ...` in the message
    text; there is no typed exception to catch.
    """
    m = re.search(r"[Ee]rror code\s*:\s*(\d+)", str(err))
    return int(m.group(1)) if m else None


def _load():
    global _nvc, _load_error, _loaded
    if _loaded:
        return
    # Serialize: the 6 grab threads call this near-simultaneously. Without the
    # lock the first thread sets _loaded and starts the (slow) import, the others
    # see _loaded=True, return early, and find _nvc still None -> they wrongly
    # fall back to raw ("unavailable: None"). The lock makes every caller wait
    # until the import has actually finished.
    with _lock:
        if _loaded:
            return
        try:
            cudart = os.path.join(sysconfig.get_paths()["purelib"],
                                  "nvidia", "cuda_runtime", "bin")
            if os.path.isdir(cudart) and hasattr(os, "add_dll_directory"):
                os.add_dll_directory(cudart)
            import PyNvVideoCodec as nvc
            _nvc = nvc
        except Exception as e:  # missing wheel, missing cudart, no GPU, etc.
            _load_error = e
            print(f"[nvenc] PyNvVideoCodec unavailable, will fall back to raw: {e}", flush=True)
        if _nvc is not None:
            _warm()
        _loaded = True


def _warm():
    """Force PyNvVideoCodec's first-Encode lazy import, single-threaded.

    Encode() pulls in more machinery the first time it is called. When six
    encoder threads hit that simultaneously they pile up on the import
    machinery and the WHOLE PROCESS wedges — one thread parks in
    importlib.find_spec() under Encode() while every grab thread sits idle and
    the recording produces nothing at all. Doing one throwaway encode here, inside the load lock,
    means the encoder threads only ever meet the already-imported fast path.

    Tiny frame, and failures are swallowed: this is a hazard removal, not a
    requirement. create_h264_encoder() still surfaces real errors.
    """
    import numpy as np
    enc = None
    try:
        # 256x256, not smaller: NVENC rejects 128x128 with "CreateEncoder
        # Error code : 8", which silently skipped this warmup when first added.
        enc = _nvc.CreateEncoder(256, 256, "NV12", True, codec="h264")
        enc.Encode(np.full((384, 256), 128, np.uint8))
        try:
            enc.EndEncode()
        except Exception:
            pass
        print("[nvenc] warmed (first-Encode import done single-threaded)", flush=True)
    except Exception as e:
        print(f"[nvenc] warmup skipped: {e}", flush=True)
    finally:
        # MUST release the session, not just end the stream. The encode session
        # is freed by the object's destructor, so EndEncode() alone leaves it
        # held for the life of the process — permanently costing one of the
        # GPU's concurrent-session slots. At 9 cameras the budget is
        # n_cams + encode_parallel + this one, and the cap is finite (currently
        # 12 on this driver), so a leaked warm session can be the difference
        # between all cameras encoding and one silently falling back to raw.
        if enc is not None:
            try:
                del enc
            except Exception:
                pass
            gc.collect()


def available() -> bool:
    _load()
    return _nvc is not None


def load_error() -> str:
    return str(_load_error) if _load_error else ""


class NvencUnavailable(RuntimeError):
    """Encoder creation failed for a reason no kwarg set can fix.

    `status` is the NVENCSTATUS code (21 is the concurrent-session cap). A
    RuntimeError, so every caller that catches RuntimeError still does.
    """

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def create_h264_encoder(width: int, height: int, qp: int,
                        fps: int = 100,
                        preset: str = "P3", tuning: str = "low_latency",
                        notes: list | None = None):
    """Create an NVENC H.264 encoder for NV12 input (CPU input buffer).

    Mono frames are fed as NV12 where the Y plane is the gray data and the UV
    plane is a constant 128.

    How frames reach the GPU follows `configure_upload()`. The default,
    'host', returns PyNvVideoCodec's encoder itself. 'pinned' returns a
    `PinnedUploadEncoder` with the same Encode/EndEncode surface and a Close().

    `notes`, when given, receives one human-readable line per way the created
    encoder differs from what the profile asked for (a rung below the full
    config, or the host upload used in place of the configured pinned one).
    The caller folds those into the recording's warnings so a degraded encode
    reaches WARNINGS.txt rather than only stdout.

    Raises RuntimeError when no accepted kwarg set can create an encoder. A
    caller that cannot get an encoder falls back to raw frames; it never
    receives an encoder with an unknown GOP.
    """
    _load()
    if _nvc is None:
        raise RuntimeError(f"PyNvVideoCodec unavailable: {_load_error}")
    upload, context = _snapshot_upload()
    if upload == "pinned":
        return _create_pinned(width, height, qp, fps, preset, tuning, notes,
                              context)
    return _create_session(width, height, qp, fps, preset, tuning, notes)


def _create_session(width: int, height: int, qp: int, fps: int, preset: str,
                    tuning: str, notes: list | None, extra: dict | None = None):
    """One PyNvVideoCodec encoder through the kwarg ladder.

    `extra` is merged into the CreateEncoder call on every rung and kept out
    of the rung shown in notes. The pinned path passes its CUDA context and
    stream this way, so both paths share one ladder and one set of rules.
    """
    extra = extra or {}
    # Older/newer builds may not accept every kwarg, so descend a ladder of
    # reduced kwarg sets -- but say so: a reduced set silently changes rate
    # control (constqp -> driver default), i.e. different output quality than
    # the profile asked for.
    #
    # EVERY rung carries the GOP, and there is no bare attempt after the ladder.
    # Without an explicit GOP, NVENC's driver default produces ONE IDR for a
    # whole recording, which makes the mp4 unseekable in the LUC3D labeler and
    # unwalkable by ffprobe, and that failure is invisible until someone scrubs
    # a finished video days later. Failing to create an encoder is the better
    # outcome: the callers fall back to raw frames, which encode later with a
    # known GOP.
    #
    # RULE: the keys are lowercase `gop` and `idrperiod`, and the only proof
    # they work is the bitstream -- see gop_is_honoured(). REASON:
    # PyNvVideoCodec accepts unknown keyword arguments SILENTLY. `gopLength`
    # and `idrPeriod`, the names this ladder carried before, were dropped on
    # the floor: measured, they produce output byte-identical to passing no GOP
    # at all, so every real-time recording had a single IDR while the code, the
    # tests and the docs all agreed the GOP was explicit. A test that asserts
    # on keyword NAMES cannot catch this; only counting IDRs in the output can.
    gop = str(fps)
    _gop_kw = dict(gop=gop, idrperiod=gop)
    ladder = (
        dict(codec="h264", preset=preset, tuning_info=tuning, rc="constqp",
             qp=str(qp), **_gop_kw),
        dict(codec="h264", preset=preset, rc="constqp", qp=str(qp), **_gop_kw),
        dict(codec="h264", preset=preset, tuning_info=tuning, **_gop_kw),
        dict(codec="h264", **_gop_kw),
    )
    last_err = None
    for n, kw in enumerate(ladder):
        try:
            enc = _nvc.CreateEncoder(width, height, "NV12", True, **kw, **extra)
        except Exception as e:
            code = _nvenc_status(e)
            if code in _NVENC_FATAL:
                # Not a config problem: retrying a reduced config cannot fix it,
                # and if a slot frees mid-ladder a later rung would silently
                # succeed with a degraded encoder. Give the GC one chance to
                # reap an encoder that is unreferenced but not yet finalized,
                # then retry THIS rung (the kwarg set the build had accepted so
                # far, not the full one it may already have rejected) and fail
                # loudly if that does not take.
                gc.collect()
                try:
                    enc = _nvc.CreateEncoder(width, height, "NV12", True, **kw,
                                             **extra)
                except Exception as e2:
                    why = (f"NVENC unavailable: {_NVENC_FATAL[code]} "
                           f"(NVENCSTATUS {code}). Concurrent encode sessions "
                           f"are capped by the driver; the budget is one per "
                           f"camera plus encode_parallel plus the warm-up "
                           f"session. Original: {e2}")
                    if n > 0:
                        # Both causes are visible: the kwarg rejection that
                        # moved the ladder off rung 0, and the fatal code.
                        why += f" (rung 0 had been rejected with: {last_err})"
                    raise NvencUnavailable(why, code) from e2
            else:
                last_err = e
                continue
        if n > 0:
            note = (f"NVENC full encoder config rejected ({last_err}); created "
                    f"with reduced settings {kw} -- quality may differ from "
                    f"profile qp={qp}. The GOP is still explicit "
                    f"(gop={gop}).")
            print(f"[nvenc] WARNING: {note}", flush=True)
            if notes is not None:
                notes.append(note)
        return enc
    raise RuntimeError(
        f"NVENC encoder creation failed with every accepted kwarg set: "
        f"{last_err}") from last_err


# ----------------------------------------------------------------- upload path
#
# PyNvVideoCodec's Encode(frame) copies a pageable host frame to the GPU with
# the GIL held: the driver first copies it into a staging buffer of its own,
# inside the call. At one Encode per camera per frame period that is most of
# the GIL's busy time, and a grab thread waiting for the GIL falls behind its
# camera. The pinned path moves that copy out from under the GIL (see
# PinnedUploadEncoder). The default is 'host'; the profile opts in to 'pinned'.

#: Values `configure_upload()` accepts. The profile fields `nvenc_upload` and
#: `nvenc_context` carry the same values.
UPLOAD_MODES = ("host", "pinned")
CONTEXT_MODES = ("shared", "own")

#: Page-locked staging buffers per pinned encoder, used round robin. A buffer
#: is rewritten only once the upload that last read it has completed, so this
#: bounds the uploads in flight per encoder. An upload finishes well within a
#: frame period, so with 4 the wait for a free buffer rarely blocks.
PINNED_STAGING_BUFFERS = 4

_upload_lock = threading.Lock()
_upload_cfg = {"upload": "host", "context": "shared"}

_stats_lock = threading.Lock()
_stats = {"pinned_encoders": 0, "pinned_bytes": 0, "own_contexts": 0,
          "shared_context_retained": False, "host_fallbacks": 0,
          "pinned_disabled": ""}

_shared_ctx = 0
_shared_ctx_lock = threading.Lock()

_pinned_warm_done = False
_pinned_warm_lock = threading.Lock()


def configure_upload(upload: str = "host", context: str = "shared") -> dict:
    """Choose how every encoder created after this call receives its frames.

    upload='host' (the default) hands PyNvVideoCodec the caller's frame, as
    it always has. upload='pinned' builds a PinnedUploadEncoder: the frame is
    copied into page-locked memory outside the GIL and uploaded from there.
    context applies to 'pinned' only: 'shared' runs every encoder in the
    device's primary context (no extra GPU memory), 'own' gives each encoder
    a CUDA context of its own. An own context costs GPU memory (a few hundred
    MiB each), and encoders in separate contexts share no driver locks.

    Encoders that already exist keep the path they were built with. A pinned
    path whose first encode failed stays off for the process, whatever this
    is set to (upload_stats()["pinned_disabled"] says why). Returns the
    previous setting, so a caller can restore it. Raises ValueError for a
    value outside UPLOAD_MODES or CONTEXT_MODES.
    """
    if upload not in UPLOAD_MODES:
        raise ValueError(f"nvenc upload must be one of {UPLOAD_MODES}, "
                         f"got {upload!r}")
    if context not in CONTEXT_MODES:
        raise ValueError(f"nvenc context must be one of {CONTEXT_MODES}, "
                         f"got {context!r}")
    with _upload_lock:
        previous = dict(_upload_cfg)
        _upload_cfg["upload"] = upload
        _upload_cfg["context"] = context
    if previous != {"upload": upload, "context": context}:
        if upload == "pinned":
            where = ("one CUDA context per encoder" if context == "own"
                     else "the shared primary CUDA context")
            print(f"[nvenc] upload: pinned staging, {where}", flush=True)
        else:
            print(f"[nvenc] upload: host (PyNvVideoCodec copies each frame "
                  f"with the GIL held)", flush=True)
    return previous


def upload_config() -> dict:
    """The current setting, as {"upload": ..., "context": ...}."""
    with _upload_lock:
        return dict(_upload_cfg)


def _snapshot_upload() -> tuple[str, str]:
    with _upload_lock:
        return _upload_cfg["upload"], _upload_cfg["context"]


def upload_stats() -> dict:
    """Pinned-path resources alive in this process.

    pinned_encoders, pinned_bytes and own_contexts count what is alive now,
    and return to 0 once every pinned encoder is closed.
    shared_context_retained says the primary context is held (once per
    process). host_fallbacks counts pinned encoders that fell back to the
    host upload since the process started. pinned_disabled is empty, or the
    reason the pinned path is off for the rest of the process: its first
    encode failed, so every later pinned encoder gets the host upload.
    """
    with _stats_lock:
        return dict(_stats)


def _stat(key: str, delta: int) -> dict:
    with _stats_lock:
        _stats[key] += delta
        return dict(_stats)


def _mib(nbytes: int) -> str:
    return f"{nbytes / 2**20:.1f} MiB"


def _shared_context(drv) -> int:
    """The device's primary context, retained once for the process's life.

    RULE: retained once and never released. REASON: releasing the last retain
    destroys the primary context, and the next acquisition would rebuild it
    at every start. One retain per process is bounded, so it is not a leak
    across acquisitions.
    """
    global _shared_ctx
    with _shared_ctx_lock:
        if not _shared_ctx:
            _shared_ctx = drv.primary_ctx_retain(0)
            with _stats_lock:
                _stats["shared_context_retained"] = True
        return _shared_ctx


class _PinnedSetupError(RuntimeError):
    """The pinned path could not be set up for one encoder; the host upload
    is used for it instead."""


class PinnedUploadEncoder:
    """An NVENC encoder fed from page-locked (pinned) staging buffers.

    Encode(nv12) copies the frame into one of PINNED_STAGING_BUFFERS
    page-locked buffers with numpy, which releases the GIL for the copy, and
    hands that buffer to PyNvVideoCodec. From page-locked memory the upload is
    a DMA queued on this encoder's stream, so Encode holds the GIL only for
    its own bookkeeping. The bitstream is byte-identical to the host path's.

    RULE: a staging buffer is rewritten only after the CUDA event recorded
    behind its last Encode has completed. REASON: Encode can return before the
    DMA has finished, so rewriting the buffer at once races the upload and
    the encoded picture no longer matches the frame. The event covers the DMA
    only because PyNvVideoCodec queues it on this encoder's stream, which
    pinned_upload_matches_host() proves at launch.

    RULE: never synchronize the stream from the host. REASON: the stream also
    carries NVENC's own work, so a stream synchronize drains the encoder's
    pipeline every frame, and under the context's default scheduling the wait
    spins a core. A per-buffer event waits for the one upload that matters,
    and a blocking-sync event parks the thread instead of spinning.

    RULE: every driver call is bracketed by a push and a pop of this
    encoder's context. REASON: the encoder is created on one thread, fed on
    another and closed on a third. A context left current on any of them
    outlives the encoder there, and later CUDA work on that thread would land
    in a destroyed context.

    Only the Y plane is copied per frame. The capture path's UV plane is a
    constant 128 (encoders.EncoderProtocol), written into every staging
    buffer once. The first frame's UV plane is checked; when it is not a
    constant 128, this encoder copies whole frames from then on.

    Close() frees the NVENC session, then the buffers, events, stream and (for
    context 'own') the context. The destructor calls Close().
    """

    upload = "pinned"

    def __init__(self, width: int, height: int, context: str, drv,
                 buffers: int | None = None):
        self.width, self.height = int(width), int(height)
        self.context = context
        self._drv = drv
        self._ysize = self.width * self.height
        self._fsize = self._ysize * 3 // 2
        self._nbuf = int(buffers or PINNED_STAGING_BUFFERS)
        self._session = None
        self._ctx = 0
        self._own_ctx = False
        self._stream = 0
        self._events: list[int] = []
        self._ptrs: list[int] = []
        self._frames: list = []     # (height * 3 // 2, width) views, to Encode
        self._whole: list = []      # flat views of each whole buffer
        self._ydst: list = []       # flat views of each buffer's Y plane
        self._n = 0
        self._full_copy = False
        #: Encodes that found their staging buffer's upload still running and
        #: waited for it (blocking, with the GIL released).
        self.event_waits = 0
        self._closed = False
        self._close_lock = threading.Lock()
        #: No release line in the log (the one-time warm-up encoder).
        self._quiet = False
        self._allocate()

    def _allocate(self) -> None:
        drv = self._drv
        stage = "creating a CUDA context"
        try:
            if self.context == "own":
                self._ctx = drv.ctx_create(0)
                self._own_ctx = True
                _stat("own_contexts", 1)
            else:
                self._ctx = _shared_context(drv)
            stage = "creating the CUDA stream and events"
            drv.ctx_push(self._ctx)
            try:
                self._stream = drv.stream_create()
                for _ in range(self._nbuf):
                    self._events.append(drv.event_create())
                stage = (f"page-locking {self._nbuf} staging buffers of "
                         f"{self._fsize} bytes")
                for _ in range(self._nbuf):
                    p = drv.host_alloc(self._fsize)
                    self._ptrs.append(p)
                    _stat("pinned_bytes", self._fsize)
                    whole = cuda_driver.host_view(p, self._fsize)
                    whole[self._ysize:] = 128
                    self._whole.append(whole)
                    self._ydst.append(whole[:self._ysize])
                    self._frames.append(
                        whole.reshape(self.height * 3 // 2, self.width))
            finally:
                drv.ctx_pop()
        except Exception as e:
            self._free_resources()
            raise _PinnedSetupError(f"{stage} failed: {e}") from e

    def _attach(self, session) -> None:
        self._session = session
        _stat("pinned_encoders", 1)

    def Encode(self, nv12):
        session = self._session
        if session is None:
            raise RuntimeError("PinnedUploadEncoder used after Close()")
        if nv12.dtype != np.uint8 or nv12.size != self._fsize:
            raise ValueError(
                f"expected a uint8 NV12 frame of {self._fsize} bytes "
                f"({self.width}x{self.height}), got {nv12.dtype} "
                f"{nv12.shape}")
        flat = nv12.reshape(-1) if nv12.flags.c_contiguous else np.ravel(nv12)
        n = self._n
        j = n % self._nbuf
        drv = self._drv
        drv.ctx_push(self._ctx)
        try:
            ev = self._events[j]
            if n >= self._nbuf and not drv.event_query(ev):
                self.event_waits += 1
                drv.event_sync(ev)
            if n == 0:
                self._check_uv(flat)
            if self._full_copy:
                np.copyto(self._whole[j], flat)
            else:
                np.copyto(self._ydst[j], flat[:self._ysize])
            bs = session.Encode(self._frames[j])
            drv.event_record(ev, self._stream)
        finally:
            drv.ctx_pop()
        self._n = n + 1
        return bs

    def _check_uv(self, flat) -> None:
        uv = flat[self._ysize:]
        if int(uv.min()) != 128 or int(uv.max()) != 128:
            self._full_copy = True
            print(f"[nvenc] pinned upload: the first frame's UV plane is not "
                  f"a constant 128, so this {self.width}x{self.height} "
                  f"encoder copies whole frames", flush=True)

    def EndEncode(self):
        session = self._session
        if session is None:
            return b""
        self._drv.ctx_push(self._ctx)
        try:
            return session.EndEncode()
        finally:
            self._drv.ctx_pop()

    def Close(self) -> None:
        """Free the NVENC session, then every CUDA resource. Idempotent."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        session, self._session = self._session, None
        if session is not None:
            pushed = False
            try:
                self._drv.ctx_push(self._ctx)
                pushed = True
            except Exception:
                pass
            try:
                # The session is freed by the encoder object's destructor,
                # never by EndEncode(), so the last reference goes here.
                del session
                gc.collect()
            finally:
                if pushed:
                    try:
                        self._drv.ctx_pop()
                    except Exception:
                        pass
            left = _stat("pinned_encoders", -1)
        else:
            left = None
        self._free_resources()
        if left is not None and left["pinned_encoders"] == 0 and not self._quiet:
            now = upload_stats()
            if now["pinned_bytes"] or now["own_contexts"]:
                print(f"[nvenc] WARNING: every pinned encoder is closed but "
                      f"{_mib(now['pinned_bytes'])} of staging memory and "
                      f"{now['own_contexts']} CUDA context(s) are still held",
                      flush=True)
            else:
                print("[nvenc] pinned upload: every staging buffer and CUDA "
                      "context released", flush=True)

    def _free_resources(self) -> None:
        drv = self._drv
        pushed = False
        if self._ctx:
            try:
                drv.ctx_push(self._ctx)
                pushed = True
            except Exception as e:
                print(f"[nvenc] WARNING: pinned upload teardown could not "
                      f"make its CUDA context current: {e}", flush=True)
        try:
            # Every upload out of the staging buffers has finished before any
            # of them is freed. An event never recorded counts as complete.
            for ev in self._events:
                try:
                    drv.event_sync(ev)
                except Exception:
                    pass
            for ev in self._events:
                try:
                    drv.event_destroy(ev)
                except Exception:
                    pass
            self._events = []
            self._frames, self._whole, self._ydst = [], [], []
            for p in self._ptrs:
                try:
                    drv.host_free(p)
                    _stat("pinned_bytes", -self._fsize)
                except Exception as e:
                    print(f"[nvenc] WARNING: a pinned staging buffer could "
                          f"not be freed: {e}", flush=True)
            self._ptrs = []
            if self._stream:
                try:
                    drv.stream_destroy(self._stream)
                except Exception:
                    pass
                self._stream = 0
        finally:
            if pushed:
                try:
                    drv.ctx_pop()
                except Exception:
                    pass
        if self._own_ctx and self._ctx:
            try:
                drv.ctx_destroy(self._ctx)
                _stat("own_contexts", -1)
            except Exception as e:
                print(f"[nvenc] WARNING: a CUDA context could not be "
                      f"destroyed: {e}", flush=True)
        self._own_ctx = False
        self._ctx = 0

    def __del__(self):
        try:
            self.Close()
        except Exception:
            pass


def _build_pinned(width, height, qp, fps, preset, tuning, notes, context,
                  log: bool = True,
                  buffers: int | None = None) -> PinnedUploadEncoder:
    """A PinnedUploadEncoder, or _PinnedSetupError when the pinned path cannot
    be set up for it. NvencUnavailable other than out of memory propagates:
    the host path would fail the same way."""
    try:
        drv = cuda_driver.get()
    except cuda_driver.CudaUnavailable as e:
        raise _PinnedSetupError(str(e)) from e
    enc = PinnedUploadEncoder(width, height, context, drv, buffers)
    try:
        drv.ctx_push(enc._ctx)
        try:
            session = _create_session(
                width, height, qp, fps, preset, tuning, notes,
                extra={"cudacontext": enc._ctx, "cudastream": enc._stream})
        finally:
            drv.ctx_pop()
    except NvencUnavailable as e:
        enc.Close()
        if e.status == 10:
            # An own context holds GPU memory of its own, so the host path,
            # which needs none, can still fit.
            raise _PinnedSetupError(
                f"NVENC ran out of GPU memory with the pinned setup: {e}") from e
        raise
    except Exception as e:
        enc.Close()
        raise _PinnedSetupError(
            f"NVENC refused the pinned setup: {e}") from e
    enc._attach(session)
    enc._quiet = not log
    if log:
        s = upload_stats()
        where = ("its own CUDA context" if context == "own"
                 else "the shared primary CUDA context")
        print(f"[nvenc] pinned upload: {width}x{height} encoder, "
              f"{enc._nbuf} page-locked staging buffers "
              f"({_mib(enc._nbuf * enc._fsize)}), {where}. "
              f"Process total: {s['pinned_encoders']} pinned encoder(s), "
              f"{_mib(s['pinned_bytes'])} page-locked, "
              f"{s['own_contexts']} own context(s)", flush=True)
    return enc


def _warm_pinned(context: str) -> None:
    """The pinned path's first Encode, once per process and single-threaded.

    Same hazard as _warm(): concurrent first Encodes can wedge the process in
    the import machinery. In the decoupled mode every grab thread creates its
    encoder at once, so the first caller warms the path under this lock while
    the rest wait. A setup failure is logged and not retried; each real
    encoder then tries its own setup and falls back on its own.

    RULE: a pinned encoder that is built but fails to encode or to end its
    stream turns the pinned path off for the process. REASON: the library
    refuses page-locked input, so every real pinned encoder would fail on
    its first frame, and each camera would spill raw frames for the whole
    recording. With the path off, every encoder gets the host upload and a
    note, and the video is unchanged.
    """
    global _pinned_warm_done
    if _pinned_warm_done:
        return
    with _pinned_warm_lock:
        if _pinned_warm_done:
            return
        enc = None
        try:
            # 256x256 for the reason given in _warm().
            enc = _build_pinned(256, 256, 21, 100, "P3", "low_latency", [],
                                context, log=False)
        except Exception as e:
            print(f"[nvenc] pinned upload warmup skipped: {e}", flush=True)
        try:
            if enc is not None:
                try:
                    enc.Encode(np.full(256 * 384, 128, np.uint8))
                    enc.EndEncode()
                except Exception as e:
                    why = f"a pinned encoder failed its first encode: {e}"
                    with _stats_lock:
                        _stats["pinned_disabled"] = why
                    print(f"[nvenc] WARNING: pinned upload disabled for this "
                          f"process; every encoder uses the host upload ({why})",
                          flush=True)
                else:
                    print("[nvenc] pinned upload warmed (first Encode done "
                          "single-threaded)", flush=True)
        finally:
            if enc is not None:
                enc.Close()
                del enc
            # Set last, still under the lock: a caller that sees it set finds
            # the path warm, and a failed warm-up is not retried per encoder.
            _pinned_warm_done = True


def _create_pinned(width, height, qp, fps, preset, tuning, notes, context):
    """A pinned encoder, or the host path for this encoder when the pinned
    setup fails or the pinned path is off for the process.

    RULE: a pinned setup failure never fails the encoder; it falls back to the
    host upload, prints why, and adds a note the recording's WARNINGS.txt
    reports. REASON: the two paths produce the same video. Losing the pinned
    path costs GIL time, and a missing encoder would cost the camera's
    real-time video.
    """
    _warm_pinned(context)
    with _stats_lock:
        reason = _stats["pinned_disabled"]
    if not reason:
        try:
            return _build_pinned(width, height, qp, fps, preset, tuning,
                                 notes, context)
        except _PinnedSetupError as e:
            reason = str(e)
    note = (f"real-time encode uses the host upload, not the configured "
            f"pinned upload ({reason}). The video is the same; each Encode "
            f"holds the GIL longer, so grab threads can fall behind.")
    print(f"[nvenc] WARNING: {note}", flush=True)
    if notes is not None:
        notes.append(note)
    _stat("host_fallbacks", 1)
    return _create_session(width, height, qp, fps, preset, tuning, notes)


#: How long the launch check holds the encoder's stream before each Encode.
#: It must outlast one Encode call plus one frame copy, about 1 ms at
#: 1920x1200. A stall too short makes the check fail, never pass.
_CHECK_STALL_MS = 20


def pinned_upload_matches_host(width: int, height: int,
                               context: str = "shared",
                               frames: int = 16) -> bool | None:
    """Is the pinned upload ordered on the encoder's stream on this machine?

    True: PyNvVideoCodec queues its copy of each staging buffer on the
    stream it is given, behind the work already queued there, and every
    Encode waited for the previous upload before it reused the buffer. The
    staging events then guard every upload. False: that is not shown, or
    the pinned encoder was built but failed to encode; record with the host
    upload. None: nothing could be measured. NVENC is unavailable, the
    session cap is reached, or the pinned setup fails; in the last case
    each pinned encoder falls back to the host upload with a note.

    RULE: run it on every launch that uses the pinned path, at the
    recording's frame size. REASON: the events that guard the staging
    buffers are recorded on the stream PyNvVideoCodec is given, and the
    library does not document that it copies on that stream. A build that
    copies elsewhere races every rewrite of a staging buffer. At normal
    timing that race is rare, so an ordinary encode passes by luck. Stalling
    the stream makes the outcome certain.

    Frame 0 is encoded as it is, because the library's first Encode waits
    for the stream. Before each later Encode, a host function stalls the
    encoder's stream. Encode is handed the inverse of the reference picture,
    and the staging buffer is rewritten with the reference picture while the
    stall still holds the stream. A copy queued on the stream runs after the
    stall and reads the rewrite, so the bitstream equals the host path's
    encode of the reference pictures. A copy queued anywhere else has read
    the inverse by then. The encoder has one staging buffer, so each Encode
    also waits on the previous upload's event; without that wait it would
    overwrite a rewrite before the upload read it. One NVENC session at a
    time; under a second at 1920x1200 with the default 16 frames.
    """
    if context not in CONTEXT_MODES:
        raise ValueError(f"nvenc context must be one of {CONTEXT_MODES}, "
                         f"got {context!r}")
    if frames < 2:
        raise ValueError(f"the check needs at least 2 frames, got {frames}")
    _load()
    if _nvc is None:
        return None
    w, h = int(width), int(height)
    ysize = w * h
    where = f"{w}x{h}, {context} context"
    base = (np.arange(ysize, dtype=np.int64) % 251).astype(np.uint8)
    ref = np.empty(ysize, np.uint8)

    def reference(i):
        np.add(base, np.uint8(7 * i % 256), out=ref)
        return ref

    frame = np.full(ysize * 3 // 2, 128, np.uint8)
    pinned = None
    try:
        host = None
        try:
            host = _create_session(w, h, 21, 100, "P3", "low_latency", None)
            want = []
            for i in range(frames):
                frame[:ysize] = reference(i)
                want.append(bytes(host.Encode(frame)))
            want.append(bytes(host.EndEncode()))
        except Exception as e:
            print(f"[nvenc] pinned upload check could not run ({where}): the "
                  f"host encoder failed: {e}", flush=True)
            return None
        finally:
            del host
            gc.collect()
        try:
            pinned = _build_pinned(w, h, 21, 100, "P3", "low_latency", None,
                                   context, log=False, buffers=1)
        except (_PinnedSetupError, NvencUnavailable) as e:
            print(f"[nvenc] pinned upload check could not run ({where}): {e}",
                  flush=True)
            return None
        try:
            ok, why = _stalled_encode(pinned, frames, frame, reference, want)
        except Exception as e:
            # The pinned encoder exists but cannot encode, so every real
            # pinned encoder would fail on its first frame.
            ok, why = False, f"the pinned encoder failed to encode: {e}"
        if ok:
            print(f"[nvenc] pinned upload check passed ({where}): {why}",
                  flush=True)
        else:
            print(f"[nvenc] WARNING: pinned upload check FAILED ({where}): "
                  f"{why}. Record with the host upload.", flush=True)
        return ok
    finally:
        if pinned is not None:
            pinned.Close()
        gc.collect()


def _stalled_encode(enc, frames, frame, reference, want):
    """The pinned half of pinned_upload_matches_host: (verdict, reason)."""
    drv, ev, ysize = enc._drv, enc._events[0], enc._ysize
    got = []
    frame[:ysize] = reference(0)
    got.append(bytes(enc.Encode(frame)))
    covered = 0
    for i in range(1, frames):
        pic = reference(i)
        np.invert(pic, out=frame[:ysize])
        drv.ctx_push(enc._ctx)
        try:
            drv.stream_stall(enc._stream, _CHECK_STALL_MS)
        finally:
            drv.ctx_pop()
        got.append(bytes(enc.Encode(frame)))
        # Rewritten while the stall holds the stream: a copy queued on the
        # stream has not run yet and reads this.
        np.copyto(enc._ydst[0], pic)
        drv.ctx_push(enc._ctx)
        try:
            covered += not drv.event_query(ev)
        finally:
            drv.ctx_pop()
    got.append(bytes(enc.EndEncode()))
    stalled = frames - 1
    same = b"".join(got) == b"".join(want)
    detail = (f"{stalled} stalled frames, the stall still held the stream "
              f"after {covered} of {stalled} rewrites, {enc.event_waits} "
              f"upload waits")
    if not same:
        return False, (f"the pinned bitstream differs from the host path's. A "
                       f"staging buffer was read before its rewrite, because "
                       f"PyNvVideoCodec does not copy on the encoder's stream, "
                       f"or it was overwritten before its upload, because the "
                       f"event waits did not hold ({detail})")
    if covered != stalled:
        return False, (f"the stall had ended before the rewrite, so the "
                       f"ordering was not shown ({detail})")
    return True, f"the library copies on the encoder's stream ({detail})"


def count_idr(chunks) -> int:
    """IDR pictures in an Annex-B byte stream, by NAL type 5 after a start code."""
    buf = b"".join(bytes(c) for c in chunks if c is not None and len(c))
    n = i = 0
    while True:
        j = buf.find(b"\x00\x00\x01", i)
        if j < 0 or j + 3 >= len(buf):
            return n
        if buf[j + 3] & 0x1F == 5:
            n += 1
        i = j + 3


def gop_is_honoured(fps: int = 100, size: int = 256) -> bool | None:
    """Does this build apply the GOP keywords? None when NVENC is unavailable.

    RULE: verify the bitstream, never the keyword names. REASON: PyNvVideoCodec
    accepts unknown keyword arguments silently, so a build that renames one
    reads as configured while producing a single IDR for the whole recording.
    That is unseekable in the labeler and invisible until someone scrubs a
    finished video, and it is exactly what shipped while the ladder carried
    `gopLength`/`idrPeriod`. Names are unverifiable by construction; IDRs in
    the output are not.

    Encodes two GOPs of a changing picture and asks for at least two IDRs.
    Small and short: tens of milliseconds, cheap enough for a preflight.
    """
    _load()
    if _nvc is None:
        return None
    import numpy as np
    enc = None
    try:
        enc = create_h264_encoder(size, size, 21, fps)
        frame = np.full(size * size * 3 // 2, 128, dtype=np.uint8)
        out = []
        for i in range(2 * fps):
            frame[: size * size] = (i * 7) % 255
            out.append(enc.Encode(frame))
        out.append(enc.EndEncode())
        return count_idr(out) >= 2
    except Exception as e:
        print(f"[nvenc] GOP verification could not run: {e}", flush=True)
        return None
    finally:
        if enc is not None:
            del enc
        gc.collect()


def probe_monochrome_support(codec: str = "h264", gpuid: int = 0) -> int:
    """NV_ENC_CAPS_SUPPORT_MONOCHROME for this GPU: 1, 0, or -1 when unknown.

    The capture path feeds NV12 whose UV plane is a constant 128, which costs
    half a frame of RAM per ring slot and a memcpy the encoder then throws
    away. A GPU whose encoder supports monochrome could take the Y plane
    alone, so the capability is worth knowing before anyone tries.

    RULE: read the capability, never assume it. REASON: it is a per-generation
    hardware fact, and asking for monochrome where it is unsupported fails at
    session creation — mid-recording, once per camera.
    """
    _load()
    if _nvc is None:
        return -1
    try:
        caps = _nvc.GetEncoderCaps(gpuid=gpuid, codec=codec)
    except Exception as e:
        print(f"[nvenc] encoder caps unavailable: {e}", flush=True)
        return -1
    value = caps.get("support_monochrome") if hasattr(caps, "get") else None
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def probe_max_sessions_isolated(width: int = 1920, height: int = 1200,
                                limit: int = 24, timeout: float = 120.0) -> int:
    """`probe_max_sessions` in a child process, so release is guaranteed.

    Counting the cap means allocating every session the driver will grant, and
    the caller needs most of them back moments later. `EndEncode()` does not
    free a session -- only the encoder's destructor does -- so in-process the
    release always races the next allocation, no matter how carefully the
    references are dropped. An explicit pop-and-delete plus a collect is not
    enough -- the race has hung two 600 s GUI recordings at exactly the
    `[hw] NVENC sessions:` line, before start_triggers, while shorter runs on
    the same build passed.

    Process exit frees GPU sessions unconditionally, which turns that race into
    a guarantee. The cost is one interpreter start, paid once per GUI session
    because hardware_check caches the answer.

    Returns -1 if the child CANNOT BE RUN AT ALL, so the caller can fall back
    to the in-process probe rather than refusing to record. A timeout is not
    that case and raises `TimeoutError` instead -- see below.
    """
    import subprocess
    import sys

    code = (
        "from gui_app import nvenc;"
        f"print('NVENC_SESSIONS=%d' % nvenc.probe_max_sessions({width},"
        f" {height}, limit={limit}))"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
    except subprocess.TimeoutExpired as exc:
        # RULE: a timeout raises; only a child that could not START returns
        # -1. REASON: -1 is the caller's signal to repeat the count IN THE GUI
        # PROCESS, and repeating it is exactly wrong here. A child that spent
        # the whole timeout without producing a count was not merely slow to
        # launch -- the driver is busy or wedged -- and the in-process probe
        # would allocate the same sessions with the release race this function
        # exists to remove, hanging the UI thread at the `[hw] NVENC sessions:`
        # line. Distinguishing the two is the whole value of the signal.
        raise TimeoutError(
            f"NVENC session probe did not finish within {timeout:g} s; the "
            f"driver is busy or wedged. Not repeating the count in this "
            f"process: it would allocate the same sessions here.") from exc
    except Exception as exc:
        print(f"[nvenc] isolated probe could not run ({exc}); "
              f"falling back in-process", flush=True)
        return -1
    for line in (proc.stdout or "").splitlines():
        if line.startswith("NVENC_SESSIONS="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                break
    tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
    print(f"[nvenc] isolated probe gave no count ({tail[0]}); "
          f"falling back in-process", flush=True)
    return -1


def probe_max_sessions(width: int = 1920, height: int = 1200, limit: int = 24) -> int:
    """How many concurrent NVENC sessions this driver/GPU will actually grant.

    For a preflight check: at 9 cameras the pipeline needs n_cams encode sessions
    plus encode_parallel for the post-hoc/remux path. The cap has moved across
    driver generations (2 -> 3 -> 5 -> 8 -> 12), so it must be PROBED, never
    hardcoded. Sessions are released before returning.

    Releasing them is the whole difficulty, and it is the same trap `_warm()`
    documents: **`EndEncode()` ends the bitstream but does NOT free the
    session** — the encoder object's destructor does. Ending the streams and
    letting the list fall out of scope leaves release at the mercy of refcount
    and GC timing, and this probe holds the most sessions of anything in the
    process. At 9 cameras the preflight asks for 11 of a 12-session cap and the
    router then wants 9 more immediately, so any that linger put the request at
    20 against 12, an intermittent HANG at recording start right after the
    `[hw] NVENC sessions:` line — intermittent precisely because it depends on
    when the collector runs. So drop every reference
    explicitly and collect before returning.
    """
    _load()
    if _nvc is None:
        return 0
    encs = []
    try:
        for _ in range(limit):
            try:
                encs.append(_nvc.CreateEncoder(width, height, "NV12", True,
                                               codec="h264"))
            except Exception as e:
                # RULE: only NVENCSTATUS 21 (max concurrent sessions) and 10
                # (out of memory) end the count; every other failure is
                # re-raised, including an unparseable one. REASON: a bare
                # break turns a configuration or driver error on the FIRST
                # allocation -- status 8 invalid param, a cudart problem
                # surfacing lazily, no encode-capable device -- into "the cap
                # is 0", and the preflight then tells the operator to set
                # `realtime_encode: false` and pay 500x the disk for what is
                # not a session-count problem. Raising reaches the caller as
                # "unavailable", which is both true and actionable.
                if _nvenc_status(e) in (21, 10):
                    break
                raise
        return len(encs)
    finally:
        # Pop-and-delete rather than iterate: this drops the last reference to
        # each encoder as we go, instead of leaving them all alive in `encs`
        # until the function returns. The caller creates its real encoders
        # immediately after this, so "eventually" is not good enough.
        while encs:
            e = encs.pop()
            try:
                e.EndEncode()
            except Exception:
                pass
            del e
        gc.collect()
        encs.clear()
        gc.collect()
