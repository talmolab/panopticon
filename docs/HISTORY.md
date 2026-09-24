# Project history

This is the engineering ledger: dated decisions, measurements and dead ends that
explain why the code and the profiles hold the values they do. The code states
each rule in the present tense and does not retell the story behind it; the
story is here.

Each entry is one bullet: the date, what changed or what was measured, and the
number that decided it. From September 2026 on, an entry names the commit it
landed in, in backticks, and a measurement that changed no code says so. Each
section ends with the dead ends it closed, so nobody runs them again, and the
last section indexes them all.

Live measurements and the current performance picture are in
[docs/INTERNALS.md](INTERNALS.md); this file is the chronology.

---

## 1. Calibration coverage HUD (May 2026)

- 2026-05-28: A full-video sleap-anipose solve of all cameras reached 0.37 px
  reprojection error on this rig (0.063 px on the other). The GUI's Solve and
  `1_calibrate.py` fit a subsample and degrade the result, so solve from full
  videos.
- 2026-05-29: Coverage HUD merged and validated on the rig: a live ChArUco
  coverage graph in the sidebar, 30 fps calibration capture and preview,
  parallel NVENC encode, a snapshot button. READY is a connected
  co-visibility graph, not all 15 pairs, because the board is one-sided and
  opposed cameras can never co-detect. The design rationale (READY conditions,
  marker versus corner counting, reprojection targets) moved into
  `docs/INTERNALS.md`, and the old `CALIBRATION_HUD_HANDOFF.md` was retired.

---

## 2. Capture pipeline stabilisation (June 2026)

- 2026-06-10: cam6 stream stall. A GigE stall freezes the grab loop and its
  frontier, so the coordinator force-drops every later trigger. Restarting the
  grab is the only recovery short of restarting the GUI.
- 2026-06-12: Inline encode removed from the grab loop. Encoding on the
  critical path broke the 10 ms per-frame budget under six-thread contention
  and dropped about 28% of frames.
- 2026-06-12: `gige_driver: socket` adopted. The in-kernel `filter` driver with
  default resend settings discards a frame on a lost packet instead of asking
  again: about 23% loss at 6 x 100 fps.
- 2026-06-12: Legacy ChArUco layout diagnosed. The physical board predates
  OpenCV 4.6, so the OpenCV 4.7 `CharucoDetector` finds 0 corners and reports
  nothing wrong without `setLegacyPattern(True)` (`board_legacy: true`).
- 2026-06-17: `kick_max_lag: 1000` starved capture (24% loss). The NV12 ring
  scales with the cap, and 1000 was too much memory pressure.

Dead ends, do not retry:
- A bigger `kick_max_lag` ring (1000 starved capture, 24% loss).

---

## 3. Optostim and laser safety (July 2026)

- 2026-07-24: `-g <fps>` (one IDR per second) and `-movflags +faststart` set on
  every mp4 writer. Without them one 898 s, 415 MB recording held a single IDR,
  could not be sought in LUC3D, and `ffprobe` could not walk it in 10 minutes.
- 2026-07-26: Stimulation node editor added. The stim board is the camera
  trigger board (one Arduino Mega on COM3), so the generated sketch does both
  jobs.
- 2026-07-26: Suppressing the DTR reset (`dtr=False`, `rts=False` before
  `open()`) tried. It removed the boot flash, but the board then ignored the
  config and emitted zero triggers (`Total_Packet_Count 0` on all six). The
  reset returns the sketch to `setup()` with a cleared receive buffer, and the
  start depends on it. Dead end.
- 2026-07-27: The laser flash in the reset window resolved by one long-lived
  serial connection plus an `RDY <n_cams> <fps>` ack. The reset moves to launch
  and Apply, never Record.
- 2026-07-27: OpenCV floor raised to 4.7. OpenCV 4.6 crashed on the
  `chessboardCorners` accessor and turned the `setLegacyPattern` guard into a
  no-op without a message.
- 2026-07-27: A 44-minute run lost 6.34% (a 23-minute run the same day lost
  43%). The cameras split into two network groups, one segment with about
  460,000 resend requests against about 313 for the other, and the worst camera
  rode `max_lag`.
- 2026-07-27: A 6.8 kΩ pulldown on the laser MOD line measured ineffective. The
  CNI PSU-III's MOD input has an internal pullup far stiffer than 6.8 kΩ, and a
  resistor low enough to beat it would exceed the Arduino's 20 mA per-pin
  limit. The hardware fix was abandoned; the long-lived connection is what
  solved it. Dead end.
- 2026-07-27: Stall recovery and `retire()`. `grab_thread` re-arms after 25
  consecutive timeouts (up to 5 times), `_resync_offset()` recovers the true
  ordinal from the device timestamp and refuses when the gap is not within 0.25
  of a period, and `retire()` drops a camera that cannot realign so the
  survivors keep recording aligned. cam6 had stalled on 2026-06-10, cam1 on
  2026-07-27.

Dead ends, do not retry:
- `dtr=False` or `rts=False` to suppress the reset (zero triggers).
- A pulldown on the PSU-III MOD line (the internal pullup is too stiff; use the
  interlock).

---

## 4. Exposure, limiter and the 240/480 A/B (August 2026)

- 2026-08-11: Recording exposure and gain raised from 2000 µs and 0 dB to
  3000 µs and 6 dB (about 3x). The old pair put 65% of pixels in levels 0-15,
  with 21.5% clipped at 0, destroyed at the ADC. Modelled, about 7x total would
  clip 12.7%. Prefer IR illumination, then exposure, then gain.
- 2026-08-11: `trigger_rate_limit: 0` tried and reverted the same day. It
  removes the exposure floor but costs 8-15% of frames in transmission
  (delivery 85-92% against 99.98%, released rate 87.7 down to 76.9 fps),
  because every camera bursts onto its link at once after the shared trigger.
  Dead end.
- 2026-08-11: `kick_max_lag` A/B over 100,968 frames (17 minutes, identical
  camera settings): 240 lost 12.34% and released 87.68 fps; 480 lost 0.88% and
  released 99.14 fps. 480 adopted on 3dpose. The laggard drifts to whatever the
  cap is, so raising it further buys little.
- 2026-08-11: The rotating laggard traced across sessions: cam1 (07-27), cam5
  (08-11 13:55), then cam2 (08-11 14:32). cam5 and cam2 are in the light-resend
  group, so the July hypothesis blaming packet loss on cams 1, 4 and 6 was
  retracted: resends were not the mechanism.
- 2026-08-11: NVENC lazy-import deadlock. The whole process wedged with one
  thread parked in `importlib.find_spec()` under `Encode()`. The import is now
  warmed once, single-threaded, inside the load lock.
- 2026-08-11: Double-start state bug: a second acquisition left the cameras
  streaming while `_state` read IDLE.

Dead ends, do not retry:
- `trigger_rate_limit: 0` (8-15% loss in transmission).

---

## 5. First cause and the backend split (early September 2026)

- 2026-09-03: First cause of the rotating laggard found. `img = result.Array` in
  the grab loop is a GIL-held 2.3 MB memcpy (0.837 ms per frame per camera)
  against a 10 ms budget every grab thread shares, and whichever thread lost
  the GIL lottery became the laggard. `GetArrayZeroCopy` costs 0.157 ms (5.33x
  less). Rig result at six cameras: cycle 12.0 down to 10.00 ms, avg_proc
  5.2-5.5 down to 0.79-0.84 ms, underruns 245-882 down to 0, cross-camera lag
  median 235-479 down to median 0, p95 1, max 2, and forced drops 12.34% down
  to 0. `np.frombuffer(GetBuffer())` measured 0.902 ms, no better than
  `.Array`. The laggard came back at nine cameras (section 6).
- 2026-09-03: GIL-wait budget measured with `QueryThreadCycleTime`, not a
  wall-clock bracket: 300 µs or less of GIL-held work per thread per frame is
  safe even at 17 threads, and about 1000 µs breaks the 10 ms budget at 11.
  This is the acceptance criterion for any hot-path change. A wall-clock timer
  around a GIL-releasing call reports the re-acquisition wait as work (a
  0.08 ms copy read as 2.7 ms), the mistake that misled the project twice.
- 2026-09-03: Transport CPU measured. Both camera ports report
  `NumberOfReceiveQueues = 1`, so each port's 78,000 packets per second go
  through one core's DPC (about 46% on cores 0 and 1 at six cameras). Resend
  count is not loss: the discards are recovered.
- 2026-09-03: Physical network triage cancelled. After the zero-copy fix a 60 s
  six-camera run had `Failed_Buffer_Count = 0` on all six at 100.00% capture.
  Resends were never the binding constraint; a grab loop too slow to drain the
  pool was.
- 2026-09-03: 20-minute validation: 120,106 frames identical on all six, 99.95%
  kept, crossing the 16-bit block-ID wrap without incident. RSS at 4 queues
  applied but changed nothing measurable (DPC still about 46% on cores 0 and 1),
  so it is not the lever.
- 2026-09-03: The pypylon cold path moved to `gui_app/backends/basler.py`
  behind the `CameraBackend` and `GrabResultProtocol` contracts. The per-frame
  grab result is not wrapped, because a wrapper that built the array would
  bring back the GIL-held copy.
- 2026-09-03: The PEP 723 header left `1_calibrate.py`. The solve runs in the
  project environment and works offline, and the `opencv-contrib-python>=4.7`
  floor moved to `pyproject.toml` with it.
- 2026-09-03: Two sketches held and swapped automatically per acquisition type.
  Calibration always gets the recording-only sketch, so a calibration can never
  activate stim.
- 2026-09-03: `apply_exposure_gain()` runs at every acquisition start
  (`calibration_exposure_us` and `calibration_gain_db` profile fields) and
  clamps to the exposure ceiling, so a calibration exposure cannot leak into a
  recording.
- 2026-09-03: Padding check corrected in a second review round.
  `result.PaddingX` and `PaddingY`, the grab-result fields, are always present
  and checked on every frame. The earlier text had confused them with
  `cam.PaddingX`, the nodemap feature, which this model does not have.
- 2026-09-04: Block-ID rate check added (`frame_sync.check_block_id_rate()`),
  called from `sync_encode.stop()` and `align_recording()`.
  `BLOCK_RATE_TOL = 0.003` is measured across 74 camera-sessions (+220 to
  +250 ppm of the configured rate). 1% was rejected because it lands on the
  1-in-100 partial skip, the case the check exists to catch.

Dead ends, do not retry:
- Bracketing a GIL-releasing call with a wall-clock timer (it measures the wait
  as work).
- Physical network triage on the strength of resend counts (resends are
  recovered).

---

## 6. Nine cameras (10-14 September 2026)

- 2026-09-10: Third switch and subnet added (192.168.5.0/24 on Ethernet 3),
  cameras 7-9 installed, `n_cameras` 6 to 9. New serials must sort after the
  existing ones, or every later camera is renamed and old calibrations attach
  to the wrong hardware.
- 2026-09-10: `max_num_buffer` 1000, then 250, then 600 in one day. The
  nine-camera RAM preflight refused 1000 (19.3 GiB of pool), and 250 was raised
  to 600 after cam9 held 2.4 s of delivery lag for a whole recording. The value
  became a profile field, so the preflight's advice can be followed; the pool
  must be at least `kick_max_lag`.
- 2026-09-10: `kick_max_lag` 480, then 240, then 480 in one day. At 240, cam9
  sat 240 frames (2.4 s) behind, and 4,682 triggers that all nine cameras
  captured were force-dropped.
- 2026-09-10: A nine-camera calibration solved only 4 cameras while every
  per-camera figure (`paired 260/250`, `grid 4/3`) read satisfied. The
  co-visibility graph was three separate components, and the solve keeps the
  largest. Two groups sat at 46 shared detections; they would have merged at
  `min_edge` 40 but not at 80.
- 2026-09-10: HUD thresholds `min_per_cam_shared` and `min_edge` from 250 and
  80 to 120 and 40. 250 and 80 were about 4x and 2.7x the solver's caps, which
  made a nine-camera calibration take far longer than the data could be used
  for.
- 2026-09-10: `probe_network.py` located cam5 and cam6 in ten seconds after
  they had been plugged into each other's switches. The camera-to-port mapping
  changes, so never trust a written one; derive it off the wire.
- 2026-09-10: Camera temperatures recorded in session metadata for the first
  time. Temperature did not order the laggards: the hottest camera (cam6,
  75 C) had the least lag.
- 2026-09-10: NVENC session-release race documented (only the destructor frees
  a session). An explicit pop-and-delete plus `gc.collect()` proved
  insufficient (see 09-14).
- 2026-09-10: The laggard's `Release()` noted at 3-4 ms against about 1 ms on
  healthy cameras. The GIL cost of `Release()` was unmeasured and parked.
- 2026-09-11: Switch flow control set to symmetric. Resends on the switch with
  flow control disabled fell from 16,800 to about 10 per 90 s.
- 2026-09-11: The two original switches swapped between NIC ports. The noisy
  group followed the switch (about 11,000 resends either side), so the
  asymmetry is the switch, not the NIC port or its interrupt placement. Do not
  investigate the host side again on the old notes.
- 2026-09-11: First CPU-placement work (affinity, thread priority, timer
  resolution). Every earlier optimisation had attacked the GIL or the network
  only.
- 2026-09-11: Nine-camera DPC sample: cores 0 and 1 (P-cores) at 55-56%, core 2
  (an E-core) at 66%, the rest under 3%. RSS queue count 1 to 4 changed nothing
  measurable.
- 2026-09-11: `pin_capture_threads` enabled. A grab thread on an E-core runs a
  few percent slow, and two grab threads on one core carrying NIC DPC gave one
  camera a monotonic lag (67 to 173 frames) with zero underruns: pure CPU
  contention.
- 2026-09-11: Thread-placement flags moved into the shared `rig_setup` path.
  The GUI had run unpinned while every probe read as pinned, because the
  preview grab threads pin as they start and a flag applied after `open_all`
  reached only the recording threads.
- 2026-09-11: Live thermal watch added (`thermal_poll_s`). Four of nine cameras
  sat above the 76 C Critical threshold by installation over a 600 s run (cam6
  peaked at 80 C against an 81 C shutdown), so the 3dpose profile kept the watch
  off (`thermal_poll_s: 0`) until heatsinks. Temperatures still went into
  session metadata.
- 2026-09-11: 3dface HUD thresholds pinned in its profile, so a change to the
  3dpose-tuned code defaults cannot lower that rig's bar without a measurement
  on its own cameras.
- 2026-09-14: `capture_core_exclude: [0, 1]` keeps grab threads off the NIC DPC
  cores (median, p95 and max lag 0, 6 and 12 there, against 0, 1 and 1
  excluded).
- 2026-09-14: Rings allocated before the triggers start. A camera whose ring was
  allocated late began 124 frames behind, rode `kick_max_lag` the whole session
  and force-dropped 2,036 frames.
- 2026-09-14: `calibration_min_edge` 40 to 20. Waving the board still demanded
  too many pairings; a 9 of 9 solve had succeeded at 40, so 20 keeps a margin.
  The per-pair HUD prompt was removed: it repainted every tick and named a pair
  the operator was often not working on.
- 2026-09-14: Stim guard added after a recording was 59.9% mislabelled.
  arduino-cli lost the serial-port race, Apply failed, and a recording went
  ahead labelled stimulated (1503 frames, 900 of them "stimulated") though
  nothing fired. Record is refused until an Apply succeeds.
- 2026-09-14: NVENC probe moved out of process. In-process, the session release
  always races the next allocation, and two 600 s GUI recordings hung against
  the 12-session cap.
- 2026-09-14: `probe_guard` added after three concurrent Panopticon instances
  (loop scripts that outlived their `pkill`) produced two false "divergence"
  findings that drove two code changes, both since reverted. It needs both a
  command-line scan and a lock file.
- 2026-09-14: `probe_seq.py` added. Three consecutive 600 s recordings in a
  fresh process pass, while a mixed in-process sequence (recording,
  calibration, recording) degrades the second recording (encode queue qsize
  183-204 of 200).

Unresolved at this point: one camera occasionally lags in a mixed in-process
sequence, and the GIL cost of `Release()` is unmeasured (parked).

Dead ends, do not retry:
- Confining RSS to the E-cores (`-BaseProcessorNumber 2`): a regression.
- Pinning encoders to the P-core set (`encoder_pcores`): grab copy time rose
  from 0.7 to 2.8 ms, and divergence did not stop.
- Pinning each encoder to a single E-core: one E-core cannot sustain a
  1920 x 1200 stream at 100 fps (cam9 fell 321 frames behind).

---

## 7. Standing dead ends (index)

Everything already tried and reverted, so nobody spends a rig day on it again:

- Bigger `kick_max_lag` (1000 starved capture, 24% loss).
- `trigger_rate_limit: 0` (8-15% loss in transmission).
- `dtr=False` to suppress the serial reset (zero triggers).
- A pulldown on the PSU-III MOD line (internal pullup too stiff).
- Wall-clock timing of a GIL-releasing call (measures re-acquisition wait as
  work).
- Physical network triage on the strength of resend counts (resends are
  recovered).
- Confining RSS to the E-cores (regression).
- Pinning encoders to the P-cores (regression).
- One encoder per E-core (321 frames behind).
- Widening the block-rate tolerance to 1% (lands on the 1-in-100 skip).
- A separate `_board_has_paradigm` flag (derive it from `_sketch_for` and
  `_session_stim_ino`; a parallel flag drifts from the sketch on the board).
- Unanchoring `/_*.py` in `.gitignore` (a bare `_*.py` also matches
  `__init__.py`).
