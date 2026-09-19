# CPU H.264 fallback (libx264)

Panopticon's real-time encode path is NVENC. This document covers the other
path: `gui_app/cpu_encode.py`, which encodes with `libx264` inside one `ffmpeg`
child process per camera when the GPU cannot serve every camera.

## Status: the encoder is here, the operator cannot choose it yet

The encoder, the seam and the preflight arithmetic are in place and tested, but
**no caller runs the selection**: `main_window` constructs `HardwareCheckThread`
without a profile and calls `check_capacity` without `encoder=`, so
`select_encoder` never runs and `encoder: x264` in the rig profile has no
effect. Until that consumer half lands, a recording on this branch uses the
built-in NVENC factory whatever the profile says, and the preflight says so
rather than advising an edit that would do nothing: with no selection the
report prints `Using: the encoder selection did not run for this session`, and
the refusals leave out "set `encoder: x264`" — `hardware_check.encoder_selection_live()`
is what they ask.

## Why it exists

Before it, a machine with no NVIDIA GPU — or one whose driver session cap was
already spent — had exactly one option left: write `raw.bin`, the full mono8
frame every frame. At 1920x1200 and 100 fps that is **230 MB/s per camera,
~129 GiB per camera per 10 minutes**, roughly 500x the H.264 size. A six-camera
session fills a 2 TB drive in under half an hour, and until the preflight was
fixed nothing warned about it (`hardware_check.check_capacity` budgeted 4.6
KB/frame while every camera wrote 2.3 MB/frame).

libx264 sits between the two: real CPU cost, but H.264-sized output and no
post-hoc encode pass at all.

## How it is wired in

`gui_app/encoders.py` is the seam. A factory is called as
`factory(width, height, quality, fps, notes)` and returns an object with
`Encode(nv12) -> bytes`, `EndEncode() -> bytes` and an optional `Close()`.

- `cpu_encode.x264_factory` is that factory for the CPU path.
- `cpu_encode.create_x264_encoder(width, height, fps, quality, preset='ultrafast', threads=1)`
  is the direct constructor (note the argument order differs from the seam's).
- `hardware_check.select_encoder(profile, n_cams, fps, w, h)` decides which
  path a run uses and installs it with `encoders.set_default_factory()`. The
  grab threads and `SyncEncodeRouter` are untouched: they resolve the default
  factory and never learn which encoder they got.

The child process is:

```
ffmpeg -y -nostdin -hide_banner -loglevel error \
  -f rawvideo -vcodec rawvideo -pix_fmt gray -s WxH -r FPS -an -i pipe:0 \
  -c:v libx264 -preset PRESET -tune zerolatency -qp QP -g FPS -bf 0 -threads N \
  -pix_fmt yuv420p -f h264 -flush_packets 1 pipe:1
```

`Encode()` writes the NV12 frame's **Y plane only** (the gray image) to stdin
and returns whatever a dedicated reader thread has drained from stdout since
the previous call — possibly nothing, because libx264 answers a frame a frame
or two later. The bytes are appended to `stream.h264` in order, so nothing is
lost by arriving late. `EndEncode(timeout_s=2.0)` closes stdin, waits for the
child and returns the remainder; `Close()` / `kill()` is the abandon path, and
killing the child is what releases an encoder thread blocked in a pipe write.

### `frames_out`: frames fed are not frames coded

`Encode()` returns as soon as the Y plane is in the child's stdin, so the
number of calls leads the number of coded pictures by one or two (measured: 50
fed, 49 coded, at both 640x400 and 1920x1200 — the flush at `EndEncode()` makes
up the difference). NVENC has no such gap, because its `Encode()` returns that
frame's bytes.

`X264Encoder.frames_out` is the count of coded pictures the child has actually
emitted, taken by counting Annex-B NAL types 1 and 5 in the drained bytes.
**Any bookkeeping that maps a recorded frame to a trigger must use it**, because
CLAUDE.md's grab-loop invariant is that `blockids.npy` records only frames that
were actually persisted. On the encoder-death path the flush never arrives, so
the lead never closes: a `block_ids` list truncated to frames fed claims a frame
`stream.h264` does not contain, and `write_split_point()` puts the raw-tail
boundary one frame late — after the tail merge every frame from the failure
point on maps to the wrong trigger for that camera, with no gap in
`blockids.npy` to show for it.

`_EncoderThread` (`gui_app/grab_thread.py`) still counts its own `Encode()`
calls in `encoded`; the encoder-agnostic reconciliation is
`min(self.encoded, getattr(enc, "frames_out", self.encoded))`, for both the
`encoded + spilled` reconciliation and the `write_split_point()` call. That
file is outside this package.

### Why `EndEncode()` is bounded

`SyncEncodeRouter.abandon()` documents its `timeout_s` as the bound on the
whole teardown and runs on the Qt main thread, and it reaches `EndEncode()` for
each camera through `_EncoderThread.release_encoder()`. So `EndEncode()` takes
a `timeout_s` that bounds the WHOLE call — the reader join and the child's exit
share one deadline — defaulting to 2 s, past which the child is killed (`kill()`
is bounded in turn). A clean flush is nowhere near that: measured 5–7 ms, since
`-tune zerolatency` with `-bf 0` leaves no lookahead to drain. The bound is per
camera, so a caller that is itself under a deadline should pass its remaining
time rather than take the default.

Invariants that are not negotiable and are asserted in `test_cpu_encode.py`:

- **`-g <fps>`** — one IDR per second. The LUC3D labeler seeks by IDR; a stream
  with one IDR is unseekable and the failure is invisible until someone opens
  the file days later.
- **`-bf 0`** — no B-frames, so decode order is display order.
- **A reader thread drains stdout for the child's whole life.** The pipe holds
  a few tens of kilobytes; an encoder nobody reads blocks inside its own write
  and the `Encode()` call feeding it never returns, which is the one state
  `SyncEncodeRouter.abandon()` cannot recover from.

## Measured throughput on this machine

**CPU: Intel(R) Core(TM) Ultra 9 285K, 24 physical cores / 24 threads.**
These numbers describe this host only. Re-run the bench on any other machine
before trusting a camera count — that is what the command below is for, and it
is also what the GUI runs at launch.

Method — the launch bench, `cpu_encode.x264_bench(w, h, fps, preset)`:

```
python -m gui_app.cpu_encode --bench 1920 1200 100
```

It runs one single-threaded `libx264` encode of 2 s of synthetic `testsrc2`
video at the target geometry and divides frames by wall time. The source
synthesis is inside the timed region, which makes the answer conservative.
`HardwareCheckThread` runs the same bench at launch, but for `ultrafast` only
(`hardware_check.BENCH_PRESETS`), so Record never pays for it. The `veryfast`
row below comes from the command above: the factory builds `ultrafast` and the
rig profile has no field for the preset, so benching a second one at launch
would cost a real encode for a number nobody can act on.

| preset      | fps per core (1920x1200) | cameras at 100 fps |
|-------------|--------------------------|--------------------|
| `ultrafast` | 362                      | 48                 |
| `veryfast`  | 111                      | 20                 |

Cross-check with the real pipeline (300 synthetic gray frames pushed through
`X264Encoder` at 1920x1200): **350 fps/thread at `ultrafast`**, within 4 % of
the bench, so the bench is a fair proxy for the production path.

### How the camera count is derived

`cpu_encode.sustainable_cameras(fps_per_core, fps, cores)`:

```
cores_per_camera = CAPTURE_CORE_FRACTION + fps / fps_per_core
cameras          = floor((cores - RESERVED_CORES) / cores_per_camera)
```

`CAPTURE_CORE_FRACTION = 0.2` is the capture side of one camera — the grab
thread, the NV12 ring copy and the router submit — at the measured 0.8 ms of
work per 10 ms trigger cycle, with headroom. `RESERVED_CORES = 1.0` is the GUI,
the preview decimation and the OS.

**Read the camera count as a CPU-throughput ceiling, not a promise.** It is the
only limit this arithmetic knows about. The rig's real ceiling at nine cameras
is set by the GIL, the driver buffer pool's RAM and the NIC, all of which bind
before 48 cameras of encoding would. Treat the number as "the CPU is not the
thing stopping you" when it comfortably exceeds `n_cameras`, and as a refusal
when it does not.

## Choosing the path: `encoder` in the rig profile

`RigProfile.encoder` takes `auto` (the default), `nvenc`, `x264` or `raw`.

- **`auto`** — NVENC when the session probe grants at least `n_cameras`
  sessions; otherwise libx264 when the launch bench shows the machine can
  sustain `n_cameras` at the profile frame rate; otherwise the start is
  refused with a message, because the remaining option (raw) costs 500x the
  disk and must be chosen deliberately.
- **`nvenc`** — force the GPU path; the preflight still refuses when the probe
  grants fewer sessions than cameras.
- **`x264`** — force the CPU path. Also switches the post-hoc writers
  (`gui_app/ffmpeg_cmd.py`) to `libx264`, so a machine with no NVIDIA GPU can
  run the tail merge and the alignment re-encode too.
- **`raw`** — `raw.bin` plus a post-hoc encode. **Only meaningful together with
  `realtime_encode: false`**, see below.

Whichever real-time path is chosen, the post-hoc writers follow the launch-time
`h264_nvenc` test encode rather than the choice: the two NVENC libraries fail
independently, so a host where PyNvVideoCodec works and ffmpeg's `h264_nvenc`
does not records on the GPU and runs the tail merge and the alignment re-encode
on libx264.

### `encoder: raw` needs `realtime_encode: false`

**`realtime_encode` is the only field that switches the capture path**; nothing
outside the preflight reads `encoder`. So `encoder: raw` with
`realtime_encode: true` really does encode in real time, and believing it would
skip the NVENC session check and leave the GPU factory installed — every camera
that could not get a session would fall to `raw.bin` one at a time, at a rate
(1.29 GiB/s at six cameras) below the sustained-write warning, with a preflight
that said nothing. `select_encoder` and `check_capacity` therefore **refuse**
that combination and name `realtime_encode: false` as the field to set. Budget
the disk accordingly when it is set: the preflight budgets the full frame every
frame in raw mode rather than the 4.6 KB/frame the H.264 path uses.

### The disk estimate on the CPU path is a lower bound

`hardware_check.H264_BYTES_PER_FRAME = 4600` was measured on real recordings,
which are NVENC recordings at the rig's qp. libx264 at `ultrafast` and the same
`-qp` writes materially more, and no measurement of the CPU path on rig content
exists to replace it, so the disk warnings label the estimate a lower bound
wherever the CPU factory is the one installed.

## When to prefer which

- **NVENC**, always, when the driver grants a session per camera. It costs
  almost no CPU, and the CPU is what capture needs.
- **`x264` at `ultrafast`** for a GPU-less host, or when NVENC's session cap is
  spent. Expect the encode to compete with the grab threads for cores; watch
  the queue-full count in the recording's `WARNINGS.txt`, which is how a
  too-slow encoder announces itself (frames dropped from that camera only, so
  its video ends up shorter than the others).
- **`raw`** only deliberately, with `realtime_encode: false` and the disk
  checked first.

There is no preset knob. `cpu_encode.set_factory_options()` exists and works,
but no production caller sets it and the rig profile has no field for it, so
every recording runs `ultrafast` with one thread. `veryfast` is ~3x slower here
for a modest bitrate saving at the same `qp`; the `--bench` numbers above are
what an evaluation of adding that field would start from.

## Testing

```
python test_cpu_encode.py      # command invariants, router round trip, teardown
python test_hardware_check.py  # the preflight branches, stubbed
```

`test_cpu_encode.py` pushes 200 synthetic frames per camera for two 640x400
cameras through `SyncEncodeRouter` with the x264 factory and asserts the
stream's coded-picture count equals the recorded block IDs, that there is one
IDR per second, that the stream remuxes with `-c copy` and decodes back to the
same frame count, that `frames_out` equals the coded pictures in the stream and
does not catch up when the child is killed, that `EndEncode` kills a wedged
child inside its deadline, and that `Close` and `kill` behave. It needs ffmpeg
and nothing else; it skips cleanly when the binary is missing.

`test_hardware_check.py` starts no process at all: the NVENC session probe, the
`h264_nvenc` test encode, `PyNvVideoCodec` and the libx264 bench are stubbed in
every case, so it neither allocates a GPU session nor depends on the order the
cases run in.

## Related measurement: NVENC monochrome support

`nvenc.probe_monochrome_support()` reads `NV_ENC_CAPS_SUPPORT_MONOCHROME` from
the encoder capabilities. On this rig's GPU it returns **0**: the hardware
encoder does not take a monochrome surface, so the capture path's NV12 frame
with its constant-128 chroma plane stays as it is, and the half-frame of
chroma per ring slot is not recoverable by asking the GPU for mono. The probe
returns -1 when NVENC is unavailable or the query fails, which is "unknown",
never "yes".
