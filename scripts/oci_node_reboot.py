"""Resolve a drained Slurm compute node and optionally request an OCI hard reset."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid

from captop import config_value

NODE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
INSTANCE_PATTERN = re.compile(r"ocid1\.instance\.[A-Za-z0-9_.-]+")


def slurm_target(node):
    if not NODE_PATTERN.fullmatch(node) or node.upper() == "ALL":
        raise ValueError("Select exactly one literal compute node.")
    result = subprocess.run(["scontrol", "show", "node", node, "-o"],
                            capture_output=True, text=True, check=True, timeout=15)
    fields = dict(re.findall(r"(?:^|\s)(NodeName|NodeAddr|State|CPUAlloc)=(\S+)", result.stdout))
    states = fields.get("State", "").split("+")
    if (fields.get("NodeName") != node or "DRAIN" not in states or
            fields.get("CPUAlloc") != "0" or any(s in states for s in ("ALLOCATED", "MIXED", "COMPLETING"))):
        raise ValueError("The selected node must be fully drained with no allocated CPUs.")
    address = str(ipaddress.ip_address(fields.get("NodeAddr", "")))
    compute_addresses = set()
    for line in Path("/etc/ansible/hosts").read_text().splitlines():
        if "role=compute" not in line:
            continue
        tokens = shlex.split(line, comments=True)
        values = dict(token.split("=", 1) for token in tokens[1:] if "=" in token)
        if values.get("role") == "compute":
            compute_addresses.add(values.get("ansible_host"))
    if address not in compute_addresses:
        raise ValueError("Node address is not in the controller's compute inventory.")
    jobs = subprocess.run(["squeue", "-h", "-w", node,
                           "--states=RUNNING,COMPLETING,CONFIGURING,SUSPENDED", "-o", "%i"],
                          capture_output=True, text=True, check=True, timeout=15)
    if jobs.stdout.strip():
        raise ValueError("The selected node still has active jobs; hard reboot was blocked.")
    return address


def clients():
    import oci
    compartment = config_value("/opt/oci-hpc/conf/queues.conf", "targetCompartment", ":")
    region = config_value("/opt/oci-hpc/conf/queues.conf", "region", ":")
    if not compartment:
        raise ValueError("Cluster targetCompartment is missing or ambiguous.")
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    options = {"signer": signer, "timeout": (5, 15), "retry_strategy": oci.retry.NoneRetryStrategy()}
    config = {"region": region or signer.region}
    return oci, compartment, oci.core.ComputeClient(config, **options), oci.core.VirtualNetworkClient(config, **options), config["region"]


def resolve(node):
    address = slurm_target(node)
    oci, compartment, compute, network, region = clients()
    subnets = oci.pagination.list_call_get_all_results(network.list_subnets, compartment).data
    vnics = set()
    for subnet in subnets:
        if ipaddress.ip_address(address) not in ipaddress.ip_network(subnet.cidr_block):
            continue
        ips = oci.pagination.list_call_get_all_results(network.list_private_ips,
                                                      subnet_id=subnet.id, ip_address=address).data
        vnics.update(ip.vnic_id for ip in ips if ip.is_primary and ip.ip_address == address)
    instances = set()
    for vnic_id in vnics:
        vnic = network.get_vnic(vnic_id).data
        if not vnic.is_primary or vnic.private_ip != address:
            continue
        attachments = oci.pagination.list_call_get_all_results(compute.list_vnic_attachments,
                                                              compartment, vnic_id=vnic_id).data
        instances.update(a.instance_id for a in attachments if a.lifecycle_state == "ATTACHED")
    if len(instances) != 1:
        raise ValueError("The compute node must map to exactly one attached OCI instance.")
    response = compute.get_instance(instances.pop())
    instance = response.data
    if instance.compartment_id != compartment or not instance.shape.startswith("BM."):
        raise ValueError("The selected instance is not bare metal in the cluster compartment.")
    if instance.lifecycle_state != "RUNNING":
        raise ValueError(f"Instance is {instance.lifecycle_state}; only RUNNING bare-metal instances can be rebooted.")
    target = {"node": node, "private_ip": address, "instance_id": instance.id,
              "display_name": instance.display_name, "shape": instance.shape,
              "region": region,
              "lifecycle_state": instance.lifecycle_state}
    return target, compute, response.headers.get("etag")


def hard_reboot(node, expected_instance):
    if not INSTANCE_PATTERN.fullmatch(expected_instance):
        raise ValueError("Invalid instance OCID.")
    target, compute, etag = resolve(node)
    if target["instance_id"] != expected_instance:
        raise ValueError("Node-to-instance mapping changed. Rebuild the command and confirm again.")
    if slurm_target(node) != target["private_ip"]:
        raise ValueError("Node address changed during validation; reboot was blocked.")
    response = compute.instance_action(expected_instance, "RESET", if_match=etag,
                                       opc_retry_token=str(uuid.uuid4()))
    return target, response.headers.get("opc-request-id", "")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resolve", action="store_true")
    parser.add_argument("node")
    parser.add_argument("instance_id", nargs="?")
    args = parser.parse_args()
    try:
        if args.resolve and args.instance_id is None:
            print(json.dumps(resolve(args.node)[0]))
        elif not args.resolve and args.instance_id:
            target, request_id = hard_reboot(args.node, args.instance_id)
            print(f"OCI hard reboot accepted for {target['node']} ({target['shape']}, {target['instance_id']}).", flush=True)
            print(f"Request ID: {request_id}. The node remains drained; verify it recovers before resuming it.", flush=True)
        else:
            parser.error("Use --resolve NODE or NODE INSTANCE_OCID.")
    except Exception as exc:
        print(f"Hard reboot: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
