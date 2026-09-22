# CLAUDE.md — Panopticon (3dpose)

Multi-camera synchronized acquisition GUI (PyQt5 + pypylon + NVENC) for the 3dpose
rig: hardware-triggered capture of 3D animal pose, real-time GPU encode, ChArUco
calibration, and an optostim editor. The `gui_app/` codebase is shared with the
**3dface** repo; only the rig profile (`profiles/*.yaml`) differs.

Launch: `uv run gui.py` (or `conda run -n 3dpose python gui.py`).

> **History and the reasons behind every number live in [`docs/HISTORY.md`](docs/HISTORY.md);
> live measurements in [`docs/INTERNALS.md`](docs/INTERNALS.md).** This file states the
> rules in the present tense; when a rule needs a "why", it is one of those two files.

## Conventions

- Run the matching test after touching a module (plain scripts, no pytest; `CONTRIBUTING.md`
  has the full map): `test_stim_compiler.py` after `stim_compiler.py`/`stim_trace.py`,
  `test_serial_handshake.py` after `serial_controller.py` (the guard against recording zero
  frames), `test_frame_sync.py` after `frame_sync.py`, `test_grab_failure.py` after
  `grab_thread.py`, `test_board_coverage.py` after `board_detector.py`,
  `test_sync_router[_offline].py` for the encoder router.
- `.gitignore` anchors `/_*.py` to the repo root — do **not** unanchor it. A bare `_*.py`
  also matches `__init__.py` and silently keeps a package out of every commit, breaking a
  fresh clone. `!**/__init__.py` is a second line of defence; after adding a package, verify
  with `git ls-files "*__init__.py"`.
- Camera vendor code is confined to `gui_app/backends/`. `backends/__init__.py` defines the
  `CameraBackend` / `GrabResultProtocol` contracts and `load_backend(name)`; `basler.py`
  holds every pypylon cold-path call. `camera_manager` and `grab_thread` reach it only via
  `load_backend("basler")`. Do not `import pypylon` outside `backends/` — a non-Basler rig
  must fail in one place with a clear message. Porting means writing one new module.
- `gui_app/` is shared with 3dface, so **no rig-specific numbers live in code** — they live
  in the profile (`profiles/*.yaml`) and reach the code through `session_config`. Do not
  repeat a profile value in a comment or a constant.
- Blocking camera ops (open/close/reconfigure) run off the Qt main thread via
  `gui_app/ui_workers.py` `CallableWorker`, or the window goes "not responding". Quitting
  mid-session abandons and deletes the incomplete data (`_abandon_and_cleanup`).

## Capture invariants

Breaking any of these fails **silently** — an empty or misaligned recording that looks fine
until someone opens it.

- **Never write `result.Array` in the grab loop.** It is a GIL-held 2.3 MB memcpy
  (~0.84 ms/frame/camera). Use `with result.GetArrayZeroCopy() as img:`;
  `np.frombuffer(GetBuffer())` is not a substitute (it copies too).
- **`img` is a VIEW over the driver buffer** — it must not escape the `with` block or outlive
  `result.Release()`. Every consumer copies out; a consumer that stores `img` reads freed
  memory.
- **`result.PaddingX`/`PaddingY` are checked on EVERY frame** (before the `with`); a non-zero
  value retires the camera, because row padding shears every frame. These are grab-result
  fields (always present, and what `GetArray()` reads to build strides) — not the
  `cam.PaddingX` nodemap feature, which is absent on this model.
- **The NV12 ring's `np.full(..., 128, ...)` is load-bearing twice**: it sets the constant
  chroma plane AND pre-faults every page. `np.empty`/`np.zeros` puts a ~0.4 ms first-touch
  fault back on the hot path.
- **No work on the grab loop's critical path** — it does only retrieve → copy → queue →
  release. Inline encode dropped ~28% of frames; encoders drain separately through
  PyNvVideoCodec (`gui_app/nvenc.py`) into `stream.h264`.
- **A camera that cannot start or realign MUST be retired** (`router.retire`). In kick mode
  the coordinator waits for every camera, so one camera that never publishes force-drops
  every trigger for all of them — one dead camera silently yields an empty recording.
- **`blockids.npy` records only frames that were actually persisted.** A `queue.put_nowait`
  means the queue accepted the frame, not that it was encoded; a dead encoder maps frame *i*
  to the wrong trigger. `sync_encode.stop()` reconciles against `encoded + spilled` and
  writes `WARNINGS.txt`.
- **NVENC sessions are capped by the driver.** Probe the cap (`nvenc.probe_max_sessions`),
  never hardcode it. `EndEncode()` does not free a session — the destructor does, so encoders
  must be `del`'d (`_EncoderThread.release_encoder()`). NVENCSTATUS **21 is the session
  limit, not a config error**: never descend a kwarg fallback ladder on it, and keep the GOP
  keys (`gop`/`idrperiod`, lowercase) on every rung.
- **The GOP is proven from the bitstream, never from the keyword names**
  (`nvenc.gop_is_honoured()`, run by the launch preflight). PyNvVideoCodec accepts unknown
  kwargs silently, so a misspelled GOP key (`gopLength`/`idrPeriod`) yields one IDR for a
  whole recording while the code reads as correct. The default path stream-copies
  `stream.h264`, so `-g` on the ffmpeg writers cannot cover this.
- **Every mp4 the rig writes needs `-g <fps>` AND `-movflags +faststart`**, so recordings
  load and seek in the browser labeler (LUC3D): without `-g` the GOP can be one IDR per file
  (unseekable); without `+faststart` moov-at-end forces a full-file read per camera before
  frame 1. Applies to the mp4 writers (`encode_worker._cmd` and
  `alignment.extract_aligned`), not to the Annex-B `.h264` streams the remux later wraps.
- **BlockID == trigger ordinal is an AXIOM, guarded because one failure mode falsifies it
  silently.** A BlockID counts frames a camera *acquired*, not triggers *fired*: a camera
  over the exposure ceiling ignores the next pulse, consumes no ID, and from then on its
  block ID N is trigger N+k — with **no gap in `blockids.npy`**, so it presents as a perfect
  recording that drifts in time. The guard: the device timestamp is a free-running hardware
  clock, so block IDs must advance at the trigger rate (`frame_sync.check_block_id_rate()`,
  from `sync_encode.stop()` and from `align_recording()` *before* its `needs_alignment` early
  return). `BLOCK_RATE_TOL = 0.003` is measured — do not widen it to 1% (see HISTORY.md). All
  cameras off by the same amount is *not* this bug; it means the reference (`frame_rate` or
  the timestamp unit) is wrong.
- **Cameras drop frames independently, so frame `i` is not the same trigger across cameras.**
  Every frame carries its GigE BlockID in `blockids.npy`, and the videos are trigger-aligned
  two ways: real-time kick-out (default, `realtime_kick: true`) releases a trigger only once
  every camera caught it; post-hoc alignment (`realtime_kick: false`, `gui_app/alignment.py`,
  CLI `2_align.py`) re-encodes down to the common frames afterward. `_unwrap_blockids`
  handles the 16-bit wrap at 65535 (~11 min).
- **Stall recovery.** A GigE stall must not end the session: `grab_thread` re-arms after 25
  consecutive timeouts (up to 5×). `StartGrabbing` restarts the block-ID counter, so
  `_resync_offset()` recovers the true ordinal from the device timestamp and **refuses if the
  gap is not within 0.25 of a period** — a guessed ordinal is worse than losing the camera.
  If it cannot realign, `retire()` keeps the survivors aligned.

## Exposure and the limiter

- **Exposure/gain live in the `.pfs`; the exposure ceiling is ~3.9 ms at 100 fps.** In
  trigger mode the frame-rate timer starts after exposure ends, so the interval is
  `exposure + 1/AcquisitionFrameRate`. `_set_trigger_mode()` hardcodes 165 (1/165 = 6.06 ms)
  for both rigs, so exposure past ~3.94 ms pushes the interval over the 10 ms trigger period
  and every second trigger is skipped (the 50 fps bug).
- **The `.pfs` is the source of exposure/gain, but the code also writes it.**
  `camera_manager.apply_exposure_gain()` runs on every acquisition start: with
  `exposure_us=None` (recording) it re-applies the pfs baseline captured at open, so a
  calibration exposure cannot leak in; with a value (calibration) it applies that. Either way
  it **clamps to `(1e6/fps - 1e6/limit) * 0.9`** and logs `CLAMPED from ...`, so read the
  `[cam1] exposure=...` line, don't assume the pfs.
- Recording exposure/gain are **3000 µs / 6 dB**. Lower values crush the histogram; ~7× total
  clips ~13%. Prefer more IR illumination over gain, then exposure, then gain. Verify any
  change against a **recording, not the preview** (preview is free-run at 30 fps, 33 ms
  headroom, so an over-long exposure only shows once triggered).
- **`trigger_rate_limit` stays at 165, never 0.** The limiter paces each camera's readout
  across 6.06 ms; without it every camera bursts after the shared trigger and marginal links
  drop 8-15% of frames in transmission. The exposure ceiling is therefore real and not worth
  buying out until the network margin is fixed.

## Calibration

- **`board_legacy: true`** in the board config: the physical 3dpose board uses the
  pre-OpenCV-4.6 ChArUco layout, and without `setLegacyPattern(True)` the ≥4.7
  `CharucoDetector` returns 0 corners silently. Both call sites go through
  `apply_legacy_pattern()`, which raises rather than skip. Defaults false for other boards.
- **`opencv-contrib-python>=4.7` is pinned in `pyproject.toml`** (never a PEP 723 header on
  `1_calibrate.py`): OpenCV moved `chessboardCorners` to `getChessboardCorners()` across the
  4.6/4.7 line, and the solve must run in the project env so an offline rig never needs a
  second resolve.
- **The HUD counts markers, not ChArUco corners** (`len(ids) >= 4`, matching the
  `1_calibrate.py` prescan). Corner-counting is stricter than calibration eligibility and
  starves oblique cameras.
- **READY requires three conditions**: `min_per_cam_shared` co-detection ticks per camera, a
  **single connected coverage graph** (`min_edge` co-detections make a pair an edge — one
  connected component, NOT all 15 pairs, because the board is one-sided so opposed cameras
  can never co-detect), and `MIN_GRID_CELLS` of the 2×2 FOV grid covered per camera. READY
  thresholds count **frames**, never the partner-weighted display number, and are sized to
  the solver's caps (~2× the intrinsics/stereo caps, no more).
- **The solve keeps the LARGEST connected component.** A calibration can read satisfied on
  every per-camera figure and still solve only a subset, because the graph is several
  components; the HUD's `groups N/1` is what surfaces that.
- **For the best calibration, solve from full videos of all cameras** (sleap-anipose); the
  GUI "Solve" / `1_calibrate.py` subsample path degrades the result.

## Optostim

Bonsai-style node editor (sidebar → **Stimulation**) that compiles a block graph into the
Arduino sketch: `gui_app/widgets/stimulation_window.py` (canvas + UI),
`gui_app/stim_compiler.py` (graph → `.ino`, pure functions, no Qt).

- **The stim board IS the camera-trigger board** — one Arduino Mega 2560 on COM3; the sketch
  does both the TTL trigger protocol and a non-blocking stim state machine.
- **`allStimLow()` is the first statement in `setup()`, before `Serial.begin()`.** `setup()`
  blocks on the handshake, so anything later leaves the pin floating — which a powered laser
  driver reads as ON. Pins come from the profile's **`stim_safe_pins`** (`[53]` = laser on
  3dpose, `[]` on 3dface); do not hardcode pins in `stim_compiler.py`.
- **The sequence is baked into the `.ino` at compile time**, not sent over serial: editing
  does nothing until **Apply** (arduino-cli compile + upload). Test and Record warn when the
  canvas has drifted from the last upload.
- **Two sketches are held and swapped automatically per acquisition type**
  (`_sketch_for`/`_ensure_sketch_for`): calibration always gets the recording-only sketch, so
  **calibration can never activate stim** — a safety property. `_session_stim_ino` is
  session-scoped and never persisted, so stim is opt-in per launch by Apply. Do not add a
  separate "board holds a paradigm" flag; derive it from `_sketch_for` and `_session_stim_ino`
  (a parallel flag drifts from the sketch on the board).
- **No floating-point math in `updateStim()`** — the compiler resolves period and pulse width
  to integer µs, because an AVR float divide (~30 µs) inside the trigger busy-wait blunts the
  ±0.35 µs edge precision. `test_stim_compiler.py` asserts no floats reach it.
- **Pulse width ≥ period means constant ON**, not "invalid" (treating it as unrepresentable
  held the laser LOW for a whole recording).
- **`_extract_chains` must stay cycle-safe** (a revisit closes the loop via `loop_to`); a pure
  loop has no source, so it must be pinned or it compiles to nothing.
- **"Ending" stops the recording, not the chain** — a loop keeps running, so bound it with a
  parallel timer chain. Two chains on one pin fight over the output; `pin_conflicts()` blocks
  Apply and Test.
- A stim block on a **camera trigger pin** (2/4/6/8/10/12) injects edges into one camera and
  breaks the alignment axiom undetectably; pins 0/1 garble the serial link and the RDY ack.
  `compile_ino` raises, so the `.ino` can never be generated.
- Every recording writes **`stim_paradigm.json`** and **`stim_paradigm.ino`** into the
  recording dir (`matches_uploaded_firmware: null` means nothing was uploaded that session —
  unknown, not wrong). `stim_trace.csv` is **derived, not observed** — it uses
  `t = (unwrapped_blockid - 1) / fps`, never the frame index, and cannot know the laser fired.

## Serial and laser safety

- **Never suppress the DTR reset** (`dtr=False`/`rts=False` before `open()`): the board then
  ignores the config and emits zero triggers. The reset returns the sketch to `setup()` with
  a cleared RX buffer, so it is load-bearing; the flash it causes is handled by keeping the
  connection open, not by defeating the reset.
- **One long-lived `TeensyController`**, claimed at startup (`_warm_serial()`) and held until
  quit. Both the startup open and the Apply reclaim (`_on_upload_done` after
  `release_serial_port()`) must stay eager — a lazy open moves the reset flash into
  recording #1. The board resets at launch and on Apply, never at Record start.
- **Every start is confirmed** by an `RDY <n_cams> <fps>` ack, because a start can land in
  `loop()`'s reconfigure branch instead of a fresh `setup()`. `start_triggers()` returns a
  bool over four branches: (1) ack → proceed; (2) no ack → reset and retry; (3) still no ack
  and this board has *never* acked → assume pre-RDY firmware and proceed; (4) no ack but the
  board *has* acked before → real fault, return False and roll the cameras back rather than
  record an empty session. The 3-vs-4 distinction is the whole safety property;
  `test_serial_handshake.py` pins all four.
- **Quitting always sends `stop_triggers`** if the port is open, so closing the GUI can never
  leave a paradigm or laser running. `stop_triggers`/`_rollback_acquisition` return a bool
  and never infer success from port state (`pyserial`'s `is_open` stays True after unplug).
- **A pulldown does not gate this laser.** The CNI PSU-III's MOD input carries an internal
  pullup far stronger than 6.8 kΩ, and a resistor stiff enough to beat it would exceed the
  Arduino's 20 mA per-pin limit — there is no safe value. For a hard gate use the PSU
  interlock, or a normally-closed relay across MOD/GND held open by a dedicated pin. Stay on
  the TTL toggle (analog mode maps 0-5 V onto power).
- **Never flash `campy/campy/trigger/trigger.ino`** to the rig board: it has no
  `stim_safe_pins` boot guard, so it drops laser safety and all stim. It exists only for the
  legacy `campy` capture path.

## Network

- **Never trust a written camera→port mapping** — derive it with `uv run probe_network.py`,
  which reads it off the wire. Cameras have been re-addressed and re-cabled; any hardcoded
  segment list is stale.
- **Resend count is not loss.** The two segments differ by ~9,700 vs ~3 resend requests over
  a 20-minute run and still deliver identical frame counts; resends are recovered. What once
  turned them into lost frames was a slow grab loop, now fixed.
- **The resend asymmetry follows the SWITCH, not the NIC port** — do not re-investigate the
  host side of the network on the strength of old notes.
- `gige_driver: socket` (user-space, reliable resends). The in-kernel `filter` driver drops
  a frame on a lost packet with default resend settings (~23% loss at 6×100 fps).

## CPU placement

- **Pin each grab thread to its own P-core** (`pin_capture_threads`): a grab thread on an
  E-core runs a few percent slow, which the 10 ms loop cannot absorb.
- **Keep capture threads off CPUs 0 and 1** (`capture_core_exclude: [0, 1]`), which carry
  ~46% of NIC DPC — a grab thread pinned there is descheduled by the traffic it is receiving.
- **Do not confine RSS or encoders to a core subset.** Confining RSS to the E-cores, pinning
  encoders to the P-cores, and one-encoder-per-E-core were each measured worse (see
  HISTORY.md). `configure_nic.ps1` defaults **restore** the vendor RSS placement; pass other
  values only for a deliberate, one-variable experiment.

## Measurement discipline

- **Never bracket a GIL-releasing call with a wall-clock timer.** numpy releases the GIL for
  the memcpy and re-acquires before returning, so the re-acquisition wait lands inside the
  bracket — a 0.08 ms copy reads as 2.7 ms. Split executing from waiting with
  `QueryThreadCycleTime` (`probe_gil_wait.py`). The budget: ≤300 µs of GIL-held work per
  thread per frame is safe even at 17 threads; ~1000 µs blows the 10 ms budget at 11.
- **Never measure with a second Panopticon running.** Concurrent instances contend for the
  GIL, the NVENC session cap and the cameras, and produce false "divergence" findings;
  `probe_guard` refuses to start when it detects another instance.
