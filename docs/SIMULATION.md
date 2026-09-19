# The simulated rig

Panopticon can run with no cameras, no trigger board, no vendor SDK and no
NVIDIA GPU. Selecting the `sim` rig profile swaps the Basler backend for
`gui_app/backends/sim.py` and the serial port for `gui_app/backends/sim_board.py`,
and everything else — the grab threads, the kick-out router, the encoders, the
block-ID guards, the alignment pass, the stimulation editor's Apply — is the
code the rig runs.

Two reasons it exists.

**Development and CI.** The whole test suite runs on a laptop, so a change can
be shown to work before anyone books the rig.

**Failures that are expensive or impossible to stage.** A camera that ignores
triggers, a stream that wedges, a 16-bit block-ID wrap, row padding, a board
that mis-parses its frame rate: each is a knob here
([Fault injection](#fault-injection)), so the guard against it can be proven
instead of hoped for. The knobs are written the way the rig produces the
failure — whether a block ID is consumed, whether the ids stay contiguous —
so a test asserts on the guard, not on the simulation.

What it cannot tell you is in [What the simulated rig is not](#what-the-simulated-rig-is-not).

---

## Running the application on it

```
uv run gui.py
```

and pick **sim** from the profile dropdown. The choice is remembered per
machine, so subsequent launches come up on it. On the console you should see

```
[acq] profile: sim
[cam1] SIM00001 640x400 Mono8
...
[teensy] simulated board opened
```

Record and Calibrate then behave exactly as on the rig: the cameras go into
trigger mode, the "serial port" carries the sketch's real line protocol, the
board acks with `RDY <n> <fps> <id>`, the videos and `blockids.npy`,
`frametimes.npy`, `session_metadata.json`, `stim_paradigm.json` and
`stim_trace.csv` land under the output directory you chose.

Two things are worth knowing before the first run:

- **Set the output directory.** The profile's default is the repository's
  `data/`, the same tree a real session goes to. Point it somewhere scratch.
- **Nothing is flashed.** `stim_compiler.upload(ino, "sim")` short-circuits
  arduino-cli and hands the sketch to `sim_board.accept_upload`, so Apply
  takes milliseconds instead of ~30 s and the simulated board's RDY line then
  reports that sketch's identity — which is what makes the firmware-identity
  check runnable offline.

### Installing without the vendor SDKs

`pypylon` and `PyNvVideoCodec` are in the `rig` dependency group, so a machine
that has neither installs with

```
uv sync --no-group rig
```

Everything below runs in that environment.

RULE: run the offline suite at least once with `PyNvVideoCodec` un-importable
before believing it is hardware-free. REASON: `profiles/sim.yaml` says
`encoder: auto`, so the launch preflight picks NVENC on a machine that has it
and libx264 on one that does not, and the CPU path raises two dialogs the GPU
path never shows — the capacity preflight's "Proceed?" and the completion
warning carrying libx264's own note. A suite green on an NVENC workstation can
therefore still fail on the acceptance host. `test_sim_gui.py` derives every
dialog expectation from the encoder that was actually installed, which is what
makes one file cover both; a host that has the GPU can reproduce the other
branch by putting a `sitecustomize.py` on `PYTHONPATH` whose meta-path finder
raises `ImportError` for `PyNvVideoCodec`.

---

## `profiles/sim.yaml`

The profile is the ONE place the simulated rig's shape is written down:
`gui_app/backends/sim.py` reads `n_cameras`, `frame_width` and `frame_height`
from this file (`sim.profile_shape`) rather than restating them, because
`load_backend` takes only a name and a second copy in the backend would be
what the application actually built while `camera_manager` checks the cameras
against the PROFILE. Growing the simulated rig is an edit here and nowhere
else.

Fields that select or shape the simulation:

| Field | Value | What it does here |
|---|---|---|
| `camera_backend` | `sim` | Selects `SimBackend` through `load_backend`. Reaches `CameraManager.open_all(backend=...)` via `rig_setup.open_kwargs`. |
| `serial_port` | `sim` | `TeensyController.open()` builds a `SimSerial` on the shared clock instead of opening a port (`stim_compiler.is_sim_port`, case- and space-insensitive). |
| `encoder` | `auto` | Real-time encoder selection. `auto` takes NVENC when the driver grants a session per camera, else libx264, so the profile runs on a host with no NVIDIA GPU. `x264` forces the CPU path. |
| `n_cameras` | `3` | How many simulated cameras are built AND the count `open_all` refuses a mismatch against. |
| `frame_width` / `frame_height` | `640` / `400` | The simulated sensor's ROI, the NV12 ring's geometry, and what `open_all(expect_geometry=...)` checks. |
| `frame_rate` | `100` | Recording trigger rate, sent to the simulated board. |
| `calibration_frame_rate` | `30` | Calibration trigger rate. |
| `trigger_rate_limit` | `165` | The simulated camera enforces `exposure + 1/trigger_rate_limit` exactly as a real one does, so an over-long exposure makes it IGNORE triggers here too. Not switched off for convenience: that rule is the whole point of the `ignore_every` fault. |
| `realtime_encode` / `realtime_kick` | `true` / `true` | Capture path, unchanged from the rig. |
| `kick_max_lag` | `240` | Kick-out cap. Small frames, so the ring is cheap. |
| `max_num_buffer` | `600` | Recorded, not allocated: the simulated pool is four buffers deep (`sim.BUFFER_POOL`) so that a leaked grab result fails immediately instead of growing. The capacity preflight still reads this number, which is why it stays plausible. |
| `pfs_path` | a real `.pfs` | Accepted and ignored — the simulated camera has no feature file. The path must EXIST, because the window refuses to open cameras for a profile whose `.pfs` is missing. |
| `output_dir` | `data` | Where sessions land. Override it in the sidebar. |
| `trigger_pins` / `stim_safe_pins` | `[2,4,6,8,10,12]` / `[53]` | Passed to the simulated board and compiled into the sketch, so the pin guards and the safe-pin boot order are exercised. |
| `pin_capture_threads` | `false` | No hybrid-CPU behaviour to tune here. |
| `thermal_poll_s` | `0` | The simulated temperatures never move; `SimBackend.thermals` still answers in the keys the thermal watch reads. |

Baseline exposure and gain are `sim.BASELINE_EXPOSURE_US` (3000 µs) and
`sim.BASELINE_GAIN_DB` (6.0 dB) — the values the rigs record with, so the
exposure ceiling lands in the same place.

---

## Virtual time

`sim_board.SimBoard` owns the only clock in the simulated rig, and every
camera is paced from it: that is what makes the cameras trigger-synchronised
for the same reason the real ones are.

`speed` is virtual seconds per real second. Ordinals, device timestamps and
the frame rate the cameras report are all in virtual seconds, so a
20-virtual-second recording at `speed = 5` takes four real seconds and still
looks like 100 fps to every consumer.

```python
from gui_app.backends import sim_board

sim_board.reset_board(speed=5.0)     # fresh board, fault knobs cleared
```

RULE: set `speed` before a run starts, never during one. REASON: the virtual
clock is derived from the real one, so changing the factor mid-run steps the
clock.

`retrieve()` blocks until a trigger is actually due and raises
`sim.SimTimeout` when none is coming, rather than synthesising a frame per
call. That is deliberate: a backend that answered on demand would make every
timing assertion in the capture path vacuous and could never reproduce the
silence a grab loop reads as "the triggers stopped".

---

## Fault injection

### Per camera — `sim.SimFaults`

Armed as a map of camera index to `SimFaults` before the manager opens the
cameras, because `load_backend` takes only a name:

```python
from gui_app.backends import sim
from gui_app.backends.sim import SimFaults

previous = sim.set_faults({
    0: SimFaults(drop_every=50),                 # cam1 loses every 50th frame
    2: SimFaults(ppm=250e-6, jitter_s=0.002),    # cam3 drifts and arrives late
})
...
sim.set_faults(previous)                          # put it back
```

Or pass `faults=` straight to `SimBackend(...)` when the test builds its own
backend. `sim.configure(n_cameras=, width=, height=)` overrides the profile's
shape for a test that needs a rig the profile does not describe; calling it
with no arguments restores the profile's.

Every default is a healthy camera.

| Knob | Models | Seen by the host as |
|---|---|---|
| `ppm` | the camera's oscillator is not the board's (+250 ppm is the measured real offset) | device timestamps advancing at a slightly different rate — what `GrabThread._block_rate` measures |
| `jitter_s` | delivery jitter, upper bound in virtual seconds; a frame is never early | late frames, as a buffer pool produces |
| `lag_frames` | a constant delivery backlog of k triggers | frame N arriving when trigger N+k is due, with N's own block ID and timestamp |
| `drop_triggers` (set) / `drop_every` | frames lost in transmission — the camera acquired them, so a block ID IS consumed | a GAP in `blockids.npy`, `Failed_Buffer_Count` rising |
| `underrun_every` | the host's buffer pool exhausted | a gap, counted under `Buffer_Underrun_Count` — kept apart from network loss because that separation is what `stream_stats` is for |
| `failed_grab_every` | a buffer arriving with `GrabSucceeded()` false | one buffer, counted once, in the failed half |
| `ignore_every` | the camera never acquiring that trigger (also produced naturally by an exposure over the ceiling) | **NO gap**: ids stay contiguous while the camera falls behind the clock. The one loss mode that leaves a recording looking perfect — caught only by `frame_sync.check_block_id_rate` |
| `stall_at` / `stall_s` | a wedged stream | silence long enough for the grab loop's re-arm ladder, after which the block-ID counter restarts and the loop must re-base it from the timestamps |
| `blockid_start` | where the counter starts after each `StartGrabbing` | `65500` puts a 16-bit wrap a few frames into the recording |
| `padding_x` / `padding_y` / `padding_from` | row padding from frame N on | must RETIRE the camera: the (H, W) reshape would shear every row |
| `dead_after` | a link that dies at trigger N and never speaks again | silence, so the loop finds it through its stall ladder |
| `fail_after` | a transport error on every `retrieve()` after N delivered frames | retirement after `MAX_CONSEC_ERRORS` |
| `blockid_raises` | `BlockID` raising instead of answering, which the contract allows | the grab loop must survive it |
| `stats_error` | a stream-statistics read that fails | `{"error": ...}` under that key |
| `thermals` | a camera's temperature reading, or `None` for one that reports nothing | what the thermal watch polls |

### The board — `sim_board.SimBoard`

Set on the board object itself, from `sim_board.shared_board()` or the one
`reset_board()` returns:

| Knob | Models |
|---|---|
| `miss_pulses` (set of ordinals) | pulses the board never fires. A property of the BOARD, so no camera acquires them and none consumes a block ID: the recording stays aligned and simply has one trigger fewer. |
| `fps_misparse` | the sketch parsing a rate it was not sent. It then RUNS at the wrong rate as well as reporting it, which is the failure the RDY ack exists to catch. |
| `silent` | a wedged sketch that answers nothing. The host reopens the port to force a reset and finally refuses to record. |
| `ack_delay_s` | the ~1.5 s the real sketch takes on every configuration path. |
| `sketch_id` / `accept_upload(ino)` | the board's firmware identity. `None` models firmware predating the identity line. |

`starts` and `stops` count the commands honoured, so a test can prove the
board was driven rather than assume it.

---

## The offline suite

Every suite below runs with no hardware and no vendor SDK. They are plain
scripts: one `PASS`/`FAIL` line per case, non-zero exit on failure.

```
set QT_QPA_PLATFORM=offscreen
uv run python test_frame_sync.py
uv run python test_board_coverage.py
uv run python test_stim_guard.py
uv run python test_thermal_watch.py
uv run python test_mp_framesync.py
uv run python test_stim_compiler.py
uv run python test_serial_handshake.py
uv run python test_grab_failure.py
uv run python test_sim_backend.py
uv run python test_sync_router_offline.py
uv run python test_alignment.py
uv run python test_calibrate.py
uv run python test_camera_manager.py
uv run python test_cpu_affinity.py
uv run python test_cpu_encode.py
uv run python test_hardware_check.py
uv run python test_main_window_start.py
uv run python test_probe_guard.py
uv run python test_session_config.py
uv run python test_widgets.py
uv run python test_sim_gui.py
```

`QT_QPA_PLATFORM=offscreen` matters for every suite that builds a widget:
without it they need a display. `PYTHONIOENCODING=utf-8` is worth setting on
Windows, because several print non-ASCII.

`test_sync_router.py` is the one test NOT in this list: it is an NVENC smoke
test and needs the GPU.

The two that are specifically about the simulated rig:

- **`test_sim_backend.py`** drives the real `GrabThread`, `SyncEncodeRouter`
  and `FrameSyncCoordinator` against the simulated cameras and asserts on the
  GUARDS, one fault at a time. `pypylon` is made un-importable up front.
- **`test_sim_gui.py`** builds the actual `MainWindow` on `profiles/sim.yaml`
  and drives a calibration then a recording through the sidebar toggles,
  twice — once on whichever encoder the launch preflight selects, once on
  libx264. Each acquisition runs for the duration the acceptance names, 20
  virtual seconds of calibration and 40 of recording, compressed by the
  virtual clock (`SPEED`) into about 35 seconds of wall clock for the whole
  file. What it asserts:
  - both passes reach IDLE having shown exactly the dialogs the selected
    encoder implies — none on the GPU path, "Proceed?" plus "Recording
    completed with problems" on the CPU one, and the move-aside prompt on the
    second pass;
  - block IDs contiguous, equal-length and identical across the three
    cameras, with `frametimes.npy` beside them holding one entry each;
  - one mp4 per camera, as long as the trigger record;
  - that the mp4s came from the encoder the pass demands — the installed
    factory, and the libx264 note in each camera's `WARNINGS.txt`, which is
    present on the CPU path and absent on the GPU one;
  - that `alignment.load_blockids` reads the acquisition back and finds
    nothing to trim;
  - `session_metadata.json` beside each acquisition's videos, `stim_trace.csv`
    and `stim_paradigm.json`/`.ino` under an applied paradigm, and the
    moved-aside folders on the second pass.

### Known gaps

`test_sim_gui.py` marks these XFAIL and names them, so the suite passes as a
whole while they stand. Both are in files that package does not own.

- **M1-01** — `main_window.__init__` builds `CameraManager()` before the
  profile is resolved, and `CameraManager.__init__` loads the default
  `basler` backend eagerly, so `pypylon` is imported at construction whatever
  the profile names. Until it is fixed, the GUI itself needs pypylon
  installed even to run the simulated rig; the offline suites do not.
- **M1-02** — a simulated camera arms (`StartGrabbing`) before the board is
  told to start and anchors its next trigger ordinal to the pulse train that
  has just ENDED, while `SimBoard.start` restarts ordinals at 1. So from the
  second acquisition in one process on, the camera ignores every trigger
  until that stale ordinal comes round. It is silent: the frames that do
  arrive still carry block IDs from 1 and stay contiguous, so the recording
  looks perfect and is merely short at the front.

`probe_seq.py`, the interactive GUI-driving probe, has no `--profile` switch
yet, so it always runs the remembered profile. Select `sim` in the GUI once
and `uv run probe_seq.py --steps c:20,r:40` then drives the simulated rig.

---

## What the simulated rig is not

It proves the SOFTWARE's sequencing, bookkeeping and guards. It says nothing
about the things that only a rig can answer, and a green suite here is not a
substitute for any of them:

- GigE transport — packet loss, resend behaviour, jumbo frames, the switch's
  flow control, the per-camera bandwidth split;
- real NVENC session limits, real encoder throughput, and whether the CPU can
  actually sustain capture and encode together at the rig's geometry;
- thread placement on a hybrid CPU, which is measured in frames lost, not in
  assertions;
- exposure, gain, illumination and anything about image content — the
  simulated frame is a flat field with one byte of identity in its corner, so
  a calibration against it solves nothing;
- the trigger board's electrical behaviour, the laser interlock, and the
  reset window during which every GPIO floats.

Those are validated on the rig. This is what lets everything else be
validated before you get there.
