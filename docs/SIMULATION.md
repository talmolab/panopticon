# The simulated rig

Panopticon runs with no cameras, no trigger board, no camera SDK and no NVIDIA
GPU. The `sim` profile replaces the camera backend with
`gui_app/backends/sim.py` and the serial port with
`gui_app/backends/sim_board.py`. Everything else is the code the rig runs: the
grab threads, the kick-out router, the encoders, the block-ID checks, the
alignment pass and the stimulation editor's Apply. A second simulated rig,
`flir_sim`, runs the FLIR backend over a simulated Spinnaker library
([The simulated FLIR rig](#the-simulated-flir-rig)).

The simulated rig serves development and tests: a change can be shown to work
before anyone books the rig. It also stages failures that are expensive or
impossible to produce on the rig. A camera that ignores triggers, a stream that
wedges, a 16-bit block-ID wrap, row padding and a board that misreads its frame
rate are each a knob ([Fault injection](#fault-injection)). The knobs follow
the way the hardware produces each failure, such as whether a block ID is
consumed, so a test can assert on the guard that catches it.

What it cannot show is in
[What the simulated rig is not](#what-the-simulated-rig-is-not).

---

## Running the application on it

```
uv run gui.py --profile sim
```

`--profile` opens the profile and remembers it for later launches. On a
computer with no profile remembered, `uv run gui.py` opens no camera and no
serial port until you choose one; choose **sim** in the profile dropdown. The
log then shows lines like these (each line starts with a time stamp and a
thread name, left out here):

```
[acq] profile: sim
[cam1] SIM00001 640x400 Mono8
[teensy] simulated board opened
```

Record and Calibrate then run as on the rig. The cameras go into trigger mode,
the serial port carries the sketch's line protocol, the board answers
`RDY <n> <fps> <id>`, and the videos, `blockids.npy`, `frametimes.npy`,
`session_metadata.json`, `session.log`, `stim_paradigm.json` and
`stim_trace.csv` land under the output directory.

- Set the output directory first. The profile's default is the repository's
  `data/` folder, where a real session goes; point it at a scratch folder.
- Nothing is flashed. `stim_compiler.upload(ino, "sim")` hands the sketch to
  `sim_board.accept_upload` instead of running arduino-cli, so Apply takes
  milliseconds. The simulated board then reports that sketch's identity in its
  RDY line, which lets the firmware-identity check run with no board.

### Installing without the vendor SDKs

`pypylon` and `PyNvVideoCodec` are in the `rig` dependency group, so a machine
that has neither installs with

```
uv sync --no-group rig
```

Everything on this page runs in that environment. The camera manager loads a
backend only when it first opens cameras, so the window starts on a host
without pypylon. A remembered Basler profile there shows the Camera Error
dialog instead of stopping the window.

The encoder depends on the host. `profiles/sim.yaml` sets `encoder: auto`, so
the launch check picks NVENC on a machine that has it and libx264 on one that
does not. On the CPU path Panopticon asks "Proceed?" before each recording,
because the capacity check warns that it records on the CPU, and after it
shows "Recording completed with problems" with libx264's note from
`WARNINGS.txt`. The GPU path shows neither. To try the CPU path on a machine
with a GPU, set `encoder: x264` in a copy of `profiles/sim.yaml` that has a
`name` of its own; a forced libx264 shows the completion note but no
"Proceed?". [CPU_ENCODE.md](CPU_ENCODE.md) describes the CPU path.

---

## `profiles/sim.yaml`

The profile is where the simulated rig's shape is written. `sim.py` reads
`n_cameras`, `frame_width` and `frame_height` from it (`sim.profile_shape`),
because `load_backend` takes only a name. A second copy in the backend would be
what the application built, while `camera_manager` checks the cameras against
the profile. Growing the simulated rig is an edit to this file.

The fields that select or shape the simulation:

| Field | Value | What it does here |
|---|---|---|
| `camera_backend` | `sim` | Selects `SimBackend` |
| `serial_port` | `sim` | `TeensyController` opens a `SimSerial` on the shared clock instead of a port |
| `encoder` | `auto` | NVENC when the driver grants a session per camera, else libx264 |
| `n_cameras` | `3` | How many simulated cameras exist, and the count `open_all` checks |
| `frame_width` / `frame_height` | `640` / `400` | The simulated sensor size, the NV12 ring's geometry, and the size `open_all` checks |
| `frame_rate` | `100` | The recording trigger rate sent to the simulated board |
| `calibration_frame_rate` | `30` | The calibration trigger rate |
| `trigger_rate_limit` | `165` | The simulated camera applies `exposure + 1/trigger_rate_limit`, so an over-long exposure makes it ignore triggers, as on the rig |
| `realtime_encode` / `realtime_kick` | `true` / `true` | The rig's capture path |
| `kick_max_lag` | `240` | The kick-out cap; small frames keep the ring cheap |
| `max_num_buffer` | `600` | Recorded, not allocated: the simulated pool is four buffers deep (`sim.BUFFER_POOL`), so a leaked result fails at once |
| `pfs_path` | a `.pfs` | Ignored: the simulated camera has no settings file |
| `trigger_pins` / `stim_safe_pins` | `[2, 4, 6, 8, 10, 12]` / `[53]` | Compiled into the sketch, so the pin guards and the boot order run |
| `pin_capture_threads` | `false` | No hybrid-CPU placement to tune here |
| `thermal_poll_s` | `0` | The simulated temperatures never move; `SimBackend.thermals()` still answers in the thermal watch's keys |

The baseline exposure and gain are `sim.BASELINE_EXPOSURE_US` (3000 µs) and
`sim.BASELINE_GAIN_DB` (6.0 dB), the values the reference rig records with, so
the exposure ceiling falls in the same place.

---

## Virtual time

`sim_board.SimBoard` owns the only clock in the simulated rig, and every camera
is paced from it. The simulated cameras are trigger-synchronised for the same
reason the real ones are.

`speed` is virtual seconds per real second. Trigger numbers, device timestamps
and the frame rate the cameras report are all in virtual seconds, so a
20-virtual-second recording at `speed = 5` takes four real seconds and still
reads as 100 fps everywhere.

```python
from gui_app.backends import sim_board

sim_board.reset_board(speed=5.0)     # a fresh board, fault knobs cleared
```

Set `speed` before a run starts. The virtual clock is derived from the real
one, so changing `speed` during a run makes it jump.

`retrieve()` blocks until a trigger is due and raises `sim.SimTimeout` when
none is coming, instead of making a frame per call. A backend that answered on
demand would make every timing assertion in the capture path meaningless, and
could never produce the silence a grab loop reads as the triggers having
stopped.

A camera armed while the board runs starts at the next trigger, because a real
camera cannot deliver a trigger that has passed. A camera armed before the
board starts takes the first trigger of the new pulse train as block ID 1, as a
real camera armed before the first trigger does.

---

## Fault injection

### Per camera: `sim.SimFaults`

Set as a map of camera index to `SimFaults` before the manager opens the
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

A test that builds its own backend passes `faults=` to `SimBackend(...)`
instead. `sim.configure(n_cameras=, width=, height=)` overrides the profile's
shape for a test that needs another rig, and calling it with no arguments
restores the profile's. Every default is a healthy camera.

| Knob | Models | What the host sees |
|---|---|---|
| `ppm` | The camera's oscillator differing from the board's (250e-6 is the measured real offset) | Device timestamps advancing slightly off the trigger rate |
| `jitter_s` | Delivery jitter, an upper bound in virtual seconds; a frame is never early | Late frames, as a buffer pool delivers them |
| `lag_frames` | A constant backlog of k triggers | Frame N arriving when trigger N+k is due, with N's own ID and time |
| `drop_triggers` (a set), `drop_every` | Frames lost in transmission; the camera acquired them, so a block ID is consumed | A gap in `blockids.npy`, and `Failed_Buffer_Count` rising |
| `underrun_every` | The host's buffer pool running dry | A gap, counted under `Buffer_Underrun_Count` |
| `failed_grab_every` | A result arriving with `GrabSucceeded()` False | One failed buffer, counted once |
| `ignore_every` | The camera not acquiring a trigger, as an exposure over the ceiling causes | No gap: the IDs stay contiguous while the camera falls behind; only the block-ID rate check catches it |
| `stall_at`, `stall_s` | A wedged stream | Silence long enough for a re-arm, after which the counter restarts and must be re-based |
| `blockid_start` | Where the counter starts after each `StartGrabbing` | `65500` puts a 16-bit wrap a few frames in |
| `first_block_id` | The counter's numbering: 1 is the contract's, 0 a 0-based counter passed through unnormalised | With 0, the first frame reads `blockid_start - 1`, and the counter never wraps |
| `padding_x`, `padding_y`, `padding_from` | Row padding from frame N on | The camera must be retired |
| `dead_after` | A link that dies at trigger N | Silence, found by the stall ladder |
| `fail_after` | A transport error on every `retrieve()` after N frames | Retirement after ten errors in a row |
| `blockid_raises` | `BlockID` raising, which the contract allows | The grab loop must survive it |
| `stats_error` | A failing stream-statistics read | `{"error": ...}` from `stream_stats()` |
| `thermals` | A camera's temperature reading, or None for one that reports nothing | What the thermal watch reads |

### The board: `sim_board.SimBoard`

Set on the board object itself, from `sim_board.shared_board()` or from the
board `reset_board()` returns:

| Knob | Models |
|---|---|
| `miss_pulses` | Trigger numbers the board never fires. No camera acquires them, so the recording stays aligned, one trigger shorter |
| `fps_misparse` | The sketch reading a rate it was not sent; it runs at that rate and reports it, which the RDY check catches |
| `silent` | A wedged sketch that answers nothing; the host resets it, then refuses to record |
| `ack_delay_s` | The time the sketch takes to answer, about 1.5 s on the real board |
| `sketch_id`, `accept_upload(ino)` | The firmware identity; None models firmware without the identity |

`starts` and `stops` count the commands the board honoured, so a test can prove
the board was driven.

### A split rig, and a source of your own

`sim_board.SharedSimBoard` is a `SimBoard` whose clock other processes can
read. A simulated rig captured in several processes (`capture_processes`
above 0) still needs one board to fire every camera. The process that drives
the serial link holds the writer, and each capture worker attaches a reader
(`SharedSimBoard.attach(board.spec)`), which refuses start and stop. Install it
with `sim_board.use_shared_board(board)` before the cameras open;
`SimBackend.spawn_state()` refuses a split rig whose board is not shared.
`trigger_limit` ends each pulse train after that many triggers, so two runs of
the same faults fire the same triggers whenever the host sends its stop.
`miss_pulses` travels at each start, at most 64 missed pulses
(`sim_board.SHARED_MISS_PULSES`).

`sim_board.SimExternalClock` drives the board's pulse train with no serial
link, as a lab's own pulse generator does for a profile with
`trigger_source: external`: nothing the host sends starts or stops it.
`start(fps, triggers=None)` begins a train, and with `triggers` the train ends
by itself after that many pulses. A train started before the cameras are armed
is what the external source's arm check refuses.

---

## The simulated FLIR rig

`profiles/templates/flir_sim.yaml` sets `camera_backend: flir_sim`: the real
`FlirBackend` over `gui_app/backends/fake_spinc.py`, which implements every
method of the Spinnaker binding over simulated cameras with GenICam-style
nodes. The cameras are paced by the same `SimBoard`, with a virtual device
clock, and a camera receives a pulse only on the line its trigger wire is on
(`FlirFaults.wired_line`, `Line3` by default). Nothing loads a DLL or imports
an SDK.

The dropdown lists only `profiles/*.yaml`, so copy the template into
`profiles/` to run it in the window. To rehearse the FLIR probe, run
`uv run probe_flir.py --fake`, which uses the template where it is
([FLIR.md](FLIR.md#rehearse-without-cameras)).

`fake_spinc.FlirFaults` holds the knobs, keyed by serial or camera index
(`fake_spinc.set_faults()`). Besides transport faults like the sim backend's,
they cover what a real FLIR camera may do that the backend cannot know in
advance:

- whether the frame ID restarts, and from 0 or 1 (`id_restart_on_begin`,
  `id_base`);
- the timestamp unit (`ts_unit`) and the counter width (`counter_max`);
- whether the exposure maximum follows the frame rate
  (`ceiling_follows_frame_rate`);
- which nodes exist (`absent_nodes`).

Misuse whose result on hardware is undefined, such as releasing an image twice
or keeping a zero-copy view after its buffer comes round again, raises
`FakeMisuse`.

---

## What the simulated rig is not

It proves the software's sequencing, bookkeeping and guards. It says nothing
about what only a rig can answer:

- GigE transport: packet loss, resends, jumbo frames, switch flow control and
  the bandwidth each camera gets;
- real NVENC session limits and encoder throughput, and whether the CPU
  sustains capture and encoding together at the rig's frame size;
- thread placement on a hybrid CPU, which shows in frames lost, not in
  assertions;
- exposure, gain, illumination and image content: a simulated frame is a flat
  field with one byte of identity in its corner, so a calibration on it solves
  nothing;
- the trigger board's electrical behaviour, the laser interlock, and the reset
  window in which every pin floats;
- start-up timing: a simulated camera whose `retrieve()` begins before the
  board runs waits out its whole timeout. Its first frame can then arrive up to
  one retrieve timeout late (200 ms, which is 100 triggers at `speed` 5), and a
  small `kick_max_lag` can force drops at the start.

Those are checked on the rig. The simulated rig lets everything else be checked
before then.
