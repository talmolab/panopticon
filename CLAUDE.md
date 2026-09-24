# Working rules for Panopticon

Panopticon records hardware-triggered video from any number of machine-vision
cameras on Windows. Basler cameras run through pypylon and FLIR cameras through
the Spinnaker C API. It encodes on an NVIDIA GPU while it captures, and writes
trigger-aligned videos, ChArUco calibrations and optogenetic stimulation
records. The GUI is PyQt5. `gui_app/` is shared with the 3dface rig: each rig
differs only in its profile (`profiles/*.yaml`), and `profiles/3dface.yaml` must
keep loading and behaving.

This file states the rules the code relies on, in the present tense. The
reasons behind the numbers, and what was tried, are in
[docs/HISTORY.md](docs/HISTORY.md). Live measurements are in
[docs/INTERNALS.md](docs/INTERNALS.md). A number marked "reference rig" comes
from the nine-camera 3dpose rig and lives in its profile, never in code.

Launch with `uv run gui.py`. `gui.py --profile NAME` opens that profile and
remembers it. `gui.py --force` starts a second instance, for an operator who has
checked by hand that no other one holds the hardware.

## Conventions

- The offline suites (`test_*.py`), the dev probes (`probe_lag.py`,
  `probe_seq.py`, `probe_abuse.py`, `probe_cpu_load.py`, `probe_mp.py`) and
  `tools/` are local-only by the maintainer's choice. They are gitignored and
  are not in the public tree, so public docs never tell a reader to run them.
  In a working tree that has them, run the matching suite after touching a
  module (plain scripts, no pytest, with `QT_QPA_PLATFORM=offscreen`):
  - `test_stim_compiler.py` and `test_stim_trace.py` after `stim_compiler.py`
    or `stim_trace.py`;
  - `test_serial_handshake.py` after `serial_controller.py`;
  - `test_frame_sync.py` after `frame_sync.py`;
  - `test_grab_failure.py` and `test_capture_faults.py` after `grab_thread.py`;
  - `test_sync_router_offline.py` after `sync_encode.py` (`test_sync_router.py`
    is its GPU half);
  - `test_ring_release.py` after anything that holds the NV12 ring
    (`grab_thread.py`, `sync_encode.py`, `camera_manager.py`, `gui_app/mp/`);
  - `test_board_coverage.py` after `board_detector.py`;
  - `test_calibrate.py` and `test_calibrate_degenerate.py` after
    `1_calibrate.py`;
  - `test_backend_contract.py` after `backends/__init__.py` or any backend,
    `test_spinc.py` after `_spinc.py` or `fake_spinc.py`, and
    `test_flir_backend.py` after `flir.py`;
  - `test_nvgil.py` after `nvenc.py` or `cuda_driver.py`;
  - `test_trigger_source.py` after `trigger_source.py`;
  - `test_logging.py` and `test_session_log_gui.py` after `logging_setup.py`;
  - `test_main_window_start.py`, `test_first_launch.py` and `test_flir_gui.py`
    after `main_window.py`, and `test_single_instance.py` after `gui.py`;
  - `test_probe_lag_sim.py` after `probe_lag.py`, `test_probe_flir.py` after
    `probe_flir.py`, and `test_probe_network.py` after `probe_network.py`;
  - `test_flir_doc.py` after `docs/FLIR.md` or anything it quotes;
  - `test_history_claude.py` after this file or `docs/HISTORY.md`.
- In a working tree that has the local-only files,
  `bash tools/run_suite.sh <log_dir>` runs every hardware-free suite (all but
  `test_sync_router.py`) one at a time, each through `tools/run_nogpu.py`, so no
  suite takes an NVENC session. `test_sim_gui.py` reaches the GPU only with
  `PANOPTICON_TEST_GPU=1`. Never run the local-only `test_nvgil.py --gpu` and a
  GPU run of `test_sim_gui.py` at the same time.
- A test that validates a fix drives the real GUI the way an operator does,
  in the app launched with `gui.py` or a shortcut to it: let the preview run,
  Record, stop, then Record again (and Calibrate) in the same process, and
  watch the process's memory between acquisitions. Headless probes,
  `CameraManager` scripts and scripts that build `MainWindow` themselves
  diagnose; they do not validate.
  Some defects show only that way: NV12 rings that outlive a recording pass
  every probe and suite, and then refuse the second Record for lack of RAM.
- `.gitignore` anchors `/_*.py` to the repository root. Keep the anchor. An
  unanchored `_*.py` also matches every `__init__.py` and
  `gui_app/backends/_spinc.py`, keeps them out of every commit and breaks a
  fresh clone. `!**/__init__.py` is a second guard. After adding a package or
  an underscore module, check `git ls-files "*__init__.py" "*_spinc.py"`.
- Camera vendor code lives in `gui_app/backends/`. `backends/__init__.py`
  defines the `CameraBackend` and `GrabResultProtocol` contracts,
  `KNOWN_BACKENDS` and `load_backend(name)`. `camera_manager` loads a backend
  with `load_backend(profile.camera_backend)`, hands the instance to each
  `GrabThread` and reads its optional members with `getattr`. Nothing outside
  `backends/` imports pypylon or loads Spinnaker, so a rig without an SDK fails
  in one place with a clear message. A new vendor is one new module, a name in `KNOWN_BACKENDS` and a
  branch in `load_backend()`.
- `gui_app/backends/_spinc.py` is the only code that loads the Spinnaker DLL,
  apart from `probe_flir.py`'s optional `--pyspin` stage (next rule). `_spinc`
  loads through `ctypes.CDLL`, never `PyDLL`, which holds the GIL through
  every wait, inside a scoped `os.add_dll_directory`. Never add the SDK's
  `bin64\vs2015` to `PATH`: its Qt5 DLLs would shadow PyQt5's. Importing
  `_spinc` or `fake_spinc` loads no DLL.
- `probe_flir.py` is tracked, because FLIR volunteers run it from the clone. It
  is the only exception to the vendor rule: its optional `--pyspin` stage
  imports PySpin lazily, and its measurements call SpinC methods through
  `FlirBackend.api` on cameras the backend would refuse. Importing
  `probe_flir.py` imports no vendor SDK. Its UNKNOWNS mirror `flir.py`'s
  docstring, and `test_probe_flir.py` fails when they drift.
- `docs/FLIR.md` quotes `probe_flir.py`'s stages and constants and the FLIR
  refusal texts, and `test_flir_doc.py` fails when they drift. Change the guide
  in the same commit as the code.
- No rig-specific numbers live in code. They live in the profile and reach the
  code through `session_config`. Do not repeat a profile value in a comment or
  a constant. Nothing assumes a camera count: it comes from the profile and the
  enumeration.
- Comments and docstrings state the rule and its reason in the present tense,
  with no dates, names, commit hashes or story (`CONTRIBUTING.md`). The story
  goes in `docs/HISTORY.md`.
- Blocking camera work (open, close, reconfigure) runs off the Qt main thread
  through `ui_workers.CallableWorker`; on the main thread the window stops
  responding. Quitting mid-session abandons the incomplete data and deletes it
  (`_abandon_and_cleanup`).
- `rig_setup.apply_profile_to_manager` and `rig_setup.open_kwargs` are the path
  from a profile to a camera manager. The window, the local-only `probe_lag.py`
  (which `probe_mp.py` runs) and the capture workers go through them, and
  `apply_profile_to_manager` puts the profile's `log_level` in force.
  `probe_flir.py` and `probe_network.py` open cameras through the backend
  directly.

## Logging

- Printing never waits. `gui_app/logging_setup` replaces stdout and stderr.
  Each line gets a millisecond timestamp and its thread's name and goes on a
  bounded queue (`QUEUE_LINES`) that one writer thread drains to the log file.
  The console has a queue and a thread of its own, so a console window that is
  not being read holds up neither the file nor `session.log`. A full queue
  drops the line and counts it, and the writer reports "N log lines dropped".
- Add no flush, lock or file write to the print path. Never call
  `logging_setup.flush()`, `verbose()`, `debug()` or `transition()` from a grab
  or encoder thread. `flush()` finishes only the calling thread's unfinished
  line. In a capture worker the writer writes the worker's log file before it
  sends the line up the pipe to the parent, so a parent that stops reading
  cannot hold the worker's own log.
- The session header prints at every level. `log_level` (normal, verbose or
  debug; default verbose) adds cold-path detail only. `verbose` adds the
  requested and read-back camera settings, `[state]` transitions and the stop
  summary, and `debug` adds more, such as every `.pfs` feature the camera read
  back. Nothing logs per frame at any level, and the grab loop's stats line
  stays periodic.
- Regression runs compare the `[camN] exposure=` line verbatim, so read-backs go
  on lines of their own. Before comparing a log line with older output, strip
  the stamp `^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} \[[^\]]+\] `.
- Every recording and calibration folder gets `session.log`, the slice of the
  log from arm to finalize. Lines still queued at a native crash are lost;
  faulthandler writes to the log file directly.

## Capture invariants

A break here usually gives an empty or misaligned recording that looks normal
until someone analyses it.

- Basler: never read `result.Array` in the grab loop. It copies the whole frame
  with the GIL held (0.84 ms per frame per camera at 1920 x 1200). Use
  `with result.GetArrayZeroCopy() as img:`. `np.frombuffer(GetBuffer())` copies
  too.
- `img` is a view over the driver buffer. It must not leave the `with` block or
  outlive `result.Release()`, and every consumer copies out of it.
- `result.PaddingX` and `result.PaddingY` are checked on every frame, before the
  `with`. A nonzero value retires the camera, because row padding shears every
  frame. They are grab-result fields and always present; the `PaddingX`
  nodemap feature is a different thing and is absent on the reference cameras.
  On FLIR, `retrieve()` checks the first image after each start (8 bits per
  pixel, a stride equal to the width, the opened size), and otherwise releases
  it and raises `FlirFrameError`.
- The NV12 ring is built with `np.full(..., 128, ...)`. That fills the chroma
  plane with its constant 128 and touches every page. `np.empty` or `np.zeros`
  puts a first-touch page fault (about 0.4 ms) back on the hot path.
- In kick mode a ring slot is written only while it is on the free list. The
  router owns a slot from `submit()` until the frame is encoded or dropped
  (`attach_ring`). A camera whose encoder falls behind finds no free slot and
  loses frames from its own video (`dropped_full`, no block ID); the pixels of a
  queued frame are never overwritten. That camera's video is then shorter than
  the others, and the session warnings name it.
- Every acquisition frees its NV12 rings when it stops. The grab thread drops
  its ring and free list when its loop ends, `_CameraSink.release_ring()` drops
  the router's references once that camera's encoder has exited, and
  `CameraManager.stop_acquisition` runs `gc.collect()` after every grab thread
  has exited. The ring, the router's sinks and the encoder threads refer to one
  another. Without the release the rings wait for a full garbage collection,
  which a quiet GUI may never run, and the next Record is refused for lack of
  RAM.
- The grab loop's critical path is retrieve, copy, queue, release, and nothing
  else. Encoders drain separately into `stream.h264` through PyNvVideoCodec
  (`gui_app/nvenc.py`) or libx264 (`gui_app/cpu_encode.py`). Encoding inline
  costs about 28% of frames (HISTORY.md).
- A camera that cannot start, realign or deliver is retired (`router.retire`):
  a failed StartGrabbing, a late arm (StartGrabbing returning after the
  barrier), grabs that all fail for `FAILED_GRAB_RETIRE_S`, or no frame since the
  triggers started. In kick mode the coordinator waits for every camera, so one
  camera that never publishes force-drops every trigger for all of them.
- `blockids.npy` records only frames that were persisted. A `queue.put_nowait`
  means the queue took the frame, not that it was encoded, and a dead encoder
  would map frame i to the wrong trigger. `sync_encode.stop()` reconciles each
  camera against its coded pictures plus the frames spilled raw
  (`coded + spilled`) and writes `WARNINGS.txt`. A release with no ring slot, or
  one still waiting at the stop deadline, is not recorded. No camera's `raw.bin`
  or block IDs are cut to another camera's length.
- Block ID equals trigger ordinal is an axiom, and one failure breaks it without
  a trace. A block ID counts frames the camera acquired, not triggers fired. A
  camera over its exposure ceiling ignores the next pulse and consumes no ID, so
  from then on its block ID N is trigger N+k, with no gap in `blockids.npy`.
  The guard is `frame_sync.check_block_id_rate()`: the device clock is a
  hardware clock, so block IDs must advance at the trigger rate. It runs from
  `sync_encode.stop()`, from `align_recording()` before its `needs_alignment`
  early return, and from the main window's finalize in every non-kick mode.
  `BLOCK_RATE_TOL = 0.003` is measured; do not widen it to 1% (HISTORY.md).
  Every camera off by the same amount means the reference (`frame_rate` or the
  timestamp unit) is wrong.
- Every backend normalises its counters to the contract: block ID 1 is the
  first frame after each StartGrabbing, and `TimeStamp` is a device clock in
  nanoseconds that keeps running across a stream restart. The Basler backend
  logs each camera's timestamp tick rate and warns when it is not 1 GHz.
- Cameras drop frames independently, so frame i is not the same trigger on
  every camera. Each frame's block ID goes to `blockids.npy`, and the videos
  are aligned by trigger in one of two ways. Real-time kick-out
  (`realtime_kick: true`, the default) encodes a trigger only once every camera
  caught it. Post-hoc alignment (`realtime_kick: false`, `gui_app/alignment.py`,
  `2_align.py`) re-encodes to the common frames afterwards. A replace refuses
  while a camera ended early, started late or stopped for more than 1 s, unless
  that camera is excluded (`--exclude`, or `RETIRED.json`) or
  `--truncate-to-shortest` is given. A replace decodes with
  `-fps_mode passthrough` and refuses when a camera's decoded frame count is not
  the length of its `blockids.npy`. Anything that rewrites `blockids.npy`
  rewrites `stim_trace.csv`.
- A Basler 16-bit block ID wraps at 65535 (about 11 minutes at 100 fps). Every
  unwrap uses one rule, a drop of more than half a period is a wrap
  (`frame_sync.unwrap_one`, `frame_sync.unwrap_blockids` and
  `alignment._unwrap_blockids`). A FLIR 16-bit frame ID is
  unwrapped in the backend from the device clock, so a trigger ignored at a
  wrap is a gap there.
- The trigger board starts only when every camera is armed. The start waits in
  `wait_until_ready()` and is refused when a camera has not armed after
  `READY_TIMEOUT_S`. It calls `mark_board_starting()` immediately before
  `start_triggers`, and refuses when any camera counted a frame before that
  (`frames_before_barrier()`). A camera armed after the first trigger counts its
  block IDs from a later trigger than the others, and its `blockids.npy` and
  the rate check both look clean.
- With `trigger_source: external` the host cannot start the source.
  `ExternalTriggerSource.close_barrier()` watches the armed cameras, marks the
  barrier and refuses the recording if any camera retrieved a frame before the
  operator is asked to start the source. A source already running during arming
  gives each camera a different first pulse. This mode opens no serial port,
  refuses `serial_port`, `trigger_pins` and `stim_safe_pins`, and runs no
  stimulation.
- A stall must not end the session. A recording camera re-arms after 25
  consecutive timeouts (`STALL_TIMEOUTS`, about 5 s), up to 5 times
  (`MAX_REARMS`). StartGrabbing restarts the block-ID counter, so
  `_resync_offset()` recovers the ordinal from the device clock and refuses when
  the gap is not within 0.25 of a period: a guessed ordinal is worse than losing
  the camera. A camera that cannot realign is retired, which keeps the
  survivors aligned.
- While every active camera is silent (`CameraManager.source_silent`), a camera
  with frames waits `SOURCE_DOWN_WAIT_WINDOWS` stall windows of that silence and
  then re-arms as usual, because a stalled shared switch, NIC port or USB host
  needs the re-arm. Re-arms that run out while every camera is silent retire no
  camera. A camera with no frame since the triggers started is retired at its
  first stall, unless every camera is silent; then it waits.
- A recording's kick-out losses reach the dialog and `WARNINGS.txt` as one line,
  "Effective frame rate X fps (target Y).", once they pass
  `KICKOUT_WARN_FRACTION`. The counts go to `session_metadata.json` (`kickout`)
  and the detail to the log. Forced drops, the block-ID rate check and
  retirements keep messages of their own.
- RAM either refuses the start, when the need exceeds what is available, or
  raises no warning. Running out of RAM mid-session loses frames.

## FLIR backend

[docs/FLIR.md](docs/FLIR.md) is the bring-up guide for FLIR cameras.

- `FlirBackend` runs a free-run self-test at open. It refuses a camera whose
  device clock reads 0, does not increase, restarts at each start, or runs in a
  unit it cannot convert to nanoseconds. The same test asks whether the frame
  ID restarts at 1 (or 0) at each start and steps by 1. Under the default
  `camera.flir.block_id_source: auto`, a camera whose frame ID fails that test
  is aligned by its count of trigger edges (the CounterValue chunk) instead. It
  is refused only when it has no CounterValue chunk either, or when
  `block_id_source: frame_id` is forced.
- `FlirBackend` never executes `TimestampReset` or `TriggerSoftware`, and it
  releases every image before `EndAcquisition`.
- `camera.trigger.overlap` defaults to `ReadOut`. A camera that does not overlap
  exposure with readout ignores a trigger that arrives during readout, and an
  ignored trigger consumes no frame ID.
- The trigger witness counts edges on the trigger line (Counter0) and the
  exposures the camera started (Counter1). Edges minus exposures goes to
  `WARNINGS.txt`, and in `frame_id` mode a nonzero count means a misaligned
  camera.
- The witness never reports a clean camera from counts it cannot prove.
  Counters that do not count, reads that disagree and edges it could not place
  each write the no-witness or the not-proven sentence. Do not turn one of those
  into silence to quiet a false alarm.
- A counter narrower than `2**31` is trusted only while the frames and the stall
  re-arm down-time edges stay under half its period. Past that, the witness
  writes the limited-witness sentence and gives no count.
- A camera with no witness sentence is still not proven aligned in the cases
  the `flir.py` module docstring lists:
  - a trigger ignored between the arming inside `BeginAcquisition` and the
    first read after it;
  - an exposure that starts later than the `TriggerDelay` plus one register
    read at a re-arm;
  - a narrow counter that ignored a whole multiple of its period (the rate
    check reports it);
  - two counters that count the wrong events but agree;
  - a `frame_id` camera without an edge counter.
- A 16-bit wrap is decided from the device clock, never from the first raw ID
  after it. A re-arm's witness window ends after `BeginAcquisition` returns.

## NVENC

- The driver caps concurrent NVENC sessions, and the cap is often what limits
  the camera count. Probe it (`nvenc.probe_max_sessions`); never hardcode it.
  `EndEncode()` does not free a session, the destructor does, so encoders are
  released with `_EncoderThread.release_encoder()`. NVENCSTATUS 21 is the
  session limit: never descend a keyword fallback ladder on it, and keep the
  GOP keys (`gop`, `idrperiod`, lowercase) on every rung.
- Prove the GOP from the bitstream (`nvenc.gop_is_honoured()`, run by the launch
  preflight). PyNvVideoCodec ignores keywords it does not know, so a misspelled
  key (`gopLength`, `idrPeriod`) gives one IDR for a whole recording while the
  code reads as correct. The default path stream-copies `stream.h264`, so `-g`
  on the ffmpeg writers cannot repair it.
- Every mp4 the rig writes carries `-g <fps>` and `-movflags +faststart`, so it
  loads and seeks in the browser labeler (LUC3D). Without `-g` a file can hold
  one IDR; without `+faststart` the player reads the whole file before frame 1.
  Build writer commands from `gui_app/ffmpeg_cmd.py` (`h264_encoder_args` plus
  `mp4_container_args`). The rule covers the mp4 writers, not the Annex-B
  `.h264` streams the remux wraps.
- The encoder does not copy frames to the GPU with the GIL held. On the `host`
  path PyNvVideoCodec's `Encode()` uploads each NV12 frame with the GIL held,
  0.5-1.1 ms per call on the reference rig, and starves a grab thread until its
  camera drifts hundreds of frames behind. The profile default is
  `nvenc_upload: pinned` with `nvenc_context: shared`: `PinnedUploadEncoder`
  copies the frame into page-locked staging buffers with numpy, which releases
  the GIL, and the driver uploads it by DMA on the encoder's stream.
- Never synchronize the encoder's stream from the host. The stream also carries
  NVENC's own work, so a host synchronize drains the encoder's pipeline every
  frame and spins a core. A staging buffer is reused only after the CUDA event
  recorded behind its last `Encode` has completed, and every driver call is
  bracketed by a push and a pop of the encoder's context.
- The pinned path relies on PyNvVideoCodec queuing its copy on the stream it is
  given, which the library does not document.
  `nvenc.pinned_upload_matches_host(width, height, context)` proves it: it
  stalls the stream, rewrites the staging buffer during the stall and compares
  the bitstream with the host path's. The launch preflight runs it at every
  launch, at the recording's frame size, and False means record with the host
  upload. The process's one pinned warm-up encode (`nvenc._warm_pinned`)
  decides for the whole process: if it fails, every later encoder gets the host
  upload (`upload_stats()["pinned_disabled"]`). A pinned setup failure falls back to the
  host upload for that encoder only, with a `WARNINGS.txt` line. A recording
  encoder whose `Encode` fails takes the path of any encoder failure (flush,
  then spill that camera's frames raw) and leaves the pinned path on for the
  others. In a working tree that has the local-only suites, also run
  `test_nvgil.py --gpu` after a PyNvVideoCodec or driver update.
- In the window, the profile reaches `nvenc` only through
  `hardware_check.configure_nvenc_upload`, at launch and after a profile switch,
  before any encoder exists and before the GOP check. A capture worker applies
  the choice its parent sends when it arms. `nvenc_context: own` needs
  free GPU memory for one extra context per camera (`own_context_headroom`);
  without it the shared context is used, with a warning.
- A `PinnedUploadEncoder` counts itself closed only after its buffers, stream
  and context are freed, and the teardown line reads that count's snapshot. A
  leak warning at teardown is therefore real.

## Exposure and the limiter

- Basler: exposure and gain live in the `.pfs`. In trigger mode the camera's
  frame-rate timer starts after exposure ends, so the shortest interval between
  frames is exposure plus 1/AcquisitionFrameRate. `set_triggered` sets
  AcquisitionFrameRate to the profile's `trigger_rate_limit`, so the ceiling is
  `1e6/fps - 1e6/trigger_rate_limit` (`BaslerBackend.exposure_ceiling_us`). A
  trigger inside the interval is ignored. On the reference rig (100 fps, limit
  165) an exposure past 3.94 ms makes every second trigger skip.
- `camera_manager.apply_exposure_gain()` runs at every acquisition start. With
  `exposure_us=None` (a recording) it re-applies the `.pfs` baseline captured at
  open, so a calibration exposure cannot leak into a recording. With a value (a
  calibration) it applies that value. It clamps to 90% of the backend's ceiling
  and logs `CLAMPED from ...`, so read the `[camN] exposure=` line instead of
  assuming the `.pfs`. The line names the backend's `CEILING_BASIS` when it has
  one.
- A backend's `RefusalException` from `exposure_ceiling_us` refuses the start
  (`AcquisitionStartRefused`) before any exposure is written. Keep it a refusal:
  a recording made anyway skips triggers or drops frames.
- FLIR: settings come from the profile's `camera:` block and the user set it
  names. There is no `.pfs`, and `pfs_path`, `trigger_rate_limit`,
  `gige_driver` and the `gev_*` fields are refused. The ceiling is the camera's
  own ExposureTime maximum at the frame rate, and opening refuses an exposure
  above 90% of it. `backend.open` receives `frame_size` and `frame_rate` only
  when its signature names them; the Basler open is called with
  `(device, pfs_path, max_num_buffer)` as before.
- Reference-rig recording exposure and gain are 3000 µs and 6 dB. Lower values
  crush the histogram, and about 7x total clips about 13% of pixels. Prefer more
  IR light, then exposure, then gain. Check any change on a recording: the
  preview free-runs at 30 fps with 33 ms of headroom, so a too-long exposure
  shows only once triggered.
- Keep `trigger_rate_limit` at 165 on the reference rig, never 0. The limiter
  paces each camera's readout across 6.06 ms. Without it every camera bursts
  after the shared trigger, and marginal links lose 8-15% of frames in
  transmission. The exposure ceiling therefore stays, and is not worth buying
  out until the network has margin to spare.

## Thermals

- A camera that reports its shutdown temperature (`temp_shutdown_c`) alerts at
  that temperature minus the profile's `thermal_warn_margin_c`, and always in
  the camera's own error state. The camera's Critical status alone does not
  alert. Thresholds come from the camera, never from code. A camera that
  reports no shutdown point is judged by its own status, and one that reports
  neither cannot be judged; the log says which.
- Never write `BsliDeviceTemperatureOverwriteEnable` or
  `BsliDeviceTemperatureOverwriteValue`. They fake the reported temperature and
  defeat the camera's own over-temperature shutdown.
- The reference rig sets `thermal_warn_margin_c: 2.0`, so its cameras, which
  shut down at 81 C, warn at 79 C. Start a rig measurement only with every
  camera at 78 C or less, and abort at 80 C.

## Calibration

- `board_legacy: true` in a board config selects the pre-OpenCV-4.6 ChArUco
  layout of the physical 3dpose board. Without `setLegacyPattern(True)`, the
  OpenCV 4.7 `CharucoDetector` finds 0 corners and reports nothing wrong. Both
  call sites go through `charuco.apply_legacy_pattern()`, which raises instead
  of skipping. Other boards default to false.
- `opencv-contrib-python>=4.7` is pinned in `pyproject.toml`, never in a PEP 723
  header on `1_calibrate.py`. OpenCV renamed `chessboardCorners` to
  `getChessboardCorners()` between 4.6 and 4.7, and the solve runs in the
  project environment so an offline rig never resolves a second one.
- The HUD counts ArUco markers, not interpolated ChArUco corners. Counting
  corners is stricter than calibration needs and starves oblique cameras.
- `READY` needs `min_per_cam_shared` co-detection ticks per camera, one
  connected coverage graph (`min_edge` co-detections make a pair an edge), and
  `MIN_GRID_CELLS` of the 2 x 2 view grid per camera. One connected component is
  enough: the board is one-sided, so opposed cameras never co-detect. `READY`
  counts one per camera per co-detection tick, and its thresholds stay at most
  about twice the solver's frame caps.
- The solve keeps the largest connected component. Every per-camera figure can
  read satisfied while the graph is several components, and the HUD's
  `groups N/1` shows it.
- Co-detection hints and pairing use block IDs, not frame indices: after one
  missed trigger, frame i is a different trigger on that camera.
  `codet_frames.json` is format 3.
- `1_calibrate.py` passes a view to `cv2.calibrateCamera` only when its corners
  determine a homography (`_homography_ok`). A view whose corners lie on one
  board row makes calibrateCamera assert and would end the solve. An OpenCV
  error while fitting one camera fails that camera only.
- Pair RMS below `RMS_GOOD_PX` (1.5 px) is good, and from `RMS_POOR_PX` (3.0 px)
  up it is poor and warned about.
- The best calibration comes from full videos of every camera (sleap-anipose).
  The GUI's Solve and `1_calibrate.py` fit a subsample: faster, less accurate.

## Optostim

The node editor (sidebar, Stimulation) compiles a block graph into the Arduino
sketch. `gui_app/widgets/stimulation_window.py` holds the canvas and its UI;
`gui_app/stim_compiler.py` turns the graph into the `.ino` with pure functions
and no Qt.

- The stim board is the camera trigger board: an Arduino Mega-class board on the
  profile's `serial_port`, whose sketch runs the TTL trigger protocol and a
  non-blocking stim state machine. Stimulation needs `trigger_source: board`.
- `allStimLow()` is the first statement in `setup()`, before `Serial.begin()`.
  `setup()` blocks on the handshake, so a later call leaves the pin floating,
  and a powered laser driver reads a floating pin as ON. The pins come from the
  profile's `stim_safe_pins` (`[53]`, the laser, on the reference rig; `[]` on
  3dface). Never hardcode pins in `stim_compiler.py`.
- The sequence is compiled into the `.ino`, not sent over serial, so an edit does
  nothing until Apply (arduino-cli compile and upload). Record refuses a canvas
  that was never applied, a canvas edited since the last Apply, and an empty
  canvas while an earlier Apply still holds a paradigm. Test asks to upload a
  changed canvas and refuses after a failed Apply.
- Two sketches are held and swapped per acquisition type (`_sketch_for`,
  `_ensure_sketch_for`). Calibration always gets the recording-only sketch, so a
  calibration can never activate stim. `_session_stim_ino` belongs to the
  session and is never saved, so stim is opt-in per launch through Apply.
  Derive whether the board holds a paradigm from `_sketch_for` and
  `_session_stim_ino`; a separate flag drifts from the board.
- `updateStim()` does no floating-point math. The compiler resolves period and
  pulse width to integer microseconds (`block_timing()`), because an AVR float
  divide (about 30 µs) inside the trigger busy-wait blurs the ±0.35 µs edge
  timing. Every train-or-constant decision, `stim_trace` included, goes through
  `stim_compiler.drive_mode()` on those integers. `test_stim_compiler.py`
  asserts that no float reaches the sketch.
- A pulse width at or above the period means constant ON. A rule that calls it
  invalid holds the laser LOW for the whole recording.
- `_extract_chains` stays cycle-safe: a revisit closes the loop through
  `loop_to`. A pure loop has no source, so it must be pinned or it compiles to
  nothing.
- An Ending block stops the recording, and a loop keeps running; bound a loop
  with a parallel timer chain. Two chains on one pin fight over it, and
  `pin_conflicts()` blocks Apply and Test.
- A stim block on a camera trigger pin (the profile's `trigger_pins`) injects
  edges into one camera and breaks block-ID alignment with no trace. Pins 0 and
  1 garble the serial link and the RDY ack. `compile_ino` raises for both, so
  such a sketch is never generated.
- Every recording writes `stim_paradigm.json` and `stim_paradigm.ino` into its
  folder. `matches_uploaded_firmware: null` means nothing was uploaded that
  session, so the match is unknown. `stim_trace.csv` is derived: it uses
  `t = (unwrapped_blockid - 1) / fps`, never the frame index, and cannot know
  whether the laser fired.

## Serial and laser safety

- A computer may have no profile chosen: nothing remembered, or a remembered or
  `--profile` name that does not load. The window then opens no camera and no
  serial port, runs no hardware check and flashes nothing until the operator
  chooses a profile in the dropdown or with `gui.py --profile NAME`. The choice
  is remembered. Never fall back to another profile: it names another rig's
  port and pins, and its sketch holds none of this rig's pins low.
- Never suppress the DTR reset (`dtr=False` or `rts=False` before `open()`). The
  board then ignores the config and emits no triggers. The reset returns the
  sketch to `setup()` with an empty receive buffer. The flash during the reset
  is handled by keeping the connection open.
- Each board has one long-lived `TeensyController`, claimed eagerly at launch
  (`_warm_serial()`) and reclaimed eagerly after every Apply, failed or not
  (`_on_upload_done` after `release_serial_port()`). A lazy open moves the reset
  flash into the first recording. The board resets at launch, on Apply and at a
  switch to a profile on another port. At Record it resets only when the first
  start gets no ack (branch 2 below).
- Every exchange with the board holds the controller's lock. `board_id` is
  cleared on every open and at the start of every ack wait.
- Every start is confirmed by an `RDY <n_cams> <fps> <id>` ack, because a start
  can land in `loop()`'s reconfigure branch instead of a fresh `setup()`.
  `start_triggers()` returns a bool over four branches:
  1. ack: proceed;
  2. no ack: reset and retry;
  3. still no ack from a board that has never acked: assume pre-RDY firmware
     and proceed;
  4. no ack from a board that has acked before: return False and roll the
     cameras back.

  Telling branch 3 from branch 4 is what keeps an empty session from being
  recorded. `test_serial_handshake.py` pins all four.
- The retry (branch 2) is skipped when the owner stops or closes the link during
  the first attempt or the reopen, and when the caller's `may_retry` veto
  returns False. Every caller that starts the board under armed cameras (the
  GUI, `probe_lag.py`, `probe_flir.py`) passes a veto that refuses once any
  camera counted frames, on any firmware. The exception is the local-only
  `probe_lag.py --no-ready-barrier`, which passes none (see Measurement
  discipline). The reset restarts the board's trigger count but not the
  cameras' block IDs.
- Closing the GUI never leaves the board triggering, a paradigm running or the
  laser on. Quitting always calls `stop_and_close()`. Under the controller's
  lock and after any start in flight, it stops the board if the link is open,
  closes the link and retires the controller, so `open()` refuses afterwards.
  It warns when a start went out and no stop was confirmed. `stop_triggers` and
  `_rollback_acquisition` never infer success from port state: `is_open` stays
  True after an unplug. `_rollback_acquisition` returns a dict for the dialog.
- A profile switch to another serial port, or to `trigger_source: external`,
  sends the old board `stop_and_close` on the old profile's pins and shows a
  stop it cannot confirm.
- `probe_flir.py` starts the board only when the board reports the
  recording-only sketch for the profile's pins, and stands it down in a
  `finally`. Keep that check: another sketch may start a paradigm with the
  triggers.
- A pulldown does not gate the reference rig's laser. The CNI PSU-III's MOD
  input has an internal pullup far stronger than 6.8 kΩ, and a resistor stiff
  enough to beat it would exceed the Arduino's 20 mA per-pin limit. For a hard
  gate use the PSU interlock, or a normally-closed relay across MOD and GND held
  open by a dedicated pin. Stay on the TTL toggle; analog mode maps 0-5 V onto
  laser power.
- Flash only sketches that `stim_compiler` generates. A sketch without the
  `stim_safe_pins` boot guard, such as a stock trigger sketch, leaves the laser
  pin floating at every reset.

## Network

- Never trust a written camera-to-port mapping. Derive it with
  `uv run probe_network.py`, which reads it off the wire. `probe_network.py
  --sweep` goes through the profile's backend (`set_packet_size`,
  `device_address`) and touches no vendor node itself.
- A resend is not a lost frame: two segments with about 9,700 and 3 resend
  requests over 20 minutes delivered identical frame counts. Resends turn into
  lost frames only when the grab loop is too slow to drain the pool.
- The resend asymmetry follows the switch, not the NIC port. Do not
  re-investigate the host side of the network on the strength of old notes.
- Basler: `gige_driver: socket` (user space, reliable resends). The in-kernel
  `filter` driver drops a frame on a lost packet with default resend settings
  (about 23% loss at 6 x 100 fps).
- Read RSS from an elevated shell. Unelevated, `Get-NetAdapterRss` returns a
  queue count and processor range nobody configured, with `MaxProcessors` and
  `RssProcessorArray` blank, and `configure_nic.ps1 -Check` refuses its RSS
  verdict.

## CPU placement

- Pin each grab thread to its own P-core (`pin_capture_threads`). A grab thread
  on an E-core runs a few percent slow, which the grab loop's per-frame budget
  cannot absorb.
- Keep capture threads off the cores that carry NIC DPC
  (`capture_core_exclude`; `[0, 1]` on the reference rig, where those cores
  spend about half their time in NIC DPC). A grab thread there is descheduled by
  the traffic it is receiving.
- Do not confine RSS or encoders to a core subset. Confining RSS to the E-cores,
  pinning encoders to the P-cores and one encoder per E-core each measured worse
  (HISTORY.md). The defaults of `configure_nic.ps1` restore the vendor RSS
  placement; pass other values only for a one-variable experiment.

## Multi-process capture (stage 1, experimental)

- `capture_processes` above 0 selects `ProcessCameraManager`
  (`rig_setup.make_manager`), which captures the cameras in worker processes.
  The GUI opens no camera for such a profile
  (`MainWindow._capture_processes_refusal`); only the local-only `probe_mp.py`
  builds one. `session_metadata.json` records the request (`capture_processes`)
  and what ran (`capture_processes_used`).
- `gui_app/mp` refuses a CPU other than x86-64, because its shared-memory
  protocol relies on aligned 8-byte loads and stores. Cross-process int64 fields
  go through aligned numpy views, never `struct`.
- The ledger (`gui_app/mp/ledger.py`) publishes in a fixed order: presence
  before the frontier, bits before `decided_upto`, `retired_at` before
  `retired`, the final announce before `eos`. A worker harvests at least every
  `ring_bits / fps` seconds, on its grab loop's timeouts too.
- Only the parent deletes `blockids.partial`, and only once it holds that
  worker's stop results. A worker that misses the stop's results bound is
  terminated. Capture workers opt out of Windows' EcoQoS throttling.

## Measurement discipline

- Never time a GIL-releasing call with a wall clock. numpy releases the GIL for
  a copy and re-acquires it before returning, so the wait for the GIL lands
  inside the bracket: a 0.08 ms copy reads as 2.7 ms. Separate executing from
  waiting with `QueryThreadCycleTime` (the local-only
  `tools/experiments/probe_gil_wait.py`). The budget is 300 µs or less of
  GIL-held work per thread per frame, safe at 17 threads; about 1000 µs breaks
  the 10 ms budget at 11 threads.
- Never measure with a second Panopticon running. Two instances contend for the
  GIL, the NVENC session cap and the cameras, and produce false divergence.
  `probe_guard` refuses to start beside another instance, and `gui.py`'s
  single-instance check calls `probe_guard.other_panopticons()`, so both use the
  same matching rule.
- The local-only `probe_lag.py` starts the board only after every camera is
  armed and exits 4 otherwise. `--no-ready-barrier` exists only to compare with
  runs recorded before the barrier.
- Run no suite, analysis or second probe beside a rig measurement. Their CPU
  load changes the numbers, and their child processes make a guarded probe
  refuse.
- After an interrupted command that started a probe or the GUI, list the Python
  processes and stop what it started. A leftover process keeps the cameras and
  the serial port.
