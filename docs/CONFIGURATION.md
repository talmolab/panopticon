# Configuration

A Panopticon rig is described by one YAML file in `profiles/`, the rig profile.
This page lists every setting a profile can hold, what each one does, when to
change it and what happens when it is wrong. It also covers the camera settings
file, the calibration board file, the sizing formulas and the checks Panopticon
makes when it loads a profile.

Panopticon runs on Windows with an NVIDIA GPU, Basler or FLIR cameras (GigE or
USB3, any number) and a hardware TTL trigger. Values marked "reference rig" come
from `profiles/3dpose.yaml`: nine Basler a2A1920-165g5m GigE cameras at 1920×1200
and 100 fps. They are one working example, and your rig's values will differ.

Contents:

- [Where settings live](#where-settings-live)
- [A complete annotated profile](#a-complete-annotated-profile)
- [Templates](#templates)
- [Configure a new rig, step by step](#configure-a-new-rig-step-by-step)
- [Parameter reference](#parameter-reference)
- [Camera settings](#camera-settings)
- [Board config](#board-config)
- [Sizing formulas](#sizing-formulas)
- [Trigger source](#trigger-source)
- [What the loader checks, and what it does not](#what-the-loader-checks-and-what-it-does-not)
- [Changing settings safely](#changing-settings-safely)

## Where settings live

| File | Holds | Read |
|---|---|---|
| `profiles/<rig>.yaml` | Panopticon's settings for one rig | At launch |
| `configs/*.pfs` | A Basler camera's own settings: frame size, pixel format, exposure, gain, packets | Each time the cameras open |
| The profile's `camera:` block | A FLIR camera's settings: exposure, gain, trigger line, Spinnaker options | Each time the cameras open |
| `configs/boards/*.yaml` | The printed calibration board | When a calibration starts, and at Solve |
| The source code | Queue depths, timeouts and other constants ([INTERNALS.md](INTERNALS.md)) | Fixed; change them by editing the code |

Panopticon reads every file in `profiles/` when it starts, so edit a profile and
then restart. The profile dropdown at the top of the sidebar lists every file
that loaded. A file with a mistake is left out, and a dialog after the window
opens names the file and the field.

This computer also remembers the last profile you opened, outside the repository,
and opens it at the next launch. `uv run gui.py --profile NAME` opens the profile
whose `name` is `NAME` instead, and remembers it. When this computer has never
opened a profile, or the one remembered or named with `--profile` does not load,
the window waits for you to choose one in the profile dropdown. Until then it
opens no camera and no serial port, programs no board and runs no hardware check.
The choice is remembered.

## A complete annotated profile

This is [`profiles/templates/basler_gige.yaml`](../profiles/templates/basler_gige.yaml),
which names every field. It loads as written. Fields that do not apply to a Basler
GigE rig are commented out.

```yaml
# Template: Basler GigE cameras on Panopticon's trigger board, with every
# profile field. docs/CONFIGURATION.md describes each one.
#
# To use it, copy it into profiles/, give it a name of your own, and replace
# every value marked SITE. Files in profiles/templates/ never appear in the
# profile dropdown.
#
# The values suit four Basler a2A1920-165g5m 5GigE cameras at 1920x1200
# Mono8 and 100 fps, the reference rig's camera.

# --- Identity ---------------------------------------------------------------
name: basler_gige                 # SITE: unique among the files in profiles/
metadata_defaults:                # SITE: pre-filled in the sidebar
  experimenter: ""
  assay: ""

# --- Cameras ----------------------------------------------------------------
camera_backend: basler            # basler | flir | sim | flir_sim
pfs_path: configs/mono8_1920x1200.pfs   # SITE: the .pfs saved from your cameras
# camera:                         # FLIR only; a Basler camera's settings are its .pfs
frame_width: 1920                 # SITE: Width in the .pfs
frame_height: 1200                # SITE: Height in the .pfs
n_cameras: 4                      # SITE
# SITE: your cameras' serial numbers, quoted, in ascending order as text.
# cam1 is the first entry.
camera_serials: ["40000001", "40000002", "40000003", "40000004"]

# --- Timing and exposure ----------------------------------------------------
frame_rate: 100                   # recording trigger rate, Hz
calibration_frame_rate: 30        # calibration trigger rate, Hz
trigger_rate_limit: 165           # SITE: the camera's maximum frame rate; never 0
calibration_exposure_us: 5000     # 0 keeps the .pfs exposure
calibration_gain_db: -1           # any negative value keeps the .pfs gain

# --- Encoding ---------------------------------------------------------------
encoder: auto                     # auto | nvenc | x264 | raw
realtime_encode: true             # false writes whole frames to disk
quality: 21                       # H.264 QP, 0 to 51; lower is larger
encode_parallel: 3                # cameras finished at once after Stop
nvenc_upload: pinned              # pinned | host
nvenc_context: shared             # shared | own

# --- Alignment and memory ---------------------------------------------------
realtime_kick: true               # align during the recording
kick_max_lag: 240                 # raise only after a test on your rig
max_num_buffer: 600               # at least kick_max_lag; 4 x 600 x 2.3 MB = 5.1 GiB
capture_processes: 0              # experimental; the window records only at 0

# --- GigE network -----------------------------------------------------------
gige_driver: socket               # socket | filter | auto
# gev_bandwidth_reserve_pct: 10   # unset keeps the .pfs value
# gev_bandwidth_reserve_accum: 4  # unset keeps the .pfs value

# --- Trigger source and stimulation -----------------------------------------
trigger_source: board             # board | external
serial_port: COM3                 # SITE: Device Manager > Ports (COM & LPT)
trigger_pins: [2, 4, 6, 8]        # SITE: every pin wired to a camera trigger input
stim_safe_pins: []                # SITE: every pin wired to a laser or LED driver

# --- Paths ------------------------------------------------------------------
output_dir: data                  # SITE: your largest, fastest drive
board_config: configs/boards/charuco_8x8_15mm.yaml   # SITE: your printed board

# --- Calibration coverage ---------------------------------------------------
calibration_min_per_cam_shared: 120
calibration_min_edge: 40
calibration_min_grid_cells: 3     # quarters of each view, of 4

# --- CPU placement (Windows, hybrid CPUs) -----------------------------------
pin_capture_threads: false        # turn on after a test shows a camera falling behind
capture_core_exclude: [0]
encoder_pcores: false
pin_encoder_threads: false

# --- Monitoring and logging -------------------------------------------------
thermal_poll_s: 20
thermal_warn_margin_c: 3
log_level: verbose                # normal | verbose | debug
```

## Templates

Templates live in `profiles/templates/`. Panopticon lists only `profiles/*.yaml`,
so a template never appears in the profile dropdown. To use one, copy it into
`profiles/`, rename the copy, set its `name`, and replace every value marked
`SITE`.

| Template | Start from it for |
|---|---|
| [`basler_gige.yaml`](../profiles/templates/basler_gige.yaml) | Basler GigE cameras. Names every field. |
| [`basler_usb3.yaml`](../profiles/templates/basler_usb3.yaml) | Basler USB3 cameras. No GigE fields. |
| [`minimal.yaml`](../profiles/templates/minimal.yaml) | A profile written from scratch: only the fields with no safe default. |
| [`flir_usb3.yaml`](../profiles/templates/flir_usb3.yaml) | FLIR USB3 cameras on Panopticon's trigger board. |
| [`flir_gige.yaml`](../profiles/templates/flir_gige.yaml) | FLIR GigE cameras with jumbo frames. |
| [`external_ttl.yaml`](../profiles/templates/external_ttl.yaml) | Cameras on your own TTL source instead of the trigger board. |
| [`flir_sim.yaml`](../profiles/templates/flir_sim.yaml) | A simulated FLIR rig: no camera, SDK or board. |

`flir_usb3.yaml`, `flir_gige.yaml` and `external_ttl.yaml` are untested on real
hardware. They load and pass every check Panopticon can make without cameras, and
each says so at the top. [FLIR.md](FLIR.md) takes a FLIR rig from install to a
first test recording.

`profiles/sim.yaml` is the simulated Basler-style rig. It ships in `profiles/`
itself, because the simulated backend reads its camera count and frame size from
that file. [SIMULATION.md](SIMULATION.md) describes it.

`minimal.yaml` sets the fields whose defaults suit no other rig. `n_cameras`
defaults to 0, which turns the count check off, and `camera_serials` to none,
which names the cameras in enumeration order. `serial_port`, `pfs_path`,
`output_dir` and `board_config` default to empty. `trigger_pins` and
`stim_safe_pins` default to the reference rig's wiring, and `trigger_rate_limit`,
the frame size and the frame rate to its camera.

## Configure a new rig, step by step

1. Write down the frame size, frame rate and camera count. Work out the network
   load, RAM, disk and [NVENC sessions](#nvenc-sessions) with the
   [sizing formulas](#sizing-formulas), and stop if a number does not fit your
   hardware.
2. Copy a [template](#templates) into `profiles/<your_rig>.yaml` and set `name`.
3. Make the camera settings. Basler: set the frame size, Mono8, exposure and gain
   in pylon Viewer and save a `.pfs` into `configs/` ([Basler cameras: the .pfs
   file](#basler-cameras-the-pfs-file)), then point `pfs_path` at it. FLIR: fill
   in the `camera:` block ([FLIR cameras: the camera: block](#flir-cameras-the-camera-block)).
4. Set `frame_width` and `frame_height` to that frame size, and `frame_rate` and
   `calibration_frame_rate`. On Basler, set `trigger_rate_limit` to the camera's
   maximum frame rate at that frame size.
5. Name the cameras. `uv run probe_network.py` lists every GigE camera with its
   serial number, and pylon Viewer shows USB3 ones. On FLIR,
   `uv run probe_flir.py --list` prints two lines to paste. Set `n_cameras` and
   `camera_serials`: quoted, in ascending order as text.
6. Wire the trigger and declare it: `serial_port`, every pin that drives a camera
   in `trigger_pins`, and every pin wired to a laser or LED driver in
   `stim_safe_pins` (or `[]`). With a trigger source of your own, set
   `trigger_source: external` and leave those three out
   ([Your own TTL source](#your-own-ttl-source)).
7. Leave `encoder: auto`, `realtime_encode: true` and `realtime_kick: true`.
8. Size memory. Start with `kick_max_lag: 240` and `max_num_buffer` at least that,
   and check the [RAM](#ram) total against the computer. Raise either only after a
   test on your rig.
9. On a GigE Basler rig, keep `gige_driver: socket` and leave the two `gev_`
   fields unset.
10. Describe your printed board in `configs/boards/<board>.yaml`
    ([Board config](#board-config)), point `board_config` at it, and keep the
    coverage thresholds at their defaults for the first calibration.
11. Leave the four CPU placement fields at their defaults unless the CPU is a
    hybrid Intel part and a test recording shows a camera falling behind.
12. Open the profile with `uv run gui.py --profile NAME`. Opening it resets the
    board on [serial_port](#serial_port), which floats every pin, so read the
    [laser warning](INSTALLATION.md#step-8--flash-the-trigger-firmware) first.
    A profile that fails the [loader's checks](#what-the-loader-checks-and-what-it-does-not)
    is not in the dropdown.
13. Record a one-minute test. Check that every camera's video has the same number
    of frames, that the recording folder has no `WARNINGS.txt`, and that no
    `[camN] exposure=` line in the log says `CLAMPED`. On a computer that has
    never run Panopticon, run the `sim` profile first ([SIMULATION.md](SIMULATION.md)).

## Parameter reference

Each entry gives the type, the default (what a profile that leaves the field out
gets) and the reference rig's value ("not set" means the reference profile leaves
it out). Relative paths are read from the repository folder, the one that holds
`gui.py`.

### Identity

#### `name`

Text. Default: the file name without `.yaml`. Reference rig: `3dpose`.

- Does: Names the profile in the profile dropdown and in `session_metadata.json`
  (`rig`).
- Change when: Always, when you copy a profile or a template. Use a name no other
  file in `profiles/` uses.
- Goes wrong: Two files with the same name both appear in the dropdown under one
  label, and the remembered choice opens the first of them in file-name order.
  After a rename, the next launch does not find the
  [remembered](#where-settings-live) name and asks you to choose a profile. An
  empty name loads, but Panopticon uses an empty name to mean that no profile is
  chosen.

#### `metadata_defaults`

Mapping. Default `{experimenter: "", assay: ""}`. Reference rig:
`{experimenter: IT, assay: open_field}`.

- Does: Fills the sidebar's metadata fields for a new session. The keys may be
  `experimenter`, `assay`, `cohort`, `cage` and `notes`. What the fields hold
  when an acquisition ends goes into `session_metadata.json`.
- Change when: Set your own operator and assay when you copy a shipped profile.
- Goes wrong: A copy of `profiles/3dpose.yaml` keeps `experimenter: IT`, so every
  session nobody edits is attributed to that operator. Any other key is refused
  when the profile loads. A field you have typed into keeps your text when you
  switch profiles.

### Cameras

#### `camera_backend`

Text. Default `basler`. Reference rig: `basler`.

- Does: Picks the module that drives the cameras. `basler` runs Basler GigE and
  USB3 cameras through pypylon. `flir` runs FLIR GigE and USB3 cameras through
  the Spinnaker SDK. `sim` and `flir_sim` run simulated cameras, the second
  through the FLIR backend.
- Change when: For a FLIR rig ([FLIR.md](FLIR.md)), or to run with no hardware.
- Goes wrong: Any other name is refused when the profile loads. Without the
  vendor SDK, opening the cameras fails with a message that says what to install.
  A profile names one backend, so a rig cannot mix Basler and FLIR cameras.

#### `pfs_path`

Text, a path. Default `""`. Reference rig: `configs/mono8_1920x1200.pfs`.

- Does: Basler only. Names the camera settings file loaded into every camera each
  time the cameras open ([Basler cameras: the .pfs file](#basler-cameras-the-pfs-file)).
- Change when: When you make settings for another camera model, or change
  exposure, gain or the frame size.
- Goes wrong: With no file set, or a missing one, the cameras do not open and a
  dialog names the file. The load skips any feature the camera does not have,
  then Panopticon checks the pixel format and frame size. A FLIR profile refuses
  a non-empty `pfs_path`. The simulated backend ignores it.

#### `camera`

Mapping. Default `null` (no block). Reference rig: not set.

- Does: Holds the camera settings of a backend with no settings file of its own:
  exposure, gain, trigger line, frame offset and the Spinnaker options. Its keys
  are listed under [FLIR cameras: the camera: block](#flir-cameras-the-camera-block).
- Change when: Required for `flir` and `flir_sim`. The `sim` backend accepts a
  block and stores it without applying it.
- Goes wrong: A `basler` profile refuses the block, because Basler cameras take
  their settings from the `.pfs`. A `flir` or `flir_sim` profile without one is
  refused.

#### `frame_width`

Integer, pixels. Default `1920`. Reference rig: `1920`.

- Does: The width of the frame every camera records. It sizes the frame buffers
  and the RAM and disk checks. On Basler it must equal `Width` in the `.pfs`; on
  FLIR the backend writes it to each camera.
- Change when: When you change the frame size.
- Goes wrong: An odd width is refused when the profile loads, because the
  encoders' NV12 frames need even sizes. A width that differs from what the
  cameras report refuses the camera open, and Calibrate and Record are refused
  while the two disagree (`The profile records`). FLIR also refuses a width
  outside the camera's range or off its increment.

#### `frame_height`

Integer, pixels. Default `1200`. Reference rig: `1200`.

- Does: The height of the frame every camera records, used as
  [frame_width](#frame_width) is.
- Change when: When you change the frame size.
- Goes wrong: As for `frame_width`.

#### `n_cameras`

Integer. Default `0`. Reference rig: `9`.

- Does: The number of cameras that must be present before any camera opens. `0`
  turns the count off. With `camera_serials` set, cameras the list does not name
  are not counted.
- Change when: When you add or remove a camera. Read
  [camera_serials](#camera_serials) first.
- Goes wrong: At `0` a rig with one camera missing opens the others. Without
  `camera_serials`, a missing camera then renames every camera after it, and the
  calibration attaches to the wrong cameras. A count that differs refuses the
  open (`Expected 9 cameras but 8 are available to open`).

#### `camera_serials`

List of quoted text, or null. Default `null`. Reference rig: not set.

- Does: The serial numbers of the cameras the rig is made of. cam1 is the first
  entry, cam2 the second, and so on. Panopticon opens only these cameras, logs
  and ignores any other it finds, and refuses to open when a listed camera is
  missing.
- Change when: Set it on every rig whose calibration you keep. After replacing a
  camera, update the list and calibrate again.
- Goes wrong: Without it, cameras are named in serial-number order as they
  enumerate, so a camera that fails to appear renames every camera after it. The
  loader refuses an unquoted serial, because YAML reads a leading-zero number as
  octal. It also refuses a serial listed twice, a list out of ascending text
  order, and a length that differs from a nonzero `n_cameras`. Text order puts
  `"10"` before `"9"`; the refusal gives the order to use.

### Timing and exposure

#### `frame_rate`

Integer, Hz. Default `100`. Reference rig: `100`.

- Does: The trigger rate of a recording. It also sets the keyframe interval (one
  per second) and the rate the block-ID check compares each camera with.
- Change when: When the experiment needs another rate.
- Goes wrong: A higher rate shortens the [exposure ceiling](#exposure-ceiling)
  and raises network load, disk use and CPU load in proportion. On `basler` and
  `sim`, a rate at or above `trigger_rate_limit` is refused when the profile
  loads. With `trigger_source: external`, set it to your source's rate
  ([Your own TTL source](#your-own-ttl-source)).

#### `calibration_frame_rate`

Integer, Hz. Default `30`. Reference rig: `30`.

- Does: The trigger rate while calibrating. Its longer period leaves room for a
  longer calibration exposure.
- Change when: Rarely. Lower it if the board is too dark with the calibration
  exposure at its ceiling.
- Goes wrong: A higher rate gives a hand-moved board nothing and shortens the
  exposure ceiling. The `trigger_rate_limit` rule of `frame_rate` applies here
  too. On FLIR, each camera's limits at this rate are checked when calibration
  starts.

#### `trigger_rate_limit`

Number, frames per second. Default `165`. Reference rig: `165`.

- Does: Basler and sim only. Panopticon writes it to each camera's
  `AcquisitionFrameRate` while the cameras are triggered. The camera then needs
  `exposure + 1/trigger_rate_limit` between frames, which sets the
  [exposure ceiling](#exposure-ceiling), and it spaces each camera's readout onto
  the network.
- Change when: Set it to your camera's maximum frame rate at your frame size, from
  its data sheet or the resulting frame rate pylon Viewer shows. The default is
  the a2A1920-165g5m's maximum.
- Goes wrong: `0` turns the limiter off. Every camera then sends its frame the
  moment the trigger arrives, and with six of the reference rig's cameras 8 to
  15% of frames were lost on the network ([HISTORY.md](HISTORY.md)). A value
  above the camera's real maximum makes the computed ceiling too long. An
  exposure Panopticon accepts can then make the camera skip triggers
  ([exposure ceiling](#exposure-ceiling)). A FLIR profile refuses
  the field; FLIR pacing is [camera.flir.link_throughput_limit](#cameraflirlink_throughput_limit).

#### `calibration_exposure_us`

Number, microseconds. Default `0`. Reference rig: `5000`.

- Does: The exposure used while calibrating, and only then. `0` keeps the
  recording exposure (the `.pfs` value on Basler, `camera.exposure_us` on FLIR).
  Panopticon caps it at the [exposure limit](#exposure-ceiling) for
  `calibration_frame_rate`, and logs `CLAMPED` when it lowers it. Each recording
  restores the recording exposure, so a calibration exposure cannot reach a
  recording.
- Change when: When the board is too dark to detect while calibrating.
- Goes wrong: Too long, and a moving board blurs until its corners are no longer
  found. At 30 fps, motion blur limits the exposure long before the ceiling does,
  so move the board slowly. A negative value is refused when the profile loads.

#### `calibration_gain_db`

Number, dB. Default `-1`. Reference rig: `-1`.

- Does: The gain used while calibrating, and only then. Any negative value keeps
  the recording gain; `0` is a real gain of 0 dB.
- Change when: After you have added light and raised `calibration_exposure_us`.
- Goes wrong: Each +6 dB doubles the noise along with the signal.

### Encoding

#### `encoder`

Text. Default `auto`. Reference rig: `auto`.

- Does: Picks the H.264 encoder. Panopticon decides at launch and again at each
  Record and Calibrate. `auto` uses NVENC when the driver grants a session per
  camera, else libx264 on the CPU when the launch benchmark says the CPU keeps
  up, and otherwise refuses the start. `nvenc` and `x264` force one path; `x264`
  also moves the post-session re-encodes to the CPU. `raw` is accepted only with
  `realtime_encode: false`. When the session limit cannot be measured, `auto` and
  `nvenc` keep NVENC, and the start asks whether to proceed
  (`The NVENC session cap could not be probed`).
- Change when: `x264` on a computer without a usable NVIDIA GPU. `nvenc` when a
  refusal suits you better than encoding on the CPU.
- Goes wrong: `raw` with `realtime_encode: true` is refused at Record. On libx264
  the encoders compete with capture for CPU cores, so read `WARNINGS.txt` after a
  test recording. [CPU_ENCODE.md](CPU_ENCODE.md) describes the CPU path.

#### `realtime_encode`

True or false. Default `true`. Reference rig: `true`.

- Does: `true` encodes H.264 while recording. `false` writes every frame whole to
  `raw.bin` and encodes after Stop.
- Change when: Set `false` only when the computer cannot encode every camera live
  and the disk can take the [raw rate](#disk).
- Goes wrong: `false` writes about 500 times the H.264 volume ([raw rate](#disk)).
  With `false`, `realtime_kick` has no effect, and the videos are aligned after
  the encode as they are with `realtime_kick: false`.

#### `quality`

Integer, 0 to 51. Default `21`. Reference rig: `21`.

- Does: The H.264 quantiser (QP) every encoder uses. Lower gives higher quality
  and larger files ([frame size at 21](#disk)). `session_metadata.json` records
  it, and `2_align.py` and `0_encode.py` reuse it.
- Change when: To trade file size against image detail.
- Goes wrong: A value outside 0 to 51 is refused when the profile loads. File size
  depends on the scene as well as the QP, so measure it on a test recording.

#### `encode_parallel`

Integer. Default `3`. Reference rig: `3`.

- Does: How many cameras are finished at once after Stop. In real-time mode each
  job copies one camera's H.264 stream into its mp4. In raw mode each job encodes
  one camera's `raw.bin`. Post-session alignment also re-encodes that many cameras
  at a time.
- Change when: Raise it to finish raw-mode encodes sooner on a GPU with sessions
  to spare.
- Goes wrong: Each raw-mode encode and each alignment re-encode holds an NVENC
  session while it runs (or CPU cores on libx264), so keep the value within what
  the driver grants. The real-time copy uses no session.

#### `nvenc_upload`

Text. Default `pinned`. Reference rig: not set.

- Does: How each frame reaches the NVENC encoder. `pinned` copies the frame into
  page-locked memory without holding Python's GIL, and the GPU reads it from
  there. `host` hands the frame to PyNvVideoCodec, which copies it to the GPU
  while it holds the GIL.
- Change when: Leave `pinned`. `host` exists for comparison runs.
- Goes wrong: With `host`, the GIL-held copy makes the grab threads wait, and on
  the reference rig one camera then drifts behind the others. `pinned` costs about
  1 ms more CPU per frame per camera and 4 × width × height × 1.5 bytes of
  page-locked memory per camera. At launch Panopticon checks that PyNvVideoCodec
  copies on the encoder's stream; if the check fails, the session uses `host` and
  the log says why. An encoder that cannot get page-locked memory uses `host` by
  itself, and `WARNINGS.txt` says so (`real-time encode uses the host upload`).
  `session_metadata.json` records what each acquisition used
  (`nvenc_upload_used`). The setting does nothing on libx264.

#### `nvenc_context`

Text. Default `shared`. Reference rig: not set.

- Does: The CUDA context the pinned upload runs in. `shared` runs every encoder in
  the GPU's primary context. `own` gives each encoder a context of its own.
- Change when: Leave `shared`. On the reference rig the two lagged alike, and
  `own` used about 225 MiB more GPU memory per camera.
- Goes wrong: `own` with `nvenc_upload: host` is refused when the profile loads.
  At launch Panopticon measures what a context costs on this GPU; when the free
  memory does not cover one per camera, it uses `shared` and logs a warning.

### Alignment and memory

#### `realtime_kick`

True or false. Default `true`. Reference rig: `true`.

- Does: `true` drops, during the recording, every trigger that some camera
  missed, so the videos come out aligned. When more than 0.5% of the triggers are
  dropped, the post-session dialog and `WARNINGS.txt` give the recording's
  `Effective frame rate`, and `session_metadata.json` holds the counts
  (`kickout`). `false` records every frame each camera caught and aligns the
  videos after the recording with a full re-encode.
- Change when: Leave `true`. Set `false` only to compare with post-session
  alignment.
- Goes wrong: With `false`, the post-session pass re-encodes every video, which
  takes longer and needs NVENC sessions or CPU time. The field applies only with
  `realtime_encode: true`.

#### `kick_max_lag`

Integer, frames. Default `240`. Reference rig: `480`.

- Does: How many frames the kick-out waits for a camera that falls behind before
  it drops triggers the other cameras caught (a forced drop). It also sizes each
  camera's NV12 ring: `kick_max_lag + 264` frames ([RAM](#ram)).
- Change when: Only after a test on your rig. Lower it to save RAM if no camera
  ever falls behind.
- Goes wrong: Too low, and a camera that falls behind reaches the limit and forces
  drops. Too high, and the rings outgrow the free RAM, so Record refuses to
  start. A value above `max_num_buffer` is refused in kick-out mode.
  [HISTORY.md](HISTORY.md) has the tests behind the reference rig's value.

#### `max_num_buffer`

Integer. Default `1000`. Reference rig: `600`.

- Does: The driver buffers queued per camera (`MaxNumBuffer` on Basler,
  `StreamBufferCountManual` on FLIR). A camera that falls behind keeps its backlog
  here. The pool takes `n_cameras × max_num_buffer × width × height` bytes
  ([RAM](#ram)).
- Change when: Lower it when the RAM check refuses a start. Raise it when
  `buffers_underrun` in the recording's `session_metadata.json`
  (`camera_stream_stats`) is above 0.
- Goes wrong: Below `kick_max_lag`, the pool runs dry while the kick-out still
  waits, and a camera that could have caught up loses frames; the loader refuses
  that in kick-out mode. Too high, and the RAM check refuses to start. A deep pool
  also hides a grab loop that is slightly too slow, because each frame retrieved
  is older than the last. FLIR refuses a value above the camera's
  `StreamBufferCountMax`.

#### `capture_processes`

Integer. Default `0`. Reference rig: not set.

- Does: Experimental. The number of worker processes the cameras would be
  captured in, each taking a contiguous group of cameras. `0` captures every
  camera in Panopticon's own process. `session_metadata.json` records the value
  asked for (`capture_processes`) and what ran (`capture_processes_used`).
- Change when: Keep `0`. The Panopticon window records only with
  `capture_processes: 0`.
- Goes wrong: For a profile above `0`, the window opens no camera and a dialog
  says why. The loader refuses a negative value, and a nonzero value without
  `realtime_encode: true` and `realtime_kick: true`. It also refuses more
  processes than cameras when the profile states the count, through `n_cameras`
  or `camera_serials`.

### GigE network

#### `gige_driver`

Text. Default `socket`. Reference rig: `socket`.

- Does: Basler GigE only: pylon's receive driver. `socket` runs in user space,
  asks the camera to resend lost packets, and sets the largest socket buffer the
  driver allows. `filter` is pylon's in-kernel driver: less CPU, but with default
  resend settings it discards a frame that lost a packet. `auto` keeps pylon's
  default. USB3 cameras ignore it.
- Change when: Keep `socket` unless a test on your rig favours another driver.
- Goes wrong: `filter` lost about 23% of frames with six of the reference rig's
  cameras at 100 fps. Any other value is refused when the profile loads. A FLIR
  profile refuses any value but `auto`; its equivalent is
  [camera.flir.stream_mode](#cameraflirstream_mode).

#### `gev_bandwidth_reserve_pct`

Number from 0 to 100, or null. Default `null`. Reference rig: not set.

- Does: Basler GigE only. Writes `GevSCBWR` when the cameras open: the share of
  each camera's link bandwidth held back for packet resends. Unset keeps the
  `.pfs` value.
- Change when: Only to test resend headroom on a GigE rig.
- Goes wrong: More reserve lowers the bandwidth each camera is assigned. A camera
  without the node, such as any USB3 camera, fails the whole open with a message
  naming `GevSCBWR`: remove the field. A FLIR profile refuses it.

#### `gev_bandwidth_reserve_accum`

Integer 0 or more, or null. Default `null`. Reference rig: not set.

- Does: Basler GigE only. Writes `GevSCBWRA` when the cameras open: how many
  reserve periods may pool for a burst of resends. Unset keeps the `.pfs` value.
- Change when: Only together with `gev_bandwidth_reserve_pct`.
- Goes wrong: A camera without the node fails the open, as above. A negative value
  is refused when the profile loads, and a FLIR profile refuses the field.

### Trigger source and stimulation

#### `trigger_source`

Text. Default `board`. Reference rig: not set.

- Does: What fires the cameras' trigger inputs. `board` is Panopticon's trigger
  board on `serial_port`, which also runs stimulation. `external` is a TTL source
  you run, such as a pulse generator or a DAQ, and Panopticon opens no serial
  port. [Trigger source](#trigger-source) describes both.
- Change when: Set `external` for a trigger source of your own.
- Goes wrong: A source already running when the cameras arm refuses the
  recording. [Your own TTL source](#your-own-ttl-source) lists what else
  `external` changes.

#### `serial_port`

Text. Default `""`. Reference rig: `COM3`.

- Does: The trigger board's serial port, `COMn` on Windows (Device Manager, under
  Ports (COM & LPT)). `sim` selects the simulated board. Opening the profile
  resets the device on this port and, unless it already carries it, programs it
  with the recording-only sketch: camera triggers and no stimulation. Every pin
  floats during the reset, so read the
  [laser warning](INSTALLATION.md#step-8--flash-the-trigger-firmware) before you
  open the profile.
- Change when: For every new computer or board.
- Goes wrong: A wrong port resets whatever device is on it, and can reprogram
  it, so check the port before you open the profile. Left empty, nothing is
  programmed at launch, and Calibrate and Record are refused because the board
  does not open.
  A port another program holds, such as the Arduino Serial Monitor, fails the same
  way. An external-source profile refuses the field.

#### `trigger_pins`

List of integers. Default `[2, 4, 6, 8, 10, 12]`. Reference rig: `[2, 4, 6, 8, 10, 12]`.

- Does: Every board pin wired to a camera's trigger input. The board switches all
  of them in one step, so their order carries no meaning, and one pin may drive
  several cameras.
- Change when: When you rewire the trigger lines.
- Goes wrong: A camera on a pin the list leaves out receives no triggers.
  Panopticon retires it after a few seconds (`no frame received since the
  triggers started`) and records the others. The loader refuses pins 0 and 1 (the
  board's serial link), a pin also in `stim_safe_pins`, and a pin listed twice. It
  accepts an empty list and pins the board does not have. One pin driving more
  inputs than its current rating allows (about 20 mA on an ATmega2560) makes a
  camera miss triggers without a gap in `blockids.npy`.

#### `stim_safe_pins`

List of integers. Default `[53]`. Reference rig: `[53]`.

- Does: Pins the board's sketch drives low as its first step after every reset,
  before it waits for Panopticon, so a laser or LED driver wired there does not
  read a floating pin as on. Pins a stimulation paradigm uses are added to the
  list.
- Change when: List every pin your stimulation hardware is wired to, or `[]` if
  there is none.
- Goes wrong: The default `[53]` is the reference rig's laser pin and protects
  nothing on other wiring. A stimulus pin missing from the list floats while the
  board waits for Panopticon, and a powered laser driver reads that as on. Every
  pin still floats for about a second during the reset itself: read the warning in
  [INSTALLATION.md step 8](INSTALLATION.md#step-8--flash-the-trigger-firmware)
  before you connect a laser. The loader refuses a pin also in `trigger_pins`, but
  not pins 0 or 1. An external-source profile refuses a non-empty list.

### Paths

#### `output_dir`

Text, a path. Default `""`. Reference rig: `data`.

- Does: The folder sessions are written under, as
  `<output_dir>\<date>\<mouse_1>_<mouse_2>\` ([WORKFLOW.md](WORKFLOW.md#paths-and-names)).
  The sidebar's folder button overrides it until you restart, or switch to a
  profile that sets `output_dir`.
- Change when: Point it at your largest, fastest drive.
- Goes wrong: The disk check at Record measures the folder the button shows. Left
  empty, the sidebar keeps the folder it already shows, which can be another
  profile's `output_dir`, a folder picked by hand, or its default, `data` in the
  repository folder. Set it in every profile.

#### `board_config`

Text, a path. Default `""`. Reference rig: `configs/boards/charuco_8x8_15mm.yaml`.

- Does: The file that describes your printed ChArUco board ([Board config](#board-config)).
  The coverage display during calibration and Solve both read it.
- Change when: When you print a different board.
- Goes wrong: A missing file hides the coverage display and refuses Solve. A file
  that describes another board finds no corners (wrong layout) or scales every 3D
  coordinate (wrong `square_length`).

### Calibration coverage

These three decide when the coverage display shows READY during a calibration.
You can stop before READY appears. The recording is still valid, and Solve
reports whether it had enough views.

#### `calibration_min_per_cam_shared`

Integer. Default `120`. Reference rig: `120`.

- Does: How many detection ticks each camera needs in which it saw the board
  together with at least one other camera.
- Change when: Raise it if calibrations come out marginal; lower it if waving
  takes too long while the per-pair chart in `reprojection_error_histogram.png`
  stays good.
- Goes wrong: Too high wastes time in the arena, because the solve uses at most 60
  frames per camera for its intrinsics. Too low gives a marginal solve. The loader
  does not check the value.

#### `calibration_min_edge`

Integer. Default `40`. Reference rig: `20`.

- Does: How many shared detections make a camera pair count as connected. READY
  needs the connected pairs to join every camera into one group.
- Change when: Lower it when two groups of cameras will not join. On a large rig
  this decides most of the waving time.
- Goes wrong: Too high, and a rig whose cameras face each other never reaches
  READY, because the board is one-sided. Too low, and a weak pair joins the chain
  the solve walks. The loader does not check the value.

#### `calibration_min_grid_cells`

Integer, 0 to 4. Default `3`. Reference rig: `3`.

- Does: How many of the four quarters of its own view each camera must see the
  board in.
- Change when: Relax it last. It keeps the board from being waved in one spot,
  which gives confident but badly conditioned intrinsics.
- Goes wrong: The loader accepts a value above 4, which no camera can meet, so
  READY never appears.

### CPU placement

These four act on Windows with a hybrid CPU (performance and efficiency cores).
Elsewhere they do nothing.

#### `pin_capture_threads`

True or false. Default `false`. Reference rig: `true`.

- Does: Pins each camera's grab thread to a performance core of its own and raises
  its priority.
- Change when: Turn it on for a hybrid Intel CPU after a test recording shows a
  camera falling behind.
- Goes wrong: Off on a hybrid CPU, Windows may run a grab thread on an efficiency
  core, and that camera falls behind; which camera changes from launch to launch.
  With more cameras than cores in the pool, the extra grab threads share the
  pool's cores.

#### `capture_core_exclude`

List of integers. Default `[0]`. Reference rig: `[0, 1]`.

- Does: The logical CPUs pinned grab threads stay off, normally the cores that
  handle the network card's interrupts. It applies only with
  `pin_capture_threads: true`.
- Change when: Set it to the cores your network card's receive work lands on,
  measured with a DPC trace. With six of the reference rig's cameras, CPUs 0 and
  1 carried about 46% of that work.
- Goes wrong: A list that excludes every performance core falls back to all of
  them. The log line `[rig] capture core pool` shows the pool in use.

#### `encoder_pcores`

True or false. Default `false`. Reference rig: `false`.

- Does: Confines the encoder threads to the performance cores.
- Change when: Leave it off, unless the rig has more cameras than performance
  cores and a test shows a gain.
- Goes wrong: On the reference rig it raised the grab threads' copy time from 0.7
  to 2.8 ms. With `pin_encoder_threads: true` as well, that field applies and this
  one is ignored.

#### `pin_encoder_threads`

True or false. Default `false`. Reference rig: not set.

- Does: Confines the encoder threads to the efficiency cores. Windows moves them
  among those cores as it likes.
- Change when: Leave it off, unless the rig has more cameras than performance
  cores and a test shows a gain.
- Goes wrong: On the reference rig it measured worse than unpinned encoders: each
  grab thread's processing time per frame rose from 2.19 to 2.53 ms.

### Monitoring and logging

#### `thermal_poll_s`

Number, seconds. Default `20`. Reference rig: `20`.

- Does: How often the cameras' temperatures are read during a recording or a
  calibration, for the live warning. `0` or less turns the watch off. The
  temperatures read at Stop go into `session_metadata.json` either way.
- Change when: Leave it on. The `sim` profile sets `0`, because its temperatures
  never change.
- Goes wrong: With the watch off, a camera that reaches its shutdown temperature
  stops delivering during the recording, and nothing warns you before the end.

#### `thermal_warn_margin_c`

Number, °C. Default `3.0`. Reference rig: `2.0`.

- Does: The live warning starts this many degrees below the shutdown temperature
  each camera reports. The reference rig's cameras report 81 °C, so it warns at
  79 °C. A camera in its own over-temperature state always warns. A camera's
  Critical status alone does not.
- Change when: Raise it if your cameras heat quickly and you need more time to
  act. The thresholds themselves always come from the camera.
- Goes wrong: The loader refuses 0 or less. A camera that reports no shutdown
  temperature is judged by its own temperature status, Critical included; one
  that reports neither cannot warn, and the log says which, once per camera. A
  camera that reaches its shutdown point is named in `WARNINGS.txt` and the
  post-session dialog. A warning below that point reaches them only when the
  recording lost frames.

#### `log_level`

Text. Default `verbose`. Reference rig: not set.

- Does: How much the log says. At every level each line starts with the time, to
  the millisecond, and the thread that printed it. A header at launch and at each
  acquisition start records the computer, GPU, driver, NVENC session limit,
  package versions, the whole profile and each camera. `verbose` adds each camera
  setting written, with the value the camera reads back, each acquisition step,
  and a per-camera summary at Stop. `debug` adds more detail on the same cold
  paths, such as every `.pfs` feature read back. One log per launch goes to
  `logs\` in the repository folder, and each recording and calibration folder
  gets its part as `session.log`. Capture worker processes log at the same
  level. `uv run probe_flir.py` logs at `debug` when the profile sets it, and
  at `verbose` otherwise.
- Change when: Leave `verbose` while you bring a rig up: its read-backs are what a
  bug report needs. `normal` gives a shorter log.
- Goes wrong: Any other value is refused when the profile loads. No level logs
  anything per frame, and printing never makes a capture thread wait for the disk.
  When the log queue is full, lines are dropped and counted
  (`log lines dropped`).

## Camera settings

### Basler cameras: the .pfs file

A `.pfs` is pylon's settings file: one camera feature per line, as
`Name<TAB>value`, or `Name<TAB>{Selector=Entry}<TAB>value` for a feature that
belongs to a selector. Panopticon loads the same file into every camera each
time the cameras open. It is the only source of a recording's exposure and
gain.

To make one, open one camera in pylon Viewer, set the features below, and save
the camera's features into `configs/`. Then point `pfs_path` at the file.

| Feature | Reference value | What Panopticon needs |
|---|---|---|
| `PixelFormat` | `Mono8` | Required. Opening refuses any other format. |
| `Width`, `Height` | 1920, 1200 | Equal to `frame_width` and `frame_height`, on every camera. |
| `OffsetX`, `OffsetY` | 8, 8 | Where the frame sits on the sensor. |
| `ExposureAuto`, `GainAuto` | `Off` | Off, so each camera keeps the exposure and gain you set. |
| `ExposureTime` | 3000 µs | Under the [exposure ceiling](#exposure-ceiling). |
| `Gain` | 6 dB | Add light before gain. |
| `LineInverter` on `Line1` | 0 | Which physical edge counts as rising. |
| `GevSCPSPacketSize` | 9000 | GigE: only when every device in the path passes jumbo frames. |
| `GevSCPD` | 10000 | GigE: the delay between one camera's packets. Retune it for your network. |

What Panopticon writes over the file:

- At every acquisition start it sets `TriggerSelector` to `FrameStart`,
  `TriggerMode` to `On`, `TriggerSource` to `Line1` and `TriggerActivation` to
  `RisingEdge`. Wire each camera's trigger to `Line1`.
- In trigger mode it writes `AcquisitionFrameRate` from `trigger_rate_limit`.
- It sets `MaxNumBuffer` from `max_num_buffer`, and `GevSCBWR` and `GevSCBWRA`
  when the profile sets the `gev_` fields.
- At every acquisition start it writes back the exposure and gain the file set,
  capped at the exposure ceiling, or the calibration values while calibrating.
  Each camera's `[camN] exposure=` log line shows what it got.

The load skips any feature the camera does not have, so a file saved on one camera
of a model loads on the others. `LineInverter` differs between the two shipped
files: `configs/mono8_mono.pfs` inverts `Line1`, so the 3dface rig exposes on
the opposite physical edge to the 3dpose rig. Judge an exposure change on a test
recording, because the preview runs untriggered at 30 fps, where an exposure over
the recording ceiling still looks fine.

### FLIR cameras: the camera: block

A FLIR camera has no settings file, so a `flir` or `flir_sim` profile states its
camera settings in a `camera:` block. Settings the block does not name come from
the user set named by `camera.flir.user_set`, the camera's factory `Default`
unless you change it. Panopticon loads that user set every time it opens a
camera, so a change made in SpinView does not reach a recording unless you save
it into the user set the profile names.

The loader checks each key's type and allowed values, and refuses an unknown key
at every level with the closest known key as a suggestion. Opening the cameras
then checks each value against what each camera reports, and refuses one outside
the camera's range with the camera, the setting and the range in the message.
[FLIR.md](FLIR.md#when-something-refuses) lists those refusals and what to do.

```yaml
camera:
  pixel_format: Mono8
  exposure_us: 3000
  gain_db: 6.0
  offset_x: center
  offset_y: center
  trigger:
    line: Line3
    activation: RisingEdge
    overlap: ReadOut
    delay_us: null
  per_camera:
    "21039792": {exposure_us: 2500}
  flir:
    user_set: Default
    gamma_enable: false
    black_level: null
    adc_bit_depth: null
    link_throughput_limit: auto
    packet_size: null
    packet_delay: null
    stream_mode: auto
    extended_ids: true
    block_id_source: auto
    timestamp_source: auto
    sdk_dir: null
```

#### `camera.exposure_us`

Number, microseconds. Required.

- Does: The recording exposure of every camera. `per_camera` can give one camera
  another value.
- Change when: To match your lighting. Add light before you raise gain.
- Goes wrong: The loader refuses 0 or less, and an exposure as long as the
  `frame_rate` trigger period. Opening the cameras refuses an exposure above
  each camera's own [exposure limit](#exposure-ceiling) at `frame_rate`
  (`is above what this camera can expose`).

#### `camera.gain_db`

Number, dB. Required.

- Does: The recording gain of every camera.
- Change when: After illumination and exposure.
- Goes wrong: A negative value is refused when the profile loads, and one outside
  the camera's `Gain` range refuses the open.

#### `camera.pixel_format`

Text. Default `Mono8`.

- Does: The camera's pixel format. The capture path is 8-bit throughout.
- Change when: Never. A colour camera works if it offers Mono8.
- Goes wrong: Any other value is refused when the profile loads, because a wider
  format would be cut to its low 8 bits. A camera without Mono8 refuses the open.

#### `camera.offset_x`

Integer, pixels, or `center`. Default `center`.

- Does: Where the `frame_width` × `frame_height` region starts across the sensor.
  `center` centres it.
- Change when: To move the region off centre.
- Goes wrong: A negative value is refused when the profile loads. A value off the
  camera's increment, or one that puts the region past the sensor edge, refuses
  the open with the nearest values that fit.

#### `camera.offset_y`

Integer, pixels, or `center`. Default `center`.

- Does: Where the region starts down the sensor, as `camera.offset_x` does across.
- Change when: To move the region off centre.
- Goes wrong: As for `camera.offset_x`.

#### `camera.trigger.line`

Text, `Line0`, `Line1` and so on. Required.

- Does: The camera input the trigger wire is on.
- Change when: Run `uv run probe_flir.py --find-line`: it shows which line toggles
  and prints what to write.
- Goes wrong: `Software` is refused, because Panopticon is always
  hardware-triggered, and so is any name that is not `Line` and a number. A line
  that is not an input on the camera, or is set as an output, refuses the open.
  On some cameras a trigger pin is also a power input:
  [FLIR.md](FLIR.md#2-wire-the-trigger) has the Blackfly S warning.

#### `camera.trigger.activation`

`RisingEdge` or `FallingEdge`. Default `RisingEdge`.

- Does: The edge of each pulse that starts an exposure. Panopticon's board drives
  rising edges.
- Change when: For an external source whose pulses start on a falling edge.
- Goes wrong: The wrong edge moves every exposure by the pulse width. An entry
  the camera does not offer refuses the open.

#### `camera.trigger.overlap`

`ReadOut`, `Off` or `PreviousFrame`. Default `ReadOut`.

- Does: Whether the camera takes a trigger while it is still reading out the
  previous frame.
- Change when: Leave `ReadOut`.
- Goes wrong: With `Off`, a trigger that arrives during readout is ignored and
  uses no frame ID, so that camera's block IDs fall behind the trigger count. The
  trigger witness and the block-ID rate check report it after the recording. An
  entry the camera does not offer refuses the open. A camera without the setting
  logs that at open.

#### `camera.trigger.delay_us`

Number, microseconds, or null. Default `null`.

- Does: The camera's `TriggerDelay`. `null` leaves the user set's value.
- Change when: Rarely; for example to line up cameras whose exposures start at
  different delays.
- Goes wrong: A negative value is refused when the profile loads, and one outside
  the camera's range refuses the open.

#### `camera.per_camera`

Mapping of quoted serial to settings. Default: empty.

- Does: Values for single cameras, keyed by quoted serial. Each entry may set
  `exposure_us`, `gain_db`, `offset_x`, `offset_y` and `trigger_line`, and any
  key it leaves out takes the block's value.
- Change when: For a camera that needs less exposure (one facing a lamp), another
  offset, or a different trigger line. `--find-line` prints entries when the
  cameras are wired to different lines.
- Goes wrong: The profile must list `camera_serials`, and every key must be one of
  them. An unquoted key is refused, because YAML reads a bare number as an
  integer and a leading-zero one as octal. The values follow the rules of the
  block's own keys.

#### `camera.flir.user_set`

`Default`, `UserSet0`, `UserSet1` or `none`. Default `Default`.

- Does: The user set loaded before anything else is written, each time the
  cameras open. Every setting the block does not name comes from it. `none` keeps
  whatever the camera holds. Panopticon never saves a user set.
- Change when: To start from a user set you saved in SpinView, for example one
  that sets a line to Input.
- Goes wrong: `none` lets a setting another program left on the camera reach a
  recording. A camera without user sets refuses any value but `none`.

#### `camera.flir.gamma_enable`

True or false. Default `false`.

- Does: Writes `GammaEnable`. Off keeps pixel values in proportion to light.
- Change when: Leave it off for tracking and calibration.
- Goes wrong: A camera without the setting keeps its own, and the log says so when
  you asked for `true`.

#### `camera.flir.black_level`

Number or null. Default `null`.

- Does: Writes `BlackLevel`. `null` leaves the user set's value.
- Change when: Rarely.
- Goes wrong: A value outside the camera's range refuses the open.

#### `camera.flir.adc_bit_depth`

`Bit8`, `Bit10`, `Bit12` or null. Default `null`.

- Does: Writes `AdcBitDepth`, the depth of the sensor's conversion. The recorded
  pixels stay Mono8. `null` leaves the user set's value.
- Change when: Rarely, and only when your camera's manual ties the depth to the
  frame rate you need.
- Goes wrong: An entry the camera does not offer refuses the open.

#### `camera.flir.link_throughput_limit`

Bytes per second, `auto`, `max` or null. Default `auto`.

- Does: Writes `DeviceLinkThroughputLimit`, which spaces each camera's frame
  transfer; it is the FLIR counterpart of `trigger_rate_limit`'s pacing. `auto`
  gives one frame 60% of the trigger period (payload × frame rate ÷ 0.6, within
  the camera's range). `max` sets the camera's maximum, a number is used as
  given, and `null` leaves the camera's value.
- Change when: Leave `auto`.
- Goes wrong: When the link cannot carry payload × frame rate at any setting, the
  open refuses (`DeviceLinkThroughputLimit can go no higher`). Lower `frame_rate`
  or the frame size, or check the cable and port. A number below what the frames
  need also refuses the open. The check covers each camera's own link only, and
  not cameras that share a USB controller, a hub or a network link.

#### `camera.flir.packet_size`

Integer, 576 to 9000 bytes, or null. Default `null`.

- Does: GigE only. Writes `GevSCPSPacketSize`, the stream packet size. `null`
  leaves the camera's value.
- Change when: 9000 when every adapter and switch port in the path passes jumbo
  frames. Check with `uv run probe_network.py --sweep --profile NAME`.
- Goes wrong: A USB3 camera refuses the key. A size some device in the path cannot
  pass delivers no complete frames. A value outside the camera's range or off its
  increment refuses the open.

#### `camera.flir.packet_delay`

Integer 0 or more, or null. Default `null`.

- Does: GigE only. Writes `GevSCPD`, the delay between one camera's packets.
- Change when: When a switch drops packets from cameras that share a port.
- Goes wrong: A USB3 camera refuses the key. A value outside the camera's range
  refuses the open.

#### `camera.flir.stream_mode`

`auto`, `TeledyneGigEVision`, `LWF` or `Socket`. Default `auto`.

- Does: GigE only. Picks Spinnaker's GigE stream driver; `auto` leaves Spinnaker's
  choice. It is the FLIR counterpart of `gige_driver`.
- Change when: Only for a comparison test on your rig.
- Goes wrong: A USB3 camera refuses any value but `auto`. An entry the camera does
  not offer refuses the open.

#### `camera.flir.extended_ids`

True or false. Default `true`.

- Does: GigE only. Asks the camera for 64-bit frame IDs. Without them the frame ID
  has 16 bits and wraps after 65535 frames, which the backend unwraps with the
  camera's clock.
- Change when: Leave it on.
- Goes wrong: A camera without the setting keeps 16-bit IDs. USB3 cameras ignore
  the key.

#### `camera.flir.block_id_source`

`auto`, `frame_id` or `trigger_counter`. Default `auto`.

- Does: What each frame's block ID comes from. `frame_id` is the camera's frame
  counter, used when the self-test at open proves that it restarts at each
  acquisition and counts frames by 1. `trigger_counter` is the camera's count of
  edges on its trigger line, carried in the `CounterValue` chunk, so a trigger
  the camera ignored is a gap in the IDs. `auto` uses the frame ID when the
  self-test proves it, and the trigger counter otherwise.
- Change when: Set `trigger_counter` when `WARNINGS.txt` says a camera's trigger
  witness is limited and the camera offers the `CounterValue` chunk.
- Goes wrong: `trigger_counter` on a camera without the chunk refuses the open,
  and so does `frame_id` on a camera whose frame ID fails the self-test. A camera
  whose frame ID fails and that has no `CounterValue` chunk cannot record.

#### `camera.flir.timestamp_source`

`auto`, `image` or `chunk`. Default `auto`.

- Does: Where each frame's camera time comes from: the image's timestamp, or the
  `Timestamp` chunk. `auto` uses the image's, and the chunk when the image's
  reads 0. The self-test at open checks that the clock runs, in a unit it can
  convert to nanoseconds, and keeps running across a restart.
- Change when: Only when the probe shows that your model's image timestamp is 0.
- Goes wrong: A camera whose clock fails the self-test is refused at open.

#### `camera.flir.sdk_dir`

Text, a path, or null. Default `null`.

- Does: The Spinnaker SDK folder to load the C library from, when it is not in a
  default install location. The `PANOPTICON_SPINNAKER_DIR` environment variable
  does the same for every profile and every process.
- Change when: When your SDK is installed somewhere else.
- Goes wrong: A folder that does not exist stops the cameras opening. The library
  loads once per process, when that process first opens FLIR cameras, so a
  profile that names another folder after that is refused. Restart Panopticon, or
  set `PANOPTICON_SPINNAKER_DIR`, to change folders.

## Board config

A board config describes the printed ChArUco board in `configs/boards/`. The
coverage display and Solve read it through `board_config`. Measure the printed
board with a ruler: printers scale.

| Key | Type | Default | Reference board |
|---|---|---|---|
| `board_x` | Integer | Required | 8 |
| `board_y` | Integer | Required | 8 |
| `square_length` | Number | Required | 15.0 |
| `marker_length` | Number | Required | 10.0 |
| `marker_bits` | Integer | 4 | 4 |
| `dict_size` | Integer | 1000 | 1000 |
| `board_legacy` | True or false | false | true |

- `board_x` and `board_y` count the squares across and down. A wrong count finds
  no board.
- `square_length` is one square's side, in the unit you want 3D output in
  (millimetres on the reference board). It sets the scale of every 3D coordinate,
  and a wrong value still gives small reprojection errors.
- `marker_length` is the side of the ArUco marker inside a square, in the same
  unit.
- `marker_bits` and `dict_size` name the ArUco dictionary, `DICT_4X4_1000` by
  default. A combination OpenCV does not have is refused with the valid names.
- `board_legacy: true` is for a board printed with the ChArUco layout of OpenCV
  before 4.6, such as the reference board. Wrong in either direction, every marker
  is found and no ChArUco corners come back.

A missing required key, or one that is not a positive number, is refused with the
key named. Unknown keys are ignored, so a misspelled `board_legacy` falls back to
`false`.

## Sizing formulas

Each formula is per rig, for N cameras of W × H pixels at F frames per second.
Mono8 is one byte per pixel. GiB is 2^30 bytes; GB and MB/s are powers of 10.
Section 1 of [INSTALLATION.md](INSTALLATION.md) explains how to choose hardware
from these numbers.

### Exposure ceiling

The longest exposure at which a camera still takes every trigger.

- Basler (and `sim`): in trigger mode the camera's frame-rate timer starts after
  the exposure ends, so the ceiling is `1/F − 1/trigger_rate_limit`.
- FLIR: Panopticon measures each camera's ceiling at F from what the camera
  reports ([FLIR.md](FLIR.md#the-exposure-ceiling)).

Panopticon keeps a 10% margin below the ceiling, so the exposure limit is 90% of
it:

- On Basler it lowers an exposure above the limit at every acquisition start,
  and the `[camN] exposure=` line says `CLAMPED`.
- On FLIR, opening the cameras refuses a recording exposure above the limit at
  `frame_rate`. It caps a calibration exposure and logs `CLAMPED`.

With `trigger_rate_limit: 165`:

| Frame rate | Trigger period | Ceiling | 90% of it |
|---|---|---|---|
| 100 fps (recording) | 10.0 ms | 3.94 ms | 3.55 ms |
| 30 fps (calibration) | 33.3 ms | 27.3 ms | 24.5 ms |

A camera over its ceiling ignores the next trigger and uses no block ID for it,
so its video drifts in time with no gap in `blockids.npy`. The block-ID rate check
after each recording reports that.

### Network

One camera sends W × H × F bytes per second, before protocol headers: 230.4 MB/s,
or 1.84 Gbit/s, for 1920×1200 at 100 fps. Each camera's own link and each host
port must carry that with room to spare. A host port carries the sum of every
camera behind it.

### RAM

```
driver pool = N × max_num_buffer × W × H
NV12 ring   = N × slots × W × H × 1.5
slots       = kick_max_lag + 264    (realtime_kick: true)
              204                   (realtime_kick: false)
              no ring               (realtime_encode: false)
page-locked = N × 4 × W × H × 1.5   (nvenc_upload: pinned)
```

The reference rig needs 11.6 GiB of pool and 21.6 GiB of ring, 33.1 GiB in all,
plus 119 MiB page-locked. Record and Calibrate refuse to start when the pool and
ring need more than the memory available at that moment
(`Not enough RAM for`). The rings are freed when each acquisition stops, so the
next acquisition needs the same memory as the first.
[INSTALLATION.md](INSTALLATION.md#ram) says how much RAM to buy.

### Disk

- Real-time H.264: about 4,600 bytes per frame at `quality: 21` on the reference
  rig, so N × F × 4,600 bytes per second. Nine cameras at 100 fps write 4.1 MB/s,
  about 15 GB an hour.
- Raw capture (`realtime_encode: false`): N × F × W × H bytes per second, the
  [network](#network) rate of every camera together. One 1920×1200 camera at
  100 fps fills 129 GiB every 10 minutes.

At each start Panopticon estimates a 10-minute recording against the free space
on the output folder's drive, and warns when it is short. It does not refuse, because
the recording may be shorter.

### NVENC sessions

Real-time encoding holds one NVENC session per camera for the whole recording.
The NVIDIA driver limits how many sessions run at once, and the limit depends on
the GPU and the driver version. Panopticon measures it at launch, and again at a
start that needs more sessions than it last measured. More cameras need a more
capable GPU, and this driver limit is often what caps the camera count. With
`encoder: auto` and fewer sessions than cameras, Panopticon encodes on the CPU if
its benchmark says the CPU keeps up, and otherwise refuses the start. The copies
into mp4 after Stop use no session. In raw mode, and during alignment re-encodes,
each of the `encode_parallel` jobs holds one.

## Trigger source

Every camera takes its trigger from a hardware TTL pulse. Panopticon never
triggers a camera from software. The profile's
[trigger_source](#trigger_source) picks where the pulses come from.

### Panopticon's trigger board

The default, `trigger_source: board`. The board is an Arduino Mega 2560 running a
sketch Panopticon generates from the profile, and it also runs stimulation. The
profile names it with [serial_port](#serial_port),
[trigger_pins](#trigger_pins) and [stim_safe_pins](#stim_safe_pins).
[INSTALLATION.md step 8](INSTALLATION.md#step-8--flash-the-trigger-firmware)
covers installing `arduino-cli` and flashing, and
[INSTALLATION.md](INSTALLATION.md#wiring-the-trigger-line) covers wiring.

Panopticon starts the board only once every camera is armed, and the board
acknowledges each start. A stimulation paradigm reaches the board only through
Apply in the Stimulation editor, and calibration always runs on the
recording-only sketch.

### Your own TTL source

With `trigger_source: external`, your pulse generator or DAQ drives every
camera's trigger input, and you start and stop it yourself. Panopticon opens no
serial port. Start from [`external_ttl.yaml`](../profiles/templates/external_ttl.yaml).

- Stimulation needs Panopticon's board, so the Stimulation editor is unavailable.
- The profile refuses `serial_port`, `trigger_pins` and a non-empty
  `stim_safe_pins`, because each names a board this mode never opens.
- Panopticon cannot read your source's rate. Set `frame_rate` and
  `calibration_frame_rate` to the rates you run it at. The block-ID rate check
  after each recording compares each camera with `frame_rate`, and
  `session_metadata.json` records `trigger_source: external`.
- Wire the source to every camera's trigger input with a common ground, and check
  that one output can drive every input it feeds.

Every camera must be armed before the first pulse, or the cameras count their
frames from different pulses and nothing in the files shows it. Panopticon
therefore refuses a recording in which any camera receives a frame before every
camera is armed (`received frames before every camera was armed`), and asks you
to start the source only after that check.
[WORKFLOW.md](WORKFLOW.md#your-own-trigger-source) gives the order a recording
runs in, with its timings.

## What the loader checks, and what it does not

Panopticon checks each profile as it loads it at launch, and
[Where settings live](#where-settings-live) says what you see when it refuses
one. The tables quote the start of each message or a phrase from it.

### Refused when the profile loads

The file:

| The profile | The message contains |
|---|---|
| Is not valid YAML | `not valid YAML` |
| Cannot be read | `cannot be read` |
| Is empty | `the profile file is empty` |
| Is a list or a single value at the top | `expected a mapping of profile fields` |
| Has a key that is not a field | `unknown profile field(s)` |

Types. Each message names the field:

| The value | The message contains |
|---|---|
| Is not true or false where one is needed | `expected true/false` |
| Is not a whole number where one is needed | `expected an integer` |
| Is not a number where one is needed | `expected a number` |
| Is a list or mapping where text is needed | `expected text` |
| Is not a list where one is needed | `expected a list` |
| Is not a mapping where one is needed | `expected a mapping` |
| Is empty (`key:` with nothing after it), for a true/false, number or text field that cannot be null | `value is empty` |
| Is an unquoted camera serial | `camera serials must be quoted strings` |

Single fields:

| Field | Refused when | The message contains |
|---|---|---|
| `camera_backend` | Empty | `must be a non-empty backend name` |
| `camera_backend` | Not `basler`, `flir`, `sim` or `flir_sim` | `is not a known backend` |
| `gige_driver` | Not `socket`, `filter` or `auto` | `an unknown name would fall through` |
| `encoder` | Not `auto`, `nvenc`, `x264` or `raw` | `encoder` and `is not one of` |
| `trigger_source` | Not `board` or `external` | `trigger_source` and `is not one of` |
| `log_level` | Not `normal`, `verbose` or `debug` | `log_level` and `is not one of` |
| `nvenc_upload` | Not `pinned` or `host` | `nvenc_upload` and `is not one of` |
| `nvenc_context` | Not `shared` or `own` | `nvenc_context` and `is not one of` |
| `trigger_pins` | Includes 0 or 1 | `are the Serial0 link` |
| `trigger_pins` | Lists a pin twice | `lists a pin twice` |
| `trigger_rate_limit` | Negative | `must be 0 (off) or positive` |
| `frame_rate`, `calibration_frame_rate` | 0 or less | `must be positive` |
| `frame_width`, `frame_height`, `kick_max_lag`, `max_num_buffer`, `encode_parallel` | 0 or less | `must be positive` |
| `frame_width`, `frame_height` | Odd | `must be even` |
| `quality` | Outside 0 to 51 | `the H.264 QP range` |
| `calibration_exposure_us` | Negative | `must be 0 (keep the recording exposure)` |
| `thermal_warn_margin_c` | 0 or less | `must be a positive number of degrees C` |
| `n_cameras` | Negative | `must be 0 (unchecked) or positive` |
| `camera_serials` | An empty list | `camera_serials is empty` |
| `camera_serials` | A serial listed twice | `lists a serial twice` |
| `camera_serials` | Not in ascending text order | `is not in ascending order` |
| `capture_processes` | Negative | `must be 0 (capture in this process)` |
| `capture_processes` | More than `n_cameras` or `camera_serials` gives | `each worker needs a camera` |
| `gev_bandwidth_reserve_pct` | Outside 0 to 100 | `must be within 0..100` |
| `gev_bandwidth_reserve_accum` | Negative | `must be non-negative` |
| `metadata_defaults` | Has another key | `are not session metadata fields` |

Combinations:

| The profile | The message contains |
|---|---|
| Lists a trigger pin in `stim_safe_pins` too | `are also in stim_safe_pins` |
| Sets `frame_rate` or `calibration_frame_rate` at or above `trigger_rate_limit` (Basler, sim) | `the camera would skip triggers` |
| Sets `max_num_buffer` below `kick_max_lag` in kick-out mode | `is below kick_max_lag` |
| Sets `nvenc_context: own` with `nvenc_upload: host` | `chooses the CUDA context of the pinned upload path` |
| Sets `n_cameras` to another count than `camera_serials` has | `disagrees with the` |
| Sets `capture_processes` above 0 without real-time encoding and kick-out | `needs realtime_encode: true and realtime_kick: true` |

A FLIR or flir_sim profile:

| The profile sets | The message contains |
|---|---|
| A non-empty `pfs_path` | `pfs_path is a Basler pylon feature file` |
| `trigger_rate_limit` | `which FLIR cameras do not have` |
| `gige_driver` other than `auto` | `gige_driver chooses the pylon GigE driver` |
| Either `gev_` field | `FLIR cameras have no equivalent` |
| No `camera:` block | `camera block is required` |

An external trigger source:

| The profile sets | The message contains |
|---|---|
| `serial_port` | `trigger_source is external, so Panopticon opens no serial port` |
| `trigger_pins` | `your own source drives the cameras' trigger inputs` |
| A non-empty `stim_safe_pins` | `Stimulation needs trigger_source: board` |

The `camera:` block. Messages start with the key, such as `camera.trigger.line`:

| The block | The message contains |
|---|---|
| Is in a `basler` profile | `camera block given but camera_backend is 'basler'` |
| Is not a mapping, at any level | `expected a mapping of` |
| Has an unknown key, at any level | `unknown field` |
| Leaves out `exposure_us` or `gain_db` | `is required for camera_backend` |
| Leaves out `trigger` or `trigger.line` | `camera.trigger.line is required` |
| Sets a key to nothing | `is empty; remove the key to keep the default` |
| Sets a pixel format other than `Mono8` | `is not supported` |
| Sets an exposure of 0 or less | `must be a positive number of microseconds` |
| Sets an exposure as long as the trigger period | `cannot fit a frame_rate` |
| Sets a negative gain | `must be 0 dB or more` |
| Sets an offset that is not a number or `center` | `must be a pixel count of 0 or more` |
| Sets a negative offset | `must be 0 or more pixels, or center` |
| Sets `trigger.line: Software` | `Panopticon is always hardware-triggered` |
| Sets a line not named `Line` and a number | `is not a camera input line` |
| Sets an activation, overlap or other named value not in its list | `is not one of` |
| Sets a negative `trigger.delay_us` | `must be 0 or more microseconds` |
| Has `per_camera` without `camera_serials` | `must list them in camera_serials` |
| Has a `per_camera` key not in `camera_serials` | `is not in camera_serials` |
| Has an unquoted `per_camera` key | `must be a quoted serial` |
| Has an empty `per_camera` entry | `give the settings this camera overrides` |
| Has a `per_camera` serial twice | `lists a serial twice` |
| Sets a `link_throughput_limit` that is not a positive number, `auto` or `max` | `must be a positive number of bytes per second` |
| Sets a `packet_size` outside 576 to 9000 | `bytes` and `is outside` |
| Sets a negative `packet_delay` | `packet_delay` and `must be 0 or more` |
| Sets a `black_level` that is not a finite number | `must be a finite number` |

### Refused before the cameras open

| Cause | What you see |
|---|---|
| A Basler profile's `.pfs` is missing | `The profile's camera settings file is missing` |
| `camera.flir.sdk_dir` is not a folder | `camera.flir.sdk_dir is not a folder` |
| `capture_processes` is above 0 | `The profile sets capture_processes` |
| No camera is found | `No cameras found` |
| A camera in `camera_serials` is missing | `Requested cameras did not enumerate` |
| The camera count differs from `n_cameras` | `are available to open` |
| A camera's pixel format is not Mono8 | `not Mono8` |
| A camera's frame size differs from the profile's | `differs from the profile's` |
| A Basler camera lacks a node a `gev_` field needs | `failed to open/configure` |
| A FLIR value is outside the camera's range | See [FLIR.md](FLIR.md#when-something-refuses) |

### Refused when an acquisition starts

| Cause | What you see |
|---|---|
| The open cameras' frame size differs from the profile's | `The profile records` |
| `encoder: raw` with `realtime_encode: true` | `encoder: raw` |
| `encoder: auto`, and neither NVENC nor the CPU keeps up | `No encoder on this machine can keep up` |
| `encoder: nvenc`, and too few NVENC sessions | `NVENC granted only`, `NVENC granted no encode sessions` or `NVENC is unavailable` |
| `encoder: x264`, and the CPU does not keep up | `The profile selects libx264` |
| The pool and ring need more RAM than is available | `Not enough RAM for` |
| A FLIR camera cannot record at this rate | `These cameras cannot record at` |
| External source: a frame arrived before every camera was armed | `received frames before every camera was armed` |

### Not checked

These mistakes load without a message. Check them yourself.

- Two profiles with the same `name`, or an empty `name`.
- An empty `trigger_pins`, or pins the board does not have.
- `stim_safe_pins` that includes 0 or 1, or leaves out a pin your laser is on.
- A `trigger_rate_limit` above the camera's real maximum.
- A `calibration_min_grid_cells` above 4, and any value of the other coverage
  thresholds.
- A `capture_core_exclude` that leaves no performance core (it falls back to all
  of them).
- A `serial_port` that names another device, which opening the profile resets
  ([serial_port](#serial_port)).
- `output_dir` and `board_config` paths. Panopticon creates a missing output
  folder when it records. A missing board config hides the coverage display and
  refuses Solve.
- Keys in a board config: unknown ones are ignored.
- With an external source, a source running at another rate than `frame_rate`.
  Only the block-ID rate check after the recording can show it.
- Several cameras sharing one USB controller, hub or network link. Panopticon
  checks each camera's own link only.

## Changing settings safely

Restart Panopticon after editing a profile. Some changes invalidate what you have
already recorded or measured.

- Calibrate again after you add, remove, replace or move a camera, change its lens
  or focus, change `camera_serials`, or change the frame size or offset. A
  calibration attaches to camera names, and each name follows `camera_serials` (or
  the serial order without it).
- Test on the rig before you rely on a change to `kick_max_lag`, `max_num_buffer`,
  `trigger_rate_limit`, `frame_rate`, the exposure, `gige_driver`, the `gev_`
  fields, the CPU placement fields, `nvenc_upload`, `nvenc_context` or anything in
  `camera.flir`. A one-minute recording shows most problems (step 13 of
  [Configure a new rig](#configure-a-new-rig-step-by-step)).
- `metadata_defaults`, `output_dir`, `quality`, `log_level`,
  `thermal_warn_margin_c` and the coverage thresholds change nothing already
  recorded.
- Renaming a profile makes the next launch ask you to choose one
  ([name](#name)).
