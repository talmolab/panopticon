"""Encoder factory seam: who makes the per-camera H.264 encoder.

The capture path needs exactly two things from an encoder object, `Encode()`
and `EndEncode()` (the duck type below), and one way to obtain such an object
for a given geometry. Which library provides it is decided here, once, so a
CPU encoder can be plugged in without touching the grab loop or the router:
`set_default_factory()` at startup, or `encoder_factory=` on `SyncEncodeRouter`
and `GrabThread` for a single acquisition.

The default is NVENC (`gui_app.nvenc.create_h264_encoder`). It is imported
lazily inside the factory so importing this module never loads PyNvVideoCodec.
"""
from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable


@runtime_checkable
class EncoderProtocol(Protocol):
    """What the encoder threads call, once per frame and once at the end.

    Frames arrive as NV12 `(height * 3 // 2, width)` uint8 arrays whose Y plane
    is the gray image and whose UV plane is a constant 128. Both calls return
    Annex-B H.264 bytes, possibly empty; the caller appends them to
    `stream.h264` verbatim. Every produced stream must carry an explicit GOP of
    one IDR per second (`gopLength == fps`), because the labeler seeks by IDR.

    Any GPU or process resource the encoder holds is released when the object
    is destroyed, and additionally by `Close()` when the object has one. The
    encoder threads call `EndEncode()`, then `Close()` if present, then drop
    their last reference; nothing else references the object.
    """

    def Encode(self, nv12) -> bytes: ...

    def EndEncode(self) -> bytes: ...


#: A factory is called as `factory(width, height, quality, fps, notes)` and
#: returns an `EncoderProtocol` or raises. `notes` is a list the factory
#: appends one line to for every way the encoder differs from what the profile
#: asked for (a reduced kwarg set, a different rate-control mode); the caller
#: folds those lines into the recording's WARNINGS.txt. Callers always pass a
#: list, never None.
EncoderFactory = Callable[[int, int, int, int, list], EncoderProtocol]


def nvenc_factory(width: int, height: int, quality: int, fps: int,
                  notes: list) -> EncoderProtocol:
    """The default: an NVENC session via PyNvVideoCodec."""
    from gui_app import nvenc
    return nvenc.create_h264_encoder(width, height, quality, fps=fps,
                                     notes=notes)


_default_factory: EncoderFactory = nvenc_factory


def get_default_factory() -> EncoderFactory:
    """The factory used when a caller passes `encoder_factory=None`."""
    return _default_factory


def set_default_factory(factory: EncoderFactory | None) -> EncoderFactory:
    """Install `factory` as the default (None restores NVENC). Returns the
    previous default so a test can put it back."""
    global _default_factory
    previous = _default_factory
    _default_factory = factory if factory is not None else nvenc_factory
    return previous
