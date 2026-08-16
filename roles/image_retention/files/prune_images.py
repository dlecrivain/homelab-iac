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

# Dangling (untagged) images have no repo to apply "keep last 2" to - these are typically
# leftover intermediate layers from a tag being repointed to a newer pull. podman's own
# `image prune` already refuses to remove anything still attached to a container, so this
# is safe the same way podman rmi above is.
prune_result = subprocess.run(['podman', 'image', 'prune', '-f'], capture_output=True, text=True)
if prune_result.returncode == 0:
    pruned_ids = [line for line in prune_result.stdout.splitlines() if line.strip()]
    removed.extend(pruned_ids)

print(f"removed:{len(removed)}")
