# A session, end to end

A session is one visit to the rig. You launch Panopticon, choose the rig's
profile and the output folder, describe the animals, calibrate, solve the
calibration, optionally load a stimulation
[paradigm](GLOSSARY.md#paradigm), and record.

[OVERVIEW.md](OVERVIEW.md) describes each control of the window.
[INSTALLATION.md](INSTALLATION.md) covers building and wiring a rig, and
[CONFIGURATION.md](CONFIGURATION.md) every setting of a profile.
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) lists the messages Panopticon shows,
with their causes and fixes, and [GLOSSARY.md](GLOSSARY.md) defines terms such
as [block ID](GLOSSARY.md#block-id) and [kick-out](GLOSSARY.md#kick-out).

The page is written for a rig of N cameras. A value marked "on the reference
rig" comes from `profiles/3dpose.yaml`, which describes one nine-camera Basler
rig.

- [1. Launch](#1-launch)
- [2. Choose a profile](#2-choose-a-profile)
- [3. Set the output directory](#3-set-the-output-directory)
- [4. Fill in the metadata](#4-fill-in-the-metadata)
- [5. Calibrate](#5-calibrate)
- [6. Solve](#6-solve)
- [7. Optional: stimulation](#7-optional-stimulation)
- [8. Record](#8-record)
- [9. After you stop](#9-after-you-stop)
- [10. What the session leaves on disk](#10-what-the-session-leaves-on-disk)

---

## 1. Launch

Start Panopticon from the desktop shortcut, `_launch.bat` or a terminal:

- Double-click the desktop shortcut, if `make_shortcut.ps1` has made one. It
  opens no console window. It also installs no new dependency, so after an
  update that changes `pyproject.toml`, run `uv sync` once or use
  `_launch.bat`.
- Double-click `_launch.bat` in the repository folder. It runs
  `uv run python gui.py`, which first installs any dependency that
  `pyproject.toml` has gained. Its console stays open if Panopticon exits with
  an error.
- Run `uv run gui.py` in a terminal in the repository folder. The log then
  appears in the terminal as it is written.

Only the terminal passes options. `--profile <name>` opens that profile and
remembers it for later launches. `--force` starts a second copy of Panopticon.

Only one copy runs at a time. A second launch, or a launch while one of
Panopticon's probes runs, shows `Panopticon is already running` and exits with
status 3. Two copies would compete for the cameras, the
[trigger board](GLOSSARY.md#trigger-board) and the GPU encoder. Use `--force`
only once you know what the other copy is doing.

A splash screen reads `Loading cameras...` while the window builds.

### The log

Everything Panopticon prints goes to `logs\panopticon_<date>_<time>.log` in the
repository folder, one file per launch. Each line starts with the time, to the
millisecond, and the name of the thread that printed it:

```
2026-09-24 10:15:02.481 [MainThread] [acq] profile: 3dpose
```

Near the top, the log holds a header of `[header]` lines. It describes the
computer: the Panopticon version and git commit, Python, Windows, the CPU,
RAM, the GPU and NVIDIA driver, the
[NVENC session](GLOSSARY.md#nvenc-session) cap, package versions and
the network links. It then lists every field of the profile and each open
camera. The header is written again at every profile switch and at the start
of every acquisition.

The profile's `log_level` sets how much else the log holds
([CONFIGURATION.md](CONFIGURATION.md#log_level)). The default, `verbose`, adds
the camera settings Panopticon asked for and read back, and a `[state]` line
at each step of an acquisition. No level logs each frame, and printing never
makes a capture thread wait. If the log writer falls behind, it drops lines
and says so with `[log] N log lines dropped`. `probe_flir.py` writes its own
log at `verbose`, or at `debug` when the profile asks for it.

Quote the launch log and the acquisition's `session.log`
([section 10](#10-what-the-session-leaves-on-disk)) when you report a problem.

### What happens at launch

With a profile remembered on this computer, or named with `--profile`,
Panopticon at launch:

1. Opens the profile's cameras and starts the preview.
2. Checks the hardware in the background. The status bar reads
   `Checking hardware: Record and Calibrate are available once it reports`,
   and those two toggles stay disabled until it reports. The check measures
   the output drive, probes how many NVENC sessions the GPU driver grants,
   times a libx264 encode and picks the encoder. A `Hardware Check` dialog
   lists any problem it finds, for example:
   - fewer than 4 CPU cores, under 16 GB of RAM, under 500 GB free or under
     500 MB/s of writing on the output drive;
   - an NVENC path that does not work, or an encoder that ignores the
     keyframe setting, which would leave recordings with one keyframe and
     hard to seek;
   - an encoder choice that will refuse the next Record;
   - a check that could not finish.

   Read the dialog. It stops nothing. The status bar then reads
   `Hardware check done: encoding with <encoder>`.
3. Puts the trigger board back to the
   [recording-only sketch](GLOSSARY.md#recording-only-sketch), which
   triggers the cameras and drives no stimulation pin. A paradigm lives in
   the board's flash memory and survives quitting, a power cycle and an
   unplugged cable. So Panopticon reflashes the board at every launch, unless
   this computer's record says the board already carries the recording-only
   sketch. The sidebar reads `Clearing stim firmware…` for about 30 s. If
   the flash fails, a `Could not clear stim firmware` dialog says the board
   may still carry a paradigm, possibly a looping one. Open Stimulation and
   press Apply with an empty canvas before you record, or switch the laser
   off.
4. Opens the board's serial port and holds it until you quit. Opening the
   port resets the board. Panopticon then asks the board which sketch it
   runs, and reflashes it once if the answer is not the recording-only sketch.

Stimulation stays off until you Apply a paradigm in this launch
([section 7](#7-optional-stimulation)).

Resetting and flashing the board leave its pins floating for a moment, and a
powered laser driver can read a floating input as on. On a rig with a laser,
read the [laser warning](#7-optional-stimulation) before you launch.

On a computer where Panopticon has not opened a profile yet, the window opens
no camera and no serial port, runs no hardware check and programs no board.
The same holds when the remembered profile, or the one `--profile` names, does
not load. A `Choose your rig's profile` dialog says why. Choose the profile in
the dropdown at the top of the sidebar ([section 2](#2-choose-a-profile)).

If no profile loads at all, a `No rig profile` dialog appears instead, and
Record and Calibrate stay disabled. The board has not been cleared then and
may still carry a paradigm. Switch the laser off, fix the profile the dialog
names, and start Panopticon again.

### A good launch

![Panopticon at idle](images/main_idle.png)

Every camera has a live pane, and each pane reads about `30 fps`. Between
acquisitions the cameras run free at 30 fps for the preview. They switch to
the trigger rate only while you calibrate or record. The state label at the
bottom right reads `IDLE`.

If a pane is missing or black, fix that before anything else.
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) explains the messages a camera open
can show.

---

## 2. Choose a profile

A profile is one YAML file in `profiles/` that describes one rig: its cameras,
frame rates, trigger board and pins, encoder and output folder.
[CONFIGURATION.md](CONFIGURATION.md) describes every field and has templates
to start from. The dropdown at the top of the sidebar lists every profile that
loaded. A file that fails to load is left out, and after the window opens a
`Rig profiles` dialog names the file and the field at fault.

Choosing a profile closes the cameras and opens the new profile's. The sidebar
reads `Switching cameras…` for a second or two. Panopticon remembers the choice
on this computer and opens it at the next launch.

When the new profile names a different serial port, Panopticon sends the old
board a stop and closes its link. The new board then gets the launch sequence,
including the 30 s flash. Choosing a profile on a new computer runs the same
sequence on that profile's board.

[OVERVIEW.md](OVERVIEW.md#4-profile) says when the dropdown is disabled and
when it refuses a switch.

### Camera names

Panopticon names the cameras `cam1` to `camN`. Every file name and the
calibration use these names. With `camera_serials` in the profile, `cam1` is
the first serial in that list. Without it the names follow the serial numbers
in order, so a camera missing from the set renames every camera after it. The
files keep their usual names, and the calibration then describes the wrong
cameras.

Set `camera_serials`, or at least `n_cameras`, so the open is refused instead
([CONFIGURATION.md](CONFIGURATION.md#camera_serials)):

- `Expected N cameras but M are available to open.` lists the serials it
  found.
- `Requested cameras did not enumerate: ...` names the listed cameras that are
  missing.

Power-cycle the missing camera and choose the profile again.

The open is also refused when a camera's pixel format is not Mono8, or when
its image size differs from the profile's `frame_width` and `frame_height`.
A profile with `capture_processes` above 0 opens no camera: capture in several
processes is experimental and the window cannot use it yet
([CONFIGURATION.md](CONFIGURATION.md#capture_processes)).

For FLIR cameras, [FLIR.md](FLIR.md) covers the profile's `camera:` block, the
Spinnaker SDK and `probe_flir.py`, which tests each camera before a first
recording.

A profile with `trigger_source: external` takes its triggers from your own TTL
source. Panopticon then opens no serial port and flashes nothing, and a
recording follows [Your own trigger source](#your-own-trigger-source).

---

## 3. Set the output directory

The button under the dropdown shows the folder that sessions are written
under. Click it to choose another folder. Choose the profile first, because
switching profiles and relaunching both reset the folder to the profile's
`output_dir`.

Every session is written as
`<output>/<date>/<mouse1>_<mouse2>/<calibration|recording>/`. Put the output
folder on your largest, fastest drive. The disk check at Record measures that
drive.

---

## 4. Fill in the metadata

| Field | Default | Used for |
|---|---|---|
| Date | today, as `YYYYMMDD` | folder and file names |
| Mouse 1 | blank, which becomes `m1` | folder and file names |
| Mouse 2 | blank, which becomes `m2` | folder and file names |
| Assay, Experimenter, Cohort, Cage, Notes | the profile's `metadata_defaults` | `session_metadata.json` |

With Date `20260904` and both mice blank, the session folder is
`<output>/20260904/m1_m2/`, and cam1's recording is
`20260904-m1_m2-cam1-recording.mp4`.

The reference rig's profile fills in Experimenter `IT` and Assay `open_field`.
Put your own values in your profile's `metadata_defaults`, because whatever a
field holds when an acquisition ends goes into `session_metadata.json`.
Switching profiles fills in only the fields you have not typed into.

Fill in Date and the mice before you calibrate. Solve looks for the
calibration in the folder the fields name when you press it, so a changed
mouse ID sends it to another folder. Until you type into Date, it moves to the
new day when you press Calibrate or Record. A session started after midnight
is then filed under the day it started.

A Date that is not a `YYYYMMDD` calendar date is refused with a
`Check the session details` dialog. So is a value that cannot be part of a
folder name, such as one with a slash, `..`, a reserved Windows name or a
trailing dot or space.

Press Snapshot to save one full-resolution PNG per camera into
`<session>/snapshots/<date>_<HHMMSS>/`.
[OVERVIEW.md](OVERVIEW.md#10-snapshot) says what a snapshot shows.

---

## 5. Calibrate

A calibration records a [ChArUco board](GLOSSARY.md#charuco-board) carried
through the arena. The solve ([section 6](#6-solve)) turns it into each
camera's lens model and the cameras' positions relative to each other.

### Check the board first

The coverage display and the solve read the board's description from the file
the profile's `board_config` names. On the reference rig that is
`configs/boards/charuco_8x8_15mm.yaml`: 8 × 8 squares of 15.0 mm, 10.0 mm
markers from the 4×4 dictionary of 1000, printed in the layout OpenCV used
before 4.6 (`board_legacy: true`). Count the squares on your board and
measure one before you calibrate.

The coverage display counts board markers only, so it catches only some
mismatches:

- A board printed from another dictionary than `marker_bits` and `dict_size`
  name can give no detections at all. The coverage display then stays dark.
- A wrong square count or layout (`board_x`, `board_y`, `board_legacy`) still
  finds every marker. The display lights up and can reach READY, and the solve
  then finds few or no board corners.
- A wrong `square_length` gives a solve that looks good and a 3D
  reconstruction at the wrong scale, because every reprojection error is the
  same at any scale.

[CONFIGURATION.md](CONFIGURATION.md#board_config) describes the board file.

### Start

A calibration is an acquisition, so the checks under [Record](#8-record) run
for it as well. They include the prompt when the `calibration/` folder already
holds data. A calibration always runs under the recording-only sketch. If a
paradigm is on the board, Panopticon first flashes the recording-only sketch
(`Flashing recording-only firmware…`, about 30 s). A calibration therefore
never runs stimulation while you stand in the arena.

Flip Calibrate on. The cameras switch to triggered mode at
`calibration_frame_rate` (30 fps on the reference rig), and the preview shows
every frame. The profile's `calibration_exposure_us` and `calibration_gain_db`
replace the recording exposure and gain for this acquisition only. An exposure
of 0 or a gain of -1 keeps the recording value, and the reference rig keeps its
recording gain this way. Panopticon caps the exposure at 90% of the exposure
ceiling at the calibration rate. When it does, the camera's exposure line in
the log says `CLAMPED`
([CONFIGURATION.md](CONFIGURATION.md#calibration_exposure_us)). The next
recording uses the exposure from the camera settings again.

Move the board slowly and pause at each pose. A long exposure blurs a moving
board, and a blurred board yields no corners.

### Work towards READY

The coverage display in the sidebar shows what the cameras have seen.
[OVERVIEW.md](OVERVIEW.md#the-calibration-coverage-hud) explains each mark and
what READY needs, and gives the caption's format. The figures come from a
six-camera profile, and their captions lack the `groups` segment yours shows.

| | |
|---|---|
| ![Coverage graph, nothing detected](images/calib_stage_1_start.png) | ![Coverage graph, partial coverage](images/calib_stage_2_partial.png) |
| 1. Nothing detected yet. Hold the board where at least two cameras see it. | 2. Two cameras see the board now. Carry it into the corners of each view and where views overlap. |
| ![Coverage graph, nearly ready](images/calib_stage_3_nearly.png) | ![Coverage graph, READY](images/calib_stage_4_ready.png) |
| 3. One thin edge is left. Show the board to both of its cameras at once. | 4. READY. Flip Calibrate off, or keep going to add frames. |

When a count stops climbing, find the one that is stuck:

- `grid` below its target: carry the board into the corners of that camera's
  view.
- `groups` above `1/1`: the orange line above the caption lists the groups,
  for example `{1,2,3} {4,5}`. Show the board to one camera of each group at
  the same moment.
- A node that never lights: that camera does not detect the board. Check its
  view, its focus and the board file.
- `paired` slow for one camera: hold the board where that camera and a
  neighbour both see it. For two cameras that face each other, hold it edge-on
  between them.

You can stop before READY appears. The recording is still a valid calibration,
and the solve reports how many cameras it could place.

If the board is too dark to detect, add infrared light first, then raise
`calibration_exposure_us`. The
[brightness and contrast sliders](OVERVIEW.md#13-and-14-brightness-and-contrast)
do not help.

Flip Calibrate off to finish. Panopticon writes `codet_frames.json` beside the
videos for the solve ([section 6](#6-solve)). The videos are then finalised as
for a recording ([section 9](#9-after-you-stop)).

---

## 6. Solve

With the state at `IDLE`, press Solve. Solve runs `1_calibrate.py` with
Panopticon's own Python on the calibration of the session the fields name,
using the profile's `board_config`. It takes a few minutes. The
[state label](OVERVIEW.md#16-state) shows the solve, and progress goes to the
status bar and the log. Solve gives up after 30 minutes.
[OVERVIEW.md](OVERVIEW.md#controls-that-hide-and-controls-that-disable) lists
the controls that stay disabled until it ends.

`codet_frames.json` lists the triggers at which two or more cameras saw the
board. When it matches the videos, the solve decodes only those frames.
Otherwise it scans the videos, looking at every third frame until it
finds the board (`--skip 3`). A hint file it cannot use, for example one
written for other videos, raises a warning.

The solve writes into `calibration/`:

- `calibration.toml`: per camera a size, a camera matrix, distortion, a
  rotation and a translation, in aniposelib's layout. A `[metadata]` block
  holds the solve's quality figures. Match cameras by each section's `name`.
- `calibration_report.json`: the same figures, for the window.
- `reprojection_error_histogram.png`: the pairwise quality plot. Nothing
  opens it for you.

Solve then copies `calibration.toml` into `recording/`, so each recording
carries the calibration it was made with. If `recording/calibration.toml`
exists and differs, Panopticon asks `Replace the recording's calibration?`,
with No as the default. Answer Yes when you have recalibrated and not recorded
yet. Answer No when a recording in that folder was made with the calibration
already there. The new solve then stays in `calibration/`.

| Status bar | Meaning |
|---|---|
| `Solved N of M cameras — copied to <path>` | Copied into `recording/`. N below M means the solve dropped cameras |
| `Solved N of M cameras — kept in calibration/, recording's copy left unchanged` | You answered No |
| `Calibration solved (no toml found to copy)` | Treat it as a failure and read the log |
| `... (with warnings)` | A `Calibration Warnings` dialog listed problems |

### Which cameras made it into the solve

The solve drops a camera when:

- it has no calibration video it can read;
- it has fewer than 5 detection frames;
- its lens model fails, which needs 20 or more frames with 6 or more corners
  (`intrinsics FAILED`);
- it is not connected to the largest group of cameras that saw the board
  together.

The solve also skips single views whose corners fit no homography, such as a
view of one row of the board, and says how many it skipped per camera.

A solve that drops cameras still succeeds. The `Calibration Warnings` dialog
then starts with `PARTIAL: solved N of M cameras.` and names each dropped
camera with its reason. The log then names the cameras that went in:

```
Calibration complete (PARTIAL).
  ...\calibration\calibration.toml
  REPORT_PATH=...\calibration\calibration_report.json
  Cameras: cam1 cam2 cam3 cam5 cam6
```

A dropped camera adds nothing to 3D, however good its video. Record the
calibration again, holding the board where that camera and a neighbour both
see it.

The solve fails, with a `Calibration Failed` dialog, when too few cameras are
left to place: fewer than two with detections or lens models, no pair that saw
the board together, or fewer than two connected. It also fails with a board
file it cannot use. Before any solve runs, a missing calibration folder or
missing videos give a `No Data` dialog, and a missing board file a
`Missing Board Config` dialog.

The `Calibration Warnings` dialog also warns about:

- a pair whose stereo error is poor, or that shares fewer than 10 frames;
- a camera with fewer than 30 detection frames;
- a camera name that another acquisition of the session records with a
  different serial, because the calibration then describes other cameras.

### Reading the pairwise calibration plot

`reprojection_error_histogram.png` is a bar chart titled
`Pairwise calibration quality`, with one bar per camera pair. Each bar is the
pair's [stereo RMS](GLOSSARY.md#stereo-rms) in pixels. It measures how far
the corners seen by the two cameras land from where that pair's geometry puts
them. The fit uses up to 30 shared views chosen to span different poses. The
number belongs to the pair, before the pairs are chained into one coordinate
frame.

| Colour | Stereo RMS | Meaning |
|---|---|---|
| Green | below 1.5 px | Good |
| Amber | 1.5 to 3 px | Worth a look |
| Red | 3 px and above | Poor, and the solve warns about it |

A dashed line marks 1.5 px and a dotted line 3 px. Read the pair names first,
then the heights:

- Every bad bar includes the same camera and the other pairs are good: that
  camera is the fault. Check its focus, and its mount for anything that moved
  since the calibration, then calibrate again.
- One bad pair whose cameras are good with everyone else: the two cameras
  share too few views, or only oblique ones. Calibrate again, showing the
  board to both at once.
- A missing bar: that pair never shared 5 views, so there is nothing to plot.
  Count the bars against the pairs you expect.

The plot cannot show a wrong `square_length`, which scales every 3D
coordinate and leaves every bar the same. A dropped camera has no bars at all.
The chained solve has no bundle adjustment, so small errors add up along a
long chain of pairs.

### Solving by hand

From the repository folder:

```
uv run python 1_calibrate.py <session_dir> --board-config configs/boards/<your_board>.yaml
```

`<session_dir>` is the folder that holds `calibration/`. Pass the board file
your profile names. Add `--excluded-views cam4` to leave a camera out, or
`--skip 1` to scan every frame. `--skip` counts only when the solve does not
use `codet_frames.json`, so move that file aside first. A solve by hand writes
into `calibration/` and copies nothing into `recording/`, so copy
`calibration.toml` yourself.

The solve fits each lens on at most 60 pose-diverse frames, and each pair on
at most 30, so that it finishes in minutes. For the best calibration, solve
from full videos with a package that does bundle adjustment. sleap-anipose and
aniposelib both read the `calibration.toml` this writes.

### Keep the cameras still

A calibration describes the cameras as they were while the board was recorded.
From the calibration to the last recording of the session, do not move,
re-aim, refocus or re-mount any camera. If one is bumped, calibrate again
before you record. A moved camera changes no frame count, plot or warning.
Only the 3D output is wrong.

---

## 7. Optional: stimulation

Skip this section when the session has no optogenetic stimulation.

> [!WARNING]
> Flashing resets the trigger board, and the laser driver input floats during
> the reset. Switch the laser off or block the beam before you launch
> Panopticon, before Apply, and, while a paradigm is Applied, before each
> Calibrate and each Record that follows a calibration.

Choosing a profile on a new computer, or switching to a profile on another
serial port, flashes the board too. Fit the laser's own interlock if you need
a hard gate.

Set up stimulation after calibrating and before recording, so that Record
starts without a flash. Open the editor with the Stimulation button.
[OVERVIEW.md](OVERVIEW.md#the-stimulation-editor) names each of its controls.

![The stimulation editor](images/stim_clean.png)

### Pins

The trigger board's numbered sockets are its pins. A digital output pin is
either at 0 V (LOW) or at 5 V (HIGH), which a TTL input reads as off or on.
The number you type into Pin is the number printed beside the socket. Nothing
in the software knows what is wired to a pin, so a wrong number drives the
wrong device. On the reference rig pin 53 drives the laser driver's modulation
input. If a driver has a TTL/analog switch, use TTL, because analog mode maps
0-5 V onto output power.

A stimulation block cannot use a camera trigger pin, one of the profile's
`trigger_pins` (`[2, 4, 6, 8, 10, 12]` on the reference rig). Extra edges on a
trigger line give one camera extra frames, so its block IDs stop matching the
other cameras'. Every alignment step would then pair the wrong frames. The
editor refuses such a block:

```
Pin 2 cannot carry a stim waveform: camera trigger line — extra edges on one
camera would break cross-camera block-ID alignment.
```

Pins 0 and 1 carry the board's serial link and are refused too, as is a pin
the board does not have.

### When the board resets

The profile's `stim_safe_pins` (`[53]` on the reference rig) are driven LOW as
the first action of every sketch Panopticon builds, before the board waits for
the host. Every pin a paradigm uses joins that list. While the board resets
and waits in its bootloader, no sketch runs and every pin floats. That happens
whenever the serial port is opened or the board is flashed:

- at launch, and when you choose a profile on a new computer or one on another
  port;
- at every Apply;
- before an acquisition that needs the other sketch. Panopticon holds two
  sketches per launch, recording-only and recording plus stimulation, and
  swaps them itself. After an Apply, a calibration flashes the recording-only
  sketch (`Flashing recording-only firmware…`). The next recording flashes the
  paradigm back (`Flashing recording + stimulation firmware…`). Each flash
  takes about 30 s;
- at the first Calibrate or Record after the port could not be opened. At
  launch the log then says `will retry on first use`. After an Apply the
  editor's status line says that Record reopens the port;
- at a start the board does not acknowledge at first. Panopticon reopens the
  port once to reset the board, and the log says
  `[teensy] no ack — reopening port to force a board reset`.

A failed flash refuses the acquisition, because the board's contents are then
unknown. The dialog says to switch the laser off and check the board.
Panopticon refuses to close during a flash, because an interrupted upload can
leave the board without its safe-pins guard, and then the laser pin floats at
the next power-up. For the same reason, do not end Panopticon from Task
Manager during a flash: while the sidebar reads `Clearing stim firmware…` or a
`Flashing …` state, or the editor reads `Compiling + uploading… (~30 s)`.

### Apply, then Record

The paradigm is compiled into the board's sketch. Nothing you draw reaches the
board until Apply compiles and uploads it, which takes about 30 s. Record then
sends its usual start command, and the board runs the paradigm from the first
trigger on its own clock.

Record refuses to start when the canvas and the board would disagree:

| Dialog | Cause |
|---|---|
| `Apply the stimulation paradigm first` | The canvas holds a paradigm that was never Applied in this launch |
| `Apply the edited paradigm first` | The canvas changed after the last Apply |
| `Apply the empty canvas first` | The canvas is empty, but the board would still run the paradigm Applied earlier |
| `Cannot record with this stim workflow` | A failed Apply, an upload in progress, or a graph problem the editor shows in red, such as a forbidden pin |
| `Stop the stimulation test first` | A Test is running |

The last two refuse Calibrate as well. To reuse a saved paradigm in a later
launch, Load it and press Apply.

### Build a paradigm

A block drives one pin with one square wave for a set time. An arrow means
"when this block ends, start that one", so a chain runs in sequence. Chains
that are not connected run at the same time.
[OVERVIEW.md](OVERVIEW.md#the-stimulation-editor) describes each field and
button, and the rules a graph must follow.

1. Type Pin, Freq (Hz), PW (ms) and Dur (s), and press Create Block.
2. Drag from a port on one block to a port on another to connect them.
3. For a pause, add a block at [0 Hz](OVERVIEW.md#4-freq-hz). For a loop,
   tick [Starting](OVERVIEW.md#7-starting) on one of its blocks. To stop the
   recording when a block finishes, tick [Ending](OVERVIEW.md#8-ending) on it.
4. Read the preview's caption before you Apply. A pulse as long as its period
   or longer holds the pin HIGH for the whole block
   ([the waveform preview](OVERVIEW.md#the-waveform-preview)).
5. Save writes the graph as JSON, by default `stim_config.json` in the output
   folder. Load reads one back.

### A worked paradigm

This paradigm gives a 5-minute baseline, 30 s of 20 Hz stimulation and a
5-minute post-period, and stops the recording at the end. It is three blocks
on pin 53, joined in one chain:

1. 0 Hz, 0 ms, 300 s: the baseline. The sequence starts with the first
   trigger, so a baseline is a block at 0 Hz.
2. 20 Hz, 10 ms, 30 s: 20 pulses a second, 10 ms each, at 20% duty.
3. 0 Hz, 0 ms, 300 s, with Ending ticked.

The status line then reads `Recording will stop 630 s after start.` Press Test
with the beam blocked, and watch the paradigm run once without recording. Then
press Apply, wait for `Upload successful — press Record to run paradigm.`, and
press Record.

### Test

Press Test to run the paradigm on the board with no camera triggered and
nothing recorded. [OVERVIEW.md](OVERVIEW.md#10-test) describes its countdown,
how a looping test ends, and what to do when the board does not confirm the
stop.

### After a stimulated recording

A recording with blocks on the canvas also writes, beside the videos:

- `stim_paradigm.json`: the paradigm as it stood when the recording started,
  the sketch's hash and `matches_uploaded_firmware`. Read that field first.
  `true` means the file describes the sketch the board ran, and Record starts
  only when it is. A file that reads `false` (the canvas differed from the
  sketch) or `null` (nothing was uploaded) does not describe what the board
  ran.
- `stim_paradigm.ino`: the sketch's source.
- `stim_trace.csv`: one row per trigger, with the stimulus the paradigm should
  have delivered at that trigger ([section 10](#stim_tracecsv)).

`stim_trace.csv` is computed from block IDs, as `t = (blockid - 1) / fps`. It
is a model: it cannot know whether the laser was on, the interlock in or the
beam blocked. For evidence that the laser fired, put its sync LED in a
camera's view. At 100 fps with a 3 ms exposure a camera resolves when a train
starts and stops, and a 20 Hz train aliases against the frame rate.
Pulse-level evidence needs a photodiode on a spare board input.

---

## 8. Record

Flip Record on. Panopticon runs these checks in order, and each one can refuse
the start before anything is recorded:

1. The stimulation editor: a Test running, a failed Apply, or a canvas the
   board does not carry ([section 7](#apply-then-record)).
2. The session fields ([section 4](#4-fill-in-the-metadata)).
3. The image size. The profile's `frame_width` and `frame_height` must match
   the cameras
   (`The profile records WxH but the cameras are configured for WxH`).
4. Capacity, for the open cameras at this acquisition's frame rate
   ([Capacity](#capacity)).
5. The board's sketch. When the board does not carry the sketch this
   recording needs, Panopticon flashes it, which takes about 30 s
   ([section 7](#when-the-board-resets)).
6. Data already in the folder
   ([If the folder already holds data](#if-the-folder-already-holds-data)).
7. The serial port. `Could not open the trigger board` means another program
   holds the port, such as the Arduino Serial Monitor. Nothing has been
   deleted at this point.
8. The cameras switch to trigger mode with the exposure and gain from their
   camera settings, so a calibration exposure never carries into a recording.
   A camera that cannot record at the frame rate refuses the start here, as a
   FLIR camera on a slow link does ([FLIR.md](FLIR.md#when-something-refuses)).
   Every camera must fill its frame ring and arm within 30 s.
9. A frame that reached any camera before the board started refuses the start
   (`Frames arrived before the trigger board was started`). Something else is
   triggering the cameras.
10. The board must acknowledge the start. Without the acknowledgement the
    cameras are rolled back and the start is refused
    (`The trigger board did not acknowledge the start command, so no triggers would be sent.`).
    A start the board did not take would record a session with no frames. A
    board that acknowledges with the wrong sketch is refused too, and the next
    start flashes the right one. So is a start whose acknowledgement was lost
    after the cameras had counted frames, because restarting the board then
    would shift every block ID.

### Capacity

Panopticon picks the encoder again at every start, for the cameras that are
open. These refuse the start:

- `No cameras are open.`
- `Not enough RAM for N cameras: ...`: the driver's buffers and the frame
  rings need more memory than is free. Lower `max_num_buffer` or
  `kick_max_lag` in the profile, or close other programs.
- `NVENC granted only S concurrent sessions but N cameras need one each (the driver caps this).`
  With `encoder: auto` this refuses only when libx264 on the CPU cannot take
  the cameras either, and with `encoder: nvenc` it always refuses. More
  cameras need a more capable GPU, and the driver's NVENC session cap is often
  the limit.
- `libx264 encodes about R fps per core at WxH here, enough for K cameras at F fps, but N are open.`
  with `encoder: x264`.
- `encoder: raw` with `realtime_encode: true`
  ([CONFIGURATION.md](CONFIGURATION.md#encoder)).

These warn, in a `Proceed?` dialog that asks `Start anyway?`:

- a disk that may be too small for a 10-minute recording (`Disk may be short`
  or `Disk is tight`);
- an NVENC session cap that could not be probed;
- encoding on the CPU with libx264, because NVENC granted too few sessions or
  because the CPU's speed was never measured;
- raw capture (`realtime_encode: false`) that writes faster than 1.5 GiB/s, or
  whose encode after the stop has to run on the CPU.

There is no RAM warning: RAM either refuses the start or raises nothing.

### If the folder already holds data

When the target `recording/` or `calibration/` folder holds a non-empty video,
`raw.bin`, `stream.h264`, `blockids.npy`, `frametimes.npy`, `alignment.npz` or
`stim_paradigm.json`, Panopticon asks `Overwrite the existing data?`. Cancel is
the default and leaves everything as it was.

> [!WARNING]
> Yes deletes that folder, all of it except `calibration.toml`, once Panopticon
> holds the trigger board's serial port. A start refused after that point,
> such as a camera that does not arm or a board that does not acknowledge,
> has already deleted the old data, and nothing brings it back. To keep an
> earlier take, change Date, Mouse 1 or Mouse 2 first, or copy the folder
> elsewhere.

Panopticon deletes the whole folder so that no file of the old take sits
beside the new one. A camera that recorded nothing would otherwise keep the
old take's video and block IDs under the same names, and the alignment would
mix two takes. `calibration.toml` stays because Solve copied it there for the
session. In `calibration/` that means the previous solve's `calibration.toml`
stays until the next Solve replaces it.

The prompt concerns the acquisition folder only. The session folder, its
`snapshots/` and the other acquisition's folder stay as they are.

With `trigger_source: external`, the old folder is moved aside into a hidden
folder beside it, and deleted only when the first trigger arrives. A start
that ends before then puts it back.

### While it records

The state label reads `RECORDING` in red. Each pane's frame rate should read
the trigger rate, 100 fps on the reference rig. One pane at about half the
rate usually means that camera's exposure is over the ceiling
([CONFIGURATION.md](CONFIGURATION.md#trigger_rate_limit)). The preview shows
every tenth frame during a recording.

With real-time kick-out (`realtime_kick: true`, the default), Panopticon
drops every trigger that some camera missed while it records, so every video
holds the same triggers. The status bar reports capture health each time
the [frame-rate labels](OVERVIEW.md#3-frame-rate) refresh. In kick-out it
counts how many triggers the slowest camera is behind the fastest, against a
cap of `kick_max_lag` (480 on the reference rig).

| Status bar | Meaning |
|---|---|
| `Capture healthy — every camera within N trigger(s) of the leader` | Every camera is within a quarter of the cap |
| `CAPTURE FALLING BEHIND: camN is N triggers behind the leader (cap C). Close other applications.` | Within three quarters of the cap. Nothing is lost yet |
| `camN IS N TRIGGERS BEHIND THE LEADER (cap C): frames every camera captured are being dropped. Stop and investigate.` | Three quarters of the cap or more. At the cap, triggers are dropped from every camera |
| `... RETIRED: camN` | That camera was retired, and the others stay aligned |
| `EVERY CAMERA IS RETIRED: nothing is being recorded. Stop the recording.` | Stop now |

With kick-out off, the status bar reports how late the slowest camera's frames
arrive. It reads `Capture healthy — keeping up with the trigger (max lag N ms)`
below 0.25 s, `CAPTURE FALLING BEHIND: camN is S s behind real time and growing.`
below 1 s, and `CAPTURE S s BEHIND REAL TIME (camN).` beyond that.

When a camera stops delivering, the status bar reads:

- `NO FRAMES from camN for S s`: one camera.
- `NO FRAMES FROM ANY CAMERA for S s: the trigger board may have stopped.`,
  with a `No frames from any camera` dialog. Check the board and its USB cable.
  On a rig with stimulation pins, check the laser too, because a board without
  power leaves those pins undriven.

A camera near its shutdown temperature puts `CAMERA TEMPERATURE: camN T C ...`
at the start of the status bar. The alert fires at the shutdown temperature
the camera reports, minus the profile's `thermal_warn_margin_c`
(2 C on the reference rig, so 79 C on cameras that shut down at 81 C). It also
fires whenever a camera reports its own over-temperature state. On a camera
that reports its shutdown temperature, the `Critical` status alone does not
raise it. A camera that reports no shutdown temperature is judged by its own
status instead: any status but `Ok` raises the alert, `Critical` included. A
camera that reports neither cannot be watched. The log says once per camera
which rule applies
(`[acq] thermal watch: camN reports no shutdown temperature`). The warning
reaches `WARNINGS.txt` when the recording lost frames, and always when a
camera reached its shutdown point. `camera_thermals` in `session_metadata.json` records each camera's
temperatures. Read `temp_max_c` there, because the current temperature falls
as soon as the load comes off.

### Stopping

Flip Record off, or let a block marked Ending flip it. Panopticon sends the
board a stop and keeps the serial port open, so the next recording does not
reset the board. If the board does not confirm the stop, a
`Trigger board did not confirm the stop` dialog appears. The board may still
be triggering, and a looping paradigm may still drive its pin. Power-cycle the
board and switch the laser off.

### Your own trigger source

With `trigger_source: external`, a pulse generator or DAQ that you run
triggers the cameras, and Panopticon opens no serial port. Stimulation needs
Panopticon's board, so a recording with blocks on the canvas is refused. Every
camera must be armed before the first pulse, so a recording runs in this
order:

1. Keep the source stopped, and press Record (or Calibrate).
2. Panopticon arms every camera and watches them for at least 0.5 s. A frame
   on any camera means the source was already running. The start is then
   refused (`The trigger was already running`) and nothing is kept.
3. The label reads `WAITING FOR TRIGGER`, and a `Start your trigger source`
   prompt appears. Start the source at `frame_rate`. The recording begins with
   its first pulse. With no pulse within 45 s the start ends in `NO TRIGGER`
   and nothing is kept. Cancel on the prompt also ends the start and keeps
   nothing.
4. To finish, stop the source. The recording ends once no camera has received
   a frame for 2 s. If you flip Record off before you stop the source, the
   label reads `STOP YOUR TRIGGER SOURCE`. Panopticon waits up to 30 s for the
   source to stop, then stops the cameras itself and notes it in
   `WARNINGS.txt`.

Panopticon cannot read the source's rate. After the recording, the block-ID
rate check compares each camera against `frame_rate`.
[FLIR.md](FLIR.md#use-your-own-trigger-source) covers the wiring.

---

## 9. After you stop

The state label names each step as it runs.

`Finishing…`: the encoders drain and the cameras return to free-run preview.
Panopticon writes each camera's `blockids.npy` and `frametimes.npy`, the
acquisition's `session_metadata.json`, `stim_trace.csv` for a stimulated
recording, and `session.log`. The frame buffers the acquisition used are freed,
so memory use does not grow from one recording to the next.

`ENCODING`: the sidebar shows `Encoding k/N`. With real-time encoding, the
default, each camera's `stream.h264` is copied into an mp4 in seconds. With
`realtime_encode: false`, each `raw.bin` is encoded, `encode_parallel` cameras
at a time. A source file is deleted only after its mp4 has been checked. A
camera whose encode fails keeps its source files and gets an
`encode_error.log`.

What follows depends on the mode:

- With kick-out, the videos already hold the same triggers and nothing is
  re-encoded. If a camera was retired or the cameras kept different frames, a
  `Videos are not equal length` dialog says so. It gives the `2_align.py`
  command to run ([Check the block IDs](#check-the-block-ids)).
- Without kick-out (`realtime_kick: false`), or with `realtime_encode: false`,
  the alignment runs when the cameras kept different frames. It shows as
  `ALIGNING` and `Aligning k/N`. It keeps the triggers every camera recorded,
  re-encodes each video to them, and rewrites each camera's block IDs and
  frame times, then `stim_trace.csv`. A retired camera, or one with no frames,
  is left out and named. The status bar ends with
  `Aligned: N synchronized frames per camera`.
- When a camera ended early or stopped for a while, the alignment writes its
  index and replaces no video, because aligning would cut the other cameras.
  A dialog says why, and [Check the block IDs](#check-the-block-ids) gives the
  options.

`IDLE`: the session is complete.

### Warnings

Problems appear in a `Recording completed with problems` dialog, and in
`WARNINGS.txt` in the acquisition folder. A camera folder gets its own
`WARNINGS.txt` when that camera's block IDs had to be repaired to match its
video, or its encode lost frames. A new take deletes the old take's
`WARNINGS.txt`, so the file always describes the take beside it.

| Line | Meaning |
|---|---|
| `Effective frame rate X fps (target Y).` | Triggers some camera missed were dropped from every video. The videos stay aligned. The counts are under `kickout` in `session_metadata.json` |
| `N triggers (...) are missing from every camera's video: they were force-dropped ...` | A camera fell more than `kick_max_lag` triggers behind |
| `camN was RETIRED mid-recording (...)` | That camera's video ends early, and the others stay aligned |
| `camN: block IDs advanced at R/s while ...` | Below the trigger rate, that camera ignored triggers: do not use the recording for 3D. TROUBLESHOOTING.md covers the other cases |
| `camN reached T C during this acquisition ...` | A camera ran hot. Shown when frames were lost or a camera reached shutdown |

The effective-frame-rate line appears when more than 0.5% of the triggers were
dropped this way. [TROUBLESHOOTING.md](TROUBLESHOOTING.md) explains every line
and every dialog. After an alignment, `Alignment reported problems`,
`Cameras left out of the alignment` and `Videos were not aligned` go into
`WARNINGS.txt` too.

A `Recording did not finish cleanly` dialog means saving failed, for example
on a full disk. The capture files stay in the folder, neither encoded nor
deleted. Do not start another recording into that folder. Once the cause is
fixed, `uv run python 0_encode.py "<folder>"` turns them into mp4s. The cameras
are closed. Switch profile and back, or restart Panopticon, to reopen them.

### Check the session

Check the session before the animal goes back, while the rig is still set up:

1. The status bar reads `Recording encoded: F frames, R fps`. One frame count
   means every camera kept the same number. A range (`F1-F2 frames`) means
   they did not. `k CAMERA(S) FAILED: camN` names cameras with no usable
   video. The rate comes from the cameras' own timestamps.
2. No `WARNINGS.txt` anywhere under the acquisition folder.
3. The block IDs ([Check the block IDs](#check-the-block-ids)).
4. One frame of each video. Open an mp4 and check that the animal is neither
   black nor blown out. The preview cannot show this, because it runs free at
   30 fps and is downsampled. Check an exposure change against a recording.
   For more light, add infrared illumination first, then exposure, then gain
   ([CONFIGURATION.md](CONFIGURATION.md#pfs_path)).
5. After a stimulated recording, the stimulation files
   ([section 7](#after-a-stimulated-recording)).

A clean session ends with no dialog.

### Check the block IDs

`blockids.npy` holds each frame's trigger number. Check the recording with:

```
uv run python 2_align.py <recording folder>
```

It reads the frame rate from the acquisition's `session_metadata.json`, and
prints the cameras, the triggers every camera recorded and a table:

```
cam     recorded  dropped   %drop    first     last
cam1        6022        0   0.00%        1     6022
cam2        6022        0   0.00%        1     6022
```

On a clean kick-out recording every `dropped` is 0. The same non-zero
`dropped` on every camera means kick-out removed those triggers from all of
them, and the videos are still aligned. A different count on one camera means
the videos are not aligned yet. Run the command again with `--replace` to
re-encode them to the common triggers.

`--replace` refuses while a camera ended early, started late or stopped for
more than about a second. Aligning to that camera would cut the others down to
it. `--exclude camN` aligns the other cameras and leaves that one as recorded,
and `--truncate-to-shortest` cuts every camera anyway. A camera with a
`RETIRED.json` is left out unless you pass `--include-retired`.

The command also runs the block-ID rate check on every camera. A camera that
ignores triggers keeps equal frame counts and gapless block IDs while its
frames drift in time, and only this check finds it. The command always writes
the `aligned/` index, even for a clean recording. Only `--replace` changes the
videos. Its exit status is 0 when all is well, 2 for a warning such as a rate
warning, and 1 for an error or a refused replace.

### Quitting in the middle

Closing the window while work runs asks first, with No as the default:

| State | What Yes does |
|---|---|
| `RECORDING` or `CALIBRATING` | Deletes the unfinished capture |
| `ENCODING` | Keeps the capture. Run `0_encode.py`, then `2_align.py` on the folder, with `--replace` for a recording made without kick-out |
| `ALIGNING` | Keeps the videos, partly aligned. Run `2_align.py --replace`, then `3_stim_trace.py` |
| A solve, a profile switch or a camera operation | Cancels it, and deletes no data |

The dialog names the commands for your folder. Panopticon does not close
during a firmware flash.

Quitting sends the board a stop whenever Panopticon holds its serial port. If
the board does not confirm it, the `Trigger board did not confirm the stop`
dialog of [Stopping](#stopping) appears before the window closes. Power-cycle
the board and switch the laser off.

---

## 10. What the session leaves on disk

### Paths and names

```
<output>/<date>/<mouse1>_<mouse2>/<calibration|recording>/<camN>/
```

The date and the mouse IDs come from the sidebar, and blank mice become `m1`
and `m2`. `calibration/` and `recording/` sit side by side in one session, so
one calibration serves the recording beside it. Videos are named
`<date>-<mouse1>_<mouse2>-<camN>-<calibration|recording>.mp4`, for example
`20260904-m1_m2-cam1-recording.mp4`. The solve finds its videos by
`calibration` in the name.

### Every file a session can contain

In each camera folder:

| File | What it is |
|---|---|
| `<date>-<session>-<camN>-<type>.mp4` | The video: H.264, one keyframe per second, index at the front so a browser can seek |
| `blockids.npy` | One trigger number (block ID) per frame. A dropped frame leaves a gap |
| `frametimes.npy` | 2 × F: frame numbers 1 to F, and each frame's device time in seconds from the first |
| `WARNINGS.txt` | Written when this camera's block IDs had to be repaired to match its video, or its encode lost frames |
| `RETIRED.json` | Written when the camera was retired: why, and its last block ID |
| `encode_error.log`, `tail_error.log` | Written when an encode failed: ffmpeg's output |
| `stream.h264`, `raw.bin` | Capture files, deleted once the mp4 is checked. Left behind, they mean the encode did not finish |
| `raw_tail.bin`, `encoded.json` | Frames written raw after a camera's encoder failed, and where they start. The encode merges them |
| `blockids.full.npy`, `frametimes.full.npy` | The full arrays, kept when a failed merge cut the metadata to the mp4 |
| `frametimes_synthesized.json`, `frametimes.orig.npy` | An alignment made the frame times from block IDs. The rate check then skips this camera |
| `aligned_tmp.mp4` | An alignment in progress |

In the acquisition folder, `calibration/` or `recording/`:

| File | What it is |
|---|---|
| `session_metadata.json` | This acquisition's metadata ([below](#session_metadatajson)) |
| `session.log` | The log from arming the cameras to the end of the encode |
| `WARNINGS.txt` | Written when something went wrong |
| `calibration.toml` | The solve's result, copied into `recording/` by Solve |
| `calibration_report.json`, `reprojection_error_histogram.png` | The solve's figures and plot. Calibration only |
| `codet_frames.json` | The triggers at which two or more cameras saw the board. Calibration only |
| `stim_paradigm.json`, `stim_paradigm.ino`, `stim_trace.csv` | The paradigm, its sketch and the modelled stimulus per trigger ([section 7](#after-a-stimulated-recording)) |
| `aligned/alignment.npz`, `aligned/alignment.json` | The alignment index, written whenever an alignment runs ([below](#aligned)) |

The session folder holds a copy of `session_metadata.json` from the session's
first acquisition, which is never overwritten, and
`snapshots/<date>_<HHMMSS>/camN.png`. The launch log stays in `logs\` in the
repository folder.

### session_metadata.json

Each acquisition writes its own copy when it stops. It holds:

- the sidebar fields and the profile's name;
- the cameras' names, serials and models, in cam1 to camN order;
- the frame rates, the resolution and the time;
- the host, the OS, Python, the GPU with its driver and memory, and the NVENC
  sessions available;
- each camera's temperatures (`camera_thermals`, where `temp_max_c` is the
  one to read);
- the encoder used and the one the profile asked for;
- settings the videos cannot show, among them `trigger_source`, `log_level`,
  `nvenc_upload`, `thermal_warn_margin_c` and `capture_processes`;
- `kickout`: the triggers decided, kept, kicked out and force-dropped, and the
  [effective frame rate](GLOSSARY.md#effective-frame-rate);
- `log`: the log's level and file, and the lines this acquisition lost;
- `camera_stream_stats` and `nvenc_upload_used`.

The GPU driver and its session count are there because the driver's NVENC
session cap changes between driver versions. A cap below the camera count
changes how a session records, with nothing else on the machine changing.

### stim_trace.csv

One row per trigger:

- `frame`: the frame's index in the reference camera's video, counted from 0;
- `blockid`, `t_s` and `any_active`;
- per chain, `chain<i>_step`, `chain<i>_active`, `chain<i>_freq_hz` and
  `chain<i>_pw_ms`;
- a modelled `pin<N>_ttl` per pin;
- a `frame_<cam>` column per camera, blank where that camera has no frame for
  the trigger.

When the videos are aligned, every `frame_<cam>` equals `frame`.
`2_align.py --replace` rewrites the file, and `3_stim_trace.py` writes it again
by hand.

### aligned/

`alignment.npz` holds `common_block_ids`, `frame_index` (cameras × common
triggers), `camera_names` and `video_is_common`. `alignment.json` holds a
readable summary:

- `trigger_span`, `common_frames`, whether alignment was `needed`, and
  whether the videos were `replaced`;
- per camera, `recorded`, `dropped`, `video_is_common` and
  `frametimes_synthesized`;
- any `excluded` or `short_cams`, a `refused` replace, `failures` and the rate
  warnings.

An `aligned/` folder beside a clean recording usually means someone ran
`2_align.py` to check it. Its `alignment.json` then reads `replaced: false`
with every `dropped` at 0.

### Two example sessions

A calibration-only session on N cameras:

```
data/
└── 20260904/
    └── m1_m2/
        ├── session_metadata.json
        ├── snapshots/
        │   └── 20260904_101500/
        │       └── cam1.png … camN.png
        └── calibration/
            ├── session_metadata.json
            ├── session.log
            ├── codet_frames.json
            ├── calibration.toml
            ├── calibration_report.json
            ├── reprojection_error_histogram.png
            └── cam1/ … camN/
                ├── 20260904-m1_m2-camK-calibration.mp4
                ├── blockids.npy
                └── frametimes.npy
```

Solve also creates `recording/`, holding only the copied `calibration.toml`.

A stimulated recording in the same session:

```
data/20260904/m1_m2/recording/
├── calibration.toml          copied by Solve
├── session_metadata.json
├── session.log
├── stim_paradigm.json
├── stim_paradigm.ino
├── stim_trace.csv
└── cam1/ … camN/
    ├── 20260904-m1_m2-camK-recording.mp4
    ├── blockids.npy
    └── frametimes.npy
```

That is a clean take: no `WARNINGS.txt`, and no `stream.h264` or `raw.bin`
left behind. A take whose cameras kept different frames without kick-out
also has `aligned/`, and so does one checked later with `2_align.py`.

### The command-line tools

Each runs in the project environment, from the repository folder, and takes
`--help`:

| Command | What it does | Exit status |
|---|---|---|
| `uv run python 0_encode.py <folder>` | Turns an acquisition's capture files into mp4s when the window's encode did not finish | 0 done; 1 a camera failed; 2 a camera holds fewer frames than captured |
| `uv run python 1_calibrate.py <session> --board-config <file>` | The solve ([Solving by hand](#solving-by-hand)) | 0 solved, also when cameras were dropped; 1 failed |
| `uv run python 2_align.py <folder> [--replace]` | Checks the block IDs and their rate. Aligns the videos with `--replace` | 0 fine; 2 a warning; 1 an error or a refused replace |
| `uv run python 3_stim_trace.py <folder>` | Writes `stim_trace.csv` again | 0 fine; 2 the cameras disagree; 1 skipped |

For FLIR cameras, `probe_flir.py` tests each camera before a first recording
([FLIR.md](FLIR.md)).

---

For every message Panopticon shows, with its cause and fix, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).
