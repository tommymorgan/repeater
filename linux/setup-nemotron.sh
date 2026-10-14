#!/usr/bin/env bash
# NeMo source: Apache-2.0. Both checkpoints: OpenMDW 1.1 (model cards retained).
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
build="$root/.build/linux"
source_dir="$build/nemo-speech"
commit=50c71dbe1534a89e0d07fe66fbffd205408062ad
asr_revision=ea30d66debe3740a08b573244286791d423d6b3e
diar_revision=f667ed73aee57d40cc39428eb768b4fd87a0a29e
export UV_CACHE_DIR="$build/uv-cache"
export UV_PYTHON_INSTALL_DIR="$build/python"
export UV_PYTHON_BIN_DIR="$build/bin"
export UV_PROJECT_ENVIRONMENT="$build/nemotron-venv"
command -v uv >/dev/null || { printf 'Install uv before running this setup.\n' >&2; exit 1; }
command -v ffmpeg >/dev/null || { printf 'Install ffmpeg before running this setup.\n' >&2; exit 1; }
mkdir -p "$build/models/nemotron-3.5" "$build/models/nemotron-diarization"
exec 9>"$build/nemotron-setup.lock"
flock 9
if [[ ! -d "$source_dir/.git" ]]; then
  git clone --filter=blob:none https://github.com/NVIDIA-NeMo/Speech.git "$source_dir"
  git -C "$source_dir" checkout --detach "$commit"
fi
[[ $(git -C "$source_dir" rev-parse HEAD) == "$commit" ]] || {
  printf 'NeMo checkout differs from required commit %s; refusing to rewrite it.\n' "$commit" >&2
  exit 1
}
uv python install 3.13.14
(cd "$source_dir" && uv sync --python "$build/python/cpython-3.13.14-linux-x86_64-gnu/bin/python3.13" --extra asr --extra cu13 --no-dev --frozen --link-mode copy)
fetch() {
  local repo=$1 revision=$2 filename=$3 directory=$4 sha=$5 destination="$4/$3"
  if [[ -f "$destination" ]] && printf '%s  %s\n' "$sha" "$destination" | sha256sum --check --status; then
    return
  fi
  curl --fail --location --retry 3 "https://huggingface.co/$repo/resolve/$revision/$filename" -o "$destination.part"
  printf '%s  %s\n' "$sha" "$destination.part" | sha256sum --check --status || {
    printf 'Checkpoint hash mismatch: %s\n' "$filename" >&2; exit 1;
  }
  mv -- "$destination.part" "$destination"
}
fetch nvidia/nemotron-3.5-asr-streaming-0.6b "$asr_revision" nemotron-3.5-asr-streaming-0.6b.nemo \
  "$build/models/nemotron-3.5" 210214ed94039bf6bfbb9a047c7fa289628db75b103e2bf6381fa78285436a74
fetch nvidia/Nemotron-3-Diarization "$diar_revision" Nemotron-3-Diarization.nemo \
  "$build/models/nemotron-diarization" 867c53f552998f772e5b5e5c082962ae85ee7ca5669c2bc17d7f615133d4e96d
for item in "nvidia/nemotron-3.5-asr-streaming-0.6b $asr_revision nemotron-3.5" \
            "nvidia/Nemotron-3-Diarization $diar_revision nemotron-diarization"; do
  read -r repo revision directory <<< "$item"
  curl --fail --location --retry 3 "https://huggingface.co/$repo/resolve/$revision/README.md" \
    -o "$build/models/$directory/README.md"
done
curl --fail --location --retry 3 https://openmdw.ai/license/1-1/ -o "$build/models/OPENMDW-1.1.html"
"$build/nemotron-venv/bin/python" -c 'import sys, torch; print("Python", sys.version); print("Torch", torch.__version__, "CUDA", torch.version.cuda); print("CUDA available:", torch.cuda.is_available())'
printf '%s\n' 'Installed local-only coupled Nemotron ASR + eight-speaker V3 runtime.' \
  'GPU execution requires working CUDA; explicit CPU execution is supported but slower and is not a modest-hardware realtime guarantee.' \
  'Inference never downloads models. Eight speakers is a capacity limit, not overlap-recovery assurance.'
