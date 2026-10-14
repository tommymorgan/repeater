"""Private, local PipeWire capture with arrival-clock track alignment."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from datetime import datetime, timezone

SAMPLE_RATE = 16000
_STARTUP_TIMEOUT = 8.0
_STALL_TIMEOUT = 5.0
_SILENCE = bytes(SAMPLE_RATE * 2)


class CaptureError(Exception):
    """A recording could not be started or completed safely."""


def _command(args: list[str]) -> str:
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True,
                              timeout=3).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise CaptureError(f'{args[0]} failed: {error}') from error


def _graph() -> list[dict]:
    try:
        graph = json.loads(_command(['pw-dump']))
        if not isinstance(graph, list):
            raise ValueError('expected a JSON list')
        return graph
    except (ValueError, TypeError) as error:
        raise CaptureError(f'Invalid pw-dump response: {error}') from error


def list_devices() -> dict:
    """Return stable PipeWire source/sink names, descriptions and defaults."""
    graph = _graph()
    defaults = {}
    for kind in ('source', 'sink'):
        try:
            defaults[kind] = _command(['pactl', f'get-default-{kind}']).strip()
        except CaptureError:
            defaults[kind] = None
    devices = {'microphones': [], 'outputs': []}
    for node in graph:
        if node.get('type') != 'PipeWire:Interface:Node':
            continue
        props = node.get('info', {}).get('props', {})
        category = props.get('media.class')
        if category not in ('Audio/Source', 'Audio/Sink') or not props.get('node.name'):
            continue
        name = props['node.name']
        key, kind = ('microphones', 'source') if category == 'Audio/Source' else ('outputs', 'sink')
        devices[key].append({'name': name,
                             'description': props.get('node.description') or props.get('node.nick') or name,
                             'default': name == defaults[kind]})
    for entries in devices.values():
        entries.sort(key=lambda entry: entry['name'])
    return devices


def _device(entries: list[dict], requested: str | None, kind: str) -> str:
    if requested is not None:
        if any(entry['name'] == requested for entry in entries):
            return requested
        raise CaptureError(f'Unknown {kind}: {requested}. Use the devices command to list names.')
    default = next((entry['name'] for entry in entries if entry['default']), None)
    if default is None:
        raise CaptureError(f'No default {kind}; select an explicit device name.')
    return default


def silence_owed(elapsed: float, written: int, incoming: int) -> int:
    """Match AudioCaptureManager: place startup exactly, ignore <=1s jitter.

    Arrival timestamps are approximate within backend latency, not sample-exact.
    A backend replaying a backlog after a stall can run ahead of this clock.
    """
    owed = int(elapsed * SAMPLE_RATE) - written - incoming
    return owed if owed > (0 if written == 0 else SAMPLE_RATE) else 0


class _Track:
    def __init__(self, path: Path, metadata: dict):
        self.metadata = metadata
        self.file = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb')
        self.wav = wave.open(self.file, 'wb')
        self.wav.setparams((1, 2, SAMPLE_RATE, 0, 'NONE', 'not compressed'))
        self.pending = b''
        self.received = 0
        self.last_arrival = None
        self.process = None
        self.node_name = ''
        self.stderr = tempfile.TemporaryFile()

    def pad(self, frames: int) -> None:
        if self.metadata['frames'] + frames > (0xffffffff - 36) // 2:
            raise CaptureError('Recording exceeds the 4 GiB WAV format limit.')
        while frames > 0:
            count = min(frames, SAMPLE_RATE)
            self.wav.writeframesraw(_SILENCE[:count * 2])
            self.metadata['frames'] += count
            frames -= count

    def append(self, data: bytes, elapsed: float) -> None:
        data = self.pending + data
        complete = len(data) // 2 * 2
        self.pending = data[complete:]
        if not complete:
            return
        frames = complete // 2
        self.pad(silence_owed(elapsed, self.metadata['frames'], frames))
        if self.metadata['frames'] + frames > (0xffffffff - 36) // 2:
            raise CaptureError('Recording exceeds the 4 GiB WAV format limit.')
        self.wav.writeframesraw(data[:complete])
        self.metadata['frames'] += frames
        self.received += frames
        self.last_arrival = elapsed

    def close(self) -> None:
        try:
            self.wav.close()
        finally:
            self.file.close()
            self.stderr.close()


def _manifest(output: Path, session: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix='.session-', dir=output)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(session, handle, indent=2, allow_nan=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output / 'session.json')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _linked(graph: list[dict], track: _Track) -> bool:
    nodes = {node.get('info', {}).get('props', {}).get('node.name'): node['id']
             for node in graph if node.get('type') == 'PipeWire:Interface:Node'}
    capture_id = nodes.get(track.node_name)
    target_id = nodes.get(track.metadata['device'])
    if capture_id is None or target_id is None:
        return False
    node_ids = {node['id'] for node in graph
                if node.get('type') == 'PipeWire:Interface:Node'}
    routes = {}
    for node in graph:
        if node.get('type') != 'PipeWire:Interface:Link':
            continue
        info = node.get('info', {})
        source, destination = info.get('output-node-id'), info.get('input-node-id')
        if (source in node_ids and destination in node_ids
                and info.get('state') in ('active', 'paused')):
            routes.setdefault(source, set()).add(destination)
    # A sink's echo-reference path must not be mistaken for its output monitor.
    if track.metadata['source'] == 'them':
        return capture_id in routes.get(target_id, ())
    # Microphone processing may insert several nodes before the capture stream.
    pending = [target_id]
    visited = {target_id}
    while pending:
        for destination in routes.get(pending.pop(), ()):
            if destination == capture_id:
                return True
            if destination not in visited:
                visited.add(destination)
                pending.append(destination)
    return False


def _stop(tracks: list[_Track]) -> None:
    children = [track.process for track in tracks if track.process is not None]
    unexpected_exit = any(process.poll() is not None for process in children)
    forced_shutdown = False
    for process in children:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
    deadline = time.monotonic() + 2
    for process in children:
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            forced_shutdown = True
            process.kill()
            process.wait(timeout=2)
        if process.stdout is not None:
            process.stdout.close()
    if unexpected_exit:
        raise CaptureError('Capture child exited before recording shutdown.')
    if forced_shutdown:
        raise CaptureError('Capture child required forced shutdown.')


def record_session(output: Path, *, title: str, microphone: str | None,
                   system: str | None, duration: float | None) -> dict:
    """Record two mono WAVs; Ctrl+C/SIGTERM finalize normally on the main thread.

    output must not exist and its parent must exist. Device selection is checked
    before creating output. Failures preserve a failed manifest and usable WAVs.
    """
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise CaptureError('Duration must be finite and greater than zero.')
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise CaptureError(f'Refusing to overwrite existing output: {output}')
    devices = list_devices()
    mic = _device(devices['microphones'], microphone, 'microphone')
    sink = _device(devices['outputs'], system, 'output sink')
    session = {'schemaVersion': 1, 'id': str(uuid.uuid4()), 'title': title,
               'startedAt': datetime.now(timezone.utc).isoformat(),
               'sampleRate': SAMPLE_RATE, 'channels': 1, 'status': 'recording',
               'durationSeconds': 0.0,
               'tracks': [{'source': 'me', 'file': 'mic.wav', 'device': mic, 'frames': 0},
                          {'source': 'them', 'file': 'system.wav', 'device': sink, 'frames': 0}]}
    try:
        output.mkdir(mode=0o700)
    except OSError as error:
        raise CaptureError(f'Cannot create private recording directory {output}: {error}') from error
    tracks = []
    selector = selectors.DefaultSelector()
    origin = time.monotonic()
    stopped = False
    error = None
    old_handlers = {}

    def stop_signal(signum, frame):
        nonlocal stopped
        stopped = True

    try:
        _manifest(output, session)
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                old_handlers[signum] = signal.signal(signum, stop_signal)
        for metadata in session['tracks']:
            track = _Track(output / metadata['file'], metadata)
            tracks.append(track)
        for track in tracks:
            metadata = track.metadata
            track.node_name = f'parrot.capture.{session["id"]}.{metadata["source"]}'
            properties = {'node.name': track.node_name, 'node.dont-reconnect': True,
                          'node.dont-fallback': True}
            if metadata['source'] == 'them':
                properties['stream.capture.sink'] = True
            track.process = subprocess.Popen(
                ['pw-record', '--raw', '--rate', str(SAMPLE_RATE), '--channels', '1',
                 '--format', 's16', '--target', metadata['device'],
                 '--properties', json.dumps(properties), '-'],
                stdout=subprocess.PIPE, stderr=track.stderr)
            os.set_blocking(track.process.stdout.fileno(), False)
            selector.register(track.process.stdout, selectors.EVENT_READ, track)
        next_graph = 0.0
        connected = set()
        while not stopped:
            elapsed = time.monotonic() - origin
            for track in tracks:
                if track.process.poll() is not None:
                    track.stderr.seek(0)
                    detail = track.stderr.read(4096).decode(errors='replace').strip()
                    raise CaptureError(f'{track.metadata["source"]} capture exited unexpectedly '
                                       f'({track.process.returncode}): {detail}')
                if track.last_arrival is None and elapsed > _STARTUP_TIMEOUT:
                    raise CaptureError(f'{track.metadata["source"]} capture produced no audio frames.')
                if track.last_arrival is not None and elapsed - track.last_arrival > _STALL_TIMEOUT:
                    raise CaptureError(f'{track.metadata["source"]} capture stopped delivering audio frames.')
            if elapsed >= next_graph:
                graph = _graph()
                for track in tracks:
                    if _linked(graph, track):
                        connected.add(track.node_name)
                    elif track.node_name in connected or elapsed > _STARTUP_TIMEOUT:
                        raise CaptureError(f'{track.metadata["source"]} capture is not linked to '
                                           f'{track.metadata["device"]}.')
                next_graph = time.monotonic() - origin + (1.0 if len(connected) == 2 else 0.1)
            if duration is not None and elapsed >= duration:
                break
            timeout = 0.1 if duration is None else min(0.1, max(0, duration - elapsed))
            for key, _ in selector.select(timeout):
                data = os.read(key.fd, 65536)
                if not data:
                    raise CaptureError(f'{key.data.metadata["source"]} capture stream closed.')
                key.data.append(data, time.monotonic() - origin)
        for track in tracks:
            if not track.received or track.node_name not in connected:
                raise CaptureError(f'{track.metadata["source"]} capture never became ready; '
                                   'record longer or check the selected device.')
            if track.pending:
                raise CaptureError(f'{track.metadata["source"]} capture ended with an incomplete sample.')
        session['status'] = 'recorded'
    except (Exception, KeyboardInterrupt) as caught:
        error = caught
        session['status'] = 'failed'
    finally:
        elapsed = time.monotonic() - origin
        session['durationSeconds'] = elapsed if duration is None else min(elapsed, duration)
        try:
            _stop(tracks)
            if session['status'] == 'recorded':
                end_frames = max(int(session['durationSeconds'] * SAMPLE_RATE),
                                 *(track.metadata['frames'] for track in tracks))
                for track in tracks:
                    track.pad(end_frames - track.metadata['frames'])
                session['durationSeconds'] = end_frames / SAMPLE_RATE
        except Exception as caught:
            error = error or caught
            session['status'] = 'failed'
        finally:
            selector.close()
            for track in tracks:
                try:
                    track.close()
                except Exception as caught:
                    error = error or caught
                    session['status'] = 'failed'
            for signum, previous in old_handlers.items():
                signal.signal(signum, previous)
            _manifest(output, session)
    if error is not None:
        raise CaptureError(f'Recording failed; partial tracks preserved in {output}: {error}') from error
    return session
