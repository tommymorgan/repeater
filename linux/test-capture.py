"""Run with python3 linux/test-capture.py; no physical audio devices are used."""

import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import wave

import capture

DEVICES = {'microphones': [{'name': 'test-mic', 'default': True}],
           'outputs': [{'name': 'test-sink', 'default': True}]}


class CaptureTests(unittest.TestCase):
    def test_startup_silence_and_gap_are_written_into_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'mic.wav'
            metadata = {'frames': 0}
            track = capture._Track(path, metadata)
            track.append(b'\x01\x00' * 160, 0.5)
            self.assertEqual(metadata['frames'], 8000)
            track.append(b'\x02\x00' * 160, 0.53)
            self.assertEqual(metadata['frames'], 8160)
            track.append(b'\x03\x00' * 160, 2.0)
            track.close()
            with wave.open(str(path), 'rb') as audio:
                self.assertEqual(audio.getnframes(), 32000)
                self.assertEqual(audio.readframes(7840), bytes(7840 * 2))
                self.assertEqual(audio.readframes(160), b'\x01\x00' * 160)
                self.assertEqual(audio.readframes(160), b'\x02\x00' * 160)
                self.assertEqual(audio.readframes(23680), bytes(23680 * 2))
                self.assertEqual(audio.readframes(160), b'\x03\x00' * 160)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_partial_sample_is_not_lost_between_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'mic.wav'
            track = capture._Track(path, {'frames': 0})
            track.append(b'\x34', 0)
            track.append(b'\x12', 0)
            track.close()
            with wave.open(str(path), 'rb') as audio:
                self.assertEqual(audio.readframes(1), b'\x34\x12')

    def test_jitter_and_one_second_boundary_do_not_insert_silence(self):
        self.assertEqual(capture.silence_owed(2, 15900, 100), 0)
        self.assertEqual(capture.silence_owed(2.5, 15900, 100), 24000)
        self.assertEqual(capture.silence_owed(0, 100, 100), 0)

    def test_unknown_device_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(capture, 'list_devices', return_value=DEVICES):
            output = Path(directory) / 'meeting'
            with self.assertRaises(capture.CaptureError):
                capture.record_session(output, title='Test', microphone='missing', system=None, duration=1)
            self.assertFalse(output.exists())

    def test_existing_directory_and_symlink_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            sentinel = parent / 'sentinel'
            sentinel.write_text('keep')
            link = parent / 'link'
            link.symlink_to(parent / 'nonexistent')
            for output in (parent, link):
                with self.assertRaises(capture.CaptureError):
                    capture.record_session(output, title='Test', microphone=None, system=None, duration=1)
            self.assertEqual(sentinel.read_text(), 'keep')
            self.assertTrue(link.is_symlink())

    def test_invalid_duration_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'meeting'
            for duration in (0, -1, float('inf'), float('nan')):
                with self.assertRaises(capture.CaptureError):
                    capture.record_session(output, title='Test', microphone=None, system=None, duration=duration)
                self.assertFalse(output.exists())

    def test_launch_failure_preserves_private_failed_manifest_and_wav(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(capture, 'list_devices', return_value=DEVICES), patch.object(capture.subprocess, 'Popen', side_effect=OSError('backend unavailable')):
            output = Path(directory) / 'meeting'
            with self.assertRaises(capture.CaptureError):
                capture.record_session(output, title='Test', microphone=None, system=None, duration=1)
            manifest = json.loads((output / 'session.json').read_text())
            self.assertEqual(manifest['status'], 'failed')
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            self.assertEqual((output / 'session.json').stat().st_mode & 0o777, 0o600)
            for filename in ('mic.wav', 'system.wav'):
                with wave.open(str(output / filename), 'rb') as audio:
                    self.assertEqual(audio.getnframes(), 0)
                    self.assertEqual(audio.getframerate(), 16000)


class LinkTests(unittest.TestCase):
    def setUp(self):
        nodes = [(1, 'selected-mic'), (2, 'selected-output'),
                 (3, 'echo-filter'), (4, 'level-filter'),
                 (5, 'processed-source'), (6, 'capture-me'),
                 (7, 'other-mic'), (8, 'capture-them')]
        self.graph = [{'id': identifier, 'type': 'PipeWire:Interface:Node',
                       'info': {'props': {'node.name': name}}}
                      for identifier, name in nodes]
        # The playback reference enters the echo filter alongside the microphone.
        self.graph += [{'type': 'PipeWire:Interface:Link',
                        'info': {'output-node-id': source, 'input-node-id': target,
                                 'state': 'active'}}
                       for source, target in [(1, 3), (2, 3), (3, 4), (4, 5), (5, 6)]]
        self.track = SimpleNamespace(node_name='capture-me',
                                     metadata={'device': 'selected-mic', 'source': 'me'})

    def test_selected_microphone_reaches_capture_through_processing_chain(self):
        self.assertTrue(capture._linked(self.graph, self.track))

    def test_other_microphone_and_broken_processing_link_are_rejected(self):
        self.track.metadata['device'] = 'other-mic'
        self.assertFalse(capture._linked(self.graph, self.track))
        self.track.metadata['device'] = 'selected-mic'
        self.graph[-2]['info']['state'] = 'error'
        self.assertFalse(capture._linked(self.graph, self.track))

    def test_playback_reference_is_not_a_system_monitor_capture(self):
        self.track.metadata = {'device': 'selected-output', 'source': 'them'}
        self.assertFalse(capture._linked(self.graph, self.track))
        self.track.node_name = 'capture-them'
        self.graph.append({'type': 'PipeWire:Interface:Link',
                           'info': {'output-node-id': 2, 'input-node-id': 8,
                                    'state': 'paused'}})
        self.assertTrue(capture._linked(self.graph, self.track))

    def test_dangling_nodes_and_unrelated_cycles_do_not_count_as_a_route(self):
        self.graph = [node for node in self.graph if node.get('id') != 4]
        self.assertFalse(capture._linked(self.graph, self.track))
        self.graph += [{'type': 'PipeWire:Interface:Link',
                        'info': {'output-node-id': source, 'input-node-id': target,
                                 'state': 'active'}}
                       for source, target in [(1, 3), (3, 1)]]
        self.assertFalse(capture._linked(self.graph, self.track))

if __name__ == '__main__':
    unittest.main()
