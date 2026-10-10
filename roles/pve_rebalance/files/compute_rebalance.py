import json
import re
import subprocess
import sys

nodes = sys.argv[1].split(',')
SAFETY_THRESHOLD = float(sys.argv[2])
IMPROVEMENT_MARGIN = float(sys.argv[3])  # only move a VM if it meaningfully improves balance
STORAGE_ID = sys.argv[4]  # the shared storage name VM disks are migrated onto (e.g. nvme_data)
# VMs this rebalance never moves. They still count towards the load of the node they sit on.
FIXED_NAMES = sys.argv[5].split(',') if len(sys.argv) > 5 and sys.argv[5] else []
# Groups of VMs that must not share a node, as JSON: [["semaphore101", "semaphore102"]].
ANTI_AFFINITY = json.loads(sys.argv[6]) if len(sys.argv) > 6 and sys.argv[6] else []

PEERS = {}
for group in ANTI_AFFINITY:
    for member in group:
        PEERS.setdefault(member, set()).update(set(group) - {member})

def get_node_status(node):
    raw = subprocess.check_output(['pvesh', 'get', f'/nodes/{node}/status', '--output-format', 'json'])
    return json.loads(raw)

def get_storage_status(node, storage_id):
    raw = subprocess.check_output(['pvesh', 'get', f'/nodes/{node}/storage/{storage_id}/status', '--output-format', 'json'])
    return json.loads(raw)

def get_all_vms():
    raw = subprocess.check_output(['pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json'])
    data = json.loads(raw)
    return [vm for vm in data if vm.get('template') != 1 and vm.get('status') == 'running']

def warn(message):
    print(f'WARNING: {message}', file=sys.stderr)

node_total = {}
node_avail_disk = {}
for node in nodes:
    status = get_node_status(node)
    node_total[node] = status['memory']['total']
    node_avail_disk[node] = get_storage_status(node, STORAGE_ID)['avail']

vms = get_all_vms()
vms.sort(key=lambda v: (-v.get('maxmem', 0), v['vmid']))
fixed = [vm for vm in vms if vm.get('name') in FIXED_NAMES]
movable = [vm for vm in vms if vm.get('name') not in FIXED_NAMES]

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

# Greedily rebuild an ideal assignment from scratch. The VMs that never move go in first, where
# they already are, so everything else is balanced around them.
ideal_used = {n: 0 for n in nodes}
ideal_avail_disk = dict(node_avail_disk)
ideal_count = {n: 0 for n in nodes}
assignment = {}
placed = {}  # VM name -> node, for the anti-affinity check

def place(vm, node):
    assignment[vm['vmid']] = node
    placed[vm.get('name')] = node
    ideal_used[node] += vm.get('maxmem', 0)
    ideal_avail_disk[node] -= vm_disk[vm['vmid']]
    ideal_count[node] += 1

for vm in fixed:
    place(vm, vm['node'])

for vm in movable:
    vm_mem = vm.get('maxmem', 0)
    # Allocated/nominal size, not actual bytes written - nvme_data is thin-provisioned, so a VM can
    # grow up to this over time even if it barely uses any space today.
    vm_needs_disk = vm_disk[vm['vmid']]
    forbidden = {placed[peer] for peer in PEERS.get(vm.get('name'), ()) if peer in placed}
    safe_nodes = [
        n for n in nodes
        if n not in forbidden
        and (ideal_used[n] + vm_mem) / node_total[n] <= SAFETY_THRESHOLD
        and ideal_avail_disk[n] >= vm_needs_disk
    ]
    # Rebalance moves are optional (unlike placement, nothing forces this VM to move) - if no
    # node is safe on both memory and disk, treat its current node as the ideal target instead
    # of forcing it onto a disk-unsafe node, so no move gets proposed for it below. A VM that
    # shares a node with one of its anti-affinity peers is the exception: it goes to any node
    # that holds its disks and has no peer, if there is one.
    if safe_nodes:
        candidates = safe_nodes
    elif vm['node'] in forbidden:
        candidates = [n for n in nodes if n not in forbidden and ideal_avail_disk[n] >= vm_needs_disk] or [vm['node']]
    else:
        candidates = [vm['node']]
    best_node = min(
        candidates,
        key=lambda n: (ideal_count[n], usage_pct(n, ideal_used[n] + vm_mem))
    )
    place(vm, best_node)

# Only report moves where the VM isn't already on its ideal node, and where
# moving it actually closes a meaningful gap between the source and target
# node's current usage (avoids migrating VMs for a marginal rebalance). A VM that
# currently shares a node with one of its anti-affinity peers skips that margin check,
# until a move has already separated them.
positions = {vm.get('name'): vm['node'] for vm in vms}
moves = []
for vm in movable:
    name = vm.get('name')
    target = assignment[vm['vmid']]
    if vm['node'] == target:
        continue

    shares_node_with_peer = any(positions.get(peer) == vm['node'] for peer in PEERS.get(name, ()))
    if not shares_node_with_peer:
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
    positions[name] = target

for group in ANTI_AFFINITY:
    by_node = {}
    for member in group:
        if member in positions:
            by_node.setdefault(positions[member], []).append(member)
    for node, members in by_node.items():
        if len(members) > 1:
            warn(f"{' and '.join(members)} would still share {node} after this plan")

print(json.dumps(moves, indent=2))
