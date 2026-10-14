"""UI-independent desktop jobs; only explicit actions launch the local CLI."""

from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable
import uuid

import parrot
from transcribe import (PROFILE_DEFAULTS, DEFAULT_PROFILE, TranscriptionError, render_exports, validate_options,
                        session_lock, _publish_exports, load_transcript_edit_state, transcribe_session)
from inference import ModelRuntime, RuntimeCancelled
from urllib.parse import quote
from review import DEFAULT_REVIEW_ENDPOINT, DEFAULT_REVIEW_MODEL


class DesktopError(Exception):
  pass


@dataclass(frozen=True)
class Preferences:
  microphone: str | None = None
  system: str | None = None
  profile: str | None = DEFAULT_PROFILE
  parakeet_model: Path = PROFILE_DEFAULTS['multitalker-parakeet'].model
  parakeet_runtime: Path = PROFILE_DEFAULTS['multitalker-parakeet'].runtime
  nemotron_model: Path = PROFILE_DEFAULTS['nemotron-3.5'].model
  nemotron_runtime: Path = PROFILE_DEFAULTS['nemotron-3.5'].runtime
  nemotron_diarizer: Path = PROFILE_DEFAULTS['nemotron-3.5'].diarizer
  language: str = 'en'
  threads: int = 4
  use_gpu: bool = True
  auto_transcribe: bool = True
  review_endpoint: str = DEFAULT_REVIEW_ENDPOINT
  review_model: str = DEFAULT_REVIEW_MODEL


@dataclass(frozen=True)
class MeetingInfo:
  path: Path
  title: str
  started_at: str
  status: str
  duration_seconds: float
  has_transcript: bool
  error: str | None = None
  archived: bool = False


@dataclass(frozen=True)
class TrashEntry:
  original_path: Path
  trashed_path: Path
  info_path: Path


def _safe_recording(session: Path) -> dict:
  root = meetings_root()
  if not session.is_absolute() or session.parent != root or session.resolve() != session or session.is_symlink() or not session.is_dir():
    raise DesktopError('Choose a local recording directly inside the recordings folder.')
  for directory, folders, files in os.walk(session, followlinks=False):
    for name in folders + files:
      if (Path(directory) / name).is_symlink():
        raise DesktopError('Recording contains a symlink; nothing was moved or changed.')
  manifest = _json(session / 'session.json')
  if manifest.get('status') == 'recording':
    raise DesktopError('Stop the recording before moving or archiving it.')
  if type(manifest.get('archived', False)) is not bool:
    raise DesktopError('Session archived must be a boolean.')
  return manifest


def set_archived(session: Path, archived: bool) -> None:
  if type(archived) is not bool:
    raise DesktopError('Archived must be a boolean.')
  try:
    with session_lock(session):
      manifest = _safe_recording(session)
      manifest['archived'] = archived
      with tempfile.TemporaryDirectory(prefix='.archive-', dir=session) as temporary:
        _publish_exports(session, Path(temporary), {'session.json': json.dumps(manifest, indent=2, allow_nan=False) + '\n'})
  except (OSError, TranscriptionError) as error:
    raise DesktopError(f'Cannot change collection: {error}') from error


def _trash_root() -> Path:
  configured = os.environ.get('XDG_DATA_HOME')
  base = Path(configured).expanduser() if configured else Path.home() / '.local/share'
  if not base.is_absolute() or base.resolve() != base:
    raise DesktopError('Trash needs an absolute local XDG_DATA_HOME without symlinks.')
  root = base / 'Trash'
  for directory in (base, root, root / 'files', root / 'info'):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir() or directory.stat().st_uid != os.getuid():
      raise DesktopError(f'Unsafe Trash directory: {directory}')
    if directory != base and directory.stat().st_mode & 0o077:
      raise DesktopError(f'Trash directory must be private (0700): {directory}')
  return root


def _rename_without_replace(source: Path, destination: Path) -> None:
  # Linux renameat2 refuses collisions atomically, including empty directories.
  import ctypes
  library = ctypes.CDLL(None, use_errno=True)
  rename = library.renameat2
  rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
  if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
    code = ctypes.get_errno()
    raise OSError(code, os.strerror(code), str(destination))


def trash_meeting(session: Path) -> TrashEntry:
  try:
    with session_lock(session):
      _safe_recording(session)
      root = _trash_root()
      if session.stat().st_dev != (root / 'files').stat().st_dev:
        raise DesktopError('Trash is on another filesystem; recording was left untouched. Move it with your file manager.')
      name = f'{session.name}-{uuid.uuid4()}'
      entry = TrashEntry(session, root / 'files' / name, root / 'info' / f'{name}.trashinfo')
      info = '[Trash Info]\nPath=' + quote(str(session), safe='/') + '\nDeletionDate=' + datetime.now().strftime('%Y-%m-%dT%H:%M:%S') + '\n'
      descriptor = os.open(entry.info_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
      try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
          output.write(info)
          output.flush()
          os.fsync(output.fileno())
        _rename_without_replace(session, entry.trashed_path)
      except BaseException:
        entry.info_path.unlink()
        raise
      return entry
  except (OSError, TranscriptionError) as error:
    raise DesktopError(f'Cannot move recording to Trash: {error}') from error


def restore_trashed_meeting(entry: TrashEntry) -> Path:
  try:
    root = _trash_root()
    if entry.original_path.parent != meetings_root() or entry.original_path.parent.resolve() != entry.original_path.parent:
      raise DesktopError('Original recording location is unsafe.')
    if entry.trashed_path.parent != root / 'files' or entry.info_path != root / 'info' / f'{entry.trashed_path.name}.trashinfo':
      raise DesktopError('This is not a local recording Trash entry.')
    if entry.trashed_path.resolve() != entry.trashed_path or entry.info_path.is_symlink():
      raise DesktopError('Trash entry contains an unsafe symlink.')
    expected = 'Path=' + quote(str(entry.original_path), safe='/')
    if expected not in entry.info_path.read_text(encoding='utf-8').splitlines():
      raise DesktopError('Trash metadata does not match the original recording.')
    with session_lock(entry.trashed_path):
      for directory, folders, files in os.walk(entry.trashed_path, followlinks=False):
        for name in folders + files:
          if (Path(directory) / name).is_symlink():
            raise DesktopError('Trash entry contains an unsafe symlink; nothing was restored.')
      _rename_without_replace(entry.trashed_path, entry.original_path)
      try:
        entry.info_path.unlink()
      except OSError:
        _rename_without_replace(entry.original_path, entry.trashed_path)
        raise
    return entry.original_path
  except (OSError, TranscriptionError) as error:
    raise DesktopError(f'Cannot restore recording; original location must be free: {error}') from error


@dataclass(frozen=True)
class JobSnapshot:
  state: str = 'idle'
  session: Path | None = None
  error: str | None = None
  started_at: float | None = None
  transcription_state: str = 'idle'
  transcription_session: Path | None = None
  transcription_error: str | None = None
  queued_sessions: tuple[Path, ...] = ()


def meetings_root() -> Path:
  try:
    return parrot.meetings_root()
  except (ValueError, OSError) as error:
    raise DesktopError(str(error)) from error


def _settings_path() -> Path:
  configured = os.environ.get('XDG_CONFIG_HOME')
  base = Path(configured).expanduser() if configured else Path.home() / '.config'
  if not base.is_absolute():
    raise DesktopError('XDG_CONFIG_HOME must be an absolute path.')
  return base / 'parrot/settings.json'


PREFERENCE_PATHS = ('parakeet_model', 'parakeet_runtime', 'nemotron_model', 'nemotron_runtime', 'nemotron_diarizer')


def inference_options(prefs: Preferences) -> dict:
  if prefs.profile is None:
    raise DesktopError('Choose a transcription model in Settings; old Whisper settings cannot select a new model.')
  prefix = 'parakeet' if prefs.profile == 'multitalker-parakeet' else 'nemotron'
  return {'profile': prefs.profile, 'model': getattr(prefs, f'{prefix}_model').expanduser(),
          'runtime': getattr(prefs, f'{prefix}_runtime').expanduser(),
          'diarizer': prefs.nemotron_diarizer.expanduser() if prefix == 'nemotron' else None,
          'language': prefs.language, 'threads': prefs.threads, 'use_gpu': prefs.use_gpu}


def _validate_preferences(prefs: Preferences) -> None:
  if prefs.profile is not None and (not isinstance(prefs.profile, str) or prefs.profile not in PROFILE_DEFAULTS):
    raise DesktopError('Choose a supported transcription model.')
  for name in ('microphone', 'system'):
    value = getattr(prefs, name)
    if value is not None and (not isinstance(value, str) or not value.strip() or '\x00' in value):
      raise DesktopError(f'{name} must be a stable device name or None.')
  for name in PREFERENCE_PATHS:
    value = getattr(prefs, name)
    if not isinstance(value, Path) or not value.expanduser().is_absolute() or '\x00' in str(value):
      raise DesktopError(f'{name} must be an absolute local path.')
  if not isinstance(prefs.language, str) or not prefs.language.strip() or '\x00' in prefs.language:
    raise DesktopError('Enter a language locale; model support is checked before transcription.')
  if type(prefs.threads) is not int or prefs.threads < 1:
    raise DesktopError('CPU threads must be a positive integer.')
  if type(prefs.use_gpu) is not bool or type(prefs.auto_transcribe) is not bool:
    raise DesktopError('GPU and automatic transcription settings must be booleans.')
  for name in ('review_endpoint', 'review_model'):
    value = getattr(prefs, name)
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
      raise DesktopError(f'{name} must be a nonempty string.')


def load_preferences() -> Preferences:
  path = _settings_path()
  try:
    payload = json.loads(path.read_text(encoding='utf-8'))
  except FileNotFoundError:
    return Preferences()
  except (OSError, ValueError) as error:
    raise DesktopError(f'Cannot read settings {path}: {error}. Fix or remove that file.') from error
  legacy = {'model', 'whisper_cli'} if isinstance(payload, dict) and 'profile' not in payload else set()
  if not isinstance(payload, dict) or set(payload) - (set(Preferences.__dataclass_fields__) | legacy):
    raise DesktopError(f'Invalid settings object: {path}. Fix or remove that file.')
  if 'profile' not in payload:
    payload = {key: value for key, value in payload.items() if key not in legacy}
    payload['profile'] = None
  try:
    for name in PREFERENCE_PATHS:
      if name in payload:
        if not isinstance(payload[name], str) or not payload[name]:
          raise ValueError(f'{name} must be a path string')
        payload[name] = Path(payload[name])
    prefs = Preferences(**payload)
    _validate_preferences(prefs)
    return prefs
  except (TypeError, ValueError, DesktopError) as error:
    raise DesktopError(f'Invalid settings {path}: {error}. Fix or remove that file.') from error


def save_preferences(prefs: Preferences) -> None:
  _validate_preferences(prefs)
  path = _settings_path()
  temporary = None
  try:
    missing = []
    directory = path.parent
    while not directory.exists():
      missing.append(directory)
      directory = directory.parent
    for directory in reversed(missing):
      directory.mkdir(mode=0o700, exist_ok=True)
    payload = asdict(prefs)
    for name in PREFERENCE_PATHS:
      payload[name] = str(payload[name].expanduser())
    fd, temporary = tempfile.mkstemp(prefix='.settings-', dir=path.parent)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
      json.dump(payload, handle, indent=2, allow_nan=False)
      handle.write('\n')
      handle.flush()
      os.fsync(handle.fileno())
    os.replace(temporary, path)
  except OSError as error:
    raise DesktopError(f'Cannot save settings {path}: {error}') from error
  finally:
    if temporary and os.path.exists(temporary):
      os.unlink(temporary)


def settings_ready(prefs: Preferences) -> None:
  _validate_preferences(prefs)
  try:
    validate_options(**inference_options(prefs))
  except (OSError, TranscriptionError) as error:
    raise DesktopError(str(error)) from error


def _json(path: Path) -> dict:
  try:
    if path.is_symlink():
      raise DesktopError(f'Expected a local JSON file, not a symlink: {path}')
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
      raise DesktopError(f'Expected a JSON object: {path}')
    return payload
  except (OSError, ValueError) as error:
    raise DesktopError(f'Cannot read {path}: {error}') from error


def _number(value: object, field: str) -> float:
  try:
    valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
  except OverflowError:
    valid = False
  if not valid:
    raise DesktopError(f'{field} must be a finite nonnegative number.')
  return float(value)


def read_transcript_payload(session: Path) -> dict:
  try:
    return load_transcript_edit_state(session).payload
  except TranscriptionError as error:
    raise DesktopError(str(error)) from error


def read_transcript(session: Path) -> str:
  return render_exports(read_transcript_payload(session))['transcript.txt']


def list_meetings() -> list[MeetingInfo]:
  root = meetings_root()
  try:
    paths = sorted(root.iterdir(), reverse=True)
  except OSError as error:
    raise DesktopError(f'Cannot list meetings in {root}: {error}') from error
  meetings = []
  for path in paths:
    if path.is_symlink() or not path.is_dir() or path.name.startswith('.'):
      continue
    title, started, status, duration, error = path.name, '', 'failed', 0.0, None
    archived = False
    try:
      manifest = _json(path / 'session.json')
      archived = manifest.get('archived', False)
      if type(archived) is not bool:
        raise DesktopError('Session archived must be a boolean.')
      if isinstance(manifest.get('title'), str):
        title = manifest['title']
      if isinstance(manifest.get('startedAt'), str):
        started = manifest['startedAt']
      if type(manifest.get('schemaVersion')) is not int or manifest['schemaVersion'] != 1:
        raise DesktopError('Unsupported session schemaVersion (expected 1).')
      uuid.UUID(manifest['id'])
      date = datetime.fromisoformat(started.replace('Z', '+00:00'))
      if date.utcoffset() != timezone.utc.utcoffset(date):
        raise ValueError('startedAt must be UTC')
      if not isinstance(manifest.get('title'), str):
        raise DesktopError('Meeting title must be a string.')
      duration = _number(manifest.get('durationSeconds'), 'durationSeconds')
      status = manifest.get('status')
      if status not in ('recorded', 'recording', 'failed'):
        raise DesktopError('Unknown recording status.')
      if status == 'failed':
        error = 'Recording failed; partial audio is preserved. Open the folder to inspect it.'
      elif status == 'recording':
        error = 'Unfinished recording on disk; not an active job in this window.'
    except (DesktopError, ValueError, KeyError, TypeError, AttributeError) as caught:
      status, error = 'failed', f'Cannot load session: {caught}. Open the folder to inspect session.json.'
    has_transcript = False
    if (path / 'transcript.json').exists():
      try:
        read_transcript(path)
        has_transcript = True
      except DesktopError as caught:
        if isinstance(caught.__cause__, TranscriptionError) and isinstance(caught.__cause__.__cause__, BlockingIOError):
          has_transcript = True
        else:
          error = f'{error + " " if error else ""}Transcript error: {caught}'
    meetings.append(MeetingInfo(path, title, started, status, duration, has_transcript, error, archived))
  return sorted(meetings, key=lambda item: (item.started_at, item.path.name), reverse=True)


class DesktopController:
  """Independent capture and serial ASR lanes. Observers must not block.

  Recording receives SIGINT on its CLI parent, so capture finalizes its children.
  Escalation and transcription cancellation target only this controller's group.
  """

  def __init__(self, on_change: Callable[[JobSnapshot], None]):
    self._on_change = on_change
    self._lock = threading.RLock()
    self._snapshot = JobSnapshot()
    self._shutdown = False
    self._stop = threading.Event()
    self._process: subprocess.Popen | None = None
    self._condition = threading.Condition(self._lock)
    self._queue: deque[tuple[Path, Preferences]] = deque()
    self._recording_thread = None
    self._thread = None
    self._runtime = ModelRuntime()

  @property
  def snapshot(self) -> JobSnapshot:
    with self._lock:
      return self._snapshot

  def _update(self, **changes) -> None:
    with self._lock:
      self._snapshot = replace(self._snapshot, **changes)
      try:
        self._on_change(self._snapshot)
      except Exception:
        # Observer failures must never abandon an owned recording process.
        pass

  def _publish(self, state: str, *, session: Path | None = None,
               error: str | None = None, started_at: float | None = None) -> None:
    self._update(state=state, session=session, error=error, started_at=started_at)

  def _jobs(self) -> None:
    # Linux parent-death signals follow the spawning thread, not just the PID.
    # Keep this creator alive throughout the resident model's lifetime.
    try:
      while True:
        with self._condition:
          self._condition.wait_for(lambda: self._queue or self._shutdown)
          if self._shutdown:
            return
          session, prefs = self._queue.popleft()
          self._update(transcription_state='transcribing', transcription_session=session,
                       transcription_error=None, queued_sessions=tuple(path for path, _ in self._queue))
        error = None
        try:
          self._transcribe(session, prefs)
        except RuntimeCancelled:
          pass
        except Exception as caught:
          error = str(caught)
        finally:
          with self._lock:
            self._update(transcription_state='error' if error else 'idle',
                         transcription_error=error)
    finally:
      self._runtime.shutdown()

  def _run_recording(self, title: str, prefs: Preferences) -> None:
    error = None
    try:
      self._record(title, prefs)
    except Exception as caught:
      error = str(caught)
    finally:
      with self._lock:
        self._process = None
        self._publish('error' if error else 'idle', session=self._snapshot.session, error=error)

  def start_recording(self, title: str, prefs: Preferences) -> None:
    if not isinstance(title, str) or not title.strip() or '\x00' in title:
      raise DesktopError('Enter a meeting title.')
    with self._lock:
      if self._shutdown:
        raise DesktopError('This window is shutting down; new jobs are disabled.')
      if self._snapshot.state not in ('idle', 'error'):
        raise DesktopError('A recording is already running.')
      self._stop.clear()
      self._publish('starting')
      self._recording_thread = threading.Thread(target=self._run_recording,
        args=(title.strip(), prefs), name='parrot-recording', daemon=True)
      self._recording_thread.start()

  def stop_recording(self) -> None:
    with self._lock:
      if self._snapshot.state not in ('starting', 'recording', 'stopping'):
        raise DesktopError('There is no recording to stop.')
      self._stop.set()
      self._publish('stopping', session=self._snapshot.session, started_at=self._snapshot.started_at)

  def transcribe(self, meeting: Path, prefs: Preferences) -> None:
    session = meeting.expanduser().resolve()
    with self._condition:
      if self._shutdown:
        raise DesktopError('This window is shutting down; new jobs are disabled.')
      if ((self._snapshot.transcription_state == 'transcribing' and
           self._snapshot.transcription_session == session) or
          any(path == session for path, _ in self._queue)):
        raise DesktopError('This recording is already active or queued for transcription.')
      self._queue.append((session, prefs))
      self._update(queued_sessions=tuple(path for path, _ in self._queue))
      if self._thread is None:
        self._thread = threading.Thread(target=self._jobs, name='parrot-asr', daemon=True)
        self._thread.start()
      self._condition.notify_all()

  def shutdown(self) -> None:
    with self._lock:
      self._shutdown = True
      self._stop.set()
      self._queue.clear()
      self._update(queued_sessions=())
      if self._snapshot.state in ('starting', 'recording'):
        self._publish('stopping', session=self._snapshot.session, started_at=self._snapshot.started_at)
      self._condition.notify_all()
      threads = (self._recording_thread, self._thread)
    self._runtime.shutdown()
    for thread in threads:
      if thread is not None and thread is not threading.current_thread():
        thread.join()


  @staticmethod
  def _group_signal(process: subprocess.Popen, signum: int) -> None:
    try:
      os.killpg(process.pid, signum)
    except ProcessLookupError:
      pass

  def _execute_recording(self, args: list[str], session: Path) -> None:
    with tempfile.TemporaryFile() as errors:
      with self._lock:
        if self._shutdown or self._stop.is_set():
          return
        process = subprocess.Popen([sys.executable, str(Path(parrot.__file__).resolve()), *args],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=errors, start_new_session=True)
        self._process = process
      launched = time.monotonic()
      stop_at = None
      escalated = False
      try:
        while process.poll() is None:
          now = time.monotonic()
          with self._lock:
            cancel = self._shutdown or self._stop.is_set()
            if self._snapshot.state == 'starting':
              try:
                ready = all((session / name).stat().st_size > 44 for name in ('mic.wav', 'system.wav'))
              except OSError:
                ready = False
              if ready:
                self._publish('recording', session=session, started_at=now)
          if cancel and stop_at is None:
            # Track creation follows capture's signal-handler installation.
            safe_to_stop = all((session / name).exists() for name in ('mic.wav', 'system.wav'))
            if safe_to_stop or now - launched >= 3:
              try:
                process.send_signal(signal.SIGINT)
              except ProcessLookupError:
                pass
              stop_at = now
          if stop_at is not None and now - stop_at > 8:
            self._group_signal(process, signal.SIGKILL)
            escalated = True
          time.sleep(0.05)
        process.wait()
      finally:
        # A crashed parent can leave descendants behind; this is our group only.
        self._group_signal(process, signal.SIGKILL)
        process.wait()
        with self._lock:
          self._process = None
      if escalated:
        raise DesktopError('Job did not stop normally; owned processes were terminated. Partial files are preserved.')
      if process.returncode:
        errors.seek(0, os.SEEK_END)
        size = errors.tell()
        errors.seek(max(0, size - 8192))
        detail = errors.read().decode('utf-8', errors='replace').strip()
        raise DesktopError(f'Recording exited {process.returncode}: {detail or "No diagnostic output."}')

  def _record(self, title: str, prefs: Preferences) -> None:
    _validate_preferences(prefs)
    if prefs.auto_transcribe:
      settings_ready(prefs)
    with self._lock:
      if self._stop.is_set():
        return
      session = parrot.recording_path(None)
      self._publish('starting', session=session)
    args = ['record', '--output', str(session), '--title', title]
    for name in ('microphone', 'system'):
      value = getattr(prefs, name)
      if value is not None:
        args.extend([f'--{name}', value])
    self._execute_recording(args, session)
    if not session.exists():
      return
    manifest = _json(session / 'session.json')
    if manifest.get('status') != 'recorded':
      raise DesktopError(f'Recording did not finalize successfully. Partial audio is preserved in {session}.')
    with self._lock:
      if self._shutdown or not prefs.auto_transcribe:
        return
      self.transcribe(session, prefs)

  def _transcribe(self, session: Path, prefs: Preferences) -> None:
    settings_ready(prefs)
    with self._lock:
      if self._shutdown:
        return
    try:
      transcribe_session(session, **inference_options(prefs), runtime_executor=self._runtime.execute,
                         cancelled=lambda: self._shutdown)
    except TranscriptionError:
      if self._shutdown:
        return
      raise
    with self._lock:
      if self._shutdown:
        return
    read_transcript(session)
