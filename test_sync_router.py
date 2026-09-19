"""Headless integration test of SyncEncodeRouter with REAL NVENC encoders and
concurrent submitting threads (no cameras). Verifies the router produces, per
camera, a stream.h264 that decodes to the common frame count, with identical
block IDs across cameras.

Needs an NVIDIA GPU and cv2, so it is a rig test, not one of the offline
suites. It opens one NVENC session per camera: the concurrent-session cap is a
driver property and is measured, never assumed, so a --cameras above it fails
with NVENCSTATUS 21 rather than anything this test did wrong.

RULE: nothing runs at import -- argv parsing, NVENC and the temporary tree all
live inside main(), behind the __main__ guard.
REASON: any collector that imports test modules (pytest imports every
test_*.py it collects) would otherwise consume the HOST's argv and exit with
SystemExit(2) on a flag meant for the collector, before a line of the code
under test runs.

    python test_sync_router.py
    python test_sync_router.py --cameras 9
    python test_sync_router.py --profile 3dpose
"""
import argparse, shutil, tempfile, threading, time, random
from pathlib import Path
import numpy as np, cv2
from gui_app.sync_encode import SyncEncodeRouter
from gui_app import nvenc


def main() -> int:
    ap = argparse.ArgumentParser(description="SyncEncodeRouter smoke test")
    ap.add_argument("--cameras", type=int, default=6)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1200)
    ap.add_argument("--frames", type=int, default=3000,
                    help="triggers to simulate")
    ap.add_argument("--profile", default=None,
                    help="take the camera count and frame size from this rig "
                         "profile, so the test matches what the rig records")
    args = ap.parse_args()

    # The geometry comes from argv or the profile, never from a constant: a
    # test pinned to one rig's six 1920x1200 cameras cannot be run on the other
    # rig, and the camera count is exactly the variable that decides whether
    # the router still aligns.
    if args.profile:
        from gui_app.session_config import RigProfile
        prof = next(RigProfile.load(p) for p in RigProfile.list_profiles()
                    if p.stem == args.profile)
        NCAM = prof.n_cameras or args.cameras
        W, H = prof.frame_width, prof.frame_height
    else:
        NCAM, W, H = args.cameras, args.width, args.height
    N = args.frames
    random.seed(7)
    print(f"config: {NCAM} cameras, {W}x{H}, {N} triggers")

    tmp = Path(tempfile.mkdtemp(prefix="router_test_"))
    raw_paths = []
    for i in range(NCAM):
        d = tmp / f"cam{i+1}"; d.mkdir()
        raw_paths.append(d / "raw.bin")

    # Each camera drops a different ~2% of triggers (independent), cam3 also
    # freezes.
    delivered = []
    for i in range(NCAM):
        ids = [t for t in range(1, N + 1) if random.random() >= 0.02]
        if i == 2:
            ids = [t for t in ids if not (1200 <= t < 1320)]  # 120-frame freeze
        delivered.append(ids)
    common_expected = set(delivered[0])
    for d in delivered[1:]:
        common_expected &= set(d)
    common_expected = sorted(common_expected)
    print(f"simulated: per-cam {[len(d) for d in delivered]}, "
          f"expected common {len(common_expected)}")

    router = SyncEncodeRouter(raw_paths, W, H, 21)
    if not router.available:
        print("RESULT: FAIL -- NVENC unavailable")
        shutil.rmtree(tmp, ignore_errors=True)
        return 1
    router.start()

    def cam_thread(i):
        ring = [np.full((H * 3 // 2, W), 128, np.uint8)
                for _ in range(router.max_lag + 256)]
        ri = 0
        # Pace at ~100 fps (hardware trigger period) so the encoders, which run
        # ~192 fps, stay ahead and queues never fill -- the real capture regime.
        for t in delivered[i]:
            buf = ring[ri]; ri = (ri + 1) % len(ring)
            buf[:H, :] = (t % 256)
            router.submit(i, t, t * 0.01, buf)
            time.sleep(0.01)

    threads = [threading.Thread(target=cam_thread, args=(i,))
               for i in range(NCAM)]
    t0 = time.perf_counter()
    for th in threads: th.start()
    for th in threads: th.join()
    results = router.stop()
    dt = time.perf_counter() - t0
    print(f"router ran in {dt:.1f}s")

    ok = True
    for i in range(NCAM):
        count, ts, bids = results[i]
        h264 = raw_paths[i].parent / "stream.h264"
        # remux-free decode count via cv2 on the elementary stream
        cap = cv2.VideoCapture(str(h264)); nframes = 0
        while cap.read()[0]: nframes += 1
        cap.release()
        cam_ok = (count == len(common_expected) and bids == common_expected
                  and nframes == len(common_expected))
        ok = ok and cam_ok
        print(f"cam{i+1}: meta={count} h264_frames={nframes} "
              f"ids==common={bids == common_expected} "
              f"-> {'OK' if cam_ok else 'FAIL'}")

    ids_identical = all(results[i][2] == results[0][2] for i in range(NCAM))
    print(f"\nall cameras identical block IDs: {ids_identical}")

    # The GOP is only provable from the bitstream: an encoder library that does
    # not recognise a keyword drops it without a word, and the recording then
    # holds one keyframe for its whole length -- unseekable in the labeler, and
    # invisible until someone scrubs a finished video. This is the GPU-side
    # check; the offline suite can only pin the keyword names.
    gop_ok = nvenc.gop_is_honoured()
    print(f"NVENC applies its GOP (measured on the bitstream): {gop_ok}")
    if gop_ok is None:
        print("  (could not measure; not counted for or against)")

    passed = ok and ids_identical and gop_ok is not False
    print("RESULT:", "ALL PASS" if passed else "FAIL")
    shutil.rmtree(tmp, ignore_errors=True)
    # Exit status, not just a printed verdict: a harness or shell loop reads the
    # status, and a failing run that exits 0 is reported as a success.
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
