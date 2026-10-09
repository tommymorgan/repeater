"""Local opt-in voice learning: profiles, enrollment, tiered matching.

Consent-first and local-only: samples come exclusively from speaker confirmations
(✓) in the transcript UI, profiles live in one JSON file on this computer, and
Wrong on an auto-match only increments an inert per-profile counter — the future
training signal, deliberately not acted on. Thresholds are first-cut anchors from
the 2026-10-08 real-meeting calibration (TitaNet, turn-level FAR@0.60 = 0/1914).
"""

import json
import math
import os
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

STRICTNESS_PRESETS = {'strict': 0.65, 'balanced': 0.60, 'relaxed': 0.50}
SUGGEST_FLOOR = 0.40
LANE_WAV = {'me': 'mic.wav', 'them': 'system.wav'}
_EMBED_VENV = Path(__file__).resolve().parent.parent / '.build/linux/nemotron-venv/bin/python'
_EMBED_MODEL = Path(__file__).resolve().parent.parent / '.build/linux/models/titanet-embed/speakerverification_en_titanet_large.nemo'
_EMBED_SCRIPT = Path(__file__).resolve().parent / 'voice_embed.py'
MAX_NAME = 60


class VoiceError(Exception):
  pass


def _normalize(vector):
  magnitude = math.sqrt(sum(value * value for value in vector))
  if magnitude <= 0:
    raise VoiceError('Empty speaker embedding')
  return [value / magnitude for value in vector]


def centroid(vectors):
  """Mean of normalized embeddings, re-normalized (benchmark enrollment recipe)."""
  if not vectors:
    raise VoiceError('A voice profile needs at least one sample')
  summed = [0.0] * len(vectors[0])
  for vector in vectors:
    summed = [total + value for total, value in zip(summed, vector)]
  return _normalize(summed)


def cosine(a, b):
  return sum(x * y for x, y in zip(a, b))


def windows_for_segment(segment, duration, length=4.0, per_segment=2):
  """Speech windows (file-relative seconds) inside one transcript segment."""
  span = min(duration, segment['endTime']) - segment['startTime']
  if span < length:
    return []
  if span >= 2 * length:
    starts = [segment['startTime'], segment['endTime'] - length]
  else:
    starts = [segment['startTime'] + (span - length) / 2]
  return [{'source': segment['source'], 'start': round(start, 3), 'length': length}
          for start in starts[:per_segment]]


class VoiceProfile:
  def __init__(self, name, source, profile_id=None, embeddings=None, samples=None,
               wrong_auto=0, created_at=''):
    self.id = profile_id or str(uuid.uuid4())
    self.name = name
    self.source = source
    self.embeddings = embeddings or []
    self.samples = samples or []  # {meetingId, segmentIndex, source, start, active}
    self.wrong_auto = wrong_auto
    self.created_at = created_at

  def active_embeddings(self):
    return [vector for vector, sample in zip(self.embeddings, self.samples) if sample.get('active', True)]

  def to_json(self):
    return {'id': self.id, 'name': self.name, 'source': self.source,
            'embeddings': self.embeddings, 'samples': self.samples,
            'wrongAuto': self.wrong_auto, 'createdAt': self.created_at}

  @staticmethod
  def from_json(entry):
    if not isinstance(entry, dict) or not isinstance(entry.get('name'), str) or not entry['name'].strip():
      raise VoiceError('Invalid voice profile entry')
    profile = VoiceProfile(entry['name'], entry.get('source', 'me'), entry.get('id'),
                           entry.get('embeddings', []), entry.get('samples', []),
                           entry.get('wrongAuto', 0), entry.get('createdAt', ''))
    for vector in profile.embeddings:
      if not isinstance(vector, list) or len(vector) != 192 or not all(isinstance(v, (int, float)) for v in vector):
        raise VoiceError('Voice profile embeddings must be 192-dimension numbers')
    return profile


class VoiceStore:
  """One JSON file, one writer at a time, no network, no derived state on disk."""

  def __init__(self, path: Path):
    self.path = Path(path)
    self._lock = threading.Lock()
    self._data = {'schemaVersion': 1, 'master': False, 'strictness': 'balanced', 'profiles': []}
    self.load()

  def load(self):
    with self._lock:
      if self.path.exists():
        try:
          raw = json.loads(self.path.read_text())
        except (OSError, ValueError) as error:
          raise VoiceError(f'Cannot read voice profiles ({self.path}): {error}') from error
        if raw.get('schemaVersion') != 1 or not isinstance(raw.get('profiles'), list):
          raise VoiceError('Unsupported voice profile store')
        if raw.get('strictness') not in STRICTNESS_PRESETS:
          raise VoiceError('Unknown auto-match strictness')
        self._data = {'schemaVersion': 1, 'master': bool(raw['master']),
                      'strictness': raw['strictness'],
                      'profiles': [VoiceProfile.from_json(entry) for entry in raw['profiles']]}
      return self

  def save(self):
    with self._lock:
      self.path.parent.mkdir(parents=True, exist_ok=True)
      body = json.dumps({'schemaVersion': 1, 'master': self._data['master'],
                         'strictness': self._data['strictness'],
                         'profiles': [p.to_json() for p in self._data['profiles']]},
                        ensure_ascii=False, indent=1)
      handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.voices-', suffix='.json')
      try:
        with os.fdopen(handle, 'w') as stream:
          stream.write(body)
          stream.flush()
          os.fsync(stream.fileno())
        os.replace(temporary, self.path)
      finally:
        if os.path.exists(temporary):
          os.unlink(temporary)
      return self

  # ---- settings ----
  @property
  def master(self):
    return self._data['master']

  def set_master(self, enabled: bool):
    with self._lock:
      self._data['master'] = bool(enabled)
    return self.save()

  @property
  def strictness(self):
    return self._data['strictness']


  def set_strictness(self, preset: str):
    if preset not in STRICTNESS_PRESETS:
      raise VoiceError('Unknown auto-match strictness')
    with self._lock:
      self._data['strictness'] = preset
    return self.save()

  @property
  def profiles(self):
    with self._lock:
      return list(self._data['profiles'])

  def profile(self, profile_id):
    with self._lock:
      return next((p for p in self._data['profiles'] if p.id == profile_id), None)

  def profile_for_name(self, name, source):
    with self._lock:
      return next((p for p in self._data['profiles'] if p.name == name and p.source == source), None)

  # ---- enrollment and deletion ----

  def enroll(self, name, source, meeting_id, segment_index, vectors):
    """Add confirmed samples; creating the profile turns master on (explicit opt-in)."""
    if not name or len(name) > MAX_NAME:
      raise VoiceError('Voice profile name is missing or too long')
    normalized = [_normalize(vector) for vector in vectors]
    if not normalized:
      raise VoiceError('Enrollment needs at least one confirmed snippet')
    with self._lock:
      profile = next((p for p in self._data['profiles'] if p.name == name and p.source == source), None)
      created = profile is None
      if created:
        profile = VoiceProfile(name, source)
        self._data['profiles'].append(profile)
      for vector in normalized:
        profile.embeddings.append(vector)
        profile.samples.append({'meetingId': meeting_id, 'segmentIndex': segment_index,
                                'source': source, 'start': 0.0, 'active': True})
      if created:
        self._data['master'] = True
    self.save()
    return profile.id

  def forget(self, profile_id):
    with self._lock:
      self._data['profiles'] = [p for p in self._data['profiles'] if p.id != profile_id]
    return self.save()

  def record_wrong_auto(self, profile_id):
    """Inert counter: the recorded training signal for deferred relearning."""
    with self._lock:
      profile = next((p for p in self._data['profiles'] if p.id == profile_id), None)
      if profile:
        profile.wrong_auto += 1
    return self.save()

  def set_meeting_active(self, meeting_id, active: bool, purge=False):
    with self._lock:
      for profile in self._data['profiles']:
        if purge:
          keep = [(v, s) for v, s in zip(profile.embeddings, profile.samples) if s['meetingId'] != meeting_id]
          profile.embeddings, profile.samples = [v for v, _ in keep], [s for _, s in keep]
        else:
          profile.samples = [{**s, 'active': bool(active)} if s['meetingId'] == meeting_id else s
                             for s in profile.samples]
      self._data['profiles'] = [p for p in self._data['profiles'] if p.samples or p.wrong_auto or p.id]
    return self.save()

  # ---- matching ----

  def match(self, vector, master=None):
    """Tiered match of one embedding against active enrollment centroids.

    Returns (profile, score, tier) with tier in {'auto', 'suggest', None};
    matching is refused unless the master switch is on.
    """
    if master is None:
      master = self.master
    if not master:
      return None, 0.0, None
    probe = _normalize(vector)
    cutoff = STRICTNESS_PRESETS[self.strictness]
    best, best_score = None, -1.0
    with self._lock:
      for profile in self._data['profiles']:
        active = profile.active_embeddings()
        if not active:
          continue
        score = cosine(probe, centroid(active))
        if score > best_score:
          best, best_score = profile, score
    if best is None or best_score < SUGGEST_FLOOR:
      return None, best_score if best else 0.0, None
    return best, best_score, 'auto' if best_score >= cutoff else 'suggest'


def embed_windows(meeting_dir: Path, windows, python=_EMBED_VENV, model=_EMBED_MODEL,
                 script=_EMBED_SCRIPT, timeout=300):
  """Run the pinned TitaNet embedder over {source,start,length} windows; returns unit vectors."""
  if not windows:
    return []
  for path in (python, script, model):
    if not Path(path).is_file():
      raise VoiceError(f'Voice embedding runtime missing: {path}; run linux/setup.sh')
  request = {'model': str(model), 'windows': [
    {'wav': str(Path(meeting_dir) / LANE_WAV[window['source']]),
     'start': float(window['start']), 'length': float(window['length'])} for window in windows]}
  with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as handle:
    json.dump(request, handle)
    request_path = handle.name
  try:
    completed = subprocess.run([str(python), str(script), request_path],
                               capture_output=True, text=True, timeout=timeout)
    response_path = request_path + '.out'
    if completed.returncode != 0 or not Path(response_path).is_file():
      raise VoiceError(f'Voice embedding failed: {completed.stderr.strip()[-400:]}')
    try:
      response = json.loads(Path(response_path).read_text())
      embeddings = response['embeddings']
    except (OSError, ValueError, KeyError) as error:
      raise VoiceError(f'Voice embedding returned invalid output: {error}') from error
  finally:
    for stale in (request_path, request_path + '.out'):
      if os.path.exists(stale):
        os.unlink(stale)
  return [_normalize(vector) for vector in embeddings]
