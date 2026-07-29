import json
import subprocess
import sys

source_node = sys.argv[1]
target_nodes = sys.argv[2].split(',')
SAFETY_THRESHOLD = float(sys.argv[3])  # never push a node above this fraction of memory usage

def get_node_status(node):
    raw = subprocess.check_output(['pvesh', 'get', f'/nodes/{node}/status', '--output-format', 'json'])
    return json.loads(raw)

def get_node_vms(node):
    raw = subprocess.check_output(['pvesh', 'get', f'/nodes/{node}/qemu', '--output-format', 'json'])
    return json.loads(raw)

def get_vms_to_evacuate(node):
    vms = get_node_vms(node)
    return [vm for vm in vms if vm.get('status') == 'running']

vms_to_move = get_vms_to_evacuate(source_node)
vms_to_move.sort(key=lambda v: v.get('maxmem', 0), reverse=True)

node_total_mem = {}
node_used_mem = {}
node_vm_count = {}
for node in target_nodes:
    status = get_node_status(node)
    node_total_mem[node] = status['memory']['total']
    node_used_mem[node] = status['memory']['total'] - status['memory']['free']
    node_vm_count[node] = len(get_node_vms(node))

placements = []
for vm in vms_to_move:
    vm_mem = vm.get('maxmem', 0)

    safe_nodes = [
        n for n in target_nodes
        if (node_used_mem[n] + vm_mem) / node_total_mem[n] <= SAFETY_THRESHOLD
    ]

    if safe_nodes:
        best_node = min(
            safe_nodes,
            key=lambda n: (node_vm_count[n], -(node_total_mem[n] - node_used_mem[n]))
        )
    else:
        best_node = max(target_nodes, key=lambda n: node_total_mem[n] - node_used_mem[n])

    placements.append({
        'vmid': vm['vmid'],
        'name': vm.get('name', f"vm-{vm['vmid']}"),
        'mem_required': vm_mem,
        'target_node': best_node
    })
    node_used_mem[best_node] += vm_mem
    node_vm_count[best_node] += 1

print(json.dumps(placements, indent=2))
