import ipaddress
import json
import sys

cidr = sys.argv[1]
start_ip = ipaddress.ip_address(sys.argv[2])
used = set(json.load(sys.stdin))

for candidate in ipaddress.ip_network(cidr, strict=False).hosts():
    if candidate < start_ip:
        continue
    if str(candidate) in used:
        continue
    print(candidate)
    sys.exit(0)

print("NONE")
