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
  consecutive timeouts, up to 5 times. `_resync_offset()` recovers the true
  ordinal from the device timestamp and refuses when the gap is not within 0.25
  of a period. `retire()` drops a camera that cannot realign, so the survivors
  keep recording aligned. cam6 had stalled on 2026-06-10, cam1 on 2026-07-27.

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
  5.2-5.5 down to 0.79-0.84 ms, and underruns 245-882 down to 0. Cross-camera
  lag went from a median of 235-479 to median 0, p95 1, max 2, and forced drops
  from 12.34% to 0. `np.frombuffer(GetBuffer())` measured 0.902 ms, no better than
  `.Array`. The laggard came back at nine cameras (section 6); its main cause
  is in section 8.
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
sequence, and the GIL cost of `Release()` is unmeasured (parked). Section 8
finds the holder.

Dead ends, do not retry:
- Confining RSS to the E-cores (`-BaseProcessorNumber 2`): a regression.
- Pinning encoders to the P-core set (`encoder_pcores`): grab copy time rose
  from 0.7 to 2.8 ms, and divergence did not stop.
- Pinning each encoder to a single E-core: one E-core cannot sustain a
  1920 x 1200 stream at 100 fps (cam9 fell 321 frames behind).

---

## 7. The audit, the simulated rig and the public release (15-21 September 2026)

- 2026-09-17: The calibration solve keeps the largest connected group of
  cameras instead of the group holding cam1, records every quality figure, and
  exits with machine-readable error codes (`9184c1f`). The GUI launches it with
  `sys.executable`, so the solve no longer needs uv (`0ece45e`).
- 2026-09-17: Every rung of the NVENC keyword ladder keeps an explicit GOP, and
  an exhausted ladder raises (`19e03be`).
- 2026-09-18: The GOP never reached NVENC. The ladder passed `gopLength` and
  `idrPeriod`, which PyNvVideoCodec ignores, so every mp4 held one keyframe
  (27,114 frames, 1 IDR) and seeking decoded from the start of the file. The
  keys are lowercase `gop` and `idrperiod`, and the launch preflight counts IDRs
  with `nvenc.gop_is_honoured()` (`a6f489e`). The offline test had asserted the
  wrong names, so it enforced the bug. Recordings made before keep their single
  keyframe.
- 2026-09-18: A simulated camera backend and trigger board with fault injection
  (`42828da`), and a suite that drives the whole window on it with pypylon
  unimportable (`732f199`). The window then loads the camera backend only after
  the profile has named it, so Panopticon starts on a host without the vendor
  SDK (`5c144f0`).
- 2026-09-18: libx264 CPU encoder behind the encoder factory (`813246e`). A host
  with no usable NVENC had only `raw.bin`, about 129 GiB per camera per 10
  minutes at 1920 x 1200 and 100 fps. The launch hardware check runs the NVENC
  session probe, the libx264 bench and the encoder selection off the UI thread
  (`160aaa9`, `7c5a984`).
- 2026-09-18: Cameras are named by the profile's `camera_serials`: cam(i+1) is
  entry i, an unlisted device is ignored, and a listed serial that did not
  enumerate is refused by name (`e1b7963`).
- 2026-09-18: The legacy campy acquisition path and its submodule removed;
  nothing imported them (`71a10db`). The package metadata declares the license,
  and the vendor SDKs moved into a dependency group (`d81564b`).
- 2026-09-18: The trigger sketch prints an 8-hex sketch identity in its RDY ack,
  so the host compares what the board runs with what it wants (`8fc65eb`). A
  stop is confirmed by `RDY n 0` on RDY firmware (`4a43133`).
- 2026-09-18: The test suites had been rewriting the operator's remembered
  profile. `QSettings.setDefaultFormat` plus `setPath` does not redirect a
  two-argument `QSettings(ORG, APP)`, so the next launch would have opened the
  simulated rig. `PANOPTICON_SETTINGS_FILE` now redirects the store (`486cbc9`).
- 2026-09-19: First rig session of the audited code, nine cameras, 20 s at
  100 fps. Every camera had one keyframe per second, and seek time was flat at
  55-87 ms against 2.5-9.0 s on an older file. Block IDs were identical on all
  nine, and forced was 0. cam6 alone degraded (lag 129, 244, then 278 of 480; cycle 12.7 ms) and was
  also the hottest camera (77.6 C, peak 80.6 C). Measured on the rig; no code
  change.
- 2026-09-19: Later the same day a 5-minute calibration and a 5-minute
  recording peaked at a lag of 1 of 480 with cam6 at the same temperature (77.8
  C, peak 79.7 C), so heat alone did not predict the laggard. Frame loss was per
  switch: the three cameras on one switch lost 12 buffers each and the other six
  none. Measured on the rig; no code change.
- 2026-09-20: GPL-3.0 license text added (`b10f992`). The repository was made
  public, with the audit branch as its first pull request.
- 2026-09-21: The overwrite prompt deletes the earlier session and records
  fresh under the same name, instead of moving it aside; repeat takes had left
  `.previous-HHMMSS` folders to prune by hand. The prompt defaults to Cancel,
  and the delete runs after the serial port opens (`31fff80`).
- 2026-09-21: A kick-mode session no longer runs a post-hoc alignment re-encode
  by itself. It says when the videos are unequal and points at `2_align.py`.
  Heat is reported after a recording only when frames were lost (`52bae8d`).
- 2026-09-21: Calibration exposure 15 ms to 5 ms after the lighting was
  improved, and the live thermal watch back on (`thermal_poll_s` 0 to 20)
  (`78721e0`).
- 2026-09-21: The new lighting heated the array. Seven of nine cameras sat above
  Critical, and six peaked at 81.0-81.1 C, the shutdown point, where a camera
  stops delivering. The fault rotated from cam4 to cam6 to cam5 across switches,
  and the coordinator force-dropped whole sessions (20,560 forced in one run).
  After a cool-down (camera power unplugged, lights off) the recording was clean:
  worst lag 54 of 480, forced 0. Measured on the rig; no code change.
- 2026-09-21: The audit merged into master (`302d943`). This ledger replaced the
  dated narrative in CLAUDE.md (`ade997b`). The public tree was slimmed: the test
  suites, dev probes, tools and doc generators became local-only and gitignored
  (`2cf12e1`).

---

## 8. The GIL holder found (22 September 2026)

- 2026-09-22: The first rig baseline was blocked on heat. At idle four cameras
  sat at 78.3-79.6 C, flat for ten minutes, and cam6's over-temperature error
  count read 3. The maintainer fitted heatsinks; after a cool-down all nine read
  34.5-35.4 C. Measured on the rig; no code change.
- 2026-09-22: The excursion caught live on the heatsinked array, in three
  150-165 s runs of the capture probe. It was a drift, not a gap: cam2, whose
  grab thread was pinned to CPU 11, fell 205, 367 and 367 frames behind (of
  480) at 10-13 frames per second, with forced 0. In 20 of 20 native py-spy
  dumps its grab thread was waiting to re-acquire the GIL. The GIL was held
  about 77% of wall time, and 87-90% of that by the encoder threads inside
  PyNvVideoCodec's `Encode()`, uploading each NV12 frame (`cuMemcpy2DAsync_v2`)
  without releasing the GIL. Of the capture cores, CPU 11 alone carried about
  13% interrupt and 5% DPC time. Measured on the rig; no code change.
- 2026-09-22: Heatsinked idle equilibrium at 15:33: 72.1-77.2 C and still
  creeping (cam6 77.2, cam4 76.7, cam5 76.2 C, all three at Critical). A
  recording adds 1-2 C. On the a2A1920-165g5m the Critical (76.0 C),
  over-temperature (81.0 C) and hysteresis (4 C) values are read-only firmware
  values. Measured on the rig; no code change.
- 2026-09-22: RSS false alarm. The rig check read all three camera ports at 4
  queues on processors 2-9, the setting section 6 records as a regression. The
  read was unelevated, and `Get-NetAdapterRss` without elevation reports values
  that are not the adapter's (with `MaxProcessors` and `RssProcessorArray`
  blank). Read elevated, the ports had been at 1 queue on processors 0-23
  throughout, so no baseline changed. `configure_nic.ps1 -Check` now refuses the
  RSS verdict when it is not elevated (`c0a1e39`).
- 2026-09-22: Temperature reads do not starve the grab threads. Across 39
  archived nine-camera runs that read every camera's temperature every 5 s, the
  slowest camera fell below 90 fps in 1.4% of the intervals containing a read
  (n = 793) against 2.9% of the others (n = 3,211). Closed; no code change.
- 2026-09-22: The morning's "cam6 158 of 480" run was loss plus drift, not one
  slow camera. cam6 retrieved 8 frames in 1.4 s while every other camera
  retrieved about 145, then cam2 to cam6 drifted together for about 10 s. No
  code change.
- 2026-09-22: Simultaneity measured with an IR LED on the stim pin pulsed at
  2 Hz for 45 s: all 92 rising edges landed on the same trigger in all nine
  cameras. Measured on the rig; no code change.
- 2026-09-22: GIL-free upload prototype, on the GPU alone. It copies each frame
  into page-locked staging with numpy, which releases the GIL, and lets the
  driver DMA it. The GIL held per `Encode()` fell from 0.44-0.72 ms to about
  0.07-0.09 ms, and the encoders' GIL occupancy at 9 x 100 fps from about 42% to
  10-15%, with byte-identical bitstreams. A host synchronize of the encoder
  stream drained NVENC's pipeline and spun a core (2.7 ms of CPU per frame);
  per-buffer CUDA events replaced it. No code change.
- 2026-09-22: Rig A/B of that prototype, seven 150 s runs, against the
  maintainer's bar (after the first 2 s no camera more than 5 frames behind, and
  forced 0). The production path passed 0 of 3 (worst 267, 155 and 45 frames)
  and had failed 6 of 6 runs that day. The pinned upload passed 2 of 3 (56, 5,
  5), kept every camera within 3 frames after 15 s in all four pinned runs, and
  forced was 0 in all seven. What remained were short events 4-12 s after the
  start that hit one switch's cameras together, alongside incomplete buffers or
  NIC DPC saturation on CPUs 0-2. Cost: about 0.8 more cores and 2.6 GiB more
  private memory. Measured on the rig; no code change.
- 2026-09-22: Page-locking the whole NV12 ring, instead of copying each frame
  into staging buffers, was the prototype's runner-up. It was the only variant
  that also lowered CPU, to 0.36-0.46 ms per frame against 0.88-1.03 ms for the
  production path. It pins 21.5 GiB at nine cameras, CUDA warns against
  page-locking that much, and it is safe only in kick mode. It was not run on
  the rig and stays open: a rig A/B is worth it if the staged upload's extra
  0.8 cores matters. No code change.
- 2026-09-22: Thermal policy. A fan is not an option on this rig, and the
  cameras' thresholds cannot be raised, so Panopticon's reaction changed. It
  warns at the camera's reported shutdown point minus `thermal_warn_margin_c`,
  and no longer on Critical alone (`86e18a7`; the live watch in `f4ad2da`). The
  program default is 3.0 C; the 3dpose profile sets 2.0, so it warns at 79 C.
  `BsliDeviceTemperatureOverwrite*` fakes the reading and defeats the camera's
  shutdown, so Panopticon never writes it.
- 2026-09-22: FLIR support is a `ctypes` binding to the Spinnaker C API
  (`224eccf`) with a simulated SDK behind the same methods (`71992c1`), not
  PySpin. The C calls release the GIL, give zero-copy frames and need no wheel;
  PySpin's GIL behaviour and copying are undocumented, and its Python 3.10 wheel
  is built for NumPy 1.x while Panopticon needs NumPy 2.
- 2026-09-22: Free-threaded CPython (3.14t) checked and set aside: only numpy
  ships cp314t wheels, and pypylon, PyNvVideoCodec, PyQt5 and OpenCV do not. No
  code change.

Dead ends, do not retry:
- A host synchronize of the encoder's CUDA stream per frame (it drains NVENC
  and spins a core).
- Writing `BsliDeviceTemperatureOverwrite*` to quiet the thermal alert (it
  defeats the camera's shutdown).
- A shorter `sys.setswitchinterval` against the encoders' GIL hold: a C call
  that holds the GIL cannot be preempted.
- Reading RSS from an unelevated shell (the values are not the adapter's).

---

## 9. The drop fix, FLIR and the review fixes (22-24 September 2026)

- 2026-09-22: Backend contract: optional members are read with `getattr`, the
  Basler exposure-ceiling formula lives in the backend (`1ec1db0`), and each
  camera's timestamp tick rate is logged, with a warning when it is not 1 GHz
  (`8b5261f`). Merged as `31c830d`.
- 2026-09-22: Profile schema. The `camera:` block serves backends without a
  settings file, and FLIR refuses the Basler-only fields (`1b6e4f0`).
  `realtime_kick` defaults to true (`1b6c2d8`). An unknown backend, a quality
  outside 0-51 and a pool below `kick_max_lag` are refused at load (`16fef54`).
  Merged as `3839cc3`.
- 2026-09-22: Offline pipeline. A replace never cuts surviving cameras to a
  retired or short camera's length (`49c89bd`). The stim trace is rewritten
  whenever alignment replaces videos (`8663079`). A replace decodes every coded
  frame once and refuses on a count mismatch (`2f0f53c`). Merged as
  `c49545e`.
- 2026-09-22: App safety. Every exchange with the trigger board holds one lock,
  and a start does not retry after a quit (`8657083`). A start retry is refused
  once armed cameras counted triggers (`7b29419`), on pre-RDY firmware too
  (`527f47e`). The quit stands the board down even when the link reads as
  closed, and the port cannot be reopened afterwards (`a1f71f3`, `15ec781`). A
  second launch is refused before it touches hardware (`0867351`). Merged as
  `8da2802`.
- 2026-09-22: The GIL-free upload ported behind `nvenc.configure_upload`, host
  by default (`a30e89a`), with `pinned_upload_matches_host`, which stalls the
  encoder stream to prove the library copies on it (`ebc8f9c`). Merged as
  `a8ff8cf`.
- 2026-09-22: Capture core: kick-mode ring slots have an owner, so a backlogged
  frame is never overwritten (`a25c897`). A retirement's backlog is released at
  a pace the encoder queues can take (`f964746`), after rig logs from 09-21
  showed 2,221-2,227 queue-full drops (277-279 per surviving camera) blamed on a
  wedged encoder. A `stop()` that lands before `run()` is honoured; it had left
  a preview thread retrieving beside the recording thread (`172f6ea`). Failing
  grabs and dead starts retire the camera (`ceb4b7b`). Merged as `f3597c1`.
- 2026-09-23: Calibration hints and pairing use trigger ordinals, not frame
  indices (`9b2208f`). In a rig session with drops, 56% of near-coincident
  hints between two cameras were one frame apart, against 2-3% in sessions
  without drops. On a synthetic session with one missed trigger the affected
  pairs solved at 23.3 and 19.1 px, and at 0.17-0.19 px after the change.
  Merged as `5fee287`.
- 2026-09-23: The main window never starts the trigger board with an unarmed
  camera, and refuses a start when any camera took a frame before it
  (`59885f0`). Without that barrier, a camera armed 0.5 s late on the simulator
  recorded 49 trigger periods of offset with identical block IDs, which the rate
  check cannot see. The thermal alert follows the margin rule (`f4ad2da`).
  Merged as `799d2b6`.
- 2026-09-23: Multi-process capture, stage 1: capture worker processes behind a
  `CameraManager` facade, off by default (`0e79ff5`). Merged as `41e790d`. The
  GUI opens no camera for a profile with `capture_processes` above 0 until
  stage 2 (`8d11f4b`).
- 2026-09-23: External TTL trigger mode (`trigger_source: external`). The
  recording is refused when any camera receives a frame before every camera is
  armed (`4139318`). Merged as `fc61510`.
- 2026-09-23: A silence every camera shares is waited out for two stall windows
  (about 15 s), and then the cameras re-arm, because a stalled shared switch,
  NIC port or USB host needs the re-arm. The bound of two windows is a choice,
  not a measurement (`c295cf1`).
- 2026-09-23: Rig check of the merged pinned upload, eight 150 s runs over
  three arms. The GIL drift is gone (grab-thread `rel` 0.02-0.04 ms against
  0.44-1.12 ms on the host upload), but only 1 of 6 pinned runs met the bar. The
  failures were events grouped by switch segment: CPU 2 DPC at a median 62-66%
  with peaks of 76-104%, bursts of incomplete buffers, and about 2% of triggers
  lost on the wire in two runs. The host upload drifted cam6 to 480 frames (488
  forced). Shared and own CUDA contexts lagged alike, and own used 1.5-2.2 GiB
  more GPU memory. Measured on the rig; no code change. A network and interrupt
  investigation is next.
- 2026-09-23: The pinned upload with the shared context became the profile
  default (`d0f7bac`).
- 2026-09-23: NV12 ring leak. After one GUI recording the process kept about
  21.6 GiB, the nine cameras' rings, and the next Record was refused ("33.1 GiB
  needed, 18.1 GiB available"). The ring, the router's sink and the encoder's
  recycle hook formed a reference cycle that only a full collection frees, which
  a quiet GUI never runs. Each acquisition now frees its rings when it stops
  (`f8566ee`). Every probe run and suite had passed; only the second Record in
  the real GUI showed it. In the maintainer's GUI session after the fix (two
  recordings and a calibration), memory went back to about 13 GiB after each
  stop, the worst lag was 1 frame and forced was 0.
- 2026-09-23: Calibration solve crash. `cv2.calibrateCamera` asserted in
  `initIntrinsicParams2D` on two partial views of cam2 whose corners lay on one
  board row, and ordinal hints find more partial views. Views that fit no
  homography are now skipped before the fit, and one camera's OpenCV error fails
  only that camera (`032e054`). The failing nine-camera session then solved with
  every pair graded good.
- 2026-09-23: cam6 got a second heatsink and idles about 2.5 C cooler. No code
  change.
- 2026-09-23: The FLIR backend merged (`46e7d82`). Five review rounds kept
  finding edge cases in the trigger witness, and round 5 let a dead narrow
  counter read as a clean witness past half its period (`7761615`). The rule
  became conservative: the witness never reports a clean camera from counts it
  cannot prove, and a counter narrower than `2**31` gives no count past half its
  period (`d3a3e64`, `2cf8690`).
- 2026-09-23: Camera-manager seam for other backends: `backend.open` receives
  the frame size and rate, and a backend's rate refusal refuses the start before
  any exposure is written (`144f568`). A pinned encoder counts itself closed only
  after its resources are freed, which removed a false teardown leak warning the
  rig check had printed (`66c875b`). Merged as `133c001`.
- 2026-09-23: Session logging, verbose for the volunteer phase at the
  maintainer's request, with the rule that logging must cost the acquisition
  nothing. Every line is stamped with milliseconds and its thread and written
  by one background thread from a bounded queue that drops and counts instead of
  blocking (`5aa3467`). `log_level` is normal, verbose or debug, default verbose
  (`405fdfa`). A session header, requested and read-back camera settings and a
  `session.log` in every session folder (`61d516b`). The 3dface `.pfs` gave 71
  false "(differs)" read-back lines per camera until only the writes the camera
  holds after the load were compared (`3df80ae`).
- 2026-09-23: The kick-out paragraph after a recording became one line with the
  effective frame rate (`61812bb`). The soft "RAM is tight" warning and its
  "Start anyway?" prompt were removed at the maintainer's request, and the hard
  RAM refusal stays (`ac5742f`).
- 2026-09-24: Logging merged (`e82e165`). `probe_flir.py`, the FLIR volunteer
  probe (`1489916`), and a `probe_network.py --sweep` that goes through the
  profile's backend (`08d046f`), merged as `bc99eaf`. The FLIR bring-up guide
  and issue template (`428eb97`).
- 2026-09-24: On a computer with no remembered profile, every launch opened
  3dface, reset COM3 and flashed 3dface's sketch, which holds no pin low at
  boot, until the operator chose a profile. Such a launch now opens no camera
  and no serial port and flashes nothing until the operator chooses, and
  `gui.py --profile NAME` opens and remembers a profile (`bf345be`).
- 2026-09-24: `rig_setup` applies the profile's `log_level`, so `probe_lag.py`
  (which `probe_mp.py` runs) and the capture workers log at it, and a
  normal-versus-verbose comparison measures something (`8d03136`).

Dead ends, do not retry:
- Validating a fix with headless probes and suites alone (the ring leak passed
  them all; drive the real GUI through repeated acquisitions).
- Patching the FLIR witness one edge case at a time (each round found another;
  the conservative rule replaced it).

---

## 10. Standing dead ends (index)

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
- Chasing the laggard by camera temperature (the hottest camera had the least
  lag on 09-10, and no laggard appeared at the same temperature on 09-19).
- A host synchronize of the encoder's CUDA stream per frame (drains NVENC, spins
  a core).
- Writing `BsliDeviceTemperatureOverwrite*` (defeats the camera's shutdown).
- A shorter switch interval against the encoders' GIL hold (a C call holding
  the GIL cannot be preempted).
- Free-threaded CPython while pypylon, PyNvVideoCodec, PyQt5 and OpenCV ship no
  free-threaded wheels.
- Reading RSS unelevated (wrong values).
- Validating a fix without driving the real GUI through repeated acquisitions.
- Patching the FLIR witness edge case by edge case.
