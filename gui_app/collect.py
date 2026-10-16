"""Move the files other programs write during a recording into its folder.

The profile's ``collect_dirs`` names folders another program writes into
while a recording runs, such as an audio recorder's output folder. When the
recording finishes, every file in such a folder that changed at or after the
recording's start moves into the recording folder, under a folder of the
same name, at the same path below it. A file unchanged since before the
start belongs to something else and stays.

RULE: a file moves only once its writer has let it go, and never as a copy
that could be partial. REASON: a recorder can still hold its file open after
the triggers stop (Avisoft-RECORDER's hold time, for one), and a file moved
or copied mid-write is a truncated recording that looks complete. A rename
on one volume fails while the writer holds the file, and a move to another
volume copies to a ``.partial`` name, deletes the original (which fails while
the writer holds it, and then the copy is deleted instead) and only then
takes the final name.

Nothing here imports Qt, opens a camera or talks to the board.
"""
import errno
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from gui_app.session_config import is_within

#: Seconds a file must go unmodified before it moves. A program that opens,
#: appends and closes its file in turns has let it go between turns, and
#: this tells that pause from the end. The collection runs after the
#: finalize, a second or more after the triggers stop, so a file closed at
#: the stop is usually past it already.
SETTLE_S = 1.0
#: Seconds between attempts at the files still held or still changing.
POLL_S = 0.25
#: Windows' ERROR_NOT_SAME_DEVICE, which a rename across volumes raises.
_NOT_SAME_DEVICE = 17
#: Files one WARNINGS.txt paragraph names before it counts the rest.
NAMED_MAX = 5
#: _move()'s answer for a file its writer still holds or is still changing.
_HELD = "held"


@dataclass
class Collected:
    """What one collection did."""
    #: One entry per collected folder, for session_metadata.json: ``from``
    #: and ``to`` (folder paths), ``files`` moved (``name`` below the folder,
    #: ``bytes``) and ``left`` (names that stayed).
    folders: list = field(default_factory=list)
    #: WARNINGS.txt paragraphs, at most two per folder.
    problems: list = field(default_factory=list)


def changed_since(folder: Path, since: float) -> list:
    """Files under ``folder`` last modified at or after ``since`` (epoch
    seconds), in path order. A file that cannot be read is left out."""
    out = []
    for root, _dirs, files in os.walk(folder):
        for name in files:
            p = Path(root) / name
            try:
                if p.stat().st_mtime >= since:
                    out.append(p)
            except OSError:
                continue
    return sorted(out)


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _move(src: Path, dst: Path) -> str | None:
    """Move one file. None once it has moved, ``_HELD`` while its writer
    holds it or it changes, or the reason it cannot move."""
    if dst.exists():
        return f"{dst} already exists"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        # Atomic on one volume, and refused while the writer holds the file.
        # Windows refuses an existing destination too, so nothing is replaced.
        os.rename(src, dst)
        return None
    except PermissionError:
        return _HELD
    except OSError as e:
        if (getattr(e, "winerror", None) != _NOT_SAME_DEVICE
                and e.errno != errno.EXDEV):
            return str(e)
    return _copy_then_remove(src, dst)


def _copy_then_remove(src: Path, dst: Path) -> str | None:
    """Move ``src`` to another volume: copy, check, delete, then rename."""
    part = dst.with_name(dst.name + ".partial")
    try:
        before = src.stat()
        shutil.copy2(src, part)
        after = src.stat()
        if ((after.st_size, after.st_mtime_ns)
                != (before.st_size, before.st_mtime_ns)
                or part.stat().st_size != before.st_size):
            _remove_quietly(part)
            return _HELD
        os.remove(src)
    except PermissionError:
        _remove_quietly(part)
        return _HELD
    except OSError as e:
        _remove_quietly(part)
        return str(e)
    try:
        os.rename(part, dst)
    except OSError as e:
        return f"copied, but kept the name {part.name}: {e}"
    return None


def _named(names: list) -> str:
    shown = ", ".join(names[:NAMED_MAX])
    more = len(names) - NAMED_MAX
    return shown + (f" and {more} more" if more > 0 else "")


def collect(dirs, dest_root, since: float, wait_s: float, *,
            settle_s: float = SETTLE_S, sleep=time.sleep,
            clock=time.monotonic, now=time.time, log=print) -> Collected:
    """Move each folder's files changed since ``since`` into ``dest_root``.

    ``dirs`` is the profile's collect_dirs, ``dest_root`` the recording
    folder and ``since`` the recording's start, in epoch seconds. The files
    are found once, when the collection starts. Each is tried until it
    moves, or until ``wait_s`` seconds after the start, and one still held
    then stays where it is. Nothing raises: a folder that cannot be read,
    a file that cannot move and a folder with nothing new each become a
    paragraph of ``problems``.
    """
    out = Collected()
    dest_root = Path(dest_root)
    deadline = clock() + max(0.0, float(wait_s))
    work = []
    for raw in dirs:
        src = Path(raw)
        dest = dest_root / src.name
        entry = {"from": str(src), "to": str(dest), "files": [], "left": []}
        if not src.is_dir():
            out.problems.append(
                f"collect_dirs: {src} does not exist, so nothing was "
                f"collected from it.")
            continue
        if is_within(dest_root, src):
            out.problems.append(
                f"collect_dirs: the recording folder is inside {src}, so "
                f"nothing was collected from it. Collect from a folder of "
                f"its own.")
            continue
        files = changed_since(src, since)
        out.folders.append(entry)
        if not files:
            out.problems.append(
                f"collect_dirs: nothing in {src} changed during the "
                f"recording, so nothing was collected from it. The program "
                f"that writes there may not have been recording.")
            continue
        work.append((src, dest, entry, files, {}))

    while True:
        waiting = False
        for src, dest, entry, files, failed in work:
            still = []
            for f in files:
                try:
                    st = f.stat()
                except OSError as e:
                    failed[f] = str(e)
                    continue
                if now() - st.st_mtime < settle_s:
                    still.append(f)
                    continue
                size = st.st_size
                why = _move(f, dest / f.relative_to(src))
                if why is None:
                    name = f.relative_to(src).as_posix()
                    entry["files"].append({"name": name, "bytes": size})
                    log(f"[collect] moved {name} ({size} bytes) from {src} "
                        f"to {dest}")
                elif why == _HELD:
                    still.append(f)
                else:
                    failed[f] = why
            files[:] = still
            waiting = waiting or bool(still)
        if not waiting or clock() >= deadline:
            break
        sleep(POLL_S)

    for src, dest, entry, files, failed in work:
        held = [f.relative_to(src).as_posix() for f in files]
        entry["left"] = held + [f.relative_to(src).as_posix() for f in failed]
        if held:
            out.problems.append(
                f"collect_dirs: {len(held)} file(s) in {src} were still open "
                f"or changing after {float(wait_s):g} s and stay there: "
                f"{_named(held)}. Move them into {dest} once the program that "
                f"writes them has closed them.")
        if failed:
            why = [f"{f.relative_to(src).as_posix()} ({r})"
                   for f, r in failed.items()]
            out.problems.append(
                f"collect_dirs: {len(failed)} file(s) in {src} could not be "
                f"moved into {dest} and stay there: {_named(why)}.")
        moved = entry["files"]
        log(f"[collect] {src}: {len(moved)} file(s) moved, "
            f"{len(entry['left'])} left")
    return out
