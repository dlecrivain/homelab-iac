import json
import subprocess

raw = subprocess.check_output(['podman', 'images', '--format', 'json'])
images = json.loads(raw)

by_repo = {}
for img in images:
    repos = img.get('Names') or img.get('RepoTags') or ['<none>']
    if repos and repos[0] not in (None, '<none>:<none>'):
        repo = repos[0].rsplit(':', 1)[0]
    else:
        repo = 'dangling-' + img['Id'][:12]
    by_repo.setdefault(repo, []).append(img)

removed = []
for repo, imgs in by_repo.items():
    if repo.startswith('dangling-'):
        continue
    imgs.sort(key=lambda i: i.get('Created', 0), reverse=True)
    for old in imgs[2:]:
        result = subprocess.run(['podman', 'rmi', old['Id']], capture_output=True)
        if result.returncode == 0:
            removed.append(old['Id'])

print(f"removed:{len(removed)}")
