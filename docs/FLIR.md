# FLIR cameras

Panopticon runs FLIR (Teledyne) machine-vision cameras, GigE or USB3, through
Teledyne's Spinnaker SDK. This page takes a FLIR rig from installation to a first
test recording, and says what to send back to the maintainers.

The FLIR backend drives any model from what the camera reports about itself: the
range of each setting, its trigger lines and its pixel formats. There is no table
of supported models.

Contents:

1. [Install](#1-install)
2. [Wire the trigger](#2-wire-the-trigger)
3. [Write the profile](#3-write-the-profile)
4. [Open your profile in Panopticon](#4-open-your-profile-in-panopticon)
5. [Run the probe](#5-run-the-probe)
6. [Record a short test](#6-record-a-short-test)
7. [Send the results](#7-send-the-results)

Also on this page: [Use your own trigger source](#use-your-own-trigger-source),
[Known limitations](#known-limitations) and
[When something refuses](#when-something-refuses).

## Status: nothing has run on FLIR hardware yet

Panopticon has not yet opened a real FLIR camera. The FLIR backend
(`gui_app/backends/flir.py`), its binding to the Spinnaker C library
(`gui_app/backends/_spinc.py`), the diagnostic probe (`probe_flir.py`) and the FLIR
profile templates have run only against a simulated Spinnaker library
(`gui_app/backends/fake_spinc.py`) and a simulated trigger board.

What that means for you:

- The first contact with a real camera can fail in ways the simulation cannot
  show. A Spinnaker function can behave differently on your SDK version, your
  model can name a setting differently, or its frame counter can restart
  differently from what the backend expects.
- Some behaviours the backend relies on stay unknown until a real camera shows
  them, for example how wide the camera's counters are and whether its frame ID
  restarts at 1. The probe measures each one and writes the answers into its
  JSON file.
- Run the probe before you record, and treat your first recordings as tests.
  Record nothing you cannot repeat until the maintainers have seen your probe
  results and a short test recording.
- The alignment checks run as they do on a Basler rig. After each recording
  Panopticon checks that each camera's block IDs (the trigger number each frame
  carries) advanced at the trigger rate. On a FLIR camera with counters it also
  counts the triggers that camera ignored. A recording that fails either check
  says so in `WARNINGS.txt`.

## What you need

- 64-bit Windows, in a version the Spinnaker SDK supports (Teledyne's download
  page lists them).
- An NVIDIA GPU. Real-time encoding uses one NVENC session per camera, and the
  driver limits how many run at once, so more cameras need a more capable GPU.
  [INSTALLATION.md](INSTALLATION.md#gpu) sizes it for your camera count.
- FLIR cameras, GigE or USB3, each with a Mono8 pixel format and a hardware
  trigger input.
- A hardware TTL trigger. The default is Panopticon's trigger board: an Arduino
  Mega 2560 that Panopticon programs through `arduino-cli`. A pulse generator or
  DAQ of your own also works ([Use your own trigger source](#use-your-own-trigger-source)).
- The Spinnaker SDK 4.x. PySpin, Spinnaker's Python package, is not needed.

## 1. Install

### Panopticon

Follow steps 1, 3 and 4 of [INSTALLATION.md](INSTALLATION.md#2-install-the-software):
install uv, clone the repository and run `uv sync`. Skip step 2, the Basler pylon
SDK. `uv sync` still installs pypylon, the Basler Python package, but a FLIR
profile never loads it.

Every command on this page runs in PowerShell, from the repository folder.

### The Spinnaker SDK

1. Download the Spinnaker SDK 4.x for Windows x64 from
   [Teledyne's Spinnaker page](https://www.teledynevisionsolutions.com/products/spinnaker-sdk/).
   The download needs a free Teledyne account.
2. Run the full installer. It installs SpinView, the USB3 and GigE drivers, and
   the C library Panopticon calls, `SpinnakerC_v140.dll`.
3. Open SpinView and stream each camera at your target frame rate and frame
   size. Stream them all at once if SpinView lets you, because that also tests
   the USB controllers or network links they share. A camera that cannot keep up
   in SpinView has a cable, driver or bandwidth problem that Panopticon cannot
   fix.
4. Close SpinView. Panopticon cannot open a camera that SpinView holds.

Panopticon looks for the library in `C:\Program Files\Teledyne\Spinnaker`, where
Spinnaker 4.x installs, and in the folders older SDKs used (`FLIR Systems` and
`Point Grey Research` under `C:\Program Files`), then on `PATH`. If your SDK is
elsewhere, set `camera.flir.sdk_dir` in the profile, or the
`PANOPTICON_SPINNAKER_DIR` environment variable, to its install folder.

Panopticon's binding follows the Spinnaker 4.x C headers. An SDK that lacks a
function Panopticon calls is refused when the library loads, and the message
names the missing functions.

To check the install, run the probe's first stage. It needs no profile:

```powershell
uv run probe_flir.py --list
```

Its check `Spinnaker C library loads` prints the SDK version and the path of the
library it loaded.

PySpin is needed only for the probe's optional `--pyspin` stage. To run that
stage, install Teledyne's PySpin wheel for your Python version with
`uv pip install <path to the .whl>`. A later plain `uv sync` removes it again.
The `spinnaker-python` project on PyPI is an old upload, so do not install that
one.

### GigE cameras: the network

GigE cameras need the same network as a Basler rig: an address plan, jumbo
frames on every adapter and switch port, and a firewall rule.
[INSTALLATION.md step 5](INSTALLATION.md#step-5--put-the-cameras-on-the-network-gige)
covers each part. Give each camera its address in SpinView.

To see which adapter each camera answers on, run:

```powershell
uv run probe_network.py
```

Once the profile exists (section 3), run the packet-size sweep with your
profile's name:

```powershell
uv run probe_network.py --sweep --profile my_lab
```

It grabs frames from each camera at packet sizes from 1500 to 9000 bytes, and
prints how many arrived complete at each size. A clean cutoff between two sizes
means a device in that camera's path does not pass jumbo frames. The GigE
template sets `camera.flir.packet_size: 9000`, which needs every size in the
sweep to pass. The sweep skips USB3 cameras. Keep `--profile`: without it, the
sweep uses a shipped Basler profile until Panopticon has opened yours.

### The trigger board

Skip this part if you use your own trigger source.

Install the Arduino IDE (which includes `arduino-cli`) and the Mega's board
support, as [INSTALLATION.md step 8](INSTALLATION.md#step-8--flash-the-trigger-firmware)
describes. Panopticon generates the board's sketch and uploads it when it opens
a profile, so there is nothing to upload by hand. Note the board's COM port
(Device Manager, under *Ports (COM & LPT)*) for the profile's `serial_port`.

## 2. Wire the trigger

Wire each camera's trigger input to one pin of the trigger board, and give each
input a ground reference from the board. The templates list one pin per camera
in `trigger_pins`. The board drives 5 V pulses, and each camera triggers on the
rising edge (`activation: RisingEdge` in the profile).
[INSTALLATION.md](INSTALLATION.md#wiring-the-trigger-line) covers pin choice and
when one pin can drive several cameras.

Connect nothing but cameras to the board while you bring the rig up, and keep
`stim_safe_pins: []` in the profile. The probe and Panopticon reset the board
whenever they open its port, and the board's pins float for about a second
during a reset. If a laser or LED driver is wired to the board, read the
flashing warning in
[INSTALLATION.md step 8](INSTALLATION.md#step-8--flash-the-trigger-firmware)
first.

A FLIR trigger input is either opto-isolated or non-isolated, and the two are
grounded differently:

- An opto-isolated input has a ground of its own, the opto ground, which is not
  connected to the camera's ground. Wire the board's GND to the opto ground pin.
  An opto-isolator is driven by current, so check the input's current and voltage
  ratings.
- A non-isolated input shares the camera's ground. Wire the board's GND to the
  camera's GND pin, and check that the input accepts a 5 V signal.

The Blackfly S BFS-U3-16S2, as an example (its Technical Reference, section 20).
Check your own model's pinout before you wire it:

| Pin | Wire | Line | Function |
|---|---|---|---|
| 1 | green | Line3 | Non-isolated input, and the camera's power input (8-24 V) |
| 2 | black | Line0 | Opto-isolated input |
| 3 | red | Line2 | Non-isolated input or output, or a 3.3 V output |
| 4 | white | Line1 | Opto-isolated output |
| 5 | blue | none | Opto ground, not connected to the camera's ground |
| 6 | brown | none | Camera ground |

On this camera:

- For `Line0`, wire the board's pin to pin 2 and the board's GND to pin 5.
- For `Line3`, wire the board's pin to pin 1 and the board's GND to pin 6. Use
  `Line3` only on a camera powered over USB or PoE.

> [!WARNING]
> On a Blackfly S, pin 1 (Line3) is also the camera's power input. On a camera
> powered through its I/O connector, pin 1 carries the supply voltage, so never
> wire a trigger signal to it. Use Line0 (pin 2, with the trigger's ground on
> pin 5) instead.

Other families (Chameleon3, Grasshopper3, Oryx and others) can put their lines on
other pins, with other ratings. Read the I/O section of your model's Technical
Reference. The probe's `--find-line` stage shows which line each wire reaches.

## 3. Write the profile

A profile is the YAML file that describes your rig. A Basler rig loads its
exposure and gain from a `.pfs` settings file. A FLIR camera has no such file, so
the profile's `camera:` block is the only source of the recording exposure, gain
and trigger line. Anything the block does not name comes from the user set it
names, the camera's factory `Default` unless you change it. Panopticon loads
that user set every time it opens a camera, so a setting you change in SpinView
does not carry into a recording.

1. Copy a template from `profiles/templates/` into `profiles/`, and give the copy
   a name of your own, for example `profiles/my_lab.yaml` with `name: my_lab`
   inside it. Only files in `profiles/` appear in Panopticon's profile dropdown.

   | Template | For |
   |---|---|
   | [`flir_usb3.yaml`](../profiles/templates/flir_usb3.yaml) | USB3 cameras on the trigger board |
   | [`flir_gige.yaml`](../profiles/templates/flir_gige.yaml) | GigE cameras on the trigger board, with jumbo frames |
   | [`external_ttl.yaml`](../profiles/templates/external_ttl.yaml) | USB3 cameras on your own trigger source |

2. Run `uv run probe_flir.py --list`. It prints each camera's serial number and
   model, and two lines to paste into the profile. Its checks of the template's
   placeholder serials fail until you paste the real ones. From a rehearsal on
   three simulated cameras (each line of the probe's output starts with a time
   stamp and a thread name, left out here):

   ```
   [probe] for the profile:  n_cameras: 3
   [probe]                   camera_serials: ["90000001", "90000002", "90000003"]
   ```

3. Replace every value marked `SITE` in your copy:
   - `n_cameras` and `camera_serials`, from step 2. Keep the serials quoted and
     in ascending order. cam1 is the first entry.
   - `frame_width` and `frame_height`: the region each camera records, in even
     numbers, centred on the sensor.
   - `frame_rate`.
   - `camera.exposure_us` and `camera.gain_db`. Keep the exposure under 90% of
     the camera's [exposure ceiling](#the-exposure-ceiling) at `frame_rate`.
     The ceiling is at most the trigger period, 10,000 µs at 100 fps.
   - `camera.trigger.line`: a first guess is fine, because `--find-line` checks
     it in section 5.
   - `serial_port` and `trigger_pins`, for the trigger board.

Leave `stim_safe_pins: []` and leave out `log_level`. Its default, `verbose`,
logs every setting written to a camera with the value the camera reads back,
which is what the maintainers need to see.

Settings a bring-up may need beyond the template's `SITE` values:

| Key | What it does |
|---|---|
| `camera.per_camera` | Values for single cameras, keyed by quoted serial: `exposure_us`, `gain_db`, `offset_x`, `offset_y`, `trigger_line` |
| `camera.offset_x`, `camera.offset_y` | Where the region sits on the sensor, in pixels. The default, `center`, centres it. |
| `camera.flir.sdk_dir` | The Spinnaker install folder, when it is not in a default location |
| `camera.flir.packet_size` | GigE only. 9000 needs jumbo frames end to end. |
| `camera.flir.user_set` | The user set loaded before anything else: `Default` (factory settings), `UserSet0`, `UserSet1` or `none` |
| `camera.flir.block_id_source` | `auto`: the frame ID when the camera's self-test proves it restarts, else the camera's count of trigger edges |

Each template's comments explain the rest of its values.

Loading the profile refuses the Basler-only fields in a FLIR profile
(`pfs_path`, `trigger_rate_limit`, `gige_driver`, `gev_bandwidth_reserve_pct`,
`gev_bandwidth_reserve_accum`), and each message names the FLIR setting to use
instead. Opening the cameras then checks every value against what each camera
reports, and refuses one outside the camera's range. The message names the
camera, the setting and the range.

### The exposure ceiling

Panopticon measures each camera's exposure ceiling at `frame_rate`: the longest
exposure at which the camera still takes every trigger. Opening the cameras
refuses an exposure above 90% of the ceiling. The message includes
`is above what this camera can expose at frame_rate` and the longest exposure
allowed.

A calibration runs at `calibration_frame_rate`, with `calibration_exposure_us`
(0 keeps the recording exposure). Panopticon caps the calibration's exposure at
90% of the ceiling at that rate. A capped exposure shows as `CLAMPED` in that
camera's exposure line in the log.

## 4. Open your profile in Panopticon

Do this section whatever your trigger source. On a new computer the first
launch opens a shipped profile, and the launch after it opens yours. With
Panopticon's board, the launches also leave the board carrying the
recording-only sketch for your profile (camera triggers, no stimulation). The
probe starts the board only once the board reports that sketch.

> [!WARNING]
> Before the first launch on a new computer, unplug every Arduino and every
> other serial device except Panopticon's trigger board. That launch opens a
> shipped reference profile, which resets whatever device is on COM3 and tries
> to reprogram it.

1. Run `uv run gui.py`. On the first launch on a new computer, Panopticon has
   no profile of yours to remember. It opens the first shipped profile that is
   ready (a Basler reference rig) and reports that it cannot open that rig's
   cameras. That reference profile names COM3.
   - If your board is on COM3, Panopticon programs it with the reference rig's
     sketch. This takes about 30 s, and does no harm with only cameras on the
     board. The sidebar shows `Clearing stim firmware…` meanwhile.
   - If a `Could not clear stim firmware` dialog appears, the board on COM3
     could not be programmed, or there is none. Close the dialog. Step 4
     checks your own board.
2. Choose your profile in the profile dropdown. The dropdown is unavailable
   while Panopticon programs a board. If your profile names another port,
   Panopticon programs the board on that port now, in about 30 s.
3. Quit, and run `uv run gui.py` again. This launch opens your profile, the one
   you chose last. The first session loaded the Basler SDK for the reference
   profile, and running both vendors' SDKs in one process is untested.
4. With Panopticon's board, wait until the PowerShell window shows
   `board flashed with the recording-only sketch`, which takes about 30 s, or
   `board already carries the recording-only sketch`. A
   `Could not clear stim firmware` dialog at this launch is about your own
   board. Fix what its message names, such as a missing `arduino-cli` or the
   wrong `serial_port`, and launch again.
5. Quit.

## 5. Run the probe

`probe_flir.py` tests each part of the rig in stages, and writes what each camera
reports into one JSON file per run. Quit Panopticon first. The probe refuses to
start while Panopticon or another probe runs, and exits with code 3.

### Rehearse without cameras

```powershell
uv run probe_flir.py --fake
```

`--fake` runs every stage on three simulated cameras and a simulated trigger
board, with the [`flir_sim`](../profiles/templates/flir_sim.yaml) template as its
profile. It needs no camera, SDK or board, and takes about a minute. It prints
the same table a real run prints, and on the simulation no check fails.

### The stages

Run them in this order:

```powershell
uv run probe_flir.py --list
uv run probe_flir.py --find-line
uv run probe_flir.py --selftest
uv run probe_flir.py --triggered 60
```

`uv run probe_flir.py --all` runs the same four stages in one go.

| Stage | What it does | What it changes on the cameras |
|---|---|---|
| `--list` | Lists each camera and what it reports, and checks the profile's camera settings against it. Runs without a profile. | Nothing. It only reads. |
| `--find-line` | Runs the board at 10 Hz and watches each camera's input lines for about 1 s. The line that toggles is the wired one. | Nothing. It only reads line levels. |
| `--selftest` | Free-runs each camera to test its frame ID, clock, buffers and chunks. With a profile, it then opens each camera through the FLIR backend. | Loads the profile's user set, then free-runs with its own test settings. |
| `--triggered 60` | Captures 60 s at `frame_rate` through the FLIR backend, as a recording does, then runs counter checks at 10 Hz. | Opens the cameras as Panopticon does. The counter checks change exposure and trigger delay briefly, then put them back. |

`--find-line` compares each wire with the profile's `camera.trigger.line`. When
they differ, the check fails and the probe prints what to write, for example:

```
[probe] write in the profile:  camera: trigger: line: Line0
```

When the cameras are wired to different lines, it prints one `per_camera` entry
for each camera instead. Edit the profile and run `--find-line` again until it
passes.

`--triggered` runs 60 s by default, and takes a duration in seconds after it for
a longer run. Its counter checks add about half a minute. `--no-counter-check`
skips them.

The probe uses the profile Panopticon opened last, if that is a FLIR profile, or
else the only FLIR profile in `profiles/`. To choose one, add `--profile` with
the profile's name or the path to its file, for example `--profile my_lab`.

The probe writes nothing but its JSON file, its log and the `--collect` zip. It
never saves a user set, so the settings the camera keeps across a power cycle are
untouched.

### Reading the results

Each check prints as it runs. At the end the probe prints a table (stage, check,
result, detail), where the result is PASS, FAIL, WARN, SKIP or INFO. Below the
table, under `what the cameras answered`, it lists each open question about the
cameras with each camera's answer. The last line gives the overall result and
the path of the JSON file.

| Exit code | Meaning |
|---|---|
| 0 | No check failed. |
| 1 | At least one check failed. |
| 2 | The command line has a mistake. The probe prints its usage. |
| 3 | Refused to start: Panopticon or another probe is running. |
| 130 | Stopped with Ctrl+C. |

A FAIL can be a wiring or profile mistake, or a behaviour of your camera that
Panopticon does not handle yet. Fix what the detail points at on your side (a
wire, a profile value, SpinView still open), run the stage again, and send what
remains.

Each run writes `probe_out\flir_probe_<computer>_<date>-<time>.json`, and a log
with the same name ending in `.log`, in the repository folder.

### Optional stages

| Stage | What it measures |
|---|---|
| `--exposure-sweep` | The longest exposure at which every camera takes every trigger at `frame_rate`. Needs the trigger board. |
| `--wrap-test` | Whether the frame ID wraps after 65535, once per model, at a small frame and the fastest free-run rate. Up to 15 minutes per model. |
| `--pyspin` | Whether PySpin copies each image, and whether its wait for an image stalls Python's other threads. Needs PySpin. |

Run each with the probe, for example `uv run probe_flir.py --exposure-sweep`.

## 6. Record a short test

1. Run `uv run gui.py`. Panopticon opens your profile. Check that every camera
   shows a picture in the preview.
2. Fill in the metadata and record for 2 minutes: press **Record**, and press it
   again to stop. Take a screenshot of the window while it records.
   [WORKFLOW.md](WORKFLOW.md#8-record) describes a recording in full.
3. Wait until encoding finishes and the sidebar status returns to `IDLE`, then
   quit. A dialog appears only when something went wrong, such as
   `Recording completed with problems`, and what it says is also in
   `WARNINGS.txt`.
4. Collect the recording:

   ```powershell
   uv run probe_flir.py --collect <recording folder>
   ```

The recording folder is `<output_dir>\<date>\<session>\recording`
([WORKFLOW.md](WORKFLOW.md#paths-and-names) explains the names). Name the session
folder above it instead to collect the calibration too. `--collect` opens no
camera or board, so it also runs while Panopticon is open.

Once the maintainers have looked at it, record for 30 minutes and collect that
recording the same way.

## 7. Send the results

### Where the logs are

| File | Where | What it holds |
|---|---|---|
| `panopticon_<date>_<time>.log` | `logs\` in the repository folder, one per launch | Everything Panopticon printed, after a header describing the computer, the SDK, the profile and each camera |
| `session.log` | Each recording or calibration folder | The part of that log from arming the cameras to the end of encoding |
| `WARNINGS.txt` | The recording folder, when there is something to report | What went wrong in that recording |
| `flir_probe_<computer>_<date>-<time>.json` and `.log` | `probe_out\` in the repository folder | One probe run |
| `flir_collect_<computer>_<date>-<time>.zip` | `probe_out\` in the repository folder | What `--collect` gathered |

Every log line starts with the time, to the millisecond, and the name of the
thread that printed it.

`--collect` puts these files in its zip:

- from the folder you name: every `session.log`, `session_metadata.json`,
  `blockids.npy`, `frametimes.npy` and `WARNINGS.txt`;
- the Panopticon log the recording names, and the newest log in `logs\`;
- the 20 newest probe JSON files, each with its log;
- `MANIFEST.txt`, which lists every file with its size.

It picks files by name, so no video goes into the zip.

### What to send

Open an issue with the
[FLIR bring-up template](https://github.com/talmolab/panopticon/issues/new?template=flir_bringup.md)
and attach:

1. Before any recording: the JSON and log files in `probe_out\` from your probe
   runs.
2. After the 2-minute recording: its `--collect` zip, and the screenshot.
3. After the 30-minute recording: its `--collect` zip.

The JSON files, the logs and the zip contain your computer's name, its network
adapters and IP addresses, your camera serial numbers, and folder paths that
usually include your Windows user name. GitHub issues are public. If you would
rather not post these files, say so in the issue and ask for another way to send
them.

## Use your own trigger source

With `trigger_source: external` in the profile, Panopticon opens no trigger
board. Your pulse generator or DAQ drives every camera's trigger input, and you
start and stop it yourself. Start from
[`external_ttl.yaml`](../profiles/templates/external_ttl.yaml).

What changes:

- Stimulation needs Panopticon's board, so the Stimulation editor is
  unavailable.
- Panopticon cannot read your source's rate. Set `frame_rate` and
  `calibration_frame_rate` to the rates you run it at. The block-ID rate check
  after each recording compares the two.
- The profile refuses `serial_port`, `trigger_pins` and `stim_safe_pins`.
- Wire the source's output to each camera's trigger input with a common ground,
  as in [Wire the trigger](#2-wire-the-trigger). Check that one output can drive
  every input it feeds.

Every camera has to be armed before the first pulse. A camera armed after it
counts its frames from a later pulse than the others, and nothing in the files
would show that. So a recording runs in this order:

1. Keep your source stopped, and press **Record** (or **Calibrate**).
2. Panopticon arms every camera and watches them for at least half a second. If
   any camera receives a frame, the source was already running. Panopticon then
   refuses the recording, names the cameras that received frames, and removes
   what the start wrote. Stop the source and press **Record** again.
3. When the prompt `Every camera is armed. Start your trigger source now`
   appears, start the source at `frame_rate`. The recording begins with the first
   pulse. If no camera receives a pulse within 45 s, the start is cancelled and
   nothing is kept. **Cancel** on the prompt does the same.
4. To finish, stop your source. The recording ends once no camera has received a
   frame for 2 s. You can also press **Record** first. Panopticon then asks you
   to stop the source, and if frames still arrive 30 s later it stops the cameras
   itself and notes it in `WARNINGS.txt`.

Calibrate runs the same way, at `calibration_frame_rate`.

The probe works with your source too:

- `--find-line` asks you to start the source and watches the lines for up to
  45 s. Its prompt names `frame_rate`, but any rate works for this stage if each
  pulse lasts at least 10 ms, because the probe reads each line's level every few
  milliseconds. About 10 Hz is safest.
- `--triggered` asks you to start the source once every camera is armed. Keep it
  running until the probe asks you to stop it, because the probe checks that the
  triggers reached every camera up to that point. A frame that arrives before
  every camera is armed fails the run, just as Panopticon refuses such a
  recording.
- The counter checks after `--triggered`, and `--exposure-sweep`, need
  Panopticon's board. The probe skips them and says so.

## Known limitations

- Nothing has run on FLIR hardware yet ([Status](#status-nothing-has-run-on-flir-hardware-yet)).
- Mono8 only. A colour camera works if it offers a Mono8 pixel format.
- One vendor per rig. A profile names one camera backend, so a rig cannot mix
  FLIR and Basler cameras.
- Windows only.
- A model whose frame ID does not restart at each acquisition records with the
  camera's count of trigger edges instead, which needs the `CounterValue` chunk.
  A model with neither is refused when Panopticon opens it.
- Panopticon checks that each camera's own link can carry its frames at
  `frame_rate`. It does not add up the cameras that share a USB3 controller, a
  hub or a 1 GbE link. Spread the cameras over controllers and links, and test
  them together in SpinView.
- On a camera without counters there is no count of ignored triggers. The
  block-ID rate check after each recording is then the only check, as on a
  Basler rig, and the log says the camera has no counters.
- Where the camera's `ExposureTime` maximum does not follow the frame rate,
  Panopticon estimates the exposure ceiling from the camera's readout time, and
  the log says so. `--exposure-sweep` measures the real ceiling.
- A camera that reports no shutdown temperature is judged by its own
  temperature status instead. One that reports neither raises no temperature
  alert. The log says which, once per camera.
- With your own trigger source, stimulation is unavailable, and the rate is
  checked only after each recording.

## When something refuses

A refusal about one camera starts with that camera's name and serial number.

| The message says | What to do |
|---|---|
| `The Spinnaker SDK is not installed` | Install the Spinnaker SDK 4.x, or set `camera.flir.sdk_dir` to its install folder. |
| `is open in another program` | Close SpinView or any other camera software, then choose the profile again. |
| `is not an input on this camera` | Run `--find-line` and write the line it prints into `camera.trigger.line`. |
| `is set as an output on this camera` | Set the line to Input in SpinView, or wire the trigger to another input line. |
| `not the recording-only sketch` | Open your profile in Panopticon ([section 4](#4-open-your-profile-in-panopticon)), quit, and run the probe again. |
| `cannot be aligned by frame ID, and it offers no CounterValue chunk` | This model cannot record yet. Send the probe's output. |
| `DeviceLinkThroughputLimit can go no higher` | The link cannot carry the frames. Lower `frame_rate` or the frame size, or check the cable and port. |
| `AcquisitionFrameRate can go no higher than` | The camera cannot reach `frame_rate` at this frame size. Lower `frame_rate` or the frame size. |
| `is above what this camera can expose` | Lower `camera.exposure_us` to the longest exposure the message gives, or add light. |
| `REFUSING TO START: Panopticon is already running` | Quit Panopticon and any other probe, then run the probe again. |
| `received frames before every camera was armed` | Your source was running while the cameras armed. Stop it, and start it only when asked. |
