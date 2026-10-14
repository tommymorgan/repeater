#!/usr/bin/env python3
"""Explicit, local-only recording and offline transcription on Linux."""

import argparse
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import sys
import uuid

from transcribe import DEFAULT_PROFILE, PROFILE_DEFAULTS


def positive_duration(value: str) -> float:
    try:
        duration = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('duration must be a positive finite number') from None
    if not math.isfinite(duration) or duration <= 0:
        raise argparse.ArgumentTypeError('duration must be a positive finite number')
    return duration


def positive_threads(value: str) -> int:
    try:
        threads = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('threads must be a positive integer') from None
    if threads <= 0:
        raise argparse.ArgumentTypeError('threads must be a positive integer')
    return threads


def inference_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--profile', choices=tuple(PROFILE_DEFAULTS), default=DEFAULT_PROFILE,
                        help='manual model choice: Parakeet supports 4 speakers, Nemotron supports 8 per source')
    parser.add_argument('--model', type=Path, help='local model checkpoint; defaults to the selected profile')
    parser.add_argument('--runtime', type=Path, help='native executable or isolated Python; defaults to the selected profile')
    parser.add_argument('--diarizer', type=Path, help='Nemotron diarization checkpoint; not used by Parakeet')
    parser.add_argument('--language', default='en', help='language locale, for example en-US; Parakeet is English-only')
    parser.add_argument('--threads', type=positive_threads, default=4, help='CPU threads (default: 4)')
    parser.add_argument('--cpu', action='store_true', help='disable GPU inference')


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Parrot Linux: two-track local recording and offline transcription. No cloud calls; no recording without the record command.')
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('devices', help='list microphone and output names without recording')
    record = commands.add_parser('record', help='explicitly start recording; Ctrl+C stops and saves')
    record.add_argument('--output', type=Path, help='new session directory; parent must already exist')
    record.add_argument('--title', default='Meeting', help='meeting title (default: Meeting)')
    record.add_argument('--microphone', help='stable microphone name from devices (default: default microphone)')
    record.add_argument('--system', help='stable output name from devices (default: default output monitor)')
    record.add_argument('--duration', type=positive_duration, help='stop after this many seconds; otherwise Ctrl+C')
    record.add_argument('--transcribe', action='store_true', help='transcribe locally after stopping; preflight inference options before capture')
    inference_arguments(record)
    transcribe = commands.add_parser('transcribe', help='transcribe an existing session locally')
    transcribe.add_argument('session', type=Path, help='session directory containing session.json and both WAV tracks')
    inference_arguments(transcribe)
    return parser


def meetings_root() -> Path:
    configured = os.environ.get('XDG_DATA_HOME')
    base = Path(configured).expanduser() if configured else Path.home() / '.local/share'
    if not base.is_absolute():
        raise ValueError('XDG_DATA_HOME must be an absolute path; refusing to store recordings relative to the working directory.')
    parent = base / 'parrot/meetings'
    missing = []
    directory = parent
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
    # Existing directories may be shared; the session itself is created privately by capture.
    return parent


def recording_path(output: Path | None) -> Path:
    if output is not None:
        path = output.expanduser().absolute()
        if not path.parent.is_dir():
            raise ValueError(f'Output parent does not exist: {path.parent}. Create it first with mkdir -p, then retry.')
        return path
    parent = meetings_root()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    return parent / f'{stamp}-{uuid.uuid4()}'


def show_devices() -> None:
    from capture import list_devices
    devices = list_devices()
    for key, heading in (('microphones', 'Microphones'), ('outputs', 'Outputs (system-audio monitors)')):
        print(f'\n{heading}')
        rows = devices[key]
        if not rows:
            print('  No devices found.')
            continue
        width = max(len('NAME'), *(len(item['name']) for item in rows))
        print(f"  {'DEFAULT':7}  {'NAME':{width}}  DESCRIPTION")
        for item in rows:
            default = '*' if item['default'] else ''
            print(f"  {default:7}  {item['name']:{width}}  {item['description']}")
    print('\nUse the NAME value with --microphone or --system; * marks the default.')


def inference_options(args: argparse.Namespace) -> dict:
    return {'profile': args.profile, 'model': args.model.expanduser() if args.model else None,
            'runtime': args.runtime.expanduser() if args.runtime else None,
            'diarizer': args.diarizer.expanduser() if args.diarizer else None,
            'language': args.language, 'threads': args.threads, 'use_gpu': not args.cpu}


def transcribe(args: argparse.Namespace, session: Path) -> None:
    from transcribe import transcribe_session
    print(f'Transcribing locally: {session}', flush=True)
    transcribe_session(session, **inference_options(args))
    print(f'Local transcript saved: {session / "transcript.json"}')
    print(f'Text: {session / "transcript.txt"}\nSubtitles: {session / "transcript.srt"}')


def main(argv: list[str] | None = None) -> int:
    parser = argument_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    from capture import CaptureError, record_session
    from transcribe import TranscriptionError, validate_options
    try:
        if args.command == 'devices':
            show_devices()
        elif args.command == 'transcribe':
            transcribe(args, args.session.expanduser().absolute())
        else:
            if args.transcribe:
                validate_options(**inference_options(args))
            try:
                output = recording_path(args.output)
            except (ValueError, OSError) as error:
                print(f'parrot: {error}', file=sys.stderr)
                return 1
            print('Warning: the output monitor captures ALL audio routed to the selected output, not just a meeting.', file=sys.stderr, flush=True)
            print('Use headphones: Linux capture has no acoustic echo cancellation (AEC). Obtain recording consent.', file=sys.stderr, flush=True)
            print(f'Starting recording. Local storage: {output}', flush=True)
            print('Press Ctrl+C to stop and save.' if args.duration is None else f'Stopping automatically after {args.duration:g} seconds; Ctrl+C also stops and saves.', flush=True)
            manifest = record_session(output, title=args.title, microphone=args.microphone, system=args.system, duration=args.duration)
            print(f'Recording stopped ({manifest["durationSeconds"]:.2f} seconds). Local session saved: {output}', flush=True)
            if args.transcribe:
                transcribe(args, output)
        return 0
    except (CaptureError, TranscriptionError) as error:
        print(f'parrot: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('parrot: interrupted.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
