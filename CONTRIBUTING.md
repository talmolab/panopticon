# Contributing to Panopticon

Panopticon records from real cameras, a trigger board and a GPU. A wrong change in the
capture path can record a session that plays normally while its views are out of step.
Follow the rules below so that such a change is found before it merges.

## Setting up

The project uses [uv](https://docs.astral.sh/uv/). `uv sync` installs everything,
including the `rig` dependency group: pypylon, the NVENC bindings (PyNvVideoCodec) and
the CUDA runtime. On a computer without Basler cameras or an NVIDIA GPU, leave the group
out of the sync and out of every run:

```powershell
uv sync --no-group rig
uv run --no-group rig gui.py --profile sim
```

A plain `uv run` installs the `rig` group again.

The `sim` profile runs the whole application on simulated cameras and a simulated
trigger board ([SIMULATION.md](docs/SIMULATION.md)). For FLIR work, `probe_flir.py`
can rehearse against simulated cameras
([FLIR.md](docs/FLIR.md#rehearse-without-cameras)).

## Tests

The offline test suites are kept out of the public tree by the maintainer's choice.
The maintainer runs them on every change before it merges. In a pull request:

- say what the change does and which modules it touches;
- say how you checked it: on the simulated rig, on your own rig, or both, with what you
  ran and what you saw.

The maintainer then runs the suites that cover those modules, and adds a test for the
change where one is missing.

## Changes to the capture path need a rig run

These modules run while the cameras stream:

- `gui_app/grab_thread.py`, the per-camera grab loop;
- `gui_app/frame_sync.py`, the kick-out coordinator;
- `gui_app/sync_encode.py`, `gui_app/encoders.py`, `gui_app/nvenc.py`,
  `gui_app/cpu_encode.py` and `gui_app/cuda_driver.py`, the encoder threads, the NVENC
  and libx264 encoders, and the GPU upload;
- `gui_app/cpu_affinity.py`, which places the capture threads on CPU cores and sets
  their priority;
- `gui_app/camera_manager.py`, which starts and stops every acquisition;
- `gui_app/trigger_source.py`, which arms every camera before the first trigger;
- `gui_app/logging_setup.py`, which takes every print from a capture thread and must
  never make that thread wait;
- `gui_app/mp/`, the multi-process capture workers;
- `gui_app/backends/`, the camera backends.

Each grab thread has one trigger period per frame (10 ms at 100 fps), and every camera's
threads share one [GIL](docs/GLOSSARY.md#gil). A change to one of these modules can pass
every offline suite and still lose frames, because the failure is a scheduling effect
that appears only with real cameras streaming. A pull request that touches them states
how it was tested on a rig:

- the camera count and model, the frame rate and the duration;
- from the recording's `session_metadata.json`, the `kickout` counts (kept, kicked and
  forced triggers);
- every `WARNINGS.txt` the recording wrote, or that it wrote none.

A capture-path change merges only with that rig run.

[CLAUDE.md](CLAUDE.md) holds the rules these modules rely on. Among them: the grab loop
reads each frame through a zero-copy view, the NV12 ring is pre-faulted, NVENC sessions
are counted, and `blockids.npy` lists only frames that were persisted. Read it before you
edit the capture path. A change that
breaks one of those rules does not merge, even when every test passes.

## Adding a camera backend

A camera vendor is one module in `gui_app/backends/`, written against the
`CameraBackend` contract in `gui_app/backends/__init__.py`. Add the backend's name to
`KNOWN_BACKENDS` and a branch to `load_backend()`. The module imports its SDK itself, so
a computer without that SDK fails in one place with a message that says what to
install. [INTERNALS.md](docs/INTERNALS.md) describes the contract.

## Comments, docstrings and CLAUDE.md

`CLAUDE.md` is the working-rules file for anyone who changes the code, a person or an
agent. It states each rule and its reason in the present tense. Dates, names, commit
hashes and accounts of what was tried belong in [HISTORY.md](docs/HISTORY.md). A pull
request that changes a rule edits the rule's text in place and moves any old narrative
to HISTORY.md. Comments and docstrings follow the same convention: state the rule and
its reason.

## Style

- Camera SDK imports stay in `gui_app/backends/`, PySpin included: `probe_flir.py`'s
  optional `--pyspin` stage runs through `gui_app/backends/pyspin_probe.py`.
- No rig-specific number lives in code. Numbers that belong to a rig go in its profile,
  because `gui_app/` also runs rigs other than the reference one.
- Every mp4 writer builds its ffmpeg command with `gui_app/ffmpeg_cmd.py`, which adds
  the index at the front, and the keyframe interval on every re-encode
  ([why](docs/INTERNALS.md#the-stream-and-the-remux)). The remux is a stream copy, so
  its keyframes come from the encoder's GOP.
- Commit messages say what changed and why in plain words, and name the checks that
  verified it.
- Documentation states each fact once, in the page where a reader acts on it, and links
  to it from anywhere else.

## Reporting a problem

Use the
[bug report template](https://github.com/talmolab/panopticon/issues/new?template=bug_report.md).
It asks for the files that show what happened: the launch log, the acquisition's
`session.log` and `WARNINGS.txt`, and the rig profile.

## Licence

Panopticon is licensed under GPL-3.0-only. By contributing you agree that your
contribution is licensed under the same terms.
