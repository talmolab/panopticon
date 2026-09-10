"""PyNvVideoCodec loader shim + encoder factory for real-time GPU H.264 encoding.

The package only adds the CUDA runtime to the DLL search path when CUDA_PATH is
set; on this rig the CUDA toolkit isn't installed and we rely on the pip-provided
`nvidia-cuda-runtime-cu12` wheel (cudart64_12.dll — the NVENC-13 build links the
CUDA *12* runtime). So we add that directory ourselves before importing.

Everything is guarded: if PyNvVideoCodec or the runtime is missing, available()
returns False and the caller falls back to raw-to-disk. The GUI never breaks just
because the GPU encode path is unavailable.
"""
import gc
import os
import re
import sysconfig
import threading

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
    machinery and the WHOLE PROCESS wedges — proven 2026-08-11 with a
    faulthandler dump showing one thread parked in importlib.find_spec() under
    Encode() while every grab thread sat idle and the recording produced
    nothing at all. Doing one throwaway encode here, inside the load lock,
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


def create_h264_encoder(width: int, height: int, qp: int,
                        fps: int = 100,
                        preset: str = "P3", tuning: str = "low_latency"):
    """Create an NVENC H.264 encoder for NV12 input (CPU input buffer).

    Mirrors the validated PoC config. Mono frames are fed as NV12 where the Y
    plane is the gray data and the UV plane is a constant 128.
    """
    _load()
    if _nvc is None:
        raise RuntimeError(f"PyNvVideoCodec unavailable: {_load_error}")
    # Tolerate older/newer builds that may not accept every kwarg — but say so:
    # a reduced kwarg set silently changes rate control (constqp -> driver
    # default), i.e. different output quality than the profile asked for.
    #
    # EVERY rung carries gopLength/idrPeriod. No plausible build accepts `rc`/`qp`
    # but rejects the GOP kwargs, and losing them is far worse than losing quality
    # settings: without an explicit GOP, NVENC's driver default produced ONE IDR
    # for a whole 898 s recording (CLAUDE.md), which makes the mp4 unseekable in
    # the LUC3D labeler and unwalkable by ffprobe. That failure is invisible until
    # someone opens the file days later, so the GOP is non-negotiable.
    gop = str(fps)
    _gop_kw = dict(gopLength=gop, idrPeriod=gop)
    last_err = None
    for n, kw in enumerate((
            dict(codec="h264", preset=preset, tuning_info=tuning, rc="constqp",
                 qp=str(qp), **_gop_kw),
            dict(codec="h264", preset=preset, rc="constqp", qp=str(qp), **_gop_kw),
            dict(codec="h264", preset=preset, tuning_info=tuning, **_gop_kw),
            dict(codec="h264", **_gop_kw),
            dict(codec="h264"))):
        try:
            enc = _nvc.CreateEncoder(width, height, "NV12", True, **kw)
            if n > 0:
                lost_gop = "gopLength" not in kw
                print(f"[nvenc] WARNING: full encoder config rejected ({last_err}); "
                      f"created with reduced settings {kw} — quality may differ "
                      f"from profile qp={qp}"
                      + ("  *** AND WITHOUT AN EXPLICIT GOP: this recording may "
                         "have a single IDR and be unseekable in the labeler ***"
                         if lost_gop else ""), flush=True)
            return enc
        except Exception as e:
            code = _nvenc_status(e)
            if code in _NVENC_FATAL:
                # Not a config problem — retrying a reduced config cannot fix it,
                # and if a slot frees mid-ladder we would silently succeed with a
                # degraded encoder. Give the GC one chance to reap an encoder that
                # is unreferenced but not yet finalized, then fail loudly.
                gc.collect()
                try:
                    return _nvc.CreateEncoder(width, height, "NV12", True,
                                              codec="h264", preset=preset,
                                              tuning_info=tuning, rc="constqp",
                                              qp=str(qp), **_gop_kw)
                except Exception as e2:
                    raise RuntimeError(
                        f"NVENC unavailable: {_NVENC_FATAL[code]} "
                        f"(NVENCSTATUS {code}). Concurrent encode sessions are "
                        f"capped by the driver; the budget is one per camera plus "
                        f"encode_parallel plus the warm-up session. Original: {e2}"
                    ) from e2
            last_err = e
            continue
    # Last attempt: surface the real error.
    return _nvc.CreateEncoder(width, height, "NV12", True, codec="h264")


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
    20 against 12. Observed 2026-09-10 as an intermittent HANG at recording
    start, right after the `[hw] NVENC sessions:` line — intermittent precisely
    because it depended on when the collector ran. So drop every reference
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
            except Exception:
                break
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
