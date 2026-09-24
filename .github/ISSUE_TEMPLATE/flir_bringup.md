---
name: FLIR bring-up
about: Send the probe results and test recordings from a rig with FLIR cameras
title: "FLIR bring-up: <camera model>, <USB3 or GigE>"
labels: flir
---

<!--
Thank you for testing Panopticon on FLIR cameras. The steps are in
https://github.com/talmolab/panopticon/blob/master/docs/FLIR.md

Fill in what you know and leave the rest blank. Keep all three rounds in
this one issue: post each later round's files as a comment instead of
opening a new issue.

The files below hold your computer's name, its network adapters and IP
addresses, your camera serial numbers and folder paths on your computer.
GitHub issues are public. If you would rather not post the files, say so
here and ask for another way to send them.
-->

## Round

- [ ] 1: probe runs, before any recording
- [ ] 2: a 2-minute test recording
- [ ] 3: a 30-minute recording

## Cameras

- Models, as part numbers (for example BFS-U3-16S2M-C), and how many of each:
- Interface of each camera (USB3, 1 GigE, 5 GigE, 10 GigE):
- Firmware version, if you know it:
- Mono or colour. For a colour model, does it offer a Mono8 pixel format?

## Connections

- USB3: which cameras share a USB controller or a hub:
- GigE: network adapters, switches, whether jumbo frames are set on each, PoE:
- How each camera is powered (USB, PoE, or its I/O connector):

## Trigger

- Trigger source: Panopticon's board (Arduino Mega 2560), or your own (make and model):
- For each camera, the input line and the connector pins it is wired to, and where its ground goes:
- Opto-isolated or non-isolated input:
- Pull-up resistors and cable lengths, if any:
- Anything connected to the trigger board besides cameras:

## Computer

- Windows version:
- CPU, RAM, disk type and free space:
- GPU model and NVIDIA driver version:
- Spinnaker SDK version:
- Panopticon commit (`git rev-parse --short HEAD`):
- Python version (`uv run python --version`), and whether PySpin is installed:
- Other software on this computer that uses the cameras or the GPU encoder (SpinView, OBS):

## What you aim to record

- Frame size, frame rate and exposure:
- Typical session length:

## SpinView

- Does every camera stream at your target frame rate and frame size in SpinView, all at once?

## Probe results

Paste the `Overall:` line of each probe run, and every row of its table that is not PASS:

```
```

## Attachments

- Round 1: the `flir_probe_*.json` and `flir_probe_*.log` files in `probe_out\`.
- Round 2: the `flir_collect_*.zip` from `uv run probe_flir.py --collect <recording folder>`, and a screenshot of Panopticon while it records.
- Round 3: the `flir_collect_*.zip` of the 30-minute recording.

## Anything else

What went wrong or looked odd, and what you changed in the profile:
