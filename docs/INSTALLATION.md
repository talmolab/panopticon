# Installation

This page takes a rig that is already wired up to a first working launch. It
assumes no terminal experience: every command is written out in full, with the
output you should expect.

Physical construction is not covered. Mounting, lighting and power depend on
what you are filming, not on the software. The one piece of wiring that is
neither optional nor obvious is the trigger line from the microcontroller to the
cameras, under [A trigger source](#a-trigger-source) below.

Contents:

1. [What the rig needs](#1-what-the-rig-needs)
2. [Install the software](#2-install-the-software)
3. [Verify it works](#3-verify-it-works)
4. [Troubleshooting](#4-troubleshooting)

---

## 1. What the rig needs

Nothing here names a part number. The load every part of the rig carries, from
the camera's Ethernet port to the disk, follows from four numbers you choose
before buying anything: resolution, bit depth, frame rate, camera count.

Two cameras at 1920x1200 and 30 fps are happy on ordinary gigabit hardware. Six
at 100 fps need a 10 GbE network, tens of gigabytes of RAM and a GPU encoder. A
rig short on any one of those does not error; it quietly delivers fewer frames
than it triggered. Run the arithmetic below on your own numbers and you will
know which part binds first.

It all follows from the size of one frame. For mono8, one byte per pixel and the
only format the capture path accepts, that is width times height:

```
frame_bytes = width x height
1920 x 1200 = 2,304,000 bytes  (~2.3 MB per frame per camera)
```

### Cameras

Basler cameras work today, through pypylon. `gui_app/backends/basler.py` is the
only module that imports pypylon; everything else is vendor-neutral. Another
vendor means writing one file against the `CameraBackend` contract in
`gui_app/backends/__init__.py` and registering it in `load_backend()`. The API
is the easy part. The contract requires three guarantees:

- a per-frame **monotonic trigger ordinal**, the same value on every camera for
  a given trigger, surviving a stream restart (Basler: the GigE Vision BlockID).
  Cross-camera alignment has nothing to align on without it.
- a **buffer pool** deep enough to absorb network jitter, plus a counter that
  reveals when it has run dry.
- **zero-copy access** to the pixel data. A copying accessor puts a full-frame
  memcpy on the per-frame hot path while holding the global interpreter lock
  (the GIL, which lets only one thread run Python at a time), and that alone
  loses frames on every camera. Measure it before assuming it is affordable.

Each camera also needs a **hardware trigger input**. In triggered mode the app
sets `TriggerSelector=FrameStart`, `TriggerMode=On`, `TriggerSource=Line1`,
`TriggerActivation=RisingEdge`.

Every camera must be set to **Mono8** and to the **same resolution**. The
capture path assumes 8 bits. A 12-bit frame arrives as 16-bit data and is
truncated mod 256 with no error at all, producing a full-length, perfectly
aligned, visually shredded recording. Opening refuses on both mismatches.

### A trigger source

An Arduino- or Teensy-class microcontroller on a serial port, with one output
pin per camera (the profile's `trigger_pins`) and, if you use optogenetic
stimulation, one more pin for the stimulus driver. The reference firmware
targets `arduino:avr:mega`.

**Why hardware triggering rather than software sync.** Under software sync each
camera free-runs on its own crystal oscillator. Those oscillators differ, so the
cameras drift apart continuously, leaving no per-frame correspondence to
recover, only an estimate that decays over the recording. A shared TTL edge
makes frame *N* of every camera the same instant of the world, to within the
cameras' trigger-to-exposure jitter. Triangulation intersects rays from several
views taken at one instant, so non-simultaneous views miss a moving animal's
true position by roughly its speed times the timing offset.

Hardware triggering also gives every frame a shared ordinal. Cameras lose
packets independently, so frame *i* of one video is not frame *i* of another,
but the trigger ordinal is, and it is recorded per frame.

#### Wiring the trigger line

Get this wrong and you cannot repair it afterwards: a camera that misses
triggers, or exposes on anything other than the shared line, yields a recording
whose views are not simultaneous. Every camera's `Line1` — the input the cameras
trigger on (`TriggerSource=Line1`, set for you when acquisition starts) — has to
be driven by a pin listed in the profile's `trigger_pins`. `Line1` sits on the
camera's I/O connector, separate from the network one; the data sheet gives its
pinout.

That is **not** the same as one pin per camera. A single output can feed several
cameras, and the reference rig does exactly that: nine cameras on the six pins
`[2, 4, 6, 8, 10, 12]`. What sets the limit is current, not logic — see *One pin
per camera, or one pin fanned out to several?* below. Whichever you choose, the
list must cover every pin that has a camera on it; a camera on an unlisted pin
never fires.

**Which pins you may choose.** Any digital output will do except three
categories, and two of them are refused outright rather than silently
misbehaving:

| Unavailable | Why |
|---|---|
| The board's UART pins (`0` and `1` on an Arduino Mega) | They carry the serial link that configures the board and acknowledges the start of a recording. Driving them garbles it. Refused by the stim compiler. |
| Any pin used for stimulation | Refused, and the reverse is refused too: a stimulation block on a trigger pin injects extra edges into one camera, so its block IDs advance faster than everyone else's and the trigger ordinal stops meaning the same instant in every view. Nothing downstream can detect that. |
| Pins with a conflicting alternate function on your board | Not enforced, so check the pinout. On a Mega, `14`-`19` are the extra hardware serial ports (`Serial1`/`2`/`3`); the shipped firmware uses only `Serial0`, so they *work* as digital outputs, but avoiding them keeps those ports free. |

Beyond that the numbers are arbitrary. Pick a contiguous, easy-to-wire run and
write down which pin goes to which camera — not because the software cares, but
because you will need it when one camera stops triggering.

Three things have to be true besides the signal wire.

**Common ground.** A trigger is a voltage difference; without a shared reference
the camera has nothing to measure the board's output against. Run a ground wire
from the board to the camera I/O connector's ground pin alongside each signal
wire, or to a single ground point they all share.

**Power**, which does not come from the trigger line. Depending on the model
that is either Power over Ethernet from the switch, which then has to supply it
and have the budget for every camera plugged into it, or an external supply into
the I/O connector. The data sheet says which; settle it before ordering the
switch.

**A signal the camera recognises.** An Arduino Mega drives 5 V HIGH and 0 V LOW,
an ordinary TTL level, but the input at the far end varies by camera model. Some
machine-vision trigger inputs are opto-isolated, and an opto-coupler is driven by
current rather than voltage, so "it is 5 V, it will work" is not enough. Two
numbers from the camera's I/O documentation settle it: the input's switching
threshold, and the current it draws when driven.

**One pin per camera, or one pin fanned out to several?** The generated sketch
writes every pin in `trigger_pins` inside a single `noInterrupts()` block, so
every camera gets its edge in the same instant; campy, by Kyle Severson, the
firmware lineage this comes from, documents roughly 30 ns of synchronicity
between pins. The pins being identical in time, a fan-out is equivalent to
several pins, *provided that one output can source the current every input
draws*. An ATmega2560 output is rated for roughly 20 mA, so a fan-out is only
safe if the inputs' combined current stays well inside that, and getting it
wrong risks the output driver, not just the trigger. One pin per camera gives
each input the whole 20 mA budget and needs no arithmetic; fanning out trades
that headroom for fewer wires, which is how the reference rig runs nine cameras
on six pins.

Marginal current is worth calling out separately, because it does not fail
cleanly. A camera that is under-driven misses triggers *intermittently*, and a
missed trigger is not a dropped frame: the camera never acquires, so it consumes
no block ID, leaves no gap, and moves no error counter. The videos come out
equal in length and drift apart in time. Only the block-ID rate check catches
it, and only after the recording — see *The recording looks fine but the views
are out of sync* in section 4. If you fan out, get the input current from the
camera's I/O documentation and do the arithmetic rather than trusting that it
looked fine on the bench.

The *order* of the list carries no meaning; all the pins take the same edge
together. What matters is that every camera's `Line1` is driven by a pin the
list names — one entry per camera if you wire it that way, fewer if you fan out.
A camera fed by a pin outside `trigger_pins` never triggers, and
because the default mode holds every trigger until all cameras have delivered it
(see *RAM* below), one camera that never delivers stalls the whole recording.

### Network

A link near capacity does not slow down, it drops packets, and a GigE Vision
frame with a missing packet is a frame you do not get. Work out what the cameras
will put on the wire before choosing ports and switches. One camera's demand on
its link is the frame size in bits, times the frame rate:

```
bits per frame  = width x height x bits per pixel
                = 1920 x 1200 x 8            = 18,432,000 bits
bits per second = bits per frame x frame rate
                = 18,432,000 x 100           = 1.84 Gbit/s per camera
```

A few common configurations, for reference:

| Format | Bytes per frame | Payload rate |
|---|---|---|
| 1920x1200 mono8, 100 fps | 2,304,000 | 1.84 Gbit/s |
| 1920x1200 mono8, 30 fps | 2,304,000 | 553 Mbit/s |
| 1280x1024 mono8, 100 fps | 1,310,720 | 1.05 Gbit/s |

Those are payload only, before the headers of GVSP (the GigE Vision streaming
protocol the cameras speak), UDP, IP and Ethernet. Treat them as a floor rather
than a budget.

**Two links have to carry that rate, not one.** Both get loosely called "GigE",
and the word invites the wrong purchase. "GigE Vision" names the protocol, not a
speed: the same standard runs over 1, 2.5, 5, 10 and 25 Gbit/s links alike. The
**camera's own** link carries that one camera's payload alone. The **host port**
carries the sum of everything behind it. Getting the second right and the first
wrong is the classic irrecoverable mistake: no tuning fixes a camera whose own
cable is too slow.

Take the camera's own link first. One 1920x1200 mono8 camera at 100 fps wants
1.84 Gbit/s, nearly twice what a 1 Gbit/s link carries, so a plain 1 GigE camera
cannot do it at any packet size: 1 Gbit/s divided by 18,432,000 bits per frame
is about 54 fps at line rate, less once headers and a sane margin are counted.
1920x1200 at 100 fps needs 5GBASE-T or 10GBASE-T, which is why the reference rig
uses the a2A1920-165g5m, a 5 GigE model. The same format at 30 fps is
553 Mbit/s and fits on 1 GbE with room to spare, so if 30 fps is enough for what
you are filming, gigabit cameras are enough. Cutting resolution is a weaker
lever than it looks: 1280x1024 mono8 at 100 fps is 1.05 Gbit/s, still over
1 GbE line rate. Frame rate moves this number the furthest.

**Port speed and cameras per port.** Add up the cameras sharing a host port and
keep the total comfortably under line rate. Three 1920x1200 mono8 cameras at
100 fps is 5.53 Gbit/s, so that group needs a 10 GbE port and still leaves about
45% headroom. A 1 GbE host port fits exactly one camera at 30 fps: two are
already 1.1 Gbit/s, over line rate before a single header. The rule is

```
host ports = ceil(n_cameras / cameras that fit on one port)
```

Six 100 fps cameras at three per 10 GbE port is two host ports, and three
cameras need a switch in between. That is how the reference rig is built: six
cameras in two groups of three, each group behind its own switch, each switch
uplinked to its own 10 GbE port on the host. Nine cameras would be three groups,
three switches and three ports.

Two things about those switches are easy to overlook. Their access ports have to
run at the camera's own link speed, not just the uplink; a switch with 1 GbE
access ports throttles a 5 GigE camera to 1 GbE however fast its uplink is. And
every switch is one more device that has to pass jumbo frames: one left at the
default 1500-byte MTU silently discards every frame from the cameras behind it.

**Jumbo frames.** The reference camera settings file sets
`GevSCPSPacketSize 9000`, so each GVSP packet carries 9000 bytes. Every device
in the path, NIC and any switch, must accept an MTU of 9014 bytes (9000 payload
plus headers), or those packets are dropped. Set the adapter's *Jumbo Packet*
property to 9014 and its *Receive Buffers* to the maximum.

**Inter-packet delay.** `GevSCPD` (10000 in the reference settings file) spaces
out one camera's packets so cameras sharing a port do not burst into each other;
at zero they collide and the switch drops what it cannot forward. The right
value depends on cameras per port, link speed and frame size, so re-tune it when
any of those change rather than copying it.

**Cabling.** A 10 GbE copper run needs Cat6a or better, and a marginal cable
shows up as packet loss rather than a link that refuses to come up.

### CPU

Sizing the CPU is about meeting a deadline: every camera's grab loop has to
finish with a frame before the next one arrives. Three things consume CPU, and
only the first two are visible inside the application:

1. **One grab thread per camera.** Per frame it retrieves the driver buffer,
   copies the grey plane into a buffer from the NV12 ring (NV12 is the pixel
   layout the GPU encoder consumes: a full-resolution grey plane followed by a
   half-size colour plane), hands that buffer to the encoder, and releases the
   driver buffer. At 1920x1200 that measures about 0.8 ms per frame per camera,
   roughly 8% of one core at 100 fps. The whole iteration must finish inside one
   frame period (10 ms at 100 fps). When it does not, nothing errors: the driver
   buffer pool absorbs the deficit and every retrieved frame is a little staler
   than the last, until the pool runs out.
2. **One encoder thread per camera.** Compression happens on NVENC, the
   dedicated video encoder built into NVIDIA GPUs, so these threads mostly move
   bytes.
3. **GigE packet reassembly**, in the network stack rather than the app. With
   `gige_driver: socket` the packet resends run in user space; that costs more
   host CPU than the in-kernel driver but recovers lost packets instead of
   discarding the frame. Each port's receive work runs as deferred procedure
   calls, and if the adapter spreads that work over only one
   receive-side-scaling (RSS) queue, a single core carries the entire port.
   Three 1920x1200 cameras at 100 fps is about 78,000 packets/s per port,
   measured at 46% of a single core against a 4% average across 24 cores.

**How it scales.** Threads grow at two per camera plus the UI, about 13 busy
threads at six cameras. All share the one GIL, so the binding constraint is
GIL-held work per thread per frame rather than core count: up to about 300 µs
per thread per frame is safe even at 17 threads, while ~1000 µs breaks the 10 ms
budget at 11. Cores beyond the grab and encode threads mostly help the network
stack.

The startup check asks one question about the CPU: it warns below 4 physical
cores, a floor for running the application rather than a verdict on the rig you
are sizing.

### RAM

These buffers are allocated up front, so they either fit or the recording does
not start. Memory is dominated by two allocations, both linear in camera count.

By default the application runs in **kick-out mode**: a trigger's frames are
held rather than encoded until every camera has delivered that same trigger, so
the videos come out aligned with no post-processing. Holding
costs memory, and `kick_max_lag`, a profile field covered in step 7, is how many
frames of holding are allowed before a straggler's missing triggers are given up
on. It sizes the ring, so it sets the RAM bill.

```
driver buffer pool = n_cams x MaxNumBuffer x frame_bytes
NV12 ring          = n_cams x (kick_max_lag + ENCODE_QUEUE_DEPTH + 64)
                            x 1.5 x frame_bytes
```

Pool depth is the profile field **`max_num_buffer`**, covered in step 7. That
name is the one to edit: `MAX_NUM_BUFFER = 1000` in `gui_app/camera_manager.py`
is only the fallback for a caller that passes no depth, and `MaxNumBuffer` is
pylon's own node name. The shipped profile sets 600, six seconds of slack at
100 fps. `ENCODE_QUEUE_DEPTH = 200` (`gui_app/grab_thread.py`). The factor 1.5
is NV12: a full-size luma plane plus a half-size chroma plane held at a constant
128. The `+ 64` is spare slots so a buffer cannot be reused while still in
flight. The ring exists only in real-time encode mode; in kick-out mode it is
sized as above, otherwise `ENCODE_QUEUE_DEPTH + 4` buffers per camera.

Worked example at the shipped settings, `max_num_buffer: 600` and
`kick_max_lag: 480`, 6 cameras at 1920x1200:

```
pool  = 6 x 600 x 2,304,000 B                    =  7.7 GiB
ring buffers per camera = 480 + 200 + 64         = 744
ring  = 6 x 744 x (1920 x 1800) B                = 14.4 GiB
total                                            = 22.1 GiB
```

The same settings at 9 cameras: 11.6 + 21.6 = **33.1 GiB**. Halving the cap to
`kick_max_lag: 240` does not halve the ring, because the queue depth and spare
slots stay put: 504 buffers instead of 744, or 1.62 GiB per camera, giving
17.4 GiB at 6 cameras and 26.2 GiB at 9. Keep the pool at or above
`kick_max_lag` whichever way you move it: a pool shallower than the coordinator's
patience empties before the coordinator gives up on a laggard, and a recoverable
lag becomes lost frames.

Those totals have to be *available* when Record is pressed, with the operating
system and the GUI on top, so budget roughly twice the pool-plus-ring figure for
your camera count, not a little more. The reference rig carries 63.4 GB against
a nine-camera demand of 33.1 GiB, so more than half the machine is buffers.

The application does this arithmetic at Record time, against the cameras
actually open, and **refuses to start if it will not fit** in available memory:

```
Not enough RAM for 9 cameras: 33.1 GiB needed (11.6 pylon pool + 21.6 NV12
ring), 31.2 GiB available. Lower `max_num_buffer` or `kick_max_lag` in the rig
profile, or close other applications.
```

Above 75% of available memory it warns and asks before proceeding.

Separately, the startup check warns below 16 GB of RAM in total, a floor for the
application to run at all. A 16 GB machine passes the launch check happily and
is then refused by every six-camera recording, 22.1 GiB being nowhere near it.
Size against your own figure, not the startup warning.

### GPU

H.264 compression runs on NVENC, the card's dedicated video encoder, while
capture is still going on. That is what keeps the disk requirement in the next
section sane. Get it wrong and the recording still happens, uncompressed, at a
rate few disks absorb.

You need an NVIDIA GPU with NVENC. Encoding runs through PyNvVideoCodec, and the
CUDA runtime arrives as a Python dependency (`nvidia-cuda-runtime-cu12`), so a
CUDA toolkit installation is not required.

A recording needs **one concurrent encode session per camera**. The driver caps
concurrent sessions, and that cap has moved across driver generations (2, then
3, 5, 8, 12), so it is probed rather than assumed: `nvenc.probe_max_sessions`,
called from `hardware_check.nvenc_session_capacity` before each recording. The
budget is one session per camera, plus `encode_parallel` for the remux jobs,
plus one warm-up session. If the grant is below the camera count, Record
refuses:

```
NVENC granted only 8 concurrent sessions but 9 cameras need one each. The
driver caps this. Cameras beyond the cap would silently fall back to raw.bin at
~129 GiB per 10 min each. Record fewer cameras, or set `realtime_encode: false`
in the rig profile to put every camera on the raw path deliberately.
```

On the reference rig the probe returns **12 sessions on an RTX 5080**, more than
the six cameras it runs and more than the nine it is being scaled towards, so a
current consumer card is not the binding constraint. The historic caps of 2 and
3 are why the number is probed: an older card really can grant fewer sessions
than you have cameras.

Ask a candidate GPU the same question before committing. It needs only uv, the
repository and `uv sync`, steps 1, 3 and 4, since this touches neither the
cameras nor the network:

```powershell
uv run python -c "from gui_app import nvenc; print(nvenc.probe_max_sessions())"
```

It prints a line about warming the encoder and then a single number: how many
concurrent sessions the driver granted. Anything at or above your intended
camera count is fine. `0` means NVENC is unusable on this machine, which puts
you in the raw fallback under *Disk* below, a very different disk budget.

With no working NVENC the startup check warns that `NVENC not found in ffmpeg —
there is no CPU fallback, so encoding raw.bin to mp4 and the post-hoc alignment
re-encode will FAIL`, and the capture path falls back to writing raw frames to
disk. Every mp4 writer asks for `h264_nvenc` by name, so such a machine captures
raw frames and then cannot turn them into video at all. (The real-time path's
`.h264` to mp4 step is a stream copy and needs no encoder, but that path needs
NVENC to have produced the `.h264` in the first place.) ffmpeg is bundled with
the Python dependencies (`imageio-ffmpeg`); nothing to install separately.

### Disk

Storage has two answers, decided by one profile switch, `realtime_encode`, and
they are not in the same league. Know which mode you will run before you buy
drives.

**Real-time GPU encode (the default).** Frames are compressed as they arrive and
only H.264 reaches the disk. At qp 21 that is about 4.6 KB per frame:

```
6 x 100 x 4600 B = 2.8 MB/s  ~= 10 GB per hour
```

A session is a few GB, and any ordinary drive keeps up.

**Raw fallback (`realtime_encode: false`).** Use this when the GPU cannot hold
one live encode session per camera, or the real-time path is misbehaving: every
frame is written to disk uncompressed and encoded afterwards. It still needs
NVENC, because the post-hoc pass also encodes with `h264_nvenc`, so it is a way
around a shortage of concurrent sessions, not around a card with no encoder. The
rate is the full sensor payload, the number the network section arrived at:

```
n_cams x fps x frame_bytes
6 x 100 x 2,304,000 B = 1.38 GB/s   (83 GB per minute)
9 x 100 x 2,304,000 B = 2.07 GB/s
```

Above 1.5 GiB/s the preflight advises spreading the output across drives,
because a single consumer NVMe drops to roughly 1.6 GB/s once its SLC cache is
exhausted. Startup warns below 500 GB free, and below 500 MB/s measured write
speed (it writes and deletes a 16 MB file to find out).

### Operating system

Windows, today, but the dependency is shallow. The Windows-specific pieces are
`PylonGigEConfigurator` and the inbound firewall rule, the `configure_nic.ps1`
and `make_shortcut.ps1` scripts, and the two performance probes
`probe_gil_wait.py` and `probe_native_cpu.py`. The capture path does not depend
on them: the binary-file flag is resolved with `getattr(os, "O_BINARY", 0)`, the
`subprocess.STARTUPINFO` use in `encode_worker.py` is guarded by `sys.platform`,
the serial port is a profile field, and `arduino-cli` is found via PATH or an
environment variable. NVENC and pypylon both support Linux.

### The reference rig, for comparison

Every performance figure in these pages was measured on this configuration. Read
it as one known-good point to check your own sums against, not as a shopping
list.

| Part | What the reference rig has |
|---|---|
| CPU | Intel Ultra 9 285K, 24 cores (8 performance + 16 efficiency) |
| RAM | 63.4 GB |
| GPU | NVIDIA RTX 5080, 12 concurrent NVENC sessions measured |
| Cameras | 6x Basler a2A1920-165g5m (5 GigE), 1920x1200 mono8 at 100 fps |
| Network | 2 switches, 3 cameras each, one 10 GbE host port per switch |
| Trigger board | Arduino Mega 2560 on `COM3`, one pin per camera |
| OS | Windows |

On that machine a 60-second six-camera run at 100 fps captured 100.00% of
triggers; section 3 quotes the rest of that run's numbers.

---

## 2. Install the software

The ten steps build four things in order: a Python environment that can talk to
cameras (steps 1 to 4), a network the cameras can stream over (step 5), the two
files that describe your rig (steps 6 and 7), and a trigger board carrying
firmware you can account for (step 8). Step 9 starts it all for the first time,
step 10 turns that into a double-click.

Do them in order; each ends with something you can check.

Everything in this section is typed into PowerShell.

**Opening PowerShell:** press the Windows key, type `powershell`, press Enter. A
window opens with a prompt like `PS C:\Users\you>`. Type a command, press Enter,
wait for the prompt to come back. Right-click pastes.

**Opening it as Administrator** (needed in step 5): press the Windows key, type
`powershell`, right-click *Windows PowerShell* in the results, choose *Run as
administrator*. The window title will say *Administrator*.

### Step 1 — install uv

uv manages the Python version and every package, so nothing is installed into a
system Python and conda is not involved. Installation instructions are at
<https://docs.astral.sh/uv/getting-started/installation/>; the Windows command
given there is:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close PowerShell and open it again (the installer adds uv to PATH, and only new
windows see the change), then check:

```powershell
uv --version
```

Expected: one line starting with `uv` and a version number, then a build hash
and the platform. The exact version does not matter. If you get
`uv : The term 'uv' is not recognized`, the new window did not pick up PATH;
sign out and back in.

### Step 2 — install the Basler pylon SDK

pypylon is a thin binding onto Basler's own SDK, so the SDK has to be there
first. Download it from
<https://www.baslerweb.com/en/downloads/software-downloads/> and install it
**before** the Python dependencies. It provides the camera driver, **pylon
Viewer** (step 6) and **PylonGigEConfigurator** (step 5).

Check it landed:

```powershell
Test-Path "C:\Program Files\Basler\pylon\Runtime\x64\PylonGigEConfigurator.exe"
```

Expected: `True`. If it prints `False`, find the install directory and use that
path in step 5 instead.

### Step 3 — get the code

Clone the repository. It lives wherever you like; the Desktop is used throughout
this page only to keep the example paths short.

```powershell
cd $HOME\Desktop
git clone https://github.com/talmolab/panopticon.git
cd panopticon
```

Expected output ends with something like:

```
Cloning into 'panopticon'...
remote: Enumerating objects: ...
Receiving objects: 100% ...
Resolving deltas: 100% ...
```

The repository has no submodules, so a plain clone is complete.

If `git` is not recognized, install Git for Windows from
<https://git-scm.com/download/win> and reopen PowerShell.

Every later command assumes the prompt is inside the repository
(`PS C:\Users\you\Desktop\panopticon>`). To get back there in a new window:
`cd $HOME\Desktop\panopticon`.

### Step 4 — install the Python dependencies

One command reads `pyproject.toml` and builds the environment:

```powershell
uv sync
```

The first run downloads a Python interpreter and about two dozen packages, and
ends with a line like `Installed 22 packages in 42s`. Later runs are instant:

```
Resolved 26 packages in 1ms
warning: Skipping installation of entry points (`project.scripts`) for package
`panopticon` because this project is not packaged; ...
Checked 22 packages in 0.82ms
```

That warning is normal. The command creates a `.venv` folder in the repository
holding the interpreter and all packages; nothing is installed system-wide.

Check the important imports:

```powershell
uv run python -c "import pypylon.pylon, PyQt5, numpy; print('ok')"
```

Expected: `ok`. Anything else means the sync did not complete; run `uv sync`
again and read the error.

This step and the clone are the only ones that need internet. Acquisition,
encoding and calibration all run offline.

### Step 5 — put the cameras on the network (GigE)

Four things have to be true before a GigE Vision camera will stream. It and its
adapter need addresses on the same subnet, or the camera never appears. **Every
switch between them has to pass jumbo frames**, or the camera is discovered and
delivers nothing. Windows has to let its traffic reach the application. And the
adapter has to be configured for the traffic the camera sends.

Each of those fails differently, and only the first is obvious.

#### The addressing scheme

Give **each switch its own /24**, with the host adapter always at `.2` and the
cameras from `.3` upward. One subnet per switch means an address tells you which
switch a camera is on, and a camera plugged into the wrong switch stops working
loudly instead of half-working.

The reference rig, nine cameras across three switches:

| Subnet | Host adapter | Switch management | Cameras |
|---|---|---|---|
| `192.168.3.0/24` | Ethernet 4 → `.2` | `.250` | `.3` `.4` `.5` |
| `192.168.4.0/24` | Ethernet 5 → `.2` | `.250` | `.3` `.4` `.5` |
| `192.168.5.0/24` | Ethernet 3 → `.2` | `.250` | `.3` `.4` `.5` |

Camera *names* do not come from addresses — `cam1`..`camN` are assigned by
serial-number order (Step 7), independently of which switch a camera sits on. Keep
the two in sync anyway; a rig where `cam5` is at `192.168.3.5` is much easier to
reason about at 2 a.m.

If you have a single switch and no wish to plan, pylon can do the whole thing:

```powershell
& "C:\Program Files\Basler\pylon\Runtime\x64\PylonGigEConfigurator.exe" auto-all
```

That assigns compatible addresses to every adapter and camera it finds. It is
fine for one switch. It does **not** configure switches, and it gives you no
say in which camera lands where, so plan the addressing by hand once you have
more than one segment.

#### Configure the switches

**This is the step most likely to be skipped, and it fails silently.** A managed
switch left at its default 1500-byte MTU discards every GVSP data packet while
link lights, enumeration, pylon Viewer's device list and ICMP all look perfectly
healthy. The cameras appear and deliver nothing.

Set the **maximum frame size to at least 9014** on every port in use, **including
the uplink to the host**. A jumbo-capable access port behind a 1500-byte uplink
still fails.

Set **flow control to `symmetric` on every port in use, including the uplink.**
This is the single highest-impact switch setting on this rig and it is off by
default on most managed switches. Measured on three otherwise identically
configured switches, same model, same firmware, same ports, nine cameras, 90 s:

| flow control | GVSP resend requests per camera | worst per-camera lag |
|---|---|---|
| **symmetric** | **8-10** | 1-4 frames |
| disabled | **16,700-16,900** | 5-12 frames |

That is a **1,100x** difference, and the three cameras behind the switch we
changed went from the worst in the rig to the best. The mechanism: several
cameras burst simultaneously into one uplink, and 802.3x PAUSE lets the switch
ask a camera to hold off for microseconds while its egress queue drains. Without
it the switch's only option is to discard the frame, which becomes a GVSP resend,
which completes that camera's buffer late — indistinguishable from a slow camera.

The tell is in the stream counters the grab threads print at the end of every
recording: `Resend_Request_Count` in the thousands while
`Buffer_Underrun_Count` stays at 0 means loss in the **network**, not starvation
in the host, and flow control is the first thing to check.

Also check **which physical ports you used.** Multi-gigabit switches commonly
split their ports into speed blocks — the reference rig's are four 100M/1G/2.5G
ports plus four 1/2.5/5/10G ports — and the faster block is usually the higher
numbers. A camera that negotiates 2.5 Gbit/s where its siblings get 5 Gbit/s is
not an error anywhere; it simply has less headroom for retransmission, and that
shows up as resend requests under load rather than as a failure. Put every camera
*and* the host uplink in the fastest block, and verify the **negotiated** speed
in the switch UI (*Switching -> Ports*) rather than assuming it from the label.

Worked example, NETGEAR MS510TXM (the reference rig's switches; other managed
switches differ only in menu names):

1. A factory switch is a DHCP client and falls back to **`192.168.0.239` /
   `255.255.255.0`** when no DHCP server answers — which is the normal case on an
   isolated camera segment. To reach it, temporarily give that adapter an address
   in its subnet, from an Administrator PowerShell:
   ```powershell
   New-NetIPAddress -InterfaceAlias "Ethernet 3" -IPAddress 192.168.0.100 -PrefixLength 24
   ```
2. Browse to `http://192.168.0.239`, user `admin`. First login forces you to set a
   password. **Record it** — there is no recovery other than a factory reset
   (reset button, ~10 s), which returns the switch to `192.168.0.239`.
3. *Switching → Ports → Port Configuration* → set **Maximum Frame Size** to 9216
   on all ports in use. The label varies by firmware: look for "Frame Size",
   "MTU" or "Jumbo".
   On the same page set **Flow Control** to **Symmetric** on every port in use,
   including the uplink. See the measurement above; this one setting is worth
   three orders of magnitude in resend requests.
4. *System → Management → IP Configuration* → set the protocol to **Static**
   (the fields are often greyed out until you do), address `192.168.5.250`, mask
   `255.255.255.0`, gateway `0.0.0.0`. An isolated camera segment has no router,
   and the factory gateway points at a device that does not exist. Applying this
   drops your browser session — expected, the switch has just moved subnet.
5. Remove the temporary address and give the adapter its real one:
   ```powershell
   Remove-NetIPAddress -InterfaceAlias "Ethernet 3" -IPAddress 192.168.0.100 -Confirm:$false
   New-NetIPAddress   -InterfaceAlias "Ethernet 3" -IPAddress 192.168.5.2 -PrefixLength 24
   Set-NetIPInterface -InterfaceAlias "Ethernet 3" -Dhcp Disabled
   ```
6. Reconnect on the new address and confirm the frame-size setting survived. Some
   firmware needs an explicit *Maintenance → Save Configuration* before a reboot.

Repeat per switch, one subnet each. Do not put a `192.168.0.x` address on more
than one camera adapter at a time, or Windows will have two routes to that subnet
and choose one arbitrarily.

Give every switch a management address on **its own camera subnet** (step 4
above), not on a shared one. Each then stays reachable with no temporary address,
and because the subnets differ you can have all of their web UIs open at once to
compare settings — which you will want to do the first time one switch behaves
differently from another.

##### Finding a switch whose address you have lost

Sweep the camera subnet and look for a live host that is neither the adapter nor
a camera. No temporary address is needed, because you are already on that subnet:

```bash
for i in $(seq 1 254); do
  ( ping -n 1 -w 250 "192.168.5.$i" | grep -qa "TTL=" && echo "ALIVE 192.168.5.$i" ) &
  (( i % 64 == 0 )) && wait
done; wait
```

Then tell switch from camera by MAC prefix — `arp -a` after the sweep. Basler
cameras are `00-30-53-*`; your switch vendor has its own OUI. Confirm with
`curl -s -o /dev/null -w '%{http_code}' http://<ip>/`, which answers `200` for a
web UI.

Do **not** bother with vendor layer-2 discovery protocols. NETGEAR's NSDP (UDP
63321/63322) is unimplemented on the Smart Managed Pro line: on the reference rig
it returned zero replies even from a switch that was answering HTTP on the same
segment at that moment, and with an inbound firewall rule explicitly allowing it.
If the subnet sweep fails, the switch has no management address on that segment
at all, and your options are the vendor's discovery utility or a factory reset.

#### Configure the host adapters

Device Manager (Windows key, type `device manager`), *Network adapters*,
right-click the camera port, *Properties*, *Advanced* tab. Or PowerShell, which
is easier to repeat across ports:

```powershell
Set-NetAdapterAdvancedProperty -Name "Ethernet 3" -DisplayName "Jumbo Packet" -DisplayValue "9014 Bytes"
Set-NetAdapterAdvancedProperty -Name "Ethernet 3" -DisplayName "Receive Buffers" -DisplayValue 4096
Set-NetAdapterAdvancedProperty -Name "Ethernet 3" -DisplayName "Energy Efficient Ethernet" -DisplayValue "Disabled"
Set-NetAdapterAdvancedProperty -Name "Ethernet 3" -DisplayName "Interrupt Moderation" -DisplayValue "Disabled"
```

| Property | Value | Why |
|---|---|---|
| Jumbo Packet | 9014 bytes | Matches the camera's `GevSCPSPacketSize 9000` |
| Receive Buffers | the driver's maximum (4096 here) | Absorbs receive bursts |
| Energy Efficient Ethernet | **Disabled** | Not cosmetic. EEE caused a catastrophic stall on this rig: worst frame gap 471, resend requests ~100,000. Disabling it cut those to 128 and ~4,000 |
| Interrupt Moderation | Disabled | Measured no improvement here, but keeps ports comparable |

These are per-port, and a **freshly added port does not inherit them** — EEE and
interrupt moderation in particular default the wrong way. Set them on every
camera port, not just the first two.

Optionally spread each port's receive processing across cores:

```powershell
powershell -ExecutionPolicy Bypass -File configure_nic.ps1 -Ports "Ethernet 3","Ethernet 4","Ethernet 5"
```

Run it elevated. It prints BEFORE and AFTER tables of `NumberOfReceiveQueues`. A
driver that only applies RSS to TCP accepts the call and keeps fewer queues,
reported as `NOT APPLIED on: ...`. On the reference rig this changed nothing
measurable (DPC stayed at ~46% on the same two cores), so treat it as a
maybe rather than a requirement. Applying it resets the adapters, so cameras
disappear and re-enumerate over a few seconds. Never run it during a recording.

#### Give the cameras their addresses

Run the pylon **IP Configurator** (`C:\Program Files\Basler\pylon\Applications\x64\bin\ipconfigurator.exe`)
as Administrator, select each camera, and set a **static/persistent** address
from the plan above with mask `255.255.255.0` and gateway `0.0.0.0`. Turn DHCP
off so the camera does not spend every boot waiting for a server that is not
there.

Order matters: a camera keeps its address across reboots, so if you change an
adapter's subnet first, the cameras behind it become unreachable until they are
renumbered too. The IP Configurator can still reach them — it addresses cameras
by MAC over broadcast, not by IP — which is what makes it the right tool for a
camera stranded on the wrong subnet.

The same operation from Python, useful for scripting a rebuild:

```python
tl = pylon.TlFactory.GetInstance().CreateTl("BaslerGigE")
tl.BroadcastIpConfiguration(mac, True, False, "192.168.5.3", "255.255.255.0", "0.0.0.0", "")
tl.RestartIpConfiguration(mac)          # applies without a power cycle
```

#### Let the traffic through the firewall

Discovery and streaming are
UDP, and Windows blocks inbound UDP to an unknown program by default. Replace
the path with your own:

```powershell
New-NetFirewallRule -DisplayName "PanopticonGigE" -Direction Inbound -Action Allow -Protocol UDP -Program "C:\Users\you\Desktop\panopticon\.venv\Scripts\python.exe"
New-NetFirewallRule -DisplayName "PanopticonGigE-w" -Direction Inbound -Action Allow -Protocol UDP -Program "C:\Users\you\Desktop\panopticon\.venv\Scripts\pythonw.exe"
```

Each command echoes the rule it created, ending with `Enabled : True`. Two rules
are needed because a program-scoped rule matches one executable: `uv run gui.py`
runs `python.exe`, while the desktop shortcut and `_launch.bat` run
`pythonw.exe`.

#### Verify it, because two of these failures are silent

```
uv run probe_network.py --sweep
```

This is the acceptance test for the whole step. It broadcasts a GigE Vision
discovery request from every host adapter and then, for each camera, sweeps
`GevSCPSPacketSize` upward while grabbing real frames.

What it tells you that nothing else does:

- **Which switch each camera is physically plugged into.** Discovery is answered
  by every camera on the segment whatever address it holds, so the adapter that
  hears a camera is the switch it is on. The tool flags any camera whose address
  is outside that adapter's subnet.
- **Whether the path carries 9000-byte packets.** Healthy output is
  `complete=10/10` at every size up to 9000. A clean cutoff — everything passing
  at 1500 and everything failing from 2000 up — is a switch still at the default
  MTU.

> **Do not test jumbo frames with `ping`.** These cameras answer only very small
> ICMP echoes, so `ping -f -l 8972` fails against a camera on a *known-good*
> jumbo path, and even `-l 1472` fails. A ping-based test reports every switch as
> broken and will send you chasing a fault that is not there. The only valid test
> is a real grab at the target packet size, which is what `--sweep` does.

Then open pylon Viewer from the Start menu. Every camera should be listed, and
each should open and show live video.

### Step 6 — make the camera settings file (.pfs)

Steps 6 and 7 write the two files that describe your rig, so be clear which
settings live where. Every setting has one of four homes. The **camera settings
file** (`.pfs`) dumps the cameras' own registers: exposure, gain, resolution,
pixel format, GigE transport parameters. The **rig profile** (a YAML file in
`profiles/`) is the application's own configuration: camera count, frame rate,
serial port, pins. The **board config** (`configs/boards/*.yaml`) describes the
printed calibration board and is read only by calibration. Everything else is a
**code constant**, changeable only by editing the source. The homes do not
overlap: the application never writes exposure or gain into the `.pfs`, and the
`.pfs` never overrides the profile.

A `.pfs`, pylon's name for a GenApi persistence file, is a plain-text list of
camera features loaded into every camera at open. **It is the only source of a
recording's exposure and gain.** The application does write both at every
acquisition start, but what it writes is the pair it read back from this file
when the camera opened, capped at the exposure ceiling below. Build it in pylon
Viewer with one camera open, save it once, reuse it for all cameras.

The settings that matter, and why:

| Feature | Reference value | Why |
|---|---|---|
| `PixelFormat` | `Mono8` | The capture path assumes 8 bits. Anything wider is truncated mod 256 with no error, so opening refuses with `PixelFormat is Mono12, not Mono8`. |
| `Width` / `Height` | 1920 / 1200 | Must match the profile and be identical on every camera; opening refuses if camera 2 disagrees with camera 1. |
| `ExposureAuto` | `Off` | Auto exposure drifts between cameras and can wander past the ceiling below. |
| `GainAuto` | `Off` | Same reason. |
| `ExposureTime` | 3000 µs | Subject to the ceiling below. |
| `Gain` | 6.0 dB | Each +6 dB doubles signal, and noise with it. |
| `AcquisitionFrameRate` | 165 | Sets the exposure ceiling. See below. |
| `AcquisitionFrameRateEnable` | 1 | Leave enabled. |
| `GevSCPSPacketSize` | 9000 | Jumbo packets. Only if the NIC and switches pass an MTU of 9014. |
| `GevSCPD` | 10000 | Inter-packet delay. Spaces one camera's packets so cameras sharing a port do not collide; 0 collides. |

**The exposure ceiling.** In triggered mode the camera's frame-rate timer starts
*after* exposure ends, so the minimum interval between frames is
`exposure + 1/AcquisitionFrameRate`. With the rate limiter at 165 Hz, 6.06 ms of
a 100 fps trigger period is already spent, leaving about 3.94 ms for exposure.
Exceed it and the camera skips every second trigger: 50 fps and no error message
anywhere. The application computes the ceiling from the profile's
`trigger_rate_limit`, clamps to 90% of it (about 3.5 ms at 100 fps), and logs
`CLAMPED from ...` when it does. For more light, add illumination first, then
exposure, then gain.

**Getting enough light without overdoing it.** The reference values come from
measurement. They were raised to 3000 µs and 6.0 dB from 2000 µs and 0 dB on
2026-08-11, because the older pair left 65% of pixels in levels 0–15 with
**21.5% clipped at exactly 0**, destroyed at the converter and unrecoverable
however much you brighten the video afterwards. Overshooting is just as easy:
that 3.0x increase lands about 4% of pixels saturated, where 7x would clip
12.7%. Hence the order illumination, exposure, gain: infrared light is real
photons and better signal-to-noise, while gain amplifies the noise with it.

Judge any change against a real recording, never the live preview. The preview
free-runs at 30 fps, where the 33 ms period has room for almost any exposure, so
a setting over the ceiling looks healthy there and only halves the frame rate
once the cameras are triggered.

Do not set `trigger_rate_limit: 0`. Turning the limiter off does remove the
exposure ceiling, and it costs 8–15% of frames in transmission: the limiter also
paces sensor readout across 6.06 ms, and without it every camera bursts onto the
link immediately after the shared trigger.

Steps in pylon Viewer:

1. Open one camera.
2. Set the features above in the feature tree (use the search box).
3. **File > Save Features**, and save into the repository as
   `configs/<descriptive_name>.pfs`, for example `configs/mono8_1920x1200.pfs`.

Check the result in Notepad. It is one `Feature<TAB>value` per line. Confirm
`Width`, `Height`, `PixelFormat`, `ExposureTime`, `Gain`, `GevSCPSPacketSize`
and `GevSCPD` read what you expect.

A `.pfs` saved from a different camera of the same model is fine. It loads with
validation disabled, and geometry and pixel format are read back from each
camera afterwards and checked against each other.

### Step 7 — write the rig profile

The `.pfs` describes the cameras. The profile describes everything else, and it
is where the numbers from section 1 become configuration.

A profile is one YAML file in `profiles/`. Every `.yaml` there appears in the
sidebar dropdown under its `name`. Copy `profiles/3dpose.yaml` to
`profiles/my_rig.yaml` and edit it.

```yaml
name: my_rig                 # what the dropdown shows
frame_width: 1920            # must match the .pfs Width
frame_height: 1200           # must match the .pfs Height
frame_rate: 100              # trigger rate for recordings, Hz
calibration_frame_rate: 30   # trigger rate for calibration captures
quality: 21                  # NVENC constant quantizer; lower = better, bigger
encode_parallel: 3           # concurrent remux / post-hoc encode jobs
realtime_encode: true        # GPU H.264 during capture; false = raw fallback
realtime_kick: true          # release a trigger to the encoders only once every
                             # camera has caught it, so videos come out aligned
kick_max_lag: 480            # frames of cross-camera lag tolerated; the NV12
                             # ring scales with this
gige_driver: socket          # socket | filter | auto
trigger_rate_limit: 165      # AcquisitionFrameRate applied in trigger mode

pfs_path: "configs/mono8_1920x1200.pfs"
output_dir: "data"
board_config: "configs/boards/charuco_8x8_15mm.yaml"

serial_port: COM3                    # the trigger board's port
trigger_pins: [2, 4, 6, 8, 10, 12]   # one output pin per camera
n_cameras: 6                         # refuse to start unless exactly this many

stim_safe_pins: [53]                 # YOUR stim pins, forced LOW from the
                                     # instant the sketch boots; [] if none
calibration_exposure_us: 15000       # calibration-only exposure; 0 = keep .pfs
calibration_gain_db: -1              # calibration-only gain; -1 = keep .pfs
```

Field by field. The middle column is what the application uses when the field is
absent, which is not always what the reference rig runs; the paragraphs after
the table sort out where those two part company.

| Field | If the field is omitted | What it does |
|---|---|---|
| `name` | file stem | Label in the profile dropdown. |
| `frame_width`, `frame_height` | 1920, 1200 | Frame geometry. Must match what the cameras report after the `.pfs` loads. |
| `frame_rate` | 100 | Trigger rate for recordings. Sets the frame period the grab loop must keep up with, and the H.264 GOP length: the spacing of keyframes a player can start decoding from, one per second here. |
| `calibration_frame_rate` | 30 | Trigger rate for calibration captures. A slowly waved board gains nothing from 100 fps, and the longer period raises the exposure ceiling from about 3.94 ms to about 27 ms (3.5 ms and 24.5 ms after the 90% clamp), which is why `calibration_exposure_us: 15000` is safe. |
| `quality` | 21 | NVENC constant quantiser. |
| `encode_parallel` | 3 | Concurrent encode/remux jobs after a recording. Counts against the NVENC session budget. |
| `realtime_encode` | `true` | GPU H.264 during capture. `false` writes raw frames and encodes afterwards, at the raw disk rate from section 1 (1.38 GB/s for six cameras at 100 fps, against 2.8 MB/s encoded). |
| `realtime_kick` | `false`, selecting post-hoc alignment instead | Gate frames through the cross-camera coordinator during capture, so the videos are trigger-aligned with no post-hoc re-encode. With it off, alignment runs after encoding and re-encodes each video. The shipped `3dpose` profile sets `true`; kick-out is the mode the rest of this documentation describes. |
| `kick_max_lag` | 240; the shipped `3dpose` profile sets 480 | How many frames one camera may lag the others before its missing triggers are force-dropped. Drives the NV12 ring size, so it is the main RAM lever; see *Choosing `kick_max_lag`* below. |
| `gige_driver` | `socket` | `socket` is user-space with reliable packet resends. `filter` is the in-kernel driver: less CPU, but with default resend settings it discards a frame rather than asking for the lost packet again, measured dropping about 23% of frames under six cameras at 100 fps on 2026-06-12. `auto` leaves pylon's default. |
| `trigger_rate_limit` | 165, the reference camera's own maximum frame rate rather than a property of Panopticon | `AcquisitionFrameRate` written in trigger mode. Set it to *your* camera's maximum frame rate; 165 is that number for the reference a2A1920-165g5m. Keep it above the trigger rate, and never set it to `0`. See *Setting `trigger_rate_limit`* below. |
| `thermal_poll_s` | 20.0; `0` disables | Seconds between camera temperature checks while acquiring. A camera that reaches its shutdown temperature stops delivering mid-session, so the GUI warns in the status bar and in `WARNINGS.txt` while there is still time to act. Every threshold is read from the camera itself (`BslTemperatureStatus`, `BsliOverTemperature`), so nothing here assumes a particular model. The read is a GVCP register access per camera — cheap at this interval, never put it on a per-frame path. |
| `pfs_path` | — | The camera settings file from step 6. |
| `output_dir` | — | Where recordings go. The sidebar's directory button overrides it per machine. |
| `board_config` | — | The printed ChArUco board, in `configs/boards/*.yaml`. The shipped `charuco_8x8_15mm.yaml` describes the reference rig's own board, `board_legacy: true` and all, so it is the wrong starting point for a freshly printed one; see *Calibration* in section 4. Measure your board and correct `square_length`: it sets the world scale of the solve, so a wrong value scales every 3D coordinate downstream. |
| `serial_port` | `COM3`, the reference rig's port rather than a sensible fallback | The trigger board's serial port. Step 8 shows how to find yours. |
| `trigger_pins` | `[2, 4, 6, 8, 10, 12]`, the reference rig's wiring rather than a sensible fallback | One output pin per camera, each wired to that camera's `Line1`; see *Wiring the trigger line* in section 1. Also refused as stimulation pins, since extra edges on one camera would break alignment. |
| `n_cameras` | 0, a code fallback nobody should keep | Refuse to start unless exactly this many cameras enumerate. `0` disables the check. Camera names are positional by serial-number order, so a camera that fails to enumerate renames every camera after it and attaches the calibration extrinsics to the wrong physical cameras. Set it. |
| `stim_safe_pins` | `[53]`, the reference rig's laser pin, which is no protection at all on a rig wired differently | **Set this to the pin or pins your own stimulus hardware is wired to**; `[]` if you have none. They go LOW in the first statement of the sketch's `setup()`, before the serial handshake. `setup()` blocks on that handshake until the GUI connects, so a pin not listed here floats for the whole wait, and a powered laser driver reads floating as ON. Pins a loaded paradigm uses are added automatically, so this list is the floor protecting the recording-only sketch, the one flashed at launch when no paradigm is loaded. |
| `calibration_exposure_us` | 0.0 | Exposure for calibration captures only; the `.pfs` values return for recordings. `0` keeps the `.pfs` value. The binding limit here is motion blur rather than the ceiling: at 15 ms a briskly waved board smears and its corners stop resolving, so move it slowly and pause at each pose. |
| `calibration_gain_db` | -1.0 | Same for gain. `-1` keeps the `.pfs` value. |

Paths may be relative to the repository root or absolute.

**Setting `trigger_rate_limit`.** A higher limiter shrinks the `1/rate` floor and
so buys exposure headroom (step 6). It is the knob to reach for when frames come
out dark. Above the camera's own maximum it buys nothing: the camera clamps it
silently, and the ceiling the software computes then exceeds the real one. That
is the condition behind *The recording looks fine but the views are out of sync*
in section 4.

**What the shipped profiles deliberately override.** Copying
`profiles/3dpose.yaml` gets all of this right without thinking. Writing a
minimal profile from scratch does not, because three of the fallbacks above are
not what the reference rig runs: the shipped profile sets `realtime_kick: true`,
`kick_max_lag: 480` and `n_cameras: 6`. Real-time kick-out, the mode the rest of
this documentation describes and the one the RAM arithmetic in section 1
assumes, is on because the profile says so, not because the field is optional.
Leave it out and you silently get post-hoc alignment with a re-encode instead,
and nothing will tell you.

**Choosing `kick_max_lag`.** This trades RAM against frames, and both directions
have cost frames on a real rig, so it is not a free dial. The ring grows
linearly with the cap, a cap far above the lag the rig actually shows buys
nothing, and a cap high enough to outgrow the machine starves capture outright.
Two rules hold whatever value you pick: `max_num_buffer` stays at or above
`kick_max_lag`, and a change is A/B'd on the rig rather than reasoned about.

The shipped value is 480, and the comment on `kick_max_lag` in
`profiles/3dpose.yaml` is the single source for what that number is worth here:
it records the measurements that raised it, the measurements that lowered it,
and the condition under which lowering it again would be safe. Read that comment
before changing the field, and size RAM for whatever it says.

### Step 8 — flash the trigger firmware

There is no `.ino` file for you to open and upload; the sketch is generated.
`gui_app/stim_compiler.py` compiles the combined camera-trigger and stimulation
sketch from the profile and the node graph, and `recording_only_sketch()` is
that same sketch with no stimulation in it: camera triggers plus the safe-pin
boot guard.

The application flashes it for you. At every launch it builds the recording-only
sketch, compares its SHA-256 against the last one it uploaded, and reflashes if
they differ. A stimulation paradigm lives in the board's flash memory, so it
survives closing the GUI, a power cycle and unplugging the USB cable, and
nothing can read it back over serial. The launch-time flash guarantees the board
carries no stimulation unless you applied one this session. On a rig with a
laser that is a safety property: you never have to trust your memory of last
week's upload.

One window is beyond the reach of any firmware. Flashing resets the board, and
while it waits in its bootloader no program is running, so every pin is
high-impedance, which a powered laser driver reads as ON and answers with a
visible flash. The safe-pin guard in `setup()` cannot help; it only runs once
the sketch is running. Nor is a pulldown resistor a general answer: a driver
input with a stiff internal pullup would need a resistor low enough to exceed
the board's per-pin current limit when the pin is driven high. The only hard
gate is the laser's own interlock. Fit one if you want that guarantee, and until
you do, key the laser off or block the beam before anything that flashes the
board. [WORKFLOW.md](WORKFLOW.md) sets out when a session does that.

So installing the firmware means wiring the pins, declaring them, installing the
compiler, and letting the application flash:

1. **Wire every camera's `Line1` to a board output**, with a common ground —
   one pin each, or several cameras fanned off one pin if the current budget
   allows. *Wiring the trigger line* in section 1 covers pin choice, ground,
   power, signal levels and the fan-out arithmetic; read it before cutting wire,
   because a camera on an undriven pin never triggers.
2. **Declare every driven pin in your profile.**
   ```yaml
   trigger_pins: [2, 4, 6, 8, 10, 12]   # use YOUR pin numbers
   n_cameras: 6                          # and YOUR camera count
   ```
   These two do **not** have to match, and nothing cross-checks them: the pin
   count equals the camera count only if you wired one pin each. What matters is
   that no camera sits on a pin missing from the list. The failure is quiet — a
   camera receiving no triggers delivers no frames, which in the default
   kick-out mode stalls every other camera until it is retired.
3. Install the **Arduino IDE** (which bundles `arduino-cli`) or `arduino-cli`
   standalone.
4. Install the core for your board once: `arduino-cli core install arduino:avr`
   for a Mega. A different board class needs its own core, and `FQBN` in
   `gui_app/stim_compiler.py` changed to match.
5. If `arduino-cli` lives somewhere unusual, set `PANOPTICON_ARDUINO_CLI` to its
   full path. The search order is `PANOPTICON_ARDUINO_CLI`, then PATH, then the
   bundled Arduino IDE locations.
6. Close the Arduino IDE's Serial Monitor. It holds the port, and both flashing
   and recording need it.

Find the board's port for `serial_port`: Device Manager > *Ports (COM & LPT)*, or

```powershell
[System.IO.Ports.SerialPort]::GetPortNames()
```

which prints e.g. `COM3`.

On the first launch after installing, the log shows:

```
[acq] board may carry a stim paradigm from a previous session — flashing the recording-only sketch
[acq] board flashed with the recording-only sketch; stim is off until you Apply one
```

Flashing takes roughly 30 seconds; the window shows *Clearing stim firmware...*
while it runs. On later launches it is skipped:

```
[acq] board already carries the recording-only sketch (no stim); skipping flash
```

Camera acquisition itself does not need `arduino-cli`, only this flash and the
Stimulation editor's Apply and Test. Without it, launch reports
`arduino-cli was not found` and lists everywhere it looked.

### Step 9 — first launch

Start it up. The console reports what it found, camera by camera, which is the
quickest way to confirm the previous eight steps landed.

```powershell
uv run gui.py
```

A splash panel reads *Panopticon / Loading cameras...*, then the main window
opens with one live preview pane per camera, free-running at about 30 fps.
[OVERVIEW.md](OVERVIEW.md) names the controls.

![The main window at idle, six live preview panes and the sidebar](images/main_idle.png)

The console shows one block per camera:

```
[startup] logging to C:\Users\you\Desktop\panopticon\logs\panopticon_20260903_191735.log
[acq] profile: 3dpose
[cam1] 41920544 1920x1200 Mono8
[cam1] extended (64-bit) block IDs: enabled
[cam1] GigE stream driver: SocketDriver (SocketBufferSize=262144 KB)
...
[grab0] StartGrabbing (recording=False)
[grab0] zero-copy view OK (PaddingX=0 PaddingY=0)
[acq] board already carries the recording-only sketch (no stim); skipping flash
[acq] opening teensy on COM3
```

The last two lines arrive about a second and a half after the window does. The
firmware check and the serial open are deferred so the window can paint and
Windows can finish registering the taskbar entry first.

What to check in that output:

- one `[camN] <serial> <width>x<height> Mono8` line per camera, with the
  geometry and format you configured;
- `zero-copy view OK (PaddingX=0 PaddingY=0)` for every camera;
- an `[hw] NVENC sessions:` count at least equal to your camera count. That line
  is not printed at launch: the session probe runs at the first Calibrate or
  Record, alongside `[nvenc] warmed (first-Encode import done
  single-threaded)`, so the first acquisition answers this. The probe asks for
  two more sessions than there are cameras and stops as soon as it gets them,
  which is why it reads `[hw] NVENC sessions: 8 (at least — probe stopped at its
  limit), needed 8` on the six-camera reference rig even though that card will
  grant 12 if asked for more.

Every launch writes the same text to `logs/panopticon_<date>_<time>.log`, which
is where to look when the app starts from the desktop shortcut and has no
console.

A *Hardware Check Results* dialog appears only if something is below the
recommended minimum (cores, RAM, free disk, disk write speed, NVENC), listing
what it found.

### Step 10 — desktop shortcut

Make the launch a double-click:

```powershell
powershell -ExecutionPolicy Bypass -File make_shortcut.ps1
```

Expected:

```
Shortcut written: C:\Users\you\Desktop\Panopticon.lnk
  Target : C:\Users\you\Desktop\panopticon\.venv\Scripts\pythonw.exe
  Args   : "C:\Users\you\Desktop\panopticon\gui.py"
  WorkDir: C:\Users\you\Desktop\panopticon
```

The shortcut points at the virtual environment's `pythonw.exe`, a GUI-subsystem
binary that Windows never gives a console, so launching it opens exactly one
window. It bypasses `uv run`, so it does not sync dependencies; run `uv sync`
yourself after `pyproject.toml` changes.

`_launch.bat` is the alternative: it goes through `uv run` and keeps a console
open for the life of the app, which is what you want when the app fails to start
and you need to see why.

If the script prints `No venv at ...`, run `uv sync` first.

### Adding cameras to a rig that already works

Scaling up is not a fresh install, and one of the steps below can silently
invalidate every calibration you have. In order:

1. **Check the serial-number order first, before buying or unboxing anything you
   can still change.** Cameras are named `cam1`..`camN` by serial-number order,
   and those names are baked into `calibration.toml`. New cameras keep your
   existing names only if their serials sort *after* every current one. If a new
   serial interleaves, every camera after it is renamed, old calibrations
   silently describe the wrong physical cameras, and triangulation produces a
   plausible wrong answer. `uv run probe_network.py` lists serials in the order
   the software will use. If they do interleave, you must recalibrate — and you
   should also rename or re-map your existing data.
2. **Work out whether you need another host port and switch.** The arithmetic is
   in *Network* in section 1: cameras per port is set by pixel rate, not by
   preference. Keeping the same cameras-per-port ratio as your working segments
   means the per-port load is unchanged and you are only adding host-side work.
3. **Give the new segment its own subnet**, following the scheme in Step 5:
   adapter at `.2`, cameras from `.3`, switch management at `.250`.
4. **Configure the new switch and the new adapter.** Both default the wrong way.
   The switch will be at a 1500-byte MTU, which delivers zero complete frames;
   the adapter will have Energy Efficient Ethernet enabled, which has caused a
   catastrophic stall on this hardware. Neither is inherited from your working
   ports. Step 5 covers both.
5. **Wire the new cameras' `Line1` inputs**, either to new pins added to
   `trigger_pins` or fanned off existing ones (Step 8). Fanning out needs no
   profile change at all, which is how this rig went from six cameras to nine
   without touching the pin list — but check the current budget first, because
   an under-driven camera misses triggers in a way that leaves no trace until
   the block-ID rate check runs.
6. **Raise `n_cameras`** to the new count. Until you do, the application refuses
   to open any cameras at all and reports the mismatch — that refusal is the
   interlock from step 1 doing its job, not a bug.
7. **Re-check capacity.** RAM is the usual binding constraint, and it scales with
   camera count *and* `kick_max_lag`: see *RAM* in section 1 and redo the
   arithmetic rather than assuming headroom. Also confirm the GPU will grant one
   NVENC session per camera; the preflight blocks a recording that would not get
   them, because the alternative is a camera silently falling back to raw.
8. **Verify, then recalibrate.** `uv run probe_network.py --sweep` should show
   every camera complete at 9000 bytes. Then calibrate from scratch: the rig's
   geometry has changed, and an old `calibration.toml` describes a camera set
   that no longer exists.

---

## 3. Verify it works

A window that opens is not proof that the rig works. The capture path is
timing-bound and its failures are quiet: a frame that never arrived looks much
like a frame that was never triggered. Verify at two levels. The code alone
needs no hardware and takes a minute. The real capture path with the cameras
running is the only thing that tells you whether *this* machine keeps up.

### Without any cameras

Four test suites run on the code alone. They are plain scripts, not pytest.

```powershell
uv run python test_frame_sync.py
uv run python test_grab_failure.py
uv run python test_stim_compiler.py
uv run python test_serial_handshake.py
```

Each ends with one summary line:

```
ALL FRAMESYNC EQUIVALENCE TESTS PASS
ALL GRAB-FAILURE TESTS PASS
ALL STIM COMPILER TESTS PASS
ALL SERIAL HANDSHAKE TESTS PASS
```

Read the last line only. The suites deliberately exercise failure paths, so
alarming output on the way through is expected:

```
[sync] cam3 RETIRED from the alignment set: stalled in test.
[grab3] STALLED — re-arming stream (attempt 1)
[teensy] no ack — reopening port to force a board reset
```

What each one covers:

| Suite | Covers |
|---|---|
| `test_frame_sync.py` | The kick-out coordinator matches a post-hoc block-ID intersection: exact equivalence, bounded skew, forced drops, freeze recovery, the 16-bit block-ID wrap, retiring a stalled camera. |
| `test_grab_failure.py` | Camera-failure paths against a stub camera and router: a camera that cannot start grabbing, a failed ring allocation, a grab thread that exits quietly, repeated grab errors, re-arm exhaustion. Each must retire that camera so the others keep recording aligned. |
| `test_stim_compiler.py` | Stimulation graph to firmware: start resolution, cycle-safe chain extraction, integer-microsecond encoding, safe-pin boot order, pin conflicts, forbidden pins, the RDY ack, the per-frame stimulus trace. |
| `test_serial_handshake.py` | The four trigger-board handshake outcomes: confirmed, retry after reset, legacy firmware, and a board that has acknowledged before going silent (which must refuse to record). pyserial is stubbed, so no COM port is needed. |

Run them through `uv run`, not a bare `python`: tests 10-13 of the stimulation
suite need numpy and skip silently without it.

A fifth suite needs an NVENC GPU but still no cameras:

```powershell
uv run python test_sync_router.py
```

### With real cameras

The suites say the logic is sound. They cannot say whether this machine, network
and set of cameras hold the frame-period deadline. Answer that before a real
session:

```powershell
uv run probe_lag.py --seconds 90 --label install-check
```

This drives the real capture path headless (`CameraManager`, `GrabThread`,
`SyncEncodeRouter`, `FrameSyncCoordinator`, `TeensyController`), so its result
applies to the GUI; only Qt's display work is missing. It writes
`probe_out/install-check/trace.json`; the video goes to a scratch directory and
is deleted unless you pass `--keep`.

A healthy run looks like this:

- **`cycle` equals the trigger period.** At 100 fps that is `cycle=10.00ms` on
  every camera, for the whole run. Anything above it means the loop falls behind
  by that much per frame, and the buffer pool hides it until it is full.
- **`avg_wait` is much larger than `avg_proc`.** The thread should spend most of
  its period waiting for the next trigger: about 8.5 ms of wait against 0.8 ms
  of work at 100 fps and 1920x1200.
- **`deliv_lag` sits near zero** and does not grow. It measures how stale a
  frame is when retrieved, against the camera's own clock.
- **`Buffer_Underrun_Count` is 0** on every camera, in the per-camera
  `stream stats` line printed at stop. Nonzero means the host did not keep up.
- **Frame counts are equal across cameras**, and `forced` is 0.

A 60-second six-camera run on the reference rig reported:

```
cycle=10.00ms on all six    avg_proc 0.80-0.90ms    avg_wait 8.40-8.62ms
deliv_lag ~0                Buffer_Underrun_Count 0 on all six
grabbed per camera: [6022, 6022, 6022, 6022, 6022, 6022]
released=6022  dropped=0  forced=0  queue_full_drops=0
```

with per-camera lag behind the leader at median 0, p95 1, max 2 frames.

Two numbers in the stream statistics are easy to misread.
`Resend_Request_Count` counts packets that were lost and asked for again, and
`Failed_Buffer_Count` above 0 is a frame given up on entirely. A high resend
count with `Failed_Buffer_Count` at 0 means the link is noisy but recovering
everything — **which is not free.** A resend arrives after the rest of the
buffer, so it completes that camera's frame late, and a camera completing late
every few frames is exactly what per-camera drift looks like. On this rig the
cameras behind a switch with flow control disabled ran 16,800 resends per 90 s
against 10 for their siblings, and they were the laggards, with no frames lost at
all. Treat a resend count three orders of magnitude above the other cameras as a
fault even when every frame arrives.

Measure on an otherwise idle machine. Competing CPU load has moved the cycle
from 10.00 to 10.32 ms, which accumulates 5.6 seconds of backlog over
150 seconds.

Then make a short real recording and check the output.
[WORKFLOW.md](WORKFLOW.md) covers that end to end.

---

## 4. Troubleshooting

The left-hand column quotes what the application prints, so search for a fragment
of your message.

### Installing

| Symptom | Cause and fix |
|---|---|
| `uv : The term 'uv' is not recognized` | PowerShell was opened before uv was installed. Open a new window; if it persists, sign out and back in. |
| `git : The term 'git' is not recognized` | Install Git for Windows, then reopen PowerShell. |
| `uv sync` fails to download | No internet, or a proxy. The clone and this step are the only ones that need it. |
| `warning: Skipping installation of entry points (project.scripts)` | Normal. The project is not packaged; nothing is wrong. |
| `make_shortcut.ps1` prints `No venv at ...` | `uv sync` has not been run in this copy of the repository. |
| `... cannot be loaded because running scripts is disabled` | Launch the script as `powershell -ExecutionPolicy Bypass -File <script>.ps1`, as shown above. |
| `This must run elevated (Set-NetAdapterRss needs admin).` | `configure_nic.ps1` needs an Administrator PowerShell window. |

### Cameras and the network

| Symptom | Cause and fix |
|---|---|
| `No cameras found` | Nothing enumerated. Check power and cabling, and close pylon Viewer or anything else holding the cameras. |
| `No cameras found or .pfs missing. Check connections and profile.` | Either the above, or `pfs_path` does not point at an existing file. |
| Cameras absent from pylon Viewer as well | GigE addressing or firewall. Re-run `PylonGigEConfigurator auto-all` elevated, and add the inbound UDP rule for both `python.exe` and `pythonw.exe`. |
| `Expected 6 cameras but 5 enumerated.` | One camera did not appear: dead switch port, unpowered, still booting, **or on the wrong subnet** (see the two rows below). Camera names are positional by serial-number order, so starting anyway would rename every later camera and attach the calibration extrinsics to the wrong physical camera. The message lists the serials it found. Run `uv run probe_network.py` to see which cameras answer and where, then power-cycle or re-address the missing one and reselect the profile. |
| `Expected 6 cameras but 9 enumerated.` | The opposite case, and usually correct behaviour after adding cameras: the profile has not been told about them. Set `n_cameras` to the real count in `profiles/<rig>.yaml`. **Check the serial order before you do** — new cameras only keep the existing `cam1..camN` names if their serials sort *after* the old ones; if they interleave, every later camera is renamed and old calibrations no longer describe the cameras they name. The message lists the serials it found, in order. |
| A camera is absent from pylon Viewer and the GUI, but its switch port shows link | It is almost certainly on the **wrong subnet** — plugged into a switch whose segment does not match its address. pylon silently omits out-of-subnet cameras, so this looks exactly like a dead camera. `uv run probe_network.py` finds it: GigE Vision discovery is answered whatever address the camera holds, so the tool reports which switch it is on and flags the mismatch. Fix by moving the cable to the matching switch, or by re-addressing the camera with the pylon IP Configurator (which reaches it by MAC over broadcast). This happens most often after cameras are unplugged to be repositioned and go back crossed. |
| Cameras enumerate and open, but every frame is incomplete — `Failed_Buffer_Count` climbing, zero complete frames | A switch in the path is at the default 1500-byte MTU. GVSP data packets are 9000 bytes and are discarded, while link, discovery and ICMP all look healthy. Set maximum frame size to 9216 on every port **including the uplink**, then confirm with `uv run probe_network.py --sweep`: a clean cutoff between 1500 (passing) and 2000 (failing) is the signature. **Do not test this with `ping`** — these cameras answer only tiny ICMP echoes, so a large ping fails even on a known-good jumbo path. |
| `Camera <serial> failed to open/configure: ...` | It enumerated but would not configure. Power-cycle it, or close whatever else holds it. The whole set is closed rather than continuing with a partial one. |
| `PixelFormat is Mono12, not Mono8` | The `.pfs` was saved with the wrong pixel format. Wider than 8-bit is truncated mod 256 with no error, so this refuses instead of recording shredded video. Fix the `.pfs`. |
| `resolution 1280x1024 differs from camera 1 (1920x1200); all cameras must match` | One camera has a different ROI. Reapply the same `.pfs`. |
| `FATAL: PaddingX=... rows would shear. Refusing to record.` | The camera reports row padding, which the zero-copy frame view cannot represent. That camera is retired from the alignment set; the others keep recording aligned. |
| `extended (64-bit) block IDs: UNAVAILABLE — relying on software unwrap` | Informational. The 16-bit block ID wraps every 65,535 triggers (about 11 minutes at 100 fps) and the software unwrap handles it. |
| Cameras vanish right after a NIC change | Expected. Changing adapter settings resets the adapters; the cameras re-enumerate within seconds. |
| Preview very dark, or nearly black | Exposure and gain come only from the `.pfs`. Add illumination, then raise `ExposureTime` to the ceiling, then `Gain`. |

### Trigger board

| Symptom | Cause and fix |
|---|---|
| `arduino-cli was not found` | Install the Arduino IDE or `arduino-cli`, or set `PANOPTICON_ARDUINO_CLI`. The message lists every location searched. Only the firmware flash and the Stimulation editor need it. |
| `Compile failed (arduino-cli exit N)` mentioning a missing core | `arduino-cli core install arduino:avr`. Nothing was flashed, so the board still runs what it ran before. |
| `Upload failed on COM3 (arduino-cli exit N)` | The port is held by something else (Arduino Serial Monitor, another instance), the profile names the wrong port, or the board is not an `arduino:avr:mega`. A part-way upload leaves the firmware, safe-pin boot guard included, in an unknown state; power-cycle the board. |
| `Could not clear stim firmware` at launch | The board may still hold a paradigm from a previous session, including one that loops and never ends. Open Stimulation and press Apply with an empty canvas, or key off the laser. |
| `[teensy] no ack — reopening port to force a board reset` | One occurrence is normal: the start command is retried after forcing a board reset. |
| `[teensy] board acked previously but not now — aborting` | This board has acknowledged before, so silence is a real fault. The cameras are rolled back and the recording refused rather than recording an empty session. Check the cable and power. |
| `Trigger board did not confirm the stop` | The board may still be triggering, and a looping stimulation chain never ends on its own. Power-cycle the board and key off the laser. |
| `Pin 4 cannot carry a stim waveform: camera trigger line — ...` | A stimulation block targets a camera trigger pin (extra edges would break that camera's alignment) or UART RX0/TX0. Move it to another pin. |

### It refuses to start a recording

| Message | Cause and fix |
|---|---|
| `No cameras are open. Recording would run the trigger protocol — and any baked-in stim paradigm — while saving nothing.` | Open the cameras first: pick a profile whose `pfs_path` resolves and whose cameras enumerate. |
| `Not enough RAM for N cameras: ...` | The buffers do not fit in available memory. The message breaks it into pool and ring. Lower `max_num_buffer` or `kick_max_lag` in the rig profile, or close other applications. Both are profile fields: neither `MAX_NUM_BUFFER` nor `MaxNumBuffer` appears in the YAML you edit. |
| `RAM is tight for N cameras: ...` | Over 75% of available memory. It asks before proceeding. |
| `NVENC granted only N concurrent sessions but M cameras need one each.` | The driver's session cap is below the camera count, often because another process holds sessions (a browser's hardware encode, an orphaned ffmpeg). Close them, record fewer cameras, or set `realtime_encode: false` in the profile to put every camera on the raw path deliberately. Both shipped profiles carry that field set to `true`, so it is a value you change rather than a line you add. Read the *Disk* part of section 1 first: raw needs roughly 500x the space. |
| `NVENC granted no encode sessions, so real-time encoding cannot start.` | No sessions available at all. Set `realtime_encode: false` in the profile to write raw frames and encode afterwards, and read the *Disk* part of section 1 first, because that is a completely different disk budget. |
| `Disk may be short: a 10-minute recording would need ~N GiB` | A warning, not a refusal: 10 minutes is an assumed worst case, not a known length. A shorter recording is fine. |
| `Disk is tight: a 10-minute recording needs ~X GiB of Y GiB free` | The milder version of the same check, raised once a ten-minute recording would use more than 80% of the free space. It will fit, but there is no room for a second session. Clear space before the day's work rather than mid-experiment. |
| `Raw capture will write N GiB/s.` | Spread the output across drives. A single consumer NVMe falls to about 1.6 GB/s once its SLC cache is exhausted. |

### Recording quality

| Symptom | Cause and fix |
|---|---|
| `cycle` above the frame period | The grab loop is not finishing inside one period. Something else is using the CPU, or a change added work to the hot path. |
| Frame rate about half of what was asked | Exposure is over the ceiling, so the camera is still busy when the next trigger fires and skips it. Lower `ExposureTime` in the `.pfs`. Raising `trigger_rate_limit` buys headroom only on a camera whose maximum frame rate is above the current limiter; the reference a2A1920-165g5m is already at its maximum of 165, and a higher value is clamped silently by the camera, so the change appears to apply and does nothing. Never set the limiter to `0`; see step 6. |
| `Buffer_Underrun_Count` nonzero | The driver's buffer pool ran dry: a host-side problem, not the network. |
| One camera stops delivering partway through a session, others fine | Check its temperature. These cameras have no fan and cool through the mount, so a badly ventilated or plastic-mounted camera can reach its shutdown threshold while its neighbours stay 10 C cooler. The status bar and `WARNINGS.txt` name it if `thermal_poll_s` is on. On the reference rig four of nine cameras run above the vendor's Critical threshold and one peaked 1 C below shutdown. |
| High `Resend_Request_Count`, with or without lost frames | Network. **Check switch flow control first — `symmetric` on every port in use including the uplink.** Disabled flow control measured 16,800 resends per camera per 90 s against 10 with it on, and the affected cameras were the ones drifting. Then jumbo frames end to end, Energy Efficient Ethernet, RSS receive queues, and cameras per port. |
| Roughly a quarter of frames missing, in single-frame gaps | `gige_driver: filter` discards a frame with a lost packet instead of asking for it again. Use `socket`. |
| `camera did not start grabbing` / `stream dead after N re-arms` | That camera was retired from the alignment set so the others keep recording aligned. The session yields N-1 cameras instead of nothing. |
| `block-ID bookkeeping claimed X frames but only Y were persisted` | An encoder fell behind or died. The metadata is truncated to what is in the video and a `WARNINGS.txt` is written beside it. |
| Video will not seek in LUC3D | The mp4 lost its explicit GOP. Every encode path must pass `-g <fps>` and `-movflags +faststart`. |
| Every camera has the same frame count and no counter moved, but the views are out of sync | The block IDs are not trigger ordinals. See the subsection below. |

### The recording looks fine but the views are out of sync

The only failure on this page that presents as success, which is why it has a
heading of its own. You meet it in one of two ways. Either the recording passes
every check you would think to run (the videos play, every camera has the same
number of frames, no dropped-frame, packet, underrun or forced-drop counter has
moved) and yet triangulated points sit nowhere near the animal and the views
visibly disagree about when a fast movement happened. Or a `WARNINGS.txt`
appears in the recording folder saying one camera's block IDs advanced at the
wrong rate.

**What it means.** Everything downstream treats "same block ID" as "same
instant". A block ID is the GigE Vision frame counter arriving with each frame,
and alignment rests on it being the *trigger* ordinal: block ID *N* is the frame
the *N*th trigger produced, on every camera. That holds only while a camera
produces exactly one frame per trigger. A camera whose exposure exceeds the
ceiling (about 3.94 ms at 100 fps with the limiter at 165; step 6) is still busy
when the next pulse arrives. It does not drop that frame; it never acquires
it, so it never consumes a block ID for it. From then on its block ID *N* is
trigger *N+k*, and the offset grows each time. Its IDs stay gapless, its frame
count still matches the others because only common IDs are kept, and no other
check can see it, because the release rule compares block IDs and nothing else.
The videos look perfect while drifting apart in time.

**The software now detects it.** The camera's device clock is a free-running
hardware oscillator, independent of its block-ID counter, so the two together
settle the question: over any span, block IDs must advance at the trigger rate.
That comparison runs per camera when a recording stops, and again inside
`align_recording()`, so `uv run 2_align.py <recording_dir>` can re-examine a
recording you already have. Nothing was added to the per-frame hot path; the
device timestamps were already collected. When a camera fails, a *Recording
completed with problems* dialog names the camera, its measured rate and roughly
how far the views drift apart by the end, and the same text goes into
`WARNINGS.txt` beside the videos.

The tolerance is 0.3%, from measurement rather than theory. Across 74
camera-sessions of real data (2026-06-12 to 2026-09-03, at both 30 and 100 fps,
including the sessions that lost 24% and 43% of frames) the measured rate sits
between **+220 and +250 ppm** of configured, the fixed offset between the trigger
board's resonator and the cameras' own oscillators. 0.3% leaves 12x margin over
the worst real sample while still catching a camera that ignores one trigger in
a hundred. The check abstains below 300 frames or 2 seconds of data, where end
effects and a genuinely skipped trigger cannot be told apart, so silence on a
two-second test clip means nothing was judged, not that all is well.

**The fix is exposure, but read the log before you edit the `.pfs`.**
`apply_exposure_gain()` recomputes the ceiling at every acquisition start and
clamps to it unconditionally, logging `CLAMPED from ...` when it does. So the
natural first move, opening the `.pfs`, finding a value comfortably under
3.94 ms and then being baffled, is the wrong one: if the clamp had had anything
to act on it would already have acted.

The clamp still rests on its inputs, and both come from the profile. The ceiling
is `(1/frame_rate - 1/trigger_rate_limit) x 0.9`, so the clamp can be entirely
correct about a ceiling that is not the real one. Set `trigger_rate_limit` above
the camera's own maximum and the camera clamps it silently, the software
subtracts a smaller `1/rate` floor than the camera enforces, and the ceiling
comes out too generous. Hence step 7's rule: your camera's real maximum and
nothing else. Equally, if the board triggers faster than the profile's
`frame_rate` claims, the true period is shorter than the arithmetic assumed. And
the clamp does not run at all against a camera whose baseline exposure could not
be read back at open: it has nothing to compare against and keeps whatever the
`.pfs` gave it.

So read the exposure line logged at each acquisition start, before you read the
`.pfs`. It appears in the console and in `logs/panopticon_<date>_<time>.log`:

```
[cam1] exposure=3000 us gain=6.0 dB (ceiling 3545 us at 100 fps)
```

The exposure actually applied and the ceiling it was measured against, side by
side. The line prints for camera 1 on every start, and for any camera that was
clamped, carrying a ` CLAMPED from ...` suffix naming the value it rejected and
why. If the clamp fired, the exposure was too long and lowering `ExposureTime`
in the `.pfs` is the fix. If it did not fire and the `.pfs` really is under the
ceiling, the ceiling is the next suspect: check `trigger_rate_limit` against the
camera's maximum frame rate, and `frame_rate` against what the board is driving.

Once you know the true ceiling, lower `ExposureTime` until the exposure is under
it for the frame rate you record at, and take the recording again. The affected
recording cannot be repaired and must not be used for 3D reconstruction. If you
need the light back, add illumination rather than exposure; see step 6.

**One pattern that is not this.** If *every* camera reports the same wrong rate,
suspect the reference rather than the cameras, because cameras do not fail
identically. The usual causes are a profile `frame_rate` that does not match
what the trigger board drives, or a camera model that does not report its device
timestamp in nanoseconds. The message says so instead of blaming exposure once
per camera; in that case the videos are probably aligned with each other and it
is the absolute timebase in question.

### Calibration

| Symptom | Cause and fix |
|---|---|
| `ERROR: fewer than 2 cameras with detections` | The board description in `configs/boards/*.yaml` does not match the physical board. Check `board_legacy` first. It picks which of two ChArUco corner layouts the detector maps the markers onto: `false` for a board generated with OpenCV 4.6 or newer, `true` only for one printed to the older layout. A mismatch in *either* direction detects every marker perfectly, maps them onto the wrong layout, and returns zero board corners with no error and no warning. Flip the flag before suspecting the print or the lighting. The shipped `configs/boards/charuco_8x8_15mm.yaml` carries `board_legacy: true` because the reference rig's board predates the layout change, so a copy of it is the wrong starting point for a board printed with a current OpenCV. |
| numpy ABI error from `1_calibrate.py` | The environment is half-built. The solve runs in the project environment rather than resolving a set of its own, so its OpenCV and numpy must be the ones `uv sync` installed. Run `uv sync` again in the repository and retry. |
