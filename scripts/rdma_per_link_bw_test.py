#!/usr/bin/env python3
"""Run a bounded ib_write_bw test for each common RDMA link on two nodes."""

import ipaddress
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time

NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
MAPPING = re.compile(r"^(mlx5_\d+)\s+port\s+(\d+)\s+==>\s+(rdma\d+|ib\d+)\s+\((Up|Down)\)$", re.I)
DEVICE = re.compile(r"mlx5_\d+\Z")
NETDEV = re.compile(r"(?:rdma|ib)\d+\Z", re.I)
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
       "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2"]
DURATION = 10
active_server = None


def remote(host, command, timeout=30):
    result = subprocess.run([*SSH, host, command], text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{host}: {command.split()[0]} failed ({result.returncode}): {(result.stderr or result.stdout).strip()[-1200:]}")
    return result.stdout.strip()


def mappings(host):
    result = {}
    for line in remote(host, "/usr/sbin/ibdev2netdev", 30).splitlines():
        match = MAPPING.fullmatch(line.strip())
        if match and match[4].lower() == "up":
            result[match[3]] = (match[1], int(match[2]))
    return result


def server_ip(host, netdev):
    if not NETDEV.fullmatch(netdev):
        raise ValueError("Invalid RDMA interface")
    address = remote(host, f"ip -4 -o addr show dev {netdev}", 20)
    for line in address.splitlines():
        fields = line.split()
        if "inet" in fields:
            return str(ipaddress.IPv4Interface(fields[fields.index("inet") + 1]).ip)
    raise RuntimeError(f"{host}: {netdev} has no IPv4 address")


def parse_bandwidth(output):
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if "BW average[Gb/sec]" not in line:
            continue
        for row in lines[index + 1:index + 10]:
            fields = row.split()
            if len(fields) >= 4 and fields[0].isdigit() and fields[1].isdigit():
                try:
                    return float(fields[3])
                except ValueError:
                    pass
    return None


def stop_server():
    global active_server
    if active_server:
        host, pid = active_server
        try:
            # The timeout process belongs to the SSH user and forwards TERM
            # to its sudo child. Never kill unrelated ib_write_bw processes.
            remote(host, f"kill -TERM {pid} 2>/dev/null || true", 10)
        except (OSError, subprocess.SubprocessError, RuntimeError):
            pass
        active_server = None


def interrupt(_signum, _frame):
    stop_server()
    raise SystemExit(130)


def test_link(server, peer, netdev, server_device, peer_device, ip, port):
    global active_server
    dev, ibport = server_device
    peer_dev, peer_port = peer_device
    if not DEVICE.fullmatch(dev) or not DEVICE.fullmatch(peer_dev):
        raise ValueError("Invalid RDMA device")
    log = f"/tmp/slurm-console-ib-write-bw-{port}.log"
    server_args = f"sudo -n ib_write_bw -d {dev} -i {ibport} -p {port} --report_gbits -D {DURATION}"
    launch = f"timeout --signal=TERM --kill-after=2s 30s {server_args} > {shlex.quote(log)} 2>&1 < /dev/null & echo $!"
    pid_text = remote(server, "bash -lc " + shlex.quote(launch), 20)
    if not pid_text.isdigit():
        raise RuntimeError(f"{server}: server did not return a PID: {pid_text[-300:]}")
    active_server = (server, int(pid_text))
    try:
        time.sleep(1)
        client_args = f"sudo -n ib_write_bw -d {peer_dev} -i {peer_port} -p {port} {ip} --report_gbits -D {DURATION}"
        output = remote(peer, "bash -lc " + shlex.quote(client_args + " 2>&1"), DURATION + 35)
        bandwidth = parse_bandwidth(output)
        if bandwidth is None:
            raise RuntimeError(f"No BW average[Gb/sec] in client output: {output[-1200:]}")
        return bandwidth
    finally:
        stop_server()


def main(argv):
    if len(argv) != 3 or any(not NODE.fullmatch(host) or host.upper() == "ALL" for host in argv[1:]) or argv[1] == argv[2]:
        print("Usage: rdma_per_link_bw_test.py <server-node> <client-node> (distinct literal node names)", file=sys.stderr)
        return 2
    server, peer = argv[1:]
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    print(f"Server: {server} | Client: {peer}", flush=True)
    server_map, peer_map = mappings(server), mappings(peer)
    common = sorted(server_map.keys() & peer_map.keys(), key=lambda name: int(re.search(r"\d+", name).group()))
    if not common:
        raise RuntimeError("No common active RDMA interfaces found on the selected nodes")
    if len(common) > 32:
        raise RuntimeError("More than 32 common RDMA interfaces; refusing an unbounded test")
    print(f"Testing {len(common)} common links, {DURATION} seconds each.\n", flush=True)
    print("netdev | server_dev | server_ip | peer_dev | bw_avg_gbps", flush=True)
    print("-------+------------+-----------+----------+------------", flush=True)
    failures = 0
    base_port = 20000 + secrets.randbelow(20000)
    for index, netdev in enumerate(common):
        srv, cli = server_map[netdev], peer_map[netdev]
        ip = "-"
        try:
            ip = server_ip(server, netdev)
            bw = test_link(server, peer, netdev, srv, cli, ip, base_port + index)
            value = f"{bw:.2f}"
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
            failures += 1
            value = "FAIL"
            print(f"{netdev}: {exc}", file=sys.stderr, flush=True)
        print(f"{netdev:<6} | {srv[0]}:{srv[1]} | {ip} | {cli[0]}:{cli[1]} | {value}", flush=True)
    stop_server()
    print(f"\nResult: {len(common) - failures}/{len(common)} links passed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        stop_server()
        print(f"RDMA test failed: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
