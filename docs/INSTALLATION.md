# Installation

Settings are in [CONFIGURATION.md](CONFIGURATION.md): every profile field, the
templates and the sizing formulas. The messages Panopticon prints, with their
causes and fixes, are in [TROUBLESHOOTING.md](TROUBLESHOOTING.md). A FLIR rig
follows [FLIR.md](FLIR.md) for its install and first test.

Contents:

1. [What the rig needs](#1-what-the-rig-needs)
2. [Install the software](#2-install-the-software)
3. [Verify it works](#3-verify-it-works)

---

## 1. What the rig needs

Panopticon runs on Windows with an NVIDIA GPU, Basler or FLIR machine-vision
cameras and a hardware TTL trigger, for any number of cameras. The load on
each part of the rig follows from the frame size, the frame rate, the camera
count and the pixel format. The pixel format is always Mono8, one byte per
pixel.
A part too small for its load loses frames while the recording carries on, so
size each part before you buy it.

A frame's size is its width times its height, in bytes:

```
frame_bytes = width x height                (Mono8)
1920 x 1200 = 2,304,000 bytes                (2.3 MB per frame per camera)
```

[CONFIGURATION.md](CONFIGURATION.md#sizing-formulas) collects the formulas for
network, RAM, disk and NVENC sessions.

### Cameras

Panopticon drives cameras through one backend module per vendor, in
`gui_app/backends/`:

| Cameras | Backend | Status |
|---|---|---|
| Basler, GigE or USB3 | `basler`, through pypylon and the pylon SDK | Runs the reference rig |
| FLIR (Teledyne), GigE or USB3 | `flir`, through the Spinnaker SDK 4.x | In testing. Nothing has run on FLIR hardware yet ([FLIR.md](FLIR.md)) |

Every camera needs:

- a Mono8 pixel format. Opening refuses a camera set to a wider format,
  because the capture path stores one byte per pixel.
- the frame size the profile names, the same on every camera. Opening refuses
  a camera whose frame size differs.
- a hardware trigger input ([A trigger source](#a-trigger-source)).

A profile names one backend, so one rig uses one vendor. Support for another
vendor is one new backend module, and [INTERNALS.md](INTERNALS.md) lists what
that module has to provide.

### Camera temperature

A machine-vision camera without a fan cools through its housing and its mount.
A camera that reaches its shutdown temperature stops delivering frames in the
middle of a recording.

Measured on the reference rig in September 2026 (nine Basler a2A1920-165g5m
cameras, no fans):

- With one heatsink each, idle cameras levelled off at 72-78 C. Three sat above
  76 C, the level the camera itself flags as Critical. The camera shuts down at
  81 C.
- The heatsinks lowered the idle temperature by 2-4 C. A second heatsink on the
  hottest camera lowered it by another 2.7 C.
- Recording raised the temperature by about 0.3-0.7 C per minute with the
  heatsinks, and by 1.1-1.3 C per minute before them. From the hottest
  camera's idle level of about 77.5 C, a recording reaches 80 C within about
  3-8 minutes, 1 C below the shutdown.

Plan the cooling before you mount the cameras:

- Mount each camera on metal. A metal bracket conducts heat away from the
  housing, and a plastic one leaves the camera to cool through the air alone.
- Fit heatsinks, starting with the cameras in the least airflow.
- Some recording rooms do not allow fans (the reference rig's room is one), so
  plan for passive cooling first.

During an acquisition Panopticon reads each camera's temperature every
`thermal_poll_s` seconds. It warns in the status bar and the log when a camera
comes within `thermal_warn_margin_c` of the shutdown temperature the camera
reports, or when the camera reports its over-temperature state. That warning
reaches `WARNINGS.txt` and the post-session dialog only when the recording lost
frames; a camera that reached its shutdown point is always listed there. Every
session's `session_metadata.json` records each camera's peak temperature
(`temp_max_c`). On a camera that reports its shutdown temperature, the Critical
flag alone raises no alert. A camera that reports none is judged by its own
status, Critical included. The reference profile sets the margin to 2 C, so it warns at 79 C on
these cameras.
[CONFIGURATION.md](CONFIGURATION.md#thermal_warn_margin_c) describes both
settings.

The Critical and shutdown levels are fixed in the camera's firmware and cannot
be raised. Leave the camera's temperature override node
(`BsliDeviceTemperatureOverwriteEnable`) alone: it makes the camera report a
false temperature, and the camera's own over-temperature protection then acts
on that false value.

### A trigger source

Panopticon always triggers the cameras in hardware. Free-running cameras drift
apart, because each runs on its own oscillator. A shared TTL trigger makes frame
*N* of every camera the same instant, to within the gap between trigger pins
([Wiring the trigger line](#wiring-the-trigger-line)) and the cameras'
trigger-to-exposure jitter. Each frame carries the camera's count of the
triggers it acquired, its [block ID](GLOSSARY.md#block-id). Alignment rests on
that count.

| Source | Profile | Stimulation |
|---|---|---|
| Panopticon's trigger board, an Arduino Mega 2560 | `trigger_source: board` (the default), `serial_port`, `trigger_pins` | Runs on the same board |
| Your own TTL source, such as a pulse generator or a DAQ | `trigger_source: external` | Not available |

Panopticon programs its board itself, through `arduino-cli`
([step 8](#step-8--flash-the-trigger-firmware)). With your own source you start
and stop the pulses yourself, and Panopticon refuses the recording if any
camera receives a frame before every camera is armed. The order of the steps is
in FLIR.md's [Use your own trigger source](FLIR.md#use-your-own-trigger-source),
and it applies to Basler cameras too.
[CONFIGURATION.md](CONFIGURATION.md#trigger_source) describes the field.

#### Wiring the trigger line

A wiring mistake cannot be repaired after the recording: a camera that misses
triggers records views that are not simultaneous with the others.

Wire each camera's trigger input to a pin listed in the profile's
`trigger_pins`, or to your own source's output. The input sits on the camera's
I/O connector, and the camera's data sheet gives the pinout.

- On a Basler camera the input is `Line1`. Panopticon sets
  `TriggerSource=Line1` and `TriggerActivation=RisingEdge` when an acquisition
  starts.
- On a FLIR camera the profile's `camera.trigger.line` names the input.
  FLIR.md's [Wire the trigger](FLIR.md#2-wire-the-trigger) covers opto-isolated
  and non-isolated inputs.

Which pins to use on a Mega:

| Pins | Rule |
|---|---|
| `0` and `1` | Never. They carry the serial link that configures the board. The profile loader refuses them in `trigger_pins`. |
| Any pin in `stim_safe_pins` | Never. The loader refuses a pin listed in both. |
| `14` to `19` | Avoid. They are the board's extra serial ports. The sketch does not use them, so they work as outputs. |

The stimulation editor refuses a stimulation block on a camera's trigger pin,
because the extra edges would advance that camera's block IDs and break the
alignment. Any other digital pin works. Write down which pin drives which
camera, because you will need the list when one camera stops triggering.

Each camera also needs:

- A common ground. Run a ground wire from the board to the ground pin of each
  camera's I/O connector (the opto ground, for an opto-isolated input), or to
  one ground point they all share.
- Power, which does not come from the trigger line. Depending on the model it
  comes over Power over Ethernet from the switch, or from a supply wired to the
  I/O connector. The reference rig's cameras take 12 V through the I/O
  connector.
- A signal its input accepts. A Mega drives 5 V. An opto-isolated input is
  driven by current. The camera's I/O documentation gives its switching
  threshold and the current it draws.

The sketch writes every pin in `trigger_pins` in one loop with interrupts off,
so no interrupt can widen the gap between pins. The pins still change one after
another, microseconds apart. Cameras on one pin share one edge. One pin can
therefore drive several cameras, if it can source the current they all draw.
An ATmega2560 pin is rated for about 20 mA. One pin per camera gives each input
the full 20 mA. The reference rig drives nine cameras from six pins.

A camera driven with too little current misses a trigger now and then. A
missed trigger consumes no block ID and leaves no gap in `blockids.npy`, so the
videos keep equal lengths while they drift apart in time. Only the block-ID
rate check after the recording catches it
([TROUBLESHOOTING.md](TROUBLESHOOTING.md#the-recording-looks-fine-but-the-views-are-out-of-sync)).
Before you fan one pin out to several cameras, add up their input currents.

The order of `trigger_pins` carries no meaning, but every pin that drives a
camera must be in the list. A camera on an unlisted pin receives no triggers.
Panopticon retires it once it stalls, and the other cameras record without it.

### Network

A GigE Vision camera sends each frame as a burst of UDP packets. A link near its
capacity drops packets, and the frame is lost unless a resend recovers it. USB3
cameras skip this section, apart from [USB3 cameras](#usb3-cameras) at its end.

One camera's rate on its link is its frame size in bits times the frame rate:

```
bits per second = width x height x 8 x frame rate
1920 x 1200 x 8 x 100 = 1.84 Gbit/s per camera
```

| Format | Bytes per frame | Payload rate |
|---|---|---|
| 1920x1200 Mono8, 100 fps | 2,304,000 | 1.84 Gbit/s |
| 1920x1200 Mono8, 30 fps | 2,304,000 | 553 Mbit/s |
| 1280x1024 Mono8, 100 fps | 1,310,720 | 1.05 Gbit/s |

These rates leave out the packet headers, so treat them as a floor.

Each camera's rate crosses its own link and the host port it shares with the
other cameras behind the same switch. GigE Vision is the protocol, and it runs
over 1, 2.5, 5, 10 and 25 Gbit/s links.

#### The camera's own link

A 1920x1200 camera at 100 fps needs 1.84 Gbit/s. A 1 Gbit/s link carries about
54 fps of that frame, so the reference rig uses the a2A1920-165g5m, a 5 Gbit/s
model. At 30 fps the same frame needs 553 Mbit/s and fits a 1 Gbit/s camera.
The frame rate moves the number most: 1280x1024 at 100 fps still needs
1.05 Gbit/s.

The link speed also decides how long one frame takes to send. The camera leaves
a gap between its packets, the [inter-packet delay](GLOSSARY.md#inter-packet-delay)
(`GevSCPD` on a Basler camera), so that cameras sharing a port do not burst
into each other. One frame then takes
(packets per frame) x (packet time on the wire + delay). That time has to fit
inside the trigger period. A camera still sending when the next trigger
arrives ignores that trigger.

Keep the frame time at least a millisecond inside the period. On the reference
rig at 5 Gbit/s and 100 fps, `GevSCPD` 20000 (about 8.9 ms per frame) kept the
full frame rate. At 22000 (about 9.5 ms) every camera recorded half the
trigger rate. Do not set the delay to 0 either. At 0 each frame goes out as one
burst, and the bursts of cameras sharing a port collide at the switch.

With the reference camera settings (9000-byte packets, `GevSCPD` 10000, which is
10 µs) a 1920x1200 frame is about 260 packets:

| Negotiated link | Time per packet | Time per frame | At 100 fps (10 ms period) |
|---|---|---|---|
| 5 Gbit/s | 14.4 µs + 10 µs | about 6.3 ms | Fits |
| 2.5 Gbit/s | 28.8 µs + 10 µs | about 10.1 ms | Too long. The camera ignores every second trigger and records 50 fps |

At 2.5 Gbit/s the inter-packet delay makes each frame take 10.1 ms, longer than
the 10 ms period, although the link's bandwidth would carry the camera's
1.84 Gbit/s. The recording looks normal, and the block-ID rate check after it
reports that camera at half the trigger rate. Keep
every camera on a port that negotiates 5 Gbit/s or more. On a 2.5 Gbit/s link,
`GevSCPD` would have to drop to about 3000 (about 8.3 ms per frame), and the rig
would need testing again at that setting.

#### The host port and the switches

Add up the cameras that share a host port and keep the total well under the
port's line rate. Three 1920x1200 cameras at 100 fps send 5.53 Gbit/s, which a
10 GbE port carries with about 45% to spare. The number of host ports follows:

```
host ports = ceil(n_cameras / cameras per port)
```

The reference rig has nine cameras in three groups of three. Each group sits
behind its own switch, and each switch has its own 10 GbE port on the host.

A switch's camera ports have to run at the camera's link speed: a switch with
1 GbE access ports holds a 5 Gbit/s camera to 1 Gbit/s. Multi-gigabit switches
often split their ports into speed blocks. The reference rig's NETGEAR MS510TXM
has four 100M/1G/2.5G ports and four 1/2.5/5/10G ports. Put every camera and
the uplink in the fast block, and read the negotiated speed of each port in the
switch's web interface.

The reference camera settings send 9000-byte packets
([jumbo frames](GLOSSARY.md#jumbo-frames)). Every device in the path has to
accept them:

- the host adapter, with its Jumbo Packet at 9014 bytes
  ([Configure the host adapters](#configure-the-host-adapters))
- every switch port, the uplink included, at 9216 bytes
  ([Configure the switches](#configure-the-switches))

A 10GBASE-T copper run needs Cat6a cable or better. A marginal cable shows up as
packet loss while the link stays up.

#### USB3 cameras

USB3 cameras share the bandwidth of the host controller they plug into. Spread
them over controllers, and before you record, stream them all together at the
target rate in pylon Viewer or SpinView.

### CPU

Each camera's grab loop has to finish every frame inside one trigger period,
10 ms at 100 fps. A loop that falls behind stays behind, because it can
retrieve frames only as fast as they arrive. Its backlog grows in the driver's
buffer pool until the pool runs out.

The CPU work per camera:

- A grab thread retrieves each frame, copies it into a buffer of the
  [NV12](GLOSSARY.md#nv12) ring, queues it for the encoder and releases the
  driver's buffer. At 1920x1200 that takes about 0.8 ms per frame, roughly 8% of
  one core at 100 fps.
- An encoder thread feeds the GPU encoder. With the default
  `nvenc_upload: pinned` it first copies each frame into page-locked memory
  without holding Python's global interpreter lock (the
  [GIL](GLOSSARY.md#gil)). That costs about
  1 ms of CPU per frame, outside the GIL.
- For GigE cameras, the network driver reassembles the packets. That work runs
  as deferred procedure calls (DPCs) on the cores the adapter's receive-side
  scaling (RSS) assigns. Three 1920x1200 cameras at 100 fps send about 78,000
  packets/s into one port, and on the reference rig one core ran at 46% DPC load
  for such a port.

The GIL limits how far this scales. A recording runs two threads per camera plus
the window's, about 19 busy threads at nine cameras, and only one thread runs
Python at a time. The time each thread holds the GIL per frame therefore matters
more than the number of cores. Up to about 300 µs per thread per frame is safe
even at 17 threads, and about 1000 µs breaks the 10 ms budget at 11 threads. The pinned upload exists
for this reason. The GPU encoder's own copy of each frame holds the GIL, and on
the nine-camera rig that copy made one camera drift behind the others.
[CONFIGURATION.md](CONFIGURATION.md#nvenc_upload) describes the setting.

On a CPU with performance and efficiency cores the scheduler moves threads
between the two kinds. A grab thread on an efficiency core runs a few percent
slow, which it can never make up, and one camera falls behind in each session,
a different one each launch. `pin_capture_threads` pins each grab thread to its
own performance core, and `capture_core_exclude` keeps those threads off the
cores that carry the network adapter's DPCs. Both work on Windows only and do
nothing on a CPU with one kind of core. The two encoder placement settings
measured worse on the reference rig and stay off.
[CONFIGURATION.md](CONFIGURATION.md#pin_capture_threads) describes all four.

The launch check warns below 4 physical cores. That is the floor for running
Panopticon at all. Size the CPU from the figures above.

### RAM

Panopticon holds two sets of frame buffers per camera during an acquisition.
When you press Record it adds them up, and it refuses the start when the total exceeds the
memory available at that moment. Running out of memory in the middle of a
recording would lose frames, so a start that does not fit is refused.

```
driver pool = n_cameras x max_num_buffer x frame_bytes
NV12 ring   = n_cameras x (kick_max_lag + 264) x frame_bytes x 1.5
```

- `max_num_buffer` sets the depth of the camera driver's buffer pool, which
  holds frames while a grab thread catches up. Keep it at or above
  `kick_max_lag`. The profile loader refuses a pool shallower than
  `kick_max_lag` in kick-out mode.
- The NV12 ring holds frames on their way to the encoder. In the default
  kick-out mode a trigger's frames wait in the ring until every camera has
  delivered that trigger, for at most `kick_max_lag` frames. The 264 is the
  encoder queue (200 frames) plus 64 spare slots. The factor 1.5 is the size of
  an NV12 frame: a full-size gray plane and a half-size colour plane. With
  `realtime_kick: false` the ring is 204 buffers per camera, and with
  `realtime_encode: false` there is none.
- Each ring is freed when its acquisition stops, so the next recording needs the
  same memory as the first.
- The pinned GPU upload adds page-locked memory of n_cameras x 4 x frame_bytes x
  1.5, 0.12 GiB at nine 1920x1200 cameras.

Nine 1920x1200 cameras with the reference profile (`max_num_buffer: 600`,
`kick_max_lag: 480`):

```
pool  = 9 x 600 x 2,304,000 B        = 11.6 GiB
ring  = 9 x 744 x 3,456,000 B        = 21.6 GiB
total                                = 33.1 GiB
```

At `kick_max_lag: 240` the ring is 504 buffers per camera, 14.6 GiB at nine
cameras. [CONFIGURATION.md](CONFIGURATION.md#kick_max_lag) says what the lag
limit costs and when to change it.

The total has to be available when you press Record, with the operating system
and the window on top, so budget about twice that total. The reference rig has
63.4 GiB for a nine-camera need of 33.1 GiB. A start that fits goes ahead
without a prompt, and the log records the figures on a `[hw] RAM for N cameras`
line. The launch check warns below 16 GiB of RAM as Windows reports it, so a
16 GB machine usually gets the warning. That is the floor for running
Panopticon at all, and a 16 GB machine cannot hold a six-camera recording at
the reference settings.

### GPU

Panopticon encodes H.264 on the GPU's NVENC encoder while it captures, which
keeps the disk rate small ([Disk](#disk)). It needs an NVIDIA GPU with NVENC.
PyNvVideoCodec does the encoding, and the CUDA runtime arrives as a Python
package, so no CUDA toolkit is needed.

A real-time recording needs one NVENC session per camera. The NVIDIA driver caps
how many sessions run at once, and the cap differs between GPUs and has changed
between driver generations (2, then 3, 5, 8 and 12). More cameras need a more
capable GPU, and the session cap is often the limit that binds first.
Panopticon never assumes the cap. When it checks the hardware it asks the driver
for two more sessions than there are cameras. Before each recording it refuses
the start if the driver grants fewer than one per camera, unless `encoder: auto`
can record on the CPU instead. On the
reference rig an RTX 5080 grants 12 sessions, enough for its nine cameras.

Ask a candidate GPU the same question before you buy cameras for it. This needs
only steps 1, 3 and 4 below:

```powershell
uv run python -c "from gui_app import nvenc; print(nvenc.probe_max_sessions())"
```

It prints how many sessions the driver granted, up to 24. Anything at or above
your camera count is enough. `0` means PyNvVideoCodec did not load, or the
driver granted no session: close other programs that encode on the GPU and ask
again. An error instead of a number means the GPU or its driver refused to
create an encoder.

With the default `encoder: auto`, a machine whose GPU cannot give every camera a
session encodes on the CPU with libx264 instead, when the launch benchmark
shows the CPU can keep up. The CPU encoder competes with the grab threads for
cores. [CPU_ENCODE.md](CPU_ENCODE.md) describes it, and
[CONFIGURATION.md](CONFIGURATION.md#encoder) lists the choices. ffmpeg comes
with the Python packages (`imageio-ffmpeg`), so there is nothing else to
install.

### Disk

The profile field `realtime_encode` decides the disk rate.

With real-time encoding (the default) only H.264 reaches the disk, about 4.6 KB
per 1920x1200 frame at the default quality:

```
9 cameras x 100 fps x 4,600 B = 4.1 MB/s    (about 15 GB per hour)
```

Any ordinary drive keeps up.

With `realtime_encode: false` every frame is written whole to `raw.bin` and
encoded after the recording. Use it only when the GPU cannot give every camera
a session and the CPU cannot encode them either. The rate is the full payload:

```
n_cameras x fps x frame_bytes
9 x 100 x 2,304,000 B = 2.07 GB/s
```

That is about 500 times the H.264 rate. Above 1.5 GiB/s Panopticon warns at
Record, because a consumer NVMe drive falls to about 1-2 GB/s once its write
cache fills. Use a drive rated for that sustained rate, or split the cameras
across drives.

The launch check warns below 500 GiB free and below 500 MB/s measured write
speed. It measures the speed by writing 256 MB to the output directory and
deleting it.

### Operating system

Panopticon runs on 64-bit Windows, and only Windows has been tested. Several
parts are Windows-specific: the thread placement settings (they do nothing on
other systems), `configure_nic.ps1`, `make_shortcut.ps1` and the firewall
rule in step 5. On Linux, USB3 cameras draw their buffers from usbfs, and the
launch check warns when `usbfs_memory_mb` is too small for them.

### The reference rig

Every measurement in these pages comes from this rig. Use it as one known-good
point to check your own figures against.

| Part | The reference rig |
|---|---|
| CPU | Intel Core Ultra 9 285K, 24 cores (8 performance, 16 efficiency) |
| RAM | 63.4 GiB |
| GPU | NVIDIA RTX 5080, 12 concurrent NVENC sessions |
| Cameras | 9 Basler a2A1920-165g5m (5 GigE), 1920x1200 Mono8 at 100 fps |
| Network | 3 NETGEAR MS510TXM switches, 3 cameras each, one 10 GbE host port per switch |
| Trigger board | Arduino Mega 2560 on `COM3`, six trigger pins fanned out across nine cameras, laser on pin 53 |
| OS | Windows 11 |

---

## 2. Install the software

Do the steps in order, and check the result of each.

FLIR cameras: follow steps 1, 3 and 4 here, then [FLIR.md](FLIR.md) from its
Spinnaker install onward.

Every command goes into PowerShell. To open it, press the Windows key, type
`powershell` and press Enter. A window opens with a prompt like
`PS C:\Users\you>`. Type a command, press Enter, and wait for the prompt to come
back. Right-click pastes.

Some steps need PowerShell as Administrator, also called an elevated
PowerShell. Press the Windows key, type `powershell`, right-click
*Windows PowerShell* in the results and choose *Run as administrator*. The
window's title then says *Administrator*.

### Step 1 — install uv

uv manages the Python version and every package, so nothing goes into a system
Python. The Windows command from
<https://docs.astral.sh/uv/getting-started/installation/> is:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close PowerShell and open it again, because only new windows see the PATH the
installer changed. Then check:

```powershell
uv --version
```

Expected: one line starting with `uv` and a version number. If PowerShell says
`The term 'uv' is not recognized`, sign out of Windows and back in.

### Step 2 — install the Basler pylon SDK

pypylon calls Basler's own SDK, so install the SDK before the Python packages.
Download it from <https://www.baslerweb.com/en/downloads/software-downloads/>.
It provides the camera driver, pylon Viewer (step 6), and the IP Configurator
and PylonGigEConfigurator (step 5).

For FLIR cameras, skip this step and install the Spinnaker SDK as
[FLIR.md](FLIR.md#1-install) describes. The folder that `camera.flir.sdk_dir`
or `PANOPTICON_SPINNAKER_DIR` names may also be `bin64\vs2015` inside the
install, which holds `SpinnakerC_v140.dll`. Panopticon never adds that folder
to `PATH`, because it also holds Qt libraries that would replace PyQt5's. The
library is 64-bit, so Panopticon needs a 64-bit Python, which uv installs.

Check the install:

```powershell
Test-Path "C:\Program Files\Basler\pylon\Runtime\x64\PylonGigEConfigurator.exe"
```

Expected: `True`. If it prints `False`, find the install folder and use that
path in step 5.

### Step 3 — get the code

Clone the repository wherever you like. This page uses the Desktop to keep the
paths short:

```powershell
cd $HOME\Desktop
git clone https://github.com/talmolab/panopticon.git
cd panopticon
```

The output ends with lines like:

```
Cloning into 'panopticon'...
remote: Enumerating objects: ...
Receiving objects: 100% ...
Resolving deltas: 100% ...
```

The repository has no submodules, so the clone is complete. If PowerShell does
not recognize `git`, install Git for Windows from
<https://git-scm.com/download/win> and open PowerShell again.

Every later command runs from inside the repository
(`PS C:\Users\you\Desktop\panopticon>`). In a new window, go back there with
`cd $HOME\Desktop\panopticon`.

### Step 4 — install the Python dependencies

```powershell
uv sync
```

The first run downloads a Python interpreter and about two dozen packages into a
`.venv` folder in the repository, and ends with a line like
`Installed 22 packages in 42s`. Later runs take a moment:

```
Resolved 26 packages in 1ms
Checked 22 packages in 0.82ms
```

On a machine with no Basler cameras and no NVIDIA GPU, leave the camera and GPU
packages out:

```powershell
uv sync --no-group rig
```

Pass `--no-group rig` to every `uv run` on that machine as well, for example
`uv run --no-group rig gui.py --profile sim`. A plain `uv run` installs the
default groups again, the camera and GPU packages included, and needs the
internet to do it. `_launch.bat` runs a plain `uv run`, so start Panopticon
from PowerShell there. With the flag, that environment runs the simulated rig
([SIMULATION.md](SIMULATION.md)) and the post-session tools.

On the rig, check the main imports:

```powershell
uv run python -c "import pypylon.pylon, PyQt5, numpy; print('ok')"
```

Expected: `ok`. Anything else means the sync did not finish. Run `uv sync`
again and read its error.

Once a full `uv sync` has run, acquisition, encoding, the calibration solve and
the post-session tools need no internet. They run offline, in the environment
`uv sync` built. Run each script as `uv run python <script>.py`.

### Step 5 — put the cameras on the network (GigE)

A GigE camera streams only when all of these hold:

- It and its host adapter have addresses on the same subnet. Otherwise it does
  not appear at all.
- Every switch between them passes jumbo frames. Otherwise it appears and
  delivers nothing.
- Windows lets its traffic reach Panopticon. Otherwise Panopticon does not find
  it, or gets no frames from it.
- The adapter is set up for the traffic the camera sends.

USB3 cameras skip this step.

#### The addressing scheme

Give each switch its own /24 subnet, with the host adapter at `.2` and the
cameras from `.3` upward. An address then tells you which switch a camera is on,
and a camera plugged into the wrong switch stops answering.

The reference rig, nine cameras behind three switches:

| Subnet | Host adapter | Switch management | Cameras |
|---|---|---|---|
| `192.168.3.0/24` | Ethernet 4 at `.2` | `.240` | `.3` `.4` `.5` |
| `192.168.4.0/24` | Ethernet 5 at `.2` | `.240` | `.3` `.4` `.5` |
| `192.168.5.0/24` | Ethernet 3 at `.2` | `.250` | `.3` `.4` `.5` |

Panopticon names the cameras `cam1` to `camN` from their serial numbers
([Adding cameras](#adding-cameras-to-a-rig-that-already-works) gives the
order). Matching each camera's name to the last number of its address makes
cabling faults easier to find.

With a single switch, pylon can assign every address:

```powershell
& "C:\Program Files\Basler\pylon\Runtime\x64\PylonGigEConfigurator.exe" auto-all
```

It gives every adapter and camera it finds compatible addresses. It does not
configure the switch, and it does not let you choose which camera gets which
address, so plan the addresses by hand once you have more than one switch.

#### Configure the switches

A managed switch at its default settings passes discovery, pylon Viewer's
device list and `ping`, and drops every image packet. Set these on every port in
use, the uplink to the host included:

| Setting | Value | Why |
|---|---|---|
| Maximum frame size | 9216 | Image packets are 9000 bytes. |
| Flow control | Symmetric | Lets the switch pause a camera for microseconds instead of dropping its packets. Measured below. |
| Energy Efficient Ethernet | Disabled | Off on the reference rig's switches. On the host adapters it caused a stall ([Configure the host adapters](#configure-the-host-adapters)). |
| Storm control | Disabled | Off on the reference rig's switches, which is the setting its measurements were made with. |

Flow control measured on three switches of the same model, firmware and ports,
nine cameras, 90 s:

| Flow control | GVSP resend requests per camera | Worst per-camera lag |
|---|---|---|
| Symmetric | 8-10 | 1-4 frames |
| Disabled | 16,700-16,900 | 5-12 frames |

Several cameras burst into one uplink at once. With flow control, 802.3x PAUSE
frames ask a camera to wait while the switch's queue drains. Without it the
switch drops the packet, the camera resends it, and that camera's frame
completes late, which looks like a slow camera. At the end of each recording
the grab threads print each camera's stream counters. `Resend_Request_Count` in
the thousands with `Buffer_Underrun_Count` at 0 means loss in the network, and
flow control is the first thing to check.

Worked example on a NETGEAR MS510TXM (other managed switches differ in their
menu names):

1. A switch fresh from the factory asks for an address over DHCP, and falls back
   to `192.168.0.239` with mask `255.255.255.0` when nothing answers, which is
   the normal case on a camera network. To reach it, give its adapter a
   temporary address from an elevated PowerShell:
   ```powershell
   New-NetIPAddress -InterfaceAlias "Ethernet 3" -IPAddress 192.168.0.100 -PrefixLength 24
   ```
2. Browse to `http://192.168.0.239` and log in as `admin`. The first login makes
   you set a password. Write it down: the only recovery is a factory reset (hold
   the reset button about 10 s), which returns the switch to `192.168.0.239`.
3. Under *Switching > Ports > Port Configuration*, set *Maximum Frame Size* to
   9216 and *Flow Control* to *Symmetric* on every port in use. Firmware
   versions label the frame size as "Frame Size", "MTU" or "Jumbo".
4. Disable Energy Efficient Ethernet and storm control on the same ports.
5. Under *System > Management > IP Configuration*, set the protocol to
   *Static*. The fields stay greyed out until you do. Give the switch an address
   on its camera subnet (the reference rig uses `.240` or `.250`), the mask
   `255.255.255.0` and the gateway `0.0.0.0`, because a camera network has no
   router. Applying it ends your browser session, because the switch has moved
   subnet.
6. Remove the temporary address and give the adapter its real one:
   ```powershell
   Remove-NetIPAddress -InterfaceAlias "Ethernet 3" -IPAddress 192.168.0.100 -Confirm:$false
   New-NetIPAddress   -InterfaceAlias "Ethernet 3" -IPAddress 192.168.5.2 -PrefixLength 24
   Set-NetIPInterface -InterfaceAlias "Ethernet 3" -Dhcp Disabled
   ```
7. Browse to the switch's new address and check that the settings survived.
   Choose *Maintenance > Save Configuration*, or a power cycle brings the old
   settings back.

Repeat for each switch, one subnet each. Put a `192.168.0.x` address on only one
adapter at a time, or Windows has two routes to that subnet and picks one
arbitrarily. With each switch's management address on its own camera subnet,
every web interface stays reachable, and you can open them side by side to
compare settings.

##### Finding a switch whose address you have lost

Sweep its camera subnet from the host and look for an address that is neither
the adapter nor a camera:

```powershell
$subnet = "192.168.5"
$probes = 1..254 | ForEach-Object {
    [pscustomobject]@{
        IP   = "$subnet.$_"
        Ping = (New-Object System.Net.NetworkInformation.Ping).SendPingAsync("$subnet.$_", 250)
    }
}
[System.Threading.Tasks.Task]::WaitAll($probes.Ping)
$probes | Where-Object { $_.Ping.Result.Status -eq 'Success' } |
          ForEach-Object { "ALIVE $($_.IP)" }
```

The pings go out together, and the sweep takes about a second in the Windows
PowerShell that ships with Windows. Then tell the switch from the cameras by
the first half of each MAC address, from `arp -a`: Basler cameras start with
`00-30-53`, and the reference rig's switches with `28-94-01`. A web interface
answers `200` to:

```powershell
(Invoke-WebRequest -UseBasicParsing -Uri http://<ip>/ -TimeoutSec 3).StatusCode
```

NETGEAR's discovery protocol does not work on this model: on the reference rig
it got no reply from a switch that was answering on its web address at the same
moment. If the sweep finds nothing, the switch has no address on that subnet,
and a factory reset is the way back in.

#### Configure the host adapters

In Device Manager, open *Network adapters*, right-click a camera port, choose
*Properties* and the *Advanced* tab. PowerShell does the same and is easier to
repeat for each port:

```powershell
Set-NetAdapterAdvancedProperty -Name "Ethernet 3" -DisplayName "Jumbo Packet" -DisplayValue "9014 Bytes"
Set-NetAdapterAdvancedProperty -Name "Ethernet 3" -DisplayName "Receive Buffers" -DisplayValue 4096
Set-NetAdapterAdvancedProperty -Name "Ethernet 3" -DisplayName "Energy Efficient Ethernet" -DisplayValue "Disabled"
Set-NetAdapterAdvancedProperty -Name "Ethernet 3" -DisplayName "Interrupt Moderation" -DisplayValue "Disabled"
```

| Property | Value | Why |
|---|---|---|
| Jumbo Packet | 9014 bytes | 9000-byte packets plus headers |
| Receive Buffers | The driver's maximum (4096 here) | Absorbs bursts of packets |
| Energy Efficient Ethernet | Disabled | With it on, the reference rig stalled: worst frame gap 471, about 100,000 resend requests |
| Interrupt Moderation | Disabled | Measured no gain here. Kept off so every port is set the same |

A newly added port does not inherit these settings, and Energy Efficient
Ethernet and interrupt moderation default to on. Set them on every camera port.

`configure_nic.ps1 -Check` reads each camera port's receive buffers, interrupt
moderation, RSS ([CPU](#cpu)) and DPC placement, and prints PASS or WARN for
each. It changes nothing, so it is safe during a recording. Run it from an elevated PowerShell:
without elevation Windows reports RSS values that are not the adapter's
settings, and the script then leaves the RSS check unjudged. Any other tool
that reads RSS, `Get-NetAdapterRss` included, needs elevation too.

```powershell
powershell -ExecutionPolicy Bypass -File configure_nic.ps1 -Check
```

Add `-CaptureCores` with the core list the window logs on its
`[rig] capture core pool` line, and the check also judges whether the adapters'
DPCs land on the capture cores:

```powershell
powershell -ExecutionPolicy Bypass -File configure_nic.ps1 -Check -CaptureCores 10,11,12,13
```

Without `-Check` the script sets the RSS queue count, and its defaults restore
the vendor's placement (one queue, every processor). On the reference rig four
queues changed nothing measurable, so treat it as an experiment:

```powershell
powershell -ExecutionPolicy Bypass -File configure_nic.ps1 -Ports "Ethernet 3,Ethernet 4,Ethernet 5" -WhatIf
powershell -ExecutionPolicy Bypass -File configure_nic.ps1 -Ports "Ethernet 3,Ethernet 4,Ethernet 5"
```

- It needs an elevated PowerShell.
- Applying resets every port it touches, so the cameras drop off the network
  and come back within seconds. Never apply it during a recording.
- It refuses while Panopticon or one of its probes runs, and when it cannot
  read the process table. `-Force` overrides that after you have checked by
  hand.
- Name the ports with `-Ports`. Ports the script finds itself (every adapter
  that is up with a manual address, which can include an office adapter) are
  refused unless you add `-Confirm` to be asked about each one.
- `-WhatIf` prints what it would change and changes nothing.
- It prints the RSS state before and after. `NOT APPLIED on: ...` means the
  driver accepted the call and kept another queue count.

#### Give the cameras their addresses

Run the pylon IP Configurator
(`C:\Program Files\Basler\pylon\Applications\x64\bin\ipconfigurator.exe`) as
Administrator. Select each camera and give it a static address from your plan,
with mask `255.255.255.0` and gateway `0.0.0.0`. Turn DHCP off, so the camera
does not wait for a server at every boot. FLIR cameras take their addresses in
SpinView.

A camera keeps its address across reboots. If you move an adapter to another
subnet first, the cameras behind it stop answering until you renumber them too.
The IP Configurator still reaches them, because it addresses cameras by MAC
over broadcast, so it is the tool for a camera stranded on the wrong subnet.

The same from Python, for scripting a rebuild:

```python
from pypylon import pylon

mac = "0030531A2B3C"                    # the camera's MAC, from its label or the IP Configurator
tl = pylon.TlFactory.GetInstance().CreateTl("BaslerGigE")
tl.BroadcastIpConfiguration(mac, True, False, "192.168.5.3", "255.255.255.0", "0.0.0.0", "")
tl.RestartIpConfiguration(mac)          # applies without a power cycle
```

#### Let the traffic through the firewall

Discovery and streaming use UDP, and Windows blocks inbound UDP that no rule
allows. Allow inbound UDP on the camera adapters. From an elevated PowerShell,
with your own adapter names:

```powershell
New-NetFirewallRule -DisplayName "PanopticonGigE" -Direction Inbound -Action Allow -Protocol UDP -InterfaceAlias "Ethernet 3","Ethernet 4","Ethernet 5"
```

It prints the rule it made, with `Enabled : True` and `Action : Allow` among
its lines. The rule names no program, so it holds for whichever Python runs
Panopticon. A rule for one program has to name the process that owns the
sockets. `.venv\Scripts\python.exe` and `pythonw.exe` are launchers: each starts
the Python that uv installed, and that Python owns the sockets.

A block rule overrides every allow rule. If Windows once asked whether to let
Python through the firewall and the answer was Cancel, it may have added one.
Open *Windows Defender Firewall with Advanced Security*, look under
*Inbound Rules* for a Python rule whose action is Block, and delete it.

#### Check the network

```powershell
uv run probe_network.py
```

It sends a GigE Vision discovery request from every host adapter and lists the
cameras that answer each one. Every camera answers discovery whatever address
it holds, so the adapter that hears a camera is the switch it is plugged into.
The tool flags a camera whose address is outside that adapter's subnet.

Then open pylon Viewer from the Start menu. Every camera should appear, open and
show live video.

Discovery uses small packets, so it passes on a path that drops 9000-byte
packets. The packet-size sweep tests jumbo frames. It opens the cameras with
your profile's settings, so it runs at the end of
[step 7](#test-the-network-with-the-profile).

### Step 6 — make the camera settings file (.pfs)

For a Basler rig, a `.pfs` file (pylon's feature file) holds the camera
settings: frame size, pixel format, exposure, gain and the GigE packet
settings. Panopticon loads it into every camera when it opens them, and a
recording's exposure and gain come from it. FLIR profiles put these settings in
the profile's `camera:` block instead ([FLIR.md](FLIR.md#3-write-the-profile)).

Build the file in pylon Viewer with one camera open, save it once, and use it for
every camera:

1. Open one camera.
2. Set the features, using the search box of the feature tree:
   - `PixelFormat`: `Mono8`.
   - `Width` and `Height`: the frame size the profile will name.
   - `ExposureAuto` and `GainAuto`: `Off`. Automatic exposure drifts between
     cameras.
   - `ExposureTime`: under the
     [exposure ceiling](CONFIGURATION.md#exposure-ceiling). The reference rig
     records at 3000 µs.
   - `Gain`: the reference rig records at 6.0 dB. Add infrared light before
     exposure, and exposure before gain.
   - `GevSCPSPacketSize`: 9000, if every device in the path passes jumbo frames
     (step 5).
   - `GevSCPD`: the inter-packet delay. The reference rig uses 10000 for three
     cameras per port at 5 Gbit/s. Work it out for your links
     ([The camera's own link](#the-cameras-own-link)).
   - `LineInverter` on `Line1`: whatever makes the camera's rising edge the
     start of your trigger pulse. It decides which physical edge the camera
     exposes on, Panopticon never changes it, and the two shipped `.pfs` files
     set it differently because their rigs are wired differently.
3. Choose *File > Save Features* and save the file into the repository, for
   example as `configs/mono8_1920x1200.pfs`.

[CONFIGURATION.md](CONFIGURATION.md#basler-cameras-the-pfs-file) lists every
feature Panopticon depends on, what it writes itself at each acquisition, and
the reference values.

Judge an exposure on a recording, never on the live preview. The preview runs at
30 fps, where the 33 ms period has room for almost any exposure. A setting over
the ceiling looks fine there and halves the frame rate once the cameras are
triggered.

### Step 7 — write the rig profile

The profile is a YAML file in `profiles/` that describes everything besides the
camera settings: how many cameras, the frame rate, the trigger board's port and
pins, and where the data goes. Every file in `profiles/` appears in the
window's profile list under its `name`.

Start from a template in `profiles/templates/`: copy it into `profiles/`, give
the copy a name of your own, and change every value marked `SITE`.
CONFIGURATION.md lists the [templates](CONFIGURATION.md#templates), walks
through [a new rig step by step](CONFIGURATION.md#configure-a-new-rig-step-by-step),
and describes every field.

Every new rig sets its own `name`, `n_cameras` and `camera_serials`. It also
names its camera settings (`pfs_path` from step 6, or a FLIR `camera:` block),
the board's `serial_port` and `trigger_pins` (step 8) and `stim_safe_pins`. Put
`output_dir` on the largest, fastest drive, and point `board_config` at the file
that describes your printed calibration board.

`stim_safe_pins` protects your stimulation hardware. The board drives those
pins low as its first action at every boot, before it waits for Panopticon. A
laser or LED pin missing from the list floats during that wait, and a powered
laser driver can read a floating input as on. The default, `[53]`, is the
reference rig's laser pin and protects nothing on other wiring.
[CONFIGURATION.md](CONFIGURATION.md#stim_safe_pins) describes the field.

`camera_serials` names each camera by its place in the list. Without it, the
cameras are named in serial-number order. A camera that fails to appear then
renames every camera after it, and a calibration describes the wrong cameras,
unless `n_cameras` refuses the open. With the list, the refusal names the
missing camera, and a device the list does not name stays closed.
[CONFIGURATION.md](CONFIGURATION.md#camera_serials) describes the field.

A profile with a mistake does not load. Panopticon leaves it out of the list and
names the file and the field in a dialog once the window opens.

#### Test the network with the profile

For a GigE rig, test every camera's path with the profile's camera settings,
with your profile's `name` in place of `my_rig`:

```powershell
uv run probe_network.py --sweep --profile my_rig
```

`--profile` also takes a path to the profile file. The sweep opens the cameras
through the profile's backend and camera settings, then grabs frames from each
camera at packet sizes from 1500 to 9000 bytes. It sweeps FLIR GigE cameras
too, and skips USB3 cameras. It does not open the trigger board.

- A healthy path shows `complete=10/10` at every size up to 9000.
- Every size passing up to 1500 and failing from 2000 means a device in that
  camera's path is still at 1500 bytes: a switch port
  ([Configure the switches](#configure-the-switches)) or the adapter
  ([Configure the host adapters](#configure-the-host-adapters)).

Do not test jumbo frames with `ping`. The reference rig's cameras answer only
small echo requests, so `ping -f -l 8972` fails on a path that carries 9000-byte
packets, and so does `ping -l 1472`. The sweep's real grabs are the test.

Once Panopticon has opened the profile (step 9), `uv run probe_network.py --sweep`
uses it without `--profile`.

### Step 8 — flash the trigger firmware

Skip this step if the profile sets `trigger_source: external`.

`gui_app/stim_compiler.py` generates the board's sketch from the profile and
the stimulation editor's canvas, so you upload no `.ino` file yourself. The
[recording-only sketch](GLOSSARY.md#recording-only-sketch) is that sketch with
no stimulation in it: the camera
triggers, plus the boot guard that drives every `stim_safe_pins` pin low.
Panopticon flashes it for you.

> [!WARNING]
> Flashing resets the trigger board, and the laser driver input floats during
> the reset. Switch the laser off or block the beam before you launch
> Panopticon, and before Apply, Calibrate, or the first Record after an Apply.

During a flash the board sits in its bootloader with no program running, so
every pin floats, and a powered laser driver can read that as on. The boot guard
cannot help, because it runs only once the sketch starts. A pulldown resistor is
no general answer either: a driver input with a stiff internal pullup needs a
resistor too low for the board's per-pin current limit. The laser's own
interlock is the only hard gate. [WORKFLOW.md](WORKFLOW.md) says when a session
flashes the board.

At launch, and when you switch to a profile on another serial port, Panopticon
builds the recording-only sketch and compares it with the one this computer
last flashed. It flashes the board when they differ, then opens the
serial port and asks the board which sketch it runs, and flashes again once if
the board's answer disagrees. A stimulation paradigm stays in the board's flash
memory through closing the window, a power cycle and unplugging the cable. This
check means the board carries no stimulation at launch unless you Apply one in
the session.

A switch between profiles on the same port flashes nothing. If their
`trigger_pins` or `stim_safe_pins` differ, the first Calibrate or Record flashes
the board, so switch the laser off before it. Until that flash the board keeps
the previous profile's boot guard, which may leave this profile's laser pin
undriven.

To set it up:

1. Wire every camera's trigger input to a board pin, with a common ground
   ([Wiring the trigger line](#wiring-the-trigger-line)).
2. List every pin that drives a camera in the profile's `trigger_pins`. The pin
   count equals the camera count only if you wired one pin per camera, and
   nothing checks the two against each other.
3. Install the Arduino IDE, which includes `arduino-cli`, or `arduino-cli` on
   its own.
4. Install the board support once, for a Mega:
   `arduino-cli core install arduino:avr`. Another board class needs its own
   core, and the `FQBN` in `gui_app/stim_compiler.py` changed to match.
5. If `arduino-cli` is somewhere unusual, set `PANOPTICON_ARDUINO_CLI` to its
   full path. Panopticon looks at `PANOPTICON_ARDUINO_CLI`, then `PATH`, then the
   Arduino IDE's install folders.
6. Close the Arduino IDE's Serial Monitor. It holds the port, and flashing and
   recording both need it.

Find the board's port for `serial_port` in Device Manager under
*Ports (COM & LPT)*, or with:

```powershell
[System.IO.Ports.SerialPort]::GetPortNames()
```

which prints, for example, `COM3`.

The first time Panopticon opens your profile (step 9) the log shows:

```
[acq] board may carry a stim paradigm from a previous session — flashing the recording-only sketch
[acq] board flashed with the recording-only sketch; stim is off until you Apply one
```

Flashing takes about 30 seconds, and the sidebar shows `Clearing stim firmware…`
meanwhile. On later launches the flash is skipped:

```
[acq] board already carries the recording-only sketch (no stim); skipping flash
[acq] trigger board reports sketch <id>: the recording-only sketch
```

Only the flash and the stimulation editor's Apply and Test need `arduino-cli`.
Without it, the launch shows `arduino-cli was not found` with every place
Panopticon looked.

### Step 9 — first launch

Before you open the profile, check that its `serial_port` names Panopticon's
trigger board. Opening the profile resets the device on that port, and the
first time it also flashes the recording-only sketch onto it.

Then start Panopticon with your profile's `name`:

```powershell
uv run gui.py --profile my_rig
```

Panopticon remembers the profile, so later launches need only `uv run gui.py`.
On a computer where Panopticon has not opened a profile yet, a launch without
`--profile` asks you to choose one in the sidebar's list. Until you choose, it
opens no camera and no serial port, runs no hardware check and flashes nothing.
It does the same when the remembered profile, or the one `--profile` names,
does not load.

A splash panel reads *Panopticon / Loading cameras...*, and then the main window
opens with one live preview pane per camera, free-running at about 30 fps.
[OVERVIEW.md](OVERVIEW.md) names the controls.

![The main window at idle on the reference rig: nine live preview panes, with the sidebar on the right](images/main_idle.png)

The console shows what Panopticon found. Each line starts with the time, to the
millisecond, and the thread that printed it:

```
2026-09-03 19:17:35.101 [MainThread] [startup] logging to C:\Users\you\Desktop\panopticon\logs\panopticon_20260903_191735.log
2026-09-03 19:17:35.402 [MainThread] [acq] profile: 3dpose
2026-09-03 19:17:36.120 [MainThread] [cam1] 41920544 1920x1200 Mono8
2026-09-03 19:17:36.121 [MainThread] [cam1] extended (64-bit) block IDs: enabled
2026-09-03 19:17:36.121 [MainThread] [cam1] GigE stream driver: SocketDriver (SocketBufferSize=262144 KB)
...
2026-09-03 19:17:37.004 [grab0] [grab0] StartGrabbing (recording=False)
2026-09-03 19:17:37.015 [grab0] [grab0] zero-copy view OK (PaddingX=0 PaddingY=0)
2026-09-03 19:17:37.300 [session-header] [header] ===== Panopticon session header: launch =====
...
2026-09-03 19:17:38.950 [MainThread] [acq] board already carries the recording-only sketch (no stim); skipping flash
2026-09-03 19:17:38.951 [MainThread] [acq] opening teensy on COM3
```

Check in that output:

- One `[camN] <serial> <width>x<height> Mono8` line per camera, with the frame
  size you set.
- `zero-copy view OK (PaddingX=0 PaddingY=0)` for every camera.
- The `[header]` block. It lists the software version, the computer, the GPU
  and its driver, every profile field, and each camera's model, firmware and
  link. Quote it when you report a problem.

The firmware check and the serial port open about a second and a half after the
window, so the window can draw first.

The hardware check starts with the window and runs in the background. The
status bar says `Checking hardware` until it reports, and Record and Calibrate
stay disabled meanwhile. It measures the disk and the CPU encoder, and probes the NVENC
session cap as [GPU](#gpu) describes. Its report goes to the log:

- `[hw] NVENC sessions: 11 (at least — probe stopped at its limit), needed 11`
  on a nine-camera rig means the driver granted every session the probe asked
  for.
- `upload: pinned, shared CUDA context` means the pinned GPU upload passed its
  launch check.
- `Using: nvenc` names the encoder the recordings will use.

A *Hardware Check* dialog appears only when a finding needs your attention.
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) explains each one.

Every launch writes the same text to `logs\panopticon_<date>_<time>.log`. That
file is where to look when Panopticon starts from the desktop shortcut, which
has no console. Each recording and calibration folder also gets `session.log`,
the part of the log from arming the cameras to the end of encoding.

### Step 10 — desktop shortcut

```powershell
powershell -ExecutionPolicy Bypass -File make_shortcut.ps1
```

Expected:

```
Shortcut written: C:\Users\you\Desktop\Panopticon.lnk
  Target : C:\Users\you\Desktop\panopticon\.venv\Scripts\pythonw.exe
  Args   : "C:\Users\you\Desktop\panopticon\gui.py"
  WorkDir: C:\Users\you\Desktop\panopticon

No console window will appear. If the app fails to start and you
need to see why, run _launch.bat instead -- it keeps the console
open and pauses on failure so the traceback can be read.
```

The shortcut starts the environment's `pythonw.exe`, which Windows never gives a
console, so the launch opens one window. It skips `uv run`, so it does not
update the packages. Run `uv sync` yourself after `pyproject.toml` changes. If
the script prints `No venv at ...`, run `uv sync` first.

`_launch.bat` is the other way in. It runs through `uv run` with a console that
stays open, and it pauses when Panopticon exits with an error, so you can read
why it failed to start. Neither the shortcut nor `_launch.bat` passes arguments
to Panopticon, so run `uv run gui.py --profile my_rig` from PowerShell to
choose a profile at launch.

### Adding cameras to a rig that already works

1. Check the serial-number order before you install anything. Panopticon names
   the cameras `cam1` to `camN` in ascending serial order, compared as text,
   across every switch. `calibration.toml` stores those names. New cameras keep
   the old names only if their serials sort after every current one. If a new
   serial sorts between them, every camera after it is renamed. Old
   calibrations then describe the wrong cameras, and existing recordings need
   renaming or re-mapping as well. `uv run probe_network.py` lists each
   switch's cameras separately, so sort all the serials it prints together.
   The loader requires `camera_serials` in ascending order, so the same rule
   applies with it set.
2. Work out whether you need another host port and switch
   ([The host port and the switches](#the-host-port-and-the-switches)). Keeping
   the same number of cameras per port keeps the load on each port unchanged.
3. Give a new switch its own subnet, as in step 5: the adapter at `.2`, the
   cameras from `.3`, and a management address on the same subnet.
4. Configure the new switch and the new adapter as in step 5. Neither inherits
   the settings of your working ports: the switch starts at 1500-byte frames,
   and the adapter with Energy Efficient Ethernet on.
5. Wire the new cameras' trigger inputs, to new pins added to `trigger_pins` or
   fanned off existing ones. Fanning out needs no profile change, which is how
   the reference rig went from six cameras to nine. Check each pin's current
   first ([Wiring the trigger line](#wiring-the-trigger-line)).
6. Raise `n_cameras`, and add the new serials to `camera_serials`. Without
   `camera_serials`, Panopticon refuses to open any camera until `n_cameras`
   matches the cameras present, and names the ones it found. With it, a camera
   missing from the list stays closed, and the log names it.
7. Check capacity again. RAM grows with the camera count and with
   `kick_max_lag` ([RAM](#ram)), and the GPU has to grant one NVENC session per
   camera ([GPU](#gpu)).
8. Run `uv run probe_network.py --sweep --profile my_rig` again: every camera
   should complete at 9000 bytes. Then calibrate from scratch, because an old
   `calibration.toml` describes a camera set that no longer exists.

---

## 3. Verify it works

Check the software first on the simulated rig, then check on the rig that this
machine keeps up with its cameras.

### Without any cameras

The simulated rig needs no hardware at all:

```powershell
uv run gui.py --profile sim
```

On a machine installed with `uv sync --no-group rig`, run
`uv run --no-group rig gui.py --profile sim` instead
([step 4](#step-4--install-the-python-dependencies)).

Preview, Calibrate, Record, Stop and the stimulation editor's Apply then run end
to end. [SIMULATION.md](SIMULATION.md) walks through it. Point the output folder
somewhere scratch first, because the `sim` profile writes to the repository's
`data` folder.

Panopticon now remembers `sim`, so the next plain launch and the desktop
shortcut open the simulated rig. To go back to your rig, run
`uv run gui.py --profile my_rig`.

### With real cameras

On the rig, confirm that every camera's path carries 9000-byte packets:

```powershell
uv run probe_network.py --sweep --profile my_rig
```

Then record at least 3000 frames per camera in the window, 30 s at 100 fps.
Each grab thread prints a line every 1000 frames while recording (`cycle=`,
`avg_wait`, `avg_proc`, `deliv_lag`), and a `stream stats` line at the stop.

A healthy recording shows:

- `cycle` equal to the trigger period on every camera, for the whole run:
  `cycle=10.00ms` at 100 fps. A higher value means the loop falls further behind
  with every frame, and the buffer pool hides it until the pool is full.
- `avg_wait` much larger than `avg_proc`. At 100 fps and 1920x1200 a grab thread
  waits about 8.5 ms for each 0.8 ms of work.
- `deliv_lag` near zero, and not growing. It is how old a frame is when the grab
  thread retrieves it, by the camera's own clock.
- `Buffer_Underrun_Count` 0 on every camera, in the `stream stats` line printed
  at the stop. A nonzero count means the host did not keep up.
- Equal frame counts on every camera, and `forced=0`.
- No dialog after the stop. A recording that lost more than 0.5% of its
  triggers shows `Effective frame rate <rate> fps (target <rate>).` in the
  post-session dialog and in `WARNINGS.txt`.

A 60-second six-camera recording on the reference rig at 100 fps reported:

```
cycle=10.00ms on all six    avg_proc 0.80-0.90ms    avg_wait 8.40-8.62ms
deliv_lag ~0                Buffer_Underrun_Count 0 on all six
grabbed per camera: [6022, 6022, 6022, 6022, 6022, 6022]
released=6022  dropped=0  forced=0  queue_full_drops=0
```

with each camera's lag behind the leader at median 0, 95th percentile 1 and
maximum 2 frames.

`Resend_Request_Count` counts packets lost and asked for again, and
`Failed_Buffer_Count` counts frames given up on. A high resend count with no
failed buffers means the link recovers every packet, each one late
([Configure the switches](#configure-the-switches)). Treat a resend count a
thousand times the other cameras' as a fault even when every frame arrives.

Measure on an otherwise idle machine. Other programs' CPU load once moved the
cycle from 10.00 to 10.32 ms, and the delivery lag reached 5.6 s after 150 s.

Then make a short real recording and check its files.
[WORKFLOW.md](WORKFLOW.md) covers a session from start to finish.
