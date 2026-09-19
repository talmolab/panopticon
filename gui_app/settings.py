"""One owner for the settings Panopticon persists between launches.

RULE: nothing else constructs a ``QSettings`` or spells one of these keys.
REASON: the board-sketch key carries a safety meaning — whether the trigger
board is believed to be free of a stimulation paradigm — and a key spelled in
five places is a key that gets written under one spelling and read under
another, which here reads as "the board is clean" when nothing said so.

The store is per machine and per user, so nothing in it describes the rig or
the hardware attached to it: every value here is a HINT about what this
machine did last, never evidence about what is plugged in now.
"""
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


def app_settings() -> QSettings:
    """The application's settings store. Cheap: construct one per use."""
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
