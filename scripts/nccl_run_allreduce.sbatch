#!/usr/bin/env bash
#SBATCH --job-name=nccl-allreduce-slurm
#SBATCH --nodes=2
#SBATCH --gpus-per-node=8
#SBATCH --ntasks-per-node=8
#SBATCH --exclusive
set -euo pipefail
export PMI_DEBUG=1
export NCCL_DEBUG=WARN
export SLURM_EXPORT_ENV=ALL
exec_cmd=/opt/oci-hpc/nccl-test/build/all_reduce_perf
if [[ ! -x "$exec_cmd" ]]; then echo "NCCL executable not found: $exec_cmd" >&2; exit 1; fi
log_dir="${SLURM_CONSOLE_NCCL_LOG_DIR:-/config/logs/slurm-console-nccl}"
work_dir="$(mktemp -d "$log_dir/nccl-allreduce.XXXXXXXX")"
trap 'rm -rf -- "$work_dir"' EXIT
cd "$work_dir"
scontrol show hostnames "$SLURM_JOB_NODELIST" > hostfile
printf 'NCCL nodes:\n'; cat hostfile
/opt/oci-hpc/bin/node_ordering_by_rack.py --input_file hostfile
rankfile=""
for candidate in ordered_rankfile rankfile_system_name; do
  if [[ -s "$candidate" ]]; then rankfile="$candidate"; break; fi
done
if [[ -z "$rankfile" ]]; then
  echo 'Node ordering produced no nonempty rankfile (expected ordered_rankfile or rankfile_system_name).' >&2
  exit 1
fi
printf 'Ordered ranks (%s):\n' "$rankfile"; cat "$rankfile"
if [[ -s node_switch_list ]]; then
  printf 'Node switches:\n'; cat node_switch_list
fi
mpivars_path="$(find /usr/mpi/gcc /opt -maxdepth 4 -path '*/bin/mpivars.sh' -print -quit 2>/dev/null || true)"
if [[ -z "$mpivars_path" ]]; then echo 'Could not find MPIPATH' >&2; exit 1; fi
set +u
source "$mpivars_path"
set -u
hpcx_path="$(find /opt -maxdepth 5 -path '*/nccl_rdma_sharp_plugin/lib/libnccl-net.so' -print -quit 2>/dev/null || true)"
hpcx_path="${hpcx_path:-none}"
shape="$(curl -fsSH 'Authorization: Bearer Oracle' http://169.254.169.254/opc/v2/instance/ | jq -r .shape)"
rdma_planes="$(curl -fsSH 'Authorization: Bearer Oracle' http://169.254.169.254/opc/v2/host/rdmaFabricData/planes 2>/dev/null || true)"
[[ "$rdma_planes" =~ ^[0-9]+$ ]] || rdma_planes=1
qps=1
extra_args=()
case "$shape" in
  BM.GPU.B4.8|BM.GPU.A100-v2.8)
    ucx_device=mlx5_0:1
    hca='=mlx5_5,mlx5_6,mlx5_7,mlx5_8,mlx5_1,mlx5_2,mlx5_3,mlx5_4,mlx5_14,mlx5_15,mlx5_16,mlx5_17,mlx5_9,mlx5_10,mlx5_11,mlx5_12' ;;
  BM.GPU4.8)
    ucx_device=mlx5_4:1
    hca='=mlx5_0,mlx5_2,mlx5_6,mlx5_8,mlx5_10,mlx5_12,mlx5_14,mlx5_16,mlx5_1,mlx5_3,mlx5_7,mlx5_9,mlx5_11,mlx5_13,mlx5_15,mlx5_17' ;;
  BM.GPU.H100.8)
    ucx_device=eth0
    hca='=mlx5_0,mlx5_1,mlx5_3,mlx5_4,mlx5_5,mlx5_6,mlx5_7,mlx5_8,mlx5_9,mlx5_10,mlx5_12,mlx5_13,mlx5_14,mlx5_15,mlx5_16,mlx5_17' ;;
  BM.GPU.H200.8|BM.GPU.B200.8)
    ucx_device=eth0
    hca='=mlx5_0,mlx5_3,mlx5_4,mlx5_5,mlx5_6,mlx5_9,mlx5_10,mlx5_11' ;;
  BM.GPU.B300.8|BM.GPU.B300.HS.8)
    ucx_device=eth0
    if (( rdma_planes > 1 )); then
      hca='=rdma_vf_rail0,rdma_vf_rail1,rdma_vf_rail2,rdma_vf_rail3,rdma_vf_rail4,rdma_vf_rail5,rdma_vf_rail6,rdma_vf_rail7'
      qps=4
      extra_args+=(-x NCCL_IB_ADAPTIVE_ROUTING=1 -x NCCL_TOPO_FILE=/opt/oci-hpc/healthchecks/B300MP.xml)
    else
      hca='=mlx5_0,mlx5_1,mlx5_7,mlx5_8,mlx5_9,mlx5_10,mlx5_11,mlx5_12,mlx5_13,mlx5_14,mlx5_16,mlx5_17,mlx5_18,mlx5_19,mlx5_20,mlx5_21'
    fi ;;
  BM.GPU.RTXPRO.8)
    ucx_device=eth0
    hca='=mlx5_0,mlx5_1,mlx5_2,mlx5_3,mlx5_6,mlx5_7,mlx5_8,mlx5_9' ;;
  *) echo "Unsupported NCCL node shape: $shape" >&2; exit 1 ;;
esac
printf 'Running all_reduce_perf on %s nodes (%s)\n' "$SLURM_NNODES" "$shape"
common_args=(--mca pml ucx --bind-to numa --mca coll '^hcoll'
  -x NCCL_DEBUG=WARN -x NCCL_IB_SL=0 -x NCCL_IB_TC=41
  -x NCCL_IB_GID_INDEX=3 -x HCOLL_ENABLE_MCAST_ALL=0 -x coll_hcoll_enable=0
  -x "NCCL_IB_HCA=$hca" -x "UCX_NET_DEVICES=$ucx_device")
if [[ "$shape" == BM.GPU.B4.8 || "$shape" == BM.GPU.A100-v2.8 || "$shape" == BM.GPU4.8 ]]; then
  mpirun "${common_args[@]}" -x NCCL_IB_QPS_PER_CONNECTION=4 -x UCX_TLS=ud,self,sm \
    -x NCCL_ALGO=Ring --np "$((SLURM_NNODES * 8))" \
    --rankfile "$rankfile" "$exec_cmd" -b1G -e10G -i$((1024*1024*1024*9)) -n 100
else
  mpirun "${common_args[@]}" -x NCCL_CUMEM_ENABLE=0 -x NCCL_IB_SPLIT_DATA_ON_QPS=0 \
    -x "NCCL_IB_QPS_PER_CONNECTION=$qps" -x NCCL_IB_TIMEOUT=22 \
    -x "NCCL_NET_PLUGIN=$hpcx_path" -x UCX_TLS=tcp -x RX_QUEUE_LEN=8192 \
    -x IB_RX_QUEUE_LEN=8192 -x "NCCL_SOCKET_IFNAME=$ucx_device" \
    -x NCCL_IGNORE_CPU_AFFINITY=1 "${extra_args[@]}" \
    --np "$((SLURM_NNODES * 8))" --rankfile "$rankfile" \
    "$exec_cmd" -b 1G -e 16G -f 2 -g 1 -n 50
fi
