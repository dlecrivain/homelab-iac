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
8. **Patch Semaphore itself**: a second instance (`semaphore102`) patches the primary (`semaphore101`) OS + Podman images on its own schedule; `full-updates.yml` runs on `semaphore101` on a separate schedule timed to start afterward, and patches `semaphore102` back once everything else succeeds — so neither instance ever has to update itself

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
| `pcloud-backups.yml` | Runs a single `rclone` backup when `pcloud_backup_source` is passed as an extra var (one independent Semaphore template per backup job, replacing what used to be a system crontab on `smb101`); with no extra vars, runs all 3 built-in backups (Home Assistant, Immich Daniel, Immich Marine) in parallel instead — one failed job doesn't stop the others |
| `vm-backups.yml` | Runs a `vzdump` (ZSTD) of every VM, migrating it to `pve1` first if it isn't already there (the only node with the `Stockage_SSD` backup storage), uploads the archive to pCloud, prunes both retentions, then rebalances the cluster if anything was migrated |
| `full-updates.yml` | Runs `container-updates.yml`, then `vm-updates.yml`, then `pve-updates.yml` in one go — each stage only runs if the previous one had no problem, otherwise later stages report as `SKIPPED`; finishes by patching `semaphore102` back |
| `update-semaphore-peer.yml` | OS-patches + Podman-updates `{{ peer_host }}` (`semaphore101` or `semaphore102`), then pings its Semaphore web service to confirm it's back up — the mechanism behind item 8 above |
| `provision-semaphore-peer.yml` | One-time setup: deploys Semaphore via Podman Quadlet onto a fresh `{{ peer_host }}` VM, identically to the existing instance, then waits for its web service to respond |
| `bump-semaphore-image.yml` | Manual, deliberate action: bumps `{{ peer_host }}`'s live `semaphore.container` to `{{ semaphore_image_tag }}` and restarts it — run from the *other* Semaphore instance, never from the one being restarted |

`vm-updates.yml` and `container-updates.yml` already remove their own snapshot as soon as the post-update health check passes — `cleanup-snapshots.yml` is the backstop for the ones deliberately left behind after a failed health check, run on its own independent schedule (as two separate Semaphore templates, one per `snapshot_label` value) since OS patching and container updates don't necessarily run on the same cadence.

## Repository structure
- **`vm-updates.yml`** — Main OS patching + Katello promotion playbook (VMs)
- **`container-updates.yml`** — Podman container update playbook
- **`cleanup-snapshots.yml`** — Removes leftover snapshots for a given `snapshot_label` (parametrized, see [Playbooks](#playbooks))
- **`pve-updates.yml`** — Proxmox host patching: `CV_Proxmox` promotion, per-node evacuate/patch/reboot, cluster rebalance
- **`cv-retention.yml`** — Purges old content view versions beyond `cv_retention_count`
- **`pve-rebalance-test.yml`** — Standalone dry-run test harness for the `pve_rebalance` role
- **`pcloud-backups.yml`** — Runs one `rclone` backup per invocation (source/dest/mode via extra vars, one scheduled Semaphore template per job), or all 3 built-in jobs in parallel when called with no extra vars
- **`vm-backups.yml`** — Weekly `vzdump` of every VM to `Stockage_SSD` then pCloud, with migration + rebalance as needed
- **`full-updates.yml`** — Chains `container-updates.yml` → `vm-updates.yml` → `pve-updates.yml` → patches `semaphore102` back, gated on each stage's outcome
- **`update-semaphore-peer.yml`** — OS-patches + Podman-updates one Semaphore peer, then confirms its Semaphore web service is back up
- **`provision-semaphore-peer.yml`** — One-time deploy of Semaphore (Podman Quadlet) onto a fresh peer VM
- **`bump-semaphore-image.yml`** — Manually bumps one peer's Semaphore image tag and restarts it, run from the other peer
- **`ansible.cfg`** — Silences interpreter discovery warnings
- **`requirements.txt`** — Python deps (proxmoxer, requests) for the inventory plugin
- **`collections/requirements.yml`** — Ansible collections (community.proxmox, ansible.posix, community.general)
- **`inventory/proxmox.yml`** — Dynamic Proxmox inventory for VMs (API token via env var)
- **`group_vars/all.yml`** — Shared variables (Proxmox API host, Katello org, SMTP, retention/rebalance tuning, etc.)
- **`host_vars/`** — Per-VM variables (health checks, `podman_units`) and per-Proxmox-node connection details (`pve1`/`pve2`/`pve3`); `semaphore101`/`semaphore102` declare their own `semaphore`/`semaphore_db` Podman units like any other host
- **`roles/`**
  - `katello_promote/` — Content view promote/publish via hammer
  - `check_updates/` — dnf/apt update check
  - `proxmox_snapshot/` — Create a named Proxmox snapshot
  - `apply_updates/` — dnf/apt upgrade
  - `reboot_and_wait/` — Reboot and wait for the host to come back
  - `health_check/` — Verify containers are up + HTTP endpoint responds
  - `cleanup_snapshot/` — Remove a named Proxmox snapshot
  - `podman_update/` — Pull + conditionally restart a Quadlet unit
  - `podman_align_image/` — Pull a specific image digest (rather than re-resolving a tag) + conditionally restart a Quadlet unit, so a peer host can be forced to match another host's exact running image versions
  - `semaphore_provision/` — Deploy Semaphore's 7 Quadlet unit files + generate fresh app/DB secrets + clone this repo, for a brand-new Semaphore peer VM
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
  - `pcloud_backup_job/` — Run a single `rclone` backup (source/dest/mode/extra_args), used by `pcloud-backups.yml`
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

`pcloud-backups.yml` has two modes, switched on whether `pcloud_backup_source` is passed as an extra var:

- **Single-job mode** (`pcloud_backup_source`, `pcloud_backup_dest`, `pcloud_backup_mode` — `sync` or `copy`, defaults to `sync` — and `pcloud_backup_extra_args` passed as extra vars, not read from `host_vars`): runs one `rclone` command via the `pcloud_backup_job` role. This replaces what used to be a hand-edited crontab for the `deploy` user on `smb101`: each backup (Home Assistant, Immich per-user exports, …) is its own Semaphore template pointing at this same playbook with its own schedule and its own set of extra vars — the same "one generic playbook, several scheduled templates" pattern already used for `cleanup-snapshots.yml`.
- **All-jobs mode** (no extra vars): runs all 3 jobs from the `pcloud_backup_jobs` list built into the playbook, in **parallel** — useful for an on-demand "back up everything now" run. It launches all 3 `rclone` commands with `async`/`poll: 0` (same pattern as the VM migration/vzdump waits elsewhere in this repo), then waits for all of them via `async_status`, so the 3 backups run concurrently rather than one after another, and one failing doesn't stop the others. This mode doesn't go through the `pcloud_backup_job` role — `async`/`poll` only apply to a single module task, not to `include_role` — so the `rclone` command is inlined directly there instead.

### Safety snapshots

Both `vm-updates.yml` and `container-updates.yml` take a Proxmox snapshot before making any change (`ansible_patching` and `ansible_container` respectively), using ZFS copy-on-write storage so the cost is negligible until the underlying data actually changes. The snapshot is removed automatically as soon as the post-update health check passes; if the health check fails, the snapshot is left in place for a manual rollback decision, and `cleanup-snapshots.yml` won't touch it until you've dealt with it.

Per-host work in both playbooks is also wrapped in a `block`/`rescue`: an unhandled task failure (a registry pull timing out, a transient network blip, …) on one host is caught, recorded as `host_overall_status: PROBLEM` with the failure reason shown in the report, and the run continues with the next host instead of aborting the whole playbook — which also matters for `full-updates.yml`, since an uncaught failure would otherwise kill the entire orchestrated chain rather than just failing that one stage's gate.

`pve-updates.yml` takes a different safety approach, since it patches the hypervisors themselves rather than a single VM: if a node fails mid-run, a `rescue` block marks it `BLOCKED` and halts the entire run (`meta: end_play`) so no further node is touched, and the final cluster rebalance is skipped.

### Orchestrated run

`ansible.builtin.import_playbook` (used by `full-updates.yml`) concatenates whole playbooks into one run but doesn't support `when:`, so there's no native "run playbook B only if playbook A succeeded". Instead, `container-updates.yml`, `vm-updates.yml` and `pve-updates.yml` each read and write a shared fact, `orchestration_should_run`, set on `localhost`:

- Each stage's real work is wrapped in `when: hostvars['localhost'].orchestration_should_run | default(true)` — the `default(true)` means each playbook behaves completely normally when run standalone from its own Semaphore template, exactly as before.
- After running, each stage combines the incoming gate with its own outcome (`stage_was_allowed_to_run and not <this stage's problem flag>`), so a stage that was itself skipped can't accidentally re-open the gate for the next one.

**Reports**: run standalone, each playbook still sends its own single email exactly as before (subject/body marked `SKIPPED (previous stage had a problem)` if it was itself skipped). Run via `full-updates.yml`, the 3 individual emails are suppressed (`is_orchestrated_run`, set by a leading play before the 3 `import_playbook`s) and replaced by **one** combined email at the end: a summary table (stage, status, duration for each of the 3), followed by each stage's full report body concatenated in — plus the overall run duration, which `send_report` already adds automatically from the shared `run_start_epoch` fact. Per-stage durations use the same `capture_start_time` role with a second, stage-scoped fact name (`capture_start_time_fact_name`) alongside the existing global one.

### Mutual Semaphore updates

`semaphore101` has always been excluded from `vm-updates.yml`/`container-updates.yml` — it can't safely patch-and-reboot itself while it's the thing running the automation. `semaphore102` exists purely to break that deadlock, using `update-semaphore-peer.yml` in both directions, via two independently-scheduled Semaphore templates:

1. A template on `semaphore102` runs `update-semaphore-peer.yml -e peer_host=semaphore101` on its own schedule. This patches `semaphore101`'s OS and Podman images (reusing the same roles `vm-updates.yml` uses, minus its Katello promotion play — that would otherwise re-run redundantly here *and* again moments later when `full-updates.yml` starts), then waits for its Semaphore web service (`/api/ping`) to respond before finishing.
2. `full-updates.yml` runs on `semaphore101` on its own separate schedule, timed to start after `semaphore102`'s job is expected to be done, so it always finds `semaphore101` already up to date. It runs the whole fleet as usual, and finishes with `import_playbook: update-semaphore-peer.yml, vars: {peer_host: semaphore102, image_reference_host: semaphore101}` — gated by the same `orchestration_should_run` fact as every other stage, so `semaphore102` only gets patched back if nothing upstream failed.

There's no direct hand-off between the two instances — no API call, no shared flag — just two schedules timed so each one's prerequisite is done by the time it runs.

**Keeping Postgres/Semaphore image versions identical**: `podman_update`'s normal "pull whatever the configured tag currently resolves to" is fine for most hosts, but for this pair specifically it risks drift — a floating tag (e.g. `postgres:16`) could resolve to a newer patch release on `semaphore102` than what `semaphore101` is actually running, if a new image was published upstream between the two runs. So when `image_reference_host` is passed, `update-semaphore-peer.yml` adds two extra plays after the normal container update:
1. It checks `image_reference_host` (`semaphore101`) is healthy right now, then reads the exact image digest each of its Podman units (`semaphore`, `semaphore_db`) is *actually running* (`podman inspect` — not the configured tag, the resolved digest) and saves it on `localhost`.
2. If that succeeded, it forces `peer_host` (`semaphore102`) to pull and tag that exact digest for each matching unit (`podman_align_image` role), restarting only if it actually changed something — so `semaphore102` always ends up bit-for-bit identical to `semaphore101`, regardless of whether the image's tag is fully pinned or floating.

This only runs in the `semaphore102`-facing direction (`image_reference_host` is only passed by `full-updates.yml`); when `semaphore102` patches `semaphore101` the other way, `image_reference_host` is omitted and each unit is simply pulled by its configured tag as normal.

**Version checks (notify only, never auto-applied)**: `semaphore.container` and `semaphore-db.container` are pinned to exact/major versions on purpose (see [Provisioning a new Semaphore peer](#provisioning-a-new-semaphore-peer)), so the normal update flow never bumps Semaphore or Postgres to a new major version by itself. `update-semaphore-peer.yml` ends with two read-only checks, run in both directions: one compares the running Semaphore image against the latest GitHub release, the other compares the running Postgres major version against the latest tag published on Docker Hub. Either sends its own email the moment a newer version exists — nothing is ever pulled or restarted as a result. Semaphore's own image is a simple stateless swap-and-restart when you do decide to bump it by hand; Postgres is not — a major-version bump needs a real migration (`pg_upgrade` or dump/restore) since Postgres refuses to start against a data directory from a different major version, so the Postgres email spells that out explicitly rather than treating it like a routine update.

When a newer Semaphore version notification arrives and you've decided to act on it, `bump-semaphore-image.yml` does the actual bump as an Ansible task rather than a manual SSH session — but it still has to be triggered from the *other* peer (a one-off Semaphore template run on `semaphore102` with `-e peer_host=semaphore101 -e semaphore_image_tag=vX.Y.Z` to bump `semaphore101`, or the reverse to bump `semaphore102`), since restarting `semaphore.service` from within a run executing on that same instance would kill the task itself mid-flight. It's deliberately not wired into any schedule — always a manual, one-off run. Remember to also bump the `Image=` line in `roles/semaphore_provision/files/semaphore.container` and commit it, so a future fresh provision starts at the new version too.

### Provisioning a new Semaphore peer

`provision-semaphore-peer.yml` (run from `semaphore101`, targeting the brand-new VM) deploys Semaphore identically to the existing instance: it copies the 7 Quadlet unit files (`semaphore-network.network`, 4 `.volume` files, `semaphore-db.container`, `semaphore.container` — byte-identical to `semaphore101`'s, since nothing in them is host-specific), generates **fresh** `SEMAPHORE_DB_PASS`/`POSTGRES_PASSWORD` (kept in sync with each other) and `SEMAPHORE_ADMIN_PASSWORD` secrets rather than reusing `semaphore101`'s real ones, clones this repo into `/home/deploy/repos/homelab-iac` (bind-mounted into the container as `/repos`), then enables and starts both services.

It's safe to re-run: if `/etc/semaphore/app.env` already exists, the secrets and env files are left untouched (regenerating them on an already-initialized Postgres data volume would desync the stored DB password from a freshly-rewritten one).

Not handled by this playbook, and needs a short manual checklist once the containers are up:
- VM creation itself (Proxmox side) — same manual process as any other VM in this cluster.
- Recreating Semaphore's own data (projects/templates/environments/credentials — these live in `semaphore102`'s own Postgres DB, not in anything this repo touches): the SSH key in the Key Store (same key as `semaphore101`'s), the Proxmox/SMTP Variable Group, the repo connection (pointing at `/repos`), and at minimum the two templates this design needs — "Update semaphore101" (`update-semaphore-peer.yml -e peer_host=semaphore101`) on its own schedule, and (on `semaphore101`) "Full updates" (`full-updates.yml`) timed to run after it.
- Changing `SEMAPHORE_ADMIN_PASSWORD` from its generated value via Semaphore's own UI on first login (same as should be done for `semaphore101`'s `changeme` default while at it).

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
