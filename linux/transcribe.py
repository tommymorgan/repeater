"""Offline two-engine transcription with source-safe speaker generations."""

import datetime
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid
import wave

from contextlib import contextmanager
from dataclasses import dataclass
import copy
import fcntl
import hashlib
import signal


@contextmanager
def session_lock(session: Path, *, shared: bool = False):
  if not session.is_absolute() or session.is_symlink() or session.resolve() != session:
    raise TranscriptionError('Recording path must be an absolute local directory without symlinks')
  descriptor = os.open(session, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
  try:
    try:
      fcntl.flock(descriptor, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    except BlockingIOError as error:
      raise TranscriptionError('This recording is busy; wait for its current job to finish') from error
    yield
  finally:
    os.close(descriptor)


def _validate_identity(entry, registry, source):
  speaker_id, state = entry.get('speakerId'), entry.get('speakerState')
  if state not in ('assigned', 'unknown', 'unavailable'):
    raise TranscriptionError('Invalid speaker state')
  if state == 'assigned':
    if not isinstance(speaker_id, str) or speaker_id not in registry or registry[speaker_id]['source'] != source:
      raise TranscriptionError('Speaker identity must belong to the same audio source')
  elif speaker_id is not None:
    raise TranscriptionError('Unknown/unavailable speakers must have a null identity')


def _validate_v2(payload, duration):
  generation = payload.get('generation')
  if not isinstance(generation, dict) or not isinstance(generation.get('profile'), str) or generation['profile'] not in PROFILE_DEFAULTS:
    raise TranscriptionError('Transcript needs a supported generation profile')
  try:
    uuid.UUID(generation['id'])
  except (KeyError, TypeError, ValueError, AttributeError) as error:
    raise TranscriptionError('Generation needs an opaque UUID') from error
  if generation.get('backend') not in ('cpu', 'gpu') or not isinstance(generation.get('language'), str):
    raise TranscriptionError('Generation needs backend and language provenance')
  provenance = generation.get('provenance')
  if not isinstance(provenance, dict) or set(provenance) != set(LANES):
    raise TranscriptionError('Generation needs runtime provenance for both sources')
  for evidence in provenance.values():
    _validate_provenance(evidence, generation['backend'])
  speakers = payload.get('speakers')
  if not isinstance(speakers, list):
    raise TranscriptionError('Transcript needs a source-scoped speaker registry')
  registry, tags = {}, set()
  capacity = PROFILE_DEFAULTS[generation['profile']].capacity
  for ordinal, speaker in enumerate(speakers, 1):
    if not isinstance(speaker, dict) or not isinstance(speaker.get('source'), str) or speaker['source'] not in LANES:
      raise TranscriptionError('Invalid speaker registry source')
    try:
      uuid.UUID(speaker['id'])
    except (KeyError, TypeError, ValueError, AttributeError) as error:
      raise TranscriptionError('Speaker IDs must be opaque UUIDs') from error
    tag = speaker.get('runtimeTag')
    key = (speaker['source'], tag)
    name = speaker.get('name')
    if name is not None and (not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or name != name.strip()):
      raise TranscriptionError('Speaker name must be trimmed text of 1 to 60 characters')
    manual = speaker.get('origin') == 'manual'
    if (speaker['id'] in registry or speaker.get('label') != f'Speaker {ordinal}' or
        (manual and ('runtimeTag' not in speaker or tag is not None)) or
        (not manual and (type(tag) is not int or not 1 <= tag <= capacity or key in tags))):
      raise TranscriptionError('Invalid, duplicated or over-capacity speaker registry entry')
    registry[speaker['id']] = speaker
    if not manual:
      tags.add(key)
  turns = payload.get('speakerTurns')
  if not isinstance(turns, list):
    raise TranscriptionError('Transcript needs separate acoustic speakerTurns')
  for turn in turns:
    if not isinstance(turn, dict) or not isinstance(turn.get('source'), str) or turn['source'] not in LANES:
      raise TranscriptionError('Invalid speaker turn source')
    _validate_identity({**turn, 'speakerState': 'assigned'}, registry, turn['source'])
    start, end = _duration(turn.get('startTime'), 'turn start'), _duration(turn.get('endTime'), 'turn end')
    if not start < end <= duration + 1 / 16000:
      raise TranscriptionError('Invalid acoustic speaker turn timing')
  for segment in payload['segments']:
    _validate_identity(segment, registry, segment['source'])
    for field in ('speakerConfirmed', 'speakerRejected'):
      feedback = segment.get(field)
      if feedback is not None and (not isinstance(feedback, str) or feedback not in registry or
          registry[feedback]['source'] != segment['source'] or feedback != segment.get('speakerId')):
        raise TranscriptionError('Speaker feedback must reference the assigned same-source speaker')
    if segment.get('speakerConfirmed') is not None and segment.get('speakerRejected') is not None:
      raise TranscriptionError('Speaker cannot be both confirmed and rejected')
    if segment['text'] and segment['startTime'] >= segment['endTime']:
      raise TranscriptionError('Recognized text requires positive audio timing')
    for word in segment.get('words', []):
      if word.get('source') != segment['source']:
        raise TranscriptionError('Word source identity must match its segment')
      _validate_identity(word, registry, segment['source'])


def _validate_provenance(provenance, backend):
  if not isinstance(provenance, dict) or provenance.get('backend') != backend:
    raise TranscriptionError(f'Requested {backend} backend is not active; explicitly choose the available backend')
  for name in ('runtimeVersion', 'modelRevision', 'timingOrigin'):
    if not isinstance(provenance.get(name), str) or not provenance[name].strip():
      raise TranscriptionError(f'Runtime provenance missing {name}')


def validate_transcript_payload(payload: dict, manifest: dict) -> dict:
  if not isinstance(payload, dict):
    raise TranscriptionError('Transcript must be a JSON object')
  if type(payload.get('schemaVersion')) is not int or payload['schemaVersion'] not in (1, 2):
    raise TranscriptionError('Unsupported transcript schemaVersion (expected 1 or 2)')
  if payload.get('meetingId') != manifest.get('id') or not isinstance(payload.get('meetingId'), str):
    raise TranscriptionError('Transcript does not belong to this recording')
  if not isinstance(payload.get('title'), str) or not isinstance(payload.get('segments'), list):
    raise TranscriptionError('Transcript needs a title and segments array')
  duration = _duration(manifest.get('durationSeconds'), 'durationSeconds')
  for index, segment in enumerate(payload['segments']):
    if not isinstance(segment, dict) or not isinstance(segment.get('source'), str) or segment['source'] not in LANES:
      raise TranscriptionError(f'Invalid source at segment {index}')
    if not isinstance(segment.get('text'), str) or (payload['schemaVersion'] == 1 and segment.get('speakerLabel') != LANES[segment['source']][1]):
      raise TranscriptionError(f'Invalid speaker or text at segment {index}')
    start = _duration(segment.get('startTime'), 'startTime')
    end = _duration(segment.get('endTime'), 'endTime')
    if start > end or end > duration + 1 / 16000:
      raise TranscriptionError(f'Invalid timestamps at segment {index}')
    if 'words' in segment:
      words = segment['words']
      if not isinstance(words, list):
        raise TranscriptionError(f'Invalid words at segment {index}')
      previous_char = 0
      previous_start = previous_end = start
      for word in words:
        if not isinstance(word, dict) or not isinstance(word.get('text'), str):
          raise TranscriptionError(f'Invalid word at segment {index}')
        begin, finish = word.get('startChar'), word.get('endChar')
        if (type(begin) is not int or type(finish) is not int or
            not previous_char <= begin < finish <= len(segment['text']) or
            segment['text'][begin:finish] != word['text'] or
            not re.fullmatch(r'\S+', word['text']) or
            (begin > 0 and not segment['text'][begin - 1].isspace()) or
            (finish < len(segment['text']) and not segment['text'][finish].isspace())):
          raise TranscriptionError(f'Invalid word character span at segment {index}')
        word_start = _duration(word.get('startTime'), 'word startTime')
        word_end = _duration(word.get('endTime'), 'word endTime')
        if (not start <= word_start < word_end <= end or
            word_start < previous_start or word_end < previous_end):
          raise TranscriptionError(f'Invalid word timestamps at segment {index}')
        previous_char, previous_start, previous_end = finish, word_start, word_end
  if payload['schemaVersion'] == 2:
    _validate_v2(payload, duration)
  return payload


def _encoded(value: object) -> str:
  return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


@dataclass(frozen=True)
class TranscriptEditState:
  _baseline: str
  _operations: tuple[str, ...] = ()
  _cursor: int = 0
  _ancestors: frozenset[str] = frozenset()

  def _replay(self):
    current = json.loads(self._baseline)
    generated = copy.deepcopy(current)
    for raw in self._operations[:self._cursor]:
      operation = json.loads(raw)
      if operation['kind'] == 'replace':
        current = copy.deepcopy(operation['payload'])
        generated = copy.deepcopy(current)
      elif operation['kind'] == 'text':
        segment = current['segments'][operation['index']]
        original = generated['segments'][operation['index']]
        segment['text'] = operation['text']
        if segment['text'] == original['text'] and 'words' in original:
          segment['words'] = copy.deepcopy(original['words'])
        else:
          segment.pop('words', None)
      elif operation['kind'] == 'rename':
        for speaker in current['speakers']:
          if speaker['id'] == operation['speakerId']:
            if operation['name'] is None:
              speaker.pop('name', None)
            else:
              speaker['name'] = operation['name']
      elif operation['kind'] in ('reassign', 'create-speaker'):
        if operation['kind'] == 'create-speaker':
          current['speakers'].append(copy.deepcopy(operation['speaker']))
        segment = current['segments'][operation['index']]
        segment['speakerId'] = (operation['speaker']['id'] if operation['kind'] == 'create-speaker'
                                else operation['speakerId'])
        segment['speakerState'] = 'assigned'
        segment.pop('speakerConfirmed', None)
        segment.pop('speakerRejected', None)
      elif operation['kind'] == 'confirm':
        segment = current['segments'][operation['index']]
        if operation['speakerId'] is None:
          segment.pop('speakerConfirmed', None)
        else:
          segment['speakerConfirmed'] = operation['speakerId']
          segment.pop('speakerRejected', None)
      elif operation['kind'] == 'reject':
        segment = current['segments'][operation['index']]
        segment['speakerRejected'] = operation['speakerId']
        segment.pop('speakerConfirmed', None)
    return current, generated

  @property
  def payload(self) -> dict:
    return self._replay()[0]

  @property
  def generated_payload(self) -> dict:
    return self._replay()[1]

  @property
  def can_undo(self) -> bool:
    return self._cursor > 0

  @property
  def can_redo(self) -> bool:
    return self._cursor < len(self._operations)

  @property
  def has_corrections(self) -> bool:
    current, generated = self._replay()
    return current != generated

  def _fingerprint(self) -> str:
    return hashlib.sha256(_encoded([self._baseline, self._operations, self._cursor]).encode()).hexdigest()


def edit_transcript_text(state: TranscriptEditState, index: int, text: str, *, new_group: bool = True) -> TranscriptEditState:
  current = state.payload
  if type(index) is not int or not 0 <= index < len(current['segments']) or not isinstance(text, str):
    raise TranscriptionError('Choose an existing transcript segment and plain text')
  if current['segments'][index]['text'] == text:
    return state
  operations = state._operations[:state._cursor]
  operation = _encoded({'kind': 'text', 'index': index, 'text': text})
  if not new_group and operations:
    previous = json.loads(operations[-1])
    if previous['kind'] == 'text' and previous['index'] == index:
      operations = operations[:-1]
  operations += (operation,)
  return TranscriptEditState(state._baseline, operations, len(operations), state._ancestors | {state._fingerprint()})

def _speaker_operation(state: TranscriptEditState, operation: dict) -> TranscriptEditState:
  if state.payload['schemaVersion'] != 2:
    raise TranscriptionError('Speaker edits need a diarized schema-2 transcript')
  operations = state._operations[:state._cursor] + (_encoded(operation),)
  return TranscriptEditState(state._baseline, operations, len(operations),
                             state._ancestors | {state._fingerprint()})


def _registry_speaker(payload, speaker_id, source=None):
  for speaker in payload['speakers']:
    if speaker['id'] == speaker_id and (source is None or speaker['source'] == source):
      return speaker
  raise TranscriptionError('Choose a speaker from this transcript')


def rename_speaker(state: TranscriptEditState, speaker_id: str, name) -> TranscriptEditState:
  if name is not None:
    if not isinstance(name, str):
      raise TranscriptionError('Speaker name must be text')
    name = name.strip()
    if not 1 <= len(name) <= 60:
      raise TranscriptionError('Speaker name must be 1 to 60 characters')
  _registry_speaker(state.payload, speaker_id)
  return _speaker_operation(state, {'kind': 'rename', 'speakerId': speaker_id, 'name': name})


def reassign_segment(state: TranscriptEditState, index: int, speaker_id: str) -> TranscriptEditState:
  current = state.payload
  if type(index) is not int or not 0 <= index < len(current['segments']):
    raise TranscriptionError('Choose an existing transcript segment')
  segment = current['segments'][index]
  _registry_speaker(current, speaker_id, source=segment['source'])
  return _speaker_operation(state, {'kind': 'reassign', 'index': index, 'speakerId': speaker_id})


def confirm_segment(state: TranscriptEditState, index: int, confirmed: bool = True) -> TranscriptEditState:
  current = state.payload
  if type(index) is not int or not 0 <= index < len(current['segments']):
    raise TranscriptionError('Choose an existing transcript segment')
  segment = current['segments'][index]
  speaker_id = segment.get('speakerId')
  if not confirmed:
    if segment.get('speakerConfirmed') is None:
      return state
    return _speaker_operation(state, {'kind': 'confirm', 'index': index, 'speakerId': None})
  if segment.get('speakerState') != 'assigned' or speaker_id is None:
    raise TranscriptionError('Only an assigned speaker can be confirmed')
  if segment.get('speakerConfirmed') == speaker_id:
    return state
  return _speaker_operation(state, {'kind': 'confirm', 'index': index, 'speakerId': speaker_id})


def reject_segment(state: TranscriptEditState, index: int) -> TranscriptEditState:
  current = state.payload
  if type(index) is not int or not 0 <= index < len(current['segments']):
    raise TranscriptionError('Choose an existing transcript segment')
  segment = current['segments'][index]
  if segment.get('speakerState') != 'assigned' or segment.get('speakerId') is None:
    raise TranscriptionError('Only an assigned speaker can be rejected')
  if segment.get('speakerRejected') == segment['speakerId']:
    return state
  return _speaker_operation(state, {'kind': 'reject', 'index': index, 'speakerId': segment['speakerId']})


def create_speaker_and_reassign(state: TranscriptEditState, index: int, name: str) -> TranscriptEditState:
  current = state.payload
  if type(index) is not int or not 0 <= index < len(current['segments']):
    raise TranscriptionError('Choose an existing transcript segment')
  if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
    raise TranscriptionError('Speaker name must be 1 to 60 characters')
  speaker = {'id': str(uuid.uuid4()), 'source': current['segments'][index]['source'],
             'name': name.strip(), 'label': f'Speaker {len(current["speakers"]) + 1}',
             'origin': 'manual', 'runtimeTag': None}
  return _speaker_operation(state, {'kind': 'create-speaker', 'index': index, 'speaker': speaker})

def undo_transcript_edit(state: TranscriptEditState) -> TranscriptEditState:
  return TranscriptEditState(state._baseline, state._operations, max(0, state._cursor - 1), state._ancestors | {state._fingerprint()})


def redo_transcript_edit(state: TranscriptEditState) -> TranscriptEditState:
  return TranscriptEditState(state._baseline, state._operations, min(len(state._operations), state._cursor + 1), state._ancestors | {state._fingerprint()})


def load_transcript_edit_state(session: Path) -> TranscriptEditState:
  try:
    with session_lock(session, shared=True):
      return _load_transcript_edit_state_locked(session)
  except OSError as error:
    raise TranscriptionError(f'Cannot open transcript: {error}') from error


def _load_transcript_edit_state_locked(session: Path) -> TranscriptEditState:
  manifest = _load_json(session / 'session.json')
  payload = validate_transcript_payload(_load_json(session / 'transcript.json'), manifest)
  history_path = session / 'transcript-edits.json'
  if not history_path.exists() and not history_path.is_symlink():
    return TranscriptEditState(_encoded(payload))
  try:
    history = _load_json(history_path)
    if type(history.get('schemaVersion')) is not int or history['schemaVersion'] != 1 or history.get('meetingId') != manifest['id']:
      raise ValueError('wrong schema or recording identity')
    baseline = validate_transcript_payload(history['baseline'], manifest)
    operations = history['operations']
    cursor = history['cursor']
    if not isinstance(operations, list) or type(cursor) is not int or not 0 <= cursor <= len(operations):
      raise ValueError('invalid history cursor or operations')
    current = copy.deepcopy(baseline)
    for operation in operations:
      if not isinstance(operation, dict):
        raise ValueError('invalid operation')
      if operation.get('kind') == 'replace':
        current = copy.deepcopy(validate_transcript_payload(operation['payload'], manifest))
      elif operation.get('kind') == 'text':
        index = operation['index']
        if type(index) is not int or not 0 <= index < len(current['segments']) or not isinstance(operation['text'], str):
          raise ValueError('invalid text operation')
        current['segments'][index]['text'] = operation['text']
      elif operation.get('kind') == 'rename':
        speaker = _registry_speaker(current, operation['speakerId'])
        name = operation['name']
        if name is None:
          speaker.pop('name', None)
        else:
          speaker['name'] = name
        _validate_v2(current, manifest['durationSeconds'])
      elif operation.get('kind') in ('reassign', 'confirm', 'reject', 'create-speaker'):
        index = operation['index']
        if type(index) is not int or not 0 <= index < len(current['segments']):
          raise ValueError('invalid speaker operation')
        segment = current['segments'][index]
        kind = operation['kind']
        if kind == 'create-speaker':
          speaker = operation['speaker']
          if not isinstance(speaker, dict) or speaker.get('origin') != 'manual' or speaker.get('source') != segment['source']:
            raise ValueError('invalid manual speaker operation')
          current['speakers'].append(copy.deepcopy(speaker))
          speaker_id = speaker['id']
        else:
          speaker_id = operation['speakerId']
        if speaker_id is not None:
          _registry_speaker(current, speaker_id, segment['source'])
        elif kind != 'confirm':
          raise ValueError('invalid speaker operation')
        if kind in ('reassign', 'create-speaker'):
          segment['speakerId'] = speaker_id
          segment['speakerState'] = 'assigned'
          segment.pop('speakerConfirmed', None)
          segment.pop('speakerRejected', None)
        elif kind == 'confirm':
          if speaker_id is None:
            segment.pop('speakerConfirmed', None)
          else:
            segment['speakerConfirmed'] = speaker_id
            segment.pop('speakerRejected', None)
        else:
          segment['speakerRejected'] = speaker_id
          segment.pop('speakerConfirmed', None)
        _validate_v2(current, manifest['durationSeconds'])
      else:
        raise ValueError('unknown operation')
    state = TranscriptEditState(_encoded(baseline), tuple(_encoded(op) for op in operations), cursor)
    if state.payload != payload:
      raise ValueError('history disagrees with saved transcript')
    return state
  except (KeyError, TypeError, ValueError, TranscriptionError) as error:
    raise TranscriptionError(f'Cannot load saved corrections in {history_path}: {error}. Preserve these files and restore a matching transcript/history backup.') from error


def _state_exports(state: TranscriptEditState) -> dict[str, str]:
  payload = state.payload
  exports = render_exports(payload)
  exports['transcript-edits.json'] = _encoded({'schemaVersion': 1, 'meetingId': payload['meetingId'],
    'baseline': json.loads(state._baseline), 'operations': [json.loads(op) for op in state._operations],
    'cursor': state._cursor}) + '\n'
  return exports


def save_transcript_edit_state(session: Path, state: TranscriptEditState) -> None:
  try:
    with session_lock(session):
      manifest = _load_json(session / 'session.json')
      validate_transcript_payload(state.payload, manifest)
      disk = _load_transcript_edit_state_locked(session)
      if disk._fingerprint() not in state._ancestors | {state._fingerprint()}:
        raise TranscriptionError('Transcript changed on disk; reopen this recording before editing')
      with tempfile.TemporaryDirectory(prefix='.edits-', dir=session) as directory:
        _publish_exports(session, Path(directory), _state_exports(state))
  except OSError as error:
    raise TranscriptionError(f'Cannot save corrections: {error}') from error

class TranscriptionError(Exception):
  pass


# Static local profiles; selecting a profile never changes capture devices.
from types import MappingProxyType


@dataclass(frozen=True)
class ProfileDefaults:
  label: str
  capacity: int
  model: Path
  runtime: Path
  diarizer: Path | None = None
  backend_python_script: Path | None = None


_ROOT = Path(__file__).resolve().parent.parent
_BUILD = _ROOT / '.build/linux'
PROFILE_DEFAULTS = MappingProxyType({
  'multitalker-parakeet': ProfileDefaults('Multitalker Parakeet (up to 4 speakers per source)', 4,
    _BUILD / 'models/parakeet-bundle.gguf', _BUILD / 'bin/parakeet-transcribe'),
  'nemotron-3.5': ProfileDefaults('Nemotron 3.5 + diarization (up to 8 speakers per source)', 8,
    _BUILD / 'models/nemotron-3.5/nemotron-3.5-asr-streaming-0.6b.nemo', _BUILD / 'nemotron-venv/bin/python',
    _BUILD / 'models/nemotron-diarization/Nemotron-3-Diarization.nemo', _ROOT / 'linux/nemotron-transcribe.py'),
})
DEFAULT_PROFILE = 'nemotron-3.5'
NEMOTRON_LOCALES = frozenset('en-US en-GB es-US es-ES fr-FR fr-CA it-IT pt-BR pt-PT nl-NL de-DE tr-TR ru-RU ar-AR hi-IN ja-JP ko-KR vi-VN uk-UA pl-PL sv-SE cs-CZ nb-NO da-DK bg-BG fi-FI hr-HR sk-SK zh-CN hu-HU ro-RO et-EE'.split())
LANGUAGES = NEMOTRON_LOCALES | {'en'}
LANES = {'me': ('mic.wav', 'Me'), 'them': ('system.wav', 'Them')}


def _options(profile, model, runtime, diarizer):
  if not isinstance(profile, str) or profile not in PROFILE_DEFAULTS:
    raise TranscriptionError(f'Unknown transcription profile: {profile}')
  defaults = PROFILE_DEFAULTS[profile]
  return defaults, Path(model or defaults.model), Path(runtime or defaults.runtime), Path(diarizer or defaults.diarizer) if (diarizer or defaults.diarizer) else None


def validate_options(*, profile, model=None, runtime=None, diarizer=None, language='en',
                     threads=4, use_gpu=True) -> None:
  defaults, model, runtime, diarizer = _options(profile, model, runtime, diarizer)
  allowed = {'en', 'en-US', 'en-GB'} if profile == 'multitalker-parakeet' else LANGUAGES
  if language not in allowed:
    raise TranscriptionError(f'{defaults.label}: unsupported language {language!r}; choose a supported locale (Parakeet is English-only)')
  if type(threads) is not int or threads < 1:
    raise TranscriptionError('threads must be a positive integer')
  if type(use_gpu) is not bool:
    raise TranscriptionError('use_gpu must be a boolean')
  for name, path in [('model', model), ('diarizer', diarizer)]:
    if path is not None and (not path.exists() or not os.access(path, os.R_OK) or (path.is_file() and path.stat().st_size == 0)):
      raise TranscriptionError(f'{defaults.label}: {name} missing, empty or unreadable: {path}; run linux/setup.sh')
  if not runtime.is_file() or not os.access(runtime, os.X_OK):
    raise TranscriptionError(f'{defaults.label}: runtime missing or not executable: {runtime}; run linux/setup.sh')
  if defaults.backend_python_script and not defaults.backend_python_script.is_file():
    raise TranscriptionError(f'Bridge script missing: {defaults.backend_python_script}')
  if profile == 'multitalker-parakeet' and diarizer is not None:
    raise TranscriptionError('Multitalker Parakeet uses its bundled four-speaker diarizer; an external diarizer cannot expand capacity')



def _load_json(path: Path) -> dict:
  try:
    if path.is_symlink():
      raise TranscriptionError(f'Expected a local JSON file, not a symlink: {path}')
    value = json.loads(path.read_text(encoding='utf-8'))
  except (OSError, ValueError) as error:
    raise TranscriptionError(f'Cannot read JSON {path}: {error}') from error
  if not isinstance(value, dict):
    raise TranscriptionError(f'Expected a JSON object: {path}')
  return value


def _duration(value: object, name: str) -> float:
  if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
    raise TranscriptionError(f'{name} must be a finite nonnegative number')
  return float(value)


def validate_session(session_dir: Path) -> tuple[dict, list[tuple[str, Path, float]]]:
  manifest = _load_json(session_dir / 'session.json')
  if type(manifest.get('schemaVersion')) is not int or manifest['schemaVersion'] != 1:
    raise TranscriptionError('Unsupported session schemaVersion (expected 1)')
  if manifest.get('status') != 'recorded':
    raise TranscriptionError('Only successfully recorded sessions can be transcribed')
  try:
    uuid.UUID(manifest['id'])
    started = datetime.datetime.fromisoformat(manifest['startedAt'].replace('Z', '+00:00'))
    if started.utcoffset() != datetime.timedelta(0):
      raise ValueError('startedAt must have a UTC offset')
  except (KeyError, ValueError, TypeError, AttributeError) as error:
    raise TranscriptionError('Session needs a UUID id and UTC startedAt') from error
  if not isinstance(manifest.get('title'), str):
    raise TranscriptionError('Session title must be a string')
  if type(manifest.get('sampleRate')) is not int or manifest['sampleRate'] != 16000 or type(manifest.get('channels')) is not int or manifest['channels'] != 1:
    raise TranscriptionError('Session audio must be 16000 Hz mono')
  duration = _duration(manifest.get('durationSeconds'), 'durationSeconds')
  tracks = manifest.get('tracks')
  if not isinstance(tracks, list) or len(tracks) != 2:
    raise TranscriptionError('Session must contain exactly two tracks: me and them')
  lanes = []
  seen = set()
  for track in tracks:
    if not isinstance(track, dict) or not isinstance(track.get('source'), str) or track['source'] not in LANES or track['source'] in seen:
      raise TranscriptionError('Session must contain separate me and them sources')
    source = track['source']
    seen.add(source)
    filename = LANES[source][0]
    if track.get('file') != filename:
      raise TranscriptionError(f'{source} track must use canonical file {filename}')
    if not isinstance(track.get('device'), str) or not track['device'].strip():
      raise TranscriptionError(f'{source} track needs a stable device name')
    frames = track.get('frames')
    if type(frames) is not int or frames <= 0:
      raise TranscriptionError(f'{source} track frames must be a positive integer')
    path = session_dir / filename
    if path.is_symlink():
      raise TranscriptionError(f'Track must be a local regular WAV, not a symlink: {path}')
    try:
      with wave.open(str(path), 'rb') as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) != (16000, 1, 2, 'NONE'):
          raise TranscriptionError(f'Track must be uncompressed 16-bit 16000 Hz mono WAV: {path}')
        if audio.getnframes() != frames:
          raise TranscriptionError(f'WAV frame count disagrees with manifest: {path}')
        remaining = frames
        while remaining:
          block = audio.readframes(min(remaining, 65536))
          if not block or len(block) % 2:
            raise TranscriptionError(f'Truncated WAV sample data: {path}')
          remaining -= len(block) // 2
    except (OSError, EOFError, wave.Error) as error:
      raise TranscriptionError(f'Cannot read track {path}: {error}') from error
    lane_duration = frames / 16000
    if abs(lane_duration - duration) > 1 / 16000 + 1e-9:
      raise TranscriptionError(f'Track duration disagrees with aligned session: {path}')
    lanes.append((source, path, lane_duration))
  return manifest, sorted(lanes)


def _split_aligned_turns(segment):
  words = segment.get('words', [])
  spans = [(match.start(), match.end()) for match in re.finditer(r'\S+', segment['text'])]
  if not words or spans != [(word['startChar'], word['endChar']) for word in words]:
    return [segment]
  # Native per-speaker streams can span a whole meeting; only genuine aligned pauses create new turns.
  groups = []
  for word in words:
    if not groups or word['startTime'] - groups[-1][-1]['endTime'] > 1:
      groups.append([])
    groups[-1].append(word)
  if len(groups) == 1:
    return [segment]
  result = []
  for group in groups:
    begin, end = group[0]['startChar'], group[-1]['endChar']
    result.append({**segment, 'text': segment['text'][begin:end],
      'startTime': group[0]['startTime'], 'endTime': group[-1]['endTime'],
      'words': [{**word, 'startChar': word['startChar'] - begin, 'endChar': word['endChar'] - begin} for word in group]})
  return result


def canonical_transcript(manifest, outputs, *, profile, language, backend):
  defaults = PROFILE_DEFAULTS[profile]
  if set(outputs) != set(LANES):
    raise TranscriptionError('Diarized transcription requires successful results for both audio sources')
  segments, turns, provenance = [], [], {}
  for source, output in outputs.items():
    if (not isinstance(output, dict) or type(output.get('schemaVersion')) is not int or output['schemaVersion'] != 1 or
        output.get('engine') != profile or type(output.get('speakerCapacity')) is not int or
        output['speakerCapacity'] != defaults.capacity):
      raise TranscriptionError(f'{source}: wrong runtime engine, schema or speaker capacity')
    duration = _duration(output.get('durationSeconds'), 'runtime duration')
    if abs(duration - manifest['durationSeconds']) > 1 / 16000 + 1e-9:
      raise TranscriptionError(f'{source}: runtime duration disagrees with source WAV')
    _validate_provenance(output.get('provenance'), backend)
    provenance[source] = copy.deepcopy(output['provenance'])
    if not isinstance(output.get('segments'), list) or not isinstance(output.get('speakerTurns'), list):
      raise TranscriptionError(f'{source}: runtime needs segments and genuine acoustic speakerTurns')
    lane_segments = []
    for raw in output['segments']:
      if not isinstance(raw, dict):
        raise TranscriptionError(f'{source}: malformed runtime segment')
      segment = {name: copy.deepcopy(raw[name]) for name in ('startTime', 'endTime', 'text', 'words') if name in raw}
      segment.update(source=source, speakerLabel=LANES[source][1])
      lane_segments.append(segment)
      segment['_tag'] = _runtime_tag(raw.get('speakerTag'), defaults.capacity)
    validate_transcript_payload({'schemaVersion': 1, 'meetingId': manifest['id'], 'title': manifest['title'], 'segments': lane_segments}, manifest)
    segments.extend(turn for segment in lane_segments for turn in _split_aligned_turns(segment))
    for raw in output['speakerTurns']:
      if not isinstance(raw, dict):
        raise TranscriptionError(f'{source}: malformed acoustic turn')
      tag = _runtime_tag(raw.get('speakerTag'), defaults.capacity)
      if tag is None:
        raise TranscriptionError('Acoustic turns require an assigned runtime speaker')
      start, end = _duration(raw.get('startTime'), 'turn start'), _duration(raw.get('endTime'), 'turn end')
      if not start < end <= duration + 1 / 16000:
        raise TranscriptionError('Invalid runtime acoustic turn timing')
      turns.append({'source': source, 'startTime': start, 'endTime': end, '_tag': tag})
    if lane_segments and not output['speakerTurns']:
      raise TranscriptionError(f'{source}: ASR-only output is not successful diarization')
  segments = merge_segments(segments)
  turns = merge_segments(turns)
  registry, speakers = {}, []
  events = sorted(segments + turns, key=lambda entry: (entry['startTime'], entry['source'], entry['_tag'] or 0))
  for entry in events:
    tag = entry['_tag']
    if tag is not None and (entry['source'], tag) not in registry:
      speaker = {'id': str(uuid.uuid4()), 'source': entry['source'], 'runtimeTag': tag, 'label': f'Speaker {len(speakers) + 1}'}
      registry[(entry['source'], tag)] = speaker['id']
      speakers.append(speaker)
  for segment in segments:
    tag = segment.pop('_tag')
    segment.pop('speakerLabel')
    segment['speakerId'] = registry.get((segment['source'], tag))
    segment['speakerState'] = 'assigned' if tag is not None else 'unknown'
    for word in segment.get('words', []):
      word.update(source=segment['source'], speakerId=segment['speakerId'], speakerState=segment['speakerState'])
  for turn in turns:
    turn['speakerId'] = registry[(turn['source'], turn.pop('_tag'))]
  transcript = {'schemaVersion': 2, 'meetingId': manifest['id'], 'title': manifest['title'],
    'generation': {'id': str(uuid.uuid4()), 'profile': profile, 'language': language, 'backend': backend, 'provenance': provenance},
    'speakers': speakers, 'segments': segments, 'speakerTurns': turns}
  return validate_transcript_payload(transcript, manifest)


def _runtime_tag(tag, capacity):
  if tag is not None and (type(tag) is not int or not 1 <= tag <= capacity):
    raise TranscriptionError(f'Runtime speaker tag exceeds its honest {capacity}-speaker capacity')
  return tag



def merge_segments(*lanes: list[dict]) -> list[dict]:
  return sorted((segment for lane in lanes for segment in lane),
                key=lambda segment: (segment['startTime'], segment['endTime'], segment['source']))


def srt_timestamp(seconds: float) -> str:
  milliseconds = round(_duration(seconds, 'timestamp') * 1000)
  hours, remainder = divmod(milliseconds, 3600000)
  minutes, remainder = divmod(remainder, 60000)
  seconds, milliseconds = divmod(remainder, 1000)
  return f'{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}'


def speaker_label(payload: dict, segment: dict, *, include_source: bool = False) -> str:
  if payload['schemaVersion'] == 1:
    return segment['speakerLabel']
  speaker = next((entry for entry in payload['speakers'] if entry['id'] == segment['speakerId']), None)
  label = ((speaker or {}).get('name') or (speaker or {}).get('label') if speaker else
           ('Unknown speaker' if segment['speakerState'] == 'unknown' else 'Speaker unavailable'))
  return f'{label} ({LANES[segment["source"]][1]})' if include_source else label


def render_exports(transcript: dict) -> dict[str, str]:
  text_lines = []
  subtitles = []
  for index, segment in enumerate(transcript['segments'], 1):
    minutes, seconds = divmod(int(segment['startTime']), 60)
    text = segment['text']
    label = speaker_label(transcript, segment, include_source=True)
    text_lines.append(f'[{minutes:02}:{seconds:02}] {label}: {text}\n')
    subtitles.append(f'{index}\n{srt_timestamp(segment["startTime"])} --> '
                     f'{srt_timestamp(segment["endTime"])}\n{label}: {text}\n\n')
  return {'transcript.json': json.dumps(transcript, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
          'transcript.txt': ''.join(text_lines), 'transcript.srt': ''.join(subtitles)}


def _publish_exports(session_dir: Path, temporary: Path, exports: dict[str, str], *, cancelled=None) -> None:
  # Cancellation is delivered before staging or after the complete transaction,
  # never between canonical exports and their correction history.
  blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
  try:
    _publish_exports_locked(session_dir, temporary, exports, cancelled=cancelled)
  finally:
    signal.pthread_sigmask(signal.SIG_SETMASK, blocked)


def _publish_exports_locked(session_dir: Path, temporary: Path, exports: dict[str, str], *, cancelled=None) -> None:
  # Stage every file before replacing any existing export. Each rename is atomic.
  previous = {}
  for name, content in exports.items():
    destination = session_dir / name
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
      raise TranscriptionError(f'Export destination is not a regular file: {destination}')
    previous[name] = destination.read_bytes() if destination.exists() else None
    descriptor = os.open(temporary / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
      output.write(content)
      output.flush()
      os.fsync(output.fileno())
  published = []
  try:
    for name in exports:
      if (cancelled is not None and cancelled()) or signal.sigpending() & {signal.SIGINT, signal.SIGTERM}:
        raise OSError('Transcription cancelled before publication completed')
      os.replace(temporary / name, session_dir / name)
      published.append(name)
    if (cancelled is not None and cancelled()) or signal.sigpending() & {signal.SIGINT, signal.SIGTERM}:
      raise OSError('Transcription cancelled before publication completed')
  except OSError:
    for name in published:
      if previous[name] is None:
        (session_dir / name).unlink()
      else:
        backup = temporary / f'{name}.previous'
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as output:
          output.write(previous[name])
        os.replace(backup, session_dir / name)
    raise


def transcribe_session(session_dir: Path, *, profile, model=None, runtime=None, diarizer=None,
                       language='en', threads=4, use_gpu=True, runtime_executor=None, cancelled=None) -> dict:
  with session_lock(session_dir):
    return _transcribe_session_locked(session_dir, profile=profile, model=model, runtime=runtime,
      diarizer=diarizer, language=language, threads=threads, use_gpu=use_gpu,
      runtime_executor=runtime_executor, cancelled=cancelled)


def _transcribe_session_locked(session_dir: Path, *, profile, model, runtime, diarizer,
                               language, threads, use_gpu, runtime_executor=None, cancelled=None) -> dict:
  try:
    validate_options(profile=profile, model=model, runtime=runtime, diarizer=diarizer,
      language=language, threads=threads, use_gpu=use_gpu)
    defaults, model, runtime, diarizer = _options(profile, model, runtime, diarizer)
    manifest, lanes = validate_session(session_dir)
    previous = _load_transcript_edit_state_locked(session_dir) if (session_dir / 'transcript.json').exists() else None
    if previous is None and (session_dir / 'transcript-edits.json').exists():
      raise TranscriptionError('Saved correction history has no matching transcript; restore the matching backup')
    backend = 'gpu' if use_gpu else 'cpu'
    with tempfile.TemporaryDirectory(prefix='.transcribe-', dir=session_dir) as directory:
      temporary = Path(directory)
      outputs = {}
      for source, audio, duration in lanes:
        output = temporary / f'{source}.json'
        # Resolving a venv Python symlink bypasses its pyvenv.cfg and installed dependencies.
        command = [str(runtime.absolute())]
        if defaults.backend_python_script:
          command.append(str(defaults.backend_python_script))
        command += ['--audio', str(audio.resolve()), '--model', str(model.resolve()),
          '--output', str(output), '--language', language, '--threads', str(threads), '--backend', backend]
        if diarizer is not None:
          command += ['--diarizer', str(diarizer.resolve())]
        process = (runtime_executor(command) if runtime_executor is not None else
          subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        if process.returncode:
          raise TranscriptionError(f'{source}: {profile} runtime exited {process.returncode}: {process.stderr.strip()}')
        outputs[source] = _load_json(output)
      transcript = canonical_transcript(manifest, outputs, profile=profile, language=language, backend=backend)
      if previous is None:
        state = TranscriptEditState(_encoded(transcript))
      else:
        operations = previous._operations[:previous._cursor] + (_encoded({'kind': 'replace', 'payload': transcript}),)
        state = TranscriptEditState(previous._baseline, operations, len(operations))
      _publish_exports(session_dir, temporary, _state_exports(state), cancelled=cancelled)
      return transcript
  except (OSError, ValueError) as error:
    raise TranscriptionError(f'Transcription failed: {error}') from error

