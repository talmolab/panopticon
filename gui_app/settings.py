"""One owner for the settings Panopticon persists between launches.

RULE: the board-sketch key is spelled and reached ONLY here. REASON: it
carries a safety meaning — whether the trigger board is believed to be free of
a stimulation paradigm — and a key spelled in five places is a key that gets
written under one spelling and read under another, which here reads as "the
board is clean" when nothing said so.

The store is per machine and per user, so nothing in it describes the rig or
the hardware attached to it: every value here is a HINT about what this
machine did last, never evidence about what is plugged in now.
"""
import os

from PyQt5.QtCore import QSettings

#: Organisation and application the settings live under. Changing either
#: orphans every stored value, including the board-sketch hint.
ORG = "Salk"
APP = "Panopticon"

#: SHA of the sketch this machine last flashed onto whatever was on the serial
#: port. A hint only: a board flashed from the Arduino IDE, swapped for
#: another, or shared with a second rig makes it wrong, and the board's own
#: RDY identity is the authority.
KEY_BOARD_SKETCH = "board_sketch_sha"

#: Name of the rig profile last selected on this machine. The profile list is
#: shared between rigs, so alphabetical order picks the wrong one.
KEY_PROFILE = "profile_name"

#: Width in pixels the operator last dragged the sidebar to.
KEY_SIDEBAR_WIDTH = "sidebar_width"

# The window's state at the last close, restored at the next launch so the
# operator finds it as they left it. Every one is a convenience: a missing or
# unreadable value falls back to the default the widget would have had.

#: The main window's position and size (QMainWindow.saveGeometry).
KEY_WINDOW_GEOMETRY = "window_geometry"
#: The preview's brightness and contrast sliders.
KEY_BRIGHTNESS = "display_brightness"
KEY_CONTRAST = "display_contrast"
#: One key per session field, KEY_FIELD_PREFIX + the field's name. The date is
#: never stored: it follows the calendar unless typed.
KEY_FIELD_PREFIX = "session_field/"
#: An output directory chosen by hand, per profile: KEY_OUTPUT_DIR_PREFIX + the
#: profile's name.
KEY_OUTPUT_DIR_PREFIX = "output_dir/"
#: The Stimulation editor's entry fields, KEY_STIM_FIELD_PREFIX + pin, freq,
#: pw or dur. Only what the operator typed; nothing reaches the board from
#: here, because the sketch changes only at Apply.
KEY_STIM_FIELD_PREFIX = "stim_field/"
#: Profile files added from outside profiles/ with the dropdown's "Add a
#: profile from a file" entry, as absolute paths.
KEY_EXTRA_PROFILES = "extra_profiles"

#: Set this to an absolute .ini path to send every value somewhere else.
#:
#: RULE: a test that builds a window, selects a profile or records a flash sets
#: this first. REASON: without it those writes land in the operator's real
#: store, which decides which rig the next launch comes up on and what firmware
#: the board is believed to carry. Redirecting with
#: ``QSettings.setDefaultFormat`` plus ``setPath`` does NOT work for the
#: two-argument constructor below: it keeps the native format and the registry
#: path, so the isolation reads as working while every write still escapes.
#: This override is explicit because a silent one already cost a rig its
#: remembered profile.
ENV_SETTINGS_FILE = "PANOPTICON_SETTINGS_FILE"


def app_settings() -> QSettings:
    """The application's settings store. Cheap: construct one per use."""
    override = os.environ.get(ENV_SETTINGS_FILE)
    if override:
        return QSettings(override, QSettings.IniFormat)
    return QSettings(ORG, APP)


def sidebar_width(default: int) -> int:
    """The sidebar width this machine last used, or ``default``."""
    try:
        return int(app_settings().value(KEY_SIDEBAR_WIDTH, default, type=int))
    except (TypeError, ValueError):
        return default


def set_sidebar_width(width: int) -> None:
    app_settings().setValue(KEY_SIDEBAR_WIDTH, int(width))


def has(key: str) -> bool:
    """Whether a value is stored under ``key`` (an empty string counts)."""
    return app_settings().contains(key)


def get_text(key: str, default: str = "") -> str:
    """A stored string, or ``default`` when there is none."""
    try:
        v = app_settings().value(key, default, type=str)
    except (TypeError, ValueError):
        return default
    return default if v is None else str(v)


def get_int(key: str, default: int) -> int:
    """A stored integer, or ``default`` when there is none or it is not one."""
    try:
        return int(app_settings().value(key, default, type=int))
    except (TypeError, ValueError):
        return default


def get_bytes(key: str):
    """A stored byte string (a saved geometry), or None."""
    v = app_settings().value(key, None)
    return v if v else None


def set_value(key: str, value) -> None:
    app_settings().setValue(key, value)


def extra_profiles() -> list:
    """The profile files added from outside profiles/, as path strings.

    The registry hands back one string for a one-item list and None for an
    empty one, so both are normalised to a list here."""
    v = app_settings().value(KEY_EXTRA_PROFILES, [])
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v else []
    return [str(p) for p in v if p]


def set_extra_profiles(paths: list) -> None:
    app_settings().setValue(KEY_EXTRA_PROFILES, [str(p) for p in paths])


def board_sketch_hint() -> str:
    """The sketch SHA this machine last flashed, or "" if it has no record."""
    return app_settings().value(KEY_BOARD_SKETCH, "", type=str)


def set_board_sketch_hint(sha: str):
    """Record what was just flashed, or forget it entirely with "".

    Forgetting is the correct answer whenever the board's contents become
    unknown — a failed flash, a different serial port — because an unknown
    board must be flashed rather than trusted.
    """
    app_settings().setValue(KEY_BOARD_SKETCH, sha or "")
