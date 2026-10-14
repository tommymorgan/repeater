"""One fixed-configuration resident bridge, owned by a persistent desktop thread."""

import os
import subprocess
import tempfile
import threading


class RuntimeCancelled(Exception):
  pass


class ModelRuntime:
  def __init__(self):
    self._lock = threading.Lock()
    self._requests = threading.Lock()
    self._closing = threading.Lock()
    self._stopping = threading.Lock()
    self._closed = threading.Event()
    self._process = None
    self._errors = None
    self._config = None

  @staticmethod
  def _request(command):
    config = []
    paths = {}
    index = 0
    while index < len(command):
      argument = command[index]
      if argument in ('--audio', '--output'):
        if argument in paths or index + 1 == len(command):
          raise ValueError(f'Invalid resident command: {argument}')
        paths[argument] = command[index + 1]
        index += 2
      else:
        config.append(argument)
        index += 1
    if set(paths) != {'--audio', '--output'}:
      raise ValueError('Resident command needs audio and output paths')
    audio, output = paths['--audio'], paths['--output']
    if '\x00' in audio or '\x00' in output:
      raise ValueError('Resident paths cannot contain NUL')
    return tuple(config), (audio + '\x00' + output + '\x00').encode('utf-8')

  def _diagnostic(self):
    self._errors.seek(0, os.SEEK_END)
    size = self._errors.tell()
    self._errors.seek(max(0, size - 8192))
    return self._errors.read().decode('utf-8', errors='replace').strip() or 'No diagnostic output.'

  def _stop(self, process):
    with self._stopping:
      self._stop_locked(process)

  @staticmethod
  def _stop_locked(process):
    if process.stdin and not process.stdin.closed:
      try:
        process.stdin.close()
      except OSError:
        pass
    for timeout, action in ((2, None), (2, process.terminate), (2, process.kill)):
      if action is not None and process.poll() is None:
        try:
          action()
        except ProcessLookupError:
          pass
      try:
        process.wait(timeout=timeout)
        return
      except subprocess.TimeoutExpired:
        continue
    process.kill()
    process.wait()

  def _release(self):
    with self._lock:
      process = self._process
    if process is not None:
      self._stop(process)
      process.stdout.close()
      self._errors.close()
      with self._lock:
        self._process = None
        self._errors = None
        self._config = None

  def execute(self, command):
    config, request = self._request(command)
    with self._requests:
      if self._closed.is_set():
        raise RuntimeCancelled('Transcription cancelled')
      if self._config != config:
        self._release()
      try:
        with self._lock:
          if self._closed.is_set():
            raise RuntimeCancelled('Transcription cancelled')
          if self._process is None:
            errors = tempfile.TemporaryFile()
            try:
              process = subprocess.Popen([*config, '--worker-parent', str(os.getpid())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, bufsize=0)
            except BaseException:
              errors.close()
              raise
            self._process, self._errors, self._config = process, errors, config
            starting = True
          else:
            process = self._process
            starting = False
        if starting and process.stdout.readline() != b'ready\n':
          raise ValueError(f'Resident model failed to become ready: {self._diagnostic()}')
        if self._closed.is_set():
          raise RuntimeCancelled('Transcription cancelled')
        process.stdin.write(request)
        process.stdin.flush()
        response = process.stdout.readline()
        if self._closed.is_set():
          raise RuntimeCancelled('Transcription cancelled')
        if response == b'error\n':
          raise ValueError(f'Resident inference failed: {self._diagnostic()}')
        if response != b'ok\n':
          raise ValueError(f'Resident model protocol failed ({response!r}): {self._diagnostic()}')
        return subprocess.CompletedProcess(command, 0, '', '')
      except (OSError, ValueError):
        if self._closed.is_set():
          raise RuntimeCancelled('Transcription cancelled') from None
        self._release()
        raise

  def shutdown(self):
    self._closed.set()
    with self._closing:
      with self._lock:
        process = self._process
      if process is not None:
        self._stop(process)
      with self._requests:
        self._release()
