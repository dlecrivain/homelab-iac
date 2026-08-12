import json
import subprocess
import sys

source_node = sys.argv[1]
target_nodes = sys.argv[2].split(',')
SAFETY_THRESHOLD = float(sys.argv[3])  # never push a node above this fraction of memory usage
STORAGE_ID = sys.argv[4]  # the shared storage name VM disks are migrated onto (e.g. nvme_data)

def get_node_status(node):
    raw = subprocess.check_output(['pvesh', 'get', f'/nodes/{node}/status', '--output-format', 'json'])
    return json.loads(raw)

def get_node_vms(node):
    raw = subprocess.check_output(['pvesh', 'get', f'/nodes/{node}/qemu', '--output-format', 'json'])
    return json.loads(raw)

def get_storage_status(node, storage_id):
    raw = subprocess.check_output(['pvesh', 'get', f'/nodes/{node}/storage/{storage_id}/status', '--output-format', 'json'])
    return json.loads(raw)

def get_vms_to_evacuate(node):
    vms = get_node_vms(node)
    return [vm for vm in vms if vm.get('status') == 'running']

vms_to_move = get_vms_to_evacuate(source_node)
vms_to_move.sort(key=lambda v: v.get('maxmem', 0), reverse=True)

node_total_mem = {}
node_used_mem = {}
node_vm_count = {}
node_avail_disk = {}
for node in target_nodes:
    status = get_node_status(node)
    node_total_mem[node] = status['memory']['total']
    node_used_mem[node] = status['memory']['total'] - status['memory']['free']
    node_vm_count[node] = len(get_node_vms(node))
    node_avail_disk[node] = get_storage_status(node, STORAGE_ID)['avail']

placements = []
for vm in vms_to_move:
    vm_mem = vm.get('maxmem', 0)
    # Allocated/nominal disk size, not actual bytes written - nvme_data is thin-provisioned,
    # so a VM can grow up to this size over time even if it barely uses any space today.
    vm_disk = vm.get('maxdisk', 0)

    safe_nodes = [
        n for n in target_nodes
        if (node_used_mem[n] + vm_mem) / node_total_mem[n] <= SAFETY_THRESHOLD
        and node_avail_disk[n] >= vm_disk
    ]

    if safe_nodes:
        best_node = min(
            safe_nodes,
            key=lambda n: (node_vm_count[n], -(node_total_mem[n] - node_used_mem[n]))
        )
    else:
        # No node is safe on both fronts - disk space is the harder constraint (a migration
        # with insufficient disk space fails outright, unlike memory overcommit), so narrow to
        # nodes that can actually hold the disk and pick the one with the most free memory.
        disk_fits_nodes = [n for n in target_nodes if node_avail_disk[n] >= vm_disk]
        if not disk_fits_nodes:
            sys.exit(
                f"No target node has enough free space on {STORAGE_ID} to hold VM {vm['vmid']} "
                f"({vm['name']}, {vm_disk} bytes required) - refusing to emit a placement that "
                f"would fail migration"
            )
        best_node = max(disk_fits_nodes, key=lambda n: node_total_mem[n] - node_used_mem[n])

    placements.append({
        'vmid': vm['vmid'],
        'name': vm.get('name', f"vm-{vm['vmid']}"),
        'mem_required': vm_mem,
        'disk_required': vm_disk,
        'target_node': best_node
    })
    node_used_mem[best_node] += vm_mem
    node_avail_disk[best_node] -= vm_disk
    node_vm_count[best_node] += 1

print(json.dumps(placements, indent=2))
