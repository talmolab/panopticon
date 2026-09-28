# Installation

Previous: [README.md](../README.md).

This page takes a Windows computer from nothing to a working Panopticon. It
says what the rig's hardware needs, goes through the install one step at a
time, and checks the result. Settings are in
[CONFIGURATION.md](CONFIGURATION.md): every profile field, the templates and
the sizing formulas. The messages Panopticon prints, with their causes and
fixes, are in [TROUBLESHOOTING.md](TROUBLESHOOTING.md). A FLIR rig follows
[FLIR.md](FLIR.md) for its install and first test.

This guide assumes the rig is built. The cameras are cabled and powered, and
each camera's trigger input is wired to a pin of the trigger board. Each
network switch either already holds the settings that
[Configure the switches](#configure-the-switches) lists, or you can open its
settings page to set them. Cameras, their I/O connectors and switches vary
between labs, so their own manuals cover the wiring and the switch's menus.
This page says what Panopticon needs from them, and how its tools check it.

## Before you start

Find your case in the table, and do the parts it names.

| You want to | Do |
|---|---|
| Try Panopticon with no cameras | Steps 1, 3 and 4 of [section 2](#2-install-the-software), then [Without any cameras](#without-any-cameras) |
| Set up Panopticon on a rig that is built and wired but has never run it | [Wiring the trigger line](#wiring-the-trigger-line) for the pin list, every step of section 2 in order, then section 3 |
| Set up a new computer for a rig that already works | Steps 1 to 5 (step 5 says which parts), step 7 with the old computer's profile and `.pfs` file, steps 8 to 10, [section 3](#3-verify-it-works) |
| Build a copy of the reference rig | Buy the parts in [The reference rig](#the-reference-rig), then every step of section 2 in order, then section 3 |
| Build a new rig | [Section 1](#1-what-the-rig-needs), every step of section 2 in order, then [section 3](#3-verify-it-works) |
| Build a rig with FLIR cameras | Section 1, steps 1, 3 and 4, then [FLIR.md](FLIR.md) from its Spinnaker install onward |

On this page, a rig that already works is one that has recorded with
Panopticon before.

Several steps differ between the two kinds of camera.
[GigE](GLOSSARY.md#gige-vision) cameras plug into a network switch with an
Ethernet cable. USB3 cameras plug into the computer with a USB cable. The
reference rig's cameras are GigE.

Have these at hand:

- a Windows account that can run PowerShell as Administrator
- an internet connection, for the downloads
- each camera's serial number, printed on its label (step 7)
- a list of which board pin drives which camera, and which pin drives each
  stimulation device ([Set up the board](#set-up-the-board) says how to make
  one)
- the trigger board and its USB cable (step 8)

Each step says what to type or click, what you should see, and what to do if
you see something else.

Contents:

- [Before you start](#before-you-start)
- [1. What the rig needs](#1-what-the-rig-needs)
- [2. Install the software](#2-install-the-software)
  - [Step 1: install uv](#step-1--install-uv)
  - [Step 2: install the Basler pylon SDK](#step-2--install-the-basler-pylon-sdk)
  - [Step 3: get the code](#step-3--get-the-code)
  - [Step 4: install the Python dependencies](#step-4--install-the-python-dependencies)
  - [Step 5: put the cameras on the network (GigE)](#step-5--put-the-cameras-on-the-network-gige)
  - [Step 6: make the camera settings file (.pfs)](#step-6--make-the-camera-settings-file-pfs)
  - [Step 7: write the rig profile](#step-7--write-the-rig-profile)
  - [Step 8: flash the trigger firmware](#step-8--flash-the-trigger-firmware)
  - [Step 9: first launch](#step-9--first-launch)
  - [Step 10: desktop shortcut](#step-10--desktop-shortcut)
  - [Updating Panopticon](#updating-panopticon)
  - [Adding cameras to a rig that already works](#adding-cameras-to-a-rig-that-already-works)
- [3. Verify it works](#3-verify-it-works)

---

## 1. What the rig needs

Skip this section on a rig that already works, or to try Panopticon without
cameras.

### The reference rig

Every measurement in these pages comes from this rig. Building a copy of it?
Buy these parts and go to [section 2](#2-install-the-software). The rest of
this section is for choosing other parts, and for checking your own figures
against one known-good point.

| Part | The reference rig |
|---|---|
| CPU | Intel Core Ultra 9 285K, 24 cores (8 performance, 16 efficiency) |
| RAM | 63.4 GiB |
| GPU | NVIDIA RTX 5080, 12 concurrent NVENC sessions |
| Cameras | 9 Basler a2A1920-165g5m (5 GigE), 1920x1200 Mono8 at 100 fps |
| Network | 3 managed multi-gigabit switches (NETGEAR MS510TXM), 3 cameras each, one 10 GbE host port per switch |
| Trigger board | Arduino Mega 2560 on `COM3`, six trigger pins fanned out across nine cameras, stimulation on pin 53 |
| OS | Windows 11 |

Text in code font is typed or read exactly as written. The lower-case names
with underscores, such as `trigger_pins`, are fields of the rig's profile, the
settings file you write in [step 7](#step-7--write-the-rig-profile).

### What sets the load

Panopticon runs on Windows with an NVIDIA GPU, Basler or FLIR machine-vision
cameras and a hardware TTL trigger, for any number of cameras. The load on
each part of the rig follows from the frame size, the frame rate and the camera
count. The pixel format is always Mono8, one byte per pixel, so a 1920x1200
frame is 2.3 MB. A part too small for its load loses frames while the recording
carries on, so size each part before you buy it.

This section says what each load depends on and how to choose the hardware.
[CONFIGURATION.md](CONFIGURATION.md#sizing-formulas) holds the formulas for the
network, RAM, disk and NVENC sessions, with the reference rig's figures.

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

- With one heatsink each, idle cameras levelled off at 72-78 °C. Three sat above
  76 °C, the level the camera itself flags as Critical. The camera shuts down at
  81 °C.
- The heatsinks lowered the idle temperature by 2-4 °C. A second heatsink on the
  hottest camera lowered it by another 2.7 °C.
- Recording raised the temperature by about 0.3-0.7 °C per minute with the
  heatsinks, and by 1.1-1.3 °C per minute before them. At those rates, a
  recording started at the one-heatsink idle level of about 77.5 °C would reach
  80 °C, 1 °C below the shutdown, in about 3-8 minutes (an estimate).

Plan the cooling before you mount the cameras:

- Mount each camera on metal. A metal bracket conducts heat away from the
  housing, and a plastic one leaves the camera to cool through the air alone.
- Fit heatsinks, starting with the cameras in the least airflow.
- Some recording rooms do not allow fans (the reference rig's room is one), so
  plan for passive cooling first.

During an acquisition Panopticon reads each camera's temperature and warns
before the camera reaches the shutdown temperature it reports. The reference
profile warns at 79 °C on these cameras.
[CONFIGURATION.md](CONFIGURATION.md#thermal_warn_margin_c) gives the rule and
its two settings, and [WORKFLOW.md](WORKFLOW.md#while-it-records) says where
the warning appears.

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
| Panopticon's trigger board, an Arduino Mega 2560 or a board like it ([The trigger board](#the-trigger-board)) | `trigger_source: board` (the default), `serial_port`, `trigger_pins` | Runs on the same board |
| Your own TTL source, such as a pulse generator or a DAQ | `trigger_source: external` | Not available |

Panopticon programs its board itself, through `arduino-cli`
([step 8](#step-8--flash-the-trigger-firmware)). With your own source you start
and stop the pulses yourself, and Panopticon refuses the recording if any
camera receives a frame before every camera is armed
([CONFIGURATION.md](CONFIGURATION.md#your-own-ttl-source) describes the mode).

#### Wiring the trigger line

A wiring mistake cannot be repaired after the recording: a camera that misses
triggers records views that are not simultaneous with the others.

Which pin of a camera's I/O connector is its trigger input, and which is that
input's ground, differs between models, and the camera's data sheet gives both.
[The trigger board](#the-trigger-board) draws the wiring as a whole. Panopticon
needs:

- Each camera's trigger input on a pin listed in the profile's `trigger_pins`,
  or on your own source's output.
  - On a Basler camera the input is `Line1`. Panopticon sets
    `TriggerSource=Line1` and `TriggerActivation=RisingEdge` when an
    acquisition starts.
  - On a FLIR camera the profile's `camera.trigger.line` names the input.
    FLIR.md's [Wire the trigger](FLIR.md#2-wire-the-trigger) covers
    opto-isolated and non-isolated inputs.
- A common ground: the board's GND joined to each trigger input's own ground,
  or to one ground point they all share. An opto-isolated input has a ground of
  its own, the opto ground, separate from the camera's power ground.
- A signal each input accepts. A Mega drives 5 V. An opto-isolated input is
  driven by current. The camera's I/O documentation gives its switching
  threshold and the current it draws.

The pin numbers are printed on the board beside its sockets, and sockets marked
GND are ground. Which pins to use:

| Pins | Rule |
|---|---|
| `0` and `1` | Never. They carry the serial link that configures the board. The profile loader refuses them in `trigger_pins`. |
| A pin wired to a stimulation device | Never. It belongs in `stim_safe_pins`, and the loader refuses a pin listed in both. |
| `14` to `19` on a Mega | The Mega's extra serial ports, which Panopticon's firmware leaves free, so they work. Use them last, and leave existing wiring on them as it is. |
| `A0` to `A15` on a Mega | Digital pins too. Write them as `54` to `69` in the profile and in the editor's Pin field. |

The stimulation editor refuses a stimulation block on a camera's trigger pin,
because the extra edges would advance that camera's block IDs and break the
alignment. Any other digital pin works. Write down which pin drives which
camera, because you will need the list when one camera stops triggering. The
profile lists the pins but not which camera is on each.

The board's sketch, the program it runs ([step 8](#step-8--flash-the-trigger-firmware)),
writes every pin in `trigger_pins` in one loop with interrupts off,
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
Panopticon [retires](GLOSSARY.md#retirement) it once it stalls, and the other
cameras record without it.

### Network

A GigE Vision camera sends each frame as a burst of UDP packets. A link near its
capacity drops packets, and the frame is lost unless a resend recovers it. USB3
cameras skip this section, apart from [USB3 cameras](#usb3-cameras) at its end.

One camera's rate on its link is its frame size in bits times the frame rate,
before packet headers ([Network](CONFIGURATION.md#network) gives the formula).
That rate crosses the camera's own link and the host port it shares with the
other cameras behind the same switch. GigE Vision is the protocol, and it runs
over 1, 2.5, 5, 10 and 25 Gbit/s links.

#### The camera's own link

A 1 Gbit/s link carries about 54 fps of a 1920x1200 frame, so the reference rig
uses the a2A1920-165g5m, a 5 Gbit/s model, for 100 fps. At 30 fps the same frame
needs 553 Mbit/s and fits a 1 Gbit/s camera. The frame rate moves the number
most: 1280x1024 at 100 fps still needs 1.05 Gbit/s.

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
payload. The recording looks normal, and the block-ID rate check after it
reports that camera at half the trigger rate. Keep
every camera on a port that negotiates 5 Gbit/s or more. On a 2.5 Gbit/s link,
`GevSCPD` would have to drop to about 3000 (about 8.3 ms per frame), and the rig
would need testing again at that setting.

#### The host port and the switches

Add up the cameras that share a host port and keep the total well under the
port's line rate. Three 1920x1200 cameras at 100 fps send 5.53 Gbit/s, which a
10 GbE port carries with about 45% to spare. Divide the camera count by the
cameras one port can take, rounding up, for the number of host ports.

The reference rig has nine cameras in three groups of three. Each group sits
behind its own switch, and each switch has its own 10 GbE port on the host.

A switch's camera ports have to run at the camera's link speed: a switch with
1 GbE access ports holds a 5 Gbit/s camera to 1 Gbit/s. Multi-gigabit switches
often split their ports into speed blocks. Put every camera and the uplink on
ports that run at the camera's speed, and check the speed each port negotiated,
as the switch's manual describes.

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
  [NV12 ring](GLOSSARY.md#nv12-ring), queues it for the encoder and releases the
  driver's buffer. At 1920x1200 that takes about 0.8 ms per frame, roughly 8% of
  one core at 100 fps.
- An encoder thread feeds the GPU encoder. With the default
  [pinned upload](GLOSSARY.md#pinned-upload) it first copies each frame into
  page-locked memory without holding Python's global interpreter lock (the
  [GIL](GLOSSARY.md#gil)). That costs about 1 ms of CPU per frame, outside the
  GIL.
- For GigE cameras, the network driver reassembles the packets. That work runs
  as deferred procedure calls (DPCs) on the cores the adapter's receive-side
  scaling (RSS) assigns. Three 1920x1200 cameras at 100 fps send about 78,000
  packets/s into one port, and on the reference rig one core ran at 46% DPC load
  for such a port. [INTERNALS.md](INTERNALS.md#receive-load-on-the-host) has
  the nine-camera figures.

The GIL limits how far this scales. A recording runs two threads per camera plus
the window's, about 19 busy threads at nine cameras, and only one thread runs
Python at a time. The time each thread holds the GIL per frame therefore matters
more than the number of cores
([measured](INTERNALS.md#why-a-copying-accessor-is-unaffordable)). The pinned
upload exists for this reason. The GPU encoder's own copy of each frame holds
the GIL, and on the nine-camera rig that copy made one camera drift behind the
others. [CONFIGURATION.md](CONFIGURATION.md#nvenc_upload) describes the setting.

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
When you press Record it adds them up, and it refuses the start when the total
exceeds the memory available at that moment. Running out of memory in the
middle of a recording would lose frames, so a start that does not fit is
refused. [RAM](CONFIGURATION.md#ram) gives the formula and the reference rig's
figures.

- The driver's buffer pool (`max_num_buffer` per camera) holds frames while a
  grab thread catches up. Keep it at or above `kick_max_lag`. The profile loader
  refuses a pool shallower than `kick_max_lag` in kick-out mode.
- The [NV12 ring](GLOSSARY.md#nv12-ring) holds frames on their way to the
  encoder. In the default [kick-out](GLOSSARY.md#kick-out) mode a trigger's
  frames wait in the ring until every camera has delivered that trigger, for at
  most `kick_max_lag` frames. The ring therefore grows with `kick_max_lag`
  ([what the lag limit costs](CONFIGURATION.md#kick_max_lag)), and it is usually
  the larger of the two.
- The pinned GPU upload adds a little page-locked memory.

The total has to be available when you press Record, with the operating system
and the window on top, so budget about twice that total. The reference rig's
nine cameras need about half of its 63.4 GiB. A start that fits goes ahead
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

The CPU encoder competes with the grab threads for cores, so treat it as a
fallback. [CPU_ENCODE.md](CPU_ENCODE.md) describes it, and
[CONFIGURATION.md](CONFIGURATION.md#encoder) lists the choices. ffmpeg comes
with the Python packages (`imageio-ffmpeg`), so there is nothing else to
install.

### Disk

The profile field `realtime_encode` decides the disk rate.

With real-time encoding (the default) only H.264 reaches the disk, a few MB/s
even for nine cameras ([Disk](CONFIGURATION.md#disk) gives the rate), and any
ordinary drive keeps up.

With `realtime_encode: false` every frame is written whole to `raw.bin` and
encoded after the recording. Use it only when the GPU cannot give every camera
a session and the CPU cannot encode them either. The rate is then the full
payload, 2.07 GB/s for nine 1920x1200 cameras at 100 fps. Above 1.5 GiB/s
Panopticon warns at Record, because a consumer NVMe drive falls to about
1-2 GB/s once its write cache fills. Use a drive rated for that sustained rate,
or split the cameras across drives.

The launch check warns below 500 GiB free and below 500 MiB/s measured write
speed. It measures the speed by writing 256 MiB to the output directory and
deleting it.

### Operating system

Panopticon runs on 64-bit Windows, and only Windows has been tested. Several
parts are Windows-specific: the thread placement settings (they do nothing on
other systems), `configure_nic.ps1`, `make_shortcut.ps1` and the firewall
rule in step 5. On Linux, USB3 cameras draw their buffers from usbfs, and the
launch check warns when `usbfs_memory_mb` is too small for them.

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
*Windows PowerShell* in the results and choose *Run as administrator*. Windows
asks `Do you want to allow this app to make changes to your device?`. Press
Yes. The window's title then says *Administrator*. An Administrator window
starts in `C:\Windows\system32`, so a command that runs a script from the
repository needs `cd $HOME\Desktop\panopticon` first (step 3).

If Windows asks for another account's password instead, the window runs as
that account, and `$HOME` is that account's folder. In such a window, type
your own folder in full, as in `cd C:\Users\you\Desktop\panopticon`. With no
administrator password at all, ask your IT staff before you start, because
several installers on this page need one too.

This page runs Panopticon's scripts as `uv run <script>.py`, for example
`uv run gui.py`. `uv run python gui.py` does the same thing.

### Step 1 — install uv

uv manages the Python version and every package, so nothing goes into a system
Python. The Windows command from
<https://docs.astral.sh/uv/getting-started/installation/> is:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

It prints where it installs uv, and the prompt comes back when it is done.
Close PowerShell and open it again, so that the new window finds uv. Then
check:

```powershell
uv --version
```

Expected: one line starting with `uv` and a version number, such as:

```
uv 0.11.13 (4512a3931 2026-05-10 x86_64-pc-windows-msvc)
```

A newer version is fine. If PowerShell says `The term 'uv' is not recognized`,
sign out of Windows and back in.

### Step 2 — install the Basler pylon SDK

For FLIR cameras, skip this step and follow [FLIR.md](FLIR.md#1-install).

pypylon, the Python package that drives Basler cameras, calls Basler's own
SDK, so install the SDK before the Python packages.

1. Open <https://www.baslerweb.com/en/downloads/software-downloads/> and
   download the pylon Software Suite for Windows. The reference rig runs
   version 26.04. A later version is fine.
2. Run the installer. Its default choices are fine. Where it lists camera
   interfaces, keep GigE ticked for network cameras, or USB for USB3 cameras.
3. Wait for it to finish, and restart the computer if it asks.

The SDK provides the camera driver, pylon Viewer (step 6), and the
IP Configurator and PylonGigEConfigurator (step 5). When the installer has
finished, the Start menu holds a Basler folder with pylon Viewer and pylon IP
Configurator in it.

Check the install:

```powershell
Test-Path "C:\Program Files\Basler\pylon\Runtime\x64\PylonGigEConfigurator.exe"
```

Expected: `True`. If it prints `False`, open File Explorer at
`C:\Program Files\Basler`, type `PylonGigEConfigurator.exe` in its search box,
and note the folder that holds it. Use that folder in place of
`C:\Program Files\Basler\pylon\Runtime\x64` in step 5.

### Step 3 — get the code

The repository is the folder of Panopticon's code, and `git clone` copies it
from GitHub onto this computer. Git does the copying, so check for it first:

```powershell
git --version
```

Expected: one line such as `git version 2.54.0.windows.1`. If PowerShell says
`The term 'git' is not recognized`, install Git for Windows:

1. Download it from <https://git-scm.com/download/win> and run the installer.
2. Accept every default: press Next on each screen, then Install.
3. Close PowerShell, open a new one, and run `git --version` again.

Clone the repository wherever you like. This page uses the Desktop to keep the
paths short. First check where Windows keeps your Desktop:

```powershell
[Environment]::GetFolderPath('Desktop')
```

It prints a path such as `C:\Users\you\Desktop`. On many Windows 11 computers
OneDrive keeps the Desktop, and the path then reads
`C:\Users\you\OneDrive\Desktop`. Keep the code out of OneDrive, which would
sync thousands of files as uv installs them. If the path contains `OneDrive`,
run `cd $HOME` in place of `cd $HOME\Desktop` below. Then read
`$HOME\panopticon` wherever this page says `$HOME\Desktop\panopticon`, and
`C:\Users\you\panopticon` for `C:\Users\you\Desktop\panopticon`.

Then clone:

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

The repository has no submodules, so the clone is complete. If `git clone`
failed because Git was missing, install it as above, open a new PowerShell and
run all three lines again.

After the last `cd`, the prompt reads `PS C:\Users\you\Desktop\panopticon>`.
Every later command runs from there. In a new window, go back with
`cd $HOME\Desktop\panopticon`.

### Step 4 — install the Python dependencies

On a computer with no cameras, read
[On a computer with no cameras](#on-a-computer-with-no-cameras) first.
Otherwise run:

```powershell
uv sync
```

The first run downloads a Python interpreter and about two dozen packages into a
`.venv` folder in the repository, and ends with a line like
`Installed 27 packages in 42s`. Later runs take a moment and print two lines,
such as:

```
Resolved 34 packages in 2ms
Checked 27 packages in 0.92ms
```

On the rig, check that the camera library, the window library and numpy load:

```powershell
uv run python -c "import pypylon.pylon, PyQt5, numpy; print('ok')"
```

Expected: `ok`. Anything else means the sync did not finish. Run `uv sync`
again and read its error.

Once a full `uv sync` has run, acquisition, encoding, the calibration solve and
the post-session tools need no internet. They run offline, in the environment
`uv sync` built.

#### Check the GPU driver

Skip this on a computer with no NVIDIA GPU. The GPU encoder needs NVIDIA's own
driver, and a fresh Windows install may have only a basic display driver.
Check:

```powershell
nvidia-smi --query-gpu=name,driver_version --format=csv
```

On the reference rig it prints:

```
name, driver_version
NVIDIA GeForce RTX 5080, 610.47
```

If PowerShell does not recognize `nvidia-smi`, or the name is not an NVIDIA
card, install the current driver for your card from
<https://www.nvidia.com/Download/index.aspx> and restart the computer. Task
Manager shows the same: its Performance tab lists a GPU entry that names the
NVIDIA card. [GPU](#gpu) says how to ask the driver how many cameras it can
encode.

#### On a computer with no cameras

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

### Step 5 — put the cameras on the network (GigE)

A GigE camera streams only when all of these hold:

- It and its host adapter have addresses on the same subnet. In the scheme
  below, that means the same first three numbers. Otherwise it does not appear
  at all.
- Every switch between them passes jumbo frames. Otherwise it appears and
  delivers nothing.
- Windows lets its traffic reach Panopticon. Otherwise Panopticon does not find
  it, or gets no frames from it.
- The adapter is set up for the traffic the camera sends.

USB3 cameras skip this step.

On a new computer for a rig that already works, the switches and the cameras
keep their settings. Set up only this computer's side:

1. [Check the cabling](#check-the-cabling), and
   [find the adapter names](#find-the-adapter-names).
2. Run `uv run probe_network.py` ([Check the network](#check-the-network)). It
   lists which adapter hears which cameras, and the cameras' addresses.
3. [Give the adapters their addresses](#give-the-adapters-their-addresses).
4. [Configure the host adapters](#configure-the-host-adapters).
5. [Let the traffic through the firewall](#let-the-traffic-through-the-firewall).
6. [Check the network](#check-the-network) again.

#### Check the cabling

A switch's uplink is the one cable from that switch to this computer.
Panopticon needs each uplink in a camera network port of its own on this
computer, one switch to one port
([The host port and the switches](#the-host-port-and-the-switches)). The camera
ports are usually on an add-in network card.

What you should see, with the switches and the cameras powered: the link light
of each cabled switch port is on, and so is each camera's status light. Once
the adapters are listed below, each camera port reads `Up`. A port that stays
`Disconnected` has a loose cable or a switch without power.

#### Find the adapter names

Windows gives each network port a name, such as `Ethernet 3`, and every command
in this step takes one. To list them:

```powershell
Get-NetAdapter | Format-Table Name, InterfaceDescription, Status, LinkSpeed
```

On the reference rig, part of the output reads:

```
Name                         InterfaceDescription                         Status       LinkSpeed
----                         --------------------                         ------       ---------
Ethernet                     Intel(R) Ethernet Controller I226-V          Disconnected 0 bps
Ethernet 3                   Intel(R) Ethernet Network Adapter X710-TL    Up           10 Gbps
Ethernet 4                   Intel(R) Ethernet Network Adapter X710-TL #2 Up           10 Gbps
Ethernet 5                   Intel(R) Ethernet Network Adapter X710-TL #3 Up           10 Gbps
Wi-Fi                        Intel(R) Wi-Fi 7 BE200 320MHz                Up           172 Mbps
```

The Name column holds the names the commands take. A camera port reads `Up`
once its switch is cabled and powered, at the port's speed. Put your own names
in place of `Ethernet 3` in every command below.

Leave the port that connects this computer to your building network, and
Wi-Fi, out of every command in this step. An address from this step, or DHCP
turned off, on that port cuts the computer off the building network.

The ports look alike, so find which one each switch is cabled to. Unplug one
switch's uplink cable and run the command again. The port that changes to
`Disconnected` is that switch's. Plug the cable back in, repeat for each switch,
and write the pairs down. Device Manager lists each port under the name in its
InterfaceDescription column, such as `Intel(R) Ethernet Network Adapter X710-TL #2`.

#### The addressing scheme

Give each switch its own subnet, with the host adapter at `.2` and the cameras
from `.3` upward. A subnet written `/24`, such as `192.168.5.0/24`, holds every
address that starts `192.168.5.`, and its mask is `255.255.255.0`. An address
then tells you which switch a camera is on, and a camera plugged into the wrong
switch stops answering.

The reference rig, nine cameras behind three switches:

| Subnet | Host adapter | Cameras |
|---|---|---|
| `192.168.3.0/24` | Ethernet 4 at `.2` | `.3` `.4` `.5` |
| `192.168.4.0/24` | Ethernet 5 at `.2` | `.3` `.4` `.5` |
| `192.168.5.0/24` | Ethernet 3 at `.2` | `.3` `.4` `.5` |

Panopticon names the cameras `cam1` to `camN` by their serial numbers, sorted
as text across every switch
([Adding cameras](#adding-cameras-to-a-rig-that-already-works) says why the
order matters). The reference rig's plan, with each camera's name:

| Camera | Serial | Address | Adapter |
|---|---|---|---|
| cam1 | 41920544 | `192.168.3.3` | Ethernet 4 |
| cam2 | 41920545 | `192.168.4.3` | Ethernet 5 |
| cam3 | 41920546 | `192.168.4.4` | Ethernet 5 |
| cam4 | 41920547 | `192.168.3.4` | Ethernet 4 |
| cam5 | 41920548 | `192.168.3.5` | Ethernet 4 |
| cam6 | 41920549 | `192.168.4.5` | Ethernet 5 |
| cam7 | 42017507 | `192.168.5.3` | Ethernet 3 |
| cam8 | 42017508 | `192.168.5.4` | Ethernet 3 |
| cam9 | 42019425 | `192.168.5.5` | Ethernet 3 |

To make your own plan, give each switch a subnet and each camera behind it an
address from `.3` upward. You can reuse the reference rig's subnets:
`192.168.3.0/24` for the first switch, `192.168.4.0/24` for the second, and so
on. Run `ipconfig` first. If an address it lists for the building network or
Wi-Fi starts with one of those subnets, give that switch another number. Write
the plan down with each camera's serial number and name, so a camera on the
wrong switch or with the wrong address is easy to spot.

With a single switch, pylon can assign every address. From an elevated
PowerShell:

```powershell
& "C:\Program Files\Basler\pylon\Runtime\x64\PylonGigEConfigurator.exe" auto-all
```

It gives every adapter and camera it finds compatible addresses, and then
changes adapter and system settings of its own (jumbo frames, interrupt
moderation, receive descriptors). The host-adapter settings below replace
those. It does not configure the switch, and it does not let you choose which
camera gets which address, so plan the addresses by hand once you have more
than one switch. `auto-all` replaces only
[Give the cameras their addresses](#give-the-cameras-their-addresses) and
[Give the adapters their addresses](#give-the-adapters-their-addresses). Still
set the switch's ports as in
[Configure the switches](#configure-the-switches), then do
[Configure the host adapters](#configure-the-host-adapters), the firewall rule
and [Check the network](#check-the-network).

`uv run probe_network.py` reads the same table off the wire
([Check the network](#check-the-network)), so trust its output over a written
plan after any change of cabling.

#### Configure the switches

A managed switch at its default settings passes discovery, pylon Viewer's
device list and `ping`, and drops every image packet. A managed switch has a
settings page, which usually opens in a web browser. Its manual gives the
page's address, its first password and how a computer reaches it. If someone
else set up the switches, ask them whether the settings below are saved on
every port in use.

Set these on every port in use, the uplink to the host included:

| Setting | Value | Why |
|---|---|---|
| Maximum frame size | 9216 | Image packets are 9000 bytes. |
| Flow control | Symmetric | Lets the switch pause a camera for microseconds instead of dropping its packets. |
| Energy Efficient Ethernet | Disabled | Off on the reference rig's switches. On the host adapters it caused a stall ([Configure the host adapters](#configure-the-host-adapters)). |
| Storm control | Disabled | Off on the reference rig's switches, which is the setting its measurements were made with. |

Several cameras burst into one uplink at once. With flow control, 802.3x PAUSE
frames ask a camera to wait while the switch's queue drains. Without it the
switch drops the packet, the camera resends it, and that camera's frame
completes late, which looks like a slow camera. On the reference rig, flow
control cut resend requests from thousands per camera to about ten
([measured](INTERNALS.md#resends-driver-choice-and-flow-control)).

At the end of each recording the grab threads print each camera's stream
counters. `Resend_Request_Count` in
the thousands with `Buffer_Underrun_Count` at 0 means loss in the network, and
flow control is the first thing to check.

Save the settings in the switch, as its manual describes, so a power cycle
keeps them. A switch reset to its factory settings, or a new one, starts at
1500-byte frames. No Panopticon tool reads a switch's settings.
[Test the network with the profile](#test-the-network-with-the-profile) shows
a port that does not pass 9000-byte packets. The resend counts above show a
port without flow control.

#### Give the adapters their addresses

Give each camera port of this computer the `.2` address on its switch's subnet,
and turn off DHCP on it. From an elevated PowerShell, with your own adapter
name and subnet:

```powershell
New-NetIPAddress   -InterfaceAlias "Ethernet 3" -IPAddress 192.168.5.2 -PrefixLength 24
Set-NetIPInterface -InterfaceAlias "Ethernet 3" -Dhcp Disabled
```

`New-NetIPAddress` prints two blocks that list the address, and
`Set-NetIPInterface` prints nothing. An error that the address or object
already exists means the port holds that address already, which is fine.
Repeat for each camera port.

`Get-NetIPAddress -InterfaceAlias "Ethernet 3"` shows the addresses a port
holds. To remove one it should not hold, such as an address from an earlier
plan, put that address in place of `192.168.9.2`:

```powershell
Remove-NetIPAddress -InterfaceAlias "Ethernet 3" -IPAddress 192.168.9.2 -Confirm:$false
```

#### Configure the host adapters

In Device Manager, open *Network adapters*, right-click a camera port (listed
under its InterfaceDescription), choose *Properties* and the *Advanced* tab.
PowerShell does the same and is easier to repeat for each port. From an
elevated PowerShell:

```powershell
Set-NetAdapterAdvancedProperty -Name "Ethernet 3" -DisplayName "Jumbo Packet" -DisplayValue "9014 Bytes"
Set-NetAdapterAdvancedProperty -Name "Ethernet 3" -DisplayName "Receive Buffers" -DisplayValue 4096
Set-NetAdapterAdvancedProperty -Name "Ethernet 3" -DisplayName "Energy Efficient Ethernet" -DisplayValue "Disabled"
Set-NetAdapterAdvancedProperty -Name "Ethernet 3" -DisplayName "Interrupt Moderation" -DisplayValue "Disabled"
```

Each line prints nothing when it works. The names above are the ones the
reference rig's Intel adapters use. If a line reports no matching display name
or value, list the ones your driver uses, and put them in its place:

```powershell
Get-NetAdapterAdvancedProperty -Name "Ethernet 3" | Format-Table DisplayName, DisplayValue
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

For a WARN on receive descriptors or interrupt moderation, set that property
as in the table above and run the check again. A WARN on RSS that says
`not judged` means the window was not elevated. One that reads `enabled=False`
needs `Enable-NetAdapterRss -Name "Ethernet 3"` from an elevated PowerShell.

After the first launch (step 9), add `-CaptureCores` with the core list the
window logs on its `[rig] capture core pool` line, and the check also judges
whether the adapters' DPCs land on the capture cores:

```powershell
powershell -ExecutionPolicy Bypass -File configure_nic.ps1 -Check -CaptureCores 10,11,12,13
```

Without `-CaptureCores` the DPC line reads INFO and judges nothing. With it, a
WARN on DPC affinity says that nothing keeps the DPCs off the capture cores.
It needs no change on a first install. The script never moves DPCs, and the
profile's `capture_core_exclude` keeps the capture threads off the cores that
carry them ([CPU](#cpu)).

Without `-Check` the script sets the RSS queue count, and its defaults restore
the vendor's placement (one queue, every processor). On the reference rig four
queues changed nothing measurable. Skip this on a first install. It is for
testing one change at a time on a rig that already works:

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

FLIR cameras take their addresses in SpinView. For Basler cameras:

1. Open the Start menu's Basler folder, right-click *pylon IP Configurator*
   and choose *Run as administrator*.
2. The list shows one row per camera, with its model, serial number, MAC
   address and current address. Find each camera by its serial number in your
   plan.
3. Select the row, tick *Static IP*, and type the address from your plan, the
   mask `255.255.255.0` and the gateway `0.0.0.0`. Untick *DHCP*, so the camera
   does not wait for a server at every boot.
4. Press *Save*. The row then shows the new address.

A camera keeps its address across reboots. If you move an adapter to another
subnet first, the cameras behind it stop answering until you renumber them too.
The IP Configurator still reaches them, because it addresses cameras by MAC
over broadcast, so it is the tool for a camera stranded on the wrong subnet.

You can also set an address from Python, which helps when you rebuild a rig
often. This is optional. Type `uv run python` in the repository folder, paste
the lines at the `>>>` prompt with your camera's MAC and address, and type
`exit()` when done:

```python
from pypylon import pylon

mac = "0030531A2B3C"                    # the camera's MAC, from its label or the IP Configurator
tl = pylon.TlFactory.GetInstance().CreateTl("BaslerGigE")
tl.BroadcastIpConfiguration(mac, True, False, "192.168.5.3", "255.255.255.0", "0.0.0.0", "")
tl.RestartIpConfiguration(mac)          # applies without a power cycle
```

#### Let the traffic through the firewall

Discovery and streaming use UDP, and Windows can block inbound UDP that no rule
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

For each adapter that hears a camera, it prints the adapter's name and subnet,
then one line per camera: its serial number, address, MAC address and model.
On the reference rig, shortened to one adapter:

```
=== GVCP discovery, per host adapter ===
Answers arrive from every camera on the segment, whatever its address,
so the adapter that hears a camera is the switch it is plugged into.

Ethernet 3  (192.168.5.0/24)
  42017507  ip=192.168.5.3      mac=00305339056A  a2A1920-165g5m
  42017508  ip=192.168.5.4      mac=00305339056B  a2A1920-165g5m
  42019425  ip=192.168.5.5      mac=003053390894  a2A1920-165g5m

=== summary ===
  9 camera(s) answered discovery
  every camera is on the correct subnet for its switch
```

On a healthy network the summary ends with
`every camera is on the correct subnet for its switch`. A camera line that ends
in `<-- WRONG SUBNET FOR THIS SWITCH` needs a new address
([Give the cameras their addresses](#give-the-cameras-their-addresses)). When no
camera answers at all, check the power, the cables, the link lights and the
firewall rule.

Then open pylon Viewer from the Start menu. Every camera appears in its device
list, on the left, under its model and serial number. Double-click one to open
it, and press *Continuous Shot* in the toolbar to see live video. Close pylon
Viewer before you launch Panopticon, because a camera that another program
holds does not open.

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

With the reference rig's cameras, frame size and wiring, you can skip pylon
Viewer: use the shipped file, `configs/mono8_1920x1200.pfs`, as `pfs_path` in
step 7. Otherwise build the file in pylon Viewer with one camera open, save it
once, and use it for every camera:

1. Open pylon Viewer from the Start menu, and double-click one camera in its
   device list. The camera's features appear as a tree, with a search box.
   pylon Viewer hides some features at its lower visibility levels. If a search
   finds nothing, set the visibility to Guru and search again.
2. Set the features, using the search box of the feature tree. pylon Viewer
   shows each feature by its display name, given here in brackets:
   - `PixelFormat` (Pixel Format): `Mono8`.
   - `Width` and `Height`: the camera's full sensor size, 1920 and 1200 on the
     reference cameras, unless you need a smaller frame. Write the same two
     numbers into the profile's `frame_width` and `frame_height` in step 7.
   - `ExposureAuto` and `GainAuto` (Exposure Auto, Gain Auto): `Off`.
     Automatic exposure drifts between cameras.
   - `ExposureTime` (Exposure Time): under the
     [exposure ceiling](CONFIGURATION.md#exposure-ceiling). The reference rig
     records at 3000 µs.
   - `Gain`: the reference rig records at 6.0 dB. Add infrared light before
     exposure, and exposure before gain.
   - `GevSCPSPacketSize`: 9000, if every device in the path passes jumbo
     frames (step 5). pylon Viewer calls it Packet Size.
   - `GevSCPD`: the inter-packet delay. The reference rig uses 10000 for three
     cameras per port at 5 Gbit/s. Work it out for your links
     ([The camera's own link](#the-cameras-own-link)). pylon Viewer calls it
     Inter-Packet Delay.
   - `LineInverter` (Line Inverter) on `Line1`: set Line Selector to `Line1`
     first. It decides which physical edge of the trigger pulse the camera
     exposes on, and Panopticon never changes it. With a board pin wired
     straight to `Line1`, as on the reference rig, leave it off, as
     `configs/mono8_1920x1200.pfs` does. The 3dface rig's file,
     `mono8_mono.pfs`, turns it on because that rig is wired differently.
3. Choose *File > Save Features* and save the file into the repository's
   `configs` folder under a name of your own, such as `configs/my_rig.pfs`. The
   folder already holds the two shipped files, `mono8_1920x1200.pfs` (the
   reference rig's) and `mono8_mono.pfs` (the 3dface rig's).
4. Close pylon Viewer, so the camera is free for Panopticon.

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
and describes every field. For Basler GigE cameras, for example:

```powershell
Copy-Item profiles\templates\basler_gige.yaml profiles\my_rig.yaml
notepad profiles\my_rig.yaml
```

Notepad opens the copy. Each line holds a field, a colon and a value, and
everything after a `#` is a comment. Keep the spaces at the start of each line
as they are, because YAML reads the indentation to tell which block a field
belongs to. Save the file when you are done.

Change the line `name: basler_gige` to `name: my_rig`. Use the same word as the
file name, so the two never disagree. The window's list and `--profile` both
use the name inside the file, not the file's name.

On a new computer for a rig that already works, copy the profile and the
`.pfs` file from the old computer's `profiles` and `configs` folders instead.

Each value marked `SITE` comes from your rig:

- `name`: the word you chose above.
- `pfs_path`: the `.pfs` file from step 6. A FLIR rig writes a `camera:` block
  instead.
- `frame_width` and `frame_height`: the `Width` and `Height` in that file.
- `n_cameras` and `camera_serials`: how many cameras, and their serial numbers
  (below).
- `trigger_rate_limit`: the camera's maximum frame rate, from its data sheet.
  It is the 165 in the a2A1920-165g5m's name
  ([trigger_rate_limit](CONFIGURATION.md#trigger_rate_limit)).
- `serial_port`: leave it for step 8, which finds the board's port.
- `trigger_pins`: the board pins wired to the cameras' trigger inputs, from
  your pin list. Step 8 says how to make the list, and you can fill in the
  field then.
- `stim_safe_pins`: every pin wired to a stimulation device (below).
- `output_dir`: where sessions go (below).
- `board_config`: the file that describes your printed calibration board.
  Keep `configs/boards/charuco_8x8_15mm.yaml` for a copy of the reference
  rig's board ([Board config](CONFIGURATION.md#board-config)).
- `metadata_defaults`: your lab's defaults for the sidebar's fields.

The template sets `stim_safe_pins: []`, which holds no pin low. If a
stimulation device is wired to the board, put its pin in the list, as in
`stim_safe_pins: [53]` on the reference wiring. The board holds those pins low
from the first line of its sketch, and step 8 says what that leaves uncovered.
A profile with no `stim_safe_pins` line uses the default, and the
default, `[53]`, is the reference rig's stimulation pin and protects nothing on
other wiring ([stim_safe_pins](CONFIGURATION.md#stim_safe_pins)). A trigger
board other than the Mega 2560 also sets `board_fqbn` and `board_max_pin`
([The trigger board](#the-trigger-board)).

Put `output_dir` on the largest, fastest drive. Write a Windows path with
forward slashes, as in `output_dir: D:/panopticon_data`, and never inside
double quotes with backslashes: YAML reads a backslash in double quotes as the
start of a special character.

List every camera's serial number in `camera_serials`, quoted, in ascending
text order. The first entry is cam1. A missing camera then refuses the open by
name, and a device the list does not name stays closed.
[camera_serials](CONFIGURATION.md#camera_serials) says what goes wrong without
it. Each serial number is printed on the camera's label, and
`uv run probe_network.py` ([Check the network](#check-the-network)) prints it
beside the camera's address.

At the next launch (step 9) the profile appears in the list at the top of the
sidebar under its `name`. A profile with a mistake does not load. Panopticon
leaves it out of the list and names the file and the field in a dialog once
the window opens.

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

Flashing is writing a new program into the board's memory. You do not flash
anything yourself in this step. Panopticon flashes the board at the first
launch, in step 9. This step checks the board and its wiring, lists its pins in
the profile, installs the tool Panopticon flashes with, and finds the board's
port.

`gui_app/stim_compiler.py` generates the board's sketch, the program the board
runs, from the profile and the stimulation editor's canvas, so you upload no
`.ino` file yourself. The
[recording-only sketch](GLOSSARY.md#recording-only-sketch) is that sketch with
no stimulation in it: the camera
triggers, plus the boot guard that drives every `stim_safe_pins` pin low.
Panopticon flashes it for you.

> [!WARNING]
> Panopticon's responsibility ends at the trigger board's TTL outputs. What you
> connect to a stimulation pin, and making that device safe, is your lab's
> responsibility, for a laser, an LED or any other device. Every pin of the
> board floats for a moment whenever the board resets or is flashed.
> `stim_safe_pins` holds the listed pins low from the first line of the sketch.
> A stimulation paradigm reaches the board only through Apply, or a Test of a
> changed canvas.

[When the board resets](WORKFLOW.md#when-the-board-resets) lists every case.
During a flash the board runs its bootloader, the small program that takes in
a new sketch. No sketch runs then, so every pin floats. The boot guard cannot
help, because it runs only once the sketch starts.

A floating pin is driven neither LOW nor HIGH, so a device wired to it may
read it as on. Every launch opens the board's port and resets it, the first
launch in step 9 included. Before that first launch, agree with whoever is in
charge of each wired stimulation device how it stays safe while the pins float.

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
the board. Until that flash the board keeps the previous profile's boot guard,
which may leave this profile's stimulation pins undriven.

#### The trigger board

The Arduino Mega 2560 is the tested board, and the default. The sketch
Panopticon generates is plain Arduino code, which `arduino-cli` compiles and
uploads, so another Arduino-compatible board can take the Mega's place. It
needs:

- An `arduino-cli` core, the package that compiles and uploads for its board
  family, such as `arduino:avr` for the Mega.
- 5 V logic, or a level shifter on its outputs, when the camera and
  stimulation inputs need 5 V. A 3.3 V board's high level can fall below an
  input's switching threshold.
- A digital pin for every pin in `trigger_pins` and `stim_safe_pins`. Pins 0
  and 1 are refused on every board.
- A USB serial port that either restarts the sketch when Panopticon opens it,
  as the Mega's does, or leaves the running sketch to answer the `RDY`
  handshake. Panopticon waits for that answer at every start.

For such a board, set [`board_fqbn`](CONFIGURATION.md#board_fqbn) to its name
in `arduino-cli`, and [`board_max_pin`](CONFIGURATION.md#board_max_pin) to its
highest digital pin. `arduino-cli board listall` lists the names of the boards
each installed core supports. When an upload overruns its time limit,
Panopticon waits for the board's flashing tool to finish before it says what
to do. It knows the tools of the common cores: `avrdude`, `bossac`,
`esptool`, `picotool`, the Teensy loader, `dfu-util`, `STM32_Programmer_CLI`
and `openocd`. With another tool it cannot tell whether a write is still
running, and its message says so.

![The trigger board wired to the computer, three cameras and a stimulation device](images/trigger_wiring.png)

Everything right of the dashed line varies by lab: the cameras' I/O
connectors, the camera network and the stimulation device.

The log calls the trigger board `teensy`, and tags its lines `[teensy]`,
whatever board it is.

#### Set up the board

1. Make the pin list, if the rig came without one. Follow each wire from its
   board socket to where it ends: a camera's I/O cable, a stimulation device or
   a ground. Write the socket's number beside the camera's serial number, or
   beside the device's name. If the wires are bundled or covered, ask whoever
   wired the rig for the list. Do not unplug wires to find out.
2. Check the ground ([Wiring the trigger line](#wiring-the-trigger-line)). At
   least one wire leaves a socket marked GND and joins the cameras' trigger
   grounds, and the stimulation device's ground too, as in the diagram above.
   If you cannot trace it, ask whoever wired the rig, and do not rewire it
   yourself.
3. List every pin that drives a camera in the profile's `trigger_pins`, and
   every pin that drives a stimulation device in `stim_safe_pins`. The pin
   count equals the camera count only if you wired one pin per camera, and
   nothing checks the two against each other. A camera on a pin the list
   leaves out receives no triggers, and the test recording in
   [section 3](#with-real-cameras) shows it.
4. Install the Arduino IDE from <https://www.arduino.cc/en/software>, which
   includes `arduino-cli`, or `arduino-cli` on its own.
5. Install the board support once, for a Mega:
   `arduino-cli core install arduino:avr`. It downloads the Mega's support
   files, or reports that they are already installed. Another board needs its
   own core, and `board_fqbn` and `board_max_pin` in the profile
   ([The trigger board](#the-trigger-board)).

   If PowerShell says `The term 'arduino-cli' is not recognized`, you have the
   Arduino IDE's copy, which PowerShell does not find by name. Run it by its
   full path:

   ```powershell
   & "C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe" core install arduino:avr
   ```

   If that path is not found, the IDE was installed for your account alone,
   and its copy is under your own folder:

   ```powershell
   & "$env:LOCALAPPDATA\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe" core install arduino:avr
   ```

   Panopticon searches both folders itself.
6. If `arduino-cli` is somewhere unusual, set `PANOPTICON_ARDUINO_CLI` to its
   full path. Panopticon looks at `PANOPTICON_ARDUINO_CLI`, then `PATH`, then the
   Arduino IDE's install folders. To set the variable, press the Windows key,
   type `environment variables`, open *Edit environment variables for your
   account*, press *New*, and give the name and the path.
7. Close the Arduino IDE's Serial Monitor. It holds the port, and flashing and
   recording both need it.

Then find the board's port, the name Windows gives the USB connection, for
`serial_port`:

1. Plug the trigger board into a USB port of this computer, if it is not
   plugged in yet.
2. Right-click the Start button and choose *Device Manager*. Open
   *Ports (COM & LPT)*. On the reference rig the board is listed as
   `Arduino Mega 2560 (COM3)`, and `COM3` is what `serial_port` takes. A board
   that is not a genuine Arduino may be listed as `USB-SERIAL CH340 (COM4)` or
   `USB Serial Device (COM4)` instead.
3. To be sure which entry is the board, unplug its cable and watch the entry
   disappear, then plug it back in.

The board's model is printed on the board itself. A board other than a Mega
2560 needs `board_fqbn` and `board_max_pin`
([The trigger board](#the-trigger-board)).

PowerShell lists the port names too, without saying which device is which:

```powershell
[System.IO.Ports.SerialPort]::GetPortNames()
```

which prints, for example, `COM1` and `COM3`. Put the board's port in
`serial_port`, and the pins you wired in `trigger_pins`, in the profile from
step 7.

The first time Panopticon opens your profile (step 9) the log shows:

```
[acq] board may carry a stim paradigm from a previous session — flashing the recording-only sketch
[acq] board flashed with the recording-only sketch; stim is off until you Apply one
```

These lines appear at the first launch on every computer, with a new board
too, and need no action. The sidebar shows `Clearing stim firmware…` while the
board flashes ([how long](OVERVIEW.md#16-state)). On later launches the flash
is skipped:

```
[acq] board already carries the recording-only sketch (no stim); skipping flash
[acq] trigger board reports sketch <id>: the recording-only sketch
```

Only the flash and the stimulation editor's Apply and Test need `arduino-cli`.
Without it, the launch shows `arduino-cli was not found` with every place
Panopticon looked.

### Step 9 — first launch

Before you open the profile:

- Check that its `serial_port` names Panopticon's trigger board. Opening the
  profile resets the device on that port, and the first time it also flashes
  the recording-only sketch onto it.
- With a stimulation device wired to the board, read
  [the warning in step 8](#step-8--flash-the-trigger-firmware) first. The
  launch resets the board, and every pin floats for a moment.
- Close pylon Viewer, SpinView and any other program that holds the cameras.

Then launch, and compare each screen with the pictures:

1. Start Panopticon with your profile's `name` in place of `my_rig`:

   ```powershell
   uv run gui.py --profile my_rig
   ```

   A splash panel opens and names each startup step, down to the camera it is
   opening.

   ![The splash screen naming the step under way](images/launch_splash.png)

   If PowerShell prints an error instead, or a `Panopticon failed to start`
   dialog appears, look the message up in
   [TROUBLESHOOTING.md](TROUBLESHOOTING.md#installing-and-launching).

2. The main window opens with one pane per camera. Each pane shows live video,
   free-running at about 30 fps, with its frame rate in green at its foot. The
   status bar along the bottom reads
   `Checking hardware: Record and Calibrate are available once it reports`,
   and the Calibrate and Record toggles stay grey until then.

   ![The window while the launch hardware check runs](images/launch_checking_hardware.png)

   The pictures show a later launch on the reference rig, with the fields
   filled in from its last session, and a test folder as the output folder.
   On your first launch the fields are empty, apart from your profile's
   defaults. The board is also flashed, and
   the state label at the foot of the sidebar reads `Clearing stim firmware…`
   in place of `IDLE` until the flash is done
   ([how long](OVERVIEW.md#16-state)). If a pane stays black, or a
   `Camera Error` dialog appears, see
   [Opening the cameras](TROUBLESHOOTING.md#opening-the-cameras).

3. Wait for the check to report. On the reference rig the window opens about
   17 s after the launch, one camera every two seconds or so, and the check
   reports about 5 s later. The status bar then reads
   `Hardware check done: encoding with nvenc`, and Calibrate and Record turn
   from grey to white. `nvenc` is the GPU encoder. `x264` in its place means
   the CPU encodes ([CPU_ENCODE.md](CPU_ENCODE.md)).

   ![The window ready, every pane live](images/launch_ready.png)

4. A *Hardware Check* dialog appears only when a finding needs your attention.
   It shows the whole report, with the findings under *Warnings* at its end.
   Look each one up in [The hardware check](TROUBLESHOOTING.md#the-hardware-check),
   then press OK.

   ![A Hardware Check dialog with one warning](images/hardware_check_warning.png)

   This example comes from the reference rig. Its warning is the
   `nvenc_upload: pinned needs the launch check` row of that table.

Panopticon remembers the profile, so later launches need only `uv run gui.py`.
A launch without `--profile`, on a computer where Panopticon has not opened a
profile yet, shows this dialog instead:

![The dialog a computer with no chosen profile shows](images/first_launch_choose_profile.png)

Until you choose, Panopticon opens no camera and no serial port, runs no
hardware check and flashes nothing. The same dialog appears when the
remembered profile, or the one `--profile` names, does not load. Press OK. The
window behind it has no camera panes, Calibrate, Record and Solve are grey,
and the state label reads `Choose a profile`:

![The window with no profile chosen](images/first_launch_no_profile.png)

The profile list is the empty box directly under Metadata. Click it, and the
list opens:

![The profile list open](images/sidebar_profile_dropdown.png)

Click your profile's name. The window then opens its cameras as in items 2 to
4 above. If your profile is not in the list, it did not load, and a
`Rig profiles` dialog names the file and the field at fault.

#### Read the console

The PowerShell window you launched from shows what Panopticon found. Each line
starts with the time, to the millisecond, and the thread that printed it:

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

`teensy` in the last line is the trigger board
([The trigger board](#the-trigger-board)). Each camera's grab thread is named
from 0, so `grab0` is cam1, `grab1` is cam2, and so on.

Check in that output:

- One `[camN] <serial> <width>x<height> Mono8` line per camera, with the frame
  size you set.
- `zero-copy view OK (PaddingX=0 PaddingY=0)` for every camera.
- The `[header]` block ([WORKFLOW.md](WORKFLOW.md#the-log) lists what it
  holds). Quote it when you report a problem.

The firmware check and the serial port open about a second and a half after the
window, so the window can draw first.

The hardware check starts with the window and runs in the background. It
measures the disk and the CPU encoder, and probes the NVENC session cap as
[GPU](#gpu) describes. Its report goes to the log:

- `[hw] NVENC sessions: 11 (at least — probe stopped at its limit), needed 11`
  on a nine-camera rig means the driver granted every session the probe asked
  for.
- `upload: pinned, shared CUDA context` means the pinned GPU upload passed its
  launch check. `upload: host (the pinned upload check did not pass)` means it
  did not, and the dialog of item 4 says so.
- `Using: nvenc` names the encoder the recordings will use.

Every launch writes the same text to `logs\panopticon_<date>_<time>.log`. That
file is where to look when Panopticon starts from the desktop shortcut, which
has no console. Each recording and calibration folder also gets `session.log`,
the part of the log from arming the cameras to the end of encoding.

### Step 10 — desktop shortcut

```powershell
powershell -ExecutionPolicy Bypass -File make_shortcut.ps1
```

Expected, on an environment made by uv:

```
The venv's pythonw.exe is a console program; the shortcut uses
CPython's GUI venv launcher, copied to C:\Users\you\Desktop\panopticon\.venv\Scripts\panopticonw.exe
Shortcut written: C:\Users\you\Desktop\Panopticon.lnk
  Target : C:\Users\you\Desktop\panopticon\.venv\Scripts\panopticonw.exe
  Args   : "C:\Users\you\Desktop\panopticon\gui.py"
  WorkDir: C:\Users\you\Desktop\panopticon

No console window will appear. If the app fails to start and you
need to see why, run _launch.bat instead -- it keeps the console
open and pauses on failure so the traceback can be read.
```

A Panopticon icon appears on the desktop. Double-click it: the splash appears,
then the main window, with no console window beside them. The window opens the
profile you opened last.

The shortcut needs a launcher that Windows never gives a console. In an
environment made by uv, `.venv\Scripts\pythonw.exe` is a console program: it
opens a console window before Panopticon's, and the taskbar entry then belongs
to that console. The script checks, and in that case copies CPython's own
windowless venv launcher to `.venv\Scripts\panopticonw.exe` and points the
shortcut at it, so the launch opens one window. Run the script again after
recreating the environment. The shortcut skips `uv run`, so it does not update
the packages. Run `uv sync` yourself after an update
([Updating Panopticon](#updating-panopticon)). If the script prints
`No venv at ...`, run `uv sync` first.

`_launch.bat` is the other way in. It runs through `uv run` with a console that
stays open, and it pauses when Panopticon exits with an error, so you can read
why it failed to start. It passes its arguments on to Panopticon, so
`.\_launch.bat --profile my_rig`, run in the repository folder, chooses a
profile at launch. The shortcut passes none.

### Updating Panopticon

Close Panopticon first. Then, in PowerShell in the repository folder:

```powershell
git pull
uv sync
```

`git pull` fetches the new code and prints the files it changed, or
`Already up to date.` `uv sync` then installs any package the new code needs.
If `uv sync` rebuilt the environment, run `make_shortcut.ps1` again (step 10)
so the shortcut points at the new one. Profiles and `.pfs` files you saved
under names of your own stay as they are.

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
3. Give a new switch its own subnet, as in step 5: the adapter at `.2` and the
   cameras from `.3`.
4. Configure the new switch and the new adapter as in step 5. Neither inherits
   the settings of your working ports: the switch starts at 1500-byte frames,
   and the adapter with Energy Efficient Ethernet on.
5. Wire the new cameras' trigger inputs, to new pins added to `trigger_pins` or
   fanned off existing ones. Fanning out needs no profile change, which is how
   the reference rig went from six cameras to nine. Check each pin's current
   first ([Wiring the trigger line](#wiring-the-trigger-line)).
6. Raise `n_cameras`, and add the new serials to `camera_serials`
   ([step 7](#step-7--write-the-rig-profile)).
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

The window opens with the three simulated cameras' panes, and the launch runs
as in [step 9](#step-9--first-launch). Preview, Calibrate, Record, Stop and the
stimulation editor's Apply then run end to end.
[SIMULATION.md](SIMULATION.md) walks through it. Point the output folder
somewhere scratch first
([WORKFLOW.md section 3](WORKFLOW.md#3-set-the-output-directory)), because the
`sim` profile writes to the repository's `data` folder.

Panopticon now remembers `sim`, so the next plain launch and the desktop
shortcut open the simulated rig. To go back to your rig, run
`uv run gui.py --profile my_rig`.

### With real cameras

On the rig, confirm that every camera's path carries 9000-byte packets:

```powershell
uv run probe_network.py --sweep --profile my_rig
```

Then record at least 3000 frames per camera in the window, 30 s at 100 fps.
Click the Record toggle in the sidebar once to start, and again to stop
([WORKFLOW.md section 8](WORKFLOW.md#8-record) describes a recording). Each
grab thread prints a line every 1000 frames while recording (`cycle=`,
`avg_wait`, `avg_proc`, `deliv_lag`), and a `stream stats` line at the stop.
The lines appear in the PowerShell window and in the launch's log file in
`logs\`. From an 18 s recording on the reference rig, with each line's time and
thread left off, cam1's lines and the recording's totals read:

```
[grab0] frames=1000 timeouts=7 avg_wait=9.03ms avg_proc=0.96ms qsize=1 | deliv_lag=-0.000s copy=0.73 submit=0.02 disp=0.05 rel=0.06 cycle=9.98ms
[grab0] exiting: frames=1846 timeouts=15 drops=0 rearms=0
[grab0] stream stats: {'Total_Buffer_Count': 1846, 'Failed_Buffer_Count': 0, 'Buffer_Underrun_Count': 0, 'Total_Packet_Count': 479960, 'Resend_Request_Count': 0, 'Resend_Packet_Count': 0, 'buffers_total': 1846, 'buffers_failed': 0, 'buffers_underrun': 0, 'resend_requests': 0, 'ReceiveThreadPriorityOverride': False, 'ReceiveThreadPriority': 15, 'GevSCFTD': 0, 'GevSCBWR': 10, 'GevSCBWRA': 3}
[sync] released=16614 dropped=0 forced=0 queue_full_drops=0
[cam1] stop summary: frames_retrieved=1846, frame_count=1846, failed_grabs=0, drops=0, ring_full_drops=0, rearms=0, source_down_stalls=0, source_down_rearms=0, frames_before_barrier=0, retired=no
```

The `exiting` and `stop summary` lines give each camera's frame count. The
`[sync] released=` line gives `forced=` for the whole recording.

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
- Equal frame counts on every camera, and `forced=0`
  ([forced drops](GLOSSARY.md#forced-drop)).
- No warning in the status bar after the stop. A recording that lost more
  than 0.5% of its triggers to kick-out reports its
  [effective frame rate](GLOSSARY.md#effective-frame-rate) in
  `WARNINGS.txt`.

A 60-second six-camera recording on the reference rig at 100 fps, summed up
from those lines across all six cameras:

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

Next: [OVERVIEW.md](OVERVIEW.md) names every control in the window you just opened.
