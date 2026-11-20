"""The live tracker's worker process: finds the mice in one camera's frames.

Panopticon starts this script with the interpreter the profile's
``closed_loop.python`` names, which may be another environment than
Panopticon's own (the one that has the ONNX runtime). So it imports nothing
from gui_app, only the standard library, numpy, and OpenCV or onnxruntime.

Protocol, one JSON object per line:

- argv names the tracker, the shared-memory block that holds the frame and
  the frame's height and width, and for the onnx tracker the model, the
  runtime (ONNX Runtime's CUDA or TensorRT provider) and the precision.
- stdout first says ``{"ready": true, ...}`` once the tracker can answer, or
  ``{"ready": false, "error": ...}`` before exiting.
- Each stdin line ``{"seq": n}`` asks for the frame now in shared memory, and
  stdout answers ``{"seq": n, "det": [[x, y, confidence], ...], "ms": t}``,
  with x and y in full-resolution pixels, or ``{"seq": n, "error": ...}``.
- The process exits when stdin closes.

stdout carries only these lines. Everything else goes to stderr, which the
window copies to its log.
"""
import argparse
import gc
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np


def _say(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _log(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def _below_normal_priority() -> None:
    """Run below normal priority, so a tracking burst never takes a core
    from a grab thread. Windows only; elsewhere the default stays."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        below_normal = 0x00004000
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), below_normal)
    except Exception as e:                     # priority is a courtesy
        _log(f"could not lower the tracker's priority: {e}")


class ThresholdTracker:
    """Dark areas on a bright floor: every connected area darker than
    `threshold` and at least `min_area_px` full-resolution pixels is a
    mouse, at its centroid, with confidence 1.

    Works on a 4x smaller copy, so a frame costs about a millisecond.
    """
    SHRINK = 4

    def __init__(self, threshold: int, min_area_px: int):
        import cv2
        self.cv2 = cv2
        self.threshold = int(threshold)
        self.min_area = max(1, int(min_area_px)) / float(self.SHRINK ** 2)
        self.kernel = np.ones((3, 3), np.uint8)
        self.info = {"tracker": "threshold"}

    def __call__(self, frame: np.ndarray) -> list:
        cv2, s = self.cv2, self.SHRINK
        h, w = frame.shape
        small = cv2.resize(frame, (w // s, h // s), interpolation=cv2.INTER_AREA)
        mask = (small < self.threshold).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.kernel)
        n, _labels, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
        out = []
        for i in range(1, n):
            if stats[i, cv2.CC_STAT_AREA] >= self.min_area:
                cx, cy = cents[i]
                # The centre of a small pixel, in full-resolution pixels.
                out.append([(cx + 0.5) * s - 0.5, (cy + 0.5) * s - 0.5, 1.0])
        return out


def _tensorrt_libs() -> str:
    """Put the TensorRT libraries of this environment (the tensorrt-cu13
    package's tensorrt_libs) where ONNX Runtime's TensorRT provider loads
    them from, and return their folder."""
    try:
        import tensorrt_libs
    except ImportError:
        raise RuntimeError(
            "runtime tensorrt needs TensorRT in closed_loop.python's "
            "environment: the tensorrt-cu13 package at the major version "
            "this onnxruntime-gpu was built against") from None
    libs = os.path.dirname(tensorrt_libs.__file__)
    os.environ["PATH"] = libs + os.pathsep + os.environ.get("PATH", "")
    os.add_dll_directory(libs)
    return libs


def _gpu_name() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader", "-i",
             "0"], capture_output=True, text=True, timeout=10).stdout.strip()
        return out or "gpu"
    except Exception:
        return "gpu"


def engine_folder(root: str, model: str, precision: str, shape) -> Path:
    """The folder of the TensorRT engine for this model, GPU, ONNX Runtime,
    TensorRT, precision and input shape. An engine runs only on the GPU and
    the versions that built it, so any change gets a folder of its own and
    builds afresh, rather than loading a stale engine."""
    import onnxruntime as ort
    import tensorrt
    mid = hashlib.sha256(Path(model).read_bytes()).hexdigest()[:12]
    name = "-".join([_gpu_name().replace(" ", "_"), "ort" + ort.__version__,
                     "trt" + tensorrt.__version__, mid, precision,
                     "x".join(str(int(v)) for v in shape)])
    return Path(root) / name


class OnnxCentroidTracker:
    """A SLEAP centroid model exported to ONNX (sleap-nn export): uint8 frames
    in, ``centroids`` and ``centroid_vals`` out.

    The frame is padded at the bottom and right to a multiple of `pad_to`,
    which keeps every coordinate where it was. Every slot with a positive
    confidence is returned; the window applies min_confidence. A sleap-nn
    0.1 export marks nearly every slot valid, and a 0.3.5 export gives the
    slots at or below its peak threshold a confidence of -1e9 and junk
    coordinates, which the positive test drops.

    ``runtime`` "tensorrt" runs the model through ONNX Runtime's TensorRT
    provider, which builds an engine for this GPU from the .onnx at the first
    start and keeps it in `engine_cache` (a minute at most, then seconds).
    The input's free dimensions are fixed to one padded frame, so the engine
    is built for exactly that shape.
    """

    def __init__(self, model: str, pad_to: int, height: int, width: int,
                 runtime: str = "cuda", precision: str = "fp32",
                 engine_cache: str | None = None):
        if runtime == "tensorrt":
            _tensorrt_libs()
        import onnxruntime as ort
        # Errors only: its warnings about the exported graph repeat at every
        # start, and on Windows its native logger writes them as UTF-16.
        ort.set_default_logger_severity(3)
        preload = getattr(ort, "preload_dlls", None)
        if preload is not None:
            try:
                preload()
            except Exception as e:
                _log(f"onnxruntime.preload_dlls: {e}")
        so = ort.SessionOptions()
        # The model runs on the GPU; two threads cover the copies around it
        # without spreading over the cores the grab threads use.
        so.intra_op_num_threads = 2
        so.inter_op_num_threads = 1
        so.log_severity_level = 3
        p = max(1, int(pad_to))
        shape = (1, 1, -(-height // p) * p, -(-width // p) * p)
        cache = None
        if runtime == "tensorrt":
            for name, size in (("batch", shape[0]), ("height", shape[2]),
                               ("width", shape[3])):
                so.add_free_dimension_override_by_name(name, size)
            cache = engine_folder(engine_cache, model, precision, shape)
            cache.mkdir(parents=True, exist_ok=True)
            want = [("TensorrtExecutionProvider", {
                "trt_fp16_enable": precision == "fp16",
                "trt_engine_cache_enable": True,
                "trt_engine_cache_path": str(cache),
                "trt_timing_cache_enable": True,
                "trt_timing_cache_path": str(cache)}),
                "CUDAExecutionProvider"]
        else:
            want = [p for p in ("CUDAExecutionProvider",
                                "CPUExecutionProvider")
                    if p in ort.get_available_providers()]
        self.sess = ort.InferenceSession(model, so, providers=want)
        if (runtime == "tensorrt" and self.sess.get_providers()[0]
                != "TensorrtExecutionProvider"):
            # The profile asked for TensorRT; a silent CUDA fallback would
            # run three times slower than the operator was told.
            raise RuntimeError(
                f"ONNX Runtime did not load its TensorRT provider (it offers "
                f"{self.sess.get_providers()}); check that TensorRT's "
                f"version matches this onnxruntime-gpu")
        inp = self.sess.get_inputs()[0]
        self.input_name = inp.name
        channels = inp.shape[1]
        if isinstance(channels, int) and channels != 1:
            raise ValueError(
                f"{model} takes {channels} channels per frame; the live "
                f"tracker feeds the camera's one gray channel, so the model "
                f"must be trained with in_channels 1")
        names = {o.name for o in self.sess.get_outputs()}
        missing = {"centroids", "centroid_vals"} - names
        if missing:
            raise ValueError(f"{model} has no output {sorted(missing)}; "
                             f"expected a sleap-nn centroid export")
        self.padded = np.zeros(shape, np.uint8)
        self.info = {"tracker": "onnx", "model": model,
                     "runtime": runtime, "precision": precision,
                     "providers": self.sess.get_providers(),
                     "input": list(self.padded.shape)}
        if cache is not None:
            self.info["engine_cache"] = str(cache)

    def __call__(self, frame: np.ndarray) -> list:
        h, w = frame.shape
        self.padded[0, 0, :h, :w] = frame
        cents, vals = self.sess.run(["centroids", "centroid_vals"],
                                    {self.input_name: self.padded})
        out = []
        for (x, y), v in zip(cents[0], vals[0]):
            if np.isfinite(x) and np.isfinite(y) and np.isfinite(v) and v > 0:
                out.append([float(x), float(y), float(v)])
        return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tracker", choices=("onnx", "threshold"), required=True)
    ap.add_argument("--shm", required=True)
    ap.add_argument("--height", type=int, required=True)
    ap.add_argument("--width", type=int, required=True)
    ap.add_argument("--model")
    ap.add_argument("--pad-to", type=int, default=32)
    ap.add_argument("--threshold", type=int)
    ap.add_argument("--min-area-px", type=int)
    ap.add_argument("--runtime", choices=("cuda", "tensorrt"), default="cuda")
    ap.add_argument("--precision", choices=("fp32", "fp16"), default="fp32")
    ap.add_argument("--engine-cache")
    args = ap.parse_args(argv)
    _below_normal_priority()

    from multiprocessing import shared_memory
    try:
        shm = shared_memory.SharedMemory(name=args.shm)
        frame = np.ndarray((args.height, args.width), np.uint8, buffer=shm.buf)
        t0 = time.perf_counter()
        if args.tracker == "onnx":
            track = OnnxCentroidTracker(args.model, args.pad_to,
                                        args.height, args.width,
                                        args.runtime, args.precision,
                                        args.engine_cache)
        else:
            track = ThresholdTracker(args.threshold, args.min_area_px)
        blank = np.full((args.height, args.width), 255, np.uint8)
        for _ in range(3):               # the first runs set up the GPU
            track(blank)
        info = dict(track.info, ready=True,
                    warmup_ms=round((time.perf_counter() - t0) * 1e3, 1))
        # Everything made so far lives as long as the worker. Freezing it
        # keeps a full garbage collection from walking it in the middle of
        # tracking.
        gc.collect()
        gc.freeze()
    except Exception as e:
        _say({"ready": False, "error": f"{type(e).__name__}: {e}"})
        return 1
    _say(info)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            seq = json.loads(line).get("seq")
        except ValueError:
            _log(f"ignored a request that is not JSON: {line[:80]!r}")
            continue
        t = time.perf_counter()
        try:
            det = track(frame)
        except Exception as e:
            _say({"seq": seq, "error": f"{type(e).__name__}: {e}"})
            continue
        _say({"seq": seq, "det": det,
              "ms": round((time.perf_counter() - t) * 1e3, 2)})
    del frame
    shm.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
