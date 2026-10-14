"""Coupled decoder alignment boundaries; no model weights or inference dependencies."""

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('nemotron_bridge', Path(__file__).with_name('nemotron-transcribe.py'))
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class NemotronAlignmentTests(unittest.TestCase):
  def test_unicode_and_attached_punctuation_preserve_exact_spans(self):
    tokens = [' Hé', 'llo', ' café', '!']
    words = bridge.words_from_tokens('Héllo café!', [0, 1, 2, 3],
      [(1, 1.08), (1.2, 1.28), (2, 2.08), (2.1, 2.18)],
      lambda ids: ''.join(tokens[i] for i in ids), 5)
    self.assertEqual([word['text'] for word in words], ['Héllo', 'café!'])
    self.assertEqual((words[1]['startChar'], words[1]['endChar']), (6, 11))
    self.assertAlmostEqual(words[0]['endTime'], 1.28)
    self.assertAlmostEqual(words[1]['endTime'], 2.08)

  def test_missing_emission_timing_does_not_invent_alignment(self):
    self.assertIsNone(bridge.words_from_tokens('hello', [0], [None], lambda _: 'hello', 5))

  def test_unsupported_locale_and_wrong_checkpoint_are_rejected(self):
    with self.assertRaises(ValueError):
      bridge.locale('lt-LT')
    with tempfile.TemporaryDirectory() as directory:
      model = Path(directory) / 'wrong.nemo'
      model.write_bytes(b'wrong model')
      with self.assertRaises(ValueError):
        bridge.checkpoint(model, bridge.ASR_HASH)

  def test_late_speech_retains_absolute_offsets_and_eighth_slot(self):
    words = bridge.words_from_tokens('tail speech', [0, 1],
      [(4198, 4198.08), (4199, 4199.08)],
      lambda ids: ' '.join(['tail', 'speech'][i] for i in ids), 4200)
    segment = bridge.segments_from_words('tail speech', words, 7)[0]
    self.assertEqual(segment['speakerTag'], 8)
    self.assertEqual((segment['startTime'], segment['endTime']), (4198, 4199.08))
    self.assertEqual(segment['words'][1]['startChar'], 5)

  def test_failed_cli_preserves_existing_output(self):
    with tempfile.TemporaryDirectory() as directory:
      output = Path(directory) / 'existing.json'
      output.write_text('unchanged')
      result = subprocess.run([sys.executable, str(spec.origin), '--audio', 'missing.wav',
        '--model', 'missing.nemo', '--diarizer', 'missing.nemo', '--output', str(output),
        '--threads', '0', '--backend', 'gpu'], capture_output=True, text=True)
      self.assertEqual(result.returncode, 1)
      self.assertEqual(output.read_text(), 'unchanged')

  def test_unicode_byte_fallback_uses_all_lexical_emission_frames(self):
    words = bridge.words_from_tokens('é', [0, 1], [(1, 1.08), (1.2, 1.28)],
      lambda ids: '�' if len(ids) == 1 else 'é', 5)
    self.assertEqual((words[0]['startTime'], words[0]['endTime']), (1, 1.28))
    self.assertEqual(words[0]['endChar'], 1)

  def test_delayed_punctuation_does_not_make_an_hour_long_word(self):
    tokens = ['country', ',', ' and']
    words = bridge.words_from_tokens('country, and', [0, 1, 2],
      [(10, 10.08), (4170, 4170.08), (4170.08, 4170.16)],
      lambda ids: ''.join(tokens[i] for i in ids), 4200)
    self.assertEqual(words[0]['text'], 'country,')
    self.assertEqual(words[0]['endTime'], 10.08)
    self.assertEqual(words[0]['endChar'], 8)
    segments = bridge.segments_from_words('country, and', words, 0)
    self.assertEqual([(segment['startTime'], segment['endTime']) for segment in segments],
      [(10, 10.08), (4170.08, 4170.16)])


if __name__ == '__main__':
  unittest.main()
