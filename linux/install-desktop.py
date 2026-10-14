#!/usr/bin/env python3
"""Install this checkout’s The Repeater launcher and selected radio icon."""

import os
from pathlib import Path
import sys
import tempfile

from identity import APP_ID, APP_NAME, ICON
ROOT = Path(__file__).resolve().parent.parent


def exec_argument(value: str) -> str:
    escaped = value.replace('\\', '\\\\\\\\').replace('"', '\\\\"')
    escaped = escaped.replace('`', '\\\\`').replace('$', '\\\\$').replace('%', '%%')
    return f'"{escaped}"'


def publish_file(destination: Path, contents: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.parrot-', dir=destination.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), 0o644)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    configured = os.environ.get('XDG_DATA_HOME')
    data = Path(configured).expanduser() if configured else Path.home() / '.local/share'
    if not data.is_absolute():
        print('XDG_DATA_HOME must be absolute.', file=sys.stderr)
        return 1
    application = ROOT / 'linux/gui.py'
    if not application.is_file():
        print(f'GUI entry point is missing: {application}', file=sys.stderr)
        return 1
    directory = data / 'applications'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f'{APP_ID}.desktop'
        marker = f'X-Repeater-Checkout={ROOT}\n'
        if destination.is_symlink() or (destination.exists() and marker not in destination.read_text()):
            print(f'Refusing to replace a launcher not owned by this checkout: {destination}', file=sys.stderr)
            return 1
        icon_destination = data / 'icons/hicolor/512x512/apps' / f'{APP_ID}.png'
        entry = '\n'.join([
            '[Desktop Entry]',
            'Type=Application',
            f'Name={APP_NAME}',
            'Comment=Record meetings and transcribe them locally',
            f'Exec={exec_argument(sys.executable)} {exec_argument(str(application))}',
            f'Icon={icon_destination}',
            'Terminal=false',
            'Categories=AudioVideo;Audio;',
            'StartupNotify=true',
            f'StartupWMClass={APP_ID}',
            marker.rstrip('\n'),
            '',
        ])

        publish_file(icon_destination, ICON.read_bytes())
        publish_file(destination, entry.encode('utf-8'))
        print(f'Installed launcher: {destination}')
        print(f'Open {APP_NAME} from your application menu. Launching does not record audio.')
        return 0
    except OSError as error:
        print(f'Cannot install desktop launcher: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
