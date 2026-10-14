#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
BUILD="$ROOT/.build/linux"
SOURCE="$BUILD/transcribe.cpp"
REVISION=63a44d9239d610b3908e8a66b384924cd4a77217
MODEL_REVISION=a9a7208d8f205b5816770a6f7fb83afc81a7691b
MODEL_HASH=cf83cedc6777d2528a4d2a8a4bf476174ef7e5c65b86bc6c955f4cc9f5951ac5
MODEL="$BUILD/models/parakeet-bundle.gguf"
BACKEND=${1:-cuda}
case "$BACKEND" in
  cpu|cuda|vulkan) ;;
  *) echo 'Usage: linux/setup-parakeet.sh [cpu|cuda|vulkan]' >&2; exit 2 ;;
esac
for executable in git curl cmake gcc sha256sum; do
  command -v "$executable" >/dev/null || { echo "Required command missing: $executable" >&2; exit 1; }
done
if [[ "$BACKEND" == cuda ]]; then
  command -v nvcc >/dev/null || { echo 'CUDA setup requires nvcc. Select cpu or vulkan explicitly instead.' >&2; exit 1; }
fi
mkdir -p "$BUILD/models" "$BUILD/bin" "$BUILD/parakeet-package"
if [[ ! -d "$SOURCE" ]]; then
  git clone --no-checkout https://github.com/handy-computer/transcribe.cpp "$SOURCE"
  git -C "$SOURCE" checkout --detach "$REVISION"
fi
if [[ $(git -C "$SOURCE" rev-parse HEAD) != "$REVISION" ]] || [[ -n $(git -C "$SOURCE" status --porcelain --untracked-files=no) ]]; then
  echo "Existing runtime source is modified or not pinned to $REVISION; refusing to rewrite it." >&2
  exit 1
fi
git -C "$SOURCE" submodule update --init --recursive
if [[ ! -f "$MODEL" ]]; then
  PART=$(mktemp "$BUILD/models/parakeet-bundle.download.XXXXXX")
  trap 'rm -f "$PART"' EXIT
  curl --fail --location --retry 3 "https://huggingface.co/handy-computer/multitalker-parakeet-streaming-0.6b-v1-gguf/resolve/$MODEL_REVISION/bundle/multitalker-parakeet-streaming-0.6b-v1-Q8_0.gguf" -o "$PART"
  [[ $(stat -c %s "$PART") == 873443456 ]] || { echo 'Model size mismatch.' >&2; exit 1; }
  printf '%s  %s\n' "$MODEL_HASH" "$PART" | sha256sum --check --status
  mv "$PART" "$MODEL"
  trap - EXIT
fi
printf '%s  %s\n' "$MODEL_HASH" "$MODEL" | sha256sum --check --status || { echo 'Bundled model hash mismatch; existing artifact was not changed.' >&2; exit 1; }
RUNTIME="$BUILD/parakeet-runtime"
CUDA=OFF; VULKAN=OFF
[[ "$BACKEND" != cuda ]] || CUDA=ON
[[ "$BACKEND" != vulkan ]] || VULKAN=ON
CUDA_OPTIONS=()
[[ "$BACKEND" != cuda ]] || CUDA_OPTIONS=(-DCMAKE_CUDA_ARCHITECTURES="${PARAKEET_CUDA_ARCHITECTURES:-native}")
cmake -S "$SOURCE" -B "$RUNTIME" -DCMAKE_BUILD_TYPE=Release \
  -DTRANSCRIBE_BUILD_SHARED=ON -DTRANSCRIBE_BUILD_TESTS=OFF \
  -DTRANSCRIBE_BUILD_EXAMPLES=OFF -DTRANSCRIBE_INSTALL=ON \
  -DTRANSCRIBE_CUDA="$CUDA" -DTRANSCRIBE_VULKAN="$VULKAN" \
  -DCMAKE_INSTALL_PREFIX="$BUILD/parakeet-package" "${CUDA_OPTIONS[@]}"
cmake --build "$RUNTIME" --parallel "${PARAKEET_BUILD_JOBS:-4}"
cmake --install "$RUNTIME"
LIB="$BUILD/parakeet-package/lib"
[[ -f "$LIB/libtranscribe.so" ]] || LIB="$BUILD/parakeet-package/lib64"
gcc -std=c11 -O2 -Wall -Wextra -Werror -I "$SOURCE/include" \
  "$ROOT/linux/native-transcribe.c" -L "$LIB" -ltranscribe -lcrypto \
  -Wl,-rpath,"$LIB" -Wl,-rpath-link,"$LIB" \
  -o "$BUILD/bin/parakeet-transcribe"
mkdir -p "$BUILD/parakeet-package/notices"
cp "$SOURCE/LICENSE" "$BUILD/parakeet-package/notices/transcribe-LICENSE"
cp "$SOURCE/ggml/LICENSE" "$BUILD/parakeet-package/notices/ggml-LICENSE"
cp "$SOURCE/THIRD-PARTY-LICENSES.md" "$BUILD/parakeet-package/notices/third-party-licenses.txt"
curl --fail --location --retry 3 "https://huggingface.co/handy-computer/multitalker-parakeet-streaming-0.6b-v1-gguf/resolve/$MODEL_REVISION/README.md" -o "$BUILD/parakeet-package/notices/model-card.txt"
printf 'Runtime source: https://github.com/handy-computer/transcribe.cpp\nRuntime revision: %s\nModel revision: %s\nModel SHA256: %s\nModel license: NVIDIA Open Model License\nhttps://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/\nSpeaker capacity: 4, not extensible by selecting a backend\nBackend build: %s\n' "$REVISION" "$MODEL_REVISION" "$MODEL_HASH" "$BACKEND" > "$BUILD/parakeet-package/provenance.txt"
sha256sum "$BUILD/bin/parakeet-transcribe" "$MODEL" "$SOURCE/include/transcribe.h" "$LIB"/*.so* > "$BUILD/parakeet-package/artifact-sha256.txt"
printf 'Ready: %s\nModel: %s\nRuntime is independent of Handy.\n' "$BUILD/bin/parakeet-transcribe" "$MODEL"
