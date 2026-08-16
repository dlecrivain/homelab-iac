import json
import sys

path = sys.argv[1]
with open(path) as f:
    dashboard = json.load(f)

panels = dashboard.get('panels', [])

# Find the lowest y+h currently used, to append new rows below everything
max_bottom = 0
for p in panels:
    gp = p.get('gridPos', {})
    bottom = gp.get('y', 0) + gp.get('h', 0)
    if bottom > max_bottom:
        max_bottom = bottom

# Row 1: Current CPU, Storage usage, Space allocation side by side
row1 = ['Current CPU', 'Storage usage', 'Space allocation']
row1_size = {'h': 10, 'w': 8}
# Row 2: Current memory, on its own
row2 = ['Current memory']
row2_size = {'h': 10, 'w': 8}

changed = []
x = 0
for p in panels:
    title = p.get('title')
    if title in row1:
        p['gridPos'] = {'h': row1_size['h'], 'w': row1_size['w'], 'x': x, 'y': max_bottom}
        x += row1_size['w']
        changed.append(title)
for p in panels:
    title = p.get('title')
    if title in row2:
        p['gridPos'] = {'h': row2_size['h'], 'w': row2_size['w'], 'x': 0, 'y': max_bottom + row1_size['h']}
        changed.append(title)

# Current CPU / Current memory: their legend showed every raw label (instance=pve-exporter:9221,
# id=node/pve1, job=pve_exporter, ...) since legendFormat was empty - extract just the node name
# (pve1/pve2/pve3) from the "id" label via label_replace and show only that.
legend_fixes = {
    'Current CPU': (
        'pve_cpu_usage_ratio{instance="$instance"} / pve_cpu_usage_limit and on(id) pve_node_info',
        'label_replace(pve_cpu_usage_ratio{instance="$instance"} / pve_cpu_usage_limit and on(id) pve_node_info, "node", "$1", "id", "node/(.+)")',
    ),
    'Current memory': (
        'pve_memory_usage_bytes{instance="$instance"} / pve_memory_size_bytes and on(id) pve_node_info',
        'label_replace(pve_memory_usage_bytes{instance="$instance"} / pve_memory_size_bytes and on(id) pve_node_info, "node", "$1", "id", "node/(.+)")',
    ),
}
for p in panels:
    title = p.get('title')
    if title in legend_fixes:
        old_expr, new_expr = legend_fixes[title]
        for t in p.get('targets', []):
            if t.get('expr') == old_expr:
                t['expr'] = new_expr
                t['legendFormat'] = '{{node}}'
                changed.append(title + ' (legend)')

# Space allocation showed raw allocated capacity (pve_disk_size_bytes, identical regardless of
# actual fill level) - switch to the same utilization ratio Storage usage already shows, so the
# bargauge actually reflects how full each storage is.
for p in panels:
    if p.get('title') == 'Space allocation':
        for t in p.get('targets', []):
            if t.get('expr', '').startswith('pve_disk_size_bytes{instance="$instance"'):
                t['expr'] = (
                    'pve_disk_usage_bytes{instance="$instance", id=~"storage/.+"} '
                    '/ pve_disk_size_bytes{instance="$instance", id=~"storage/.+"} '
                    '* on (id, instance) group_left(storage, node) pve_storage_info'
                )
                changed.append('Space allocation (query)')
        fc = p.setdefault('fieldConfig', {}).setdefault('defaults', {})
        fc['unit'] = 'percentunit'
        # Vertical orientation gave each of the 7 node/storage bars only a sliver of width -
        # nowhere near enough for a label like "pve1 - Stockage_SSD". Horizontal bars use the
        # panel's full width per label instead, stacked one per row.
        p.setdefault('options', {})['orientation'] = 'horizontal'
        p['gridPos']['h'] = 16
        changed.append('Space allocation (orientation)')

with open(path, 'w') as f:
    json.dump(dashboard, f, indent=2)

print(f"changed:{changed}")
