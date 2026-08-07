import json
import subprocess
import sys

remote_path = sys.argv[1]
keep = int(sys.argv[2])

result = subprocess.run(['rclone', 'lsjson', remote_path], capture_output=True, text=True)
files = json.loads(result.stdout) if result.returncode == 0 else []
files.sort(key=lambda f: f['ModTime'], reverse=True)

removed = []
for f in files[keep:]:
    subprocess.run(['rclone', 'deletefile', f"{remote_path}/{f['Name']}"], capture_output=True)
    removed.append(f['Name'])

print(f"removed:{len(removed)}")
