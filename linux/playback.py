"""Explicit, bounded playback of one saved Me/Them audio interval."""

import math
from pathlib import Path
from typing import Callable
import wave

import gi

gi.require_version('Gst', '1.0')
from gi.repository import Gst

Gst.init(None)


class PlaybackError(Exception):
    pass


class SegmentPlayer:
    """One main-thread-owned player; never records or changes audio defaults."""

    def __init__(self, on_change: Callable[[object | None, str | None], None], *, audio_sink=None):
        self.on_change = on_change
        self._audio_sink = audio_sink
        self._pipeline = None
        self._bus = None
        self._bus_handler = None
        self._active_key = None
        self._starting = False
        self._start = 0
        self._end = 0

    @property
    def active_key(self):
        return self._active_key

    def play(self, session: Path, source: str, start: float, end: float, key: object) -> None:
        self.stop()
        if source not in ('me', 'them'):
            raise PlaybackError('Choose the Me or Them audio track.')
        if key is None:
            raise PlaybackError('Choose a transcript entry to play.')
        try:
            valid = (type(start) in (int, float) and type(end) in (int, float)
                     and math.isfinite(start) and math.isfinite(end) and 0 <= start < end)
        except OverflowError:
            valid = False
        if not valid:
            raise PlaybackError('This transcript entry has no valid audio interval.')
        audio = session / ('mic.wav' if source == 'me' else 'system.wav')
        if not session.is_absolute() or session.resolve() != session or audio.is_symlink():
            raise PlaybackError('Choose a local recording without symlinked audio.')
        try:
            with wave.open(str(audio), 'rb') as track:
                if (track.getnchannels(), track.getsampwidth(), track.getframerate(), track.getcomptype()) != (1, 2, 16000, 'NONE'):
                    raise PlaybackError('The saved audio must be mono 16 kHz PCM WAV.')
                duration = track.getnframes() / track.getframerate()
        except (OSError, EOFError, wave.Error) as error:
            raise PlaybackError(f'Cannot open the saved {source} audio: {error}') from error
        if start >= duration or end > duration + 1 / 16000:
            raise PlaybackError('This transcript entry extends past the saved audio.')
        pipeline = Gst.ElementFactory.make('playbin')
        if pipeline is None:
            raise PlaybackError('GStreamer audio playback is unavailable. Install the GStreamer base and good plugins.')
        pipeline.set_property('uri', audio.as_uri())
        if self._audio_sink is not None:
            pipeline.set_property('audio-sink', self._audio_sink)
        self._pipeline = pipeline
        self._bus = pipeline.get_bus()
        self._bus.add_signal_watch()
        self._bus_handler = self._bus.connect('message', self._message)
        self._start = round(start * Gst.SECOND)
        self._end = round(min(end, duration) * Gst.SECOND)
        self._active_key = key
        self._starting = True
        if pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
            self.stop()
            raise PlaybackError('Cannot prepare the saved audio for playback.')
        self.on_change(key, None)

    def stop(self) -> None:
        active = self._active_key is not None
        pipeline, bus, handler = self._pipeline, self._bus, self._bus_handler
        self._pipeline = self._bus = self._bus_handler = None
        self._active_key = None
        self._starting = False
        if bus is not None:
            if handler is not None:
                bus.disconnect(handler)
            bus.remove_signal_watch()
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        if active:
            self.on_change(None, None)

    def _failed(self, message: str) -> None:
        self.stop()
        self.on_change(None, message)

    def _message(self, _bus, message) -> None:
        pipeline = self._pipeline
        if pipeline is None:
            return
        if message.type == Gst.MessageType.ERROR:
            error, detail = message.parse_error()
            self._failed(f'Cannot play the saved audio: {error.message}' + (f'\n{detail}' if detail else ''))
        elif message.type == Gst.MessageType.EOS:
            self.stop()
        elif message.type == Gst.MessageType.ASYNC_DONE and self._starting:
            self._starting = False
            # A finite non-segment seek delivers EOS after the sinks drain this
            # interval. SEGMENT_DONE can arrive before buffered audio is heard.
            if not pipeline.seek(1.0, Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                                 Gst.SeekType.SET, self._start, Gst.SeekType.SET, self._end):
                self._failed('Cannot seek to this audio chunk; no full-recording playback was started.')
                return
            if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                self._failed('Cannot start playback of this audio chunk.')
