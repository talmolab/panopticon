# The interface

Panopticon lives in two windows: the main acquisition window, and the
stimulation editor that opens from it. Between them you preview the cameras and
record trigger-synchronised video. You also capture the calibration footage that
fixes the rig's geometry in 3D, and drive the board that triggers the cameras
and delivers optogenetic stimulation.

For a first session start with [WORKFLOW.md](WORKFLOW.md), which walks through
one from launch to finished files.

Callout positions come from Qt's widget geometry, so the numbers in each figure
match the numbers in the text.

---

## The main window

![The Panopticon main window with numbered callouts](images/ui_annotated.png)

A whole session happens here: pick the rig profile, fill in the metadata, check
aim and focus, calibrate, record. There is no settings window; anything not on
this one lives in a configuration file.

Camera grid on the left, fixed 260 px sidebar on the right. The sidebar runs
roughly in the order you use it: Metadata, Acquisition, Display. A status label
sits at its foot. On launch the window takes about 80% of the screen height,
and the grid's aspect ratio follows the camera count, so rigs with different
camera counts open at different shapes.

### Preview

Aim, focus, lighting, and whether a camera is still delivering frames. The
preview gives up resolution and frame rate so the recording never has to.

**1 — Camera preview grid.**
Live video from every open camera, three panes per row, rebuilt from scratch
whenever cameras are opened so no stale pane survives a camera that has gone
away.

Frames are downsampled 3x per axis in the grab thread (the per-camera thread
that pulls frames out of the driver), so 1920x1200 becomes 640x400, and panes
repaint on a timer rather than per arriving frame. The timer is 33 ms up to six
cameras and then stretches with the count, 33 ms x n/6 and never slower than
100 ms, so the total repaint work per second stays roughly flat however many
cameras are open: 50 ms at nine. Repainting happens on the Qt main thread and
its cost is linear in camera count, while the grab threads' deadline is fixed at
one trigger period, so the preview is what gives way. How many frames reach
the preview depends on the rig's state:

| State | Camera mode | Frames sent to the preview |
|---|---|---|
| Idle | free-run at 30 fps | every frame |
| Calibrating | triggered at `calibration_frame_rate` (30) | every frame |
| Recording | triggered at `frame_rate` (100) | every 10th frame |

At 100 fps the panes update about 10 times a second, keeping display work off
the loop that drains the driver's buffer pool. Nothing downstream reads the
preview.

Switching profiles ("Switching cameras…") and stopping an acquisition
("Finishing…") halt the refresh timer, so panes freeze on their last frame for a
second or two; a firmware flash freezes them for around 30 s. The state label
(16) tells you which.

**2 — One camera pane.**
One pane per camera, labelled `cam1`…`camN` in its top-left corner. That label is
the identity every later stage uses, from the video filenames to the
calibration.

Names come from **serial-number order**: the backend sorts the enumerated
devices by serial number and the pane index becomes the name. Physical position,
switch port and boot order have no effect. The calibration extrinsics attach to
that ordering, so a camera that failed to enumerate would rename every camera
after it. Setting `n_cameras` in the profile makes the software refuse a partial
set rather than silently renumber; the error lists the serials it did find.

**Double-click a pane to zoom that camera to the whole grid. Double-click again
to go back.** While zoomed the other panes are hidden, so you leave by the pane
you came in on.

**3 — Live frame rate.**
That camera's delivered frame rate, bottom-left of the pane. It is measured in
the camera's own grab thread over the last ten frames and pushed to the label on
every tenth repaint, so it refreshes about three times a second at six cameras
and twice a second at nine — fast enough to show a change within a second or so.

It should sit at whatever the cameras are doing: ~30 fps idle, the calibration
rate while calibrating, the trigger rate while recording. One pane low while the
others are right is usually the first visible sign of trouble with that camera's
link or triggers. Stop and investigate.

### Metadata

Which hardware Panopticon talks to, and what the session is called on disk. The
fields lock once an acquisition begins, because by then they name the folder
being written into.

**4 — Rig profile.**
Selects a *rig profile*: one YAML file in `profiles/` describing one physical
rig. One click settles the whole hardware configuration: resolution, the
recording and calibration frame rates, the camera settings file (`.pfs`, a saved
dump of the cameras' registers, applied to every camera as it opens), the
calibration board config, the trigger board's serial port and trigger pins, the
encode mode, the kick-out depth (frames the frame-synchronisation coordinator
holds while waiting for a lagging camera), and the calibration-only exposure and
gain. Changing it closes every camera and reopens them against the new profile:
a second or two, off the UI thread, so the window stays responsive.

Profile files are shared between rigs through git, so the choice is remembered
per machine rather than in the repo. A launch opens the profile last used here,
or the one `gui.py --profile <name>` names. On a machine with neither, the
window opens no camera and no serial port, and programs no board, until you
choose a profile in this dropdown.

Selecting a profile also resets the output directory to that profile's
`output_dir`. Profile switching is ignored unless the app is idle.

**5 — Output directory.**
The root folder every session is written under. The button opens a folder
picker; a full path rarely fits a 260 px sidebar, so it shows shortened with the
whole path as a tooltip. Sessions nest by date and subject, so one root holds a
whole project:

```
<output>/<date>/<mouse_1>_<mouse_2>/<calibration|recording>/
```

**6 — Session metadata.**
Eight text fields: Date, Mouse 1, Mouse 2, Assay, Experimenter, Cohort, Cage and
Notes. Date arrives filled in with today, Assay with `open_field`, Experimenter
with `IT`; the rest start empty.

Date, Mouse 1 and Mouse 2 are structural: they build the folder path above.
Fill them in before recording. You can rename a folder later, but the metadata
inside it will disagree. Blank subject fields give you `m1` and `m2` in the
path.

All eight go to `session_metadata.json` in the session folder when the
acquisition stops, alongside everything about the machine that could later
explain the recording: camera names, resolution, frame rates, time of day, host
name, OS and Python version, the GPU's name, **driver version** and total
memory, and the number of concurrent NVENC sessions the preflight found
available. (NVENC is the GPU's dedicated hardware video encoder.)

The driver version matters because the NVENC session cap has moved across driver
generations and each camera needs one session for compressed video: a driver
update that drops the cap below the camera count changes how a session records
with nothing else on the machine changing.

All eight go read-only during an acquisition, and so do the output-directory
button and the profile dropdown.

### Acquisition

Calibrate and Record are toggles; one is disabled while the other is on. Solve,
Snapshot and Stimulation are one-shot actions. The coverage HUD beneath them
is not a control: it appears on its own while a calibration runs. A typical
session goes Calibrate, Solve, Record.

**7 — Calibrate.**
Starts and stops a *calibration acquisition*: a short recording of a printed
board carried around the arena, from which the solve recovers each camera's lens
parameters (its intrinsics) and where the cameras sit relative to one another.

Cameras run in trigger mode at `calibration_frame_rate` (30 fps on `3dpose`),
with `calibration_exposure_us` and `calibration_gain_db` applied for that run
only: a slowly-moved board affords far more light than a fast recording. The
`.pfs` exposure and gain return as soon as a recording starts, so a long
calibration exposure cannot leak into a 100 fps session and quietly halve its
frame rate.

Calibration is the one acquisition done with a person inside the arena, so it
always gets the **stimulation-free firmware**: the recording-only sketch, which
triggers cameras and drives no stimulation pin. Panopticon flashes it if the
board is not already carrying it, which is why the state label may sit at
`Flashing recording-only firmware…` for about 30 s after you press Calibrate.
There is no way to run a calibration with a paradigm live on the board.

The board is a ChArUco board: a chessboard with a unique ArUco marker in each
white square, so a detector can name individual corners from a partial view.
Move it slowly so every camera sees it, every *pair* of cameras sees it at the
same moment, and it visits all four quadrants of every view. Watch the coverage
display (12) rather than guessing when to stop. Calibrate on disables Record.

**8 — Record.**
Starts and stops the experiment recording, cameras triggered at the profile's
`frame_rate`: 100 fps on `3dpose`.

A recording that fails quietly costs an experiment, so a preflight runs first
and refuses outright in four cases:

- **No cameras are open.** Recording would still run the trigger protocol, and
  with it any stimulation paradigm baked into the trigger board, while saving
  nothing.
- **Not enough RAM** for the driver's buffer pool plus the ring of NV12 frame
  buffers (NV12 is the pixel layout the GPU encoder consumes) at this camera
  count. The message gives the arithmetic and the machine's actual RAM.
- **Too few NVENC sessions.** The driver caps concurrent encode sessions and
  each camera needs one. Cameras beyond the cap would silently fall back to raw
  capture, writing every full frame to disk uncompressed, so the refusal states
  the size that implies for a 10-minute recording.
- **A stimulation workflow that must not be recorded**: a loop with no starting
  block, a stim block sitting on a camera trigger pin or on the board's UART
  pins, or a single pin driven by two chains at once.

A tight disk is a warning, not a refusal, and every warning becomes a "Start
anyway?" prompt. Existing data in the target folder is never overwritten
silently: a dialog headed *Overwrite the existing data?* warns that the folder
will be permanently deleted, and only on **Yes** is it removed and the
acquisition recorded fresh under the same name. Cancel leaves everything where
it is and the acquisition does not start. The delete happens only once the
serial port has opened, so a start refused because the port is busy leaves the
old data untouched. The check
counts `blockids.npy`, `frametimes.npy` and `alignment.npz` as well as videos,
so a folder whose mp4s were moved away for labelling is still recognised as
holding data; zero-length files are not counted, so a start refused after
opening its streams leaves nothing to move.

Past the preflight, the next thing may be a wait. A recording runs under
whichever firmware matches the session: the recording-only sketch if no
stimulation paradigm has been applied since launch, the paradigm's own sketch if
one has. If the board carries the wrong one, Panopticon flashes the right one
first: the state label reads `Flashing recording-only firmware…` or `Flashing
recording + stimulation firmware…` for about 30 s with every control greyed out.
Wait for it; it is not a hang.

The flash is skipped when the board already holds the right sketch, so the
usual order of a session (calibrate, then build and Apply the paradigm, then
record) costs nothing. It shows up when the two kinds of acquisition interleave:
a calibration after an Apply swaps the paradigm out, and the recording after
that calibration swaps it back in.

A failed flash refuses the recording, because it leaves the board's contents
unknown and an unknown board may be holding a stimulation pin high. The dialog
says to key off the laser and check the board before retrying. Do it in that
order.

The trigger board then has to acknowledge the start command. Without that
acknowledgement the cameras are rolled back out of trigger mode, the board is
stood down, and the recording is refused rather than producing a full-length
session containing no frames, a failure you would discover long after the animal
went back in its cage.

A recording ends when you turn Record off, or by itself when a paradigm's
"Ending" block finishes. Both do the same thing.

**9 — Solve.**
Turns the calibration footage into geometry. It runs `1_calibrate.py` through
`uv run` over the recorded calibration videos, writes each camera's intrinsics
and the poses of all cameras relative to one another into `calibration.toml` in
the calibration folder, then copies that file into the recording folder, so
every session carries its own copy rather than pointing at one that might later
be replaced.

Nothing on screen advertises the following:

- It takes several minutes with the button disabled throughout; the status bar
  and the state label are the only sign it is working. The label reads
  `CALIBRATING...` in purple during a solve, though no camera is capturing.
- It picks the folder from the metadata fields **as they read now**, not from
  the calibration most recently recorded. Change the date or a subject ID after
  capturing and Solve goes looking somewhere else.
- It refuses with a dialog if the calibration folder does not exist, contains no
  mp4 files, or the profile's board config file is missing.
- If the calibration run saved `codet_frames.json` (the frame indices where the
  coverage HUD saw the board in two or more cameras at once) the solve
  decodes only those frames, which is much faster. Without it, a full scan at
  the script's default of every third frame (`--skip 3`).
- Unavailable while an acquisition or an encode is in progress; a second click
  during a solve is ignored, not queued.

**10 — Snapshot.**
One still image from every camera at once: for documenting the day's arena
setup, and for judging focus and framing honestly, since the 3x-downsampled
preview hides exactly the softness you are looking for. Each grab thread stashes
its next **full-resolution** frame, and one PNG per camera is written to:

```
<output>/<date>/<mouse_1>_<mouse_2>/snapshots/<date>_<HHMMSS>/cam<N>.png
```

The status bar reports how many cameras were saved and where they went.
Snapshots come from the raw frame, so the brightness and contrast sliders do not
affect them.

**11 — Stimulation.**
Opens the optogenetic stimulation editor, its own section below. It is not
modal, so leave it open beside the main window and work in both.

A stimulation sequence is **compiled into the Arduino's firmware** rather than
streamed over the wire, so editing the canvas changes nothing until **Apply to
Arduino** is pressed, which takes about 30 s. Firmware survives closing the app,
so Panopticon reflashes a recording-only sketch at every launch unless the board
is already known to be carrying one. Stimulation is opt-in per session; a
paradigm from last week cannot fire because someone pressed Record. To reuse
one, **Load** it and Apply it again.

Within a session you Apply once per paradigm. After that Panopticon holds two
sketches, the recording-only one and yours, and puts whichever the next
acquisition needs onto the board itself, so **Apply is needed only when the
stimulation paradigm itself changes**. That automatic swap is what the `Flashing
… firmware…` states under Calibrate (7), Record (8) and the state label (16) are
doing.

**12 — Calibration coverage HUD.**
The live ChArUco coverage graph: how much of the calibration you have captured,
while you are still holding the board. Hidden when idle, it appears in this slot
when a calibration starts. Without OpenCV, or without the profile's board config
file, it stays hidden for the whole run and the calibration records video with
no live feedback.

Detection runs on its own worker thread at roughly 30 Hz on **full-resolution**
frames rather than the downsampled preview copies: obliquely-mounted cameras see
the board at a steep angle and need every pixel to resolve its corners at all.
Those are the frames that go into the recorded video, so the HUD judges the
footage the solve will read.

### Display

None of it touches the data.

**13 — Brightness.**  **14 — Contrast.**
Both sliders range from -100 to +100 and affect the **preview only**. Contrast
is applied first, scaling each pixel about mid-grey by `(100 + contrast) / 100`;
brightness is added on top; the result is clipped into the 0–255 range.

They change nothing about the recorded video, the snapshots, or board detection.
The coverage HUD reads frames straight from the grab threads and the solve reads
the recorded file, so both bypass the sliders: a board that looks brighter here
is no easier to detect.

If the board really is too dark, the fix is physical or in the rig profile.
Better infrared illumination first (more photons, better signal-to-noise), then
`calibration_exposure_us`, and only then `calibration_gain_db`, which amplifies
the noise along with the signal.

Calibration has room for a long exposure. In trigger mode the frame-rate timer
starts only once exposure ends, so the longest exposure that still lets a camera
answer every trigger (its *exposure ceiling*) is roughly 27 ms at the 30 fps
calibration rate, against about 3.94 ms at 100 fps: seven times the light
budget, free, which is why a `calibration_exposure_us` of 15000 (15 ms) is safe.
Panopticon works the ceiling out itself rather than trusting the profile and
clamps exposure to 90% of it (about 24.5 ms at 30 fps, about 3.5 ms at 100 fps),
leaving a margin before a camera starts ignoring triggers. Clamps are announced
on the console, because exceeding the ceiling otherwise fails silently.

Motion blur limits calibration exposure long before the ceiling does: at 15 ms a
briskly waved board smears and its ChArUco corners stop resolving at all. Move
it slowly and pause at each pose.

**15 — Progress.**
Hidden unless a post-recording stage is running, about the only time Panopticon
asks you to wait. It reads `Encoding N/M` while the videos are finalised and
`Aligning N/M` if a post-hoc alignment pass is needed, where M is the camera
count.

The wait depends on the profile's encode mode. Under real-time GPU encode, the
default, the frames were compressed during capture, so finalising is a
stream-copy remux into mp4 and takes seconds. The raw fallback still has a full
encode pass to do.

**16 — State.**
At the bottom-right of the sidebar, colour-coded, naming what the application is
doing right now:

| Text | Meaning |
|---|---|
| `IDLE` (grey) | nothing running; cameras in free-run preview |
| `CALIBRATING` (blue) | calibration acquisition in progress |
| `RECORDING` (red) | recording in progress |
| `ENCODING` (amber) | finalising video after a stop |
| `ALIGNING` (amber) | post-hoc trigger alignment re-encode |
| `CALIBRATING...` (purple) | a Solve is running — no capture |
| `Switching cameras…` (amber) | a profile change is closing every camera and reopening it |
| `Finishing…` (amber) | an acquisition is being stopped and closed out |
| `Clearing stim firmware…` (amber) | the launch-time reflash back to the recording-only sketch |
| `Flashing recording-only firmware…` (amber) | the board is being given the stimulation-free sketch |
| `Flashing recording + stimulation firmware…` (amber) | the board is being given the applied paradigm's sketch |

The first six are states the application settles into. The last five are
blocking operations: while one is on screen, every control that could start
something is disabled (all of those listed at the end of this page, which is
everything but the Stimulation button and the two display sliders), the cursor
becomes a wait cursor, and the preview freezes on its last frame.

Learn the two `Flashing …` states on sight. They are the long ones and they
arrive at the worst moment, straight after you press Calibrate or Record, quite
possibly with an animal already in the arena. The board is being given the
firmware that acquisition needs: the stimulation-free sketch for a calibration,
whichever sketch matches the session for a recording. So an Apply, then a
calibration, then a recording shows `Flashing recording-only firmware…` before
the calibration and `Flashing recording + stimulation firmware…` before the
recording, about 30 s each. Wait; do not force-quit. The acquisition starts by
itself when the flash finishes, or is refused with a dialog if it fails.

**17 — Status bar / capture health.**
Along the foot of the window, carrying whatever the application has to say:
where a snapshot was written, how a solve is progressing, the encode summary
after a recording, the results of an alignment pass. Empty until the session's
first event.

During a recording it reports **how far behind real time the worst camera is**,
refreshed about three times a second:

- under 0.25 s: `Capture healthy — keeping up with the trigger (max lag N ms)`
- under 1 s: `CAPTURE FALLING BEHIND: camN is X s behind real time and growing.
  Close other applications.`
- otherwise: `CAPTURE X s BEHIND REAL TIME (camN). Frames will be lost when the
  buffer pool fills. Stop and investigate.`

The number appears even when everything is healthy, so you can check the
reassurance rather than trust it. It is computed per camera from that camera's
own hardware timestamp against the moment its frame arrived, referenced to the
first frame of the run, so it never depends on the host clock. It comes from the
real-time kick-out path: with `realtime_kick` disabled it sits at zero and means
nothing.

The failure it catches is otherwise invisible. A grab loop running a fraction of
a millisecond over budget loses nothing at first, because the driver's buffer
pool absorbs the deficit: no error and no dropped frame for as long as ten
minutes, while every frame retrieved gets staler. A large lag also means the
preview is showing the past, which matters when aiming cameras.

After a recording stops the same bar reports the encode summary: frame counts,
average frame rate, and any camera that produced no usable video. Problems also
go to `WARNINGS.txt` in the recording folder, because a dialog is dismissed and
forgotten. A clean session produces neither.

---

## The calibration coverage HUD

Watch this while you stand in the arena with the board, deciding whether you
have moved it around enough.

One numbered node per camera on a ring, with an edge between every pair. A node
reports what one camera has seen; an edge reports what a *pair* has seen at the
same time, which is what stereo calibration depends on. When every condition is
met the graph freezes and the caption reads `READY`. That is your cue to stop.

The four figures below are rendered illustrations of particular states, not
captures of one continuous session, which is why the elapsed timer reads `0:00`
in all of them. They were drawn on a six-camera rig whose profile set
`calibration_min_per_cam_shared: 250` and `calibration_min_edge: 80`, and before
the caption carried a `groups` segment at all, so read the shape of the graph
from them and not their numerals. Your own rig prints
`paired <worst>/<calibration_min_per_cam_shared>  grid <worst>/3  groups <n>/1`
against your own profile's thresholds.

| | |
|---|---|
| ![Coverage graph, nothing detected](images/calib_stage_1_start.png) | ![Coverage graph, partial coverage](images/calib_stage_2_partial.png) |
| **Stage 1.** Nothing detected yet. Every edge is dark and thin, every node is dull, and every count in the caption is zero. | **Stage 2.** Cameras 1 and 3 are lit — they can see the board right now. Edges have begun to thicken, and `paired` and `grid` are part way to their targets. |
| ![Coverage graph, nearly ready](images/calib_stage_3_nearly.png) | ![Coverage graph, READY](images/calib_stage_4_ready.png) |
| **Stage 3.** Nearly there. Camera 5 is lit, most edges are bright and thick, and the 1-4 edge is still thin — that pair has barely seen the board together. `grid` has reached 3/3 and `paired` is just short of its target. | **Stage 4.** Every condition met. The whole graph freezes solid white and the caption reads `READY — m:ss`, with the elapsed time stopped at the moment it got there. |

Everything on the graph is counted in *detection ticks*. One tick is a single
pass of the board detector across the current frame from every camera, so a tick
is a moment in time rather than a recorded frame. The pass is sequential over
the cameras and costs more the more texture a scene has, so the rate is best
effort: typically 10-20 a second at nine cameras, and fewer when the arena is
cluttered.

- **A node lights up** (cyan, brighter rim) when that camera sees at least 4
  board markers in the current tick. The glow decays over about 0.4 s, so it
  reads as a pulse rather than a latch.
- **An edge thickens and whitens** as that pair accumulates ticks where both saw
  at least 5 markers, saturating at 200 shared ticks. A thin edge is a pair that
  has not yet seen the board together; walk the board through the space those
  two cameras share.
- **The 2x2 badge** at the bottom-right of each node is that camera's spatial
  coverage. Each detection's marker centroid is binned into one of four
  quadrants of that camera's field of view, and the cell turns green the first
  time it is hit.
- **The caption** reads
  `<elapsed>  paired <worst>/<target>  grid <worst>/<cells>  groups <count>/1`.
  The two worst-camera numbers are not averages, so they move only when the
  camera furthest behind improves; the targets come from the profile fields
  `calibration_min_per_cam_shared` and `calibration_min_grid_cells`, 120 and 3
  on the reference rig. `groups` is the third condition, and it is the one the
  other two numbers cannot show: while it reads more than `1`, an orange line
  above the caption lists the groups, for example `{1,2,3} {4,5}`. Show the
  board to one camera from each group at the same time until it reads `1/1`.

**READY requires three things at once:**

1. Every camera has at least `calibration_min_per_cam_shared` co-detection
   ticks — **120** on the reference rig — meaning ticks where it and at least
   one other camera both saw the board.
2. Every camera has hit at least `calibration_min_grid_cells` of its 4
   field-of-view quadrants, **3** on the reference rig.
3. The graph is **connected** through edges of at least `calibration_min_edge`
   shared ticks, **20** on the reference rig, so every camera is linked to every
   other directly or through a chain.

All three targets are profile fields, so a rig sets how much waving it wants in
its own YAML; the caption always shows the target it is testing against.

The quadrant condition exists because waving the board in one spot in front of
all the cameras satisfies the first and third. That gives poorly constrained
intrinsics: the solve looks fine and behaves badly towards the frame edges.

The counts are detection ticks, not recorded frames, so treat them as relative
coverage signals rather than totals to go looking for in the finished videos. At
READY detection stops and the elapsed timer freezes, so you can see how long the
run took.

Turning Calibrate off hides the graph and writes `codet_frames.json` beside the
videos: the frame indices at which two or more cameras saw the board at once.
Solve picks that up later and decodes only those frames instead of scanning
every frame of every video.

---

## The stimulation editor

![The stimulation editor with numbered callouts](images/stim_annotated.png)

Where you build an optogenetic stimulation paradigm, before a recording rather
than during one: uploading is refused while an acquisition runs. A paradigm is a
graph of blocks. Each block drives one output pin with one square wave for one
fixed duration, and arrows chain blocks into a sequence that runs from the
moment the recording starts.

The graph is **compiled into the trigger board's firmware**, not streamed while
it runs, so nothing you draw reaches the hardware until **Apply to Arduino**
uploads it, about 30 s. The same Arduino Mega triggers the cameras and delivers
the stimulation, so the editor is strict about which pins a block may use.

**1 — Node canvas.**
The graph itself. Drag blocks to arrange them; each carries four connector ports
(top, bottom, left and right) so arrows route whichever way reads most clearly.

- **Drag from a port** to another block's port to create an arrow. A port within
  snapping distance highlights and the arrow lands on it. A block takes any
  number of incoming arrows but at most one **outgoing** one, so a chain is a
  sequence. Dragging from a port on a block that already has an
  outgoing arrow moves the block instead of starting a new arrow.
- **Click** a block to select it and load its values into the fields below.
  **Drag on empty space** for a rubber-band selection, shift-drag to add to it.
- **Delete** removes the selected blocks and arrows. **Ctrl+C / Ctrl+V** copies
  and pastes blocks at the cursor. **Middle-drag** pans, **scroll** zooms.
  **Home** fits the whole graph in view. **Escape** clears the selection and
  nothing else: it deliberately does not close the editor, because a hidden
  editor with a bench test running would put its only Stop button out of sight.
  **There is no undo.** Delete is final, so Save the graph before a large edit.
- Each block shows its pin in the header, then its frequency, pulse width,
  duration, and the resulting mode: `10% duty`, `constant ON`, or `pin LOW`.
  Blocks are tinted by pin number.
- A **filled red dot** in a block's top-right corner means this block starts its
  chain; a white ring around that dot means it was pinned by hand. A **hollow
  amber dashed ring** means the block sits in a loop with no start, so it would
  never run. A **black dot** to the left of the start dot marks the "Ending"
  block. Blocks that are not chain starts are drawn faded.

**2 — Waveform preview.**
A small plot of one second of the wave the current Freq and PW fields would
produce; its own section below explains how to read it.

**3 — Pin.**
Which output pin this block drives: for a new block, or for the selected one.
The field has no default on purpose. An empty Pin is refused rather than quietly
treated as pin 0, which on a Mega is UART RX0 and would garble the link between
host and board.

Two classes of pin are rejected outright, at Apply, Test and Record alike: the
camera trigger pins named in the rig profile, and pins 0 and 1, the board's own
serial lines. A stimulation waveform on a trigger line injects extra rising
edges into one camera, so that camera counts more frames than the others and its
*trigger ordinals* stop lining up with everyone else's. (A trigger ordinal is
the running count of trigger pulses that every frame carries and `blockids.npy`
records.) Every alignment step in the pipeline assumes a given ordinal means the
same instant on every camera.

**4 — Freq (Hz).**
The pulse frequency of this block's square wave. A frequency of `0` is
legitimate rather than an error: it holds the pin LOW for the block's whole
duration, which is how a gap between stimulation periods is written.

**5 — PW (ms).**
Pulse width in milliseconds: how long the pin stays HIGH within each cycle.
Frequency and pulse width are independent fields, so check the duty cycle they
imply in the preview before committing them.

**6 — Dur (s).**
How long this block runs, in seconds, before the chain moves on to the next
block.

Pressing Enter in any of these four fields applies the values to the selected
block. With nothing selected, Enter creates a block, the same as **Create
Block**.

**7 — Starting.**
Pins the selected block as the start of its group. Disabled until exactly one
block is selected.

You rarely need it: any block with no incoming arrow already starts a chain. A
pure loop has no such block, so it must be pinned by hand or it compiles to
nothing. There is one start per weakly-connected group. Pinning one clears the
flag on the others, and if an arrow later merges two pinned groups one flag is
dropped, so the canvas cannot claim something the compiler would not do.

**8 — Ending.**
Marks the one block whose completion **stops the recording**. At most one per
canvas, and like Starting it is disabled until exactly one block is selected.

It stops the *recording*, not the chain. A looping chain runs until the
recording ends, so bound a loop either with an Ending block or with a parallel
timer chain that carries one. The countdown is armed on the host when Record
starts, from the canvas's own end time, and the board is never asked to report
back; when the time is up, the host turns Record off exactly as a hand would.
Reading the end time off the canvas is safe because Record refuses to start when
the canvas holds a paradigm that has never been Applied, or one edited since the
last Apply — in both cases the board would run something other than what the
canvas describes.

**9 — Status line.**
What the current graph would do, and what is wrong with it. In normal use, the
stop time (`Recording will stop 15 s after start.`), load and save
confirmations, upload progress and test countdowns. In red, the blocking
problems: blocks forming a loop with no start, a pin driven by two chains at
once, an Ending block that no chain reaches, or unparseable field values. (The
text in the figure above is placeholder copy for the illustration.)

**10 — Test.**
Runs the paradigm on the bench. The start command is sent with **zero camera
pins**, so the stimulation outputs fire while no camera is triggered and nothing
is recorded. Confirm a paradigm here before an animal is involved. The button
becomes **Stop Test** while a test runs and the status line counts down; a
looping paradigm has no end time, so it runs until you stop it.

Test runs whatever is on the board, not what is on the canvas. If the canvas has
changed since the last upload it offers to upload first, rather than silently
testing the previous paradigm. It borrows the main window's serial connection
instead of opening its own, which keeps a test from resetting the board. If the
board does not confirm the stop, the status line says so in red and a dialog
appears: during a bench test there is no recording whose end would stop the
output, so that single write is the only thing that does.

**11 — Apply to Arduino.**
Compiles the graph into an `.ino` sketch and uploads it to the board, in about
30 s. Apply and Test are both disabled during the upload, and the serial port is
handed to the upload tool and reclaimed afterwards.

On success the status line reads `Upload successful — press Record to run
paradigm.` and the sketch's hash is recorded, so the next launch can tell
whether the board still carries a paradigm.

Apply is refused while an acquisition is running, and for any graph carrying one
of the blocking problems listed above.

Three more buttons share that row. **Load** and **Save** read and write the
graph as JSON, defaulting to the output directory; **Clear** empties the canvas
after a confirmation. Saving is not applying: a saved file is a paradigm you can
reload, while the board only ever holds what was last uploaded.

A recording started with blocks on the canvas also writes `stim_paradigm.json`
and `stim_paradigm.ino` into the recording folder, with the firmware hash and
`matches_uploaded_firmware`, so a session describes the stimulation it delivered
without the editor. Read that flag first: the files describe the *canvas*, and
only `matches_uploaded_firmware: true` says the canvas is what the board was
carrying. `false` means it is not, and `null` means nothing was uploaded in that
GUI session, so the board's contents are unknown rather than wrong.

---

## The waveform preview

Glance at the small plot in the stimulation editor (2) every time you type
numbers into Freq and PW. It draws one second of the wave those fields would
produce and captions it, so you see the shape of the stimulation before it is
compiled into firmware and delivered to an animal. It follows the fields rather
than whichever block is selected, so it shows the values you are about to
commit.

Frequency and pulse width are independent fields, and neither shows the duty
cycle the pair implies. The period is `1000 / freq_hz` milliseconds, and the
duty cycle as a percentage is `pulse_width_ms x freq_hz / 10`.

These five figures are rendered illustrations of specific parameter pairs.

| Fields | Preview |
|---|---|
| 10 Hz, 10 ms — period 100 ms, 10% duty | ![10 Hz, 10 ms pulse](images/wave_10hz_10ms.png) |
| 40 Hz, 5 ms — period 25 ms, 20% duty | ![40 Hz, 5 ms pulse](images/wave_40hz_5ms.png) |
| 20 Hz, 25 ms — period 50 ms, 50% duty | ![20 Hz, 25 ms pulse](images/wave_20hz_25ms.png) |
| 0 Hz — pin held LOW | ![0 Hz](images/wave_0hz.png) |
| 10 Hz, 100 ms — period 100 ms, 100% duty | ![10 Hz, 100 ms pulse](images/wave_10hz_100ms.png) |

The first three are normal trains: same shape, different density and duty. A
train denser than 400 cycles per second of preview is drawn as a band rather
than individual pulses.

`0 Hz`, or a pulse width of 0, holds the pin LOW for the block's duration: a
legitimate and common block, the way a rest interval between stimulation periods
is written.

**The 100% case is the one to watch.** 10 Hz with a 100 ms pulse width is a
100 ms period fully filled: the pin goes HIGH and stays HIGH for the whole
block. It is not a 10 Hz train and it is not an error. The preview draws the
single rising edge, glows the border red, and captions it
`100% duty — constant ON, not 10 Hz`. A pulse width *longer* than the period
does the same thing physically, and is captioned
`pulse 150 ms > period 100 ms`.

The firmware drives the pin constantly HIGH in both cases rather than rejecting
them, so that is what the preview draws and what the block's body text says
(`constant ON`). Refusing to represent them would hold the pin LOW for the whole
block instead: a silent nothing where stimulation was intended, invisible in the
recorded data.

---

## Controls that hide, and controls that disable

Check here when a control is not where you expected. A few things appear only
when they have something to report; more stay visible but refuse to be pressed
while they would do the wrong thing.

**Hidden until something is running:**

- The **coverage HUD** (12), only while a calibration is in progress, and only
  if OpenCV and the board config are both available.
- The **progress bar** (15), only during encoding or alignment.
- The **status bar** (17), until the first event that produces a message.

**Disabled rather than hidden:**

- **Record** while Calibrate is on, and Calibrate while Record is on.
- Both toggles during encoding and alignment.
- **Solve** for the duration of a solve; it also refuses while acquiring or
  encoding.
- During any blocking operation (a profile switch, the end-of-acquisition
  finalise, the startup firmware flash, or the per-acquisition firmware flash
  before a calibration or a recording): the profile dropdown, output directory,
  both toggles, Solve, Snapshot and every metadata field, with a wait cursor.
  The state label (16) names the operation. The firmware flashes are the long
  ones, about 30 s.
- The metadata fields and the output directory go read-only for the duration of
  an acquisition, since they name the folder being written.
- In the stimulation editor, **Starting** and **Ending** until exactly one block
  is selected; **Apply** and **Test** while an upload is in flight; **Apply**
  while a test is running.

**At launch**, a hardware check runs in the background and raises a dialog only
if it finds something worth mentioning: fewer than 4 physical CPU cores, under
16 GB of RAM, under 500 GB of free disk, a measured disk write speed under
500 MB/s, or no `h264_nvenc` encoder in the bundled ffmpeg. It is advisory and
stops nothing, so an underpowered machine is known about before a session rather
than during one.

**On quit**, if a session is still in progress the app asks for confirmation and
then deletes the incomplete data, on the grounds that a half-written session is
worse than none. It also stands the trigger board down whether or not an
acquisition was running, so closing the window cannot leave a paradigm or a
laser running behind it, and says so loudly if the board does not accept that
stop.
