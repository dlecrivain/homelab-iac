# automatic_updates

Ansible automation for patching and maintaining a homelab infrastructure running on Proxmox VE, Foreman/Katello, and Podman. Orchestrated via [SemaphoreUI](https://semaphoreui.com), with a fully dynamic Proxmox inventory (no static host lists to maintain).

## Overview

This repository automates the full patch-management lifecycle for a set of Rocky Linux 10 VMs managed by Foreman/Katello, running services deployed via rootless/root Podman with [Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html):

1. **Promote content views** in Katello (Production, then publish + Test)
2. **Check for OS updates**, snapshot the VM if any are available, apply them, reboot, and verify the service is healthy again
3. **Check for new container images**, snapshot the VM, pull and redeploy only the containers whose image actually changed, verify health, and prune old images
4. **Clean up** the safety snapshots on their own independent schedule

All target hosts are discovered automatically from the Proxmox cluster via a dynamic inventory — adding a new VM to Proxmox is enough for it to be picked up (unless explicitly excluded).

## Playbooks

| Playbook | Purpose |
|---|---|
| `vm-updates.yml` | Promotes the `CV_Rocky_10` content view in Katello, then checks/patches/reboots every VM in the cluster (excluding the Foreman and Semaphore hosts themselves) |
| `container-updates.yml` | Updates Podman/Quadlet-managed containers on hosts that define `podman_units` in their `host_vars` |
| `cleanup-snapshots.yml` | Removes leftover safety snapshots matching `snapshot_label` (default `ansible_patching`); pass `-e snapshot_label=ansible_container` to clean up the container-update ones instead |

`vm-updates.yml` and `container-updates.yml` already remove their own snapshot as soon as the post-update health check passes — `cleanup-snapshots.yml` is the backstop for the ones deliberately left behind after a failed health check, run on its own independent schedule (as two separate Semaphore templates, one per `snapshot_label` value) since OS patching and container updates don't necessarily run on the same cadence.

## Repository structure
- **`vm-updates.yml`** — Main OS patching + Katello promotion playbook
- **`container-updates.yml`** — Podman container update playbook
- **`cleanup-snapshots.yml`** — Removes leftover snapshots for a given `snapshot_label` (parametrized, see [Playbooks](#playbooks))
- **`ansible.cfg`** — Silences interpreter discovery warnings
- **`requirements.txt`** — Python deps (proxmoxer, requests) for the inventory plugin
- **`collections/requirements.yml`** — Ansible collections (community.proxmox, ansible.posix)
- **`inventory/proxmox.yml`** — Dynamic Proxmox inventory (API token via env var)
- **`group_vars/all.yml`** — Shared variables (Proxmox API host, Katello org, etc.)
- **`host_vars/`** — Per-host variables: health checks, podman_units, etc.
- **`roles/`**
  - `katello_promote/` — Content view promote/publish via hammer
  - `check_updates/` — dnf/apt update check
  - `proxmox_snapshot/` — Create a named Proxmox snapshot
  - `apply_updates/` — dnf/apt upgrade
  - `reboot_and_wait/` — Reboot and wait for the host to come back
  - `health_check/` — Verify containers are up + HTTP endpoint responds
  - `cleanup_snapshot/` — Remove a named Proxmox snapshot
  - `podman_update/` — Pull + conditionally restart a Quadlet unit
  - `image_retention/` — Prune old container images, keep the 2 most recent
- **`BACKLOG.md`** — Planned future automation work

## How it works

### Dynamic inventory

`inventory/proxmox.yml` uses the `community.proxmox.proxmox` plugin, authenticating with a Proxmox API token (never stored in this repo — see [Secrets](#secrets)). VMs are grouped automatically (`proxmox_all_qemu`, per-node groups, etc.), and each VM's node/vmid are exposed as host facts, which the `proxmox_snapshot` and `cleanup_*` roles use to target the right cluster node via `pvesh`.

### Per-host configuration

Each host that needs container updates or health checks declares its configuration in `host_vars/<hostname>.yml`:

```yaml
health_check_url: "http://192.168.1.x:PORT"
health_check_podman_user: deploy   # or root, or a specific user like "immich"
health_check_containers:
  - container_name_1
  - container_name_2

podman_units:
  - name: container_name_1          # actual Podman container name
    scope: user                     # "user" (rootless) or "root"
    service_name: some-service      # optional, only if it differs from `name`
    become_user: someuser           # optional, only if different from the SSH login user
```

This makes the playbooks fully generic: no host-specific logic lives in the roles themselves.

### Safety snapshots

Both `vm-updates.yml` and `container-updates.yml` take a Proxmox snapshot before making any change (`ansible_patching` and `ansible_container` respectively), using ZFS copy-on-write storage so the cost is negligible until the underlying data actually changes. If anything fails mid-update, the `rescue` block logs which snapshot to roll back to.

## Requirements

- SemaphoreUI (or plain `ansible-playbook`) with the following installed:
  - Collections: `ansible-galaxy collection install -r collections/requirements.yml`
  - Python packages: `pip install -r requirements.txt`
- A Proxmox API token with sufficient privileges to list VMs, create/delete snapshots, and (for the future PVE update playbook) migrate VMs
- SSH access to all target hosts, and to the Proxmox nodes, using a key stored in Semaphore's Key Store (never committed to this repo)

## Secrets

No credentials are stored in this repository. The Proxmox API token is read via `lookup('env', 'PROXMOX_TOKEN_SECRET')` in `inventory/proxmox.yml`, injected by Semaphore through a Variable Group. SSH keys live exclusively in Semaphore's Key Store.

## Roadmap

See [`BACKLOG.md`](./BACKLOG.md) for planned work: a PVE (Proxmox host) update playbook with smart VM migration, a BunkerWeb reverse proxy rollout, weekly VM backups to pCloud, and content-view version retention.
