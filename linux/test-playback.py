"""Real GStreamer decoding into a verification sink, never an audio device."""

from pathlib import Path
import struct
import tempfile
import unittest
import wave

from gi.repository import GLib
from playback import Gst, PlaybackError, SegmentPlayer


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='repeater-playback-test-')
        self.addCleanup(self.temporary.cleanup)
        self.session = Path(self.temporary.name)
        self.audio = {}
        for source, filename, sign in [('me', 'mic.wav', 1), ('them', 'system.wav', -1)]:
            content = b''.join(struct.pack('<h', sign * (1000 + frame % 500)) for frame in range(16000))
            self.audio[source] = content
            with wave.open(str(self.session / filename), 'wb') as output:
                output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                output.writeframes(content)
        self.events = []
        self.buffers = []
        self.loop = GLib.MainLoop()
        sink = Gst.ElementFactory.make('fakesink')
        sink.set_property('sync', False)
        sink.set_property('signal-handoffs', True)
        sink.connect('handoff', self.handoff)
        self.player = SegmentPlayer(self.changed, audio_sink=sink)
        self.addCleanup(self.player.stop)

    def handoff(self, _sink, buffer, _pad):
        self.buffers.append((buffer.pts / Gst.SECOND, buffer.duration / Gst.SECOND,
                             buffer.extract_dup(0, buffer.get_size())))

    def changed(self, key, error):
        self.events.append((key, error))
        if key is None and self.loop.is_running():
            self.loop.quit()

    def drain(self):
        timed_out = []
        def expire():
            timed_out.append(True)
            self.loop.quit()
            return False
        deadline = GLib.timeout_add(2000, expire)
        if self.player.active_key is not None:
            self.loop.run()
        if not timed_out:
            GLib.source_remove(deadline)
        self.assertFalse(timed_out, 'Bounded playback did not reach EOS')
        self.assertFalse([error for _key, error in self.events if error])

    def test_each_track_decodes_only_requested_interval_and_resets_at_eos(self):
        for source, start, end in [('me', 0.125, 0.275), ('them', 0.4, 0.55)]:
            with self.subTest(source=source):
                self.buffers.clear()
                self.events.clear()
                self.player.play(self.session, source, start, end, source)
                self.drain()
                total_bytes = 0
                for pts, duration, content in self.buffers:
                    self.assertGreaterEqual(pts, start - 1 / 16000)
                    self.assertLessEqual(pts + duration, end + 1 / 16000)
                    offset = round(pts * 16000) * 2
                    self.assertEqual(content, self.audio[source][offset:offset + len(content)])
                    total_bytes += len(content)
                self.assertEqual(total_bytes, round((end - start) * 16000) * 2)
                self.assertEqual(self.events[0], (source, None))
                self.assertEqual(self.events[-1], (None, None))
                self.assertIsNone(self.player.active_key)

    def test_stop_during_preroll_does_not_leave_background_audio(self):
        self.player.play(self.session, 'me', 0, 1, 'stopped')
        self.player.stop()
        GLib.timeout_add(20, lambda: (self.loop.quit(), False)[1])
        self.loop.run()
        self.assertEqual(self.buffers, [])
        self.assertIsNone(self.player.active_key)
        self.assertEqual(self.events[-1], (None, None))

    def test_invalid_or_missing_audio_cannot_start_full_recording_fallback(self):
        for source, start, end in [('unknown', 0, 1), ('me', -0.1, 0.2), ('them', 0, 2),
                                   ('me', float('nan'), 0.2), ('them', 0.4, 0.4)]:
            with self.subTest(source=source, start=start, end=end), self.assertRaises(PlaybackError):
                self.player.play(self.session, source, start, end, 'invalid')
        (self.session / 'mic.wav').unlink()
        with self.assertRaises(PlaybackError):
            self.player.play(self.session, 'me', 0, 0.2, 'missing')
        self.assertIsNone(self.player.active_key)
        self.assertEqual(self.buffers, [])


if __name__ == '__main__':
    unittest.main()
