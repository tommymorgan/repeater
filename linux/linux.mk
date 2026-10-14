# Linux uses local Python/GTK entry points, never macOS build/signing tools.
PYTHON ?= python3
MODE ?= cuda
PROFILE ?= both
ARGS ?=
.DEFAULT_GOAL := build

.PHONY: build help run gui install-desktop devices record transcribe linux-setup linux-check linux-gtk-check test clean
build:
	@$(PYTHON) -c 'import ast, pathlib; files = [pathlib.Path("linux") / name for name in ("parrot.py", "capture.py", "transcribe.py", "review.py", "desktop.py", "inference.py", "identity.py", "gui.py", "transcript.py", "playback.py", "install-desktop.py", "nemotron-transcribe.py")]; [ast.parse(path.read_text(), filename=str(path)) for path in files]; print("Linux syntax sanity passed. No recording started. Run make gui or make help.")'
	@bash -n linux/setup.sh linux/setup-parakeet.sh linux/setup-nemotron.sh

help:
	@printf '%s\n' \
	  'The Repeater — native GUI, explicit recording and offline transcription' \
	  '  make                         Python syntax/build sanity; does not record' \
	  '  make linux-setup            Install both pinned local models (Parakeet CUDA)' \
	  '  make linux-setup PROFILE=multitalker-parakeet MODE=cpu|cuda|vulkan' \
	  '  make linux-setup PROFILE=nemotron-3.5  Install isolated coupled NeMo runtime' \
	  '  make devices                List microphone/output names; does not record' \
	  '  make gui                    Open the native GTK recording application' \
	  '  make install-desktop        Install The Repeater launcher and radio icon' \
	  '  make run                    Show CLI help; does not record' \
	  '  make run ARGS="record --duration 30 --transcribe --cpu"' \
	  '  make record ARGS="--duration 30"' \
	  '  make transcribe ARGS="/absolute/path/to/session --profile multitalker-parakeet --cpu"' \
	  '  make linux-check            Run Linux behavioral tests (no private audio)' \
	  '  make linux-gtk-check        Run isolated real-widget tests (requires display)' \
	  '  make clean                  Remove Linux inference build cache only' \
	  'See docs/linux-port.md for prerequisites, privacy and remaining port scope.'

gui: install-desktop
	@$(PYTHON) linux/gui.py

install-desktop:
	@$(PYTHON) linux/install-desktop.py

run:
	@$(PYTHON) linux/parrot.py $(ARGS)

devices:
	@$(PYTHON) linux/parrot.py devices

record:
	@$(PYTHON) linux/parrot.py record $(ARGS)

transcribe:
	@$(PYTHON) linux/parrot.py transcribe $(ARGS)

linux-setup:
	@bash linux/setup.sh "$(PROFILE)" "$(MODE)"

linux-check:
	@$(PYTHON) linux/test-capture.py
	@$(PYTHON) linux/test-transcribe.py
	@$(PYTHON) linux/test-nemotron.py
	@$(PYTHON) linux/test-desktop.py
	@$(PYTHON) linux/test-inference.py
	@$(PYTHON) linux/test-playback.py
	@$(PYTHON) linux/test-review.py

linux-gtk-check:
	@$(PYTHON) linux/test-gui.py

test: linux-check

clean:
	rm -rf .build/linux
