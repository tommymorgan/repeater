#!/usr/bin/env python3
"""Real GTK widget tests; isolated data, no capture or model inference."""

import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


def run_now(window, work, done=None, failed=None):
  try:
    result = work()
  except Exception as error:
    (failed or window.show_error)(str(error))
  else:
    if done:
      done(result)


class SettingsTests(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    temporary = tempfile.TemporaryDirectory(prefix='repeater-gtk-test-')
    cls.addClassCleanup(temporary.cleanup)
    root = Path(temporary.name)
    environment = patch.dict(os.environ, {'XDG_DATA_HOME': str(root / 'data'),
                                         'XDG_CONFIG_HOME': str(root / 'config')})
    environment.start()
    cls.addClassCleanup(environment.stop)
    cls.gui = importlib.import_module('gui')
    if not cls.gui.Gtk.init_check():
      raise RuntimeError('GTK tests require a desktop display. In headless CI, use xvfb-run -a make linux-gtk-check.')

  def test_settings_button_opens_preferences_page(self):
    gui = self.gui
    app = gui.Adw.Application(application_id='local.TheRepeater.SettingsTest',
                              flags=gui.Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    sources = []
    add_timeout = gui.GLib.timeout_add_seconds

    def track_timeout(*args):
      source = add_timeout(*args)
      sources.append(source)
      return source

    with patch.object(gui.RepeaterWindow, '_run', run_now), \
         patch.object(gui, 'list_devices', return_value={'microphones': [], 'outputs': []}), \
         patch.object(gui, 'settings_ready', return_value=None), \
         patch.object(gui.GLib, 'timeout_add_seconds', side_effect=track_timeout):
      window = gui.RepeaterWindow(app)
      self.addCleanup(window.destroy)
      self.addCleanup(lambda: [gui.GLib.source_remove(source) for source in sources])
      previous_page = window.navigation.get_visible_page()
      callback_errors = []
      with patch.object(sys, 'excepthook', lambda kind, error, traceback: callback_errors.append(error)):
        window.settings_button.emit('clicked')
      if callback_errors:
        raise callback_errors[0]
      page = window.navigation.get_visible_page()
      self.assertIsNot(page, previous_page)
      self.assertTrue(window.settings_fields['profile'].is_ancestor(page))


  def test_speaker_panel_rename_confirmation_and_wrong_correction(self):
    import json
    import uuid
    from types import SimpleNamespace
    from transcribe import canonical_transcript, load_transcript_edit_state
    gui = self.gui
    directory = tempfile.TemporaryDirectory(prefix='repeater-speakers-')
    self.addCleanup(directory.cleanup)
    path = Path(directory.name)
    manifest = {'id': str(uuid.uuid4()), 'title': 'Synthetic speaker review', 'durationSeconds': 10}
    (path / 'session.json').write_text(json.dumps(manifest))
    runtime = {'schemaVersion': 1, 'engine': 'multitalker-parakeet', 'speakerCapacity': 4,
               'durationSeconds': 10, 'segments': [
                 {'startTime': 0, 'endTime': 3, 'text': 'First synthetic turn.', 'speakerTag': 1},
                 {'startTime': 5, 'endTime': 8, 'text': 'Second synthetic turn.', 'speakerTag': 1}],
               'speakerTurns': [{'startTime': 0, 'endTime': 3, 'speakerTag': 1},
                                {'startTime': 5, 'endTime': 8, 'speakerTag': 1}],
               'provenance': {'runtimeVersion': 'fixture',
                 'modelRevision': 'fixture', 'timingOrigin': 'fixture', 'backend': 'cpu'}}
    payload = canonical_transcript(manifest, {'me': runtime, 'them': runtime},
                                   profile='multitalker-parakeet', language='en', backend='cpu')
    (path / 'transcript.json').write_text(json.dumps(payload))
    app = gui.Adw.Application(application_id='local.TheRepeater.SpeakerTest',
                              flags=gui.Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    sources = []
    original_timeout = gui.GLib.timeout_add_seconds
    def timeout(*args):
      source = original_timeout(*args)
      sources.append(source)
      return source
    with (patch.object(gui.RepeaterWindow, '_run', run_now),
          patch.object(gui, 'list_devices', return_value={'microphones': [], 'outputs': []}),
          patch.object(gui, 'settings_ready', return_value=None),
          patch.object(gui.GLib, 'timeout_add_seconds', side_effect=timeout)):
      window = gui.RepeaterWindow(app)
      self.addCleanup(window.destroy)
      self.addCleanup(lambda: [gui.GLib.source_remove(source) for source in sources])
      self.addCleanup(lambda: gui.GLib.source_remove(window.edit_save_source) if window.edit_save_source else None)
      window.selected_meeting = SimpleNamespace(path=path, title=manifest['title'],
                                                status='recorded', has_transcript=True)
      window.edit_states[path] = load_transcript_edit_state(path)
      window.stack.set_visible_child_name('detail')
      window._present_edit_state(window.edit_states[path])
      errors = []
      with patch.object(sys, 'excepthook', lambda kind, error, traceback: errors.append(error)):
        self.exercise_speakers(window, path, load_transcript_edit_state)
      if errors:
        raise errors[0]

  def exercise_speakers(self, window, path, load_state):
    gui = self.gui
    def click(caption):
      def walk(widget):
        yield widget
        child = widget.get_first_child()
        while child:
          yield from walk(child)
          child = child.get_next_sibling()
      controls = [widget for widget in walk(window.speaker_cards)
                  if isinstance(widget, gui.Gtk.Button) and widget.get_label() == caption]
      self.assertTrue(controls, caption)
      controls[0].emit('clicked')
    window.transcript_view._speaker_buttons[0].emit('clicked')
    speaker_id = window.edit_states[path].payload['segments'][0]['speakerId']
    self.assertEqual(window.sidebar_stack.get_visible_child_name(), 'speakers')
    self.assertTrue(window.speaker_occurrences[speaker_id].get_expanded())
    self.assertIsNone(window.speaker_rename_id)
    click('Rename all')
    window.speaker_name_entry.set_text('Nathan')
    click('Apply')
    self.assertEqual(next(s for s in window.edit_states[path].payload['speakers'] if s['id'] == speaker_id)['name'], 'Nathan')
    click('✓ Right')
    self.assertIsNotNone(window.voice_offer_dialog)
    window.voice_offer_dialog.close()
    window._save_edits()
    self.assertFalse(window.edit_save_failed, window.record_errors)
    click('✓ Right')
    self.assertIsNone(window.voice_offer_dialog)
    self.assertEqual(load_state(path).payload['segments'][0]['speakerConfirmed'], speaker_id)
    click('✕ Wrong')
    click('Cancel')
    self.assertEqual(window.edit_states[path].payload['segments'][0]['speakerRejected'], speaker_id)
    click('✕ Wrong')
    new_person = window.speaker_cards
    def find_entry(widget):
      if isinstance(widget, gui.Gtk.Expander) and widget.get_label() == 'New person':
        widget.set_expanded(True)
      if isinstance(widget, gui.Gtk.Entry):
        return widget
      child = widget.get_first_child()
      while child:
        found = find_entry(child)
        if found:
          return found
        child = child.get_next_sibling()
    find_entry(new_person).set_text('Another person')
    click('Create person and apply')
    corrected = window.edit_states[path].payload['segments'][0]
    self.assertNotEqual(corrected['speakerId'], speaker_id)
    self.assertNotIn('speakerConfirmed', corrected)
    self.assertNotIn('speakerRejected', corrected)
    click('Show')
    context = gui.GLib.MainContext.default()
    for _ in range(200):
      if not context.pending():
        break
      context.iteration(False)
    self.assertTrue(window.transcript_view._turn_widgets[0].get_child_at(0, 0).get_expanded())
    window._save_edits()
    self.assertEqual(load_state(path).payload, window.edit_states[path].payload)
    window._close_speakers()
    self.assertEqual(window.sidebar_stack.get_visible_child_name(), 'recordings')

class QueueWidgetTests(unittest.TestCase):
  setUpClass = classmethod(SettingsTests.setUpClass.__func__)

  def setUp(self):
    import json
    import threading
    import time
    import uuid
    import wave
    from contextlib import ExitStack
    from dataclasses import replace
    import desktop
    self.desktop = desktop
    self.time = time
    self.json = json
    self.replace = replace
    self.stack = ExitStack()
    self.addCleanup(self.stack.close)
    directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='repeater-queue-widgets-'))
    root = Path(directory)
    self.stack.enter_context(patch.dict(os.environ, {
      'XDG_DATA_HOME': str(root / 'data'), 'XDG_CONFIG_HOME': str(root / 'config')}))
    self.paths = []

    def fixture(title, *, transcript=True):
      path = desktop.meetings_root() / str(uuid.uuid4())
      path.mkdir(parents=True, mode=0o700)
      manifest = {'schemaVersion': 1, 'id': str(uuid.uuid4()), 'title': title,
                  'startedAt': '2026-10-13T12:00:00+00:00', 'status': 'recorded',
                  'durationSeconds': 1, 'sampleRate': 16000, 'channels': 1,
                  'tracks': [{'source': source, 'file': filename, 'device': 'synthetic', 'frames': 16000}
                             for source, filename in (('me', 'mic.wav'), ('them', 'system.wav'))]}
      (path / 'session.json').write_text(json.dumps(manifest))
      for filename in ('mic.wav', 'system.wav'):
        with wave.open(str(path / filename), 'wb') as audio:
          audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
          audio.writeframes(b'\0\0' * 16000)
      if transcript:
        write_transcript(path, 'Synthetic finished transcript.')
      self.paths.append(path)
      return path

    def write_transcript(path, text):
      manifest = json.loads((path / 'session.json').read_text())
      (path / 'transcript.json').write_text(json.dumps({
        'schemaVersion': 1, 'meetingId': manifest['id'], 'title': manifest['title'],
        'segments': [{'source': 'me', 'speakerLabel': 'Me', 'startTime': 0,
                      'endTime': 0.5, 'text': text}]}))

    self.fixture = fixture
    self.write_transcript = write_transcript
    self.active = fixture('Active synthetic ASR')
    self.waiting = fixture('Waiting synthetic ASR')
    self.finished = fixture('Unrelated finished recording')
    self.releases = {path: threading.Event() for path in self.paths}
    self.failures = set()
    owner = self

    class SyntheticController(desktop.DesktopController):
      def _record(self, title, prefs):
        path = fixture(title, transcript=False)
        self._publish('recording', session=path, started_at=time.monotonic())
        if not self._stop.wait(8):
          raise RuntimeError('Synthetic capture was not stopped')
        if prefs.auto_transcribe and not self._shutdown:
          self.transcribe(path, prefs)

      def _transcribe(self, path, prefs):
        gate = owner.releases.setdefault(path, threading.Event())
        deadline = time.monotonic() + 8
        while not gate.wait(0.01):
          if self._shutdown:
            return
          if time.monotonic() > deadline:
            raise RuntimeError('Synthetic ASR gate timed out')
        if path in owner.failures:
          raise RuntimeError('Synthetic ASR failed')
        write_transcript(path, 'New synthetic transcription.')

    gui = self.gui
    self.stack.enter_context(patch.object(gui, 'DesktopController', SyntheticController))
    self.stack.enter_context(patch.object(gui.RepeaterWindow, '_run', run_now))
    self.stack.enter_context(patch.object(gui, 'list_devices', return_value={
      'microphones': [{'name': 'synthetic.mic', 'description': 'Synthetic mic', 'default': True}],
      'outputs': [{'name': 'synthetic.output', 'description': 'Synthetic output', 'default': True}]}))
    self.stack.enter_context(patch.object(gui, 'settings_ready', return_value=None))
    self.sources = []
    original_timeout = gui.GLib.timeout_add_seconds
    def timeout(*args):
      source = original_timeout(*args)
      self.sources.append(source)
      return source
    self.stack.enter_context(patch.object(gui.GLib, 'timeout_add_seconds', side_effect=timeout))
    self.app = gui.Adw.Application(application_id=f'local.TheRepeater.QueueWidgetTest.t{uuid.uuid4().hex}',
                                   flags=gui.Gio.ApplicationFlags.NON_UNIQUE)
    self.app.register(None)
    self.window = gui.RepeaterWindow(self.app)
    self.addCleanup(self.cleanup_window)
    self.window.preferences = replace(self.window.preferences, profile='multitalker-parakeet',
                                      auto_transcribe=False, use_gpu=False)
    self.window.auto_row.set_active(False)
    self.window._check_inference()
    self.window.present()
    self.wait_for(lambda: self.window.get_mapped() and self.window.get_width() > 0
                  and self.window.get_frame_clock().get_frame_counter() > 0)

    self.select(self.finished)
    self.window.controller.transcribe(self.active, self.window.preferences)
    self.wait_for(lambda: self.window.controller.snapshot.transcription_state == 'transcribing')

  def cleanup_window(self):
    self.window.controller.shutdown()
    self.pump()
    for source in self.sources:
      self.gui.GLib.source_remove(source)
    for name in ('edit_save_source', 'autosave_source'):
      source = getattr(self.window, name)
      if source:
        self.gui.GLib.source_remove(source)
    self.window.destroy()

  def pump(self):
    context = self.gui.GLib.MainContext.default()
    for _ in range(500):
      if not context.pending():
        break
      context.iteration(False)

  def wait_for(self, predicate):
    deadline = self.time.monotonic() + 5
    while True:
      self.pump()
      if predicate():
        return
      if self.time.monotonic() > deadline:
        self.fail('Synthetic GTK state did not settle')
      self.time.sleep(0.01)

  def select(self, path):
    row = self.window.history_list.get_first_child()
    while row and row.meeting.path != path:
      row = row.get_next_sibling()
    self.assertIsNotNone(row)
    self.window.history_list.select_row(row)
    self.pump()
    self.assertEqual(self.window.selected_meeting.path, path)
    return row

  def test_recording_navigation_settings_and_unrelated_edits_work_during_asr(self):
    window = self.window
    self.assertTrue(window.record_button.get_sensitive())
    self.assertTrue(window.history_list.get_sensitive())
    self.assertTrue(window.active_collection.get_sensitive())
    self.assertTrue(window.archived_collection.get_sensitive())
    self.assertTrue(window.review_button.get_sensitive())
    self.assertTrue(all(editor.get_editable() for editor in window.transcript_view._editors))
    self.assertTrue(all(button.get_sensitive() for button, _, valid in
                        window.transcript_view._play_buttons.values() if valid))
    window._edit_text(0, 'An unrelated correction during ASR.')
    window._save_edits()
    self.assertIn('unrelated correction', window.transcript_view.text)
    window.settings_button.emit('clicked')
    self.pump()
    self.assertTrue(all(field.get_sensitive() for field in window.settings_fields.values()))
    self.assertIsNotNone(window.settings_page)
    window.navigation.pop()
    window.show_recorder()
    window.title_row.set_text('Concurrent synthetic capture')
    window.record_button.emit('clicked')
    self.wait_for(lambda: window.controller.snapshot.state == 'recording')
    started = window.controller.snapshot.started_at
    captured = window.controller.snapshot.session
    self.assertTrue(window.stop_button.get_sensitive())
    self.select(self.waiting)
    self.assertTrue(window.transcribe_button.get_sensitive())
    window.transcribe_button.emit('clicked')
    self.wait_for(lambda: self.waiting in window.controller.snapshot.queued_sessions)
    self.select(self.finished)
    self.assertEqual(window.stack.get_visible_child_name(), 'detail')
    window.settings_button.emit('clicked')
    settings_page = window.settings_page
    self.releases[self.active].set()
    self.wait_for(lambda: window.controller.snapshot.transcription_session == self.waiting)
    self.assertIs(window.navigation.get_visible_page(), settings_page)
    self.assertTrue(all(field.get_sensitive() for field in window.settings_fields.values()))
    self.assertTrue(window.settings_stop_button.get_sensitive())
    window.navigation.pop()
    self.releases[self.waiting].set()
    self.wait_for(lambda: window.controller.snapshot.transcription_state == 'idle')
    self.assertEqual(window.controller.snapshot.state, 'recording')
    self.assertEqual(window.controller.snapshot.session, captured)
    self.assertEqual(window.controller.snapshot.started_at, started)
    self.assertEqual(window.selected_meeting.path, self.finished)
    self.assertEqual(window.stack.get_visible_child_name(), 'detail')
    self.assertIn('Recording ·', window.detail_job_label.get_text())
    window.stop_button.emit('clicked')
    self.wait_for(lambda: window.controller.snapshot.state == 'idle')

  def test_queued_and_active_sessions_block_mutation_but_not_history(self):
    window = self.window
    row = self.select(self.waiting)
    self.assertTrue(window.transcribe_button.get_sensitive())
    window.transcribe_button.emit('clicked')
    self.wait_for(lambda: self.waiting in window.controller.snapshot.queued_sessions)
    self.assertIn('Queued #1', row.metadata.get_text())
    another = self.fixture('Second waiting synthetic ASR')
    window.refresh_history()
    second_row = self.select(another)
    window.transcribe_button.emit('clicked')
    self.wait_for(lambda: window.controller.snapshot.queued_sessions == (self.waiting, another))
    self.assertIn('Queued #2', second_row.metadata.get_text())
    for path in (self.active, self.waiting, another):
      self.select(path)
      self.assertFalse(window.transcribe_button.get_sensitive())
      self.assertFalse(window.archive_button.get_sensitive())
      self.assertFalse(window.trash_button.get_sensitive())
      self.assertFalse(window.review_button.get_sensitive())
      self.assertTrue(window.history_list.get_sensitive())
      self.assertTrue(all(not editor.get_editable() for editor in window.transcript_view._editors))
      payload = window.edit_states[path].payload
      window._edit_text(0, 'Blocked correction')
      window._toggle_review()
      window._archive_selected()
      window._confirm_trash()
      window.transcribe_selected()
      self.assertEqual(window.edit_states[path].payload, payload)
      self.assertFalse(window.review_mode)
      self.assertTrue(path.exists())
      self.assertFalse(self.json.loads((path / 'session.json').read_text()).get('archived', False))
    self.assertEqual(window.controller.snapshot.queued_sessions, (self.waiting, another))
    self.assertIn('Waiting: 1. Waiting synthetic ASR; 2. Second waiting synthetic ASR', window.job_label.get_text())
    self.select(self.finished)
    self.assertTrue(window.archive_button.get_sensitive())
    self.failures.add(self.active)
    self.releases[self.active].set()
    self.wait_for(lambda: window.controller.snapshot.transcription_session == self.waiting
                  and self.active in window.record_errors)
    self.assertEqual(window.record_errors[self.active], ('transcribe', 'Synthetic ASR failed'))
    self.assertEqual(window.selected_meeting.path, self.finished)
    self.releases[self.waiting].set()
    self.wait_for(lambda: window.controller.snapshot.transcription_session == another)
    self.releases[another].set()
    self.wait_for(lambda: window.controller.snapshot.transcription_state == 'idle')
    self.select(self.waiting)
    self.assertIn('New synthetic transcription.', window.transcript_view.text)
    self.assertTrue(window.transcribe_button.get_sensitive())

  def test_auto_capture_queues_and_close_waits_for_both_lanes_and_owned_shutdown(self):
    window = self.window
    window.show_recorder()
    window.auto_row.set_active(True)
    window.title_row.set_text('Auto queued synthetic capture')
    window.record_button.emit('clicked')
    self.wait_for(lambda: window.controller.snapshot.state == 'recording')
    captured = window.controller.snapshot.session
    window.stop_button.emit('clicked')
    self.wait_for(lambda: captured in window.controller.snapshot.queued_sessions
                         and window.controller.snapshot.state == 'idle')
    row = self.select(captured)
    self.assertIn('Queued #1', row.metadata.get_text())
    self.assertEqual(window.stack.get_visible_child_name(), 'detail')
    window.show_recorder()
    window.title_row.set_text('Closing synthetic capture')
    window.record_button.emit('clicked')
    self.wait_for(lambda: window.controller.snapshot.state == 'recording')
    with patch.object(self.app, 'quit') as quit_app:
      pending = []
      def deferred(_window, work, done=None, failed=None):
        pending.append((work, done))
      with patch.object(self.gui.RepeaterWindow, '_run', deferred):
        window.shutdown()
      self.assertTrue(window.closing)
      quit_app.assert_not_called()
      work, done = pending[-1]
      work()
      self.pump()
      self.assertTrue(window._lanes_finished())
      self.assertFalse(window.controller._thread.is_alive())
      self.assertFalse(window.controller._recording_thread.is_alive())
      quit_app.assert_not_called()
      done(None)
      quit_app.assert_called_once()

  def test_shutdown_keeps_jobs_and_unsaved_corrections_open_after_save_failure(self):
    window = self.window
    self.select(self.finished)
    window._edit_text(0, 'Unsaved synthetic correction.')
    with patch.object(self.gui, 'save_transcript_edit_state', side_effect=OSError('Synthetic save failure')), \
         patch.object(self.app, 'quit') as quit_app:
      window.shutdown()
      self.pump()
      self.assertFalse(window.closing)
      self.assertIn(self.finished, window.edit_dirty)
      self.assertIn('Unsaved synthetic correction.', window.transcript_view.text)
      self.assertEqual(window.controller.snapshot.transcription_state, 'transcribing')
      quit_app.assert_not_called()

if __name__ == '__main__':
  unittest.main()
