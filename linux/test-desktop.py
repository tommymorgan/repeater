#!/usr/bin/env python3
"""Isolated desktop behavior tests; synthetic subprocesses never open audio devices."""

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
import wave

import desktop


SYNTHETIC_CLI = '''import json, os, pathlib, signal, subprocess, sys, time
args = sys.argv[1:]
if args[0] == 'transcribe':
  child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
  pathlib.Path(args[1], 'child.pid').write_text(str(child.pid))
  time.sleep(60)
else:
  output = pathlib.Path(args[args.index('--output') + 1])
  title = args[args.index('--title') + 1]
  time.sleep(0.15)
  output.mkdir(mode=0o700)
  stopped = False
  def stop(signum, frame):
    global stopped
    stopped = title != 'stuck'
  signal.signal(signal.SIGINT, stop)
  signal.signal(signal.SIGTERM, stop)
  child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
  (output / 'child.pid').write_text(str(child.pid))
  data = {'schemaVersion': 1, 'id': '00000000-0000-0000-0000-000000000001',
          'title': title, 'startedAt': '2026-10-05T12:00:00+00:00',
          'status': 'recording', 'durationSeconds': 1}
  (output / 'session.json').write_text(json.dumps(data))
  for name in ('mic.wav', 'system.wav'):
    (output / name).write_bytes(b'0' * 64)
  while not stopped:
    time.sleep(0.02)
  child.terminate()
  child.wait()
  data['status'] = 'failed' if title == 'fail' else 'recorded'
  (output / 'session.json').write_text(json.dumps(data))
  sys.exit(1 if title == 'fail' else 0)
'''


def fixture(session: Path, *, audio: Path | None = None) -> None:
  session.mkdir(mode=0o700)
  frames = 16000
  for filename in ('mic.wav', 'system.wav'):
    if audio:
      shutil.copyfile(audio, session / filename)
      with wave.open(str(session / filename), 'rb') as handle:
        frames = handle.getnframes()
    else:
      with wave.open(str(session / filename), 'wb') as handle:
        handle.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        handle.writeframes(b'\0\0' * frames)
    (session / filename).chmod(0o600)
  manifest = {'schemaVersion': 1, 'id': str(uuid.uuid4()), 'title': 'Public fixture',
              'startedAt': '2026-10-05T12:00:00+00:00', 'status': 'recorded',
              'durationSeconds': frames / 16000, 'sampleRate': 16000, 'channels': 1,
              'tracks': [{'source': source, 'file': filename, 'device': 'synthetic.public', 'frames': frames}
                         for source, filename in (('me', 'mic.wav'), ('them', 'system.wav'))]}
  (session / 'session.json').write_text(json.dumps(manifest), encoding='utf-8')
  (session / 'session.json').chmod(0o600)


def transcript(session: Path) -> None:
  manifest = json.loads((session / 'session.json').read_text())
  payload = {'schemaVersion': 1, 'meetingId': manifest['id'], 'title': manifest['title'],
             'segments': [{'source': 'me', 'speakerLabel': 'Me', 'startTime': 0,
                           'endTime': 0.5, 'text': 'Public fixture text.'}]}
  (session / 'transcript.json').write_text(json.dumps(payload))


class DesktopTests(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory(prefix='parrot-desktop-test-')
    self.root = Path(self.temp.name)
    self.environment = patch.dict(os.environ, {'XDG_DATA_HOME': str(self.root / 'data'),
                                               'XDG_CONFIG_HOME': str(self.root / 'config')})
    self.environment.start()
    self.controllers = []
    model = self.root / 'public-model.bin'
    model.write_bytes(b'local test model')
    self.prefs = replace(desktop.Preferences(), profile='multitalker-parakeet', parakeet_model=model,
                         parakeet_runtime=Path('/usr/bin/false'), use_gpu=False)

  def tearDown(self):
    for controller in self.controllers:
      controller.shutdown()

    self.environment.stop()
    self.temp.cleanup()
  def idle(self, controller):
    snapshot = controller.snapshot
    return (snapshot.state in ('idle', 'error') and
            snapshot.transcription_state in ('idle', 'error') and not snapshot.queued_sessions)

  def wait(self, condition):
    deadline = time.monotonic() + 12
    while not condition():
      if time.monotonic() > deadline:
        self.fail('Desktop job did not reach expected state')
      time.sleep(0.02)

  def controller(self, cls=desktop.DesktopController):
    states = []
    controller = cls(states.append)
    self.controllers.append(controller)
    return controller, states

  def synthetic(self):
    path = self.root / 'synthetic-cli.py'
    path.write_text(SYNTHETIC_CLI)
    return patch.object(desktop.parrot, '__file__', str(path))

  def test_preferences_atomic_private_and_stable_names(self):
    prefs = replace(desktop.Preferences(), microphone='alsa_input.usb.stable', system='alsa_output.stable')
    desktop.save_preferences(prefs)
    self.assertEqual(desktop.load_preferences(), prefs)
    self.assertEqual(stat.S_IMODE(desktop._settings_path().stat().st_mode), 0o600)
    self.assertEqual(stat.S_IMODE(desktop._settings_path().parent.stat().st_mode), 0o700)
    with self.assertRaises(desktop.DesktopError):
      desktop.save_preferences(replace(prefs, threads=True))
    self.assertEqual(desktop.load_preferences(), prefs)

  def test_preferences_invalid_values_and_relative_xdg(self):
    self.assertEqual(desktop.load_preferences(), desktop.Preferences())
    for prefs in (replace(desktop.Preferences(), nemotron_model=Path('relative')), replace(desktop.Preferences(), use_gpu=1),
                  replace(desktop.Preferences(), microphone=''), replace(desktop.Preferences(), language='')):
      with self.assertRaises(desktop.DesktopError):
        desktop.save_preferences(prefs)
    with patch.dict(os.environ, {'XDG_CONFIG_HOME': 'relative', 'XDG_DATA_HOME': 'relative'}):
      with self.assertRaises(desktop.DesktopError):
        desktop.load_preferences()
      with self.assertRaises(desktop.DesktopError):
        desktop.meetings_root()
    path = desktop._settings_path()
    path.parent.mkdir(parents=True)
    for data in ('[]', '{"threads": false}', '{"unknown": 1}', '{'):
      path.write_text(data)
      with self.assertRaises(desktop.DesktopError):
        desktop.load_preferences()

  def test_legacy_preferences_preserve_common_settings_without_selecting_or_rewriting(self):
    path = desktop._settings_path()
    path.parent.mkdir(parents=True)
    original = json.dumps({'model': '/old/whisper-model.bin', 'whisper_cli': '/old/whisper-cli',
      'microphone': 'stable.mic', 'system': 'stable.output', 'language': 'auto', 'auto_transcribe': True,
      'threads': 7, 'use_gpu': False, 'review_model': 'local-review'})
    path.write_text(original)
    prefs = desktop.load_preferences()
    self.assertIsNone(prefs.profile)
    self.assertEqual((prefs.microphone, prefs.system, prefs.language, prefs.threads, prefs.use_gpu,
                      prefs.auto_transcribe, prefs.review_model),
                     ('stable.mic', 'stable.output', 'auto', 7, False, True, 'local-review'))
    with self.assertRaises(desktop.DesktopError):
      desktop.settings_ready(prefs)
    self.assertEqual(path.read_text(), original)
    chosen = replace(prefs, profile='multitalker-parakeet', language='en')
    desktop.save_preferences(chosen)
    self.assertEqual(desktop.load_preferences(), chosen)

  def test_history_handles_failed_stale_malformed_and_transcript(self):
    root = desktop.meetings_root()
    for name, status in (('a', 'recorded'), ('b', 'failed'), ('c', 'recording')):
      path = root / name
      fixture(path)
      payload = json.loads((path / 'session.json').read_text())
      payload['status'] = status
      (path / 'session.json').write_text(json.dumps(payload))
    transcript(root / 'a')
    (root / 'broken').mkdir()
    (root / 'broken/session.json').write_text('[]')
    (root / 'ignored-link').symlink_to(root / 'a', target_is_directory=True)
    meetings = desktop.list_meetings()
    self.assertEqual(len(meetings), 4)
    indexed = {meeting.path.name: meeting for meeting in meetings}
    self.assertTrue(indexed['a'].has_transcript)
    self.assertIn('partial', indexed['b'].error)
    self.assertIn('not an active job', indexed['c'].error)
    self.assertIn('session.json', indexed['broken'].error)
    controller, _ = self.controller()
    self.assertEqual(controller.snapshot.state, 'idle')

  def test_transcript_json_is_authoritative_and_validated(self):
    session = self.root / 'session'
    fixture(session)
    transcript(session)
    (session / 'transcript.txt').write_text('not authoritative')
    self.assertEqual(desktop.read_transcript(session), '[00:00] Me: Public fixture text.\n')
    path = session / 'transcript.json'
    original = json.loads(path.read_text())
    for key, value in (('source', {}), ('startTime', True), ('endTime', 9), ('text', None)):
      payload = json.loads(json.dumps(original))
      payload['segments'][0][key] = value
      path.write_text(json.dumps(payload))
      with self.assertRaises(desktop.DesktopError):
        desktop.read_transcript(session)
    path.unlink()
    with self.assertRaises(desktop.DesktopError):
      desktop.read_transcript(session)

  def test_construction_never_launches_and_preflight_never_captures(self):
    with patch.object(desktop.subprocess, 'Popen', side_effect=AssertionError('No process expected')):
      controller, states = self.controller()
      controller.start_recording('preflight', replace(desktop.Preferences(), nemotron_model=self.root / 'missing'))
      self.wait(lambda: self.idle(controller))
      self.assertEqual(controller.snapshot.state, 'error')

      self.assertEqual(list(desktop.meetings_root().iterdir()), [])

  def test_recording_stop_concurrency_and_retry(self):
    with self.synthetic():
      controller, states = self.controller()
      prefs = replace(desktop.Preferences(), auto_transcribe=False)
      controller.start_recording('public', prefs)
      with self.assertRaises(desktop.DesktopError):
        controller.start_recording('duplicate', prefs)
      self.wait(lambda: controller.snapshot.state == 'recording')
      self.assertIsNotNone(controller.snapshot.started_at)
      controller.stop_recording()
      self.wait(lambda: self.idle(controller))
      self.assertEqual(controller.snapshot.state, 'idle')

      self.assertEqual(json.loads((controller.snapshot.session / 'session.json').read_text())['status'], 'recorded')
      controller.start_recording('fail', prefs)
      self.wait(lambda: controller.snapshot.state == 'recording')
      controller.stop_recording()
      self.wait(lambda: self.idle(controller))
      self.assertEqual(controller.snapshot.state, 'error')
      self.assertIn('Recording exited', controller.snapshot.error)
      controller.start_recording('retry', prefs)
      controller.stop_recording()
      self.wait(lambda: self.idle(controller))
      self.assertEqual(controller.snapshot.state, 'idle')

  def test_stop_during_startup_and_shutdown_rejects_jobs(self):
    with self.synthetic():
      controller, _ = self.controller()
      controller.start_recording('startup', replace(desktop.Preferences(), auto_transcribe=False))
      controller.stop_recording()
      self.wait(lambda: self.idle(controller))
      self.assertEqual(controller.snapshot.state, 'idle')
      controller.shutdown()
      with self.assertRaises(desktop.DesktopError):
        controller.start_recording('forbidden', desktop.Preferences())

  def test_auto_transcribe_success_and_failure(self):
    class ExportController(desktop.DesktopController):
      def _transcribe(inner, session, prefs):
        if json.loads((session / 'session.json').read_text())['title'] == 'inference failure':
          raise desktop.DesktopError('Local inference failed')
        transcript(session)
        desktop.read_transcript(session)
    with self.synthetic():
      controller, states = self.controller(ExportController)
      for title, expected in (('public', 'idle'), ('inference failure', 'error')):
        controller.start_recording(title, self.prefs)
        self.wait(lambda: controller.snapshot.state == 'recording')
        controller.stop_recording()
        self.wait(lambda: self.idle(controller))
        self.assertEqual(controller.snapshot.transcription_state, expected)
        self.assertEqual(controller.snapshot.state, 'idle')

  def test_capture_and_auto_queue_remain_available_during_blocked_asr(self):
    first = self.root / 'older'
    fixture(first)
    release = threading.Event()
    entered = threading.Event()
    calls = []
    def infer(session, **options):
      calls.append((session, options['threads']))
      if session == first:
        entered.set()
        if not release.wait(10):
          raise RuntimeError('Synthetic ASR release timed out')
      transcript(session)
    with self.synthetic(), patch.object(desktop, 'transcribe_session', infer):
      controller, _ = self.controller()
      try:
        controller.transcribe(first, self.prefs)
        self.assertTrue(entered.wait(3))
        captured = []
        for title, threads in (('second', 2), ('third', 3)):
          controller.start_recording(title, replace(self.prefs, threads=threads))
          self.wait(lambda: controller.snapshot.state == 'recording')
          captured.append(controller.snapshot.session)
          controller.stop_recording()
          self.wait(lambda: controller.snapshot.state == 'idle')
          self.assertEqual(controller.snapshot.transcription_session, first)
          self.assertEqual(controller.snapshot.transcription_state, 'transcribing')
          self.assertEqual(json.loads((captured[-1] / 'session.json').read_text())['status'], 'recorded')
        self.assertEqual(controller.snapshot.queued_sessions, tuple(captured))
        self.assertEqual(calls, [(first, self.prefs.threads)])
        release.set()
        self.wait(lambda: self.idle(controller))
        self.assertEqual(calls, [(first, self.prefs.threads), (captured[0], 2), (captured[1], 3)])
      finally:
        release.set()
        controller.shutdown()

  def test_manual_fifo_rejects_duplicates_and_advances_after_failure(self):
    sessions = [self.root / name for name in ('first', 'failed', 'last')]
    for session in sessions:
      fixture(session)
    release = threading.Event()
    entered = threading.Event()
    calls = []
    def infer(session, **options):
      calls.append(session)
      if session == sessions[0]:
        entered.set()
        if not release.wait(10):
          raise RuntimeError('Synthetic ASR release timed out')
      if session == sessions[1]:
        raise desktop.TranscriptionError('Synthetic per-session failure')
      transcript(session)
    with patch.object(desktop, 'transcribe_session', infer):
      controller, snapshots = self.controller()
      try:
        controller.transcribe(sessions[0], self.prefs)
        self.assertTrue(entered.wait(3))
        for session in sessions[1:]:
          controller.transcribe(session, self.prefs)
        for session in sessions:
          with self.assertRaises(desktop.DesktopError):
            controller.transcribe(session / '.', self.prefs)
        self.assertEqual(controller.snapshot.queued_sessions, tuple(sessions[1:]))
        release.set()
        self.wait(lambda: self.idle(controller))
        self.assertEqual(calls, sessions)
        self.assertTrue(any(snapshot.transcription_session == sessions[1] and
          snapshot.transcription_error == 'Synthetic per-session failure' for snapshot in snapshots))
        self.assertEqual(controller.snapshot.transcription_session, sessions[2])
        self.assertIn('Public fixture text.', desktop.read_transcript(sessions[2]))
      finally:
        release.set()
        controller.shutdown()

  def test_observer_exceptions_do_not_abandon_capture_or_asr(self):
    def observe(snapshot):
      raise RuntimeError('Synthetic observer failure')
    with self.synthetic(), patch.object(desktop, 'transcribe_session', lambda session, **_: transcript(session)):
      controller = desktop.DesktopController(observe)
      self.controllers.append(controller)
      controller.start_recording('public', self.prefs)
      self.wait(lambda: controller.snapshot.state == 'recording')
      controller.stop_recording()
      self.wait(lambda: self.idle(controller))
      self.assertIn('Public fixture text.', desktop.read_transcript(controller.snapshot.session))

  def test_cancel_transcription_reaps_worker_preserves_exports(self):
    session = self.root / 'cancel'
    fixture(session)
    transcript(session)
    before = (session / 'transcript.json').read_bytes()
    runtime = self.root / 'resident-runtime'
    runtime.write_text(f'#!{sys.executable}\n' + '''import os, pathlib, sys, time
print('ready', flush=True)
field = bytearray()
while True:
  value = sys.stdin.buffer.read(1)
  if not value: sys.exit(0)
  if value == bytes([0]): break
  field.extend(value)
pathlib.Path(os.fsdecode(bytes(field))).parent.joinpath('child.pid').write_text(str(os.getpid()))
time.sleep(60)
''')
    runtime.chmod(0o700)
    controller, _ = self.controller()
    controller.transcribe(session, replace(self.prefs, parakeet_runtime=runtime))
    self.wait(lambda: (session / 'child.pid').exists())
    child = int((session / 'child.pid').read_text())
    with self.synthetic():
      controller.start_recording('concurrent public capture', replace(self.prefs, auto_transcribe=False))
      self.wait(lambda: controller.snapshot.state == 'recording')
      captured = controller.snapshot.session
      capture_child = int((captured / 'child.pid').read_text())
      controller.stop_recording()
      self.wait(lambda: controller.snapshot.state == 'idle')
      self.assertEqual(json.loads((captured / 'session.json').read_text())['status'], 'recorded')
      self.assertEqual(controller.snapshot.transcription_state, 'transcribing')
      self.assertTrue(Path(f'/proc/{child}').exists())
      self.assertFalse(Path(f'/proc/{capture_child}').exists())
    queued = self.root / 'unstarted'
    fixture(queued)
    controller.transcribe(queued, replace(self.prefs, parakeet_runtime=runtime))
    controller.shutdown()
    self.assertFalse((queued / 'child.pid').exists())
    self.assertFalse((queued / 'transcript.json').exists())
    self.assertEqual(controller.snapshot.queued_sessions, ())
    with self.assertRaises(desktop.DesktopError):
      controller.transcribe(queued, self.prefs)
    self.assertTrue(self.idle(controller))
    self.assertEqual((session / 'transcript.json').read_bytes(), before)
    self.assertFalse(Path(f'/proc/{child}').exists())

  def test_forced_shutdown_is_bounded_and_does_not_kill_unowned_process(self):
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
    try:
      with self.synthetic():
        controller, _ = self.controller()
        controller.start_recording('stuck', replace(self.prefs, auto_transcribe=False))
        self.wait(lambda: controller.snapshot.state == 'recording')
        child = int((controller.snapshot.session / 'child.pid').read_text())
        controller.shutdown()
        self.assertTrue(self.idle(controller))
        self.assertEqual(controller.snapshot.state, 'error')
        self.assertIn('terminated', controller.snapshot.error)
        self.assertIsNone(unrelated.poll())
        status = Path(f'/proc/{child}/stat')
        self.assertTrue(not status.exists() or status.read_text().split()[2] == 'Z')
    finally:
      unrelated.terminate()
      unrelated.wait()

  def test_shutdown_finalizes_active_recording_without_auto_inference(self):
    with self.synthetic():
      controller, states = self.controller()
      controller.start_recording('public', self.prefs)
      self.wait(lambda: controller.snapshot.state == 'recording')
      controller.shutdown()
      self.assertTrue(self.idle(controller))
      self.assertIsNone(controller.snapshot.transcription_session)
      self.assertEqual(json.loads((controller.snapshot.session / 'session.json').read_text())['status'], 'recorded')

  def test_failed_real_cli_preserves_good_exports(self):
    session = self.root / 'failed-inference'
    fixture(session)
    transcript(session)
    before = (session / 'transcript.json').read_bytes()
    controller, _ = self.controller()
    controller.transcribe(session, self.prefs)
    self.wait(lambda: self.idle(controller))
    self.assertEqual(controller.snapshot.transcription_state, 'error')
    self.assertEqual((session / 'transcript.json').read_bytes(), before)

  def test_archive_trash_restore_preserve_whole_recording_and_collision(self):
    session = desktop.meetings_root() / 'whole recording'
    fixture(session)
    transcript(session)
    before = {path.name: path.read_bytes() for path in session.iterdir()}
    desktop.set_archived(session, True)
    self.assertTrue(desktop.list_meetings()[0].archived)
    entry = desktop.trash_meeting(session)
    self.assertFalse(session.exists())
    self.assertEqual(desktop.list_meetings(), [])
    self.assertIn('Path=', entry.info_path.read_text())
    session.mkdir()
    with self.assertRaises(desktop.DesktopError):
      desktop.restore_trashed_meeting(entry)
    self.assertTrue(entry.trashed_path.is_dir())
    session.rmdir()
    self.assertEqual(desktop.restore_trashed_meeting(entry), session)
    self.assertFalse(entry.info_path.exists())
    self.assertTrue(desktop.list_meetings()[0].archived)
    desktop.set_archived(session, False)
    self.assertFalse(desktop.list_meetings()[0].archived)
    for name in ('mic.wav', 'system.wav', 'transcript.json'):
      self.assertEqual((session / name).read_bytes(), before[name])
      self.assertEqual((session / name).stat().st_mode & 0o777, 0o600 if name != 'transcript.json' else 0o644)

  def test_trash_restore_metadata_failure_preserves_retryable_entry(self):
    session = desktop.meetings_root() / 'restore failure'
    fixture(session)
    transcript(session)
    original = {path.name: path.read_bytes() for path in session.iterdir()}
    entry = desktop.trash_meeting(session)
    unlink = Path.unlink
    def fail_metadata(path, *args, **kwargs):
      if path == entry.info_path:
        raise OSError('Cannot remove Trash metadata')
      return unlink(path, *args, **kwargs)
    with patch.object(Path, 'unlink', autospec=True, side_effect=fail_metadata):
      with self.assertRaises(desktop.DesktopError):
        desktop.restore_trashed_meeting(entry)
    self.assertFalse(session.exists())
    self.assertTrue(entry.info_path.is_file())
    self.assertEqual({path.name: path.read_bytes() for path in entry.trashed_path.iterdir()}, original)
    desktop.restore_trashed_meeting(entry)
    self.assertEqual({path.name: path.read_bytes() for path in session.iterdir()}, original)

  def test_move_refuses_recording_busy_symlink_and_foreign_paths(self):
    from transcribe import session_lock
    session = desktop.meetings_root() / 'safe'
    fixture(session)
    before = (session / 'mic.wav').read_bytes()
    with session_lock(session):
      for operation in (lambda: desktop.trash_meeting(session), lambda: desktop.set_archived(session, True)):
        with self.assertRaisesRegex(desktop.DesktopError, 'busy'):
          operation()
    (session / 'escape').symlink_to(self.root)
    with self.assertRaises(desktop.DesktopError):
      desktop.trash_meeting(session)
    (session / 'escape').unlink()
    manifest = json.loads((session / 'session.json').read_text())
    manifest['status'] = 'recording'
    (session / 'session.json').write_text(json.dumps(manifest))
    with self.assertRaises(desktop.DesktopError):
      desktop.trash_meeting(session)
    foreign = self.root / 'foreign'
    fixture(foreign)
    with self.assertRaises(desktop.DesktopError):
      desktop.trash_meeting(foreign)
    self.assertEqual((session / 'mic.wav').read_bytes(), before)
    self.assertTrue(foreign.is_dir())

  def test_history_retains_transcript_during_write_and_parallel_reads(self):
    from transcribe import session_lock
    session = desktop.meetings_root() / 'busy-transcript'
    fixture(session)
    transcript(session)
    with session_lock(session):
      meeting = desktop.list_meetings()[0]
      self.assertTrue(meeting.has_transcript)
      self.assertIsNone(meeting.error)
    with session_lock(session, shared=True):
      self.assertIn('Public fixture text.', desktop.read_transcript(session))
      self.assertTrue(desktop.list_meetings()[0].has_transcript)

def smoke() -> None:
  """Run real local CUDA inference on public JFK audio, in scratch XDG storage."""
  with tempfile.TemporaryDirectory(prefix='parrot-desktop-smoke-') as directory:
    root = Path(directory)
    with patch.dict(os.environ, {'XDG_DATA_HOME': str(root / 'data'), 'XDG_CONFIG_HOME': str(root / 'config')}):
      session = desktop.meetings_root() / 'public-jfk'
      fixture(session, audio=Path(os.environ['REPEATER_SMOKE_AUDIO']))
      completed = threading.Event()
      def observe(snapshot):
        if snapshot.transcription_session == session and snapshot.transcription_state in ('idle', 'error'):
          completed.set()
      controller = desktop.DesktopController(observe)
      controller.transcribe(session, desktop.Preferences())
      if not completed.wait(120):
        controller.shutdown()
        completed.wait(12)
        raise RuntimeError('Public fixture transcription timed out')
      if controller.snapshot.transcription_state != 'idle':
        raise RuntimeError(controller.snapshot.transcription_error)
      text = desktop.read_transcript(session)
      if 'country' not in text.lower():
        raise RuntimeError('Public fixture expected speech not found')
      if not desktop.list_meetings()[0].has_transcript:
        raise RuntimeError('History failed to discover transcript')
      for filename in ('session.json', 'mic.wav', 'system.wav', 'transcript.json', 'transcript.txt', 'transcript.srt'):
        if stat.S_IMODE((session / filename).stat().st_mode) != 0o600:
          raise RuntimeError(f'Nonprivate export: {filename}')
      controller.shutdown()
      from transcribe import (load_transcript_edit_state, edit_transcript_text,
        save_transcript_edit_state, undo_transcript_edit, redo_transcript_edit)
      import hashlib
      hashes = {name: hashlib.sha256((session / name).read_bytes()).hexdigest() for name in ('mic.wav', 'system.wav')}
      state = load_transcript_edit_state(session)
      corrected = edit_transcript_text(state, 0, 'Human correction [BLANK_AUDIO] retained.')
      save_transcript_edit_state(session, corrected)
      state = load_transcript_edit_state(session)
      if not state.has_corrections:
        raise RuntimeError('Corrections did not survive reopening')
      for name in ('transcript.json', 'transcript.txt', 'transcript.srt'):
        if 'Human correction [BLANK_AUDIO] retained.' not in (session / name).read_text():
          raise RuntimeError(f'Correction missing from {name}')
      undone = undo_transcript_edit(state)
      save_transcript_edit_state(session, undone)
      save_transcript_edit_state(session, redo_transcript_edit(undone))
      subprocess.run([sys.executable, str(Path(desktop.parrot.__file__).resolve()), 'transcribe', str(session),
                      '--profile', desktop.Preferences().profile, '--language', 'en', '--cpu'], check=True)
      replacement = load_transcript_edit_state(session)
      restored = undo_transcript_edit(replacement)
      save_transcript_edit_state(session, restored)
      if restored.payload != corrected.payload or not restored.has_corrections:
        raise RuntimeError('CLI retranscription Undo did not restore corrections')
      desktop.set_archived(session, True)
      entry = desktop.trash_meeting(session)
      desktop.restore_trashed_meeting(entry)
      if not desktop.list_meetings()[0].archived:
        raise RuntimeError('Trash Undo lost original collection')
      if hashes != {name: hashlib.sha256((session / name).read_bytes()).hexdigest() for name in hashes}:
        raise RuntimeError('Audio changed')
      print('Real CUDA + CPU CLI smoke passed: corrected JSON/TXT/SRT/history, restart Undo/Redo, reversible retranscription, Archive/Trash/Restore, untouched WAV hashes:', hashes)


if __name__ == '__main__':
  import sys
  if '--smoke' in sys.argv:
    smoke()
  else:
    unittest.main()
