# Glossary

The terms the Panopticon documentation uses, grouped by topic. Other pages link a
term's first use to its entry here.

- [Triggering and alignment](#triggering-and-alignment)
- [Capture and encoding](#capture-and-encoding)
- [Network and camera settings](#network-and-camera-settings)
- [Calibration](#calibration)
- [Stimulation](#stimulation)
- [Files, settings and logs](#files-settings-and-logs)

## Triggering and alignment

### Trigger

One TTL pulse that makes every camera expose at once. "Trigger 500" names one instant
in every camera's video.

### Trigger source

Where the triggers come from, set by `trigger_source` in the rig profile. `board`, the
default, is Panopticon's [trigger board](#trigger-board). `external` is a pulse
generator or DAQ that you run. With `external`, Panopticon opens no serial port and arms
every camera, then asks you to start the source, and at the end to stop it. It refuses
the recording if a frame arrives before every camera is armed.

### Trigger board

Panopticon's default trigger source: an Arduino Mega 2560 on a serial port. Panopticon
generates the board's firmware and flashes it with `arduino-cli`. The same board runs
the stimulation [paradigm](#paradigm), so the stimulus and the frames share one clock.

### Block ID

The frame number a camera attaches to each frame it acquires. Panopticon treats it as
the trigger number: block ID 1 is the first trigger of an acquisition. Basler cameras
supply the block ID of their GigE Vision or USB3 Vision stream. The FLIR backend uses
the camera's frame ID or its count of trigger edges (`camera.flir.block_id_source`).
`blockids.npy` holds one block ID per recorded frame.

A camera that ignores a trigger does not advance its block ID. Its later frames then
carry the wrong trigger number, with no gap in `blockids.npy`. The
[block-ID rate check](#block-id-rate-check) exists for that case.

### Block-ID rate check

After every recording, Panopticon compares how fast each camera's block IDs advanced
with that camera's own hardware clock. A camera that ignored triggers falls behind the
trigger rate, and the check reports it in `WARNINGS.txt`. The two rates may differ by
0.3% before the check reports a camera.

### Trigger witness

On a FLIR camera with counters, the backend also counts the trigger edges the camera
received and the exposures it made. A difference means the camera ignored triggers, and
the backend reports it in `WARNINGS.txt`. [FLIR.md](FLIR.md#known-limitations) lists
what the counts cannot prove.

### Free-run and triggered

A free-running camera paces itself from its own timer. A triggered camera exposes only
on an external edge. The live preview is free-run at 30 fps; calibration and recording
are triggered.

### Kick-out

During a recording, a trigger that any camera missed is dropped from every camera's
video before encoding, so all the videos hold the same triggers. The
[coordinator](#coordinator) does this when `realtime_kick` is true, the default. With
`realtime_kick: false`, the videos are aligned after the recording instead, with a
re-encode when the cameras lost different frames.

### Coordinator

The part of Panopticon (`gui_app/frame_sync.py`) that holds each camera's frames
briefly and releases a trigger once every camera has delivered it.

### Forced drop

When one camera falls more than `kick_max_lag` triggers behind the others, the
coordinator stops waiting for it. It drops that trigger from every camera, including
the frames the other cameras captured. `WARNINGS.txt` names the camera, and
`session_metadata.json` counts the drops under `kickout`. A healthy recording has none.

### Effective frame rate

The rate a recording's videos hold after kick-outs: the target rate times the share of
triggers that every camera kept. When kick-outs pass 0.5% of a recording's triggers,
Panopticon reports the rate in one line, for example
`Effective frame rate 99.4 fps (target 100).` The counts behind it are under `kickout`
in `session_metadata.json`.

### Retirement

Taking one camera out of a recording while the others go on. Panopticon retires a
camera that stalls beyond recovery, cannot start its stream, or delivers frames it
cannot use, so that camera cannot hold up the others. Its video ends early, and
`WARNINGS.txt` and a `RETIRED.json` beside its video say why.

### Laggard

The camera furthest behind the others in delivering frames. The status bar and the log
name it during a recording.

### Exposure ceiling

The longest exposure a camera can use at a given frame rate and still take every
trigger. A camera over the ceiling ignores some triggers, and its block IDs keep
counting without a gap, so only the [block-ID rate check](#block-id-rate-check) finds
it. Check an exposure change on a recording: the preview is free-run, where an
over-long exposure looks fine. The Basler rule is under
[`trigger_rate_limit`](CONFIGURATION.md#trigger_rate_limit), and the FLIR rule is in
[FLIR.md](FLIR.md#the-exposure-ceiling).

## Capture and encoding

### Grab thread

One thread per camera that takes each frame from the camera driver, copies it into the
[NV12 ring](#nv12-ring), queues it and releases the driver buffer. It does nothing else,
because any other work there delays the next frame.

### GIL

Python's global interpreter lock: only one thread runs Python code at a time. Every
grab thread and encoder thread shares it, so work that a thread does while holding it
delays the other cameras.

### NVENC

The hardware video encoder on NVIDIA GPUs. Panopticon drives it through PyNvVideoCodec
and encodes H.264 while it records.

### NVENC session

One encode stream on NVENC. A recording needs one per camera. The driver caps how many
sessions run at once, and the cap varies with the GPU and the driver version, so
Panopticon measures it before it records. The cap is often what limits the camera
count.

### NV12

The pixel layout NVENC takes: a full-resolution brightness plane, then a
half-resolution colour plane. Panopticon uses a Mono8 frame as the brightness plane and
fills the colour plane with 128, the value for no colour.

### NV12 ring

The NV12 frame buffers between a camera's grab thread and its encoder. In kick-out
mode each camera's ring holds `kick_max_lag` plus 264 frames, so its size follows
[`kick_max_lag`](CONFIGURATION.md#kick_max_lag). Panopticon frees the rings after every
acquisition.

### Pinned upload

How each frame reaches the GPU by default (`nvenc_upload: pinned`). Panopticon copies
the frame into page-locked memory without holding the [GIL](#gil), and the GPU copies
it from there. With `nvenc_upload: host`, PyNvVideoCodec makes the copy while holding
the GIL, which can starve a grab thread until its camera falls behind. An encoder that
cannot get page-locked memory falls back to `host` with a warning.

### IDR frame and GOP

An IDR frame is a keyframe that a decoder can start from, and the GOP is the run of
frames from one IDR frame to the next. Panopticon writes one IDR frame per second of
video, so a player can seek to any frame without decoding from the start.

### Faststart

An mp4's index (its moov atom) normally sits at the end of the file, so a player has to
read the whole file before it shows a frame. `-movflags +faststart` moves the index to
the front. Every mp4 Panopticon writes has it, so recordings open quickly in a browser.

### Remux

Rewriting a video into a new container without re-encoding its frames. During a
recording, each camera's H.264 goes to `stream.h264`, and at stop Panopticon remuxes it
into that camera's mp4.

### libx264

The H.264 encoder that runs on the CPU. With `encoder: auto`, Panopticon uses it when
NVENC grants fewer sessions than there are cameras and a benchmark shows the CPU keeps
up. [CPU_ENCODE.md](CPU_ENCODE.md) describes it.

### Raw capture

With `realtime_encode: false`, every frame goes to disk uncompressed (`raw.bin`) and is
encoded after the session. At 1920x1200 that takes about 500 times the disk space of
H.264.

## Network and camera settings

### GVSP

GigE Vision Streaming Protocol, the UDP protocol GigE cameras send frames over. One
frame travels as many packets.

### Jumbo frames

Ethernet packets larger than the standard 1500 bytes. A GigE camera set to large
packets (9000 bytes on the reference rig) needs every network adapter and switch port
between it and the computer to accept them. A port left at 1500 bytes drops the image
packets: the camera appears on the network, and no frames arrive.

### Inter-packet delay

A pause a GigE camera leaves between packets, so that one frame's burst does not
overflow a switch buffer. It is `GevSCPD` in a Basler `.pfs` file and
`camera.flir.packet_delay` on a FLIR camera.

### Resend

A request from the computer for a packet that did not arrive. GVSP runs over UDP, which
does not retransmit by itself. A resend that arrives in time costs only latency.

### Buffer underrun

A frame lost because the driver had no free buffer to receive it into: the computer did
not take frames out of the buffer pool (`max_num_buffer`) fast enough. Each camera's
count is `buffers_underrun` under `camera_stream_stats` in `session_metadata.json`.

### Failed buffer

A frame given up on in transmission, because packets were lost and resends did not
recover them in time. It points at the network. Each camera's count is
`buffers_failed` under `camera_stream_stats` in `session_metadata.json`.

### pfs file

A Basler camera settings file, saved from Basler's pylon Viewer. A Basler profile names
it in `pfs_path`, and Panopticon loads it into every camera when it opens them. It is
the source of a Basler recording's exposure, gain and frame size.

### camera block

The `camera:` block of a FLIR rig profile. It holds what a `.pfs` file holds on Basler:
exposure, gain, the trigger line, frame offsets and the Spinnaker-only settings.
[FLIR.md](FLIR.md#3-write-the-profile) explains it.

### User set

A set of camera settings saved inside a FLIR camera. Panopticon loads
`camera.flir.user_set` (default `Default`, the factory settings) each time it opens a
camera, then writes the settings the `camera:` block names. A change made in SpinView
lasts only if it is saved in that user set.

### Chunk

Metadata a camera attaches to each image, such as its timestamp or a counter value
(chunk data, in GenICam terms). The FLIR backend can read a frame's timestamp and its
trigger count from chunks (`camera.flir.timestamp_source` and
`camera.flir.block_id_source`).

### pylon

Basler's camera SDK. Panopticon drives Basler cameras through pypylon, its Python
binding.

### Spinnaker

Teledyne FLIR's camera SDK. Panopticon loads Spinnaker's C library directly, so PySpin
is not needed.

## Calibration

### ChArUco board

A chessboard with a unique ArUco marker in each white square, so that a partial or
rotated view of it is still identified. A [board config](#board-config) describes the
printed board.

### Board config

The YAML file in `configs/boards/` that describes the printed calibration board: the
number of squares, the square and marker sizes, the ArUco dictionary, and whether the
board uses the pre-OpenCV-4.6 ChArUco layout (`board_legacy`). The square size sets the
scale of every 3D coordinate, so measure the printed board. A wrong `board_legacy`
finds every marker and no ChArUco corners.

### Co-detection

A moment when two or more cameras see the calibration board at once. The coverage
display counts them live and saves their block IDs in `codet_frames.json`, so the solve
does not have to scan whole videos.

### READY

What the calibration coverage display shows once three conditions hold:

- each camera has `calibration_min_per_cam_shared` co-detections;
- the cameras form one connected group, in which a pair with `calibration_min_edge`
  co-detections is linked;
- each camera has seen the board in `calibration_min_grid_cells` of the four quadrants
  of its view.

You can stop before READY appears. The recording is still valid, and the solve reports
whether it had enough views.

### Intrinsics

One camera's own optics: focal length, optical centre and lens distortion.

### Extrinsics

Where a camera sits and points, as a rotation and a translation from the
[reference camera](#reference-camera).

### Reference camera

The camera whose coordinate frame becomes the calibration's world frame: `cam1`, unless
`1_calibrate.py --ref-camera` names another.

### Reprojection error

The distance, in pixels, between a detected board corner and the point where the
solved calibration projects that corner. It is the basic measure of a calibration.

### Stereo RMS

The root-mean-square reprojection error of one camera pair.
`reprojection_error_histogram.png` shows one bar per pair. Below 1.5 px is good, 1.5 to
3 px is worth checking, and 3 px or more is poor.

### Spanning tree

The solve chains the pairwise calibrations into one coordinate frame along a tree of
camera pairs. The tree prefers pairs with a low error over many shared frames, and the
solve keeps the largest connected group of cameras. There is no global bundle
adjustment, so an error in one pair carries to every camera beyond it on the tree.

## Stimulation

### Paradigm

A stimulation protocol built in the Stimulation editor: which pins fire, at what
frequency and pulse width, for how long and in what order. Panopticon compiles it into
the trigger board's firmware.

### Chain

One connected sequence of blocks in a paradigm. Chains run at the same time, so two
chains on one pin are refused.

### Apply

The Stimulation editor's button that compiles the paradigm and flashes it to the
trigger board. Editing the canvas changes nothing on the board until you press Apply.

### Recording-only sketch

The trigger board's firmware with the camera triggers and the
[safe-pin](#safe-pin) guard, and no stimulation. When a profile opens, Panopticon puts
it on the board unless it was the last firmware Panopticon flashed there. Every
calibration runs on it, so a calibration never delivers stimulation.

### Safe pin

A trigger-board pin that the firmware drives low as its first action at boot, before
the serial handshake, so a powered laser driver never reads it as on. The rig profile
lists these pins in `stim_safe_pins`. While the board resets, before the firmware runs,
every pin floats: [INSTALLATION.md](INSTALLATION.md#step-8--flash-the-trigger-firmware)
says what to do about that.

## Files, settings and logs

### Rig profile

The YAML file in `profiles/` that describes one rig: its cameras, frame rate, trigger
board, pins and paths. Choose it in the sidebar's profile dropdown, or start
Panopticon with `gui.py --profile <name>`. Templates to copy are in
`profiles/templates/`, and [CONFIGURATION.md](CONFIGURATION.md) describes every field.

### Session

The folder for one pair of subjects on one day,
`<output_dir>/<date>/<mouse1>_<mouse2>/`. It holds a `calibration/` folder and a
`recording/` folder, one per acquisition type.

### Session header

The block of `[header]` lines the log writes at launch and at the start of every
acquisition: Panopticon's version and commit, the computer, the GPU and its driver, the
NVENC session cap, every profile field with its value, and each open camera. Include it
when you report a problem.

### Log level

`log_level` in the rig profile: `normal`, `verbose` (the default) or `debug`. Every
level stamps each line with its time and thread, and writes the session header.
`verbose` adds each camera setting written with the value read back, and each change of
acquisition state. `debug` adds more detail from outside the capture loop. No level
logs anything per frame or makes a capture thread wait.

### session.log

The part of the log from an acquisition's start to its finalize, copied into that
acquisition's folder beside `session_metadata.json`. The full log of each launch is
`logs/panopticon_<date>_<time>.log` in the Panopticon folder.

### WARNINGS.txt

A file Panopticon writes into an acquisition folder, and sometimes into a camera's
folder, when there is something to report: a retired camera, forced drops, a camera
that ignored triggers, or the effective frame rate. A recording with no `WARNINGS.txt`
under its folder had none of these.

### Multi-process capture

Capturing the cameras in several worker processes (`capture_processes` above 0)
instead of in one. It is experimental, and the window refuses a profile that asks for
it.

### LUC3D

A browser-based tool for multi-view pose annotation, by Eric Leonardis and hosted by
the Talmo Lab. It opens Panopticon's mp4 files and `calibration.toml` as they are
([LUC3D](https://talmolab.github.io/luc3d/)).
