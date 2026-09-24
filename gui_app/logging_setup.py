"""Session logging: per-line timestamps, a bounded asynchronous writer, the
log level, the session header and each acquisition's session.log.

Every line printed in the process goes through `StampedStream`, which
replaces sys.stdout and sys.stderr. A line gets its wall-clock time and the
name of the thread that printed it, and goes onto a bounded queue. One
background thread (`AsyncLogSink`) takes the lines off the queue, formats
them and writes them to the log file; the console gets its copy through a
second bounded queue and thread, so a console that is not being read never
holds up the file.

RULE: printing never waits for the disk or the console, at any log level.
REASON: every grab thread and encoder thread prints, and a thread that waits
on a slow disk or a blocked console window while it holds a camera buffer
falls behind the trigger. A print therefore only appends to the queue. When
the queue is full the line is dropped and counted, and the writer reports the
count ("N log lines dropped") once it catches up.

RULE: the log level adds cold-path detail only. `verbose()` and `debug()`
are asked before a camera is armed or after capture has stopped (open, mode
changes, the header, the stop summary), never in the grab loop or an encoder
thread. REASON: logging must cost the acquisition nothing at any level; a
level check per frame is per-frame work.

The file is opened unbuffered in binary mode, and each batch the writer
takes off the queue is one write, so no line sits in a Python buffer: what
the writer has taken is in the file before faulthandler, which writes to the
same file descriptor on a native crash, adds its traceback. Lines still on
the queue at a native crash are lost.

Times never go backwards in the file. A line stamped earlier than the line
above it (its thread was descheduled between the stamp and the queue, or a
capture worker's line crossed the pipe) is written with the time of the line
above.
"""
from __future__ import annotations

import atexit
import dataclasses
import json
import os
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

#: The values of the profile's `log_level`, quietest first.
LOG_LEVELS = ("normal", "verbose", "debug")
#: What a profile that does not set `log_level` gets.
DEFAULT_LOG_LEVEL = "verbose"
#: Lines the queue holds before a print is dropped. A burst the size of the
#: session header plus every camera's settings fits many times over; only a
#: writer that has stopped fills it.
QUEUE_LINES = 20000
#: Longest a line held back for its newline may grow before it is written
#: as a line of its own.
PARTIAL_LIMIT = 65536
#: Seconds a flush waits for the writer: at the excepthook, at shutdown and
#: before session.log is copied.
FLUSH_TIMEOUT_S = 2.0
#: Longest the shutdown waits for the console to show its last lines. A
#: console that is not being read is not waited for; the file has them.
CONSOLE_CLOSE_S = 0.5
#: The per-acquisition slice of the log, beside session_metadata.json.
SESSION_LOG_NAME = "session.log"
#: Separates the fields of a line a capture worker sends up its log pipe.
_FIELD_SEP = "\x1f"

_RANK = {name: i for i, name in enumerate(LOG_LEVELS)}
_rank = _RANK[DEFAULT_LOG_LEVEL]

#: The sink and streams `install` put in place, or None.
_sink = None
_streams: tuple = ()
_saved_streams: tuple = ()
_log_path: Path | None = None


# -- the level ------------------------------------------------------------------

def set_level(name) -> str:
    """Use log level `name` from now on; returns the level in force.

    An unknown name leaves the level as it was and prints why: the profile
    loader refuses one, so reaching here with it is a caller's mistake that
    must not stop an acquisition.
    """
    global _rank
    key = str(name or "").strip().lower()
    if key not in _RANK:
        print(f"[log] log_level {name!r} is not one of {list(LOG_LEVELS)}; "
              f"keeping {level()}", flush=True)
        return level()
    _rank = _RANK[key]
    return key


def level() -> str:
    """The log level in force."""
    return LOG_LEVELS[_rank]


def verbose() -> bool:
    """True at `verbose` and `debug`. Ask it on a cold path only."""
    return _rank >= 1


def debug() -> bool:
    """True at `debug`. Ask it on a cold path only."""
    return _rank >= 2


def transition(text: str) -> None:
    """One `[state]` line for an acquisition transition, at `verbose` and
    above. The timestamp the stream adds is the transition's time. Cold path
    only: the callers are the window and the managers, before triggers
    start or after the grab threads have stopped."""
    if _rank >= 1:
        print(f"[state] {text}", flush=True)


# -- formatting ------------------------------------------------------------------

_stamp_cache = [-1, ""]


def stamp(t: float) -> str:
    """`t` (time.time()) as local wall-clock time with milliseconds. A time
    the platform cannot convert is written as the raw number, so no line is
    lost for its stamp."""
    try:
        sec = int(t)
        if sec != _stamp_cache[0]:
            _stamp_cache[1] = datetime.fromtimestamp(sec).strftime(
                "%Y-%m-%d %H:%M:%S")
            _stamp_cache[0] = sec
        ms = int((t - sec) * 1000)
    except (OverflowError, OSError, ValueError, TypeError):
        return f"t={t!r}"
    return f"{_stamp_cache[1]}.{min(ms, 999):03d}"


def format_line(t: float, thread: str, text: str) -> str:
    """The one line format of every log: time, [thread], text."""
    return f"{stamp(t)} [{thread}] {text}"


_tls = threading.local()


def _resolve_thread_name() -> str:
    """The printing thread's name. A Qt thread appears to Python as an
    anonymous dummy thread, so its Qt object name is used instead, or the
    class name of its QThread (GrabThread, CallableWorker)."""
    t = threading.current_thread()
    if type(t).__name__ != "_DummyThread":
        return t.name
    qt = sys.modules.get("PyQt5.QtCore")
    if qt is not None:
        try:
            q = qt.QThread.currentThread()
            if q is not None:
                name = q.objectName()
                if name:
                    return str(name)
                cls = type(q).__name__
                if cls not in ("QThread", "QAdoptedThread"):
                    return cls
        except Exception:
            pass
    return t.name


def thread_name() -> str:
    """The calling thread's name, looked up once per thread.

    Thread-local, not keyed by thread id: Windows reuses the id of a thread
    that has exited, and a new grab thread would otherwise print under an
    old encoder thread's name.
    """
    name = getattr(_tls, "name", None)
    if name is None:
        name = _resolve_thread_name()
        _tls.name = name
    return name


def pack(t: float, thread: str, stream: int, text: str) -> bytes:
    """One stamped line as a capture worker sends it up its log pipe."""
    return _FIELD_SEP.join((repr(float(t)), thread, str(int(stream)),
                            text)).encode("utf-8", "replace")


def unpack(data: bytes):
    """(t, thread, stream, text) from `pack`, or None for any other line."""
    parts = data.decode("utf-8", "replace").split(_FIELD_SEP, 3)
    if len(parts) != 4:
        return None
    try:
        return float(parts[0]), parts[1], int(parts[2]), parts[3]
    except ValueError:
        return None


# -- the sink --------------------------------------------------------------------

class _Mark:
    """A position in the log file, taken by the writer when it reaches this
    item on the queue, so every line queued before it is before it."""

    __slots__ = ("event", "offset")

    def __init__(self):
        self.event = threading.Event()
        self.offset = None


class _Stop:
    __slots__ = ("event",)

    def __init__(self):
        self.event = threading.Event()


class _ConsoleWriter:
    """The console echo: a bounded queue and a thread of its own.

    RULE: the log file never waits for the console. REASON: a console window
    that is not being read (a selection held in it) blocks every write to
    it, and a writer that did both would stop the log file and session.log
    with it. When more than `capacity` lines wait here, the text is dropped
    and its lines counted, and the count goes to the console once it moves
    again; the file has every line either way.

    Only the sink's writer thread calls `put`, and only this thread writes
    the console, so each counter has one writer and needs no lock.
    """

    def __init__(self, consoles, capacity: int, name: str):
        self._q = queue.SimpleQueue()
        self._consoles = list(consoles)
        self._capacity = int(capacity)
        self._queued = 0      # lines put; the sink's writer thread only
        self._written = 0     # lines taken; this thread only
        self._dropped = 0     # the sink's writer thread only
        self._reported = 0    # this thread only
        self.name = name
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=name)
        self._thread.start()

    @property
    def dropped(self) -> int:
        """Lines not shown on the console because it was not being read."""
        return self._dropped

    def put(self, stream: int, lines: list) -> None:
        """Queue formatted lines for the console. What does not fit is
        dropped from the end and counted."""
        room = self._capacity - (self._queued - self._written)
        if room < len(lines):
            self._dropped += len(lines) - max(room, 0)
            lines = lines[:max(room, 0)]
        if lines:
            self._queued += len(lines)
            self._q.put((stream, "".join(lines), len(lines)))

    def close(self, timeout: float) -> bool:
        """Let the console show what is queued, waiting at most `timeout`:
        a console that is not being read is not waited for."""
        self._q.put(None)
        self._thread.join(max(0.0, timeout))
        return not self._thread.is_alive()

    def _run(self) -> None:
        q = self._q
        while True:
            item = q.get()
            batch = [item]
            while item is not None and len(batch) < 1024:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                batch.append(item)
            texts = ([], [])
            for it in batch:
                if it is not None:
                    texts[it[0]].append(it[1])
                    self._written += it[2]
            self._write(texts)
            # Lines dropped while that write was held up are reported as
            # soon as it returns, not with the next line, which may be a
            # long way off.
            self._write(([], []))
            if batch[-1] is None:
                return

    def _write(self, texts) -> None:
        """Write each stream's text, after the count of any lines dropped
        since the last one was shown."""
        lost = self._dropped - self._reported
        if lost > 0:
            self._reported += lost
            k = 1 if self._consoles[1] is not None else 0
            texts[k].append(format_line(
                time.time(), self.name,
                f"[log] {lost} log lines were not shown on this console, "
                f"which was not being read; the log file has every one")
                + "\n")
        for k in (0, 1):
            con = self._consoles[k]
            if con is None or not texts[k]:
                continue
            try:
                con.write("".join(texts[k]))
                con.flush()
            except Exception:
                self._consoles[k] = None


class AsyncLogSink:
    """The bounded queue and the one thread that writes it out.

    `file` is a binary file object or None; `consoles` holds the text
    streams stdout and stderr lines are echoed to (entries may be None);
    `forward`, when given, is called on the writer thread with each line's
    (t, thread, stream, text), which is how a capture worker sends its lines
    to the parent. The consoles are written by a thread of their own
    (`_ConsoleWriter`), so a console that stops being read never holds up
    the file.

    `put` never blocks and never raises: it appends to a SimpleQueue, whose
    put takes no lock a reader can hold. A full queue drops the line and adds
    one to the calling thread's own drop counter, which no other thread
    writes, so the total needs no lock either.

    A file write that fails (a full disk, a network drive gone) loses that
    batch, not the file: the console gets one line saying so, every later
    batch is written again, and once one lands the file says how many lines
    it is missing (`lines_not_written`, `file_error`). A closed file cannot
    be written again and stays lost.
    """

    def __init__(self, file=None, consoles=(None, None), forward=None,
                 capacity: int = QUEUE_LINES, name: str = "log-writer",
                 path: Path | None = None,
                 console_capacity: int = QUEUE_LINES):
        self._q = queue.SimpleQueue()
        self._capacity = int(capacity)
        self._drops: dict = {}
        self._reported = 0
        self._file = file
        consoles = list(consoles) + [None] * (2 - len(consoles))
        self._console = (_ConsoleWriter(consoles, console_capacity,
                                        f"{name}-console")
                         if any(c is not None for c in consoles) else None)
        self._forward = forward
        self._pos = 0
        self._last_t = 0.0
        self._closed = False
        #: Lines lost to failed file writes: since the start, and since the
        #: file last took a write.
        self._not_written = 0
        self._not_written_since = 0
        self._file_error = None
        self.path = Path(path) if path is not None else None
        self.name = name
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=name)
        self._thread.start()

    # -- any thread
    @property
    def file(self):
        """The binary log file, for faulthandler."""
        return self._file

    @property
    def dropped(self) -> int:
        """Lines dropped because the queue was full, since the start."""
        try:
            return sum(list(self._drops.values()))
        except RuntimeError:
            return self._reported

    @property
    def position(self) -> int:
        """Bytes the writer has written to the file so far."""
        return self._pos

    @property
    def lines_not_written(self) -> int:
        """Lines a failed file write lost, since the start."""
        return self._not_written

    @property
    def file_error(self):
        """The last file write error as text, or None."""
        return self._file_error

    @property
    def console_dropped(self) -> int:
        """Lines not shown on the console because it was not being read."""
        return self._console.dropped if self._console is not None else 0

    def put(self, t: float, thread: str, text: str, stream: int = 0) -> bool:
        """Queue one line; False when it was dropped. Never blocks."""
        if self._closed or self._q.qsize() >= self._capacity:
            ident = threading.get_ident()
            self._drops[ident] = self._drops.get(ident, 0) + 1
            return False
        self._q.put((t, thread, stream, text))
        return True

    def mark(self) -> _Mark:
        """A marker the writer fills with its file position when it gets to
        it. Never blocks, and is never dropped."""
        m = _Mark()
        self._q.put(m)
        return m

    def flush(self, timeout: float = FLUSH_TIMEOUT_S):
        """Wait up to `timeout` for every line queued before this call to be
        written. Returns the marker (its offset set) or None on a timeout.
        For shutdown, the excepthook and session.log; never call it on a
        capture thread."""
        if threading.current_thread() is self._thread:
            return None
        m = self.mark()
        return m if m.event.wait(max(0.0, timeout)) else None

    def close(self, timeout: float = FLUSH_TIMEOUT_S) -> bool:
        """Write what is queued, then stop the writer. True when it stopped
        within `timeout`. The console gets what is left of `timeout`, at
        most CONSOLE_CLOSE_S."""
        if self._closed:
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        stop = _Stop()
        self._q.put(stop)
        self._closed = True
        done = stop.event.wait(max(0.0, timeout))
        if done and self._console is not None:
            self._console.close(min(CONSOLE_CLOSE_S,
                                    deadline - time.monotonic()))
        return done

    # -- the writer thread
    def _run(self) -> None:
        """RULE: the writer never exits before a stop, whatever a line holds.
        REASON: with it gone every later print is dropped and no flush
        returns, so a batch that fails is reported as one line and the loop
        goes on; its markers are still released."""
        q = self._q
        while True:
            item = q.get()
            batch = [item]
            while len(batch) < 1024:
                try:
                    batch.append(q.get_nowait())
                except queue.Empty:
                    break
            try:
                if self._write_batch(batch):
                    return
            except Exception as e:
                for it in batch:
                    if isinstance(it, (_Mark, _Stop)) and not it.event.is_set():
                        if isinstance(it, _Mark):
                            it.offset = self._pos
                        it.event.set()
                try:
                    self._emit([(time.time(), self.name, 1,
                                 f"[log] the log writer lost a batch of "
                                 f"{len(batch)} lines: {type(e).__name__}: "
                                 f"{e}")])
                except Exception:
                    pass
                if any(isinstance(it, _Stop) for it in batch):
                    return

    def _write_batch(self, batch) -> bool:
        """Write one batch, releasing its markers in order. True when the
        batch held the stop."""
        lines: list = []
        for it in batch:
            if type(it) is tuple:
                lines.append(it)
                continue
            self._emit(lines)
            lines = []
            if isinstance(it, _Mark):
                it.offset = self._pos
                it.event.set()
            elif isinstance(it, _Stop):
                self._report_drops()
                it.event.set()
                return True
        self._emit(lines)
        self._report_drops()
        return False

    def _report_drops(self) -> None:
        total = self.dropped
        if total > self._reported:
            n = total - self._reported
            self._reported = total
            self._emit([(time.time(), self.name, 1,
                         f"[log] {n} log lines dropped: the log writer fell "
                         f"behind (a slow disk or a console window that is "
                         f"not being read); no thread waited for it")])

    def _emit(self, items) -> None:
        """Write one run of lines: the file, then the forward, then the
        console queue.

        RULE: the file is written before the forward is called. REASON: in
        a capture worker the forward is the log pipe to the parent, and a
        parent that is not reading it would otherwise hold the worker's own
        log file too, and the lines would be dropped from both.
        """
        if not items:
            return
        ordered = []
        forwarded = []
        by_stream = ([], [])
        now = time.time()
        first_t = None
        for t, thread, stream, text in items:
            # A time that is not a number, or is ahead of the clock (a
            # corrupt forwarded line), is replaced by the writer's own, so
            # it cannot hold every later line at its value.
            if t is None or not (t <= now + 60.0):
                t = now
            if t < self._last_t:
                t = self._last_t
            self._last_t = t
            if first_t is None:
                first_t = t
            line = format_line(t, thread, text) + "\n"
            ordered.append(line)
            by_stream[1 if stream else 0].append(line)
            forwarded.append((t, thread, stream, text))
        if self._file is not None:
            self._write_file(ordered, first_t)
        if self._forward is not None:
            for t, thread, stream, text in forwarded:
                try:
                    self._forward(t, thread, stream, text)
                except Exception:
                    self._forward = None
                    break
        if self._console is not None:
            for k in (0, 1):
                if by_stream[k]:
                    self._console.put(k, by_stream[k])

    def _write_file(self, ordered: list, first_t: float) -> None:
        """Write one batch's lines. After a failed write, the first batch
        that lands starts with a line saying how many lines the file is
        missing."""
        text = "".join(ordered)
        if self._not_written_since:
            where = ("the console showed them" if self._console is not None
                     else "no other copy was kept")
            text = format_line(
                first_t, self.name,
                f"[log] {self._not_written_since} log lines before this one "
                f"could not be written to this file ({self._file_error}); "
                f"{where}") + "\n" + text
        raw = memoryview(text.encode("utf-8", "replace"))
        try:
            # An unbuffered write may take part of the bytes.
            while raw:
                n = self._file.write(raw)
                if not n:
                    break
                self._pos += n
                raw = raw[n:]
        except (OSError, ValueError) as e:
            first = not self._not_written_since
            self._not_written += len(ordered)
            self._not_written_since += len(ordered)
            self._file_error = f"{type(e).__name__}: {e}"
            if isinstance(e, ValueError):
                # A closed file takes no later write.
                self._file = None
            if first and self._console is not None:
                self._console.put(1, [format_line(
                    time.time(), self.name,
                    f"[log] the log file could not be written "
                    f"({self._file_error}); each later batch is tried "
                    f"again, and the lines lost are counted") + "\n"])
            return
        self._not_written_since = 0


# -- the stream ------------------------------------------------------------------

class StampedStream:
    """sys.stdout or sys.stderr: each complete line goes to `sink` with its
    time and thread.

    Line-start aware per thread: a print is several writes (the text, then
    the newline), and two threads printing at once would otherwise splice
    their lines. Each thread keeps its own unfinished line, so the time is
    taken once, when the line's newline arrives, and no lock is shared.
    `flush()` returns at once: the writer thread writes each batch in one
    call, and a print(..., flush=True) must not wait for the disk.
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, sink: AsyncLogSink, stream: int = 0):
        self._sink = sink
        self._stream = int(stream)
        self._key = f"partial{id(self)}"
        #: id -> [thread, parts, length] of every thread holding an
        #: unfinished line, so shutdown can write them. Only the owning
        #: thread adds or removes its own entry.
        self._pending: dict = {}

    def _entry(self):
        entry = getattr(_tls, self._key, None)
        if entry is None:
            entry = [thread_name(), [], 0]
            setattr(_tls, self._key, entry)
        return entry

    def write(self, s) -> int:
        if not isinstance(s, str):
            s = str(s)
        if not s:
            return 0
        entry = self._entry()
        parts = entry[1]
        if "\n" not in s:
            if not parts:
                self._pending[id(entry)] = entry
            parts.append(s)
            entry[2] += len(s)
            if entry[2] > PARTIAL_LIMIT:
                self._finish(entry)
            return len(s)
        pieces = s.split("\n")
        head = pieces[0]
        if parts:
            head = "".join(parts) + head
            parts.clear()
            entry[2] = 0
            self._pending.pop(id(entry), None)
        now = time.time()
        name, sink, stream = entry[0], self._sink, self._stream
        sink.put(now, name, head, stream)
        for mid in pieces[1:-1]:
            sink.put(now, name, mid, stream)
        tail = pieces[-1]
        if tail:
            parts.append(tail)
            entry[2] = len(tail)
            self._pending[id(entry)] = entry
        return len(s)

    def _finish(self, entry) -> None:
        text = "".join(entry[1])
        entry[1].clear()
        entry[2] = 0
        self._pending.pop(id(entry), None)
        self._sink.put(time.time(), entry[0], text, self._stream)

    def finish_pending(self) -> None:
        """Queue every thread's unfinished line as it stands (shutdown)."""
        for entry in list(self._pending.values()):
            if entry[1]:
                self._finish(entry)

    def finish_own(self) -> None:
        """Queue the calling thread's unfinished line as it stands.

        RULE: a flush while other threads run finishes only the caller's
        line. REASON: another thread's print may have written its text and
        not yet its newline; finishing it splits that line in two, and a
        fragment it appends between the two steps is lost.
        """
        entry = getattr(_tls, self._key, None)
        if entry is not None and entry[1]:
            self._finish(entry)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("the Panopticon log stream has no file descriptor")


# -- the process's log -----------------------------------------------------------

def install(log_path, console: bool = True) -> Path | None:
    """Replace sys.stdout and sys.stderr with stamped streams writing to
    `log_path`, and to the original console streams when `console` (under
    pythonw there are none). Returns the path, or None when the file could
    not be opened, in which case the streams are left as they were.

    Also enables faulthandler on the log file and registers the shutdown
    flush with atexit.
    """
    global _sink, _streams, _saved_streams, _log_path
    path = Path(log_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "ab", buffering=0)
    except OSError:
        return None
    consoles = ((sys.__stdout__, sys.__stderr__) if console
                else (None, None))
    sink = AsyncLogSink(file=f, consoles=consoles, path=path)
    _saved_streams = (sys.stdout, sys.stderr)
    _sink = sink
    _streams = (StampedStream(sink, 0), StampedStream(sink, 1))
    sys.stdout, sys.stderr = _streams
    _log_path = path
    try:
        # Every thread's Python stack goes into the log on a native crash
        # (an access violation, Qt's fail-fast), which no excepthook sees.
        import faulthandler
        faulthandler.enable(file=f, all_threads=True)
    except Exception:
        pass
    atexit.register(shutdown)
    return path


def installed() -> bool:
    """True once `install` has put the stamped streams in place."""
    return _sink is not None


def log_path() -> Path | None:
    """The file this process logs to, or None."""
    return _log_path


def dropped_lines() -> int:
    """Lines dropped because the writer fell behind; 0 when not installed."""
    return _sink.dropped if _sink is not None else 0


def log_status() -> dict:
    """The log's losses for session_metadata.json: lines dropped from the
    queue, lines a failed file write lost and the last such error, and lines
    the console did not show. Zeros and None when not installed."""
    sink = _sink
    if sink is None:
        return {"lines_dropped": 0, "lines_not_written": 0,
                "file_error": None, "console_lines_dropped": 0}
    return {"lines_dropped": sink.dropped,
            "lines_not_written": sink.lines_not_written,
            "file_error": sink.file_error,
            "console_lines_dropped": sink.console_dropped}


def flush(timeout: float = FLUSH_TIMEOUT_S) -> bool:
    """Wait up to `timeout` for everything printed so far to reach the file.
    True when it did, or when nothing is installed. Cold path only: the
    excepthook, session.log. The calling thread's unfinished line is
    written as it stands; other threads' wait for their newline (shutdown
    writes every thread's)."""
    if _sink is None:
        return True
    for s in _streams:
        s.finish_own()
    return _sink.flush(timeout) is not None


def shutdown(timeout: float = FLUSH_TIMEOUT_S) -> None:
    """Write everything queued, stop the writer and put the original
    streams back. Safe to call more than once."""
    global _sink, _streams
    sink, streams = _sink, _streams
    if sink is None:
        return
    for s in streams:
        s.finish_pending()
    sink.close(timeout)
    if streams and sys.stdout is streams[0]:
        sys.stdout = _saved_streams[0]
    if len(streams) > 1 and sys.stderr is streams[1]:
        sys.stderr = _saved_streams[1]
    _sink, _streams = None, ()


def forward(t: float, thread: str, text: str, stream: int = 0) -> None:
    """Log a line stamped elsewhere (a capture worker's) with its own time
    and thread. Without an installed sink it is written, formatted, to the
    current sys.stdout."""
    if _sink is not None:
        _sink.put(t, thread, text, stream)
        return
    try:
        sys.stdout.write(format_line(t, thread, text) + "\n")
    except Exception:
        pass


def forward_packed(data: bytes, prefix: str = "") -> None:
    """`forward` for one line from a capture worker's log pipe. `prefix`
    (the worker, as "w0") goes before the worker's thread name. A line not
    in `pack` form is logged under the prefix with the time it arrived."""
    fields = unpack(data)
    if fields is None:
        forward(time.time(), prefix or thread_name(),
                data.decode("utf-8", "replace"))
        return
    t, thread, stream, text = fields
    forward(t, f"{prefix}/{thread}" if prefix else thread, text, stream)


# -- session.log -----------------------------------------------------------------

def mark():
    """The current end of the log, for `write_session_log`, or None when
    logging is not installed. Never blocks."""
    return _sink.mark() if _sink is not None else None


#: Tries at putting a new session.log in place of the last one, and the
#: pause between tries.
REPLACE_TRIES = 20
REPLACE_PAUSE_S = 0.05


def _replace(tmp: Path, dest: Path) -> None:
    """os.replace, tried again for up to REPLACE_TRIES * REPLACE_PAUSE_S.

    RULE: a refused replace is tried again before the copy is given up.
    REASON: on Windows a file another program has open cannot be replaced
    (a viewer showing session.log, a virus scanner reading the copy just
    written), and without the retry the session folder keeps the earlier
    copy, the one without the encode. Runs after the finalize only.
    """
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(REPLACE_PAUSE_S)


def write_session_log(start, dest, note: str = "",
                      timeout: float = FLUSH_TIMEOUT_S) -> Path | None:
    """Copy the log from `start` (a `mark()`) to now into `dest`.

    Waits up to `timeout` for the writer to catch up. A writer that has not
    caught up by then is not waited for: what it has written is copied, and
    a last line says where the rest is. Cold path only (after finalize).
    Returns `dest`, or None when nothing was written.
    """
    sink = _sink
    if sink is None or start is None or sink.path is None:
        return None
    end = sink.flush(timeout)
    begin = start.offset if start.offset is not None else 0
    stop = end.offset if end is not None else sink.position
    try:
        with open(sink.path, "rb") as f:
            f.seek(begin)
            data = f.read(max(0, stop - begin))
    except OSError as e:
        print(f"[log] could not read {sink.path} for {dest}: {e}", flush=True)
        return None
    head = (f"# Panopticon session log: the lines of {sink.path} from this "
            f"acquisition's start to {note or 'its finalize'}.\n")
    tail = ""
    if end is None:
        tail = (f"# The log writer had not caught up after {timeout:g} s; "
                f"the later lines are in {sink.path}.\n")
    dest = Path(dest)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(head.encode("utf-8"))
            f.write(data)
            f.write(tail.encode("utf-8"))
        _replace(tmp, dest)
    except OSError as e:
        print(f"[log] could not write {dest}: {e}", flush=True)
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    return dest


# -- the session header ----------------------------------------------------------

def _value_text(value) -> str:
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, default=str)
        except (TypeError, ValueError):
            return repr(value)
    return repr(value) if isinstance(value, str) else str(value)


def profile_lines(profile) -> list:
    """Every field of `profile`, defaults included, as '  name: value'."""
    try:
        fields = dataclasses.asdict(profile)
    except TypeError:
        fields = dict(vars(profile))
    return [f"  {k}: {_value_text(v)}" for k, v in fields.items()]


#: Per-camera facts the header names, in order, and what it says when the
#: backend did not report one.
CAMERA_FACTS = (("serial", "unavailable"), ("model", "unavailable"),
                ("firmware", "unavailable"), ("interface", "unavailable"),
                ("link_speed", "not reported"), ("backend", "unavailable"))


def camera_lines(cameras) -> list:
    """One line per camera from CameraManager.camera_info."""
    out = []
    for i, info in enumerate(cameras or []):
        info = dict(info or {})
        parts = [f"{key} {info.pop(key, None) or missing}"
                 for key, missing in CAMERA_FACTS]
        parts += [f"{k} {v}" for k, v in info.items()]
        out.append(f"  cam{i + 1}: " + ", ".join(parts))
    return out or ["  none open"]


def header_lines(title: str, facts, profile=None, cameras=None) -> list:
    """The session header, every line starting with [header]: `facts`
    ((label, value) pairs, hardware_check.environment_facts), the log's own
    state, every field of the resolved profile and the open cameras."""
    lines = [f"===== Panopticon session header: {title} ====="]
    lines += [f"{label}: {value}" for label, value in facts]
    status = log_status()
    lost = ""
    if status["lines_not_written"]:
        lost = (f", {status['lines_not_written']} lines not written to the "
                f"file ({status['file_error']})")
    lines.append(f"log: level {level()}, file {_log_path or 'not set up'}, "
                 f"{status['lines_dropped']} lines dropped so far{lost}")
    if profile is None:
        lines.append("profile: none")
    else:
        lines.append(f"profile {getattr(profile, 'name', '?')!r}, every "
                     f"field with its default filled in:")
        lines += profile_lines(profile)
    lines.append(f"cameras: {len(cameras or [])} open")
    lines += camera_lines(cameras)
    lines.append("===== end of session header =====")
    return [f"[header] {line}" for line in lines]


def print_header(title: str, facts, profile=None, cameras=None) -> None:
    """Print `header_lines`, one print per line. Every level prints it."""
    for line in header_lines(title, facts, profile, cameras):
        print(line, flush=True)
