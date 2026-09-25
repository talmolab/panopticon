---
name: Bug report
about: Something went wrong in a recording, a calibration or the program itself
title: "<what went wrong, in a few words>"
labels: bug
---

<!--
Fill in what you know and leave the rest blank. For a first run on FLIR
cameras, use the FLIR bring-up template instead.

The files below hold your computer's name, camera serial numbers and folder
paths on your computer. GitHub issues are public. If you would rather not post
a file, say so here and ask for another way to send it.
-->

## What happened

What you did, what you expected, and what happened instead:

## Messages

The text of any dialog, and any line of the log that looks related:

```
```

## Rig

- Profile name and `camera_backend`:
- Cameras (make, model, how many, GigE or USB3):
- Trigger source (Panopticon's board, or your own):
- GPU and NVIDIA driver version:
- Windows version:
- Panopticon commit (`git rev-parse --short HEAD`):

## Files

Attach what applies:

- The log of the launch where it happened: `logs\panopticon_<date>_<time>.log` in the
  Panopticon folder. Its `[header]` lines at the top describe the software, the computer
  and every camera.
- For a recording or a calibration, from its folder
  (`<output_dir>\<date>\<mouse1>_<mouse2>\recording\` or `...\calibration\`):
  `session.log`, `session_metadata.json`, every `WARNINGS.txt` (also any inside the
  `camN` folders), and any `RETIRED.json`.
- Your rig profile, `profiles\<name>.yaml`.
- For a calibration problem: `calibration_report.json` and
  `reprojection_error_histogram.png` from the `calibration` folder.

## Anything else

What you changed recently, in the profile, the cameras or the computer:
