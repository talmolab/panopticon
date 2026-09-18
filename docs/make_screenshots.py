"""Generate every screenshot used by the documentation.

    uv run python docs/make_screenshots.py            # everything
    uv run python docs/make_screenshots.py widgets    # no cameras needed
    uv run python docs/make_screenshots.py main       # cameras needed
    uv run python docs/make_screenshots.py stim       # no cameras needed

    --profile NAME   rig profile stem to illustrate (default 3dpose); the
                     available stems are listed when it does not exist
    --wait-s N       seconds the main window runs before its screenshot is
                     taken (default 50: startup firmware check + live preview)

Positions for the numbered callouts come from Qt's own widget geometry rather
than from measuring pixels, so they stay correct if the layout, the window size
or the camera count changes.

The coverage-graph stages and the waveform previews are rendered by driving the
widgets with constructed values. They are illustrations of specific states, not
captures of a particular session, and the docs say so where they appear. The
coverage stages are expressed RELATIVE to the profile's thresholds and the
READY flag comes from the detector's own rule, so the captions in the images
follow the profile instead of drifting from it.
"""
import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt, QTimer, QPoint, QRect
from PyQt5.QtGui import QPainter, QPen, QColor, QFont, QBrush

OUT = REPO / "docs" / "images"
ACCENT = QColor("#ff3b6b")
HALO = QColor(0, 0, 0, 190)


# ─── shared callout drawing ──────────────────────────────────────────────────
def _rect_in(win, widget):
    if widget is None or not widget.isVisible():
        return None
    return QRect(widget.mapTo(win, QPoint(0, 0)), widget.size())


def draw_callouts(win, targets, column_from=None):
    """Grab `win` and draw numbered callouts. Returns (pixmap, missing_numbers).

    `targets` is a list of (number, resolver, placement[, dy]). `column_from`
    resolves a widget whose left edge all "left" badges align to, so a badge
    never lands on the label of the control it points at.
    """
    pix = win.grab()
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setFont(QFont("Segoe UI", 11, QFont.Bold))

    col_ref = _rect_in(win, column_from(win)) if column_from else None
    col_x = (col_ref.left() - 22) if col_ref else None

    missing = []
    for entry in targets:
        num, resolve, placement = entry[0], entry[1], entry[2]
        dy = entry[3] if len(entry) > 3 else 0
        try:
            rect = _rect_in(win, resolve(win))
        except Exception:
            rect = None
        if rect is None or rect.width() <= 0:
            missing.append(num)
            continue

        # NoBrush: the badge sets a fill, and leaving it set makes every later
        # outline draw as a solid block over the control it points at.
        p.setBrush(Qt.NoBrush)
        for pen in (QPen(HALO, 4), QPen(ACCENT, 2)):
            p.setPen(pen)
            p.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), 4, 4)

        r = 15
        if placement == "inside":
            centre, anchor = QPoint(rect.left() + r + 8, rect.top() + r + 8), None
        elif placement == "right":
            centre = QPoint(rect.right() + 42, rect.center().y() + dy)
            anchor = QPoint(rect.right() + 6, rect.center().y() + dy)
        elif placement == "above":
            centre = QPoint(rect.center().x(), rect.top() - 26 + dy)
            anchor = QPoint(rect.center().x(), rect.top() - 3 + dy)
        elif placement == "below":
            centre = QPoint(rect.center().x(), rect.bottom() + 26 + dy)
            anchor = QPoint(rect.center().x(), rect.bottom() + 3 + dy)
        else:  # left
            x = col_x if col_x is not None else rect.left() - 40
            centre = QPoint(x, rect.center().y() + dy)
            anchor = QPoint(rect.left() - 6, rect.center().y() + dy)

        centre.setX(max(r + 2, min(centre.x(), pix.width() - r - 2)))
        centre.setY(max(r + 2, min(centre.y(), pix.height() - r - 2)))

        if anchor is not None:
            for pen in (QPen(HALO, 5), QPen(ACCENT, 2)):
                p.setPen(pen)
                p.drawLine(centre, anchor)

        p.setPen(QPen(HALO, 3))
        p.setBrush(QBrush(ACCENT))
        p.drawEllipse(centre, r, r)
        p.setPen(QPen(Qt.white))
        p.drawText(QRect(centre.x() - r, centre.y() - r, 2 * r, 2 * r),
                   Qt.AlignCenter, str(num))
    p.end()
    return pix, missing


def save(pix, name):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    pix.save(str(path))
    print(f"  {name}  ({pix.width()}x{pix.height()})")


# ─── coverage-graph stages (no hardware) ─────────────────────────────────────
def _detector(n, profile, pair, per_cam, glow_idx=(), cells=4, weak=None,
              split=None):
    """A BoardDetector in a constructed state.

    ``pair`` and ``per_cam`` are FRACTIONS of the profile's ``min_edge`` and
    ``min_per_cam_shared``; ``weak`` is ``(i, j, fraction)`` for one pair below
    the others; ``split`` is ``(k, fraction)`` and puts every pair between the
    first k cameras and the rest at that fraction, which is how a graph in two
    groups is drawn (one weak pair in an otherwise complete graph is still
    connected). Both per-camera counters are set (``per_cam_frames`` is what
    READY and the caption use, ``per_cam_covis`` is the display weighting) and
    READY comes from ``_update_ready()``, so the image can never show a state
    the real detector would not, such as READY over a disconnected graph.
    """
    import numpy as np
    from gui_app.board_detector import BoardDetector
    det = BoardDetector(
        n, profile.board_config,
        min_per_cam_shared=profile.calibration_min_per_cam_shared,
        min_edge=profile.calibration_min_edge,
        min_grid_cells=profile.calibration_min_grid_cells)
    edge = int(round(pair * det.min_edge))
    frames = int(round(per_cam * det.min_per_cam_shared))
    det.shared = np.full((n, n), edge, dtype=int) - np.eye(n, dtype=int) * edge
    if weak:
        i, j, frac = weak
        det.shared[i, j] = det.shared[j, i] = int(round(frac * det.min_edge))
    if split:
        k, frac = split
        cross = int(round(frac * det.min_edge))
        for i in range(k):
            for j in range(k, n):
                det.shared[i, j] = det.shared[j, i] = cross
    det.per_cam_frames = np.full(n, frames, dtype=int)
    det.per_cam_covis = np.full(n, frames * max(1, n - 1), dtype=int)
    det.glow = np.zeros(n)
    for i in glow_idx:
        det.glow[i] = 1.0
    det.grid_cells_hit = np.full(n, cells, dtype=int)
    det._update_ready()
    return det


def shot_coverage_stages(profile, n=6):
    from gui_app.widgets.coverage_graph import CoverageGraphWidget
    # Fractions of the profile thresholds: 1.0 is the bar. Stage 3 has every
    # per-camera figure satisfied but the cameras in two groups whose cross
    # pairs sit at half the bar, so the caption reads "groups 2/1" and READY
    # stays off however the thresholds are tuned: the state that cost a real
    # nine-camera session.
    stages = [
        ("calib_stage_1_start.png", dict(pair=0.0,  per_cam=0.0,  glow_idx=(),     cells=0)),
        ("calib_stage_2_partial.png", dict(pair=0.6,  per_cam=0.5,  glow_idx=(0, 2), cells=2)),
        ("calib_stage_3_nearly.png", dict(pair=1.75, per_cam=1.6,  glow_idx=(4,),   cells=3,
                                          split=(n // 2, 0.5))),
        ("calib_stage_4_ready.png", dict(pair=4.0,  per_cam=2.2,  glow_idx=(),     cells=4)),
    ]
    from PyQt5.QtWidgets import QWidget, QVBoxLayout
    print("coverage-graph stages:")
    for name, kw in stages:
        # The widget paints transparently because in the app it sits on the dark
        # sidebar. Standalone that makes the READY state (solid white) invisible,
        # so give it the same backdrop it has in the GUI.
        holder = QWidget()
        holder.setStyleSheet("background: #141428;")
        holder.setFixedSize(300, 240)
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(6, 6, 6, 6)
        w = CoverageGraphWidget()
        w.setup(n)
        det = _detector(n, profile, **kw)
        w.update_from(det)
        lay.addWidget(w)
        holder.show(); QApplication.processEvents(); QApplication.processEvents()
        save(holder.grab(), name)
        print(f"    paired {int(det.per_cam_frames.min())}/{det.min_per_cam_shared}"
              f"  grid {int(det.grid_cells_hit.min())}/{det.MIN_GRID_CELLS}"
              f"  groups {len(det.components)}/1  ready={det.ready}")
        holder.close()


# ─── waveform previews (no hardware) ─────────────────────────────────────────
def shot_waveforms():
    from gui_app.widgets.stimulation_window import WaveformPreview
    cases = [
        ("wave_10hz_10ms.png",  10.0, 10.0),    # 10% duty — a normal train
        ("wave_20hz_25ms.png",  20.0, 25.0),    # 50% duty
        ("wave_10hz_100ms.png", 10.0, 100.0),   # pw == period -> constant ON
        ("wave_0hz.png",         0.0, 10.0),    # 0 Hz -> pin held LOW
        ("wave_40hz_5ms.png",   40.0, 5.0),     # 20% duty, dense train
    ]
    print("waveform previews:")
    from PyQt5.QtWidgets import QWidget, QVBoxLayout
    for name, freq, pw in cases:
        holder = QWidget()
        holder.setStyleSheet("background: #141428;")
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(8, 8, 8, 8)
        w = WaveformPreview()
        w.set_params(freq, pw)
        lay.addWidget(w)
        holder.adjustSize()
        holder.show(); QApplication.processEvents(); QApplication.processEvents()
        save(holder.grab(), name)
        holder.close()


# ─── stimulation window (no hardware) ────────────────────────────────────────
STIM_TARGETS = [
    (1, lambda w: w._canvas,       "inside"),
    (2, lambda w: w._wave,         "left"),
    (3, lambda w: w._f_pin,        "above"),
    (4, lambda w: w._f_freq,       "above", -22),
    (5, lambda w: w._f_pw,         "above"),
    (6, lambda w: w._f_dur,        "above", -22),
    (7, lambda w: w._start_cb,     "above"),
    (8, lambda w: w._end_cb,       "above", -22),
    (9, lambda w: w._status_lbl,   "below"),
    (10, lambda w: w._test_btn,    "below", 22),
    (11, lambda w: w._apply_btn,   "below"),
]


def shot_stim_window(profile):
    from gui_app.widgets.stimulation_window import StimulationWindow
    win = StimulationWindow(
        get_port=lambda: profile.serial_port,
        get_output_dir=lambda: str(REPO / "data"),
        get_fps=lambda: profile.frame_rate,
        get_safe_pins=lambda: profile.stim_safe_pins,
        get_trigger_pins=lambda: profile.trigger_pins)
    # A two-step chain that loops: the shape most paradigms start from. The
    # illustrated pin is the profile's first safe pin (the laser on 3dpose);
    # gui_app is shared with other rigs, so no pin is hardcoded here.
    pins = list(getattr(profile, "stim_safe_pins", None) or [])
    pin = int(pins[0]) if pins else 53
    blocks = [
        {"id": "a", "x": 40,  "y": 40, "pin": pin, "freq": 10.0, "pw": 10.0,
         "dur": 5.0, "start": True,  "end": False},
        {"id": "b", "x": 260, "y": 40, "pin": pin, "freq": 0.0, "pw": 10.0,
         "dur": 10.0, "start": False, "end": False},
    ]
    edges = [{"src": "a", "dst": "b"}, {"src": "b", "dst": "a"}]
    win._canvas.load_workflow(blocks, edges)
    win._f_pin.setText(str(pin)); win._f_freq.setText("10")
    win._f_pw.setText("10");  win._f_dur.setText("5")
    win._update_preview()
    win._status_lbl.setText("2 blocks · 1 chain · loops")
    # Shorter than the working window: with two blocks most of a full-height
    # canvas is empty, which wastes the figure. Then centre the view on the
    # blocks, since the scene origin is not where they were placed.
    win.resize(1120, 560)
    win.show(); QApplication.processEvents()
    try:
        rect = win._canvas.scene().itemsBoundingRect()
        win._canvas.centerOn(rect.center())
    except Exception as e:
        print(f"  could not centre the canvas: {e}")
    QApplication.processEvents(); QApplication.processEvents()
    print("stimulation window:")
    save(win.grab(), "stim_clean.png")
    pix, missing = draw_callouts(win, STIM_TARGETS)
    save(pix, "stim_annotated.png")
    if missing:
        print(f"  not visible, unlabelled: {missing}")
    win.close()


# ─── main window (needs cameras) ─────────────────────────────────────────────
def _cell(w, i):
    cells = getattr(w._camera_grid, "_cells", [])
    return cells[i] if i < len(cells) else None


MAIN_TARGETS = [
    (1,  lambda w: w._camera_grid,                "inside"),
    (2,  lambda w: _cell(w, 1),                   "inside"),
    (3,  lambda w: _cell(w, 1).fps_overlay,       "right"),
    (4,  lambda w: w._sidebar._profile_combo,     "left"),
    (5,  lambda w: w._sidebar._dir_button,        "left"),
    (6,  lambda w: next(iter(w._sidebar._fields.values())), "left"),
    (7,  lambda w: w._sidebar._calibrate_toggle,  "left", -15),
    (8,  lambda w: w._sidebar._record_toggle,     "left"),
    (9,  lambda w: w._sidebar._run_calib_btn,     "left", 15),
    (10, lambda w: w._sidebar._snapshot_btn,      "left"),
    (11, lambda w: w._sidebar._stim_btn,          "left"),
    (12, lambda w: w._sidebar._coverage_graph,    "left"),
    (13, lambda w: w._sidebar._brightness_slider, "left"),
    (14, lambda w: w._sidebar._contrast_slider,   "left"),
    (15, lambda w: w._sidebar._progress,          "left"),
    (16, lambda w: w._sidebar._status,            "left"),
    (17, lambda w: w.statusBar(),                 "above"),
]


def shot_main_window(app, wait_s):
    from gui_app.main_window import MainWindow
    win = MainWindow()
    win.resize(1600, 950)
    win.show()

    def teardown():
        # Always runs, even when the screenshot code raises: an exception in a
        # QTimer callback would otherwise leave the event loop running with
        # every camera and the serial port open.
        try:
            win._camera_mgr.close_all()
        except Exception as e:
            print(f"  close_all failed: {e}")
        try:
            if win._teensy is not None and win._teensy.is_open:
                win._teensy.stop_triggers(win._profile.trigger_pins)
                win._teensy.close()
        except Exception as e:
            print(f"  trigger board teardown failed: {e}")
        app.quit()

    def finish():
        try:
            print("main window:")
            save(win.grab(), "main_idle.png")          # clean, for the workflow page
            win._sidebar.show_coverage()
            # A six-node ring at minimum: with fewer cameras the illustrated
            # graph degenerates and the callouts have nothing to point at.
            win._sidebar.update_coverage(
                _detector(max(win._camera_mgr.num_cameras, 6), win._profile,
                          pair=3.5, per_cam=1.6, glow_idx=(1, 4), cells=4,
                          weak=(0, 3, 0.9)))
            win._sidebar.show_progress(3, 6, "Encoding")
            win.statusBar().showMessage(
                "Capture healthy — keeping up with the trigger (max lag 3 ms)")
            QApplication.processEvents()
            pix, missing = draw_callouts(
                win, MAIN_TARGETS, column_from=lambda w: w._sidebar)
            save(pix, "ui_annotated.png")
            if missing:
                print(f"  not visible, unlabelled: {missing}")
        finally:
            teardown()

    # Long enough for the startup firmware check plus preview frames, so the
    # panes show live video rather than black.
    QTimer.singleShot(int(wait_s * 1000), finish)
    app.exec_()


def load_profile(stem):
    """The RigProfile with this stem, or exit 1 listing the ones that exist."""
    from gui_app.session_config import RigProfile
    paths = list(RigProfile.list_profiles())
    for p in paths:
        if p.stem == stem:
            return RigProfile.load(p)
    print(f"profile '{stem}' not found; available: "
          f"{', '.join(sorted(p.stem for p in paths)) or '(none)'}",
          file=sys.stderr)
    sys.exit(1)


def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="Generate the screenshots used by the documentation.")
    ap.add_argument("what", nargs="?", default="all",
                    choices=("all", "widgets", "stim", "main"))
    ap.add_argument("--profile", default="3dpose",
                    help="rig profile stem to illustrate (default 3dpose)")
    ap.add_argument("--wait-s", type=float, default=50.0,
                    help="seconds the main window runs before its screenshot "
                         "(default 50)")
    return ap.parse_args(argv)


def main():
    args = parse_args(sys.argv[1:])
    app = QApplication(sys.argv[:1])
    profile = load_profile(args.profile)

    if args.what in ("all", "widgets"):
        shot_coverage_stages(profile)
        shot_waveforms()
    if args.what in ("all", "stim"):
        shot_stim_window(profile)
    if args.what in ("all", "main"):
        shot_main_window(app, args.wait_s)   # runs its own event loop, exits at the end
    print("done")


if __name__ == "__main__":
    main()
