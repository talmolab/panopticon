<h1 align="center">
  <img src="panopticon.ico" width="72" height="72" alt=""><br>
  Panopticon
</h1>

<p align="center">Hardware-triggered multi-camera video for 3D animal pose estimation.</p>

Panopticon records video from many machine-vision cameras at once. One hardware trigger fires every camera, so frame N is the same instant in every view, and the GPU encodes the video while it records. It also records and solves the ChArUco calibration that 3D pose estimation needs, and it can run optogenetic stimulation from the same trigger board.

It runs on Windows with Basler cameras, and FLIR support is in testing ([FLIR.md](docs/FLIR.md)).

![The main window with nine cameras in live preview](docs/images/main_idle.png)

## What it does

- Fires every camera from one TTL trigger.
- Keeps only the triggers every camera caught ([kick-out](docs/GLOSSARY.md#kick-out)), so the videos come out aligned frame for frame.
- Encodes H.264 on the GPU with NVENC as it records.
- Checks every recording for a camera that ignored triggers, and writes what it finds to `WARNINGS.txt`.
- Records and solves a ChArUco calibration, and writes `calibration.toml` in aniposelib's format.
- Runs optogenetic stimulation from the trigger board, and writes `stim_trace.csv`: the stimulus it was set to deliver on each frame.

The videos and the calibration open in [LUC3D](https://talmolab.github.io/luc3d/), a browser-based tool for multi-view pose annotation ([repository](https://github.com/talmolab/luc3d), [docs](https://talmolab.github.io/luc3d-docs/)).

## Getting started

New here? Read these pages in order:

1. [**Installation**](docs/INSTALLATION.md): set up the computer and the rig, and launch Panopticon for the first time. You'll do this once per computer.
2. [**Overview**](docs/OVERVIEW.md): a tour of every control in the window.
3. [**Workflow**](docs/WORKFLOW.md): a whole session, step by step, from calibrating to checking the recording.

The rest are reference pages, for when you need them:

| Page | Read it when |
|---|---|
| [CONFIGURATION.md](docs/CONFIGURATION.md) | You write or change a rig profile |
| [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Panopticon shows a message you don't understand |
| [GLOSSARY.md](docs/GLOSSARY.md) | A term is new to you |
| [FLIR.md](docs/FLIR.md) | You have FLIR cameras. It replaces parts of the installation |
| [SIMULATION.md](docs/SIMULATION.md) | You want to try or develop Panopticon without hardware |
| [CPU_ENCODE.md](docs/CPU_ENCODE.md) | The GPU can't encode every camera, and the CPU takes over |
| [INTERNALS.md](docs/INTERNALS.md) | You maintain Panopticon, and want to know how capture, alignment, encoding and calibration work |
| [HISTORY.md](docs/HISTORY.md) | You maintain Panopticon, and want the dated decisions, measurements and dead ends |

## Quick start

### Try it without hardware

You don't need a single camera to try Panopticon. The `sim` profile runs three simulated cameras and a simulated trigger board, so the preview, **Calibrate**, **Record** and the stimulation editor all work without a camera SDK. In PowerShell, with uv and Git installed ([how](docs/INSTALLATION.md#2-install-the-software)):

```powershell
git clone https://github.com/talmolab/panopticon.git
cd panopticon
uv sync --no-group rig
uv run --no-group rig gui.py --profile sim
```

`--no-group rig` leaves out the camera and GPU packages, so the simulated rig encodes on the CPU instead, and Panopticon will remind you of that. Keep the flag on every `uv run`, or uv installs those packages again. [SIMULATION.md](docs/SIMULATION.md) shows you around.

### On a real rig

1. Install uv, Git, your cameras' SDK and `arduino-cli`, then clone the repository and run `uv sync` ([INSTALLATION.md](docs/INSTALLATION.md#2-install-the-software), or [FLIR.md](docs/FLIR.md#1-install) for FLIR cameras).
2. Copy the closest template from `profiles/templates/` into `profiles/`, give it a `name`, and fill in your rig ([step 7](docs/INSTALLATION.md#step-7--write-the-rig-profile)).
3. Check that the profile's `serial_port` names the trigger board, then run `uv run gui.py --profile <name>`. Panopticon remembers the profile, so after that `uv run gui.py` is enough.

> [!WARNING]
> Opening a profile resets the trigger board on its `serial_port`, and every pin of the board floats briefly while it resets. Read the [stimulation warning](docs/INSTALLATION.md#step-8--flash-the-trigger-firmware) before you wire a stimulation device to the board.

## Requirements

| Part | What Panopticon needs |
|---|---|
| Computer | 64-bit Windows. CPU, RAM and disk scale with the camera count and frame rate. |
| GPU | An NVIDIA GPU with NVENC, and one encode session per camera |
| Cameras | Basler, GigE or USB3, through pylon. FLIR, GigE or USB3, through Spinnaker, is in testing. |
| Camera settings | Mono8, the same frame size on every camera, and a hardware trigger input on each |
| Trigger | A TTL signal. By default, an Arduino Mega 2560 that Panopticon programs, or [another Arduino board](docs/INSTALLATION.md#the-trigger-board). |
| Network (GigE) | Links sized to the pixel rate, and jumbo frames on every adapter and switch port |

The NVIDIA driver limits how many cameras one GPU can encode at once, and that's often what sets the camera count, so Panopticon checks the limit every time it launches. If the GPU comes up short, `encoder: auto` encodes on the CPU with libx264 instead, as long as the CPU can keep up. You can also trigger the cameras from your own pulse generator or DAQ (`trigger_source: external`), though stimulation isn't available then. [Section 1 of the installation guide](docs/INSTALLATION.md#1-what-the-rig-needs) helps you size the GPU, network, RAM and disk.

## Status

Panopticon is beta software. The performance figures in these docs all come from our rig: nine Basler 5GigE cameras at 1920x1200 and 100 fps. [HISTORY.md](docs/HISTORY.md) has the measurements and the decisions behind them. The FLIR backend has only run against a simulated Spinnaker library so far. Capture in worker processes (`capture_processes`) is experimental, and the window doesn't allow it yet.

## What a session writes

Each session is a folder, `<output_dir>/<date>/<mouse1>_<mouse2>/`, holding a calibration folder and a recording folder. Each of those has a folder per camera, with its mp4 and block IDs, plus `session_metadata.json`, `session.log`, and `WARNINGS.txt` when something went wrong. [WORKFLOW.md](docs/WORKFLOW.md#paths-and-names) lists every file.

## Contributing, bug reports and citing

We'd love your help. [CONTRIBUTING.md](CONTRIBUTING.md) covers the setup, the testing, and the rig run that a change to the capture path needs. Found a problem? Open an issue with the [bug report template](https://github.com/talmolab/panopticon/issues/new?template=bug_report.md). Tried Panopticon with FLIR cameras? Tell us how it went with the [FLIR bring-up template](https://github.com/talmolab/panopticon/issues/new?template=flir_bringup.md).

To cite Panopticon, use [CITATION.cff](CITATION.cff) or GitHub's **Cite this repository** button.

## Credits and licence

Isaac Tang (author and maintainer), Kay Tye and Talmo Pereira, of the Tye Lab and the Talmo Lab at the Salk Institute.

Panopticon grew out of [campy](https://github.com/ksseverson57/campy) by Kyle Severson (MIT licence). The trigger firmware and the raw-capture approach descend from campy, though no campy code remains. The Spinnaker C prototype table in `gui_app/backends/_spinc.py` extends one from [octacam](https://github.com/NeLy-EPFL/octacam) (Ramdya Lab, EPFL, MIT licence), whose notice that file keeps. LUC3D is by Eric Leonardis, Salk Institute.

Panopticon is licensed under the GNU General Public License, version 3 only (`GPL-3.0-only`), the terms PyQt5 requires of a program built on it. The full text is in [LICENSE](LICENSE), and the MIT licence of the octacam table is compatible with it.
