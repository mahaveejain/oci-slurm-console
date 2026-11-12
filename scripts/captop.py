"""Read-only extraction of the supplied captop discovery and report logic."""
import argparse
from collections import Counter
from datetime import datetime
from pathlib import Path
import re
import sys


def fail(message):
    print(f"captop: {message}", file=sys.stderr)
    raise SystemExit(1)


def config_value(path, key, delimiter):
    """Read legacy HPC settings as text; never execute their contents."""
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        return None
    pattern = re.compile(r"^\s*" + re.escape(key) + r"\s*" + delimiter + r"\s*(.*?)\s*$")
    values = []
    for line in lines:
        match = pattern.match(line)
        if match:
            value = match.group(1).split(" #", 1)[0].strip().strip("\"'")
            if value:
                values.append(value)
    values = sorted(set(values))
    if len(values) > 1:
        fail(f"Multiple distinct {key} values found in {path}; supply an explicit override.")
    return values[0] if values else None


def report(hosts):
    states = Counter()
    shapes = Counter()
    repairs = []
    for host in hosts:
        status = host.lifecycle_details or "UNKNOWN"
        if status == "AVAILABLE":
            status = "RUNNING" if host.instance_id else "AVAILABLE"
        elif status in ("UNAVAILABLE", "DEGRADED"):
            status = "RUNNING" if host.instance_id else "IN_REPAIR"
            if status == "IN_REPAIR":
                repairs.append(host.id)
        states[status] += 1
        shapes[host.instance_shape or "UNKNOWN"] += 1

    print(datetime.now().strftime("%m/%d/%Y %H:%M:%S"))
    print("----------------------\n     Block Status\n----------------------")
    print(f"State :: {dict(states)}\n")
    if not shapes:
        print("Total :: 0")
    elif len(shapes) == 1:
        print(f"Total :: {len(hosts)} {next(iter(shapes))}")
    else:
        print(f"Total :: {len(hosts)}")
        for shape, count in shapes.items():
            print(f"  {shape} :: {count}")
    if repairs:
        print("\n----------------------\n    Nodes in repair\n----------------------")
        print("\n".join(repairs))
        print()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="captop", description="Read-only OCI capacity topology report using instance principal authentication.")
    parser.add_argument("--capacity-id", help="Existing topology OCID; skips discovery")
    parser.add_argument("--compartment-id", help="Overrides targetCompartment from queues.conf")
    parser.add_argument("--tenancy-id", help="Overrides tenancy_ocid from Ansible inventory")
    parser.add_argument("--region", help="Overrides region from queues.conf / signer region")
    parser.add_argument("--queues-conf", default="/opt/oci-hpc/conf/queues.conf")
    parser.add_argument("--ansible-hosts", default="/etc/ansible/hosts")
    args = parser.parse_args(argv)
    try:
        import oci
    except ImportError:
        fail("OCI Python SDK is missing. Install requirements-captop.txt in the console virtual environment, or set CAPTOP_PYTHON for standalone use.")

    compartment, tenancy, region = args.compartment_id, args.tenancy_id, args.region
    if not args.capacity_id:
        compartment = compartment or config_value(args.queues_conf, "targetCompartment", ":")
        tenancy = tenancy or config_value(args.ansible_hosts, "tenancy_ocid", "=")
        region = region or config_value(args.queues_conf, "region", ":")
        if not compartment and not tenancy:
            fail("No discovery compartment found. Supply --capacity-id, --compartment-id, or --tenancy-id, or provide the HPC configuration files.")

    print("Connecting to OCI using instance principal authentication…", flush=True)
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    client = oci.core.ComputeClient(config={"region": region} if region else {}, signer=signer,
                                    timeout=(10, 30), retry_strategy=oci.retry.NoneRetryStrategy())
    try:
        capacity_id = args.capacity_id
        if not capacity_id:
            scopes = list(dict.fromkeys(scope for scope in (compartment, tenancy) if scope))
            for scope in scopes:
                print(f"Discovering capacity topologies in {scope}…", flush=True)
                topologies = oci.pagination.list_call_get_all_results(
                    client.list_compute_capacity_topologies, compartment_id=scope).data
                matches = [topology for topology in topologies if topology.lifecycle_state in ("ACTIVE", "UPDATING")]
                if len(matches) > 1:
                    ids = "\n".join(f"  {topology.id}" for topology in matches)
                    fail(f"Multiple usable topologies in {scope}; rerun with --capacity-id:\n{ids}")
                if matches:
                    capacity_id = matches[0].id
                    break
        if not capacity_id:
            fail("No ACTIVE or UPDATING capacity topology is visible. This environment may not have capacity topology configured. Ask the administrator to enable/provide an existing topology and verify IAM access. No topology was created.")
        hosts = oci.pagination.list_call_get_all_results(
            client.list_compute_capacity_topology_compute_bare_metal_hosts, capacity_id).data
    except oci.exceptions.ServiceError as exc:
        if exc.status in (401, 403, 404):
            fail(f"Capacity topology is unavailable or access is not authorized (OCI {exc.status}, {exc.code}). Verify topology configuration, region, and controller instance-principal IAM policies. Request ID: {exc.request_id}")
        raise
    print("\nCapacity Topology Mode...\n")
    print(f"CAPTOP ID : {capacity_id}")
    report(hosts)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        fail(str(exc))
