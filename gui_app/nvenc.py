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
                        preset: str = "P3", tuning: str = "low_latency",
                        notes: list | None = None):
    """Create an NVENC H.264 encoder for NV12 input (CPU input buffer).

    Mono frames are fed as NV12 where the Y plane is the gray data and the UV
    plane is a constant 128.

    `notes`, when given, receives one human-readable line per way the created
    encoder differs from what the profile asked for (a rung below the full
    config). The caller folds those into the recording's warnings so a
    degraded encode reaches WARNINGS.txt rather than only stdout.

    Raises RuntimeError when no accepted kwarg set can create an encoder. A
    caller that cannot get an encoder falls back to raw frames; it never
    receives an encoder with an unknown GOP.
    """
    _load()
    if _nvc is None:
        raise RuntimeError(f"PyNvVideoCodec unavailable: {_load_error}")
    # Older/newer builds may not accept every kwarg, so descend a ladder of
    # reduced kwarg sets -- but say so: a reduced set silently changes rate
    # control (constqp -> driver default), i.e. different output quality than
    # the profile asked for.
    #
    # EVERY rung carries gopLength/idrPeriod, and there is no bare attempt after
    # the ladder. Without an explicit GOP, NVENC's driver default can produce ONE
    # IDR for a whole recording, which makes the mp4 unseekable in the LUC3D
    # labeler and unwalkable by ffprobe, and that failure is invisible until
    # someone opens the file days later. Failing to create an encoder is the
    # better outcome: the callers fall back to raw frames, which encode later
    # with a known GOP.
    gop = str(fps)
    _gop_kw = dict(gopLength=gop, idrPeriod=gop)
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
            enc = _nvc.CreateEncoder(width, height, "NV12", True, **kw)
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
                    enc = _nvc.CreateEncoder(width, height, "NV12", True, **kw)
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
                    raise RuntimeError(why) from e2
            else:
                last_err = e
                continue
        if n > 0:
            note = (f"NVENC full encoder config rejected ({last_err}); created "
                    f"with reduced settings {kw} -- quality may differ from "
                    f"profile qp={qp}. The GOP is still explicit "
                    f"(gopLength={gop}).")
            print(f"[nvenc] WARNING: {note}", flush=True)
            if notes is not None:
                notes.append(note)
        return enc
    raise RuntimeError(
        f"NVENC encoder creation failed with every accepted kwarg set: "
        f"{last_err}") from last_err


def probe_max_sessions_isolated(width: int = 1920, height: int = 1200,
                                limit: int = 24, timeout: float = 120.0) -> int:
    """`probe_max_sessions` in a child process, so release is guaranteed.

    Counting the cap means allocating every session the driver will grant, and
    the caller needs most of them back moments later. `EndEncode()` does not
    free a session -- only the encoder's destructor does -- so in-process the
    release always races the next allocation, no matter how carefully the
    references are dropped. That race was documented on 2026-09-10, addressed
    with an explicit pop-and-delete plus a collect, and recurred on 2026-09-14:
    two 600 s GUI recordings hung at exactly the `[hw] NVENC sessions:` line,
    before start_triggers, while shorter runs on the same build passed.

    Process exit frees GPU sessions unconditionally, which turns that race into
    a guarantee. The cost is one interpreter start, paid once per GUI session
    because hardware_check caches the answer.

    Returns -1 if the child cannot be run at all, so the caller can fall back
    to the in-process probe rather than refusing to record.
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
