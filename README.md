# automatic_updates

Ansible automation for patching and maintaining a homelab infrastructure running on Proxmox VE, Foreman/Katello, and Podman. Orchestrated via [SemaphoreUI](https://semaphoreui.com), with a fully dynamic Proxmox inventory for VMs (no static host lists to maintain there).

## Overview

This repository automates the full patch-management lifecycle for a homelab running Rocky Linux 10 VMs (managed by Foreman/Katello, services deployed via rootless/root Podman with [Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)) and a 3-node Proxmox VE cluster:

1. **Promote content views** in Katello (Production, then publish + Test) — `CV_Rocky_10` for the VMs, `CV_Proxmox` for the hypervisors
2. **Check for OS updates** on each VM, snapshot it if any are available, apply them, reboot, and verify the service is healthy again
3. **Check for new container images**, snapshot the VM, pull and redeploy only the containers whose image actually changed, verify health, and prune old images
4. **Clean up** the safety snapshot as soon as the post-update health check passes; a snapshot left behind after a failed check is swept up later by an independently scheduled cleanup run
5. **Patch the Proxmox hosts themselves**: evacuate VMs off a node with memory-aware placement, apply updates, reboot, then rebalance the whole cluster once every node is done
6. **Retain a bounded history** of content view versions in Katello
7. **Back up every VM** weekly via `vzdump` (ZSTD) to local Proxmox storage, upload to pCloud, and prune both retentions — migrating a VM to the backup node first if needed, then rebalancing the cluster afterwards

VM target hosts are discovered automatically from the Proxmox cluster via a dynamic inventory — adding a new VM to Proxmox is enough for it to be picked up (unless explicitly excluded). The 3 Proxmox nodes themselves (`pve1`/`pve2`/`pve3`) can't be discovered that way (they're the hypervisors, not VMs), so they're declared as a small static host list via their own `host_vars/pve*.yml`.

## Playbooks

| Playbook | Purpose |
|---|---|
| `vm-updates.yml` | Promotes the `CV_Rocky_10` content view in Katello, then checks/patches/reboots every VM in the cluster (excluding the Semaphore host itself, which can't safely reboot itself mid-run) |
| `container-updates.yml` | Updates Podman/Quadlet-managed containers on hosts that define `podman_units` in their `host_vars` |
| `cleanup-snapshots.yml` | Removes leftover safety snapshots matching `snapshot_label` (default `ansible_patching`); pass `-e snapshot_label=ansible_container` to clean up the container-update ones instead |
| `pve-updates.yml` | Promotes the `CV_Proxmox` content view, then evacuates/patches/reboots each of the 3 Proxmox nodes in turn (halting the whole run on the first failure), and rebalances VMs across the cluster once every node is updated |
| `cv-retention.yml` | Purges old `CV_Rocky_10`/`CV_Proxmox` content view versions beyond `cv_retention_count` |
| `pve-rebalance-test.yml` | Standalone dry-run harness to compute a `pve_rebalance` plan in isolation, without running a full `pve-updates.yml` cycle |
| `pcloud-backups.yml` | Runs a single `rclone` backup (source/destination/mode passed as extra vars); scheduled as one independent Semaphore template per backup job, replacing what used to be a system crontab on `smb101` |
| `vm-backups.yml` | Runs a `vzdump` (ZSTD) of every VM, migrating it to `pve1` first if it isn't already there (the only node with the `Stockage_SSD` backup storage), uploads the archive to pCloud, prunes both retentions, then rebalances the cluster if anything was migrated |
| `full-updates.yml` | Runs `container-updates.yml`, then `vm-updates.yml`, then `pve-updates.yml` in one go — each stage only runs if the previous one had no problem, otherwise later stages report as `SKIPPED` |

`vm-updates.yml` and `container-updates.yml` already remove their own snapshot as soon as the post-update health check passes — `cleanup-snapshots.yml` is the backstop for the ones deliberately left behind after a failed health check, run on its own independent schedule (as two separate Semaphore templates, one per `snapshot_label` value) since OS patching and container updates don't necessarily run on the same cadence.

## Repository structure
- **`vm-updates.yml`** — Main OS patching + Katello promotion playbook (VMs)
- **`container-updates.yml`** — Podman container update playbook
- **`cleanup-snapshots.yml`** — Removes leftover snapshots for a given `snapshot_label` (parametrized, see [Playbooks](#playbooks))
- **`pve-updates.yml`** — Proxmox host patching: `CV_Proxmox` promotion, per-node evacuate/patch/reboot, cluster rebalance
- **`cv-retention.yml`** — Purges old content view versions beyond `cv_retention_count`
- **`pve-rebalance-test.yml`** — Standalone dry-run test harness for the `pve_rebalance` role
- **`pcloud-backups.yml`** — Runs one `rclone` backup per invocation (source/dest/mode via extra vars); each job gets its own scheduled Semaphore template
- **`vm-backups.yml`** — Weekly `vzdump` of every VM to `Stockage_SSD` then pCloud, with migration + rebalance as needed
- **`full-updates.yml`** — Chains `container-updates.yml` → `vm-updates.yml` → `pve-updates.yml`, gated on each stage's outcome
- **`ansible.cfg`** — Silences interpreter discovery warnings
- **`requirements.txt`** — Python deps (proxmoxer, requests) for the inventory plugin
- **`collections/requirements.yml`** — Ansible collections (community.proxmox, ansible.posix, community.general)
- **`inventory/proxmox.yml`** — Dynamic Proxmox inventory for VMs (API token via env var)
- **`group_vars/all.yml`** — Shared variables (Proxmox API host, Katello org, SMTP, retention/rebalance tuning, etc.)
- **`host_vars/`** — Per-VM variables (health checks, `podman_units`) and per-Proxmox-node connection details (`pve1`/`pve2`/`pve3`)
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
  - `capture_start_time/` — Record a start-time epoch on localhost (once), for the run duration shown in reports
  - `capture_host_list/` — Snapshot the play's host list onto localhost, for the final report
  - `host_status/` — Derive a host's overall OK/PROBLEM status from its health check results
  - `send_report/` — Shared HTML email skeleton + CSS; sends the per-playbook report body
  - `pve_evacuate_host/` — Compute placement, then live-migrate every VM off a Proxmox node
  - `pve_migrate_vm/` — Live-migrate a single VM between Proxmox nodes (residual snapshot cleanup, async wait, health check)
  - `pve_placement/` — Memory-aware placement calculation for evacuating one node
  - `pve_rebalance/` — Cluster-wide rebalance calculation and move execution
  - `vzdump_backup/` — Run vzdump, locate the resulting archive, upload it to pCloud, prune the pCloud retention
- **`BACKLOG.md`** — Planned future automation work

## How it works

### Dynamic inventory

`inventory/proxmox.yml` uses the `community.proxmox.proxmox` plugin, authenticating with a Proxmox API token (never stored in this repo — see [Secrets](#secrets)). VMs are grouped automatically (`proxmox_all_qemu`, per-node groups, etc.), and each VM's node/vmid are exposed as host facts, which the `proxmox_snapshot` and `cleanup_snapshot` roles use to target the right cluster node via `pvesh`. The 3 Proxmox nodes themselves aren't part of this dynamic inventory (they're hypervisors, not VMs) — they're a small static host list, each with its own `host_vars/pve{1,2,3}.yml`.

### Per-host configuration

Each host that needs container updates or health checks declares its configuration in `host_vars/<hostname>.yml`:

```yaml
health_check_url: "http://192.168.1.x:PORT"
health_check_podman_user: deploy   # or root, or a specific user like "immich"
health_check_validate_certs: false # optional, only if health_check_url is https with a self-signed/invalid cert
health_check_retries: 30           # optional, only if the service is slow to start (default: 5 retries, 10s apart)
health_check_delay: 10
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

### pCloud backups

`pcloud-backups.yml` runs a single `rclone` command per invocation — `pcloud_backup_source`, `pcloud_backup_dest`, `pcloud_backup_mode` (`sync` or `copy`, defaults to `sync`) and `pcloud_backup_extra_args` are passed as extra vars, not read from `host_vars`. This replaces what used to be a hand-edited crontab for the `deploy` user on `smb101`: each backup (Home Assistant, Immich per-user exports, …) is its own Semaphore template pointing at this same playbook, with its own schedule and its own set of extra vars — the same "one generic playbook, several scheduled templates" pattern already used for `cleanup-snapshots.yml`.

### Safety snapshots

Both `vm-updates.yml` and `container-updates.yml` take a Proxmox snapshot before making any change (`ansible_patching` and `ansible_container` respectively), using ZFS copy-on-write storage so the cost is negligible until the underlying data actually changes. The snapshot is removed automatically as soon as the post-update health check passes; if the health check fails, the snapshot is left in place for a manual rollback decision, and `cleanup-snapshots.yml` won't touch it until you've dealt with it.

`pve-updates.yml` takes a different safety approach, since it patches the hypervisors themselves rather than a single VM: if a node fails mid-run, a `rescue` block marks it `BLOCKED` and halts the entire run (`meta: end_play`) so no further node is touched, and the final cluster rebalance is skipped.

### Orchestrated run

`ansible.builtin.import_playbook` (used by `full-updates.yml`) concatenates whole playbooks into one run but doesn't support `when:`, so there's no native "run playbook B only if playbook A succeeded". Instead, `container-updates.yml`, `vm-updates.yml` and `pve-updates.yml` each read and write a shared fact, `orchestration_should_run`, set on `localhost`:

- Each stage's real work is wrapped in `when: hostvars['localhost'].orchestration_should_run | default(true)` — the `default(true)` means each playbook behaves completely normally when run standalone from its own Semaphore template, exactly as before.
- After running, each stage combines the incoming gate with its own outcome (`stage_was_allowed_to_run and not <this stage's problem flag>`), so a stage that was itself skipped can't accidentally re-open the gate for the next one.

**Reports**: run standalone, each playbook still sends its own single email exactly as before (subject/body marked `SKIPPED (previous stage had a problem)` if it was itself skipped). Run via `full-updates.yml`, the 3 individual emails are suppressed (`is_orchestrated_run`, set by a leading play before the 3 `import_playbook`s) and replaced by **one** combined email at the end: a summary table (stage, status, duration for each of the 3), followed by each stage's full report body concatenated in — plus the overall run duration, which `send_report` already adds automatically from the shared `run_start_epoch` fact. Per-stage durations use the same `capture_start_time` role with a second, stage-scoped fact name (`capture_start_time_fact_name`) alongside the existing global one.

## Requirements

- SemaphoreUI (or plain `ansible-playbook`) with the following installed:
  - Collections: `ansible-galaxy collection install -r collections/requirements.yml`
  - Python packages: `pip install -r requirements.txt`
- A Proxmox API token with sufficient privileges to list VMs, create/delete snapshots, and migrate VMs
- SSH access to all target hosts, and to the Proxmox nodes, using a key stored in Semaphore's Key Store (never committed to this repo)
- `rclone` installed and configured with a `pcloud:` remote on `smb101` (for `pcloud-backups.yml`) and on `pve1` (for `vm-backups.yml`)

## Secrets

No credentials are stored in this repository. The Proxmox API token is read via `lookup('env', 'PROXMOX_TOKEN_SECRET')` in `inventory/proxmox.yml`, injected by Semaphore through a Variable Group. SSH keys live exclusively in Semaphore's Key Store.

## Roadmap

See [`BACKLOG.md`](./BACKLOG.md) for planned work: a BunkerWeb reverse proxy rollout.
