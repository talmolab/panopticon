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
