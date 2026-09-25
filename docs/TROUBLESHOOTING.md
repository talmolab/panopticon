# Troubleshooting

This page lists the messages Panopticon prints, with what each means and what to
do. Search the page for a few words of your message. The quoted text is what
Panopticon prints, with `<...>` in place of the parts that change, such as a
camera name or a number.

Messages appear in dialogs, in the status bar at the bottom of the window, in
the console, and in the log ([WORKFLOW.md](WORKFLOW.md#the-log) says where each
launch's log and each acquisition's `session.log` are). Each recording and
calibration folder also holds `WARNINGS.txt` when something needs your
attention.

When you report a problem, attach the log, the profile and any `WARNINGS.txt`.

Contents:

1. [Installing and launching](#installing-and-launching)
2. [Profiles](#profiles)
3. [Opening the cameras](#opening-the-cameras)
4. [The hardware check](#the-hardware-check)
5. [The network](#the-network)
6. [The trigger board at launch](#the-trigger-board-at-launch)
7. [Starting an acquisition](#starting-an-acquisition)
8. [During an acquisition](#during-an-acquisition)
9. [After a recording](#after-a-recording)
10. [The recording looks fine but the views are out of sync](#the-recording-looks-fine-but-the-views-are-out-of-sync)
11. [FLIR cameras](#flir-cameras)
12. [Calibration and the solve](#calibration-and-the-solve)
13. [Stimulation](#stimulation)
14. [The post-session tools](#the-post-session-tools)

---

## Installing and launching

| Message or symptom | Cause and fix |
|---|---|
| `The term 'uv' is not recognized` | PowerShell was open before uv was installed. Open a new window. If it persists, sign out of Windows and back in. |
| `The term 'git' is not recognized` | Install Git for Windows, then open PowerShell again. |
| `uv sync` fails to download | No internet, or a proxy. After one full `uv sync`, everything runs offline. On a machine synced with `--no-group rig`, a plain `uv run` downloads the camera and GPU packages again, so pass `--no-group rig` to every `uv run` there ([INSTALLATION.md step 4](INSTALLATION.md#step-4--install-the-python-dependencies)). |
| `cannot be loaded because running scripts is disabled` | Run the script as `powershell -ExecutionPolicy Bypass -File <script>.ps1`. |
| `No venv at <path>` | From `make_shortcut.ps1`: `uv sync` has not run in this copy of the repository. |
| `Panopticon failed to start` | The window never opened. The dialog gives the error and the log file. Common first-run causes: the camera SDK missing, a profile that will not load, or an incomplete `uv sync`. |
| `A package this build needs is not installed.` | Part of the dialog above for an import error. Run `uv sync` again, or choose a profile whose `camera_backend` does not need the missing package. |
| `Panopticon is already running (pid <pid>).` | Another copy of the window runs on this computer. Switch to it, or close it. A second copy would compete for the cameras, the trigger board and the GPU encoder, and could flash the board during a recording. |
| `Panopticon, or one of its probes, is already running:` | As above, and the list names each process. Close it first. |
| `To start a second copy anyway, run gui.py --force.` | The refusal above exits with status 3. Use `uv run gui.py --force`, or `.\_launch.bat --force`, only after you have checked the other copy by hand. The desktop shortcut passes no arguments. |
| `Panopticon — Unexpected Error` | An error inside the window. It keeps running, and every occurrence goes to the log the dialog names. Attach that log to a report. |
| `[guard] REFUSING TO START: Panopticon is already running, and two instances fight over the same cameras:` | From a probe (`probe_network.py --sweep`, `probe_flir.py`). Quit Panopticon and any other probe, then run it again. |

## Profiles

| Message or symptom | Cause and fix |
|---|---|
| `Choose your rig's profile` | No profile is remembered on this computer, or the one to open did not load. Choose one in the list at the top of the sidebar ([INSTALLATION.md step 9](INSTALLATION.md#step-9--first-launch)). |
| `Panopticon has not opened a profile on this computer yet.` | The first line of the dialog above on a new computer. |
| `No profile named <name> loaded (gui.py --profile).` | `--profile` names a profile that is not in `profiles/`, or that did not load. The dialog lists the skipped files. |
| `The profile this computer opened last, <name>, did not load.` | The remembered profile has an error now. The dialog names the file and the field. Fix it and launch again. |
| `skipping profile <file>: <reason>` | The profile loader refused this file. It is missing from the list. The reason names the field. [CONFIGURATION.md](CONFIGURATION.md) lists every check the loader makes. |
| `No rig profile could be loaded from <folder>.` | No file in `profiles/` loads. Copy a template from `profiles/templates/` into `profiles/` and restart. Until a profile loads, the board is left as it is and may still carry a stimulation paradigm, so switch the laser off. |
| `The profile's camera settings file is missing:` | `pfs_path` names a file that does not exist. Set it to your `.pfs` in `configs/`. |
| `The profile has no camera: block.` | A FLIR profile needs its `camera:` block ([FLIR.md](FLIR.md#3-write-the-profile)). |
| `camera.flir.sdk_dir is not a folder:` | Point it at the Spinnaker install folder, or remove it to search the default places. |
| `The profile sets capture_processes: <n>.` | Capturing in several processes is experimental, and the window captures in one. Set `capture_processes: 0`. |
| `A solve is running` | The profile switch was refused, and the list shows the old profile again. Choose the profile again once the calibration solve has finished. |
| `Firmware upload in progress` | The profile switch, the start or the close was refused during a [flash](OVERVIEW.md#16-state). Try again once the upload reports that it is done. An interrupted flash leaves the board without its laser-safety boot guard. |
| `The hardware check is running` | The profile switch or the start was refused. Choose the profile, or start, again once the status bar says the check is done. |

## Opening the cameras

These appear in a `Camera Error` dialog when a profile opens its cameras, except
the rows marked as log lines. After fixing the cause, choose the profile again
in the list.

| Message or symptom | Cause and fix |
|---|---|
| `No cameras found` | Nothing enumerated. Check power, cables and link lights, and close pylon Viewer, SpinView or anything else holding the cameras. For GigE cameras, run `uv run probe_network.py` ([The network](#the-network)). |
| `Expected <n> cameras but <m> are available to open.` | The count differs from `n_cameras`. Camera names are positions ([camera_serials](CONFIGURATION.md#camera_serials)), so Panopticon opens none. `uv run probe_network.py` shows which cameras answer, and flags one on the wrong subnet. Power-cycle a missing camera. After adding cameras, follow [INSTALLATION.md](INSTALLATION.md#adding-cameras-to-a-rig-that-already-works). |
| `Requested cameras did not enumerate:` | A serial in `camera_serials` did not appear. Power-cycle that camera. |
| `ignoring <n> enumerated device(s) not in the profile's camera_serials` | Log line. A camera not in `camera_serials` stays closed. Add its serial if it belongs to the rig. |
| `Camera <serial> failed to open/configure:` | It enumerated and would not configure. The next line gives the reason. Power-cycle it, or close the program holding it. Panopticon then closes every camera. |
| `PixelFormat is <format>, not Mono8.` | The `.pfs` (or the FLIR `camera:` block) sets a wider format. The capture path stores one byte per pixel, so a wider frame would be cut to its low byte. Set `PixelFormat` to `Mono8`. |
| `differs from the profile's` | A camera's frame size differs from the profile's `frame_width` and `frame_height`. Fix the `.pfs` or the profile so they agree. |
| `differs from camera 1` | One camera has a different frame size. Apply the same `.pfs` to every camera. |
| `The 'basler' camera backend needs pypylon, which is not importable in this environment` | Run `uv sync` (without `--no-group rig`). |
| `camera backend is not available:` | A backend file is missing from this copy of Panopticon. Update the copy, or choose another backend. |
| `The profile records <w>x<h> but the cameras are configured for <w>x<h>` | At Calibrate or Record: the open cameras' frame size and the profile disagree. Nothing started. Fix the `.pfs` or the profile. |
| `backend has no bandwidth-reserve control` | `gev_bandwidth_reserve_pct` or `gev_bandwidth_reserve_accum` is set for a backend without it. Remove both from the profile. |
| `is not available on this camera (a GigE Vision transport feature)` | A GigE setting reached a camera without it, such as a USB3 camera. Remove the setting from the profile. |
| `but the capture path reads TimeStamp as nanoseconds` | Log line. This camera's clock ticks at another rate, so the delivery lag, the stall recovery and the block-ID rate check are wrong for it. Report the model. |
| `extended (64-bit) block IDs:` followed by `UNAVAILABLE` | Log line, for information. The 16-bit block ID wraps every 65,535 frames (about 11 minutes at 100 fps), and Panopticon unwraps it. |
| `FATAL: PaddingX=<n> PaddingY=<n> — rows would shear. Refusing to record.` | Log line. The camera pads its rows, which the frame copy cannot handle. That camera is retired, and the others record. Check its width setting. |
| A pane stays black while its frame rate counts | A display setting. The Brightness and Contrast sliders change the preview only. |
| The preview is dark | Exposure and gain come from the `.pfs` (or the FLIR `camera:` block). Add light first, then exposure up to the [exposure ceiling](CONFIGURATION.md#exposure-ceiling), then gain. |

## The hardware check

The check runs at launch and after a profile switch. Its report goes to the log,
and a `Hardware Check` dialog shows it when there is a warning. Record and
Calibrate stay disabled while it runs.

| Message or symptom | Cause and fix |
|---|---|
| `cores detected (4+ recommended for multi-camera capture)` | The CPU is below the floor for running at all ([INSTALLATION.md](INSTALLATION.md#cpu)). |
| `GB total (16 GB+ recommended)` | The RAM is below the floor for running at all ([INSTALLATION.md](INSTALLATION.md#ram)). The capacity check at Record decides whether a recording fits. |
| `GB free (500 GB+ recommended)` | The output drive has little free space. Real-time H.264 needs little, and raw capture needs a lot ([INSTALLATION.md](INSTALLATION.md#disk)). |
| `Disk write speed: <n> MB/s` | The output drive writes slowly. It matters only for raw capture ([INSTALLATION.md](INSTALLATION.md#disk)). |
| `No working NVENC on this machine` | Neither NVENC library works. The report's `Using:` line names the encoder installed instead. Check the NVIDIA driver. |
| `The real-time GPU encode path (PyNvVideoCodec) is unavailable` | Recording then needs libx264 on the CPU. Run `uv sync`, and check the NVIDIA driver. |
| `ffmpeg's h264_nvenc test encode failed` | The post-session encodes run on the CPU instead, which is slower. |
| `GOP NOT APPLIED` | The encoder ignored the keyframe setting, so recordings would have one keyframe and could not be seeked. Report the driver and PyNvVideoCodec versions. |
| `nvenc_upload: pinned needs the launch check` | The pinned GPU upload did not pass its launch check, so this session uses the host upload. The video is the same, and grab threads can fall behind ([INSTALLATION.md](INSTALLATION.md#cpu)). The `[nvenc]` lines in the log say why. |
| `nvenc_context: own gives each of the <n> encoders a CUDA context of its own` | The GPU lacks free memory for one context per camera, or Panopticon could not measure it (the message says which), so the [pinned upload](GLOSSARY.md#pinned-upload) runs in the shared context. |
| `usbfs_memory_mb is <n> MB` | Linux only. Raise the usbfs limit as the message says. |
| The report's `Camera SDK:` line says `cannot load` | The camera SDK the profile needs did not load. Install it ([INSTALLATION.md](INSTALLATION.md#step-2--install-the-basler-pylon-sdk), or [FLIR.md](FLIR.md#1-install)). |
| `The hardware check could not finish` | Its findings are incomplete. The capacity check still runs at Record. |

## The network

| Message or symptom | Cause and fix |
|---|---|
| Cameras missing from pylon Viewer too | Check power, cables and addressing. Give each camera an address on its adapter's subnet ([INSTALLATION.md step 5](INSTALLATION.md#the-addressing-scheme)). `uv run probe_network.py` lists the cameras each adapter hears. |
| Cameras stream in pylon Viewer, but Panopticon does not find them or gets no frames | Check the firewall. Add the inbound UDP rule for the camera adapters, and delete any inbound rule that blocks Python ([INSTALLATION.md step 5](INSTALLATION.md#let-the-traffic-through-the-firewall)). |
| `No camera answered on any adapter.` | From `probe_network.py`. Check power, cables and link lights, then the firewall ([INSTALLATION.md step 5](INSTALLATION.md#let-the-traffic-through-the-firewall)). USB3 cameras never answer this discovery. |
| `on the WRONG SUBNET for the switch they are plugged into` | The camera's address does not match its switch's subnet, often after cables were swapped. Move the cable, or re-address the camera (pylon IP Configurator for Basler, SpinView for FLIR). |
| `no candidate camera adapters found` | No adapter has an address on a private subnet. Give each camera adapter its static address. |
| Cameras open but every frame is incomplete, `Failed_Buffer_Count` climbing | A device in the path is at 1500-byte frames. Set 9216 on every switch port, the uplink included, and the adapter's Jumbo Packet to 9014 bytes ([INSTALLATION.md](INSTALLATION.md#configure-the-host-adapters)). Then run the sweep in [INSTALLATION.md step 7](INSTALLATION.md#test-the-network-with-the-profile). |
| The sweep prints `FAIL` after a `complete=` count | Frames did not arrive whole at that packet size. See the row above. |
| High `Resend_Request_Count`, with or without lost frames | Check the switches' flow control first ([INSTALLATION.md](INSTALLATION.md#configure-the-switches)), on every port and the uplink. Then jumbo frames, Energy Efficient Ethernet, and cameras per port. |
| About a quarter of the frames missing, in single-frame gaps | `gige_driver: filter` drops a frame with a lost packet instead of asking for it again. Use `socket`. |
| A camera records at half the trigger rate, and its port runs at 2.5 Gbit/s | The inter-packet delay makes each frame take longer than the trigger period ([INSTALLATION.md](INSTALLATION.md#the-cameras-own-link)). Move it to a 5 Gbit/s port. |
| Cameras disappear right after a network adapter change | Changing adapter settings resets the adapter. The cameras come back within seconds. |
| `This must run elevated (Set-NetAdapterRss needs admin).` | `configure_nic.ps1` without `-Check` needs an elevated PowerShell. |
| `Not elevated: the RSS values Windows reports without elevation are not` | `configure_nic.ps1 -Check` ran without elevation, so it did not judge RSS. Run it from an elevated PowerShell. |
| `Panopticon is running; applying would reset the camera adapters under it:` | Close Panopticon and its probes first. `-Force` overrides after you have checked by hand. |
| `Cannot read the process table, so cannot tell whether Panopticon is` | Check by hand that nothing records, then re-run with `-Force`. |
| `Applying resets every one of them.` | The script found the ports itself. Name them with `-Ports`, or add `-Confirm` to be asked about each one. |
| `NOT APPLIED on: <ports>` | The driver accepted the RSS call and kept another queue count. |
| `NOT VERIFIED: no RSS information came back for <ports>` | A port was still resetting. Check the link state the script prints, then run it again elevated. |

## The trigger board at launch

Read the flashing warning in
[INSTALLATION.md step 8](INSTALLATION.md#step-8--flash-the-trigger-firmware)
before you connect a laser or LED driver to the board.

| Message or symptom | Cause and fix |
|---|---|
| The laser flashes briefly at launch | The board resets when its port opens and when it is flashed, and its pins float during the reset ([INSTALLATION.md step 8](INSTALLATION.md#step-8--flash-the-trigger-firmware)). The laser's own interlock is the only hard gate. |
| `Could not clear stim firmware` | The board could not be flashed, so it may still carry an earlier paradigm, a looping one included. Fix the cause the message gives, then press **Apply** on an empty Stimulation canvas, or switch the laser off. |
| `arduino-cli was not found` | Install the Arduino IDE or `arduino-cli`, or set `PANOPTICON_ARDUINO_CLI` to its path. The message lists every place searched. Only flashing, Apply and Test need it. |
| `trigger board not reachable on <port> at startup; will retry on first use` | The port did not open at launch. Check the cable, the port name and the Arduino Serial Monitor. The next Calibrate, Record or Test opens the port, which resets the board: switch the laser off first ([why](INSTALLATION.md#step-8--flash-the-trigger-firmware)). |
| `trigger board reports sketch <id>, not the recording-only <id>; flashing` | The board ran another sketch, and Panopticon flashes the recording-only one. |
| `trigger board still reports sketch <id>; not flashing again this launch` | The board kept reporting another sketch after one flash. Check `serial_port` names the right board, then Apply an empty canvas. |
| `trigger board reported no sketch identity` | The board runs firmware that does not report its identity. Apply an empty canvas to flash Panopticon's sketch. |
| `this profile takes its triggers from an external source: no trigger board is flashed or opened` | `trigger_source: external`. Information. |

## Starting an acquisition

A start that is refused records nothing. Most refusals leave the cameras in
preview.

| Message or symptom | Cause and fix |
|---|---|
| `Checking hardware: Record and Calibrate are available once it reports` | The launch or switch hardware check is still running. Wait for `Hardware check done`. |
| `A calibration solve is still running.` | Wait for the solve to finish. |
| `Check the session details` | A metadata field or the date cannot be used in a folder name. The message names the field. Fix it and start again. |
| `No cameras are open.` | The cameras never opened ([Opening the cameras](#opening-the-cameras)). A recording would run the trigger protocol, and any stimulation, while saving nothing. |
| `Not enough RAM for <n> cameras:` | The driver pool plus the NV12 ring exceed the memory available. The message gives both. Lower `max_num_buffer` or `kick_max_lag`, or close other programs ([INSTALLATION.md](INSTALLATION.md#ram)). |
| `NVENC granted only <n> concurrent sessions but <m> cameras need one each` | The driver's session cap is below the camera count, often because another program holds sessions (a browser's hardware video encode, an ffmpeg left running). Close them, record fewer cameras, or use a GPU with a higher cap ([INSTALLATION.md](INSTALLATION.md#gpu)). |
| `NVENC granted no encode sessions.` | NVENC gives no session at all. Check the NVIDIA driver, and close other programs that encode on the GPU. |
| `Recording on the CPU with libx264 instead` | A warning: the GPU had too few sessions and the CPU encoder took over. It competes with capture for cores, so check `WARNINGS.txt` afterwards. |
| `The NVENC session cap could not be probed` | A warning: the start goes ahead on the GPU. A camera that gets no session writes raw frames, so watch the disk. |
| `No encoder on this machine can keep up with <n> cameras at <fps> fps` | Neither NVENC nor libx264 can encode every camera. Record fewer cameras or at a lower rate, or set `realtime_encode: false` and budget the disk. |
| `libx264 encodes about <n> fps per core` | `encoder: x264` and the CPU cannot keep up with this many cameras. Record fewer cameras, lower the frame rate, or use the GPU. |
| ``The rig profile sets `encoder: raw` `` | Only `realtime_encode: false` writes raw frames. Set it, or set `encoder` to `auto`, `nvenc` or `x264`. |
| `Disk may be short:` | A warning: a 10-minute recording would not fit. A shorter one is fine. |
| `Disk is tight:` | A warning: a 10-minute recording would use more than 80% of the free space. Clear space before the next session. |
| `Raw capture will write <n> GiB/s.` | Use a drive rated for that sustained rate, or split the cameras across drives. |
| `Overwrite the existing data?` | The folder holds an earlier acquisition. **Yes** deletes it, except `calibration.toml`, once the board's port is claimed, even if the start is then refused. To keep it, choose **Cancel** and change the metadata. |
| `Overwrite the existing data?`, with an external trigger source | **Yes** sets the earlier acquisition aside until the first trigger, and puts it back if the start ends before then. |
| `Serial port <port> could not be opened, so no triggers would be sent.` | Something holds the port (the Arduino Serial Monitor, another program), or `serial_port` names the wrong port. Close it and start again. That start opens the port, which resets the board: switch the laser off first ([why](INSTALLATION.md#step-8--flash-the-trigger-firmware)). |
| `Could not create the session directories` | The output folder cannot be written. Check it and start again. |
| `Stop the stimulation test first` | A stimulation test drives the board. Stop it in the editor. |
| `Cannot record with this stim workflow` | The canvas has a pin conflict, a forbidden pin or a loop with no start, or it does not compile. The message says which ([Stimulation](#stimulation)). |
| `Apply the stimulation paradigm first` | The canvas holds a paradigm that was never Applied, so nothing would fire while the files say it did. Press **Apply**, or clear the canvas. |
| `Apply the edited paradigm first` | The canvas changed since the last Apply, so the board would run the old paradigm. Press **Apply**, or undo the edit. |
| `Apply the empty canvas first` | The canvas is empty and the board still carries the paradigm Applied earlier this session. Press **Apply** to clear the board. |
| `Stimulation needs the trigger board` | The profile uses `trigger_source: external`, which has no board to run a paradigm. Use a profile with `trigger_source: board`. |
| `The trigger board is running sketch <id>, not the <kind> sketch this` | The board reported another sketch. The start was rolled back. Start again: Panopticon flashes the right sketch first, which resets the board. Switch the laser off before that start ([why](INSTALLATION.md#step-8--flash-the-trigger-firmware)). |
| `The trigger board could not be flashed with the <kind> firmware` | Nothing started. Switch the laser off, check the board and the port, and retry. |
| `had not armed after <n> s, so the trigger board was not started` | A camera that arms after the board starts would pair every frame with the wrong trigger. Arming fills each camera's frame ring, so memory pressure is the usual cause: close other programs, or lower `kick_max_lag` or `max_num_buffer`. |
| `had not armed after <n> s, so you were not asked to start your trigger source.` | The same, with `trigger_source: external`. |
| `Frames arrived before the trigger board was started:` | Something already triggers these cameras: the board still running from an earlier start, another source on their trigger line, or a camera not in trigger mode. Check the wiring and the camera settings. |
| `The trigger board did not acknowledge the start command, so no triggers would be sent.` | The board did not confirm, even after a reset. Check the USB cable, and that the board runs Panopticon's sketch. |
| `The trigger board did not confirm the start, but the cameras had already counted <n> frames, so the board did start.` | A retry would shift every camera's block IDs against the board's trigger count, so the start was rolled back. Start again. If the message says the board never confirms a command, it runs other firmware: Apply an empty canvas. |
| `Could not put every camera into trigger mode:` | Power-cycle the camera the message names and retry. |
| `These cameras cannot record at <fps> fps:` | A camera cannot reach the frame rate at its settings. On FLIR cameras see [FLIR.md](FLIR.md#when-something-refuses). |
| `Real-time kick-out was requested but its encoders could not be created:` | Often the NVENC session cap. Close other programs that encode on the GPU, or restart Panopticon. |
| `received frames before every camera was armed:` | `trigger_source: external`: your source was running while the cameras armed, so their frame counts start on different pulses. Stop the source and start it only when Panopticon asks. |
| `No camera received a trigger within <n> s of the prompt` | `trigger_source: external`: no pulse arrived within 45 s. Check that the source runs at the profile's rate and reaches every camera's trigger input. |

## During an acquisition

Most of these appear in the status bar.

| Message or symptom | Cause and fix |
|---|---|
| `Capture healthy — every camera within <n> trigger(s) of the leader` | Normal in the default [kick-out](GLOSSARY.md#kick-out) mode. |
| `CAPTURE FALLING BEHIND: <cam> is <n> triggers behind the leader (cap <cap>). Close other applications.` | A camera lags by more than a quarter of `kick_max_lag`. Nothing is lost yet. Close other programs. |
| `TRIGGERS BEHIND THE LEADER (cap <cap>): frames every camera captured are being dropped. Stop and investigate.` | A camera lags by more than three quarters of `kick_max_lag`. At the cap, triggers every other camera captured are dropped from every video. Stop and check that camera ([After a recording](#after-a-recording)). |
| `Capture healthy — keeping up with the trigger (max lag <n> ms)` | Normal with `realtime_kick: false`. |
| `s behind real time and growing. Close other applications.` | With `realtime_kick: false`: a camera's grab loop is behind. The driver pool absorbs it until it fills, then frames are lost. |
| `s BEHIND REAL TIME (<cam>). Frames will be lost when the buffer pool fills. Stop and investigate.` | The same, by more than a second. Stop and check that camera. |
| `EVERY CAMERA IS RETIRED: nothing is being recorded. Stop the recording.` | Stop, and read the retirement reasons in the log. |
| `NO FRAMES from <cams> for <n> s` | Those cameras have delivered nothing for that long. Check their trigger cables and network links. |
| `NO FRAMES FROM ANY CAMERA for <n> s: the trigger board may have stopped.` | The trigger source stopped, or the network to every camera. A `No frames from any camera` dialog says whether the board's serial link still answers. On a profile with `stim_safe_pins` it also says to check the laser: do so. |
| `CAMERA TEMPERATURE: <cam> <t> C` | The camera is near its shutdown temperature, or in its over-temperature state. Check its airflow and mount ([INSTALLATION.md](INSTALLATION.md#camera-temperature)). |
| One camera's pane shows about half the trigger rate | Exposure over the ceiling, or a 2.5 Gbit/s link ([The network](#the-network)). The camera ignores every second trigger. See [the out-of-sync section](#the-recording-looks-fine-but-the-views-are-out-of-sync). |
| `Waiting for the first trigger: start your trigger source now` | `trigger_source: external`: start your source. |
| `Triggers arriving:` | `trigger_source: external`: the recording runs. Stop your source to finish it. |
| The recording stops on its own | A stimulation block marked **Ending** finished. |
| `log lines dropped: the log writer fell behind` | The disk or a console window that nobody reads held up the log. No capture thread waited. The log file lacks those lines. |

## After a recording

These go to the recording's `WARNINGS.txt`. Most also appear in the
`Recording completed with problems` dialog. In the default kick-out mode, the
rows about unequal videos appear in a `Videos are not equal length` dialog.
With `realtime_kick: false` or `realtime_encode: false`, the post-hoc alignment
runs after the encode, and its rows appear in dialogs of their own, such as
`Alignment reported problems`.

| Message or symptom | Cause and fix |
|---|---|
| `Effective frame rate <rate> fps (target <rate>).` | More than 0.5% of the triggers were missing from at least one camera, so they were dropped from every video. The videos stay aligned. The log has each camera's losses, and `session_metadata.json` the counts. |
| `are missing from every camera's video: they were force-dropped because a camera fell more than kick_max_lag` | A camera fell a full `kick_max_lag` behind ([forced drop](GLOSSARY.md#forced-drop)), and the message names it. The videos stay aligned. Find why that camera lagged: its link, its CPU core, its temperature. |
| `was RETIRED mid-recording` | That camera's video ends at the [retirement](GLOSSARY.md#retirement), and the others stay aligned. The reason follows in brackets, and the rows below explain the common ones. |
| `every grab failing` | Every frame from this camera failed. On GigE, check jumbo frames on the adapter and on every switch port in its path. On USB3, check the cable and the host controller. |
| `grabs failed (<n>%, last:` | More than 0.5% of this camera's frames were lost in transmission. Same checks as above. |
| `no frame received since the triggers started` | The camera never triggered. Check its trigger cable, its pin in `trigger_pins`, and its trigger line setting. |
| `armed after the trigger board started` | The camera armed late, so Panopticon retired it. Its block IDs would have counted from a later trigger than the other cameras'. |
| `stream stalled and block IDs could not be realigned` | Panopticon could not place the camera's frames after a stall with certainty, so it retired the camera. Check its link. |
| `stream dead after <n> re-arms` | The stream kept stalling. Check the camera's link and power. |
| `camera did not start grabbing` | The camera would not start. Power-cycle it. |
| `could not allocate its NV12 ring` | Out of memory at the start. Lower `kick_max_lag`, or close other programs. |
| `found no free NV12 ring slot` | The camera's encoder fell behind the trigger, and frames were dropped from that camera. Its video is shorter than the others, so pair its frames by block ID (`blockids.npy`). Check the GPU load and the log. |
| `frames were dropped because its encoder queue stayed full` | The encoder did not keep up. The drops are gaps in `blockids.npy`, which alignment accounts for. |
| `no real-time encoder could be created` | That camera wrote raw frames to `raw.bin`, and the encode ran after the session. Usually the NVENC session cap. |
| `the encoder failed after <n> frames` | The rest was written raw to `raw_tail.bin` and merged at encode time. |
| `block-ID bookkeeping claimed <n> frames but only <n> were persisted` | An encoder fell behind or failed. The block IDs were cut to what is in the video, so frame numbers still map to the right triggers. |
| `encoder did not finish draining, so the frame-to-trigger mapping is UNVERIFIED` | Check that camera's mp4 frame count against `blockids.npy` before you use its alignment. |
| `Every active camera stopped receiving frames at the same time` | The trigger source stopped, or the network to every camera. Triggers during the silence are missing from every camera. The message says whether the cameras re-armed. Check the board and its USB cable. |
| `No camera received a frame after` | Nothing was recorded. Check that the trigger source runs and is wired to every camera, and that each camera's trigger line matches the one it drives. |
| `its grab thread was still receiving frames <n> s after the trigger board was told to stop` | The board may still be triggering, or the camera was not in trigger mode. |
| `CAMERA(S) FAILED:` | In the status line: those cameras produced no usable video. Their source files are kept (`raw.bin` or `stream.h264`, `encode_error.log`) in their folders. |
| `Recording did not finish cleanly` | Saving failed, for example on a full disk. The capture files are kept and not encoded. Do not record into that folder again. Once the cause is fixed, `uv run python 0_encode.py "<folder>"` encodes them. |
| `Trigger board did not confirm the stop` | The board may still be triggering, and a looping paradigm never ends on its own. Power-cycle the board and switch the laser off. |
| `The block-ID rate check could not run on this recording` | A camera that ignored triggers would not be found. Run `uv run python 2_align.py <recording folder>` to check it. |
| `real-time encode uses the host upload, not the configured pinned upload` | The pinned GPU upload was not available for this camera. The video is the same, and the grab threads could fall behind. The log's `[nvenc]` lines say why. |
| `reached <t> C during this acquisition` | The camera ran hot. Check its airflow and mount before the next recording. This message reaches `WARNINGS.txt` only when the recording lost frames. Otherwise only the status bar and the log show it. |
| `A camera at its shutdown point stops delivering frames` | The camera reached its shutdown temperature, and its recording may end there. Let it cool before the next recording. |
| `retired during the recording, so` | Kick-out mode. The retired camera's video ends early, so the videos are not equal length. Pair its frames with the others by block ID (`blockids.npy`), not by frame number. `2_align.py` leaves a retired camera out by default. |
| `The cameras did not all keep the same frames` | Kick-out mode. A camera's video ended early or lost frames, and the videos are left as recorded. Run the `2_align.py` command the message gives. `--replace --exclude <cams>` aligns the other cameras, and `--truncate-to-shortest` cuts every camera to the triggers they share. |
| `The post-hoc alignment did not run on this recording` | The videos are left as recorded, and frame *i* is not the same trigger on every camera. Fix the cause, then run `2_align.py --replace`. |
| `The post-hoc alignment failed` | The videos are left as recorded and are not aligned. Run `2_align.py --replace` once the cause is fixed. |
| `Alignment failed: <error> — videos left as-is` | The status line for the row above. |
| `Not replacing any video:` | A camera recorded nothing, or no trigger is common to every camera. The message gives the `--exclude` or `--truncate-to-shortest` command to run. |
| `raw.bin holds <n> whole frames but <n> were recorded` | The disk lost the last frames of this camera. Its block IDs and frame times were cut to the frames on disk. |
| One camera stops delivering partway through a recording, the others fine | Check its temperature first ([INSTALLATION.md](INSTALLATION.md#camera-temperature)), then its power and its link. |
| `cycle` in a grab thread's line above the trigger period | That grab loop does not finish inside one period. Another program uses the CPU, or a change added work to the loop ([INSTALLATION.md](INSTALLATION.md#3-verify-it-works)). |
| `Buffer_Underrun_Count` above 0 | The driver's buffer pool ran dry, so the host fell behind. The network is not the cause. |
| A video does not seek in LUC3D | The mp4 lacks a keyframe every second. The launch check proves the keyframe setting, and says `GOP NOT APPLIED` when it fails. Report the log and the encoder the report names. |

## The recording looks fine but the views are out of sync

Symptom: every camera has the same number of frames and no packet or buffer
counter moved, yet triangulated points miss the animal and fast movements
happen at different times in different views. Or `WARNINGS.txt` says a camera's
block IDs advanced at the wrong rate. In the default kick-out mode that camera
also falls one trigger further behind the leader for every trigger it ignores,
which the status bar shows during the recording
([INTERNALS.md](INTERNALS.md#what-happens-over-the-ceiling)).

Alignment treats "same [block ID](GLOSSARY.md#block-id)" as "same instant". A
frame-ID block ID (every Basler camera, and a FLIR camera on its frame ID)
counts the frames a camera acquired. It equals the trigger count only while the
camera acquires one frame per trigger. A camera still busy when the next trigger
arrives ignores that trigger. It acquires no frame and consumes no block ID, so
from then on its block ID *N* belongs to trigger *N+k*. Its block IDs have no
gap, its frame count matches the others, and the videos drift apart in time.
On a FLIR camera that uses its trigger counter, the ignored trigger is a gap
([FLIR cameras](#flir-cameras)).

The common causes:

- An exposure over the [exposure ceiling](CONFIGURATION.md#exposure-ceiling).
- A `trigger_rate_limit` above the camera's real maximum frame rate, which makes
  the computed ceiling too generous.
- A camera on a 2.5 Gbit/s link with the reference inter-packet delay
  ([INSTALLATION.md](INSTALLATION.md#the-cameras-own-link)).
- A trigger input driven with too little current
  ([INSTALLATION.md](INSTALLATION.md#wiring-the-trigger-line)).

Panopticon catches it with the camera's device clock, which runs independently
of the block-ID counter: over any stretch of a recording, block IDs must advance
at the trigger rate. The check runs for each camera when a recording stops, and
again in the alignment pass. It allows the two rates to differ by 0.3%, a
tolerance measured on real recordings
([INTERNALS.md](INTERNALS.md#checking-the-axiom-against-an-independent-clock)).
The check abstains below 300 frames or 2 seconds of data, so silence on a short
test clip means nothing was judged.

| Message | Cause and fix |
|---|---|
| `block IDs advanced at <rate>/s while <source> should run at <fps>/s` | That camera ignored about as many triggers as the message says, so its frames are paired with the other cameras' frames from other instants. Do not use the recording for 3D reconstruction. Fix the cause and record again. |
| `Block IDs cannot outrun the trigger, so this is not a capture fault` | A camera's block IDs ran faster than the trigger. The profile's `frame_rate` may not match what the board drives, a stall recovery may have realigned the camera wrongly, or the camera may not report its clock in nanoseconds. |
| `cameras report the same block-ID rate` | Every camera is off by the same amount. Cameras do not fail identically, so suspect the reference: `frame_rate` against what the board drives, or the camera clock's unit. The videos are probably aligned with each other. |
| `WARNING (block rate): no camera could be checked` | From `2_align.py`: no camera had the data the check needs, so nothing shows the block IDs are trigger counts. |

To re-examine a recording, run:

```powershell
uv run python 2_align.py <recording folder>
```

It writes the alignment index and prints any rate warning without changing a
video, even when the recording reports that it is already aligned. The check
needs each camera's `frametimes.npy` as recorded. A copy trimmed by an earlier
`2_align.py --replace` may hold frame times rebuilt from the block IDs, which
the check cannot judge, so use an untrimmed copy.

Before you edit the camera settings, read the exposure line each camera logs at
every acquisition start:

```
[cam1] exposure=3000 us gain=6.0 dB (ceiling 3545 us at 100 fps, AcquisitionFrameRate=165)
```

It shows the exposure applied and the ceiling it was checked against. When the
exposure asked for is over the ceiling, Panopticon applies the ceiling, and the
line goes on with ` CLAMPED from <value> us` and the ceiling it applied. A line
without `CLAMPED` means the exposure was under the computed ceiling, unless the
line's ceiling reads `none` or the camera's exposure could not be read when it
opened. The clamp then had nothing to compare. Otherwise the inputs to the
ceiling are the next suspects: `trigger_rate_limit` against the camera's real
maximum frame rate, and `frame_rate` against what the board drives.

A recording with this fault cannot be repaired. Its frames carry the wrong
trigger numbers, and trimming frames cannot correct that. Record it again once
the cause is fixed. If you need more light, add illumination before you raise
the exposure.

## FLIR cameras

The FLIR refusals at open and at a start, and the probe's refusals, are in
FLIR.md's [When something refuses](FLIR.md#when-something-refuses). FLIR
cameras with counters also get a [trigger witness](GLOSSARY.md#trigger-witness):
at the stop Panopticon
compares the trigger edges each camera counted with the exposures it started,
and writes what it finds to `WARNINGS.txt`.

With `camera.flir.block_id_source: trigger_counter` the block IDs count the
trigger edges, so an ignored trigger is a gap, which alignment drops from every
camera. Otherwise the block IDs count the frames the camera acquired, and an
ignored trigger shifts every later block ID of that camera. A camera that
latches its trigger count before the edge has every block ID one trigger early,
and its first image may not show which way it latches.

The trigger counters wrap. On a camera with narrow counters the witness counts
ignored triggers only in recordings shorter than the frame count its message
names. The block-ID rate check still runs on a longer recording, but a few
ignored triggers stay under its tolerance.

| Message | Cause and fix |
|---|---|
| `so it ignored <n> trigger(s).` | The camera missed that many triggers. When the message goes on to say its frames are paired with the wrong instants, do not use the recording for 3D reconstruction. Lower `camera.exposure_us`, or set `camera.flir.block_id_source: trigger_counter`. |
| `each ignored trigger is a gap` | Alignment drops each of those triggers from every camera. If the message says the next warning questions that, read the latch row below first. Lower `camera.exposure_us` to keep those triggers. |
| `its first image did not show whether it latches the CounterValue chunk` | The last triggers delivered no frame, or the camera latches its count before the edge. Send the output of `uv run probe_flir.py`, and do not use the recording for 3D reconstruction until it shows which. |
| `its trigger witness is limited:` | The recording is longer than the counters can follow, so it has no count of ignored triggers. Set `camera.flir.block_id_source: trigger_counter` if the message offers it, or keep recordings under the length it names. |
| `so this recording has no trigger witness for this camera` | The counters could not be read or do not count what Panopticon set. The block-ID rate check is then the only check. Send the output of `uv run probe_flir.py`. |
| `So this camera's frames are not proven aligned.` | The witness cannot show whether the ignored triggers shifted this camera's block IDs, for example around a stall re-arm. Do not use the recording for 3D reconstruction. Lower `camera.exposure_us`, or set `camera.flir.block_id_source: trigger_counter`. |
| `no counters; the trigger witness is off for this camera` | Log line. This model has no counters, so the block-ID rate check is the only check. |

## Calibration and the solve

| Message or symptom | Cause and fix |
|---|---|
| No camera ever brightens in the coverage HUD | No camera finds the board's markers. Either the board is too dark (raise `calibration_exposure_us`), or `board_config` does not describe the printed board. |
| The coverage HUD never appears | OpenCV is missing, or `board_config` names a file that does not exist. The rest of the window still works. |
| The caption's `grid <n>/<n>` count stops short of its target | The board is waved in one part of the view. Carry it into each camera's corners. |
| One line stays thin while the rest are bright | That pair rarely sees the board together. Hold the board where both can see it. |
| `Solve unavailable while acquiring/encoding` | Solve runs only at `IDLE`. |
| `A solve is already running` | One solve at a time. It takes several minutes, and the state reads `CALIBRATING...` meanwhile. |
| `Calibration directory not found:` | The metadata fields name a session without a calibration. Solve builds the path from the fields as they are when you press it. |
| `No calibration videos found in:` | The calibration folder holds no videos yet. Wait for the encode to finish, or check the metadata fields. |
| `Board config not found:` | `board_config` names a file that does not exist. Point it at a file in `configs/boards/`. |
| `The board config cannot be used with this OpenCV build.` | Check `marker_bits`, `dict_size` and `board_legacy` in the board config. |
| `No ChArUco board detections found.` | The board was not visible, or the board config does not match the printed board. A board printed with the pre-4.6 OpenCV layout needs `board_legacy: true`. With the flag wrong either way, every marker is found and no corner comes back. |
| `Fewer than two cameras produced valid intrinsics.` | Each camera needs at least 20 frames with 6 or more corners. Record longer, with the board filling more of each view. |
| `No camera pair saw the board together.` | Hold the board where several cameras see it at once. |
| `The coverage graph is disconnected.` | Too few cameras share board sightings. Hold the board where neighbouring cameras see it together. |
| `Calibration solve failed (singular matrix).` | Too few detections, or the board seen from one angle only. Record more orientations. |
| `Missing dependency:` | The environment is incomplete. Run `uv sync` and try again. |
| `Calibration timed out (<n> min)` | The solve did not finish. A calibration recorded without the coverage HUD has no co-detection hints, so the solve scans every video. |
| `Calibration failed (exit code <n>):` | The end of the solve's error output follows. |
| `pairing detections by frame index` | A warning: some cameras have no usable block IDs, so views are paired by frame number, which is right only for aligned videos. |
| `is poor (<n> px or more)` | That camera pair's stereo error is high. Check the board config against the printed board, and record the board across more of both views. |
| `records other cameras under names this calibration solved` | A recording in this session has other cameras under the same names, so the calibration does not fit it. Recalibrate, or set `camera_serials`. |
| `is unreadable (<reason>); scanning the videos` | The co-detection hints file could not be read, so the solve scans the videos. Slower, and still correct. |
| `is malformed (<reason>); scanning the videos` | As above. |
| `skipped <n> view(s) whose corners fit no homography` | Views that show a single row or column of the board were left out. Information. |
| `intrinsics FAILED` | That camera had too few usable views and was left out. Record it with the board filling more of its view. |
| `coverage graph is disconnected:` | The solve keeps the largest connected group of cameras and drops the others. Record with the board visible to cameras of both groups. |
| `only <n> detection frames (30+ recommended)` | That camera's calibration rests on few views. |
| `Consider recording calibration longer with the board visible to more cameras simultaneously.` | The solve finished with warnings. They are listed above this line in the log. |
| `matplotlib not available, skipping histogram` | No reprojection plot. The calibration is fine. `uv sync` restores matplotlib. |
| `calibration.toml` holds fewer cameras than you recorded | Cameras were dropped from the solve. Read the `Cameras:` and `Solved <n> of <n> cameras` lines at the end of its output. |
| Every amber or red bar in the pairwise plot contains the same camera | That camera is the fault. Check its focus and that nothing has moved it since the calibration, then recalibrate. |
| One bad bar, with both of its cameras fine in every other pair | Those two cameras share few views of the board, or only oblique ones. Recalibrate with the board where both can see it. |
| Fewer bars in the plot than camera pairs | A pair gets a bar only once its two cameras have seen the board together in enough views. A missing bar means they never did. |

[WORKFLOW.md](WORKFLOW.md#reading-the-pairwise-calibration-plot) explains how
to read the pairwise plot.

## Stimulation

| Message or symptom | Cause and fix |
|---|---|
| `arduino-cli was not found, so the stim firmware cannot be compiled or uploaded.` | Install the Arduino IDE, or put `arduino-cli` on `PATH`, or set `PANOPTICON_ARDUINO_CLI`. |
| `Compile failed (arduino-cli exit <n>).` | Nothing was flashed, so the board runs what it ran before. For a missing core: `arduino-cli core install arduino:avr`. |
| `Upload failed on <port> (arduino-cli exit <n>).` | The port is held by something else, `serial_port` is wrong, or the board is not a Mega. An upload that failed part-way leaves the board's firmware, its laser-pin boot guard included, unknown. Power-cycle the board. |
| `s after the upload timed out. Do NOT power-cycle the board while it runs` | The flashing tool still runs. Wait for it to exit, then power-cycle the board and Apply again. |
| `Something else is using the serial port` | An acquisition or a flash holds the port. Wait, or stop the acquisition. |
| `block(s) form a loop with no starting block, so they would never run.` | Select one block of the loop and tick **Starting**. |
| `cannot carry a stim waveform:` with `camera trigger line` | The block is on a camera's trigger pin, where extra edges would break that camera's alignment. Move it to a free pin. |
| `cannot carry a stim waveform:` with `UART RX0/TX0` | Pins 0 and 1 carry the serial link. Move the block. |
| `is driven by more than one chain.` | Two chains on one pin fight over it. Give each its own pin, or join them into one chain. |
| `100% duty — constant ON, not <n> Hz` | The pulse width is at least the period, so the pin never returns low. Fix the numbers, or keep it for a constant output. |
| `The last Apply FAILED, so the board does not carry this paradigm.` | Press **Apply** again and let it succeed. |
| `The trigger board did not acknowledge the start command, so the paradigm would not run.` | From Test. Check the board is connected and runs Panopticon's sketch. |
| `STOP NOT CONFIRMED — stim may still be running.` | The board did not accept the stop, and a looping chain never ends on its own. Power-cycle the board and switch the laser off. |
| The paradigm did not run | It was probably never applied. Editing the canvas changes nothing until **Apply**. `matches_uploaded_firmware` in `stim_paradigm.json` records whether the canvas matched the flash. |
| A paradigm ran that nobody chose | The launch flash should prevent it. Look for `Could not clear stim firmware` at launch. Firmware survives closing Panopticon. |
| `[teensy] no ack — reopening port to force a board reset` | Once is normal: the start is retried after a reset. The reset floats the board's pins during that start ([INSTALLATION.md step 8](INSTALLATION.md#step-8--flash-the-trigger-firmware)). |
| `[teensy] board speaks RDY but did not confirm — aborting` | The board has confirmed before, so silence now is a fault. The start was rolled back. Check the cable and the power. |

## The post-session tools

| Message | Cause and fix |
|---|---|
| `no session_metadata.json records the frame rate of <folder>; pass --fps.` | From `0_encode.py`. Pass the rate the recording ran at. A guessed rate would play every video at the wrong speed. |
| `needs the frame size, which no session_metadata.json records; pass --width and --height` | From `0_encode.py`. |
| `produced no usable video; their source files are kept` | From `0_encode.py`. See `encode_error.log` and `tail_error.log` in each camera folder. |
| `no session_metadata.json records the frame rate of <folder>; assuming <fps> fps.` | From `2_align.py`. Pass `--fps` if the recording ran at another rate. |
| `Re-run 3_stim_trace.py on` | The stimulation trace no longer matches the videos. Run `uv run python 3_stim_trace.py <folder>`. |
