# Overview: a tour of the window

Before we record anything, let's take a quick tour of Panopticon. It helps to have it
open beside you, so if it isn't running yet, start it from the desktop shortcut you
made during [installation](INSTALLATION.md#step-10--desktop-shortcut).

Panopticon has two windows. The **main window** is where we preview the cameras,
calibrate and record. The **stimulation editor** opens from it, and it's where you
build the optogenetic [paradigms](GLOSSARY.md#paradigm) that the
[trigger board](GLOSSARY.md#trigger-board) plays during a recording.

> [!NOTE]
> The first time you open Panopticon on a new computer, the camera grid is empty and
> the sidebar reads **Choose a profile**. Pick your rig's profile from the dropdown
> ([4](#4-profile)) and Panopticon will open the cameras and check the hardware.
> **Calibrate** and **Record** stay greyed out until the status bar says
> `Hardware check done: encoding with <encoder>`.

## The main window

![The Panopticon main window with numbered callouts](images/ui_annotated.png)

The cameras fill the left of the window. On the right, the **sidebar** holds the
**Metadata**, **Acquisition** and **Display** controls, with the state label at the
bottom, and the status bar runs along the bottom of the window. We staged this picture
so that everything shows at once. Normally you'll only see the coverage display (12)
during a calibration, and the progress bar (15) while the videos are being finished.

Anything you can't set in the window, from the camera settings to the trigger pins,
lives in the [rig profile](GLOSSARY.md#rig-profile), a YAML file that describes your
rig. [CONFIGURATION](CONFIGURATION.md) goes through it.

> [!TIP]
> Panopticon remembers how you left it: the window and sidebar sizes, the session
> fields, each profile's output folder and **Flat final second**, any profile you added
> from a file, the **Display** sliders and the stimulation editor's fields. Two things
> start fresh at every launch. The **Date** is set to today, and the editor's canvas is
> empty, so nothing reaches the trigger board until you **Apply** it.

### Preview

#### 1. Camera grid

The grid shows live video from every camera, laid out to fit: 4 cameras in 2 × 2, 6 in
two rows of three, and 9 in 3 × 3.

At rest, the cameras free-run at 30 fps and you see every frame. During a calibration
or a recording they follow the trigger board instead, and while recording the preview
shows every 10th frame. The preview is shrunk and drawn at its own pace, so it never
slows the capture down.[^preview]

While one of the jobs listed under [State](#16-state) runs, the preview freezes on its
last frame. Don't worry, it picks up again when the job's done.

#### 2. Camera pane

Each pane shows its camera's name in the top-left corner: `cam1`, `cam2` and so on.
[Choose a profile](WORKFLOW.md#2-choose-a-profile) explains where the names come from.

Double-click a pane to fill the grid with that one camera, which is handy for checking
focus or framing. Double-click it again to go back. The other cameras keep capturing
while they're hidden.

![One camera enlarged by a double-click](images/window_camera_zoomed.png)

If a pane stays black, have a look at
[Opening the cameras](TROUBLESHOOTING.md#opening-the-cameras).

#### 3. Frame rate

The green number at the bottom-left of each pane is the rate that camera is actually
delivering, averaged over its last ten frames. At rest, every pane should read about
30 fps. During a recording it shows the camera's rate, not the preview's, so on our rig
it reads about 100 fps.

A pane that jumps high for a refresh or two, like the 67 and 69 fps in the picture at
the top, is just catching up after the computer was busy. What's worth a closer look is
one pane reading *low* while the others look fine. It's usually the first sign of
trouble with that camera's cable or trigger, and
[During an acquisition](TROUBLESHOOTING.md#during-an-acquisition) says what to check.

### Metadata

The **Metadata** group is where you tell Panopticon which rig this is, where to save,
and who's being recorded.

![The Metadata group filled in with example values](images/sidebar_metadata.png)

#### 4. Profile

The dropdown at the top picks the rig profile. Click it to see every profile in the
`profiles/` folder, with **Add a profile from a file…** at the bottom:

![The profile dropdown open](images/sidebar_profile_dropdown.png)

On our rig, choose **3dpose**. **3dface** is the other rig that shares this software,
and **sim** runs [simulated cameras](SIMULATION.md) so you can practise. Your own list
may look different. If your profile lives outside `profiles/`, pick it with
**Add a profile from a file…** and it'll stay in the list from then on.

When you pick a profile, the state label reads **Switching cameras…** while the
cameras close and reopen, then goes back to **IDLE**. A switch can also stop or flash
the trigger board, and [Choose a profile](WORKFLOW.md#2-choose-a-profile) explains
when.

The list won't switch while Panopticon is uploading a paradigm, running a stimulation
**Test** or checking the hardware. A dialog tells you what it's waiting for, and you
can switch once that's done.

#### 5. Output directory

The button under the dropdown shows where your sessions are saved. Long paths get
shortened in the middle, so hover over the button to see the whole thing. Click it to
pick another folder in the **Select Output Directory** dialog, and Panopticon will
remember your choice for this profile.

On our rig, sessions go to the repository's `data` folder, though the pictures here
were taken with a test folder.

#### 6. Session fields

**Date**, **Mouse 1**, **Mouse 2**, **Experimenter**, **Cage** and **Notes** describe
the session. The date and the two mouse fields also name the session's folder, so
everything from one session lands in one place. There's more on each field in
[Fill in the metadata](WORKFLOW.md#4-fill-in-the-metadata).

### Acquisition

The **Acquisition** group has the controls you'll use most.

![The Acquisition group at rest](images/sidebar_acquisition.png)

**Calibrate** and **Record** are toggles. Click one to start: its knob slides to the
right and the track fills with colour, blue for **Calibrate** and red for **Record**.
Click it again to stop. While one toggle is on, the other is greyed out. **Solve**,
**Snapshot** and **Stimulation** are ordinary buttons.

A typical session goes **Calibrate**, **Solve**, **Record**.

#### 7. Calibrate

Flip **Calibrate** on and carry the [ChArUco board](GLOSSARY.md#charuco-board)
through every camera's view until the coverage display reads **READY**, then flip it
off. The [calibration walkthrough](WORKFLOW.md#5-calibrate) takes you through a whole
take.

Once it's on, the state label reads **Checking capacity…**, **Starting...** and then
**CALIBRATING** in blue. When you flip it off, you'll see **Finishing…**, then
**ENCODING** while the progress bar fills, and finally **IDLE**.

If you'll end the take by laying the board flat on the arena floor for a second or
two, tick the small **Flat final second** box under the toggle:

![Flat final second ticked, under the Calibrate toggle](images/sidebar_flat_final_second.png)

**Solve** will then put the floor at Z = 0.

> [!NOTE]
> If there's a stimulation paradigm on the trigger board, Panopticon first swaps it
> for the recording-only sketch. The state label reads
> **Flashing recording-only firmware…** for about 30 s. Leave Panopticon open until
> it's done.

#### 8. Record

Flip **Record** on to start a recording at the profile's `frame_rate`. Panopticon
runs a few checks first, and if one fails, a dialog tells you why
([Starting an acquisition](TROUBLESHOOTING.md#starting-an-acquisition) lists them).
The [recording walkthrough](WORKFLOW.md#8-record) covers the question you'll get if
the session folder already holds data.

The state label goes through the same steps as for **Calibrate**, then reads
**RECORDING** in red. Flip it off and you'll see **Finishing…**, **ENCODING** and
**IDLE** again. If your paradigm has an [**Ending**](#8-ending) block, the recording
also stops by itself when that block finishes.

#### 9. Solve

**Solve** turns the calibration you just took into a model of every camera's lens and
position. It reads the calibration from the session folder that **Date**, **Mouse 1**
and **Mouse 2** point to, so leave those as they were for the calibration.

While it runs, the state label reads **CALIBRATING...** in purple and the cameras
don't capture. When it's done, the status bar shows the result, and
[Solve](WORKFLOW.md#6-solve) covers what to look for.

#### 10. Snapshot

**Snapshot** saves one full-resolution PNG from every camera into the session's
`snapshots/` folder. The status bar reads `Snapshot: saving N cameras…`, then
`Snapshot: saved k/N cameras → <folder>`:

![The status bar after a snapshot](images/snapshot_saved.png)

Snapshots are great for judging focus, which the shrunken preview hides. They're taken
from the full frame, so the **Display** sliders don't change them. If a camera sent no
frame, the message ends with `no frame from:` and the camera's name.

#### 11. Stimulation

**Stimulation** opens the [stimulation editor](#the-stimulation-editor). The editor
can stay open beside the main window, and the button keeps working while the other
controls are busy.

The editor needs a trigger board to play on, so before you've chosen a profile, or on
a profile with `trigger_source: external`, the button just shows a dialog saying why.

#### 12. Coverage display

During a calibration, the coverage display shows up here and tells you whether you
have enough views of the board. We'll look at it more closely
[below](#the-calibration-coverage-hud).

> [!NOTE]
> If it doesn't appear, the calibration still records, but Panopticon couldn't set up
> its board detector. Either the profile's
> [`board_config`](CONFIGURATION.md#board_config) file is missing or can't be used, or
> OpenCV isn't installed. Check that `board_config` names your board's file before the
> next take.

### Display

The **Display** group has two sliders. The progress bar and the state label sit below
them.

![The brightness and contrast sliders, Brightness dragged right](images/sidebar_display.png)

#### 13 and 14. Brightness and contrast

Drag **Brightness** to the right to brighten the preview, and **Contrast** to the right
to raise its contrast. Left does the opposite, and the middle leaves the preview
untouched. Only the preview changes: the recording, the snapshots and the board
detection all use the frames as they arrive.

> [!TIP]
> The sliders keep their positions between launches and show no number. If the panes
> look black, grey or washed out while their frame rates look normal, drag both
> handles back to the middle.

#### 15. Progress

The progress bar appears after a stop, while Panopticon finishes the videos
(**Encoding k/N**) or lines them up by trigger (**Aligning k/N**), where N is the
number of cameras.

#### 16. State

The label at the bottom of the sidebar tells you what Panopticon is doing right now:

| Label | What's happening |
|---|---|
| **IDLE** (grey) | Nothing is running, and the cameras are in free-run preview |
| **CALIBRATING** (blue) | A calibration is recording |
| **RECORDING** (red) | A recording is running |
| **ENCODING**, **ALIGNING** (amber) | Panopticon is finishing the videos after a stop |
| **CALIBRATING...** (purple) | A solve is running, and no camera is capturing |
| **WAITING FOR TRIGGER**, **STOP YOUR TRIGGER SOURCE** (amber) | You're using [your own trigger source](WORKFLOW.md#your-own-trigger-source): start it, or stop it, now |
| **NO TRIGGER** (red) | Your own trigger source sent no pulse, so nothing was recorded |
| **Choose a profile**, **No profile** (amber) | No profile is open yet |

> [!TIP]
> Mind the dots! **CALIBRATING...** with three dots, in purple, is a solve. The
> cameras aren't recording and it's fine to walk into the arena. **CALIBRATING**
> without dots, in blue, is a calibration take.

Some jobs make you wait. While one runs, the label names it in amber, anything that
could start something else is greyed out, and the cursor shows that Panopticon is
busy:

| Label | What Panopticon is doing |
|---|---|
| **Switching cameras…** | Closing the cameras and opening the new profile's |
| **Clearing stim firmware…** | At launch, loading the [recording-only sketch](GLOSSARY.md#recording-only-sketch) onto a board that may still carry a paradigm |
| **Flashing recording-only firmware…** | Taking the paradigm off the board before a calibration |
| **Flashing recording + stimulation firmware…** | Putting the paradigm you applied back on before a recording |
| **Checking capacity…** | Making sure the computer has the memory and encoders for the acquisition |
| **Starting...** | Arming the cameras and starting the trigger board |
| **Finishing…** | Stopping the cameras and saving the capture |
| **Updating the stimulus trace...** | Rewriting `stim_trace.csv` after an alignment |
| **Cancelling…** | Backing out of a start on your own trigger source that recorded nothing |

The **Clearing** and **Flashing** jobs load a new program onto the trigger board,
which takes about 30 s. The **Flashing** ones happen right after you press
**Calibrate** or **Record**, and the acquisition starts as soon as the flash is done.

> [!WARNING]
> Panopticon won't close while the board is being flashed, and don't end it from Task
> Manager then either. [When the board resets](WORKFLOW.md#when-the-board-resets)
> explains why, and what it means for anything wired to the board.

#### 17. Status bar

The line along the bottom of the window keeps you posted: the hardware check,
snapshot results, the solve's progress, and a summary of the encode after each stop.
During a recording it reports on the capture every time the frame rates refresh, like
the `Capture healthy — keeping up with the trigger (max lag 3 ms)` in the picture at
the top. We go through the messages you might see in
[the recording walkthrough](WORKFLOW.md#8-record).

### The sidebar's width

The sidebar starts out as narrow as it goes. Drag the thin divider on its left edge to
widen it. Once it's wide enough, the metadata fields sit two to a row, which gives the
coverage display more room during a calibration:

![A wide sidebar during a calibration](images/window_sidebar_wide.png)

The panes look brighter here because the cameras switch to the profile's calibration
exposure for the take.

Drag the divider all the way to the right and the sidebar folds away, leaving the
whole window to the cameras:

![The window with the sidebar dragged shut](images/window_sidebar_collapsed.png)

To get it back, drag the thin strip on the window's right edge to the left. It lights
up when your pointer finds it.

---

## The calibration coverage HUD

While you carry the board around during a calibration, the coverage display tells you
whether you've collected enough views for a good solve. Each camera is a numbered
node on a ring, with a line between every pair.

![The coverage display partway through a take](images/calib_stage_2_partial.png)

- A camera's node **lights up** when it sees 4 or more of the board's markers, and
  the glow fades over about 0.4 s once the board leaves.
- The line between two cameras **thickens and whitens** as they see the board
  together, with 5 or more markers in each view. It's at full strength after 200
  such ticks.[^tick]
- The little 2 × 2 grid on each node shows which **quarters** of that camera's view
  the board has visited.
- The caption keeps score. `paired` and `grid` show how the worst camera is doing
  against its target, and `groups` counts separate groups of cameras, which you want
  at `1/1`. Some profiles add `link` and `views`.
- The **orange line** tells you what's holding READY back. While the cameras are
  split, it lists the groups, as in the picture. After that it names the weakest link
  between two halves of the rig, and last of all the cameras whose views need to be
  `closer`, have more `tilt`, or reach the `edges`. Those cameras get an orange rim.

When it's all covered, the graph turns white and the caption reads `READY — m:ss`,
with the timer stopped. You can flip **Calibrate** off from then on, or keep going:
every extra view gives the solve one more to choose from. The
[calibration walkthrough](WORKFLOW.md#5-calibrate) shows the display filling in over
a real take.

### What READY needs

READY needs all of these at once. Each is a
[profile setting](CONFIGURATION.md#calibration_min_per_cam_shared), and the numbers
here are our rig's. A profile can switch the last two off by setting them to 0.

1. **paired**: each camera has shared the board with another camera on 120 ticks
   (`calibration_min_per_cam_shared`).
2. **grid**: each camera has seen it in 3 of its four quarters
   (`calibration_min_grid_cells`).
3. **groups**: the pairs that have shared 20 ticks (`calibration_min_edge`) link every
   camera into one group, directly or through other cameras.
4. **link**: the group is well knit, meaning its algebraic connectivity reaches 30
   (`calibration_min_connectivity`). One weak link between two halves of the rig
   keeps it low.
5. **views**: each camera's shared views pass the view tests. `closer` wants a board
   size of 0.10 in 5 views, `tilt` wants 40 degrees of tilt in 5 views, and `edges`
   wants marker corners in 7 of the 16 cells of a 4 × 4 grid.

Why one group, and not every pair? Cameras that face each other never see the front
of the board at the same time, so they can only join up through their neighbours. The
quarters and the view tests stop you from waving the board in one spot, which gives
lens models that fit the middle of the image and go wrong towards the edges.

---

## The stimulation editor

![The stimulation editor with numbered callouts](images/stim_annotated.png)

The stimulation editor is where you build a paradigm before a recording. A paradigm is
a small graph of **blocks**, each driving one pin of the trigger board with a square
wave for a set time. An arrow means "when this block ends, start that one", so a
chain runs in order and separate chains run side by side. Every chain starts with the
recording's first trigger.

The picture shows a loop on pin 53: 5 s of 10 Hz pulses, then 10 s with the pin held
LOW, over and over until the recording stops.[^status] You can't see the arrow back to
the first block, since it runs behind the forward one. The numbers in this picture
start again at 1.

The [stimulation walkthrough](WORKFLOW.md#7-optional-stimulation) covers building,
testing and checking a paradigm. Here we'll just go round the editor's parts.

#### 1. Canvas

The canvas starts empty. To add a block, fill in **Pin**, **Freq**, **PW** and **Dur**
and click **Create Block**. The new block appears in the middle of the view, or to the
right of the ones already there.

- **Drag** a block to move it.
- **Drag from a port**, one of the little circles on a block's edges, to another
  block's port to draw an arrow. A block takes any number of arrows in but only one
  out, so dragging from a block that already has one just moves that arrow.
- **Click** a block to select it and load its values into the fields. Drag across
  empty space to select several, and hold <kbd>Shift</kbd> to add to the selection.

Each block shows its pin, frequency, pulse width and duration, and what the pin will
actually do: `10% duty`, `constant ON` or `pin LOW`. Its colour follows its pin. A few
marks tell you how the graph will run:

- A **red dot** at the top right marks the block that starts a chain. It has a white
  ring if you ticked **Starting** yourself.
- A **hollow amber ring** marks a loop with no start, which won't run.
- A **black dot** marks the **Ending** block.
- Blocks that don't start a chain are drawn faded.

> [!TIP]
> <kbd>Delete</kbd> removes the selection, <kbd>Ctrl</kbd>+<kbd>C</kbd> and
> <kbd>Ctrl</kbd>+<kbd>V</kbd> copy and paste, <kbd>Esc</kbd> clears the selection,
> and <kbd>Home</kbd> fits the graph in view. Middle-drag to pan and scroll to zoom.
> There's no undo, so **Save** before a big edit.

#### 2. Waveform preview

The little plot at the bottom left draws one second of the wave that **Freq** and
**PW** describe, so give it a glance every time you type them in. We'll come back to
it [below](#the-waveform-preview).

#### 3. Pin

The trigger board pin this block drives. There's no default, so you always type one
in. On our rig, the stimulation pin is 53.

Some pins are off limits, like the camera trigger pins, and **Apply**, **Test**,
**Calibrate** and **Record** all refuse a paradigm that uses one.
[Pins](WORKFLOW.md#pins) lists them. Two chains can't
drive the same pin, but one chain can use a pin in several blocks, since its blocks
take turns.

#### 4. Freq (Hz)

Pulses per second. A frequency of 0 holds the pin LOW for the whole block, which is
how you write a pause.

#### 5. PW (ms)

The pulse width: how long the pin stays HIGH in each cycle. It's easy to ask for a
longer pulse than a cycle can hold, but the preview will catch it.

#### 6. Dur (s)

How long the block runs before its chain moves on. It has to be more than 0, and
fractions are fine.

> [!TIP]
> Press <kbd>Enter</kbd> in any of these four fields to apply the values to the
> selected block. With nothing selected, <kbd>Enter</kbd> creates a new block, just
> like **Create Block**. A new block fills a blank field with 0 Hz, 0 ms or 1 s.

#### 7. Starting

Marks the selected block as the start of its group. Most of the time you won't need
it, since a block with no arrow coming in already starts a chain. A loop has no such
block, though, so it needs **Starting** or it never runs. Each connected group has a
single start, so ticking one clears the others.

#### 8. Ending

Marks the block that ends the recording: the first time it finishes, the recording
stops. A canvas can only have one. A loop on its own keeps going until you stop the
recording, so to let a loop run for a set time, put the **Ending** block in a separate
chain beside it.

#### 9. Status line

The line under the fields tells you what the graph will do, like
`Recording will stop 15 s after start.` When it turns red, it's telling you what's
stopping the graph from running: a loop with no start, a pin driven by two chains, an
**Ending** block that no chain reaches, or a number it can't read. It also shows
upload progress, the test countdown, and whether the last upload or test stop worked.

#### 10. Test

**Test** runs the paradigm on the trigger board for real, without the cameras. The
pins switch just as they would in a recording, so whatever is wired to them runs too.
While it runs, the button reads **Stop Test** and the status line counts down
(`Testing — 12 s remaining.`). A looping paradigm reads
`Testing — looping, press Stop Test to end.` and keeps going until you press
**Stop Test** or close the editor.

Test runs whatever is on the board, so if you've changed the canvas since the last
upload, it asks whether to upload first. A Test can also reset the board, and
[When the board resets](WORKFLOW.md#when-the-board-resets) says when.

> [!WARNING]
> If the board doesn't confirm the stop, the status line reads
> `STOP NOT CONFIRMED — stim may still be running.` and a dialog appears.
> Power-cycle the board.

#### 11. Apply to Arduino

**Apply to Arduino** compiles the graph and flashes it onto the trigger board, which
takes about 30 s. When it works, the status line reads
`Upload successful — press Record to run paradigm.` and your next recording runs it.
Panopticon takes the paradigm off the board for each calibration and puts it back for
the recording after. **Apply** won't run while the status line shows a problem in
red, or while something else is using the board.

> [!WARNING]
> A failed upload leaves the board in an unknown state, possibly without its
> [safe-pin](GLOSSARY.md#safe-pin) guard. Apply again: **Record**, **Calibrate** and
> **Test** refuse to run until an **Apply** succeeds.

**Save** and **Load** keep the graph in a JSON file, `stim_config.json` in the output
directory unless you pick another name. **Load** asks before replacing unsaved
changes, and **Clear** asks before emptying the canvas. None of them touch the board.

---

## The waveform preview

The waveform preview draws one second of the wave, with a caption, so you can see the
shape of the stimulation before it reaches the board. The period is `1000 / freq` ms,
and the duty cycle is `pulse width × freq / 10` percent.[^cycles]

| Fields | Preview |
|---|---|
| 10 Hz, 10 ms: period 100 ms, 10% duty | ![10 Hz, 10 ms pulse](images/wave_10hz_10ms.png) |
| 40 Hz, 5 ms: period 25 ms, 20% duty | ![40 Hz, 5 ms pulse](images/wave_40hz_5ms.png) |
| 20 Hz, 25 ms: period 50 ms, 50% duty | ![20 Hz, 25 ms pulse](images/wave_20hz_25ms.png) |
| 0 Hz: pin held LOW | ![0 Hz](images/wave_0hz.png) |
| 10 Hz, 100 ms: period 100 ms, 100% duty | ![10 Hz, 100 ms pulse](images/wave_10hz_100ms.png) |

Watch out for the last row. At 10 Hz each period is 100 ms, so a 100 ms pulse fills
it completely, and the pin goes HIGH and stays there for the whole block. The preview
draws one rising edge, turns its border red and says
`100% duty — constant ON, not 10 Hz`. A pulse longer than its period gets the same
treatment, with a caption like `pulse 150 ms > period 100 ms`. Either way the board
holds the pin HIGH and the block reads `constant ON`.

---

## Controls that hide, and controls that disable

The coverage display (12) and the progress bar (15) stay hidden until they have
something to show. Other controls grey out when using them would do the wrong thing.
Hover over a greyed-out toggle or **Solve** to see why.

- **Calibrate** and **Record**: while the hardware check runs, while videos are
  encoded or aligned, during a solve or a stimulation upload, and until a profile is
  open.
- **Solve**: while a solve runs, and until a profile is open. During an acquisition
  or an encode you can still click it, but the status bar just says
  `Solve unavailable while acquiring/encoding`.
- The dropdown, the output folder button and the session fields: from the start of
  an acquisition until its videos are finished, and during a solve.
- During the jobs in the second table under [State](#16-state): everything above,
  plus **Snapshot**. **Stimulation** and the sliders keep working.
- In the editor: **Starting** and **Ending** until one block is selected, **Apply**
  and **Test** during an upload, and **Apply** during a **Test**.

If you quit in the middle of something, [After you stop](WORKFLOW.md#9-after-you-stop)
says what happens.

[*Next up:* Running a session](WORKFLOW.md)

[^preview]: The preview downsamples each frame 3× per axis, so 1920 × 1200 becomes
    640 × 400. The grid repaints every 33 × N / 6 ms for N cameras, but never more
    often than every 33 ms or less often than every 100 ms.

[^tick]: A tick is one pass of the board detector over the latest full-resolution
    frame from every camera. It visits the cameras one at a time, so a cluttered
    arena slows it down. On our rig it manages 4 to 5 ticks a second, and the log
    line `[hud] coverage ticks/s:` gives the rate.

[^status]: The text in the picture's status line (9) was added for the picture. For a
    loop with no **Ending** block, the editor leaves that line empty.

[^cycles]: The preview draws up to 400 cycles, so a train above 400 Hz fills only the
    first 400/f seconds of the plot, as a solid band.
