import json
import subprocess
import sys

nodes = sys.argv[1].split(',')
SAFETY_THRESHOLD = float(sys.argv[2])
IMPROVEMENT_MARGIN = float(sys.argv[3])  # only move a VM if it meaningfully improves balance
STORAGE_ID = sys.argv[4]  # the shared storage name VM disks are migrated onto (e.g. nvme_data)

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

node_total = {}
node_avail_disk = {}
for node in nodes:
    status = get_node_status(node)
    node_total[node] = status['memory']['total']
    node_avail_disk[node] = get_storage_status(node, STORAGE_ID)['avail']

vms = get_all_vms()
vms.sort(key=lambda v: v.get('maxmem', 0), reverse=True)

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
    # Allocated/nominal disk size, not actual bytes written - nvme_data is thin-provisioned,
    # so a VM can grow up to this size over time even if it barely uses any space today.
    vm_disk = vm.get('maxdisk', 0)
    safe_nodes = [
        n for n in nodes
        if (ideal_used[n] + vm_mem) / node_total[n] <= SAFETY_THRESHOLD
        and ideal_avail_disk[n] >= vm_disk
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
    ideal_avail_disk[best_node] -= vm_disk
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
        'disk_required': vm.get('maxdisk', 0)
    })

print(json.dumps(moves, indent=2))
