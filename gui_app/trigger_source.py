"""What starts and stops the camera triggers: Panopticon's trigger board, or a
TTL source the operator runs.

The profile's ``trigger_source`` picks one (session_config.TRIGGER_SOURCES).
Either way every camera takes the same hardware pulse on its trigger input,
and the recording relies on the same readiness barrier: every camera is armed
before the first pulse, so block ID 1 is the same trigger on every camera.

``BoardTriggerSource`` wraps the long-lived TeensyController and changes
nothing about it. The host starts the board after the barrier, the board's
RDY ack confirms the start, and the board also runs stimulation.

``ExternalTriggerSource`` opens nothing. The host can neither start nor stop
a pulse generator or a DAQ, so the barrier holds only if the operator starts
the source after every camera is armed. The host can check that. A frame that
reaches a camera before every camera is armed means the source was already
running, and then each camera's block IDs count from the first pulse after
its own arming, so the same block ID is a different trigger on different
cameras and no gap or rate check can show it. ``close_barrier`` refuses such
a start before anything is recorded. The rest of the protocol is the
operator's: start the source when asked, stop it at the end. The grab loops
then end on their retrieve timeouts, which is the board path's stop.

Nothing here opens a port, reads a camera or imports a vendor SDK.
"""
import time

from gui_app.frame_sync import source_name
from gui_app.grab_thread import PRE_TRIGGER_GRACE_S, SOURCE_SILENT_S
from gui_app.session_config import TRIGGER_SOURCES

#: The arm check watches the armed cameras at least this long before the
#: barrier closes. A source that is already running at the profile's rate
#: delivers a frame to every camera within one period plus the transport
#: delay, so a few periods are enough; the floor covers slow profile rates
#: and a host that schedules the grab threads late.
ARM_SETTLE_MIN_S = 0.5
#: Trigger periods the arm check waits at the profile's frame rate.
ARM_SETTLE_PERIODS = 5
#: Seconds after the prompt within which the first trigger must reach a
#: camera, or the start is cancelled ("no trigger received"). Half the grab
#: loop's pre-trigger grace, which runs from the moment each camera arms: the
#: other half covers the arming itself and the arm check, so the wait ends
#: before a camera with no frame starts its stall ladder.
FIRST_TRIGGER_TIMEOUT_S = PRE_TRIGGER_GRACE_S / 2
#: Seconds every camera must at least be silent, after triggers have
#: arrived, before the source counts as stopped and the recording finishes
#: (ExternalTriggerSource.end_silence_s). Longer than the all-cameras-silent
#: threshold, so a pause of a few hundred milliseconds in the network does
#: not end a recording, and shorter than one stall window of the grab loop
#: (five seconds of timeouts), so no camera sits one out for what is the end
#: of the recording.
SOURCE_STOPPED_S = 2 * SOURCE_SILENT_S
#: Trigger periods of silence on every camera that a slow source must add
#: up to before it counts as stopped. One period is the gap between two
#: pulses, so two leave a whole period for a late one. At the usual rates
#: the fixed thresholds are longer and this changes nothing.
STOPPED_PERIODS = 2
#: Seconds the stop waits for the operator to stop the source. After that the
#: recording finishes anyway: Panopticon stops the grab threads that are
#: still receiving frames, and the warnings say so.
STOP_WAIT_S = 30.0


class TriggerSource:
    """The part of the trigger protocol that differs between sources.

    The window asks a source to start and to stop the triggers, and reads
    the flags below to decide what else the mode allows. The readiness
    barrier, the stop protocol and the block-ID checks are the same for
    every source and are not here.
    """
    #: The profile's trigger_source value this class implements.
    kind = ""
    #: Whether stimulation can run. A paradigm is compiled into the trigger
    #: board's sketch and starts with the camera triggers.
    supports_stim = False
    #: Whether the host starts the triggers and has each start confirmed.
    #: False means the operator starts and stops them.
    host_started = False

    def start_triggers(self, pins, fps, may_retry=None) -> bool:
        """Start the triggers. False means nothing may be recorded."""
        raise NotImplementedError

    def stop_triggers(self, pins) -> bool:
        """Stop the triggers. False means they may still be running."""
        raise NotImplementedError

    def describe(self) -> str:
        """The source as a dialog names it."""
        raise NotImplementedError


class BoardTriggerSource(TriggerSource):
    """Panopticon's trigger board, through the long-lived TeensyController.

    Every call is the controller's own, with the same arguments and the same
    return value: the RDY ack, the reset-and-retry and its may_retry veto,
    and the stop's ack all behave as they do without this wrapper.
    With no controller (the link was never claimed) a start and a stop both
    return False, as the window's own checks for a missing link do.
    """
    kind = "board"
    supports_stim = True
    host_started = True

    def __init__(self, teensy):
        #: The TeensyController, or None while the window holds no link.
        self.teensy = teensy

    def start_triggers(self, pins, fps, may_retry=None) -> bool:
        if self.teensy is None:
            return False
        return self.teensy.start_triggers(pins, fps, may_retry=may_retry)

    def stop_triggers(self, pins) -> bool:
        return self.teensy is not None and self.teensy.stop_triggers(pins)

    def describe(self) -> str:
        port = getattr(self.teensy, "port", None)
        name = source_name(self.kind)
        return f"{name} on {port}" if port else name


class ExternalTriggerSource(TriggerSource):
    """A TTL source the operator runs: a pulse generator, a DAQ.

    Nothing is sent anywhere. What the host does in this mode is watch the
    cameras: that no frame arrives before every camera is armed
    (close_barrier), that the first trigger arrives after the prompt
    (triggers_arrived), and that every camera has gone silent once the
    operator stops the source (source_stopped).
    """
    kind = "external"

    def start_triggers(self, pins, fps, may_retry=None) -> bool:
        """Send nothing and return True.

        The operator starts the source when prompted, and the caller then
        waits for the first frame (triggers_arrived).
        """
        return True

    def stop_triggers(self, pins) -> bool:
        """Send nothing and return True. The operator stops the source, and
        the caller waits until every camera is silent (source_stopped)."""
        return True

    def describe(self) -> str:
        return source_name(self.kind)

    # ------------------------------------------------------------- arming
    @staticmethod
    def settle_s(fps) -> float:
        """How long close_barrier watches the armed cameras for frames."""
        fps = float(fps or 0)
        periods = ARM_SETTLE_PERIODS / fps if fps > 0 else 0.0
        return max(ARM_SETTLE_MIN_S, periods)

    def close_barrier(self, camera_mgr, fps, action: str = "Record",
                      sleep=time.sleep) -> str | None:
        """Watch the armed cameras, close the barrier, and return why the
        start must be refused, or None.

        Call once every camera is armed (wait_until_ready returned and
        not_ready() is empty) and before the operator is asked to start the
        source. It waits settle_s(fps), which is long enough for a source
        that is already running to reach every camera. Then it calls
        camera_mgr.mark_board_starting(), which fixes each camera's count of
        frames retrieved before the barrier, and reads those counts back
        (frames_before_barrier). Marking first and reading second means the
        read cannot miss a frame retrieved before the mark: the counts are
        final once the mark is made, and a frame retrieved after it is past
        the barrier.

        ``action`` is the button the refusal tells the operator to press.
        """
        sleep(self.settle_s(fps))
        camera_mgr.mark_board_starting()
        counts = dict(camera_mgr.frames_before_barrier())
        return self.early_frame_refusal(counts, action)

    @staticmethod
    def early_frame_refusal(counts: dict, action: str = "Record") -> str | None:
        """The refusal for frames that arrived before the barrier, naming each
        camera that received any, or None when none did."""
        early = [(name, int(n)) for name, n in counts.items() if int(n) > 0]
        if not early:
            return None
        names = _join([f"{name} ({n} frame{'s' if n != 1 else ''})"
                       for name, n in early])
        return (f"{names} received frames before every camera was armed: the "
                f"trigger was already running while the cameras armed, so "
                f"their frame counts start on different pulses. Nothing was "
                f"recorded.\n\nStop the trigger source and press {action} "
                f"again. Start the source only when Panopticon asks for it.")

    # ------------------------------------------------------------ running
    @staticmethod
    def triggers_arrived(camera_mgr) -> bool:
        """True once any camera has retrieved a result since the barrier.

        A failed grab counts as well: the camera acquired that trigger and
        lost it in transmission, so the source is running. A manager that
        does not report failed grabs is asked for its frame counts.
        """
        fn = getattr(camera_mgr, "results_received", None)
        counts = fn() if fn is not None else camera_mgr.frame_counts
        return any(int(n) > 0 for n in counts)

    @staticmethod
    def stop_silence_s(fps) -> float:
        """Silence on every camera after which a source the operator was
        asked to stop counts as stopped: SOURCE_SILENT_S, or STOPPED_PERIODS
        trigger periods at a rate slow enough for those to be longer."""
        fps = float(fps or 0)
        periods = STOPPED_PERIODS / fps if fps > 0 else 0.0
        return max(SOURCE_SILENT_S, periods)

    @staticmethod
    def end_silence_s(fps) -> float:
        """Silence on every camera after which a running source counts as
        stopped and the recording ends by itself: SOURCE_STOPPED_S, or twice
        STOPPED_PERIODS trigger periods at a rate slow enough for those to
        be longer.

        Twice the stop's threshold, because nobody asked for this end. The
        profile's rates are whole numbers of hertz, so the longest this gets
        is 4 s at 1 Hz, still shorter than one stall window of the grab loop.
        """
        fps = float(fps or 0)
        periods = 2 * STOPPED_PERIODS / fps if fps > 0 else 0.0
        return max(SOURCE_STOPPED_S, periods)

    @staticmethod
    def source_stopped(camera_mgr, silent_s: float = SOURCE_STOPPED_S) -> bool:
        """True when every camera has been silent for more than ``silent_s``.

        Retired cameras are counted as silent, so a recording whose every
        camera was retired also reads as stopped: nothing more can be
        recorded either way.
        """
        secs = list(camera_mgr.seconds_since_frame())
        return bool(secs) and min(secs) > silent_s

    # -------------------------------------------------------------- texts
    @staticmethod
    def prompt_text(fps, seconds_left: float, what: str = "recording") -> str:
        """The prompt shown once every camera is armed."""
        return (f"Every camera is armed. Start your trigger source now, at "
                f"{fps:g} Hz.\n\nThe {what} begins with its first pulse. If no "
                f"camera receives a trigger in the next {seconds_left:.0f} s, "
                f"the {what} is cancelled and nothing is kept.")

    @staticmethod
    def no_trigger_text(fps, timeout_s: float, action: str = "Record") -> str:
        """Why a start ended with no trigger received."""
        return (f"No camera received a trigger within {timeout_s:.0f} s of the "
                f"prompt, so nothing was recorded. The cameras are back in "
                f"preview.\n\nCheck that your trigger source runs at "
                f"{fps:g} Hz and that its output reaches every camera's "
                f"trigger input, on the line and edge the camera settings "
                f"name. Then press {action} again.")

    @staticmethod
    def stop_prompt_text(seconds_left: float, what: str = "recording") -> str:
        """The prompt shown when the operator stops while triggers run."""
        return (f"Stop your trigger source now.\n\nThe {what} finishes once "
                f"every camera has stopped receiving frames. If frames are "
                f"still arriving in {seconds_left:.0f} s, Panopticon stops "
                f"the cameras itself and notes it in WARNINGS.txt.")

    @staticmethod
    def no_stimulation_text(profile_name: str = "") -> str:
        """Why the Stimulation editor is unavailable with this source."""
        which = f"The profile {profile_name}" if profile_name else "This profile"
        return (f"Stimulation runs on Panopticon's trigger board: a paradigm "
                f"is compiled into the board's sketch and starts with the "
                f"camera triggers.\n\n{which} takes its triggers from your "
                f"own source (trigger_source: external), so there is no board "
                f"to run a paradigm on. To stimulate, use a profile with "
                f"trigger_source: board.")


def _join(items: list) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def make_trigger_source(profile, teensy=None) -> TriggerSource:
    """The source a profile names: an ExternalTriggerSource for
    ``trigger_source: external``, else a BoardTriggerSource over ``teensy``.

    A value outside TRIGGER_SOURCES raises ValueError; the profile loader
    refuses one first, so only a profile built in code can reach it.
    """
    kind = getattr(profile, "trigger_source", "board") or "board"
    if kind not in TRIGGER_SOURCES:
        raise ValueError(f"trigger_source {kind!r} is not one of "
                         f"{list(TRIGGER_SOURCES)}")
    if kind == "external":
        return ExternalTriggerSource()
    return BoardTriggerSource(teensy)
