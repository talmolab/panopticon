# CPU H.264 encoding (libx264)

Panopticon encodes in real time with NVENC, on the GPU. This page covers the
other real-time encoder: `gui_app/cpu_encode.py`, which encodes with libx264 in
one `ffmpeg` child process per camera. It is the fallback for a machine whose
GPU cannot give every camera an NVENC session.

Panopticon still expects an NVIDIA GPU. The driver caps how many NVENC sessions
run at once, and that cap, which the launch check probes, is often what limits
how many cameras one machine records; more cameras need a more capable GPU.
libx264 covers a shortfall only while the CPU has cores to spare, and those are
the cores the capture threads need.

## Choosing the encoder

The profile's [`encoder`](CONFIGURATION.md#encoder) field takes `auto` (the
default), `nvenc`, `x264` or `raw`. `hardware_check.select_encoder()` resolves
it and installs the encoder with `encoders.set_default_factory()`: at launch,
after each profile switch, and at every start, against the cameras that are
open. The grab threads and the kick-out router take the installed factory and
never learn which encoder they got.

- `auto`: NVENC when the session probe grants at least one session per
  camera; otherwise libx264 when the launch bench shows the CPU can carry
  every camera at the frame rate; otherwise the start is refused. The option
  left, raw frames, costs hundreds of times the disk, so it is chosen in the
  profile or not at all.
- `nvenc`: the GPU path. The start is refused when the probe grants fewer
  sessions than cameras.
- `x264`: the CPU path. It also moves the ffmpeg writers that run after the
  session (the raw-tail merge and the alignment re-encode) to libx264, so a
  machine with no NVIDIA GPU can run them too. The start is refused when the
  bench shows the CPU cannot carry every camera.
- `raw`: raw frames written during capture and encoded after the session.
  This needs `realtime_encode: false` (below).

When `auto` falls back, the capacity check warns before the recording ("...
Recording on the CPU with libx264 instead ...") and asks whether to start. Each
camera recorded on libx264 carries a note in its `WARNINGS.txt` ("Real-time
encoding runs on the CPU (libx264, ..."). `session_metadata.json` records both
what the profile asked for (`encoder_requested`) and what recorded (`encoder`).

With NVENC recording in real time, the ffmpeg writers after the session follow
the launch check's test encode with ffmpeg's `h264_nvenc`. The two NVENC
libraries fail independently, so a host where PyNvVideoCodec works and
`h264_nvenc` does not records on the GPU and merges and aligns on libx264.

### `encoder: raw` needs `realtime_encode: false`

`realtime_encode` is the only field that switches the capture path, and nothing
in the capture path reads `encoder`. With `realtime_encode: true`,
`encoder: raw` would still encode in real time while skipping the NVENC session
check, and every camera without a session would fall back to `raw.bin` one at a
time, with nothing said. `select_encoder()` and the capacity check therefore
refuse that combination and name `realtime_encode: false` as the field to set.
With raw capture set, the disk budget counts the whole frame every frame
instead of the H.264 rate.

## Why it exists

Without it, a machine whose GPU cannot serve every camera has one option left:
`raw.bin`, the whole Mono8 frame every frame. At 1920x1200 and 100 fps that is
230 MB/s per camera, 129 GiB per camera per 10 minutes, about 500 times the
H.264 size. libx264 sits between the two: a real CPU cost, H.264-sized files,
and no encode pass after the session.

## How it is wired in

`gui_app/encoders.py` is the seam. A factory is called as
`factory(width, height, quality, fps, notes)` and returns an object with
`Encode(nv12) -> bytes`, `EndEncode() -> bytes` and, optionally, `Close()`.

- `cpu_encode.x264_factory` is that factory for the CPU path.
- `cpu_encode.create_x264_encoder(width, height, fps, quality, preset='ultrafast', threads=1)`
  is the direct constructor. Its argument order differs from the seam's.

The child process:

```
ffmpeg -y -nostdin -hide_banner -loglevel error \
  -f rawvideo -vcodec rawvideo -pix_fmt gray -s WxH -r FPS -an -i pipe:0 \
  -c:v libx264 -preset PRESET -tune zerolatency -qp QP -g FPS -bf 0 -threads N \
  -pix_fmt yuv420p -f h264 -flush_packets 1 pipe:1
```

`Encode()` writes the NV12 frame's Y plane (the gray image) to the child's
stdin and returns whatever a reader thread has drained from stdout since the
previous call. That may be nothing, because libx264 answers a frame or two
later; the bytes are appended to `stream.h264` in order, so none are lost by
arriving late. `EndEncode(timeout_s=2.0)` closes stdin, waits for the child and
returns the rest. `Close()` and `kill()` are the abandon path, and killing the
child is what frees an encoder thread blocked in a pipe write.

### `frames_out`: frames fed are not frames coded

`Encode()` returns as soon as the Y plane is in the child's stdin, so the calls
lead the coded pictures by one or two. Measured at 640x400 and at 1920x1200:
50 frames fed, 49 coded, with the flush at `EndEncode()` making up the
difference. NVENC has no such lead: its `Encode()` returns that frame's bytes.

`X264Encoder.frames_out` counts the coded pictures the child has emitted, by
counting Annex-B NAL types 1 and 5 in the drained bytes. Bookkeeping that maps
a recorded frame to a trigger uses it, because `blockids.npy` may record only
frames that were persisted. Where the child dies, the flush never arrives and
the lead stays. A block-ID list cut to the frames fed would then claim a frame
`stream.h264` does not hold, and the raw-tail split point would land a frame
late. Every frame from the failure on would map to the wrong trigger, with no
gap to show it. `_EncoderThread.coded_frames()` takes the smaller of its own
count of `Encode()` calls and the encoder's `frames_out`, for the
reconciliation and the split point alike
([INTERNALS.md](INTERNALS.md#reconciliation-what-blockidsnpy-may-claim)).

### Why `EndEncode()` is bounded

`SyncEncodeRouter.abandon()` bounds the whole teardown and runs on the Qt main
thread, and it reaches each camera's `EndEncode()` through
`_EncoderThread.release_encoder()`. `EndEncode()` therefore takes a `timeout_s`
that bounds the whole call, shared by the reader join and the child's exit, 2 s
by default, after which the child is killed. A clean flush takes 5-7 ms,
because `-tune zerolatency` with `-bf 0` leaves no lookahead to drain. A caller
under a deadline of its own passes its remaining time.

The command line keeps these properties:

- `-g <fps>`, one IDR a second. The LUC3D labeler seeks by IDR, and a stream
  with a single IDR cannot be seeked.
- `-bf 0`, no B-frames, so decode order is display order.
- A reader thread drains stdout for the child's whole life. The pipe holds a
  few tens of kilobytes, so an encoder nobody reads blocks in its own write,
  and the `Encode()` call feeding it never returns. `SyncEncodeRouter.abandon()`
  cannot recover from that state.

## Measured throughput

Measured on the reference rig's CPU, an Intel Core Ultra 9 285K with 24 cores
and 24 threads. Run the bench on your own machine before trusting a camera
count; the window runs the same bench at launch.

```
uv run python -m gui_app.cpu_encode --bench 1920 1200 100
```

It runs one single-threaded libx264 encode of 2 s of synthetic `testsrc2` video
at the given size and divides the frames by the wall time. The synthesis is
inside the timed region, which makes the answer conservative. The launch check
runs the same bench for `ultrafast` only (`hardware_check.BENCH_PRESETS`), and
again after a profile switch only when the frame size or rate changed. The
`veryfast` row comes from the command above; the factory always builds
`ultrafast`.

| Preset | fps per core at 1920x1200 | Cameras at 100 fps |
|---|---|---|
| `ultrafast` | 362 | 48 |
| `veryfast` | 111 | 20 |

A cross-check with the real encoder, 300 synthetic gray frames through
`X264Encoder` at 1920x1200, ran at 350 fps per thread at `ultrafast`, within 4%
of the bench.

### How the camera count is derived

`cpu_encode.sustainable_cameras(fps_per_core, fps, cores)`:

```
cores_per_camera = CAPTURE_CORE_FRACTION + fps / fps_per_core
cameras          = floor((cores - RESERVED_CORES) / cores_per_camera)
```

`CAPTURE_CORE_FRACTION = 0.2` is one camera's capture side (the grab thread,
the NV12 copy and the router submit) at the measured 0.8 ms of work per 10 ms
cycle, with headroom. `RESERVED_CORES = 1.0` covers the window, the preview and
the operating system. `cores` is the machine's physical core count.

The count is a ceiling on CPU throughput and nothing more. It knows nothing of
the GIL, the driver pool's RAM or the network, and on the reference rig all
three limit a nine-camera session long before 48 cameras of encoding would.
Read a count well above `n_cameras` as a sign the CPU is not what stops you,
and a count below it as a refusal.

## The disk estimate on the CPU path is a lower bound

`hardware_check.H264_BYTES_PER_FRAME = 4600` was measured on NVENC recordings
at the rig's qp. libx264 at `ultrafast` and the same qp writes more, and the
CPU path has not yet been measured on rig content. The disk warnings therefore
call the estimate a lower bound whenever the CPU encoder is installed.

## When to prefer which

- NVENC, whenever the driver grants a session per camera. It costs almost no
  CPU, and capture needs the CPU.
- libx264 at `ultrafast`, for a machine whose GPU cannot serve every camera.
  The encoders compete with the grab threads for cores. A camera whose encoder
  falls behind loses frames from its own video only, and its `WARNINGS.txt`
  says so. In kick-out mode the line reads "released frames were dropped from
  this camera's video", with "no free NV12 ring slot" as the reason; in the
  decoupled mode it reads "frames were dropped because its encoder queue
  stayed full".
- `raw`, only when chosen in the profile, with `realtime_encode: false` and the
  disk checked first.

There is no preset field. `cpu_encode.set_factory_options()` exists, but nothing
calls it and the profile has no field for it, so every recording runs
`ultrafast` with one thread per camera. `veryfast` is about 3 times slower here
for a modest saving in size at the same qp; the bench above is where an
evaluation of such a field would start.

## NVENC and monochrome

`nvenc.probe_monochrome_support()` reads NVENC's `support_monochrome`
capability. On the reference rig's GPU it returns 0: the encoder does not take
a monochrome surface, so the NV12 frame keeps its constant-128 chroma plane. It
returns -1 when NVENC is unavailable or the query fails, which means unknown,
never yes.
