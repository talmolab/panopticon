"""The live tracker's worker process: finds the mice in one camera's frames.

Panopticon starts this script with the interpreter the profile's
``closed_loop.python`` names, which may be another environment than
Panopticon's own (the one that has the ONNX runtime). So it imports nothing
from gui_app, only the standard library, numpy, and OpenCV or onnxruntime.

Protocol, one JSON object per line:

- argv names the tracker, the shared-memory block that holds the frame and
  the frame's height and width.
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
import json
import sys
import time

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


class OnnxCentroidTracker:
    """A SLEAP centroid model exported to ONNX (sleap-nn export): uint8 frames
    in, ``centroids`` and ``centroid_vals`` out.

    The frame is padded at the bottom and right to a multiple of `pad_to`,
    which keeps every coordinate where it was. Every slot is returned with
    its confidence; the window applies min_confidence, because the export
    marks nearly every slot valid.
    """

    def __init__(self, model: str, pad_to: int, height: int, width: int):
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
        want = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider")
                if p in ort.get_available_providers()]
        self.sess = ort.InferenceSession(model, so, providers=want)
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
        p = max(1, int(pad_to))
        self.padded = np.zeros((1, 1, -(-height // p) * p, -(-width // p) * p),
                               np.uint8)
        self.info = {"tracker": "onnx", "model": model,
                     "providers": self.sess.get_providers(),
                     "input": list(self.padded.shape)}

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
    args = ap.parse_args(argv)
    _below_normal_priority()

    from multiprocessing import shared_memory
    try:
        shm = shared_memory.SharedMemory(name=args.shm)
        frame = np.ndarray((args.height, args.width), np.uint8, buffer=shm.buf)
        t0 = time.perf_counter()
        if args.tracker == "onnx":
            track = OnnxCentroidTracker(args.model, args.pad_to,
                                        args.height, args.width)
        else:
            track = ThresholdTracker(args.threshold, args.min_area_px)
        blank = np.full((args.height, args.width), 255, np.uint8)
        for _ in range(3):               # the first runs set up the GPU
            track(blank)
        info = dict(track.info, ready=True,
                    warmup_ms=round((time.perf_counter() - t0) * 1e3, 1))
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
