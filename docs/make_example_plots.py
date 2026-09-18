"""Generate the illustrative calibration-quality plots used in the docs.

These are synthetic numbers, not a real solve, so the docs can show a clean
result and a bad one side by side. They are drawn by the SAME function the
solve uses (``1_calibrate.save_reprojection_histogram``), so the colours,
thresholds and layout are guaranteed to match what a real
``reprojection_error_histogram.png`` looks like.

    uv run docs/make_example_plots.py

Needs the full project environment (OpenCV contrib, PyYAML, matplotlib):
1_calibrate.py imports cv2 and yaml at module level, so loading it for the
plotting function needs them present. Writes into docs/images/.
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "images"


def _load_calibrate():
    """Import 1_calibrate.py, whose name is not a valid module identifier."""
    spec = importlib.util.spec_from_file_location(
        "calibrate_mod", REPO / "1_calibrate.py")
    mod = importlib.util.module_from_spec(spec)
    # The module imports cv2/yaml at top level, so this needs the project
    # environment; the plotting function is not importable on its own.
    spec.loader.exec_module(mod)
    return mod


# A good solve: every pair sub-pixel to a couple of px, all green. Six cameras
# in a ring, so opposite pairs (cam1-cam4) see the board from very different
# angles and land a little higher than neighbours.
GOOD = {
    "cam1-cam2": 0.61, "cam1-cam3": 1.12, "cam1-cam4": 1.83,
    "cam1-cam5": 1.20, "cam1-cam6": 0.58, "cam2-cam3": 0.55,
    "cam2-cam4": 1.09, "cam2-cam5": 1.77, "cam2-cam6": 1.14,
    "cam3-cam4": 0.63, "cam3-cam5": 1.16, "cam3-cam6": 1.91,
    "cam4-cam5": 0.59, "cam4-cam6": 1.08, "cam5-cam6": 0.64,
}

# A solve to redo: every pair containing cam4 is bad and the rest are fine.
# That pattern is the diagnosis -- one camera, not one pair.
BAD = {
    "cam1-cam2": 0.72, "cam1-cam3": 1.24, "cam1-cam4": 18.4,
    "cam1-cam5": 1.31, "cam1-cam6": 0.66, "cam2-cam3": 0.61,
    "cam2-cam4": 24.7, "cam2-cam5": 1.88, "cam2-cam6": 1.22,
    "cam3-cam4": 21.2, "cam3-cam5": 1.27, "cam3-cam6": 2.03,
    "cam4-cam5": 26.1, "cam4-cam6": 19.8, "cam5-cam6": 0.70,
}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cal = _load_calibrate()
    for name, data in (("reproj_example_good", GOOD),
                       ("reproj_example_bad", BAD)):
        path = OUT / f"{name}.png"
        cal.save_reprojection_histogram(path, data)
        print(f"wrote {path}")


if __name__ == "__main__":
    sys.exit(main())
