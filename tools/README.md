# Probes and operator tools

Two kinds of script live here, and the difference decides whether you should
run one.

**Operator tools stay at the repository root.** They answer a question about
the rig you have right now, and they are meant to be run again whenever that
question comes up.

**Completed experiments live in `tools/experiments/`.** Each one answered a
question once; the answer is recorded below and in `CLAUDE.md`, and the script
is kept so the measurement can be repeated, not because it needs repeating.
Read the conclusion first: re-running an experiment whose answer is already
known costs a rig day.

Every probe that opens a camera, the trigger board or the GUI calls
`gui_app.probe_guard.refuse_if_panopticon_running()` and refuses to start while
another Panopticon is running. Two instances fight over the same cameras, and
the lag that produces looks exactly like the bug most of these scripts exist to
find. `--force` overrides the guard once you have checked the machine by hand.

## Operator tools (repository root)

| Tool | Answers | Hardware | Safe during a recording? |
| --- | --- | --- | --- |
| `probe_network.py` | Which switch is each camera plugged into, and is it on the right subnet? | Camera NICs; no camera is opened | Yes: discovery is one UDP query |
| `probe_network.py --sweep` | Does each camera's path carry 9000-byte packets? | Every camera | No: opens cameras, guarded |
| `probe_lag.py` | What is the cross-camera submission lag at the shipped profile? | Every camera plus the trigger board | No: guarded |
| `probe_seq.py` | Does a mixed sequence of recordings and calibrations in one GUI process degrade? | The whole rig | No: guarded |
| `probe_gui_record.py` | Does an unattended GUI recording reproduce a lag the headless probe does not? | The whole rig | No: guarded |
| `probe_abuse.py` | Does the GUI survive rapid toggling, a second serial handle, a mid-recording quit, and stim in tandem? | The whole rig | No: guarded |
| `probe_cpu_load.py` | How much acquisition margin is left under background CPU load? | Nothing | Yes, deliberately: it is the load |
| `configure_nic.ps1 -Check` | Do the camera NICs meet the receive-path thresholds? | Camera NICs | Yes: reads and reports only |
| `configure_nic.ps1` | Apply the RSS settings and verify them. | Camera NICs | No: resets the adapters |

`probe_lag.py` configures the manager through `gui_app.rig_setup`, the same two
calls the GUI makes, and prints a `CONFIG` block before opening. Compare those
lines with the GUI's log before comparing any lag number.

## Completed experiments (`tools/experiments/`)

Run them as `uv run tools/experiments/<name>.py`.

| Experiment | Question | Conclusion |
| --- | --- | --- |
| `probe_copy_scaling.py` | Is the gray to NV12 copy GIL-bound, and what explains ~850 MB/s? | The copy releases the GIL and costs ~0.08 ms on a warm ring; the production ring is already warm, so neither page faults nor bandwidth explain the 2.7 ms reading. |
| `probe_gil_wait.py` | How much of that 2.7 ms is executing, and how much is waiting for the GIL? | Almost all of it is GIL wait. The system tolerates about 300 us of GIL-held work per thread per frame at 17 threads; ~1000 us blows the 10 ms budget at 11 threads. |
| `probe_pypylon_gil.py` | Which pypylon calls hold the GIL? | Every wrapped call releases it except the `%nothread` set, which includes the 2.3 MB copy behind `result.Array`. Uses the pylon emulator, so it needs no rig camera. |
| `probe_zerocopy.py` | Which frame-access route should the grab loop use? | `GetArrayZeroCopy` wins: about 5x less executing time than `result.Array`, and `np.frombuffer(GetBuffer())` is no better than `.Array`. The result's row padding must be zero, which the probe now asserts. |
| `probe_release_gil.py` | Does `result.Release()` hold the GIL? | Measured by exec-versus-wall and by thread scaling, so the answer does not rest on the timer alone. Re-run only with the rig quiet. |
| `probe_native_cpu.py` | What does pylon's native GigE receive path cost, and where? | Attributes per-thread CPU to pylon's own threads and per-core DPC and interrupt time, which no Python-side measurement can see. This is the method behind the NIC preflight thresholds. |
| `probe_multiproc.py` | Does splitting the grab loops across processes recover timing margin? | A prototype only. Each worker coordinates just its own cameras, so its output is NOT globally trigger-aligned and must never be used for real data. |

## Adding a script

Put it at the root if an operator would run it again; put it in
`tools/experiments/` once its question is answered, and add a row above saying
what the answer was. Cite code by function name, not by line number: line
numbers rot silently and a stale one sends the next reader to the wrong place.
