import json
import subprocess

KEEP_PER_REPO = 2

raw = subprocess.check_output(['podman', 'images', '--format', 'json'])
images = json.loads(raw)


# An image pulled by digest, or one whose tag has since been moved onto a newer image, carries no
# tag at all - only a repo@sha256 reference. Reading the repository from either form keeps "keep the
# N most recent" meaningful per repository, rather than collapsing every untagged image into one
# shared bucket where the count means nothing.
def repository_of(img):
    for ref in (img.get('Names') or []) + (img.get('RepoTags') or []) + (img.get('RepoDigests') or []):
        if not ref or ref == '<none>:<none>' or ref.endswith(':<none>'):
            continue
        # Strip the digest first, then the tag - and only when the colon is in the last path
        # segment, so a registry port such as registry:5000/repo is not mistaken for a tag.
        ref = ref.split('@', 1)[0]
        return ref.rsplit(':', 1)[0] if ':' in ref.rsplit('/', 1)[-1] else ref
    return None


by_repo = {}
for img in images:
    repo = repository_of(img)
    if repo:
        by_repo.setdefault(repo, []).append(img)

removed = []
for imgs in by_repo.values():
    imgs.sort(key=lambda i: i.get('Created', 0), reverse=True)
    for old in imgs[KEEP_PER_REPO:]:
        # Deliberately without -f: podman refuses to remove an image a container still uses, which
        # is what protects a container pinned to an image older than the N most recent ones.
        result = subprocess.run(['podman', 'rmi', old['Id']], capture_output=True)
        if result.returncode == 0:
            removed.append(old['Id'])

# Only catches images with neither a tag nor a digest reference, such as leftover build layers.
# An untagged image that still carries repo@sha256 is not dangling to podman, so the per-repository
# pass above is what applies retention to those.
prune_result = subprocess.run(['podman', 'image', 'prune', '-f'], capture_output=True, text=True)
if prune_result.returncode == 0:
    removed.extend(line for line in prune_result.stdout.splitlines() if line.strip())

print(f"removed:{len(removed)}")
