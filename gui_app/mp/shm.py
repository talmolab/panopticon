"""Shared-memory primitives for the capture worker processes.

Named segments are page-file-backed mappings (`multiprocessing.shared_memory`).
On Windows a mapping disappears when its last handle closes and `unlink()` does
nothing, so a crash leaves no segment behind for the next launch. The risk is
reading another acquisition's data within a run, which fresh names and the
epoch stamped in every header rule out: `segment_name()` puts the epoch in the
name, and every `attach` refuses a header whose epoch differs from the one the
caller was given.

Atomicity. Every int64 and float64 field is read and written through an
aligned numpy view, one element per access. An aligned 8-byte load or store is
a single instruction on x86-64 and cannot tear. `struct.pack_into` and
`unpack_from` are never used: CPython packs `'<q'` one byte at a time, low byte
first, and unpacks high byte first, so a read racing a write can return a
value lower than the one before it. The layouts also assume x86-64 store
ordering (stores become visible to other cores in program order, and loads
are not reordered with other loads), which is what lets the seqlock below and
the ledger's publish order work without explicit fences. Importing this module
raises `UnsupportedPlatform` on any other architecture.

Seqlock. A slot has one writer. The writer makes `seq` odd, writes the payload
and its metadata, then makes `seq` even. A reader reads `seq` (and tries again
if it is odd), copies the payload, and reads `seq` again; a change means the
copy may be torn and is discarded. After `READ_RETRIES` attempts the reader
returns its last good copy, so a reader never blocks on a writer and never
returns a torn frame.
"""
from __future__ import annotations

import enum
import os
import platform
import secrets
import sys
import time
from dataclasses import dataclass
from multiprocessing import shared_memory

import numpy as np


class UnsupportedPlatform(ImportError):
    """The shared-memory protocol needs x86-64, 64-bit Python, little-endian."""


_X86_64_NAMES = frozenset({"amd64", "x86_64", "x64", "em64t", "intel64"})


def check_platform(machine: str | None = None, maxsize: int | None = None,
                   byteorder: str | None = None) -> None:
    """Raise UnsupportedPlatform unless this is x86-64, 64-bit and little-endian.

    The arguments default to the running interpreter's values; tests pass
    others to exercise the refusal.
    """
    m = platform.machine() if machine is None else machine
    size = sys.maxsize if maxsize is None else maxsize
    order = sys.byteorder if byteorder is None else byteorder
    if (m or "").lower() not in _X86_64_NAMES:
        raise UnsupportedPlatform(
            f"gui_app.mp needs an x86-64 CPU; this machine reports {m!r}. The "
            f"cross-process ledger relies on aligned 8-byte loads and stores "
            f"being atomic and on stores becoming visible in program order, "
            f"which x86-64 guarantees and other architectures do not. Set "
            f"capture_processes: 0 to capture in one process.")
    if size <= 2 ** 32:
        raise UnsupportedPlatform(
            "gui_app.mp needs a 64-bit Python: its int64 fields must be single "
            "8-byte accesses. Set capture_processes: 0 to capture in one process.")
    if order != "little":
        raise UnsupportedPlatform(
            f"gui_app.mp needs a little-endian host; this one is {order}-endian.")


check_platform()


def now_ns() -> int:
    """The clock every process compares: `time.perf_counter_ns()`.

    On Windows this is QueryPerformanceCounter, which reads the same counter in
    every process, so a timestamp written by one process can be aged by
    another.
    """
    return time.perf_counter_ns()


def new_epoch() -> int:
    """A random, nonzero, positive int64 that names one acquisition or segment."""
    return secrets.randbits(62) | 1


def segment_name(kind: str, *, parent_pid: int | None = None,
                 worker: int | None = None, epoch: int | None = None) -> str:
    """`panopticon_<parent pid>_<kind>[_w<worker>][_<epoch in hex>]`.

    The parent's pid keeps two Panopticon instances apart; the epoch keeps one
    acquisition's segment apart from the next one's.
    """
    if not kind.isalpha() or not kind.islower():
        raise ValueError(f"segment kind must be lowercase letters, got {kind!r}")
    pid = os.getpid() if parent_pid is None else int(parent_pid)
    name = f"panopticon_{pid}_{kind}"
    if worker is not None:
        name += f"_w{int(worker)}"
    if epoch is not None:
        name += f"_{int(epoch):016x}"
    return name


def _round_up(n: int, k: int) -> int:
    return (int(n) + k - 1) // k * k


def view(buf, dtype, offset: int, count: int) -> np.ndarray:
    """A numpy view of `count` elements at byte `offset`, refused unless aligned.

    Alignment is checked on the resulting address, not only on the offset,
    because the base of a caller's buffer (a bytearray in tests) is not
    guaranteed to be 8-byte aligned.
    """
    dt = np.dtype(dtype)
    if offset % dt.itemsize:
        raise ValueError(f"offset {offset} is not a multiple of {dt.itemsize}")
    arr = np.frombuffer(buf, dtype=dt, count=count, offset=offset)
    addr = arr.ctypes.data
    if addr % dt.itemsize:
        # Dropped before raising: a traceback that kept the view would keep
        # the buffer exported, and the caller could not close its segment.
        del arr
        raise ValueError(
            f"{dt} view at offset {offset} is not {dt.itemsize}-byte aligned "
            f"(address {addr:#x}); an unaligned access can tear")
    return arr


def int64_view(buf, offset: int, count: int) -> np.ndarray:
    return view(buf, np.int64, offset, count)


def float64_view(buf, offset: int, count: int) -> np.ndarray:
    return view(buf, np.float64, offset, count)


def uint8_view(buf, offset: int, count: int) -> np.ndarray:
    return view(buf, np.uint8, offset, count)


class SharedSegment:
    """One named shared-memory mapping.

    Close every numpy view over `buf` before `close()`: the mapping cannot be
    unmapped while a view exports it, and `close()` says so instead of
    raising a bare BufferError.
    """

    def __init__(self, shm: shared_memory.SharedMemory, created: bool):
        self._shm = shm
        self.created = created

    @classmethod
    def create(cls, name: str, size: int) -> "SharedSegment":
        try:
            shm = shared_memory.SharedMemory(name=name, create=True,
                                             size=max(1, int(size)))
        except FileExistsError:
            raise FileExistsError(
                f"shared-memory segment {name!r} already exists. Every "
                f"acquisition uses a fresh name, so this one belongs to another "
                f"run or to a segment that was never closed.") from None
        return cls(shm, created=True)

    @classmethod
    def attach(cls, name: str, min_size: int = 0) -> "SharedSegment":
        shm = shared_memory.SharedMemory(name=name)
        if shm.size < min_size:
            size = shm.size
            shm.close()
            raise ValueError(f"shared-memory segment {name!r} holds {size} "
                             f"bytes, expected at least {min_size}")
        return cls(shm, created=False)

    @property
    def name(self) -> str:
        return self._shm.name

    @property
    def size(self) -> int:
        return self._shm.size

    @property
    def buf(self):
        return self._shm.buf

    def close(self) -> None:
        try:
            self._shm.close()
        except BufferError as e:
            raise BufferError(
                f"cannot close shared-memory segment {self.name!r}: a numpy "
                f"view over it is still alive ({e}). Close the ledger or "
                f"segment objects built on it first.") from None

    def unlink(self) -> None:
        """Remove the name (POSIX). A no-op on Windows, where the mapping
        goes away with its last handle."""
        if self.created:
            try:
                self._shm.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        self.unlink()
        return False


# -- segment headers -----------------------------------------------------------

#: Every segment except the ledger starts with this 8 x int64 header:
#: magic, version, n_cams, param, epoch, param2, spare, spare.
HEADER_BYTES = 64
_H_MAGIC, _H_VERSION, _H_NCAMS, _H_PARAM, _H_EPOCH, _H_PARAM2 = range(6)

STATUS_MAGIC = 0x50535441     # 'PSTA'
PREVIEW_MAGIC = 0x50505256    # 'PPRV'
FRAMES_MAGIC = 0x5046524D     # 'PFRM'
RECORD_MAGIC = 0x50524543     # 'PREC'
SEGMENT_VERSION = 1


class SegmentMismatch(ValueError):
    """A segment's header does not match what the caller expects to attach to."""


def _check_cam(cam, n_cams: int) -> int:
    """A camera index checked against a segment. A negative one would index
    from the end and read or write another camera's fields."""
    c = int(cam)
    if not 0 <= c < n_cams:
        raise ValueError(f"camera index {cam} is outside this segment's "
                         f"{n_cams} cameras")
    return c


def _check_epoch(epoch) -> int:
    e = int(epoch)
    if e <= 0:
        raise ValueError(f"epoch must be a positive int64, got {epoch!r}")
    return e


def _stamp(hdr: np.ndarray, magic: int, n_cams: int, param: int, epoch: int,
           param2: int = 0) -> None:
    """Fill a header. The magic goes last, so a segment still being
    initialised reads as foreign rather than as valid and empty."""
    hdr[_H_VERSION] = SEGMENT_VERSION
    hdr[_H_NCAMS] = n_cams
    hdr[_H_PARAM] = param
    hdr[_H_EPOCH] = epoch
    hdr[_H_PARAM2] = param2
    hdr[_H_MAGIC] = magic


def _validate(buf, magic: int, what: str, epoch: int) -> tuple:
    """Check a segment header and return (n_cams, param, param2).

    The header is copied into plain ints and its view dropped before any
    check, so a refused attach leaves nothing exporting the buffer and the
    caller can close its segment while handling the error.
    """
    if len(buf) < HEADER_BYTES:
        raise SegmentMismatch(f"buffer of {len(buf)} bytes is smaller than a "
                              f"{what} segment header")
    hdr = int64_view(buf, 0, HEADER_BYTES // 8)
    vals = tuple(int(x) for x in hdr)
    del hdr
    got_magic = vals[_H_MAGIC]
    if got_magic != magic:
        raise SegmentMismatch(f"not a Panopticon {what} segment "
                              f"(magic {got_magic:#x}, expected {magic:#x})")
    version = vals[_H_VERSION]
    if version != SEGMENT_VERSION:
        raise SegmentMismatch(f"{what} segment version {version}, expected "
                              f"{SEGMENT_VERSION}")
    got_epoch = vals[_H_EPOCH]
    if got_epoch != epoch:
        raise SegmentMismatch(
            f"{what} segment epoch {got_epoch:#x} does not match the expected "
            f"{epoch:#x}: it belongs to another acquisition or worker")
    return vals[_H_NCAMS], vals[_H_PARAM], vals[_H_PARAM2]


# -- seqlock slots -------------------------------------------------------------

#: Seqlock slot header, 8 x int64: seq, nbytes, h, w, frame_n, bid, stamp_ns, spare.
_SLOT_HEADER = 64
_S_SEQ, _S_NBYTES, _S_H, _S_W, _S_FRAME, _S_BID, _S_STAMP = range(7)
#: Payload regions start on a cache line.
SLOT_ALIGN = 64
READ_RETRIES = 3


@dataclass(frozen=True)
class SlotRead:
    """One consistent copy of a slot. `data` is the reader's own array."""
    data: np.ndarray
    frame_n: int
    bid: int
    seq: int
    stamp_ns: int


class SeqlockSlot:
    """One image (2-D uint8) with its frame number and block ID, single writer.

    `write` copies the image in (`np.copyto`, which also takes a strided view
    such as `img[::d, ::d]`), so the caller's array is never stored. `read`
    returns a copy, never a view into shared memory.
    """

    def __init__(self, buf, offset: int, capacity: int):
        self.capacity = int(capacity)
        self._hdr = int64_view(buf, offset, _SLOT_HEADER // 8)
        self._data = uint8_view(buf, offset + _SLOT_HEADER, self.capacity)
        self._last: SlotRead | None = None

    @staticmethod
    def size_for(capacity: int) -> int:
        return _SLOT_HEADER + _round_up(capacity, SLOT_ALIGN)

    @property
    def seq(self) -> int:
        return int(self._hdr[_S_SEQ])

    def clear(self) -> None:
        """Mark the slot never written (seq 0). A segment's creator calls this
        before any reader attaches, because a reused buffer can hold an old
        even seq that would read back as a valid old frame."""
        self._hdr[:] = 0
        self._last = None

    def write(self, img, frame_n: int = 0, bid: int = 0) -> int:
        """Publish `img`. Returns the new (even) sequence number."""
        a = np.asarray(img)
        if a.dtype != np.uint8 or a.ndim != 2:
            raise TypeError(f"a slot holds a 2-D uint8 image, got {a.dtype} "
                            f"with {a.ndim} dimensions")
        h, w = a.shape
        n = h * w
        if n > self.capacity:
            raise ValueError(f"{h}x{w} image ({n} bytes) exceeds the slot's "
                             f"{self.capacity} bytes")
        hdr = self._hdr
        s = int(hdr[_S_SEQ])
        # Odd while writing. A slot left odd by a writer that died mid-write
        # stays odd here too, so no reader ever takes that half-written copy.
        s = s + 1 if s % 2 == 0 else s + 2
        hdr[_S_SEQ] = s
        np.copyto(self._data[:n].reshape(h, w), a)
        hdr[_S_NBYTES] = n
        hdr[_S_H] = h
        hdr[_S_W] = w
        hdr[_S_FRAME] = int(frame_n)
        hdr[_S_BID] = int(bid)
        hdr[_S_STAMP] = now_ns()
        hdr[_S_SEQ] = s + 1
        return s + 1

    def try_read(self) -> SlotRead | None:
        """One attempt: a consistent copy, or None if never written or torn."""
        hdr = self._hdr
        s1 = int(hdr[_S_SEQ])
        if s1 == 0 or s1 % 2:
            return None
        n = int(hdr[_S_NBYTES])
        h = int(hdr[_S_H])
        w = int(hdr[_S_W])
        frame_n = int(hdr[_S_FRAME])
        bid = int(hdr[_S_BID])
        stamp = int(hdr[_S_STAMP])
        if n < 0 or n > self.capacity or h * w != n:
            return None
        data = self._data[:n].copy().reshape(h, w)
        if int(hdr[_S_SEQ]) != s1:
            return None
        got = SlotRead(data, frame_n, bid, s1, stamp)
        self._last = got
        return got

    def read(self, retries: int = READ_RETRIES) -> SlotRead | None:
        """A consistent copy, or this reader's last good copy after `retries`
        failed attempts (None if it never had one)."""
        for _ in range(max(1, int(retries))):
            got = self.try_read()
            if got is not None:
                return got
        return self._last


class PingPongSlot:
    """Two seqlock slots and a `latest` index.

    The writer fills the slot the reader is not pointed at, then flips
    `latest`, so a reader copying the latest frame collides with the writer
    only when two writes land inside one copy.
    """

    _HEADER = 64    # int64: latest (-1 = never written), spare

    def __init__(self, buf, offset: int, capacity: int):
        self.capacity = int(capacity)
        self._hdr = int64_view(buf, offset, self._HEADER // 8)
        step = SeqlockSlot.size_for(capacity)
        base = offset + self._HEADER
        self._slots = (SeqlockSlot(buf, base, capacity),
                       SeqlockSlot(buf, base + step, capacity))
        self._last: SlotRead | None = None

    @classmethod
    def size_for(cls, capacity: int) -> int:
        return cls._HEADER + 2 * SeqlockSlot.size_for(capacity)

    def init(self) -> None:
        for s in self._slots:
            s.clear()
        self._hdr[0] = -1

    def write(self, img, frame_n: int = 0, bid: int = 0) -> int:
        latest = int(self._hdr[0])
        idx = 0 if latest != 0 else 1
        seq = self._slots[idx].write(img, frame_n, bid)
        self._hdr[0] = idx
        return seq

    def read(self, retries: int = READ_RETRIES) -> SlotRead | None:
        for _ in range(max(1, int(retries))):
            latest = int(self._hdr[0])
            if latest not in (0, 1):
                return self._last
            got = self._slots[latest].try_read()
            if got is not None:
                self._last = got
                return got
        return self._last


class SeqlockRecord:
    """Named float64 fields (scalars and fixed-length vectors), single writer.

    `fields` maps a name to its length (1 for a scalar). Integers up to 2**53
    round-trip exactly; NaN is a value like any other, so a field can use it
    for "none".
    """

    def __init__(self, buf, offset: int, fields: dict):
        self.fields = dict(fields)
        self._index = {}
        pos = 0
        for name, length in self.fields.items():
            length = int(length)
            if length < 1:
                raise ValueError(f"field {name!r} needs a length >= 1")
            self._index[name] = (pos, length)
            pos += length
        self._n = pos
        self._seq = int64_view(buf, offset, 1)
        self._vals = float64_view(buf, offset + 8, pos)
        self._last: dict | None = None

    @staticmethod
    def size_for(fields: dict) -> int:
        return 8 + 8 * sum(int(v) for v in fields.values())

    def write(self, values: dict) -> int:
        """Update the given fields inside one seqlock window."""
        unknown = set(values) - set(self._index)
        if unknown:
            raise KeyError(f"unknown record fields: {sorted(unknown)}")
        s = int(self._seq[0])
        s = s + 1 if s % 2 == 0 else s + 2
        self._seq[0] = s
        for name, value in values.items():
            pos, length = self._index[name]
            if length == 1:
                self._vals[pos] = float(value)
            else:
                v = np.asarray(value, dtype=np.float64).ravel()
                if v.size > length:
                    raise ValueError(f"field {name!r} holds {length} values, "
                                     f"got {v.size}")
                self._vals[pos:pos + v.size] = v
                self._vals[pos + v.size:pos + length] = 0.0
        self._seq[0] = s + 1
        return s + 1

    def _decode(self, vals: np.ndarray) -> dict:
        out = {}
        for name, (pos, length) in self._index.items():
            out[name] = float(vals[pos]) if length == 1 else vals[pos:pos + length].copy()
        return out

    def read(self, retries: int = READ_RETRIES) -> dict | None:
        for _ in range(max(1, int(retries))):
            s1 = int(self._seq[0])
            if s1 == 0 or s1 % 2:
                continue
            vals = self._vals.copy()
            if int(self._seq[0]) == s1:
                self._last = self._decode(vals)
                return self._last
        return self._last


# -- worker status -------------------------------------------------------------

class WorkerState(enum.IntEnum):
    """`state` in the status segment, one value per camera's worker."""
    NONE = 0
    SPAWNED = 1
    OPEN = 2
    ARMED = 3
    RUNNING = 4
    STOPPED = 5
    FINALIZED = 6
    ABANDONED = 7
    CLOSED = 8
    FAILED = 9


#: int64 status fields, in slot order.
STATUS_INT_FIELDS = (
    "heartbeat_ns", "state", "frame_count", "timeouts", "rearms",
    "drops_local", "failed_grabs", "retrieve_loop_exited", "ready",
    "early_frame", "first_bid", "sessions_held", "pinned_cpu", "preview_seq",
    "full_seq", "snapshot_seq", "global_index", "ring_refused",
)
#: float64 status fields, in the slots after the int64 ones. NaN = unknown.
STATUS_FLOAT_FIELDS = (
    "current_fps", "delivery_lag_s", "avg_proc_ms", "avg_wait_ms", "cycle_ms",
    "temp_c",
)
STATUS_SLOTS = 32


class StatusSegment:
    """Per-camera status of one worker: 32 eight-byte slots per camera.

    Each camera's fields have one writer (its worker). A read of one field is
    atomic; `snapshot()` reads fields one by one and is not a consistent
    multi-field copy, which is enough for status display and health checks.
    """

    _FIELD = {**{f: ("i", k) for k, f in enumerate(STATUS_INT_FIELDS)},
              **{f: ("f", len(STATUS_INT_FIELDS) + k)
                 for k, f in enumerate(STATUS_FLOAT_FIELDS)}}

    def __init__(self, buf, n_cams: int, epoch: int, worker: int):
        self.n_cams = int(n_cams)
        self.epoch = epoch
        self.worker = worker
        self._hdr = int64_view(buf, 0, HEADER_BYTES // 8)
        self._i = int64_view(buf, HEADER_BYTES,
                             self.n_cams * STATUS_SLOTS).reshape(self.n_cams, STATUS_SLOTS)
        self._f = float64_view(buf, HEADER_BYTES,
                               self.n_cams * STATUS_SLOTS).reshape(self.n_cams, STATUS_SLOTS)

    @staticmethod
    def size_for(n_cams: int) -> int:
        return HEADER_BYTES + int(n_cams) * STATUS_SLOTS * 8

    @classmethod
    def create(cls, buf, n_cams: int, *, epoch: int, worker: int = 0,
               global_indices=None) -> "StatusSegment":
        epoch = _check_epoch(epoch)
        if n_cams < 1:
            raise ValueError("a status segment needs at least one camera")
        # Checked before any view exists, so a refusal leaves the buffer free.
        gidx = list(global_indices) if global_indices is not None else list(range(n_cams))
        if len(gidx) != n_cams:
            raise ValueError(f"{len(gidx)} global indices for {n_cams} cameras")
        seg = cls(buf, n_cams, epoch, worker)
        seg._hdr[:] = 0
        seg._i[:, :] = 0
        base = len(STATUS_INT_FIELDS)
        seg._f[:, base:base + len(STATUS_FLOAT_FIELDS)] = np.nan
        for cam, g in enumerate(gidx):
            seg.set(cam, "global_index", g)
        _stamp(seg._hdr, STATUS_MAGIC, n_cams, STATUS_SLOTS, epoch, worker)
        return seg

    @classmethod
    def attach(cls, buf, *, epoch: int) -> "StatusSegment":
        epoch = _check_epoch(epoch)
        n_cams, slots, worker = _validate(buf, STATUS_MAGIC, "status", epoch)
        if slots != STATUS_SLOTS:
            raise SegmentMismatch(f"status segment has {slots} slots per camera, "
                                  f"expected {STATUS_SLOTS}")
        if len(buf) < cls.size_for(n_cams):
            raise SegmentMismatch(f"status segment is {len(buf)} bytes, "
                                  f"{n_cams} cameras need {cls.size_for(n_cams)}")
        return cls(buf, n_cams, epoch, worker)

    def set(self, cam: int, field: str, value) -> None:
        cam = _check_cam(cam, self.n_cams)
        kind, k = self._FIELD[field]
        if kind == "i":
            self._i[cam, k] = int(value)
        else:
            self._f[cam, k] = float(value)

    def get(self, cam: int, field: str):
        cam = _check_cam(cam, self.n_cams)
        kind, k = self._FIELD[field]
        return int(self._i[cam, k]) if kind == "i" else float(self._f[cam, k])

    def beat(self, cam: int, t_ns: int | None = None) -> None:
        cam = _check_cam(cam, self.n_cams)
        self._i[cam, self._FIELD["heartbeat_ns"][1]] = now_ns() if t_ns is None else int(t_ns)

    def heartbeat_age_s(self, cam: int, t_ns: int | None = None) -> float | None:
        """Seconds since the camera's last heartbeat; None if it never beat."""
        cam = _check_cam(cam, self.n_cams)
        hb = int(self._i[cam, self._FIELD["heartbeat_ns"][1]])
        if hb == 0:
            return None
        return ((now_ns() if t_ns is None else int(t_ns)) - hb) / 1e9

    def snapshot(self, cam: int) -> dict:
        return {f: self.get(cam, f) for f in self._FIELD}

    def close(self) -> None:
        self._hdr = self._i = self._f = None


# -- image segments ------------------------------------------------------------

class PreviewSegment:
    """Decimated preview frames, one ping-pong slot per camera."""

    def __init__(self, buf, n_cams: int, capacity: int, epoch: int):
        self.n_cams = int(n_cams)
        self.capacity = int(capacity)
        self.epoch = epoch
        self._hdr = int64_view(buf, 0, HEADER_BYTES // 8)
        step = PingPongSlot.size_for(capacity)
        self._slots = [PingPongSlot(buf, HEADER_BYTES + c * step, capacity)
                       for c in range(self.n_cams)]

    @staticmethod
    def size_for(n_cams: int, capacity: int) -> int:
        return HEADER_BYTES + int(n_cams) * PingPongSlot.size_for(capacity)

    @classmethod
    def create(cls, buf, n_cams: int, capacity: int, *, epoch: int) -> "PreviewSegment":
        epoch = _check_epoch(epoch)
        seg = cls(buf, n_cams, capacity, epoch)
        seg._hdr[:] = 0
        for s in seg._slots:
            s.init()
        _stamp(seg._hdr, PREVIEW_MAGIC, n_cams, capacity, epoch)
        return seg

    @classmethod
    def attach(cls, buf, *, epoch: int) -> "PreviewSegment":
        epoch = _check_epoch(epoch)
        n_cams, capacity, _ = _validate(buf, PREVIEW_MAGIC, "preview", epoch)
        if len(buf) < cls.size_for(n_cams, capacity):
            raise SegmentMismatch(f"preview segment is {len(buf)} bytes, "
                                  f"expected {cls.size_for(n_cams, capacity)}")
        return cls(buf, n_cams, capacity, epoch)

    def write(self, cam: int, img, frame_n: int = 0, bid: int = 0) -> int:
        return self._slots[_check_cam(cam, self.n_cams)].write(img, frame_n, bid)

    def read(self, cam: int) -> SlotRead | None:
        return self._slots[_check_cam(cam, self.n_cams)].read()

    def close(self) -> None:
        self._hdr = None
        self._slots = []


class FrameSegment:
    """Full-resolution frames: per camera, one seqlock slot per name.

    The default names are the ones the calibration HUD and the snapshot need:
    `full` (the latest frame, written while `keep_full` is set) and `snapshot`
    (the frame a snapshot request copied).
    """

    DEFAULT_SLOTS = ("full", "snapshot")

    def __init__(self, buf, n_cams: int, capacity: int, epoch: int, names):
        self.n_cams = int(n_cams)
        self.capacity = int(capacity)
        self.epoch = epoch
        self.names = tuple(names)
        self._hdr = int64_view(buf, 0, HEADER_BYTES // 8)
        step = SeqlockSlot.size_for(capacity)
        per_cam = step * len(self.names)
        self._slots = [
            {nm: SeqlockSlot(buf, HEADER_BYTES + c * per_cam + k * step, capacity)
             for k, nm in enumerate(self.names)}
            for c in range(self.n_cams)]

    @staticmethod
    def size_for(n_cams: int, capacity: int, names=DEFAULT_SLOTS) -> int:
        return HEADER_BYTES + int(n_cams) * len(names) * SeqlockSlot.size_for(capacity)

    @classmethod
    def create(cls, buf, n_cams: int, capacity: int, *, epoch: int,
               names=DEFAULT_SLOTS) -> "FrameSegment":
        epoch = _check_epoch(epoch)
        seg = cls(buf, n_cams, capacity, epoch, names)
        seg._hdr[:] = 0
        for per_cam in seg._slots:
            for s in per_cam.values():
                s.clear()
        _stamp(seg._hdr, FRAMES_MAGIC, n_cams, capacity, epoch, len(seg.names))
        return seg

    @classmethod
    def attach(cls, buf, *, epoch: int, names=DEFAULT_SLOTS) -> "FrameSegment":
        epoch = _check_epoch(epoch)
        n_cams, capacity, n_names = _validate(buf, FRAMES_MAGIC, "frame", epoch)
        if n_names != len(names):
            raise SegmentMismatch(f"frame segment has {n_names} slots per camera, "
                                  f"expected {len(names)} ({', '.join(names)})")
        if len(buf) < cls.size_for(n_cams, capacity, names):
            raise SegmentMismatch(f"frame segment is {len(buf)} bytes, expected "
                                  f"{cls.size_for(n_cams, capacity, names)}")
        return cls(buf, n_cams, capacity, epoch, names)

    def write(self, cam: int, name: str, img, frame_n: int = 0, bid: int = 0) -> int:
        return self._slots[_check_cam(cam, self.n_cams)][name].write(img, frame_n, bid)

    def read(self, cam: int, name: str) -> SlotRead | None:
        return self._slots[_check_cam(cam, self.n_cams)][name].read()

    def seq(self, cam: int, name: str) -> int:
        return self._slots[_check_cam(cam, self.n_cams)][name].seq

    def close(self) -> None:
        self._hdr = None
        self._slots = []


class RecordSegment:
    """One `SeqlockRecord` behind a validated header (for example shared
    simulation-board state)."""

    def __init__(self, buf, fields: dict, epoch: int):
        self.epoch = epoch
        self.fields = dict(fields)
        # The record checks its fields before it makes a view, so building it
        # first leaves the buffer free when the field list is refused.
        self.record = SeqlockRecord(buf, HEADER_BYTES, self.fields)
        self._hdr = int64_view(buf, 0, HEADER_BYTES // 8)

    @staticmethod
    def size_for(fields: dict) -> int:
        return HEADER_BYTES + SeqlockRecord.size_for(fields)

    @classmethod
    def create(cls, buf, fields: dict, *, epoch: int) -> "RecordSegment":
        epoch = _check_epoch(epoch)
        seg = cls(buf, fields, epoch)
        seg._hdr[:] = 0
        seg.record._seq[0] = 0
        seg.record._vals[:] = 0.0
        _stamp(seg._hdr, RECORD_MAGIC, 0, seg.record._n, epoch)
        return seg

    @classmethod
    def attach(cls, buf, fields: dict, *, epoch: int) -> "RecordSegment":
        epoch = _check_epoch(epoch)
        _, n_values, _ = _validate(buf, RECORD_MAGIC, "record", epoch)
        want = sum(int(v) for v in fields.values())
        if n_values != want:
            raise SegmentMismatch(f"record segment holds {n_values} values, the "
                                  f"field list describes {want}")
        return cls(buf, fields, epoch)

    def close(self) -> None:
        self._hdr = None
        self.record = None
