"""Behavioral transcript contracts using synthetic audio and runtime boundaries only."""

import copy
import datetime
import json
from pathlib import Path
import tempfile
import unittest
import uuid
import wave

from transcribe import (TranscriptionError, canonical_transcript, validate_transcript_payload, validate_options,
                        PROFILE_DEFAULTS, _publish_exports, render_exports, srt_timestamp, transcribe_session, validate_session)
from transcribe import (load_transcript_edit_state, save_transcript_edit_state, edit_transcript_text,
                        undo_transcript_edit, redo_transcript_edit, session_lock, rename_speaker, confirm_segment,
                        reject_segment, create_speaker_and_reassign, reassign_segment)
import subprocess
import sys
import signal


def runtime_payload(profile='multitalker-parakeet', tag=1, text='Café 🎧!'):
  return {'schemaVersion': 1, 'engine': profile, 'speakerCapacity': 4 if profile == 'multitalker-parakeet' else 8,
    'durationSeconds': 1, 'segments': [{'startTime': 0.1, 'endTime': 0.9, 'text': text, 'speakerTag': tag,
      'words': [{'text': 'Café', 'startTime': 0.1, 'endTime': 0.4, 'startChar': 0, 'endChar': 4},
                {'text': '🎧!', 'startTime': 0.5, 'endTime': 0.9, 'startChar': 5, 'endChar': 7}]}],
    'speakerTurns': [{'startTime': 0.05, 'endTime': 0.95, 'speakerTag': 1}],
    'provenance': {'runtimeVersion': 'synthetic-boundary', 'modelRevision': 'synthetic-fixture',
      'timingOrigin': 'fixture-declared', 'backend': 'cpu'}}


class SchemaTests(unittest.TestCase):
  def setUp(self):
    self.manifest = {'id': str(uuid.uuid4()), 'title': 'Synthetic contract', 'durationSeconds': 1}

  def canonical(self, outputs=None, profile='multitalker-parakeet'):
    return canonical_transcript(self.manifest, outputs or {source: runtime_payload(profile) for source in ('me', 'them')},
      profile=profile, language='en', backend='cpu')

  def test_source_scoped_identity_unicode_words_and_independent_turns(self):
    value = self.canonical()
    self.assertEqual([speaker['label'] for speaker in value['speakers']], ['Speaker 1', 'Speaker 2'])
    me, them = value['segments']
    self.assertNotEqual(me['speakerId'], them['speakerId'])
    self.assertEqual(me['words'][1]['endChar'], len(me['text']))
    self.assertEqual(me['words'][0]['speakerId'], me['speakerId'])
    self.assertEqual(me['words'][0]['source'], 'me')
    self.assertNotEqual(value['speakerTurns'][0]['startTime'], me['words'][0]['startTime'])
    self.assertNotEqual(value['generation']['id'], self.canonical()['generation']['id'])
    self.assertIn('Speaker 1 (Me)', render_exports(value)['transcript.txt'])
    self.assertIn('Speaker 2 (Them)', render_exports(value)['transcript.srt'])

  def test_overlap_retains_distinct_recognized_voice_segments(self):
    outputs = {source: runtime_payload() for source in ('me', 'them')}
    second = copy.deepcopy(outputs['them']['segments'][0])
    second['speakerTag'] = 2
    outputs['them']['segments'].append(second)
    outputs['them']['speakerTurns'].append({'startTime': 0.05, 'endTime': 0.95, 'speakerTag': 2})
    value = self.canonical(outputs)
    self.assertEqual(len(value['segments']), 3)
    self.assertEqual(len({segment['speakerId'] for segment in value['segments']}), 3)
    self.assertEqual(value['segments'][1]['startTime'], value['segments'][2]['startTime'])

  def test_long_speaker_stream_splits_only_at_aligned_pauses_without_losing_unicode_or_identity(self):
    self.manifest['durationSeconds'] = 4200
    outputs = {source: runtime_payload() for source in ('me', 'them')}
    for output in outputs.values():
      output['durationSeconds'] = 4200
      segment = output['segments'][0]
      segment['endTime'] = 4199
      segment['words'][1].update(startTime=4198, endTime=4199)
    value = self.canonical(outputs)
    me = [segment for segment in value['segments'] if segment['source'] == 'me']
    self.assertEqual([(s['text'], s['startTime'], s['endTime']) for s in me], [('Café', .1, .4), ('🎧!', 4198, 4199)])
    self.assertEqual(me[0]['speakerId'], me[1]['speakerId'])
    self.assertEqual(me[1]['words'][0]['startChar'], 0)
    self.assertEqual(me[1]['words'][0]['endChar'], 2)
    outputs['me']['segments'][0]['text'] += ' Unaligned!'
    retained = self.canonical(outputs)
    me = [s for s in retained['segments'] if s['source'] == 'me']
    self.assertEqual(len(me), 1)
    self.assertEqual(me[0]['text'], 'Café 🎧! Unaligned!')

  def test_unavailable_alignment_keeps_text_without_inventing_words_or_identity(self):
    outputs = {source: runtime_payload(tag=None) for source in ('me', 'them')}
    for output in outputs.values():
      del output['segments'][0]['words']
    value = self.canonical(outputs)
    self.assertEqual(value['segments'][0]['text'], 'Café 🎧!')
    self.assertNotIn('words', value['segments'][0])
    self.assertIsNone(value['segments'][0]['speakerId'])
    self.assertEqual(value['segments'][0]['speakerState'], 'unknown')

  def test_rejects_wrong_engine_capacity_backend_and_asr_only(self):
    for change in [{'engine': 'nemotron-3.5'}, {'schemaVersion': True}, {'speakerCapacity': 8},
                   {'durationSeconds': 2}, {'speakerTurns': []}, {'provenance': {}},
                   {'provenance': runtime_payload()['provenance'] | {'backend': 'gpu'}}]:
      outputs = {'me': runtime_payload() | change, 'them': runtime_payload()}
      with self.subTest(change=change), self.assertRaises(TranscriptionError):
        self.canonical(outputs)
    with self.assertRaises(TranscriptionError):
      self.canonical({'me': runtime_payload()})

  def test_rejects_fake_word_spans_zero_timing_and_cross_source_identity(self):
    for change in [{'startChar': True}, {'endChar': 3}, {'text': 'wrong'}, {'endTime': 0.1}, {'endTime': 2}]:
      outputs = {source: runtime_payload() for source in ('me', 'them')}
      outputs['me']['segments'][0]['words'][0].update(change)
      with self.subTest(change=change), self.assertRaises(TranscriptionError):
        self.canonical(outputs)
    value = self.canonical()
    value['segments'][0]['words'][0]['speakerId'] = value['segments'][1]['speakerId']
    with self.assertRaises(TranscriptionError):
      validate_transcript_payload(value, self.manifest)

  def test_eight_speaker_profile_and_four_speaker_cap_are_honest(self):
    outputs = {source: runtime_payload('nemotron-3.5', tag=8) for source in ('me', 'them')}
    for output in outputs.values():
      output['speakerTurns'][0]['speakerTag'] = 8
    self.assertEqual(len(self.canonical(outputs, 'nemotron-3.5')['speakers']), 2)
    outputs = {source: runtime_payload(tag=5) for source in ('me', 'them')}
    with self.assertRaises(TranscriptionError):
      self.canonical(outputs)
    outputs['me']['speakerCapacity'] = 9
    with self.assertRaises(TranscriptionError):
      self.canonical(outputs, 'nemotron-3.5')

  def test_legacy_export_format_is_unchanged(self):
    value = {'schemaVersion': 1, 'meetingId': self.manifest['id'], 'title': 'Legacy',
      'segments': [{'source': 'them', 'speakerLabel': 'Them', 'startTime': 0, 'endTime': 0.9, 'text': 'Legacy words'}]}
    self.assertIs(validate_transcript_payload(value, self.manifest), value)
    self.assertEqual(render_exports(value)['transcript.txt'], '[00:00] Them: Legacy words\n')
    self.assertEqual(render_exports(value)['transcript.srt'], '1\n00:00:00,000 --> 00:00:00,900\nThem: Legacy words\n\n')
    self.assertEqual(srt_timestamp(3661.234), '01:01:01,234')



class SessionTests(unittest.TestCase):
  def setUp(self):
    self.temporary = tempfile.TemporaryDirectory()
    self.addCleanup(self.temporary.cleanup)
    self.directory = Path(self.temporary.name)
    self.manifest = {'schemaVersion': 1, 'id': str(uuid.uuid4()), 'title': 'Synthetic silence',
      'startedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
      'sampleRate': 16000, 'channels': 1, 'status': 'recorded', 'durationSeconds': 1.0,
      'tracks': [{'source': source, 'file': filename, 'device': f'fixture.{source}', 'frames': 16000}
                 for source, filename in [('me', 'mic.wav'), ('them', 'system.wav')]]}
    for track in self.manifest['tracks']:
      with wave.open(str(self.directory / track['file']), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(bytes(32000))

  def save(self, manifest=None):
    (self.directory / 'session.json').write_text(json.dumps(manifest or self.manifest))

  def test_accepts_canonical_aligned_lanes(self):
    self.save()
    manifest, lanes = validate_session(self.directory)
    self.assertEqual(manifest['title'], 'Synthetic silence')
    self.assertEqual([(source, path.name, duration) for source, path, duration in lanes],
                     [('me', 'mic.wav', 1.0), ('them', 'system.wav', 1.0)])

  def test_renamed_speaker_can_be_unconfirmed_reopened_and_confirmed_again(self):
    self.save()
    payload = canonical_transcript(self.manifest, {source: runtime_payload() for source in ('me', 'them')},
      profile='multitalker-parakeet', language='en', backend='cpu')
    (self.directory / 'transcript.json').write_text(json.dumps(payload))
    speaker_id = payload['segments'][0]['speakerId']
    state = rename_speaker(load_transcript_edit_state(self.directory), speaker_id, 'Synthetic Name')
    state = confirm_segment(state, 0)
    save_transcript_edit_state(self.directory, state)
    state = confirm_segment(state, 0, False)
    save_transcript_edit_state(self.directory, state)
    reopened = load_transcript_edit_state(self.directory)
    self.assertNotIn('speakerConfirmed', reopened.payload['segments'][0])
    self.assertEqual(reopened.payload['speakers'][0]['name'], 'Synthetic Name')
    state = confirm_segment(reopened, 0)
    save_transcript_edit_state(self.directory, state)
    reopened = load_transcript_edit_state(self.directory)
    self.assertEqual(reopened.payload['segments'][0]['speakerConfirmed'], speaker_id)
    self.assertIn('Synthetic Name', (self.directory / 'transcript.txt').read_text())
    self.assertIn('Synthetic Name', (self.directory / 'transcript.srt').read_text())

  def seed_speaker_transcript(self):
    self.save()
    payload = canonical_transcript(self.manifest, {source: runtime_payload() for source in ('me', 'them')},
      profile='multitalker-parakeet', language='en', backend='cpu')
    payload['segments'].insert(1, copy.deepcopy(payload['segments'][0]))
    (self.directory / 'transcript.json').write_text(json.dumps(payload))
    return load_transcript_edit_state(self.directory)

  def test_wrong_survives_cancel_reopen_and_undo_and_right_is_idempotent(self):
    initial = self.seed_speaker_transcript()
    speaker_id = initial.payload['segments'][0]['speakerId']
    right = confirm_segment(initial, 0)
    self.assertIs(confirm_segment(right, 0), right)
    wrong = reject_segment(right, 0)
    self.assertIs(reject_segment(wrong, 0), wrong)
    save_transcript_edit_state(self.directory, wrong)
    reopened = load_transcript_edit_state(self.directory)
    self.assertEqual(reopened.payload['segments'][0]['speakerRejected'], speaker_id)
    self.assertNotIn('speakerConfirmed', reopened.payload['segments'][0])
    self.assertEqual(undo_transcript_edit(reopened).payload, right.payload)
    self.assertEqual(redo_transcript_edit(undo_transcript_edit(reopened)).payload, wrong.payload)
    corrected = confirm_segment(reopened, 0)
    self.assertNotIn('speakerRejected', corrected.payload['segments'][0])
    for feedback in (wrong, right):
      changed = reassign_segment(feedback, 0, speaker_id)
      self.assertNotIn('speakerConfirmed', changed.payload['segments'][0])
      self.assertNotIn('speakerRejected', changed.payload['segments'][0])

  def test_create_person_applies_only_to_target_preserves_audio_identity_and_undo_redo(self):
    initial = self.seed_speaker_transcript()
    before_audio = {name: (self.directory / name).read_bytes() for name in ('mic.wav', 'system.wav')}
    initial = reject_segment(initial, 0)
    created = create_speaker_and_reassign(initial, 0, ' Same Name ')
    new = created.payload['speakers'][-1]
    self.assertEqual((new['name'], new['origin'], new['runtimeTag'], new['source'], new['label']),
                     ('Same Name', 'manual', None, 'me', 'Speaker 3'))
    uuid.UUID(new['id'])
    self.assertNotIn('speakerRejected', created.payload['segments'][0])
    self.assertNotIn('speakerConfirmed', created.payload['segments'][0])
    self.assertEqual(created.payload['segments'][1:], initial.payload['segments'][1:])
    self.assertEqual(created.payload['segments'][0]['words'], initial.payload['segments'][0]['words'])
    self.assertEqual(created.payload['speakerTurns'], initial.payload['speakerTurns'])
    save_transcript_edit_state(self.directory, created)
    reopened = load_transcript_edit_state(self.directory)
    undone = undo_transcript_edit(reopened)
    self.assertEqual(undone.payload, initial.payload)
    save_transcript_edit_state(self.directory, undone)
    redone = redo_transcript_edit(load_transcript_edit_state(self.directory))
    self.assertEqual(redone.payload, created.payload)
    save_transcript_edit_state(self.directory, redone)
    for _ in range(5):
      redone = create_speaker_and_reassign(redone, 0, 'Same Name')
    save_transcript_edit_state(self.directory, redone)
    reopened = load_transcript_edit_state(self.directory)
    manual = [s for s in reopened.payload['speakers'] if s.get('origin') == 'manual']
    self.assertEqual(len(manual), 6)
    self.assertEqual(len({s['id'] for s in manual}), 6)
    self.assertEqual({s['name'] for s in manual}, {'Same Name'})
    self.assertEqual(before_audio, {name: (self.directory / name).read_bytes() for name in before_audio})

  def test_persisted_speaker_operations_reject_cross_source_even_in_redo_history(self):
    initial = self.seed_speaker_transcript()
    created = create_speaker_and_reassign(initial, 0, 'Person')
    save_transcript_edit_state(self.directory, undo_transcript_edit(created))
    history_path = self.directory / 'transcript-edits.json'
    history = json.loads(history_path.read_text())
    for operation in ({'kind': 'reject', 'index': 0, 'speakerId': initial.payload['segments'][2]['speakerId']},
                      {'kind': 'reassign', 'index': 0, 'speakerId': None},
                      {'kind': 'confirm', 'index': 0},
                      {'kind': 'create-speaker', 'index': 0,
                       'speaker': created.payload['speakers'][-1] | {'source': 'them'}}):
      with self.subTest(kind=operation['kind']):
        history_path.write_text(json.dumps(history | {'operations': [operation]}))
        with self.assertRaises(TranscriptionError):
          load_transcript_edit_state(self.directory)

  def test_rejects_failed_recording_versions_and_duplicate_lanes(self):
    for key, value in [('status', 'failed'), ('status', 'recording'), ('schemaVersion', 2),
                       ('schemaVersion', True), ('durationSeconds', 2), ('id', 'bad')]:
      with self.subTest(key=key, value=value):
        manifest = copy.deepcopy(self.manifest)
        manifest[key] = value
        self.save(manifest)
        with self.assertRaises(TranscriptionError):
          validate_session(self.directory)
    manifest = copy.deepcopy(self.manifest)
    manifest['tracks'][1] = manifest['tracks'][0]
    self.save(manifest)
    with self.assertRaises(TranscriptionError):
      validate_session(self.directory)

  def test_rejects_path_traversal_and_wrong_frames(self):
    for key, value in [('file', '../mic.wav'), ('frames', 15999), ('frames', True)]:
      with self.subTest(key=key):
        manifest = copy.deepcopy(self.manifest)
        manifest['tracks'][0][key] = value
        self.save(manifest)
        with self.assertRaises(TranscriptionError):
          validate_session(self.directory)

  def test_rejects_truncated_sample_data(self):
    path = self.directory / 'mic.wav'
    with path.open('r+b') as audio:
      audio.truncate(100)
    self.save()
    with self.assertRaises(TranscriptionError):
      validate_session(self.directory)

  def test_input_failure_preserves_existing_exports(self):
    self.save()
    names = ['transcript.json', 'transcript.txt', 'transcript.srt']
    for name in names:
      (self.directory / name).write_text('Existing good export\n')
    with self.assertRaises(TranscriptionError):
      transcribe_session(self.directory, profile='multitalker-parakeet', model=self.directory / 'missing-model',
                         runtime=self.directory / 'missing-runtime')
    for name in names:
      self.assertEqual((self.directory / name).read_text(), 'Existing good export\n')

  def test_published_exports_are_private_and_rerunnable(self):
    exports = {name: 'Private fixture text\n' for name in
               ['transcript.json', 'transcript.txt', 'transcript.srt']}
    for attempt in range(2):
      with tempfile.TemporaryDirectory(dir=self.directory) as temporary:
        _publish_exports(self.directory, Path(temporary), exports)
      for name, content in exports.items():
        path = self.directory / name
        self.assertEqual(path.read_text(), content)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

  def test_invalid_export_destination_preserves_prior_outputs(self):
    path = self.directory / 'transcript.json'
    path.write_text('Existing good JSON\n')
    (self.directory / 'transcript.srt').mkdir()
    with tempfile.TemporaryDirectory(dir=self.directory) as temporary:
      with self.assertRaises(TranscriptionError):
        _publish_exports(self.directory, Path(temporary),
                         {'transcript.json': 'New JSON', 'transcript.srt': 'New SRT'})
    self.assertEqual(path.read_text(), 'Existing good JSON\n')

  def test_original_alignment_survives_typing_restart_undo_and_redo(self):
    state = self.seed_transcript()
    value = state.payload
    value['segments'][0]['words'] = [
      {'text': 'Generated', 'startChar': 0, 'endChar': 9, 'startTime': 0.1, 'endTime': 0.5},
      {'text': 'words', 'startChar': 10, 'endChar': 15, 'startTime': 0.5, 'endTime': 0.9}]
    (self.directory / 'transcript.json').write_text(json.dumps(value))
    original = load_transcript_edit_state(self.directory)
    corrected = edit_transcript_text(original, 0, 'Short')
    self.assertNotIn('words', corrected.payload['segments'][0])
    self.assertEqual(corrected.generated_payload, value)
    detached = corrected.generated_payload
    detached['segments'][0]['words'].clear()
    self.assertEqual(corrected.generated_payload, value)
    save_transcript_edit_state(self.directory, corrected)
    reopened = load_transcript_edit_state(self.directory)
    self.assertEqual(reopened.generated_payload, value)
    undone = undo_transcript_edit(reopened)
    self.assertEqual(undone.payload, value)
    self.assertEqual(redo_transcript_edit(undone).generated_payload, value)
    restored = edit_transcript_text(reopened, 0, 'Generated words')
    self.assertEqual(restored.payload, value)
    self.assertFalse(restored.has_corrections)

  def test_saved_alignment_rejects_invalid_char_spans_and_time_order(self):
    state = self.seed_transcript()
    value = state.payload
    good = {'text': 'Generated', 'startChar': 0, 'endChar': 9, 'startTime': 0.1, 'endTime': 0.5}
    for change in [{'startChar': True}, {'endChar': 99}, {'text': 'Wrong'},
                   {'endChar': 3, 'text': 'Gen'}, {'startChar': 1, 'text': 'enerated'},
                   {'endChar': 15, 'text': 'Generated words'},
                   {'startTime': float('nan')}, {'endTime': 1.1}, {'endTime': 0.05}]:
      value['segments'][0]['words'] = [good | change]
      (self.directory / 'transcript.json').write_text(json.dumps(value))
      with self.subTest(change=change), self.assertRaises(TranscriptionError):
        load_transcript_edit_state(self.directory)
    value['segments'][0]['words'] = [good,
      {'text': 'words', 'startChar': 10, 'endChar': 15, 'startTime': 0.05, 'endTime': 0.6}]
    (self.directory / 'transcript.json').write_text(json.dumps(value))
    with self.assertRaises(TranscriptionError):
      load_transcript_edit_state(self.directory)

  def seed_transcript(self):
    self.save()
    value = {'schemaVersion': 1, 'meetingId': self.manifest['id'], 'title': self.manifest['title'],
      'segments': [{'source': 'me', 'speakerLabel': 'Me', 'startTime': 0, 'endTime': 1, 'text': 'Generated words'}]}
    (self.directory / 'transcript.json').write_text(json.dumps(value))
    return load_transcript_edit_state(self.directory)

  def test_descendant_autosave_coalescing_restart_and_empty_human_text(self):
    original = self.seed_transcript()
    first = edit_transcript_text(original, 0, 'Human')
    latest = edit_transcript_text(first, 0, 'Human [BLANK_AUDIO]', new_group=False)
    save_transcript_edit_state(self.directory, first)
    save_transcript_edit_state(self.directory, latest)
    reopened = load_transcript_edit_state(self.directory)
    self.assertTrue(reopened.has_corrections)
    self.assertIn('[BLANK_AUDIO]', (self.directory / 'transcript.txt').read_text())
    undone = undo_transcript_edit(reopened)
    save_transcript_edit_state(self.directory, undone)
    self.assertEqual(load_transcript_edit_state(self.directory).payload, original.payload)
    redone = redo_transcript_edit(undone)
    save_transcript_edit_state(self.directory, redone)
    empty = edit_transcript_text(redone, 0, '')
    save_transcript_edit_state(self.directory, empty)
    self.assertEqual(load_transcript_edit_state(self.directory).payload['segments'][0]['text'], '')
    self.assertEqual(empty.payload['segments'][0]['source'], 'me')
    self.assertEqual(empty.payload['segments'][0]['endTime'], 1)
    detached = empty.payload
    detached['segments'][0]['text'] = 'Mutation'
    self.assertEqual(empty.payload['segments'][0]['text'], '')

  def test_stale_branch_and_mismatched_history_preserve_saved_corrections(self):
    original = self.seed_transcript()
    corrected = edit_transcript_text(original, 0, 'Correction')
    save_transcript_edit_state(self.directory, corrected)
    before = (self.directory / 'transcript.json').read_bytes()
    with self.assertRaises(TranscriptionError):
      save_transcript_edit_state(self.directory, edit_transcript_text(original, 0, 'Stale'))
    self.assertEqual((self.directory / 'transcript.json').read_bytes(), before)
    path = self.directory / 'transcript-edits.json'
    history = json.loads(path.read_text())
    history['meetingId'] = str(uuid.uuid4())
    path.write_text(json.dumps(history))
    with self.assertRaisesRegex(TranscriptionError, 'saved corrections'):
      load_transcript_edit_state(self.directory)
    self.assertEqual((self.directory / 'transcript.json').read_bytes(), before)

  def test_busy_recording_refuses_save(self):
    state = self.seed_transcript()
    with session_lock(self.directory), self.assertRaisesRegex(TranscriptionError, 'busy'):
      save_transcript_edit_state(self.directory, edit_transcript_text(state, 0, 'Blocked'))

  def test_cancel_between_renames_rolls_back_all_exports(self):
    state = self.seed_transcript()
    save_transcript_edit_state(self.directory, state)
    names = list(render_exports(state.payload)) + ['transcript-edits.json']
    before = {name: (self.directory / name).read_bytes() for name in names}
    code = '''import os, signal, sys, tempfile
from pathlib import Path
import transcribe
session = Path(sys.argv[1])
state = transcribe.edit_transcript_text(transcribe.load_transcript_edit_state(session), 0, 'Cancelled replacement')
replace = os.replace
sent = False
def interrupt(source, destination):
  global sent
  replace(source, destination)
  if not sent and Path(destination).name == 'transcript.json':
    sent = True
    os.kill(os.getpid(), signal.SIGTERM)
transcribe.os.replace = interrupt
transcribe.save_transcript_edit_state(session, state)
'''
    process = subprocess.run([sys.executable, '-c', code, str(self.directory)], cwd=Path(__file__).parent)
    self.assertEqual(process.returncode, -signal.SIGTERM)
    self.assertEqual({name: (self.directory / name).read_bytes() for name in names}, before)

  def test_failed_inference_preserves_corrections_and_history(self):
    state = edit_transcript_text(self.seed_transcript(), 0, 'Saved human text')
    save_transcript_edit_state(self.directory, state)
    names = list(render_exports(state.payload)) + ['transcript-edits.json']
    before = {name: (self.directory / name).read_bytes() for name in names}
    model = self.directory / 'model.bin'
    model.write_bytes(b'Synthetic model boundary')
    with self.assertRaisesRegex(TranscriptionError, 'exited'):
      transcribe_session(self.directory, profile='multitalker-parakeet', model=model, runtime=Path('/usr/bin/false'), use_gpu=False)
    self.assertEqual({name: (self.directory / name).read_bytes() for name in names}, before)
    self.assertTrue(load_transcript_edit_state(self.directory).has_corrections)

  def test_partial_save_failure_rolls_back_canonical_and_history(self):
    from unittest.mock import patch
    import os
    state = self.seed_transcript()
    save_transcript_edit_state(self.directory, state)
    names = list(render_exports(state.payload)) + ['transcript-edits.json']
    before = {name: (self.directory / name).read_bytes() for name in names}
    replace = os.replace
    def fail_history(source, destination):
      if Path(source).name == 'transcript-edits.json':
        raise OSError('Synthetic full disk at history publication')
      replace(source, destination)
    with patch('transcribe.os.replace', side_effect=fail_history), self.assertRaises(TranscriptionError):
      save_transcript_edit_state(self.directory, edit_transcript_text(state, 0, 'Unsaved'))
    self.assertEqual({name: (self.directory / name).read_bytes() for name in names}, before)
    self.assertEqual(load_transcript_edit_state(self.directory).payload, state.payload)

  def test_runtime_executor_receives_full_lanes_and_publishes_validated_output(self):
    self.seed_transcript()
    model, runtime = self.synthetic_runtime()
    commands = []
    def execute(command):
      commands.append(command)
      output = Path(command[command.index('--output') + 1])
      output.write_text(json.dumps(runtime_payload()))
      return subprocess.CompletedProcess(command, 0, '', '')
    value = transcribe_session(self.directory, profile='multitalker-parakeet', model=model,
      runtime=runtime, use_gpu=False, runtime_executor=execute)
    self.assertEqual(len(commands), 2)
    self.assertEqual([Path(command[command.index('--audio') + 1]).name for command in commands],
                     ['mic.wav', 'system.wav'])
    self.assertEqual(load_transcript_edit_state(self.directory).payload, value)

  def test_callback_cancellation_during_publication_rolls_back_all_exports(self):
    from unittest.mock import patch
    import os
    original = self.seed_transcript()
    save_transcript_edit_state(self.directory, original)
    before = {name: (self.directory / name).read_bytes() for name in (*render_exports(original.payload), 'transcript-edits.json')}
    model, runtime = self.synthetic_runtime()
    cancelled = False
    replace = os.replace
    def cancel_after_first(source, destination):
      nonlocal cancelled
      replace(source, destination)
      cancelled = True
    with patch('transcribe.os.replace', side_effect=cancel_after_first), self.assertRaises(TranscriptionError):
      transcribe_session(self.directory, profile='multitalker-parakeet', model=model,
        runtime=runtime, use_gpu=False, cancelled=lambda: cancelled)
    self.assertEqual({name: (self.directory / name).read_bytes() for name in before}, before)

  def synthetic_runtime(self, *, fail_them=False):
    model = self.directory / 'synthetic-model'
    model.write_bytes(b'Not a real model; protocol boundary fixture only')
    runtime = self.directory / 'synthetic-runtime'
    code = f'#!{sys.executable}\n' + '''import json, sys
from pathlib import Path
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
'''
    code += f'payload = {runtime_payload()!r}\n'
    code += f"if {fail_them!r} and Path(args['--audio']).name == 'system.wav': sys.exit(7)\n"
    code += "Path(args['--output']).write_text(json.dumps(payload), encoding='utf-8')\n"
    runtime.write_text(code)
    runtime.chmod(0o700)
    return model, runtime

  def test_dispatch_replacement_restores_whole_legacy_generation_on_undo(self):
    original = self.seed_transcript()
    save_transcript_edit_state(self.directory, original)
    model, runtime = self.synthetic_runtime()
    audio_before = {track['file']: (self.directory / track['file']).read_bytes() for track in self.manifest['tracks']}
    value = transcribe_session(self.directory, profile='multitalker-parakeet', model=model, runtime=runtime, use_gpu=False)
    state = load_transcript_edit_state(self.directory)
    self.assertEqual(state.payload, value)
    self.assertEqual(value['schemaVersion'], 2)
    corrected = edit_transcript_text(state, 0, 'Human correction')
    self.assertNotIn('words', corrected.payload['segments'][0])
    self.assertEqual(corrected.generated_payload, value)
    save_transcript_edit_state(self.directory, corrected)
    reopened = load_transcript_edit_state(self.directory)
    self.assertEqual(reopened.generated_payload, value)
    self.assertEqual(reopened.payload['speakers'], value['speakers'])
    undo_correction = undo_transcript_edit(reopened)
    save_transcript_edit_state(self.directory, undo_correction)
    undo_replacement = undo_transcript_edit(undo_correction)
    save_transcript_edit_state(self.directory, undo_replacement)
    self.assertEqual(load_transcript_edit_state(self.directory).payload, original.payload)
    save_transcript_edit_state(self.directory, redo_transcript_edit(undo_replacement))
    self.assertEqual(load_transcript_edit_state(self.directory).payload, value)
    self.assertEqual({name: (self.directory / name).read_bytes() for name in audio_before}, audio_before)
    for name in ('transcript.json', 'transcript.txt', 'transcript.srt', 'transcript-edits.json'):
      self.assertEqual((self.directory / name).stat().st_mode & 0o777, 0o600)

  def test_second_lane_failure_never_publishes_partial_success(self):
    original = self.seed_transcript()
    save_transcript_edit_state(self.directory, original)
    before = {name: (self.directory / name).read_bytes() for name in (*render_exports(original.payload), 'transcript-edits.json')}
    model, runtime = self.synthetic_runtime(fail_them=True)
    with self.assertRaisesRegex(TranscriptionError, 'them:.*exited 7'):
      transcribe_session(self.directory, profile='multitalker-parakeet', model=model, runtime=runtime, use_gpu=False)
    self.assertEqual({name: (self.directory / name).read_bytes() for name in before}, before)

  def test_isolated_python_dependencies_remain_available_during_nemotron_dispatch(self):
    from unittest.mock import patch
    from dataclasses import replace
    self.save()
    environment = self.directory / 'isolated-python'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(environment)],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    runtime = environment / 'bin/python'
    packages = Path(subprocess.check_output([str(runtime), '-c',
      'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True).strip())
    packages.mkdir(parents=True, exist_ok=True)
    (packages / 'isolated_fixture.py').write_text(f'PAYLOAD = {runtime_payload("nemotron-3.5")!r}\n')
    script = self.directory / 'isolated-bridge.py'
    script.write_text('import json, sys\nfrom pathlib import Path\nfrom isolated_fixture import PAYLOAD\n'
                      'Path(sys.argv[sys.argv.index("--output") + 1]).write_text(json.dumps(PAYLOAD))\n')
    model = self.directory / 'synthetic-model'
    diarizer = self.directory / 'synthetic-diarizer'
    model.write_bytes(b'Protocol fixture'); diarizer.write_bytes(b'Protocol fixture')
    defaults = replace(PROFILE_DEFAULTS['nemotron-3.5'], backend_python_script=script)
    with patch('transcribe.PROFILE_DEFAULTS', dict(PROFILE_DEFAULTS) | {'nemotron-3.5': defaults}):
      transcribe_session(self.directory, profile='nemotron-3.5', model=model,
                         runtime=runtime, diarizer=diarizer, use_gpu=False)
    state = load_transcript_edit_state(self.directory)
    self.assertEqual([(s['source'], s['text']) for s in state.payload['segments']],
                     [('me', 'Café 🎧!'), ('them', 'Café 🎧!')])

  def test_language_and_profile_preflight_never_switches_models(self):
    model, runtime = self.synthetic_runtime()
    with self.assertRaisesRegex(TranscriptionError, 'English-only'):
      validate_options(profile='multitalker-parakeet', model=model, runtime=runtime, language='es-ES')
    with self.assertRaisesRegex(TranscriptionError, 'Unknown transcription profile'):
      validate_options(profile='other', model=model, runtime=runtime)
    with self.assertRaisesRegex(TranscriptionError, 'cannot expand capacity'):
      validate_options(profile='multitalker-parakeet', model=model, runtime=runtime, diarizer=model)

  def test_review_revision_preserves_generated_attribution_through_edits_and_rejects_reruns(self):
    from review import _revision, review_findings
    from transcribe import speaker_label
    self.save()
    model, runtime = self.synthetic_runtime()
    value = transcribe_session(self.directory, profile='multitalker-parakeet', model=model, runtime=runtime, use_gpu=False)
    original = load_transcript_edit_state(self.directory)
    segment = value['segments'][0]
    finding = {'id': 'fixture-finding', 'segmentIndex': 0, 'originalAsr': segment['text'],
      'source': segment['source'], 'startTime': segment['startTime'], 'endTime': segment['endTime']}
    store = {'runs': [{'generatedRevision': _revision(value), 'findings': [finding]}], 'decisions': []}
    corrected = edit_transcript_text(original, 0, 'Correction')
    save_transcript_edit_state(self.directory, corrected)
    reopened = load_transcript_edit_state(self.directory)
    self.assertEqual(_revision(reopened.generated_payload), _revision(value))
    self.assertEqual(len(review_findings(store, reopened)), 1)
    self.assertEqual(speaker_label(reopened.payload, reopened.payload['segments'][0]), 'Speaker 1')
    rerun = transcribe_session(self.directory, profile='multitalker-parakeet', model=model, runtime=runtime, use_gpu=False)
    self.assertNotEqual(rerun['speakers'][0]['id'], value['speakers'][0]['id'])
    self.assertEqual(review_findings(store, load_transcript_edit_state(self.directory)), [])
    save_transcript_edit_state(self.directory, undo_transcript_edit(load_transcript_edit_state(self.directory)))
    self.assertEqual(len(review_findings(store, load_transcript_edit_state(self.directory))), 1)

  def test_legacy_reader_never_rewrites_transcript_or_history(self):
    state = self.seed_transcript()
    save_transcript_edit_state(self.directory, state)
    names = ('transcript.json', 'transcript-edits.json')
    before = {name: (self.directory / name).read_bytes() for name in names}
    self.assertEqual(load_transcript_edit_state(self.directory).payload, state.payload)
    self.assertEqual({name: (self.directory / name).read_bytes() for name in names}, before)

if __name__ == '__main__':
  unittest.main()

