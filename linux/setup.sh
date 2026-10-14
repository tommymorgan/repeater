#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
profile=${1:-both}
mode=${2:-cuda}
[[ $# -le 2 && "$profile" =~ ^(both|multitalker-parakeet|nemotron-3.5)$ && "$mode" =~ ^(cpu|cuda|vulkan)$ ]] || {
  printf '%s\n' 'Usage: linux/setup.sh [both|multitalker-parakeet|nemotron-3.5] [cpu|cuda|vulkan]' >&2
  exit 1
}
if [[ "$profile" == both || "$profile" == multitalker-parakeet ]]; then
  bash "$root/linux/setup-parakeet.sh" "$mode"
fi
if [[ "$profile" == both || "$profile" == nemotron-3.5 ]]; then
  bash "$root/linux/setup-nemotron.sh"
fi
printf '%s\n' 'Setup complete. Choose the transcription model manually in Settings or with --profile.'
