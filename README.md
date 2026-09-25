<h1 align="center">
  <img src="panopticon.ico" width="72" height="72" alt=""><br>
  Panopticon
</h1>

<p align="center">Hardware-triggered multi-camera video for 3D animal pose estimation.</p>

Panopticon records many cameras at once from one hardware trigger, and encodes their
video on the GPU as it records. Every camera's video holds the same triggers, so frame N
is the same instant in every view. It runs on Windows with Basler cameras, and FLIR
support is in testing.

![The main window with nine cameras in live preview](docs/images/main_idle.png)

## What it does

- Fires every camera from one TTL [trigger](docs/GLOSSARY.md#trigger).
- Records each frame's [block ID](docs/GLOSSARY.md#block-id) and keeps only the triggers
  every camera captured ([kick-out](docs/GLOSSARY.md#kick-out)), so the videos come out
  aligned.
- Encodes H.264 on the GPU with [NVENC](docs/GLOSSARY.md#nvenc) during the recording.
- Checks each recording for a camera that ignored triggers, and writes what it finds to
  `WARNINGS.txt`.
- Records and solves a ChArUco calibration, and writes `calibration.toml` in
  aniposelib's format.
- Compiles an optogenetic stimulation paradigm into the trigger board's firmware, and
  writes `stim_trace.csv`: for every frame, the stimulus the paradigm was set to
  deliver. The file is modelled from the firmware and cannot show that the laser fired.

The videos and the calibration open in [LUC3D](https://talmolab.github.io/luc3d/), a
browser-based tool for multi-view pose annotation
([repository](https://github.com/talmolab/luc3d), [docs](https://talmolab.github.io/luc3d-docs/)).

## Requirements

| Part | What Panopticon needs |
|---|---|
| Computer | 64-bit Windows. CPU, RAM and disk scale with camera count and frame rate. |
| GPU | An NVIDIA GPU with NVENC. Each camera takes one encode session. |
| Basler cameras | Supported, GigE and USB3, through Basler's pylon SDK. |
| FLIR cameras | In testing, GigE and USB3, through Teledyne's Spinnaker SDK: [docs/FLIR.md](docs/FLIR.md). |
| Camera settings | Mono8, the same frame size on every camera, and a hardware trigger input on each. |
| Trigger | A hardware TTL signal. By default, an Arduino Mega 2560 that Panopticon programs. |
| Network (GigE) | Links sized to the pixel rate, and jumbo frames on every adapter and switch port. |

More cameras need a more capable GPU. The driver caps how many NVENC sessions run at
once, and that cap often limits the camera count, so Panopticon measures it before it
records. When the GPU grants too few sessions, `encoder: auto` encodes on the CPU with
libx264 if a benchmark at launch shows the CPU keeps up, and refuses to record if not.
[INSTALLATION.md](docs/INSTALLATION.md) sizes the GPU, network, RAM and disk.

The trigger board also runs stimulation. A pulse generator or DAQ of your own can
trigger the cameras instead (`trigger_source: external`), without stimulation.

## Status

Panopticon is beta software. The performance figures in these docs come from one
reference rig, which runs nine Basler 5GigE cameras at 1920x1200 and 100 fps.
[HISTORY.md](docs/HISTORY.md) records the measurements and the decisions behind them.
The FLIR backend has run only against a simulated Spinnaker library. Capture in
several worker processes (`capture_processes`) is experimental, and the window refuses
any value above 0.

## Try it without hardware

The `sim` profile runs three simulated cameras and a simulated trigger board, so
preview, Calibrate, Record and the stimulation editor's Apply all work with no hardware
and no camera SDK. In PowerShell, with [uv](https://docs.astral.sh/uv/) and Git:

```powershell
git clone https://github.com/talmolab/panopticon.git
cd panopticon
uv sync --no-group rig
uv run --no-group rig gui.py --profile sim
```

`--no-group rig` leaves out pypylon and the NVENC bindings, and a plain `uv run` would
install them again. With the rig group left out, the simulated rig encodes on the CPU
with libx264. Panopticon says so in a dialog at launch, asks you to confirm before each
acquisition, and repeats the note when the acquisition ends.
[SIMULATION.md](docs/SIMULATION.md) describes the simulated rig.

## Quick start on a real rig

1. Install uv, Git, your cameras' SDK and `arduino-cli`
   ([INSTALLATION.md](docs/INSTALLATION.md#2-install-the-software); FLIR cameras:
   [FLIR.md](docs/FLIR.md#1-install)).
2. Clone the repository and run `uv sync`.
3. Copy the closest template from `profiles/templates/` into `profiles/`, set its
   `name`, and edit it ([CONFIGURATION.md](docs/CONFIGURATION.md)).
4. Run `uv run gui.py --profile <name>`. Panopticon remembers the profile, so later
   launches need only `uv run gui.py`. A launch with no profile chosen opens no camera
   and no serial port until you choose one in the sidebar.

Before step 4, check that the profile's `serial_port` names the trigger board. Opening
the profile resets the device on that port, and reprograms it unless Panopticon last
programmed it with the same firmware. Every pin of the board floats during the reset, a
laser driver's included, so read the
[laser warning in INSTALLATION.md, step 8](docs/INSTALLATION.md#step-8--flash-the-trigger-firmware)
first.

### Settings to change first

| Profile field | What to set |
|---|---|
| [`name`](docs/CONFIGURATION.md#name) | The name shown in the profile dropdown and given to `--profile`. |
| [`camera_backend`](docs/CONFIGURATION.md#camera_backend), [`pfs_path`](docs/CONFIGURATION.md#pfs_path), [`camera`](docs/CONFIGURATION.md#camera) | `basler` with a `.pfs` settings file, or `flir` with a `camera:` block. `sim` and `flir_sim` are simulated. |
| [`camera_serials`](docs/CONFIGURATION.md#camera_serials), [`n_cameras`](docs/CONFIGURATION.md#n_cameras) | Every camera's serial number, quoted, in ascending order, and how many cameras must be present. |
| [`frame_rate`](docs/CONFIGURATION.md#frame_rate) | The recording trigger rate. Keep exposure under the [exposure ceiling](docs/CONFIGURATION.md#exposure-ceiling). |
| [`serial_port`](docs/CONFIGURATION.md#serial_port), [`trigger_pins`](docs/CONFIGURATION.md#trigger_pins) | The trigger board's port, and every pin wired to a camera. A camera on an unlisted pin gets no triggers. |
| [`stim_safe_pins`](docs/CONFIGURATION.md#stim_safe_pins) | Every pin wired to a laser or LED driver, held low from boot. |
| [`output_dir`](docs/CONFIGURATION.md#output_dir) | Where sessions go. Use your largest, fastest drive. |
| [`metadata_defaults`](docs/CONFIGURATION.md#metadata_defaults) | Your lab's defaults for the sidebar, saved with every session. A copied profile carries another lab's names. |

Set `camera_serials` on any rig whose calibration you keep
([why](docs/CONFIGURATION.md#camera_serials)). [CONFIGURATION.md](docs/CONFIGURATION.md)
explains every field.

## What a session writes

A session is a folder, `<output_dir>/<date>/<mouse1>_<mouse2>/`, holding a
`calibration/` and a `recording/` folder. Each of those holds one folder per camera
(`cam1/` to `camN/`, with the mp4, `blockids.npy` and `frametimes.npy`), plus
`session_metadata.json`, `session.log`, and `WARNINGS.txt` when something went wrong.
The solve writes `calibration.toml` into `calibration/` and copies it into `recording/`.
[WORKFLOW.md](docs/WORKFLOW.md#paths-and-names) lists every file.

## Documentation

| Page | What it covers |
|---|---|
| [INSTALLATION.md](docs/INSTALLATION.md) | Sizing the hardware, building the network, installing, the first launch |
| [CONFIGURATION.md](docs/CONFIGURATION.md) | Every profile field, the templates, and setting up a new rig step by step |
| [FLIR.md](docs/FLIR.md) | FLIR cameras: install, wiring, profile, the diagnostic probe, what to send back |
| [WORKFLOW.md](docs/WORKFLOW.md) | A session from start to finish: calibrate, solve, record, check the result |
| [OVERVIEW.md](docs/OVERVIEW.md) | Every control in the window, the calibration coverage display, the stimulation editor |
| [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | The messages Panopticon shows, with their causes and fixes |
| [SIMULATION.md](docs/SIMULATION.md) | The simulated rig and its fault settings |
| [CPU_ENCODE.md](docs/CPU_ENCODE.md) | The libx264 encoder |
| [INTERNALS.md](docs/INTERNALS.md) | How capture, alignment, encoding and calibration work, and adding a camera backend |
| [GLOSSARY.md](docs/GLOSSARY.md) | The terms these pages use |
| [HISTORY.md](docs/HISTORY.md) | Dated decisions, measurements and dead ends |

## Contributing, bug reports and citing

[CONTRIBUTING.md](CONTRIBUTING.md) covers setup, testing and the rig run that a change
to the capture path needs. Report a problem with the
[bug report template](https://github.com/talmolab/panopticon/issues/new?template=bug_report.md),
and FLIR results with the
[FLIR bring-up template](https://github.com/talmolab/panopticon/issues/new?template=flir_bringup.md).
To cite Panopticon, use [CITATION.cff](CITATION.cff) or GitHub's "Cite this repository"
button.

## Credits and licence

Isaac Tang (author and maintainer), Kay Tye and Talmo Pereira, of the Tye Lab and the
Talmo Lab at the Salk Institute.

Panopticon grew out of [campy](https://github.com/ksseverson57/campy) by Kyle Severson
(MIT licence). The trigger firmware and the raw-capture approach descend from campy, and
no campy code remains. The Spinnaker C prototype table in `gui_app/backends/_spinc.py`
extends one from [octacam](https://github.com/NeLy-EPFL/octacam) (Ramdya Lab, EPFL, MIT
licence), whose notice that file keeps. LUC3D is by Eric Leonardis, Salk Institute.

Panopticon is licensed under the GNU General Public License, version 3 only
(`GPL-3.0-only`), the terms PyQt5 requires of a program built on it. The full text is in
[LICENSE](LICENSE). The MIT licence of the octacam table is compatible with it.
