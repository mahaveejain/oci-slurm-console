#!/usr/bin/env bash
#SBATCH --job-name=gpu-fryer-120s
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --exclusive
#SBATCH --time=00:20:00
set -euo pipefail

if [[ -z "${SLURM_JOB_ID:-}" ]]; then
  echo 'ERROR: gpu-fryer must run in a Slurm allocation.' >&2
  exit 1
fi
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo 'ERROR: nvidia-smi is unavailable on this GPU node.' >&2
  exit 1
fi
printf 'Node: %s\nJob: %s\nVisible GPUs: %s\n' "$(hostname)" "$SLURM_JOB_ID" "${CUDA_VISIBLE_DEVICES:-all allocated GPUs}"
nvidia-smi -L

fryer="$(command -v gpu-fryer || true)"
if [[ -z "$fryer" && -x "$HOME/.cargo/bin/gpu-fryer" ]]; then
  fryer="$HOME/.cargo/bin/gpu-fryer"
fi
if [[ -z "$fryer" ]]; then
  echo 'gpu-fryer is not installed; installing it for the current user.'
  if ! command -v cargo >/dev/null 2>&1 && [[ ! -x "$HOME/.cargo/bin/cargo" ]]; then
    echo 'Rust/Cargo is not installed; downloading the official rustup installer.'
    installer="$(mktemp /tmp/gpu-fryer-rustup.XXXXXXXX.sh)"
    trap 'rm -f -- "$installer"' EXIT
    curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs -o "$installer"
    sh "$installer" -y --no-modify-path
  fi
  if [[ -f "$HOME/.cargo/env" ]]; then
    set +u
    source "$HOME/.cargo/env"
    set -u
  fi
  cargo="$(command -v cargo || true)"
  if [[ -z "$cargo" && -x "$HOME/.cargo/bin/cargo" ]]; then cargo="$HOME/.cargo/bin/cargo"; fi
  if [[ -z "$cargo" ]]; then echo 'ERROR: Cargo is unavailable after Rust setup.' >&2; exit 1; fi
  echo 'Building gpu-fryer from the Hugging Face GitHub repository.'
  "$cargo" install --git https://github.com/huggingface/gpu-fryer.git
  fryer="$HOME/.cargo/bin/gpu-fryer"
fi
if [[ ! -x "$fryer" ]]; then echo "ERROR: gpu-fryer executable not found: $fryer" >&2; exit 1; fi
printf 'Running %s for 120 seconds.\n' "$fryer"
nvml_args=()
if [[ "$(uname -m)" == aarch64 && -f /usr/lib/aarch64-linux-gnu/libnvidia-ml.so.1 ]]; then
  nvml_args=(--nvml-lib-path /usr/lib/aarch64-linux-gnu/libnvidia-ml.so.1)
fi
"$fryer" "${nvml_args[@]}" 120
