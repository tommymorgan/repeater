#!/usr/bin/env python3
"""Resident client subprocess smoke with synthetic paths and no audio devices."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from unittest.mock import patch

from desktop import DesktopController, Preferences
from inference import ModelRuntime, RuntimeCancelled


WORKER = '''import ctypes, json, os, pathlib, signal, sys, time
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
if ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) != 0: sys.exit(2)
if os.getppid() != int(args['--worker-parent']): sys.exit(3)
print('ready', flush=True)
def field():
  result = bytearray()
  while True:
    byte = sys.stdin.buffer.read(1)
    if not byte: return None
    if byte == bytes([0]): return bytes(result).decode('utf-8')
    result.extend(byte)
while True:
  audio = field()
  if audio is None: break
  output = field()
  if audio == 'error':
    print('synthetic inference failure', file=sys.stderr, flush=True)
    print('error', flush=True)
    continue
  if audio == 'busy':
    pathlib.Path(output).write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
  pathlib.Path(output).write_text(json.dumps({'pid': os.getpid(), 'audio': audio}))
  print('ok', flush=True)
'''


class InferenceTests(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory(prefix='parrot-resident-test-')
    self.root = Path(self.temp.name)
    self.worker = self.root / 'worker.py'
    self.worker.write_text(WORKER)
    self.runtime = ModelRuntime()

  def tearDown(self):
    self.runtime.shutdown()
    self.temp.cleanup()

  def command(self, audio, output, *, model='a'):
    return [sys.executable, str(self.worker), '--audio', audio, '--output', str(output), '--model', model]

  def test_reuses_newline_utf8_paths_and_reaps_before_replacement(self):
    first = self.root / 'first\n🎧.json'
    second = self.root / 'second.json'
    self.runtime.execute(self.command('public\n🎧.wav', first))
    data = json.loads(first.read_text())
    self.assertEqual(data['audio'], 'public\n🎧.wav')
    self.runtime.execute(self.command('other.wav', second))
    self.assertEqual(json.loads(second.read_text())['pid'], data['pid'])
    self.runtime.execute(self.command('other.wav', second, model='b'))
    replacement = json.loads(second.read_text())['pid']
    self.assertNotEqual(replacement, data['pid'])
    self.assertFalse(Path(f'/proc/{data["pid"]}').exists())
    self.runtime.shutdown()
    self.runtime.shutdown()
    self.assertFalse(Path(f'/proc/{replacement}').exists())

  def test_error_diagnostics_fail_loudly_and_release_worker(self):
    with self.assertRaisesRegex(ValueError, 'synthetic inference failure'):
      self.runtime.execute(self.command('error', self.root / 'error.json'))
    self.assertIsNone(self.runtime._process)
    self.assertFalse((self.root / 'error.json').exists())

  def test_busy_shutdown_escalates_and_unblocks_request(self):
    marker = self.root / 'busy.pid'
    failures = []
    def execute():
      try:
        self.runtime.execute(self.command('busy', marker))
      except RuntimeCancelled:
        failures.append('cancelled')
    thread = threading.Thread(target=execute)
    thread.start()
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
      time.sleep(0.01)
    self.assertTrue(marker.exists())
    pid = int(marker.read_text())
    started = time.monotonic()
    self.runtime.shutdown()
    thread.join(1)
    self.assertFalse(thread.is_alive())
    self.assertLess(time.monotonic() - started, 6)
    self.assertEqual(failures, ['cancelled'])
    self.assertFalse(Path(f'/proc/{pid}').exists())

  def test_desktop_reuses_resident_model_between_public_queue_requests_and_reaps_on_close(self):
    controller = DesktopController(lambda snapshot: None)
    output = self.root / 'desktop.json'
    def infer(session, **options):
      options['runtime_executor'](self.command('public.wav', output))
    try:
      with patch('desktop.transcribe_session', infer), patch('desktop.settings_ready'), patch('desktop.read_transcript'):
        for iteration in range(2):
          session = self.root / f'session-{iteration}'
          controller.transcribe(session, Preferences())
          deadline = time.monotonic() + 5
          while (controller.snapshot.transcription_session != session or
                 controller.snapshot.transcription_state != 'idle') and time.monotonic() < deadline:
            time.sleep(0.01)
          self.assertEqual(controller.snapshot.transcription_session, session)
          self.assertEqual(controller.snapshot.transcription_state, 'idle')
          current = json.loads(output.read_text())['pid']
          if iteration == 0:
            pid = current
            time.sleep(0.1)
            self.assertTrue(Path(f'/proc/{pid}').exists())
          else:
            self.assertEqual(current, pid)
        controller.shutdown()
        self.assertFalse(Path(f'/proc/{pid}').exists())
    finally:
      controller.shutdown()

  def test_force_exiting_desktop_kills_resident_worker(self):
    output = self.root / 'force-exit.json'
    script = '''import pathlib, sys, time
from unittest.mock import patch
from desktop import DesktopController, Preferences
worker, output = sys.argv[1:]
controller = DesktopController(lambda snapshot: None)
def infer(session, **options):
  options['runtime_executor']([sys.executable, worker, '--audio', 'public.wav', '--output', output, '--model', 'a'])
with patch('desktop.transcribe_session', infer), patch('desktop.settings_ready'), patch('desktop.read_transcript'):
  controller.transcribe(pathlib.Path(output).parent / 'session', Preferences())
  time.sleep(60)
'''
    parent = subprocess.Popen([sys.executable, '-c', script, str(self.worker), str(output)],
                              cwd=Path(__file__).parent, stdin=subprocess.DEVNULL)
    try:
      deadline = time.monotonic() + 5
      while not output.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
      self.assertTrue(output.exists())
      pid = json.loads(output.read_text())['pid']
      parent.kill()
      parent.wait()
      status = Path(f'/proc/{pid}/stat')
      deadline = time.monotonic() + 5
      while status.exists() and status.read_text().split()[2] != 'Z' and time.monotonic() < deadline:
        time.sleep(0.01)
      self.assertTrue(not status.exists() or status.read_text().split()[2] == 'Z')
    finally:
      if parent.poll() is None:
        parent.kill()
      parent.wait()


if __name__ == '__main__':
  unittest.main()
