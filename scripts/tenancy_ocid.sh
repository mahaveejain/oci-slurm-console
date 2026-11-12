#!/usr/bin/env bash
# Print tenancy_ocid from the OCI HPC Ansible inventory.
# Usage: ./tenancy_ocid.sh [inventory-file]
set -euo pipefail

inventory="${1:-/etc/ansible/hosts}"
if (( $# > 1 )); then
    printf 'Usage: %s [inventory-file]\n' "$0" >&2
    exit 1
fi
if [[ ! -r "$inventory" ]]; then
    printf 'Cannot read inventory: %s\n' "$inventory" >&2
    exit 1
fi

awk '
    /^[[:space:]]*tenancy_ocid[[:space:]]*=/ {
        value = $0
        sub(/^[[:space:]]*tenancy_ocid[[:space:]]*=[[:space:]]*/, "", value)
        sub(/[[:space:]]+#.*$/, "", value)
        sub(/[[:space:]]+$/, "", value)
        if (value ~ /^".*"$/ || value ~ /^\047.*\047$/)
            value = substr(value, 2, length(value) - 2)
        if (value != "") values[value] = 1
    }
    END {
        for (value in values) { result = value; count++ }
        if (count == 1) print result
        else if (count == 0) {
            print "tenancy_ocid not found in inventory" > "/dev/stderr"
            exit 1
        } else {
            print "Multiple distinct tenancy_ocid values found in inventory" > "/dev/stderr"
            exit 1
        }
    }
' "$inventory"
