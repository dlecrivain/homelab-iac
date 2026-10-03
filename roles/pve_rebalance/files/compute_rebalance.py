import json
import re
import subprocess
import sys

nodes = sys.argv[1].split(',')
SAFETY_THRESHOLD = float(sys.argv[2])
IMPROVEMENT_MARGIN = float(sys.argv[3])  # only move a VM if it meaningfully improves balance
STORAGE_ID = sys.argv[4]  # the shared storage name VM disks are migrated onto (e.g. nvme_data)
# VMs that cannot be live-migrated, because their storage is not available on the other nodes.
# No move is ever proposed for them - it would fail outright at runtime.
EXCLUDE_NAMES = sys.argv[5].split(',') if len(sys.argv) > 5 and sys.argv[5] else []

def get_node_status(node):
    raw = subprocess.check_output(['pvesh', 'get', f'/nodes/{node}/status', '--output-format', 'json'])
    return json.loads(raw)

def get_storage_status(node, storage_id):
    raw = subprocess.check_output(['pvesh', 'get', f'/nodes/{node}/storage/{storage_id}/status', '--output-format', 'json'])
    return json.loads(raw)

def get_all_vms():
    raw = subprocess.check_output(['pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json'])
    data = json.loads(raw)
    return [
        vm for vm in data
        if vm.get('template') != 1 and vm.get('status') == 'running' and vm.get('name') not in EXCLUDE_NAMES
    ]

node_total = {}
node_avail_disk = {}
for node in nodes:
    status = get_node_status(node)
    node_total[node] = status['memory']['total']
    node_avail_disk[node] = get_storage_status(node, STORAGE_ID)['avail']

vms = get_all_vms()
vms.sort(key=lambda v: v.get('maxmem', 0), reverse=True)

# /cluster/resources only reports maxdisk, which is the boot disk alone - for a multi-disk VM that
# understates what a migration has to copy by an order of magnitude. Every disk key in the VM's own
# config carries its own size=, so sum those instead. Resolved once per VM and reused below.
DISK_KEY = re.compile(r'^(?:scsi|sata|ide|virtio)\d+$|^efidisk\d+$|^tpmstate\d+$')
SIZE = re.compile(r'(?:^|,)size=(\d+(?:\.\d+)?)([KMGT]?)(?:,|$)')
SIZE_UNIT = {'': 1, 'K': 1024, 'M': 1024 ** 2, 'G': 1024 ** 3, 'T': 1024 ** 4}

def get_vm_allocated_disk(node, vmid):
    raw = subprocess.check_output(
        ['pvesh', 'get', f'/nodes/{node}/qemu/{vmid}/config', '--output-format', 'json'])
    total = 0
    for key, value in json.loads(raw).items():
        if not DISK_KEY.match(key) or not isinstance(value, str):
            continue
        match = SIZE.search(value)
        if match:
            total += int(float(match.group(1)) * SIZE_UNIT[match.group(2)])
    return total

vm_disk = {
    vm['vmid']: get_vm_allocated_disk(vm['node'], vm['vmid']) or vm.get('maxdisk', 0)
    for vm in vms
}

# Current usage per node (based on allocated VM memory, not live usage,
# so the plan is deterministic and doesn't fight live fluctuations)
node_used = {n: 0 for n in nodes}
for vm in vms:
    if vm['node'] in node_used:
        node_used[vm['node']] += vm.get('maxmem', 0)

def usage_pct(node, used):
    return used / node_total[node] if node_total[node] else 1.0

# Greedily rebuild an ideal assignment from scratch
ideal_used = {n: 0 for n in nodes}
ideal_avail_disk = dict(node_avail_disk)
ideal_count = {n: 0 for n in nodes}
assignment = {}
for vm in vms:
    vm_mem = vm.get('maxmem', 0)
    # Allocated/nominal size, not actual bytes written - nvme_data is thin-provisioned, so a VM can
    # grow up to this over time even if it barely uses any space today.
    vm_needs_disk = vm_disk[vm['vmid']]
    safe_nodes = [
        n for n in nodes
        if (ideal_used[n] + vm_mem) / node_total[n] <= SAFETY_THRESHOLD
        and ideal_avail_disk[n] >= vm_needs_disk
    ]
    # Rebalance moves are optional (unlike placement, nothing forces this VM to move) - if no
    # node is safe on both memory and disk, treat its current node as the ideal target instead
    # of forcing it onto a disk-unsafe node, so no move gets proposed for it below.
    candidates = safe_nodes if safe_nodes else [vm['node']]
    best_node = min(
        candidates,
        key=lambda n: (ideal_count[n], usage_pct(n, ideal_used[n] + vm_mem))
    )
    assignment[vm['vmid']] = best_node
    ideal_used[best_node] += vm_mem
    ideal_avail_disk[best_node] -= vm_needs_disk
    ideal_count[best_node] += 1

# Only report moves where the VM isn't already on its ideal node, and where
# moving it actually closes a meaningful gap between the source and target
# node's current usage (avoids migrating VMs for a marginal rebalance)
moves = []
for vm in vms:
    target = assignment[vm['vmid']]
    if vm['node'] == target:
        continue

    source_usage = usage_pct(vm['node'], node_used[vm['node']])
    target_usage = usage_pct(target, node_used[target])
    if source_usage - target_usage < IMPROVEMENT_MARGIN:
        continue

    moves.append({
        'vmid': vm['vmid'],
        'name': vm.get('name', f"vm-{vm['vmid']}"),
        'current_node': vm['node'],
        'target_node': target,
        'mem_required': vm.get('maxmem', 0),
        'disk_required': vm_disk[vm['vmid']]
    })

print(json.dumps(moves, indent=2))
