import json
import sys

path = sys.argv[1]
with open(path) as f:
    dashboard = json.load(f)

changed = False

def walk(panels):
    global changed
    for p in panels:
        title = p.get('title')
        if title == 'NVMe Data Read':
            p['title'] = 'NVMe Read Throughput'
            for t in p.get('targets', []):
                if t.get('expr', '').startswith('max by (device) (smartctl_device_bytes_read'):
                    t['expr'] = 'max by (device) (rate(smartctl_device_bytes_read{instance=~"$instance",device=~"nvme.*"}[$__rate_interval]))'
                    changed = True
            fc = p.get('fieldConfig', {}).get('defaults', {})
            if fc.get('unit') == 'bytes':
                fc['unit'] = 'Bps'
                changed = True
        elif title == 'NVMe Data Written':
            p['title'] = 'NVMe Write Throughput'
            for t in p.get('targets', []):
                if t.get('expr', '').startswith('max by (device) (smartctl_device_bytes_written'):
                    t['expr'] = 'max by (device) (rate(smartctl_device_bytes_written{instance=~"$instance",device=~"nvme.*"}[$__rate_interval]))'
                    changed = True
            fc = p.get('fieldConfig', {}).get('defaults', {})
            if fc.get('unit') == 'bytes':
                fc['unit'] = 'Bps'
                changed = True
        if 'panels' in p:
            walk(p['panels'])

walk(dashboard.get('panels', []))

with open(path, 'w') as f:
    json.dump(dashboard, f, indent=2)

print(f"changed:{changed}")
