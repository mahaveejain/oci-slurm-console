#!/usr/bin/env bash
# Submitted as stdin to sbatch: this script is copied by Slurm, not read from
# a controller-only path by the compute node. Logs must use shared storage.
set -euo pipefail
echo "Job ${SLURM_JOB_ID}: node $(hostname), CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-unset}"
if [[ -z "${SLURM_JOB_ID:-}" || -z "${CUDA_VISIBLE_DEVICES:-}" ]]; then
  echo "ERROR: run through Slurm with one allocated GPU." >&2
  exit 1
fi
nvcc_path="$(command -v nvcc || true)"
if [[ -z "$nvcc_path" && -x /usr/local/cuda/bin/nvcc ]]; then
  nvcc_path=/usr/local/cuda/bin/nvcc
fi
if [[ -z "$nvcc_path" ]]; then
  echo "ERROR: CUDA nvcc compiler is missing on this node." >&2
  exit 1
fi
work_dir="$(mktemp -d /tmp/oci-gpu-test.XXXXXXXX)"
trap 'rm -f -- "$work_dir/test.cu" "$work_dir/test"; rmdir -- "$work_dir"' EXIT
cat > "$work_dir/test.cu" <<'CUDA'
#include <cuda_runtime.h>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>

void check(cudaError_t result) {
  if (result != cudaSuccess) {
    fprintf(stderr, "CUDA ERROR: %s\n", cudaGetErrorString(result));
    exit(1);
  }
}
__global__ void compute(float* output) {
  int i = blockIdx.x * blockDim.x + threadIdx.x;
  float x = 0.5f + (i % 100) * 0.001f;
  for (int j = 0; j < 4096; ++j) x = fmaf(x, 0.99999f, 0.00001f);
  output[i] = x;
}
int main() {
  setvbuf(stdout, nullptr, _IONBF, 0);
  int count = 0;
  check(cudaGetDeviceCount(&count));
  if (count != 1) {
    fprintf(stderr, "ERROR: expected exactly one Slurm-visible GPU, got %d\n", count);
    return 1;
  }
  check(cudaSetDevice(0)); // Device zero is the GPU exposed by Slurm, not host GPU zero.
  cudaDeviceProp prop;
  check(cudaGetDeviceProperties(&prop, 0));
  printf("GPU: %s; starting 30-second CUDA compute test\n", prop.name);
  float* output = nullptr;
  check(cudaMalloc(&output, 262144 * sizeof(float)));
  using clock = std::chrono::steady_clock;
  auto start = clock::now();
  double elapsed = 0;
  int next_report = 5;
  unsigned long launches = 0;
  do {
    compute<<<1024, 256>>>(output);
    check(cudaGetLastError());
    check(cudaDeviceSynchronize());
    ++launches;
    elapsed = std::chrono::duration<double>(clock::now() - start).count();
    if (elapsed >= next_report) {
      printf("GPU compute: %.1f / 30 seconds\n", elapsed);
      next_report += 5;
    }
  } while (elapsed < 30);
  float sample = 0;
  check(cudaMemcpy(&sample, output, sizeof(float), cudaMemcpyDeviceToHost));
  check(cudaFree(output));
  if (!std::isfinite(sample) || sample <= 0.5f || sample >= 1.0f) {
    fprintf(stderr, "FAIL: unexpected GPU result %f\n", sample);
    return 1;
  }
  printf("PASS: %.2f seconds of GPU compute, %lu kernel launches, result=%f\n", elapsed, launches, sample);
  return 0;
}
CUDA
echo "Compiling GPU test (maximum 60 seconds)…"
timeout 60 "$nvcc_path" -O2 -arch=native "$work_dir/test.cu" -o "$work_dir/test"
"$work_dir/test"
