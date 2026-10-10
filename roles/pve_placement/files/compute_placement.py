import json
import re
import subprocess
import sys

source_node = sys.argv[1]
target_nodes = sys.argv[2].split(',')
SAFETY_THRESHOLD = float(sys.argv[3])  # never push a node above this fraction of memory usage
STORAGE_ID = sys.argv[4]  # the shared storage name VM disks are migrated onto (e.g. nvme_data)
# VMs that cannot be live-migrated, because their storage is not available on the other nodes, are
# shut down for the maintenance window instead of being given a target node.
EXCLUDE_NAMES = sys.argv[5].split(',') if len(sys.argv) > 5 and sys.argv[5] else []
# Groups of VMs that must not share a node, as JSON: [["semaphore101", "semaphore102"]].
ANTI_AFFINITY = json.loads(sys.argv[6]) if len(sys.argv) > 6 and sys.argv[6] else []

PEERS = {}
for group in ANTI_AFFINITY:
    for member in group:
        PEERS.setdefault(member, set()).update(set(group) - {member})

def get_node_status(node):
    raw = subprocess.check_output(['pvesh', 'get', f'/nodes/{node}/status', '--output-format', 'json'])
    return json.loads(raw)

def get_node_vms(node):
    raw = subprocess.check_output(['pvesh', 'get', f'/nodes/{node}/qemu', '--output-format', 'json'])
    return json.loads(raw)

def get_storage_status(node, storage_id):
    raw = subprocess.check_output(['pvesh', 'get', f'/nodes/{node}/storage/{storage_id}/status', '--output-format', 'json'])
    return json.loads(raw)

DISK_KEY = re.compile(r'^(?:scsi|sata|ide|virtio)\d+$|^efidisk\d+$|^tpmstate\d+$')
SIZE = re.compile(r'(?:^|,)size=(\d+(?:\.\d+)?)([KMGT]?)(?:,|$)')
SIZE_UNIT = {'': 1, 'K': 1024, 'M': 1024 ** 2, 'G': 1024 ** 3, 'T': 1024 ** 4}

# /cluster/resources and /nodes/{node}/qemu only report maxdisk, which is the boot disk alone - for
# a multi-disk VM that understates what a migration has to copy by an order of magnitude. Every
# disk key in the VM's own config carries its own size=, so sum those instead.
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

def get_vms_to_evacuate(node):
    vms = get_node_vms(node)
    running = [vm for vm in vms if vm.get('status') == 'running']
    to_migrate = [vm for vm in running if vm.get('name') not in EXCLUDE_NAMES]
    to_shutdown = [vm for vm in running if vm.get('name') in EXCLUDE_NAMES]
    return to_migrate, to_shutdown

vms_to_move, vms_to_shutdown = get_vms_to_evacuate(source_node)
vms_to_move.sort(key=lambda v: (-v.get('maxmem', 0), v['vmid']))

node_total_mem = {}
node_used_mem = {}
node_vm_count = {}
node_resident = {}
node_avail_disk = {}
for node in target_nodes:
    status = get_node_status(node)
    node_total_mem[node] = status['memory']['total']
    node_used_mem[node] = status['memory']['total'] - status['memory']['free']
    vms_on_node = get_node_vms(node)
    node_vm_count[node] = len(vms_on_node)
    node_resident[node] = {vm.get('name') for vm in vms_on_node}
    node_avail_disk[node] = get_storage_status(node, STORAGE_ID)['avail']

placements = []
planned = {}  # VM name -> chosen node, so a peer evacuated later avoids it
warnings = []
for vm in vms_to_move:
    vm_mem = vm.get('maxmem', 0)
    name = vm.get('name', f"vm-{vm['vmid']}")
    # Allocated/nominal size, not actual bytes written - nvme_data is thin-provisioned, so a VM can
    # grow up to this over time even if it barely uses any space today. Falls back to maxdisk only
    # if the config yielded nothing, which would otherwise leave the check with no figure at all.
    vm_disk = get_vm_allocated_disk(source_node, vm['vmid']) or vm.get('maxdisk', 0)

    peers = PEERS.get(name, set())
    forbidden = {n for n in target_nodes if node_resident[n] & peers} | {planned[p] for p in peers if p in planned}
    disk_fits_nodes = [n for n in target_nodes if node_avail_disk[n] >= vm_disk]
    # A migration with insufficient disk space fails outright, unlike memory overcommit, so the
    # disk is the one hard limit.
    if not disk_fits_nodes:
        sys.exit(
            f"No target node has enough free space on {STORAGE_ID} to hold VM {vm['vmid']} "
            f"({name}, {vm_disk} bytes required) - refusing to emit a placement that "
            f"would fail migration"
        )

    def memory_safe(n):
        return (node_used_mem[n] + vm_mem) / node_total_mem[n] <= SAFETY_THRESHOLD

    # In order of preference: a node that is memory-safe and has no peer; then one that is
    # memory-safe but shares a node with a peer (memory pressure is worse than a shared node during
    # a planned window); then one with no peer but too little memory; then anything that fits.
    tiers = [
        [n for n in disk_fits_nodes if memory_safe(n) and n not in forbidden],
        [n for n in disk_fits_nodes if memory_safe(n)],
        [n for n in disk_fits_nodes if n not in forbidden],
        disk_fits_nodes,
    ]
    tier_index, pool = next((i, t) for i, t in enumerate(tiers) if t)
    if tier_index == 0:
        best_node = min(pool, key=lambda n: (node_vm_count[n], -(node_total_mem[n] - node_used_mem[n])))
    else:
        best_node = max(pool, key=lambda n: node_total_mem[n] - node_used_mem[n])

    if not memory_safe(best_node):
        projected = (node_used_mem[best_node] + vm_mem) / node_total_mem[best_node]
        warnings.append(
            f"{name} ({vm_mem / 2**30:.0f} GB allocated): no node stays under the {SAFETY_THRESHOLD:.0%} "
            f"memory threshold, so it goes to {best_node} anyway, which would reach {projected:.0%} of its "
            f"memory only if the VM used its whole allocation"
        )
    if best_node in forbidden:
        shared = sorted((node_resident[best_node] | {n for n, t in planned.items() if t == best_node}) & peers)
        warnings.append(f"{name}: placed on {best_node} next to {', '.join(shared)}, which it should not share a node with")

    placements.append({
        'vmid': vm['vmid'],
        'name': name,
        'mem_required': vm_mem,
        'disk_required': vm_disk,
        'target_node': best_node
    })
    planned[name] = best_node
    node_used_mem[best_node] += vm_mem
    node_avail_disk[best_node] -= vm_disk
    node_vm_count[best_node] += 1
    node_resident[best_node].add(name)

result = {
    'migrate': placements,
    'shutdown': [
        {'vmid': vm['vmid'], 'name': vm.get('name', f"vm-{vm['vmid']}")}
        for vm in vms_to_shutdown
    ],
    'warnings': warnings
}
print(json.dumps(result, indent=2))
