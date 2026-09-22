# Contributing to Panopticon

Panopticon is a lab acquisition tool that runs against real cameras, a real
trigger board and a real GPU. Most of its defects are silent: a wrong change
records a perfect-looking session whose frames are misaligned. The rules below
exist so that a contribution cannot introduce that class of failure unnoticed.

## Setting up

The project is managed with [uv](https://docs.astral.sh/uv/). A plain
`uv sync` installs everything, including the vendor SDKs (pypylon, the NVENC
bindings and the CUDA runtime). On a machine without Basler cameras or an
NVIDIA GPU, install without them:

```powershell
uv sync --no-group rig
```

The offline test suites and the post-hoc tools run in that environment. The
GUI itself needs the `rig` group.

## Running the tests

The offline test suite is maintained by the project but is not shipped in the
lean public tree. It remains in git history: recover it with
`git log --all --diff-filter=D -- "test_*.py"` and check out the commit that last
held those files, or request it from the maintainers. A change that alters
behaviour comes with a test that exercises it offline.

## Changes to the hot path need rig validation

The grab loop (`gui_app/grab_thread.py`), the encoder threads
(`gui_app/sync_encode.py`, `gui_app/nvenc.py`), the frame-sync coordinator
(`gui_app/frame_sync.py`) and the camera backend (`gui_app/backends/`) run
under a 10 ms per-frame budget shared by every camera. A change there can pass
every offline suite and still lose frames, because the failure is a scheduling
effect that only appears with real cameras streaming. Any pull request that
touches those modules states how it was validated on a rig: the number of
cameras, the frame rate, the duration, and the frame-loss figure from the
recording's alignment report. Without that, the change waits.

`CLAUDE.md` holds the invariants those modules rely on (zero-copy grab view,
pre-faulted ring, NVENC session accounting, block IDs recorded for persisted
frames only, `-g <fps>` and `+faststart` on every mp4). Read it before editing
the hot path. A fix that conflicts with an invariant is wrong; the invariant
wins.

## Editing `CLAUDE.md` and comments

`CLAUDE.md` is the working-rules file for anyone, human or agent, changing the
code. It states rules and their reasons in the present tense. It does not carry
dates, names, commit hashes or narrative of what was tried when; that history
belongs in the project's history ledger under `docs/`. A pull request that
changes a rule updates the rule's text in place and moves any superseded
narrative out. Comments and docstrings follow the same convention: state the
rule and its reason, not the story.

## Style

- Camera vendor code stays inside `gui_app/backends/`; nothing else imports
  `pypylon`.
- Every mp4 writer passes `-g <fps>` and `-movflags +faststart`, so the
  recordings load in the browser labeler.
- Commit messages say what changed and why in plain words, with the tests that
  verified it.

## License

Panopticon is licensed under GPL-3.0-only. By contributing you agree that your
contribution is licensed under the same terms.
