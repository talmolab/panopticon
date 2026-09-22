# Project history

This is the engineering ledger: dated decisions, measurements and dead ends that
explain *why* the code and the profiles hold the values they do. The code states
each rule in the present tense and does not re-tell the story behind it; that story
is here. Entries are one line each — `YYYY-MM-DD — what changed — why / the decisive
number`. Each phase ends with the dead ends it closed, so nobody re-runs them.

Live measurements and the current performance picture are in
[`docs/INTERNALS.md`](INTERNALS.md); this file is the chronology.

---

## 1. Calibration coverage HUD (May 2026)

- 2026-05-28 — a full-video `sleap-anipose` solve of all cameras reached **0.37 px**
  reprojection error on this rig (0.063 px on the other); the GUI "Solve" /
  `1_calibrate.py` subsample path degrades the solve, so solve from full videos.
- 2026-05-29 — coverage HUD merged (PR #1) and rig-validated: live ChArUco coverage
  graph in the sidebar, 30 fps calibration capture+preview, parallel NVENC encode,
  snapshot button. READY is a connected co-visibility graph, not all 15 pairs — the
  board is one-sided, so opposed cameras can never co-detect. The design rationale
  (READY conditions, marker-vs-corner counting, reprojection targets) moved into
  `docs/INTERNALS.md`; the old `CALIBRATION_HUD_HANDOFF.md` was retired here.

---

## 2. Capture pipeline stabilisation (June 2026)

- 2026-06-10 — cam6 stream stall: a GigE stall freezes the grab loop and its frontier,
  so the coordinator force-drops every later trigger; restarting the grab is the only
  recovery short of restarting the GUI.
- 2026-06-12 — inline encode removed from the grab loop: encoding on the critical path
  blew the 10 ms/frame budget under 6-thread contention and dropped **~28%** of frames.
- 2026-06-12 — `gige_driver: socket` adopted: the in-kernel `filter` driver with default
  resend settings discards a frame on a lost packet rather than asking again, measured
  **~23%** loss at 6×100 fps.
- 2026-06-12 — legacy ChArUco layout diagnosed: the physical board predates OpenCV 4.6,
  so the ≥4.7 `CharucoDetector` returns 0 corners silently without `setLegacyPattern(True)`
  (`board_legacy: true`).
- 2026-06-17 — `kick_max_lag: 1000` starved capture outright (**24%** loss): the NV12 ring
  scales with the cap, and 1000 was too much memory pressure.

Dead ends, do not retry:
- A bigger `kick_max_lag` ring (1000 starved capture, 24% loss).

---

## 3. Optostim and laser safety (July 2026)

- 2026-07-24 — `-g <fps>` (1 IDR/s) and `-movflags +faststart` set on every mp4 writer:
  without them one 898 s / 415 MB recording held a **single IDR**, unseekable in LUC3D
  and unwalkable by `ffprobe` in 10 minutes.
- 2026-07-26 — Stimulation node editor added; the stim board **is** the camera-trigger
  board (one Arduino Mega on COM3), so the generated sketch does both.
- 2026-07-26 — suppressing the DTR reset (`dtr=False`/`rts=False` before `open()`) tried:
  it removed the boot flash but the board then ignored the config and emitted **zero
  triggers** (`Total_Packet_Count 0` on all six). The reset returns the sketch to
  `setup()` with a cleared RX buffer, so it is load-bearing. Dead end.
- 2026-07-27 — reset-window laser flash resolved by one long-lived serial connection plus
  an `RDY <n_cams> <fps>` ack: the reset is relocated to launch and Apply, never Record.
- 2026-07-27 — OpenCV floor raised to **4.7**: 4.6 crashed on the `chessboardCorners`
  accessor and made the `setLegacyPattern` guard no-op silently.
- 2026-07-27 — 44-minute run, **6.34%** loss (a 23-minute run the same day lost 43%): the
  cameras split into two network groups (one segment ~460,000 resend requests vs ~313 for
  the other), with the worst camera riding `max_lag`.
- 2026-07-27 — 6.8 kΩ pulldown on the laser MOD line measured **ineffective**: the CNI
  PSU-III's MOD input has an internal pullup far stiffer than 6.8 kΩ, and a resistor low
  enough to beat it would exceed the Arduino's 20 mA per-pin limit. Hardware fix abandoned;
  the software fix (long-lived connection) is what solved it. Dead end.
- 2026-07-27 — stall recovery + `retire()`: `grab_thread` re-arms after 25 consecutive
  timeouts (up to 5×); `_resync_offset()` recovers the true ordinal from the device
  timestamp and refuses if the gap is not within 0.25 of a period; `retire()` drops a
  camera that cannot realign so the survivors keep recording aligned. (cam6 stalled
  2026-06-10, cam1 2026-07-27.)

Dead ends, do not retry:
- `dtr=False`/`rts=False` to suppress the reset (zero triggers).
- A pulldown on the PSU-III MOD line (internal pullup too stiff; use the interlock).

---

## 4. Exposure, limiter and the 240/480 A/B (August 2026)

- 2026-08-11 — recording exposure/gain raised **2000 µs / 0 dB → 3000 µs / 6 dB** (~3×):
  the old pair put 65% of pixels in levels 0-15 with **21.5% clipped at exactly 0**,
  destroyed at the ADC; modelled, ~7× total would clip 12.7%. Prefer IR illumination,
  then exposure, then gain.
- 2026-08-11 — `trigger_rate_limit: 0` tried and reverted the same day: it removes the
  exposure floor but costs **8-15%** of frames in transmission (delivery 85-92% vs 99.98%,
  released rate 87.7 → 76.9 fps) because every camera bursts onto its link at once after
  the shared trigger. Dead end.
- 2026-08-11 — `kick_max_lag` A/B (100,968 frames, 17 min, identical camera settings):
  240 → **12.34%** loss / 87.68 fps released, 480 → **0.88%** / 99.14 fps. 480 adopted on
  3dpose; the laggard drifts to whatever the cap is, so raising it further buys little.
- 2026-08-11 — the rotating laggard traced across sessions: cam1 (07-27), cam5 (08-11
  13:55), then cam2 (08-11 14:32). cam5 and cam2 are in the light-resend group, so the
  July hypothesis blaming the cams 1/4/6 packet loss was **retracted** — resends were not
  the mechanism.
- 2026-08-11 — NVENC lazy-import deadlock: the whole process wedged with one thread parked
  in `importlib.find_spec()` under `Encode()`; the import is now warmed once,
  single-threaded, inside the load lock.
- 2026-08-11 — double-start state bug: a second acquisition left the cameras streaming
  while `_state` read IDLE.

Dead ends, do not retry:
- `trigger_rate_limit: 0` (8-15% loss in transmission).

---

## 5. Root cause and the backend split (early September 2026)

- 2026-09-03 — **rotating-laggard root cause found**: `img = result.Array` in the grab loop
  is a GIL-held 2.3 MB memcpy (**0.837 ms**/frame/camera) against a 10 ms budget shared by
  every grab thread; whichever thread lost the GIL lottery became the laggard.
  `GetArrayZeroCopy` costs **0.157 ms** (5.33×). Rig result, 6 cameras: cycle 12.0 →
  **10.00 ms**, avg_proc 5.2-5.5 → 0.79-0.84 ms, underruns 245-882 → **0**, cross-camera
  lag median 235-479 → **median 0 / p95 1 / max 2**, forced drops 12.34% → **0**;
  `np.frombuffer(GetBuffer())` measured 0.902 ms, no better than `.Array`.
- 2026-09-03 — GIL-wait budget measured directly (`QueryThreadCycleTime`, not a wall-clock
  bracket): **≤300 µs** of GIL-held work per thread per frame is safe even at 17 threads;
  ~1000 µs blows the 10 ms budget at 11. This is the acceptance criterion for any hot-path
  change. A wall-clock timer around a GIL-releasing call reports the re-acquisition wait as
  work (a 0.08 ms copy read as 2.7 ms) — the mistake that misled the project twice.
- 2026-09-03 — transport CPU measured: both camera ports report `NumberOfReceiveQueues = 1`,
  so each port's ~78,000 packets/s funnel through one core's DPC (~46% on cores 0/1 at six
  cameras). Resend count is not loss — the discards are recovered.
- 2026-09-03 — physical network triage cancelled: after the zero-copy fix a 60 s 6-camera
  run had `Failed_Buffer_Count = 0` on all six at 100.00% capture. Resends were never the
  binding constraint; a grab loop too slow to drain the pool was.
- 2026-09-03 — 20-minute validation: 120,106 frames identical on all six, **99.95%** kept,
  crossing the 16-bit block-ID wrap without incident. Setting RSS to 4 queues applied but
  changed nothing measurable (DPC still ~46% on cores 0/1), so it is not the lever.
- 2026-09-03 — pypylon cold path extracted to `gui_app/backends/basler.py` behind the
  `CameraBackend` / `GrabResultProtocol` contracts; the per-frame grab result is
  deliberately **not** wrapped (a wrapper that materialised the array would reintroduce the
  GIL-held copy).
- 2026-09-03 — `1_calibrate.py` PEP 723 header removed: the solve now runs in the project
  env and works OFFLINE, and the `opencv-contrib-python>=4.7` floor moved to
  `pyproject.toml` with it.
- 2026-09-03 — two sketches held and swapped automatically per acquisition type; calibration
  always gets the recording-only sketch, so calibration can never activate stim.
- 2026-09-03 — `apply_exposure_gain()` runs on every acquisition start
  (`calibration_exposure_us`/`calibration_gain_db` profile fields), clamping to the exposure
  ceiling so a calibration exposure cannot leak into a recording.
- 2026-09-03 — padding check corrected (round-2 review): `result.PaddingX`/`PaddingY` (the
  grab-result fields) are always present and checked on every frame; the earlier text
  conflated them with `cam.PaddingX` (the nodemap feature, genuinely absent on this model).
- 2026-09-04 — block-ID rate check added (`frame_sync.check_block_id_rate()`), called from
  `sync_encode.stop()` and `align_recording()`. `BLOCK_RATE_TOL = 0.003` is measured across
  74 camera-sessions (+220..+250 ppm of configured); **1% rejected** because it lands
  exactly on the 1-in-100 partial skip, the silent case worth catching.

Dead ends, do not retry:
- Bracketing a GIL-releasing call with a wall-clock timer (measures wait as work).
- Physical network triage on the strength of resend counts (they are recoverable).

---

## 6. Nine cameras (10-14 September 2026)

- 2026-09-10 — third switch and subnet added (192.168.5.0/24 on Ethernet 3), cameras 7-9
  installed, `n_cameras` 6 → 9. New serials must sort after the existing ones or every
  later camera is renamed and old calibrations attach to the wrong hardware.
- 2026-09-10 — `max_num_buffer` **1000 → 250 → 600** in one day: the 9-camera RAM preflight
  refused 1000 (20.7 GiB of pool), then 250 was raised to 600 after cam9 held 2.4 s of
  delivery lag for a whole recording. Promoted from a `camera_manager` constant to a profile
  field so the preflight's own advice can be followed; invariant: pool ≥ `kick_max_lag`.
- 2026-09-10 — `kick_max_lag` **480 → 240 → 480** in one day: at 240, cam9 sat exactly 240
  frames (2.4 s) behind and **4,682** triggers that all nine cameras captured were
  force-dropped.
- 2026-09-10 — 9-camera calibration solved only **4** cameras while every per-camera figure
  (`paired 260/250`, `grid 4/3`) read satisfied: the co-visibility graph was three separate
  components and the solve keeps the **largest** connected component. Two groups sat at 46
  shared detections — they would have merged at `min_edge` 40 but not 80.
- 2026-09-10 — HUD thresholds `min_per_cam_shared`/`min_edge` **250/80 → 120/40**: 250/80
  was ~4× and ~2.7× the solver's caps, making a nine-camera calibration take far longer than
  the data could be used for.
- 2026-09-10 — `probe_network.py` located cam5/cam6 in ten seconds after they were plugged
  into each other's switches; the camera→port mapping changed, so never trust a written
  mapping — derive it off the wire.
- 2026-09-10 — NVENC session-release race documented (only the destructor frees a session);
  an explicit pop-and-delete plus `gc.collect()` proved insufficient (see 09-14).
- 2026-09-10 — laggard `Release()` cost noted at 3-4 ms against ~1 ms on healthy cameras;
  the GIL cost of `Release()` is unmeasured and parked.
- 2026-09-11 — switch flow control set **symmetric**: resends on the disabled-flow-control
  switch fell from **16,800 to ~10** per 90 s.
- 2026-09-11 — the two original switches swapped between NIC ports: the noisy group followed
  the **switch** (~11,000 resends either side), proving the asymmetry is the switch, not the
  NIC port or its interrupt placement. Do not investigate the host side again on the old
  notes.
- 2026-09-11 — first CPU-placement work (affinity, thread priority, timer resolution);
  every earlier optimisation attacked the GIL or the network only.
- 2026-09-11 — nine-camera DPC sample: cores 0/1 (P) at 55-56%, core 2 (E) at 66%, the rest
  under 3%; RSS queue count 1 → 4 changed nothing measurable.
- 2026-09-11 — `pin_capture_threads` enabled: a grab thread on an E-core runs a few percent
  slow; two grab threads on one core carrying NIC DPC gave monotonic lag on one camera
  (67 → 173 frames) with zero underruns — pure CPU contention.
- 2026-09-11 — thermal watch added: four of nine cameras sit above the 76 C `Critical`
  threshold by installation (cam6 peaked 80 C), so `warn_temp_c` is set to 0 until
  heatsinks; temperatures still go into session metadata.
- 2026-09-11 — 3dface HUD thresholds pinned explicitly so a change to the 3dpose-tuned code
  defaults cannot lower that rig's bar without a measurement on its own cameras.
- 2026-09-14 — `capture_core_exclude: [0, 1]`: keep grab threads off the NIC-DPC cores
  (median/p95/max lag 0/6/12 there vs 0/1/1 excluded).
- 2026-09-14 — rings allocated **before** triggers start: a camera whose ring was allocated
  late began 124 frames behind, rode `kick_max_lag` the whole session and force-dropped
  2,036 frames.
- 2026-09-14 — `calibration_min_edge` **40 → 20** (board-waving still demanded too many
  pairings; a 9/9 solve has succeeded at 40, so 20 keeps margin); the per-pair HUD nag was
  removed (it repainted every tick and named a pair the operator was often not working on).
- 2026-09-14 — stim guard added after a **59.9% mislabelled** recording: arduino-cli lost
  the serial-port race, Apply failed, and a recording proceeded labelled stimulated
  (1503 frames, 900 "stimulated") though nothing fired. Refuse until an Apply succeeds.
- 2026-09-14 — NVENC probe moved out of process: in-process, session release always races
  the next allocation, and two 600 s GUI recordings hung against the 12-session cap.
- 2026-09-14 — `probe_guard` added after three concurrent Panopticon instances (loop scripts
  that outlived their `pkill`) produced two false "divergence" findings that drove two
  since-reverted code changes; both a command-line scan and a lock file are required.
- 2026-09-14 — `probe_seq.py` added: three consecutive 600 s recordings in a fresh process
  pass while a **mixed** in-process sequence (recording → calibration → recording) degrades
  the second recording (encode queue qsize 183-204 of 200).

Unresolved at this point: one camera occasionally lags in a mixed in-process sequence; the
GIL cost of `Release()` is still unmeasured (deliberately parked).

Dead ends, do not retry:
- Confining RSS to the E-cores (`-BaseProcessorNumber 2`) — a regression.
- Pinning encoders to the P-core set (`encoder_pcores`) — quadrupled grab copy time
  (0.7 → 2.8 ms) and did not stop divergence.
- Pinning each encoder to a single E-core — one E-core cannot sustain a 1920×1200 stream at
  100 fps (cam9 blew out to 321 frames behind).

---

## 7. Standing dead ends (index)

Everything already tried and reverted, so nobody spends a rig day re-running it:

- Bigger `kick_max_lag` (1000 starved capture, 24% loss).
- `trigger_rate_limit: 0` (8-15% loss in transmission).
- `dtr=False` to suppress the serial reset (zero triggers).
- A pulldown on the PSU-III MOD line (internal pullup too stiff).
- Wall-clock timing of a GIL-releasing call (measures re-acquisition wait as work).
- Physical network triage on the strength of resend counts (resends are recoverable).
- Confining RSS to the E-cores (regression).
- Pinning encoders to the P-cores (regression).
- One encoder per E-core (321 frames behind).
- Widening the block-rate tolerance to 1% (lands on the 1-in-100 skip).
- A separate `_board_has_paradigm` flag (derive it from `_sketch_for` and
  `_session_stim_ino`; a parallel flag drifts from the sketch on the board).
- Unanchoring `/_*.py` in `.gitignore` (a bare `_*.py` also matches `__init__.py`).
