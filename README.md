# automatic_updates

Ansible automation for patching and maintaining a homelab infrastructure running on Proxmox VE, Foreman/Katello, and Podman. Orchestrated via [SemaphoreUI](https://semaphoreui.com), with a fully dynamic Proxmox inventory for VMs (no static host lists to maintain there).

## Overview

This repository automates the full patch-management lifecycle for a homelab running Rocky Linux 10 VMs (managed by Foreman/Katello, services deployed via rootless/root Podman with [Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)) and a 3-node Proxmox VE cluster:

1. **Promote content views** in Katello (Production, then publish + Test) — `CV_Rocky_10` for the VMs, `CV_Proxmox` for the hypervisors — via its own independently scheduled playbook, so re-running a patch stage never republishes a new content view version
2. **Check for OS updates** on each VM, snapshot it if any are available, apply them, reboot, and verify the service is healthy again
3. **Check for new container images**, snapshot the VM, pull and redeploy only the containers whose image actually changed, verify health, and prune old images
4. **Clean up** the safety snapshot as soon as the post-update health check passes; a snapshot left behind after a failed check is swept up later by an independently scheduled cleanup run
5. **Patch the Proxmox hosts themselves**: evacuate VMs off a node with memory-aware placement, apply updates, reboot, then rebalance the whole cluster once every node is done
6. **Retain a bounded history** of content view versions in Katello
7. **Back up every VM** weekly via `vzdump` (ZSTD) to local Proxmox storage, upload to pCloud, and prune both retentions — migrating a VM to the backup node first if needed (3 at a time), then straight back to its own node once its backup is done, so the backup node never ends up hosting the whole fleet at once; a final cluster rebalance only runs as a safety net if a migrate-back itself failed
8. **Patch Semaphore itself**: a second instance (`semaphore102`) patches the primary (`semaphore101`) OS + Podman images on its own schedule; `full-updates.yml` runs on `semaphore101` on a separate schedule timed to start afterward, and patches `semaphore102` back once everything else succeeds — so neither instance ever has to update itself

VM target hosts are discovered automatically from the Proxmox cluster via a dynamic inventory — adding a new VM to Proxmox is enough for it to be picked up (unless explicitly excluded). The 3 Proxmox nodes themselves (`pve1`/`pve2`/`pve3`) can't be discovered that way (they're the hypervisors, not VMs), so they're declared as a small static host list via their own `host_vars/pve*.yml`.

## Playbooks

| Playbook | Purpose |
|---|---|
| `katello-publish.yml` | Promotes the current latest version of `CV_Rocky_10` and `CV_Proxmox` to Production, then publishes and promotes a new version to Test — run on its own independent schedule, with enough lead time before `vm-updates.yml`/`pve-updates.yml`/`full-updates.yml` so they see the content it published |
| `vm-updates.yml` | Checks/patches/reboots every VM in the cluster (excluding the Semaphore hosts themselves, which can't safely reboot themselves mid-run) |
| `container-updates.yml` | Updates Podman/Quadlet-managed containers on hosts that define `podman_units` in their `host_vars` — `semaphore102` is included but aligned to `semaphore101`'s exact image digest instead of pulled independently, see [Mutual Semaphore updates](#mutual-semaphore-updates) |
| `cleanup-snapshots.yml` | Removes leftover safety snapshots matching `snapshot_label` (default `ansible_patching`); pass `-e snapshot_label=ansible_container` to clean up the container-update ones instead |
| `pve-updates.yml` | Evacuates/patches/reboots each of the 3 Proxmox nodes in turn (halting the whole run on the first failure), and rebalances VMs across the cluster once every node is updated |
| `cv-retention.yml` | Purges old `CV_Rocky_10`/`CV_Proxmox` content view versions beyond `cv_retention_count` |
| `pve-rebalance-test.yml` | Standalone dry-run harness to compute a `pve_rebalance` plan in isolation, without running a full `pve-updates.yml` cycle |
| `pcloud-backups.yml` | Runs a single `rclone` backup when `pcloud_backup_source` is passed as an extra var (one independent Semaphore template per backup job, replacing what used to be a system crontab on `smb101`); with no extra vars, runs all 3 built-in backups (Home Assistant, Immich Daniel, Immich Marine) in parallel instead — one failed job doesn't stop the others |
| `vm-backups.yml` | Runs a `vzdump` (ZSTD) of every VM (3 at a time), migrating each to `pve1` first if it isn't already there (the only node with the `Stockage_SSD` backup storage), uploads the archive to pCloud, prunes both retentions, then migrates it straight back to its own node; a final rebalance only triggers if a migrate-back failed and left something stranded on `pve1` |
| `full-updates.yml` | Runs `container-updates.yml`, then `vm-updates.yml`, then `pve-updates.yml` in one go — each stage only runs if the previous one had no problem, otherwise later stages report as `SKIPPED`; finishes by patching `semaphore102` back |
| `update-semaphore-peer.yml` | OS-patches + Podman-updates `{{ peer_host }}` (`semaphore101` or `semaphore102`), auto-bumps its Semaphore image to the latest patch/minor within the same major line (or an explicit `semaphore_image_tag`, for a major jump), then pings its Semaphore web service to confirm it's back up — the mechanism behind item 8 above |
| `provision-semaphore-peer.yml` | One-time setup: deploys Semaphore via Podman Quadlet onto a fresh `{{ peer_host }}` VM, identically to the existing instance, then waits for its web service to respond |
| `provision-vm.yml` | Full-clones the (cloud-init-free) golden template, resolves a static IP via phpIPAM, reconfigures the clone's network/hostname/SSH host key, registers it with Katello, updates it and installs Podman+git, then commits its `host_vars` to this repo and rebalances the cluster — see [Provisioning a new fleet VM](#provisioning-a-new-fleet-vm) |
| `decommission-vm.yml` | The reverse of `provision-vm.yml`: takes a final safety backup, deregisters the host from Katello, removes its IP from phpIPAM, destroys the Proxmox VM, then removes and commits the deletion of its `host_vars` — see [Decommissioning a fleet VM](#decommissioning-a-fleet-vm) |
| `remove-cloud-init.yml` | One-time cleanup for the legacy VMs that predate `provision-vm.yml` (`adguard101`, `patchmon101`, `phpipam101`, `lpkat101`, `immich101`, `semaphore102`, `smb101`): converts their network config to a persistent static NetworkManager profile, removes cloud-init and its Proxmox drive, then asserts the SSH host key survives the reboot unchanged — see [Removing cloud-init from a legacy VM](#removing-cloud-init-from-a-legacy-vm) |

`vm-updates.yml` and `container-updates.yml` already remove their own snapshot as soon as the post-update health check passes — `cleanup-snapshots.yml` is the backstop for the ones deliberately left behind after a failed health check, run on its own independent schedule (as two separate Semaphore templates, one per `snapshot_label` value) since OS patching and container updates don't necessarily run on the same cadence.

## Repository structure
- **`katello-publish.yml`** — Standalone Katello content view promote/publish (`CV_Rocky_10`, `CV_Proxmox`), independent of any patch run
- **`vm-updates.yml`** — Main OS patching playbook (VMs)
- **`container-updates.yml`** — Podman container update playbook
- **`cleanup-snapshots.yml`** — Removes leftover snapshots for a given `snapshot_label` (parametrized, see [Playbooks](#playbooks))
- **`pve-updates.yml`** — Proxmox host patching: per-node evacuate/patch/reboot, cluster rebalance
- **`cv-retention.yml`** — Purges old content view versions beyond `cv_retention_count`
- **`pve-rebalance-test.yml`** — Standalone dry-run test harness for the `pve_rebalance` role
- **`pcloud-backups.yml`** — Runs one `rclone` backup per invocation (source/dest/mode via extra vars, one scheduled Semaphore template per job), or all 3 built-in jobs in parallel when called with no extra vars
- **`vm-backups.yml`** — Weekly `vzdump` of every VM to `Stockage_SSD` then pCloud, migrating each VM to the backup node and straight back afterwards as needed (3 at a time; a final rebalance only runs as a safety net for a failed migrate-back)
- **`full-updates.yml`** — Chains `container-updates.yml` → `vm-updates.yml` → `pve-updates.yml` → patches `semaphore102` back, gated on each stage's outcome
- **`update-semaphore-peer.yml`** — OS-patches + Podman-updates one Semaphore peer, auto-bumps its Semaphore image (patch/minor), and confirms its Semaphore web service is back up
- **`provision-semaphore-peer.yml`** — One-time deploy of Semaphore (Podman Quadlet) onto a fresh peer VM
- **`provision-vm.yml`** — Clone-based provisioning for a new generic fleet VM (see [Provisioning a new fleet VM](#provisioning-a-new-fleet-vm))
- **`decommission-vm.yml`** — Backs up, deregisters and destroys a fleet VM (see [Decommissioning a fleet VM](#decommissioning-a-fleet-vm))
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
  - `phpipam_login/` — Log in to phpIPAM (`User token` mode) and resolve a subnet ID from its CIDR; shared by every other phpIPAM role
  - `phpipam_next_ip/` — Resolve a free IP from phpIPAM, with a liveness-probe safety net that self-heals phpIPAM's data if it's stale
  - `phpipam_remove_address/` — Find and delete a phpIPAM address entry by IP (a no-op if it's already gone)
  - `pve_clone_vm/` — Full-clone the golden template, start it, confirm it's running
  - `pve_destroy_vm/` — Shut down and destroy (`--purge`) a Proxmox VM
  - `provision_vm_reconnect/` — Reconfigure a freshly cloned VM's IP/hostname/SSH host key, then reconnect at its final identity
  - `katello_register_host/` — Generate and run a Katello global registration command for a host
  - `katello_deregister_host/` — Confirm the host exists in Katello under its exact name, then delete it
  - `resolve_proxmox_api_host/` — Probe pve1/pve2/pve3 in order (SSH:22) and save the first reachable one as `proxmox_api_host`, so a down `pve1` doesn't take out every Proxmox-dependent playbook
  - `remove_cloud_init/` — Convert a live host's network to a persistent static NetworkManager profile, remove cloud-init and its Proxmox drive, then assert the SSH host key and network config survive a reboot unchanged

## How it works

### Dynamic inventory

`inventory/proxmox.yml` uses the `community.proxmox.proxmox` plugin, authenticating with a Proxmox API token (never stored in this repo — see [Secrets](#secrets)). VMs are grouped automatically (`proxmox_all_qemu`, per-node groups, etc.), and each VM's node/vmid are exposed as host facts, which the `proxmox_snapshot` and `cleanup_snapshot` roles use to target the right cluster node via `pvesh`. The 3 Proxmox nodes themselves aren't part of this dynamic inventory (they're hypervisors, not VMs) — they're a small static host list, each with its own `host_vars/pve{1,2,3}.yml`.

### Proxmox node resolution and name resolution limits

Most playbooks that need to run `pvesh`/`qm` commands (`cleanup_snapshot`, `proxmox_snapshot`, `pve_rebalance`) do it via `delegate_to: "{{ proxmox_api_host }}"` over SSH, defaulting to `pve1`. Since a 3-node Proxmox cluster answers the same `pvesh`/`qm` commands identically from any member, `vm-updates.yml`, `container-updates.yml`, `cleanup-snapshots.yml`, `vm-backups.yml` and `provision-vm.yml` each start with `resolve_proxmox_api_host`: a quick SSH:22 reachability probe of `pve1`, falling back to `pve2` then `pve3`, failing clearly only if none respond. The resolved host is saved once on `localhost` and picked up automatically by `proxmox_api_host`'s own definition in `group_vars/all.yml` (a Jinja fallback to that fact, defaulting back to the static `pve1` IP if the role never ran) — no other file needed changing. `pve-updates.yml` doesn't use this role: it already computes its own safe delegate target per-iteration (any node *other than* the one currently being rebooted), a related but distinct concern.

This repo deliberately avoids DNS for servers (see [Provisioning a new fleet VM](#provisioning-a-new-fleet-vm)) — but that guarantee only covers hosts provisioned by `provision-vm.yml`/`provision-semaphore-peer.yml`, which get an explicit `ansible_host` committed to `host_vars/`. Five older hosts predate that convention and have no `ansible_host` at all: `adguard101`, `patchmon101`, `phpipam101`, `lpkat101`, `immich101`. Ansible resolves them via the dynamic inventory's `compose: ansible_host: name` (i.e. by hostname), which only works because their name→IP mapping is hardcoded in `/etc/hosts` **on `semaphore101` specifically** — not tracked in this repo, not replicated to `semaphore102`. In practice this is safe today because `full-updates.yml` (the only playbook that reaches all 5) only ever runs from `semaphore101`; running it manually from `semaphore102` (e.g. as a fallback during a `semaphore101` outage) would fail to resolve them. Worth keeping in mind if that ever needs to happen — either add the same `/etc/hosts` entries to `semaphore102`, or give these 5 hosts explicit `ansible_host` entries like every host provisioned since.

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

`ansible.builtin.import_playbook` (used by `full-updates.yml`) concatenates whole playbooks into one run but doesn't support `when:`, so there's no native "run playbook B only if playbook A succeeded". Instead, `container-updates.yml`, `vm-updates.yml`, `pve-updates.yml` and `update-semaphore-peer.yml` each read and write a shared fact, `orchestration_should_run`, set on `localhost`:

- Each stage's real work is wrapped in `when: hostvars['localhost'].orchestration_should_run | default(true)` — the `default(true)` means each playbook behaves completely normally when run standalone from its own Semaphore template, exactly as before.
- After running, each stage combines the incoming gate with its own outcome (`stage_was_allowed_to_run and not <this stage's problem flag>`), so a stage that was itself skipped can't accidentally re-open the gate for the next one.

**Reports**: run standalone, each playbook still sends its own single email exactly as before (subject/body marked `SKIPPED (previous stage had a problem)` if it was itself skipped). Run via `full-updates.yml`, the 4 individual emails are suppressed (`is_orchestrated_run`, set by a leading play before the 4 `import_playbook`s) and replaced by **one** combined email at the end: a summary table (stage, status, duration for each of the 4), followed by each stage's full report body concatenated in — plus the overall run duration, which `send_report` already adds automatically from the shared `run_start_epoch` fact. Per-stage durations use the same `capture_start_time` role with a second, stage-scoped fact name (`capture_start_time_fact_name`) alongside the existing global one.

`update-semaphore-peer.yml` itself internally imports `container-updates.yml` a second time (scoped to just `peer_host`, to update its containers) — reusing that file's exact fact names (`container_updates_stage_status`/`_report_body`/etc.) a second time would silently overwrite the fleet-wide numbers from the first, top-level import once both run in the same `full-updates.yml` chain. `full-updates.yml` guards against this two ways: it snapshots the fleet-wide values into separately-named facts (`fleet_container_updates_*`) right after the first import, before the nested one can clobber them (a real bug caught and fixed during development, not a hypothetical) — and it pins an explicit `target_hosts` on its own top-level import that still excludes `semaphore102`, so the two calls don't also *process* it twice (container-updates.yml's own default now includes `semaphore102`, for standalone visibility — see below).

### Mutual Semaphore updates

`semaphore101` and `semaphore102` are both excluded from `vm-updates.yml` — neither can safely patch-and-reboot itself while it might be the thing running the automation, and each is already covered end-to-end by its own `update-semaphore-peer.yml` run. `semaphore102` exists purely to break that deadlock, using `update-semaphore-peer.yml` in both directions, via two independently-scheduled Semaphore templates:

1. A template on `semaphore102` runs `update-semaphore-peer.yml -e peer_host=semaphore101` on its own schedule. This patches `semaphore101`'s OS and Podman images (reusing the same roles `vm-updates.yml` uses), then waits for its Semaphore web service (`/api/ping`) to respond before finishing.
2. `full-updates.yml` runs on `semaphore101` on its own separate schedule, timed to start after `semaphore102`'s job is expected to be done, so it always finds `semaphore101` already up to date. It runs the whole fleet as usual, and finishes with `import_playbook: update-semaphore-peer.yml, vars: {peer_host: semaphore102, image_reference_host: semaphore101}` — gated by the same `orchestration_should_run` fact as every other stage, so `semaphore102` only gets patched back if nothing upstream failed.

There's no direct hand-off between the two instances — no API call, no shared flag — just two schedules timed so each one's prerequisite is done by the time it runs.

**Keeping Postgres/Semaphore image versions identical**: `podman_update`'s normal "pull whatever the configured tag currently resolves to" is fine for most hosts, but for `semaphore102` specifically it risks drift — a floating tag (e.g. `postgres:16`) could resolve to a newer patch release there than what `semaphore101` is actually running, if a new image was published upstream between the two runs. So `container-updates.yml` — not `update-semaphore-peer.yml` — owns this: `semaphore101` stays excluded from its host pattern (it's the reference, never itself force-aligned), but `semaphore102` is included and given different treatment in Step 3 of the per-host loop:
1. A leading play (`hosts: semaphore101`, gated on `ansible.builtin.inventory_hostnames` confirming `semaphore102` is actually a target this run — skips opening a connection to `semaphore101` entirely otherwise) checks it's healthy, then reads the exact image digest each of its Podman units (`semaphore`, `semaphore_db`) is *actually running* (`podman inspect` — not the configured tag, the resolved digest) and saves it on `localhost`.
2. When the per-host loop reaches `semaphore102`, Step 3 branches: instead of `podman_update`'s normal tag pull, it forces `semaphore102` to pull and tag `semaphore101`'s exact digest for each matching unit (`podman_align_image` role), restarting only if it actually changed something — so `semaphore102` always ends up bit-for-bit identical to `semaphore101`, regardless of whether the image's tag is fully pinned or floating. `podman_align_image` populates the same `podman_update_summary` fact `podman_update` does, so `semaphore102` shows up as a normal row in `container-updates.yml`'s own report table, in the same run standalone visitors and `full-updates.yml` already see for every other host. If `semaphore101` wasn't healthy enough to read from, `semaphore102` gets an explicit "alignment skipped" row instead of silently vanishing from the table.

`update-semaphore-peer.yml`'s own nested `import_playbook: container-updates.yml` (`target_hosts: "{{ peer_host }}"`) triggers this same logic automatically whenever `peer_host` is `semaphore102` — so the `semaphore102`-facing direction gets both OS-patch (this file) and container alignment (container-updates.yml) in one run, same as before, just with the alignment logic now living in one place instead of duplicated. When `semaphore102` patches `semaphore101` the other way (`peer_host: semaphore101`), the nested import only targets `semaphore101` — `semaphore102`'s alignment branch never triggers, since `semaphore102` isn't in that run's target set at all.

**Semaphore version bumps (auto-applied for patch/minor)**: in the `semaphore102`-facing direction (no `image_reference_host`), `update-semaphore-peer.yml` also checks Docker Hub for the latest Semaphore release, filtering to plain `vMAJOR.MINOR.PATCH` tags (excluding betas/RCs/arch-suffixed variants) within whatever major line is *currently running*, and picks the highest one numerically (not lexicographically, so e.g. `v2.19.0` correctly outranks `v2.9.5`). Patch **and** minor bumps are applied automatically — updating the live `Image=` line and restarting `semaphore.service` — since semver treats minor releases as backward-compatible by convention, unlike majors; it no-ops if there's nothing newer. So the recurring template's CLI args only ever need `peer_host` baked in once, with no other input needed for routine bumps. Moving to a new *major* version (e.g. `v3.0.0`) is still a deliberate, reviewed action: pass `-e semaphore_image_tag=vX.Y.Z` explicitly that one time to override the auto-detection.

This only runs in the direction that patches `semaphore101` (i.e. skipped whenever `image_reference_host` is set) — `semaphore102` never needs its own independent version check, since the alignment step above already forces it to match whatever `semaphore101` is running, bump or no bump.

Whenever a bump actually happens (auto-detected or explicit), a further play reads the version currently recorded in `roles/semaphore_provision/files/semaphore.container` (via `slurp` + a Jinja `regex_search` evaluated on the controller, not a shelled-out `grep` — Semaphore's own container ships BusyBox grep, which lacks `-P`/PCRE support) and compares it against what the peer is *actually* running; if they differ, it updates the `Image=` line, commits and pushes to `origin/main` directly. Comparing against the repo's own recorded value (rather than "did a bump happen in this exact run") makes this self-healing: if a previous run's live bump succeeded but its push failed partway through (a real incident — `/repos/homelab-iac` isn't writable on `semaphore101`, only `semaphore102` has that persistent bind-mount; fixed by using `playbook_dir`, the same per-template ephemeral checkout every other push in this repo already uses), the next run still catches and fixes the drift instead of assuming there's nothing to sync. This is the one place in this repo where automation pushes to GitHub on its own rather than the user doing it; it authenticates with a **`GITHUB_PUSH_TOKEN`** (a GitHub Personal Access Token with write access to this repo), injected the same way as every other secret here. The token briefly appears in the push command's argv while it runs (visible to `ps` on that host during that moment) — accepted as a reasonable tradeoff for a single-tenant homelab host, but worth knowing if that host's threat model ever changes.

**Reporting**: `update-semaphore-peer.yml` tracks its own stage status (`OK`/`SKIPPED`/`PROBLEM`) the same way `container-updates.yml`/`vm-updates.yml`/`pve-updates.yml` do — `SKIPPED` covers both "an earlier stage in an orchestrated run had a problem" and "the peer was patched fine but image alignment was skipped because `image_reference_host` wasn't healthy". When `full-updates.yml` calls it (`peer_host: semaphore102, image_reference_host: semaphore101`), it now runs *before* the combined report is built, so its status becomes a 4th row in that single email instead of being invisible; run standalone (e.g. `semaphore102`'s `update-semaphore-peer.yml -e peer_host=semaphore101` template), it sends its own email exactly like the other 3 playbooks do. Its leading play also force-sets `is_orchestrated_run` before its own nested `container-updates.yml` import, so that import doesn't send a second, separate email of its own — its content is folded into `update-semaphore-peer.yml`'s own final report body instead (only when run standalone; the combined `full-updates.yml` email already covers container-updates fleet-wide, so it's omitted there to avoid a duplicate section).

### Provisioning a new Semaphore peer

`provision-semaphore-peer.yml` (run from `semaphore101`, targeting the brand-new VM) deploys Semaphore identically to the existing instance: it copies the 7 Quadlet unit files (`semaphore-network.network`, 4 `.volume` files, `semaphore-db.container`, `semaphore.container` — byte-identical to `semaphore101`'s, since nothing in them is host-specific), generates **fresh** `SEMAPHORE_DB_PASS`/`POSTGRES_PASSWORD` (kept in sync with each other) and `SEMAPHORE_ADMIN_PASSWORD` secrets rather than reusing `semaphore101`'s real ones, clones this repo into `/home/deploy/repos/homelab-iac` (bind-mounted into the container as `/repos`, so the clone itself is at `/repos/homelab-iac`), then enables and starts both services.

It's safe to re-run: if `/etc/semaphore/app.env` already exists, the secrets and env files are left untouched (regenerating them on an already-initialized Postgres data volume would desync the stored DB password from a freshly-rewritten one).

Not handled by this playbook, and needs a short manual checklist once the containers are up:
- VM creation itself (Proxmox side) — same manual process as any other VM in this cluster.
- Recreating Semaphore's own data (projects/templates/environments/credentials — these live in `semaphore102`'s own Postgres DB, not in anything this repo touches): the SSH key in the Key Store (same key as `semaphore101`'s), the Proxmox/SMTP Variable Group, the repo connection (pointing at `/repos/homelab-iac`), and at minimum the two templates this design needs — "Update semaphore101" (`update-semaphore-peer.yml -e peer_host=semaphore101`) on its own schedule, and (on `semaphore101`) "Full updates" (`full-updates.yml`) timed to run after it.
- Changing `SEMAPHORE_ADMIN_PASSWORD` from its generated value via Semaphore's own UI on first login (same as should be done for `semaphore101`'s `changeme` default while at it).

### Provisioning a new fleet VM

`provision-vm.yml` (`-e new_vm_name=<hostname>`) replaces the old cloud-init-based cloning flow, which regenerated the SSH host key on *every boot* (not just the first) and caused constant `known_hosts` churn. The golden template no longer has cloud-init or an EFI disk at all — everything it used to do at boot now happens once, in this playbook, right after cloning:

0. Fail clearly, before touching anything, if `new_vm_name` is already taken: an existing Proxmox VM of that name (checked via the dynamic inventory's `hostvars`), an existing `host_vars/<new_vm_name>.yml` in this repo, or an existing Katello host under that exact name. Without this, an accidental double-run (or a name colliding with an existing host) would silently produce two same-named Proxmox VMs, breaking the exact-name match `decommission-vm.yml` relies on to find a host in Katello.
1. Resolve a free IP via phpIPAM (`phpipam_next_ip` role) starting from `phpipam_search_start_ip`, with a TCP:22 liveness probe as a safety net — phpIPAM's own data isn't assumed to be current, so a candidate that's actually alive gets self-healed back into phpIPAM and the search retries (`phpipam_max_retries`) before failing loudly.
2. Full-clone the template on `pve_clone_node` (`pve_clone_vm` role: `qm clone --full --storage pve_clone_storage`), start it, and connect to it at its baked-in default IP (`provision_template_ip`, `192.168.1.200` — every fresh clone boots here since the template's static config never changes).
3. Reconfigure the clone (`provision_vm_reconnect` role): switch its network config to the resolved IP via `nmcli`, set the hostname, and regenerate its SSH host key — the key regenerates exactly **once**, at this point, not on every future boot like cloud-init used to do, so each VM still gets its own stable identity without the churn. This is the one genuinely delicate part: the IP change and the host-key change both invalidate the current SSH session, so it's done as a single fire-and-forget async command, followed by clearing just that IP's stale `known_hosts` entry on the control node (`ansible_ssh_common_args`'s `accept-new` only covers hosts never seen before, not a *changed* key at an IP that's been reused), then `meta: reset_connection` + `wait_for_connection` to land back on the same host at its new identity.
4. Register with Katello (`katello_register_host` role) via `hammer host-registration generate-command`, then confirms Katello registered it under the exact hostname (`hammer host info --name`, failing loudly on any mismatch — this is what lets `decommission-vm.yml` find it by `new_vm_name` alone, no guessing later), disable the default vendor repos (`appstream`, `baseos`, `extras`, `epel` — left enabled by the OS install, they conflict with Katello's own content-view repos on tightly version-locked packages like `selinux-policy-targeted`/`-extra`, a real incident on `patchmon101` where `vm-updates.yml` failed with a depsolve error), apply OS updates, install `podman`+`git`.
5. Register the resolved IP as officially used in phpIPAM, then write `host_vars/<new_vm_name>.yml` (`ansible_host: <resolved IP>`) into this repo and commit+push it — servers are **never** resolved via AdGuard (that's DHCP clients only) or any other DNS, so this is how every later fleet-wide playbook finds the new VM, the same pattern already used for `semaphore101`/`semaphore102`.
6. Rebalance the cluster (`pve_rebalance`), since the clone always lands on `pve_clone_node` first.

**Manual prerequisites, before this playbook can be used at all:**
- **Golden template rebuild** (Proxmox side, one-time): no cloud-init drive; keeps UEFI (`bios: ovmf`) with **`efidisk0` permanently attached to the template** — `qm clone --full` inherits it automatically, so there's no more manual "create an EFI disk" step, without needing the riskier switch to `bios: seabios` (which would mean redoing the disk's boot partition/bootloader entirely, since the OS is already installed for UEFI); `deploy` user pre-created with passwordless sudo and `authorized_keys` already containing the Semaphore automation key, Katello/Foreman's key, and your personal key; SSH host keys generated *once* at template-build time (not a stock/placeholder key); static `192.168.1.200/24` via a NetworkManager profile with **no `connection.interface-name` or `802-3-ethernet.mac-address` binding** — a real incident this session: the template's `eth0-static` connection was bound to both, and cloning gives every VM a fresh MAC (and often a differently-numbered predictable interface name, e.g. `eth0` on the template vs `ens18` on a clone), so the profile silently failed to activate and the clone fell back to a DHCP address outside the intended range. Match by device *type* only (`nmcli con modify eth0-static connection.interface-name "" 802-3-ethernet.mac-address ""`) so it applies regardless of naming/MAC on any future clone. `qm template <vmid>` once done, then set `pve_template_vmid` (passed as an extra var, or add it to `group_vars/all.yml` once known).
- **phpIPAM API App**: Administration → Server Management → `phpIPAM settings` → enable the **API** module first (it's off by default) → then Administration → API → create an App (`phpipam_api_app_id`, default `ansible`), permissions `Read/Write`, security **`User token`**. Not `SSL with App code token`/`SSL with User token` — both enforce a real HTTPS connection and fail with `503 SSL connection is required for API` over plain HTTP, and phpipam101 is HTTP-only for now; revisit this if it ever gets a reverse proxy in front of it. `User token` mode logs in with real phpIPAM credentials (`phpipam_api_username`/`phpipam_api_password` — this repo's phpIPAM Admin account) to obtain a short-lived session token, done once at the start of `phpipam_next_ip`. Confirm `phpipam_subnet_cidr` (`192.168.1.0/24`) already exists as a Subnet object in phpIPAM.

### Decommissioning a fleet VM

`decommission-vm.yml` (`-e new_vm_name=<hostname>`) is the reverse of `provision-vm.yml` — no confirmation flag by design (just the name), but every step is set up to fail loudly rather than guess:

1. Resolve the VM's Proxmox VMID/node and IP from `hostvars` (the dynamic inventory + its `host_vars` file) — fails clearly if the name isn't a known host at all. The IP resolution has a fallback: hosts provisioned by `provision-vm.yml` always have an explicit `ansible_host` in `host_vars`, but older hosts that predate that convention (`adguard101`, `patchmon101`, `phpipam101`, `lpkat101`, `immich101` — see [Proxmox node resolution and name resolution limits](#proxmox-node-resolution-and-name-resolution-limits)) don't; for those, the IP is extracted from `health_check_url` instead. Uses bracket notation (`hostvars[new_vm_name]['ansible_host']`) rather than dot notation - a real incident decommissioning `patchmon101`, whose `hostvars` entry has no `ansible_host` attribute at all: dot notation raised a hard error there instead of resolving to undefined.
2. Migrate to `vm_backup_node` if it isn't already there, then take a final `vzdump` safety backup to pCloud (`pve_migrate_vm` + `vzdump_backup`, the exact same roles/pattern `vm-backups.yml` uses) — a last-resort rollback if the decommission turns out to be a mistake.
3. Deregister from Katello (`katello_deregister_host` role): looks up the host by its exact name and fails clearly if not found, then deletes it — `provision-vm.yml`'s own `katello_register_host` role verifies the exact same exact-name match right after registration, so a VM provisioned by this repo is guaranteed findable by `new_vm_name` alone at decommission time.
4. Remove the IP's address entry from phpIPAM (`phpipam_remove_address` role) — a no-op, not an error, if it's already absent.
5. Destroy the VM (`pve_destroy_vm` role: graceful `qm shutdown`, confirm stopped, then `qm destroy --purge`).
6. Remove `host_vars/<new_vm_name>.yml` from this repo (if present) and commit+push the deletion.

### Removing cloud-init from a legacy VM

`remove-cloud-init.yml` (`-e target_hosts=<hostname>`, defaults to `adguard101,patchmon101,phpipam101,lpkat101,immich101,semaphore102,smb101`, `serial: 1`) is a one-time cleanup for VMs that predate `provision-vm.yml`'s cloud-init-free golden template and still regenerate their SSH host key on *every* reboot, not just the first. This has caused real incidents: a `decommission-vm.yml` failure on `patchmon101` and a phpIPAM API 503 investigation on `phpipam101`, both traced back to a stale/changed SSH host key after a routine reboot. `semaphore102` and `smb101` are safe to include in the same default list because this playbook is only ever run from `semaphore101` — the same non-self-reference reasoning `update-semaphore-peer.yml` already relies on; `smb101` needed a `host_vars/smb101.yml` with an explicit `ansible_host` added first, since it previously had none at all.

Per host, in order: take a Proxmox snapshot (`ansible_cloudinit_removal` label, distinct from `ansible_patching`/`ansible_container` so it can't collide with a concurrent patch run — sweepable later with `cleanup-snapshots.yml -e snapshot_label=ansible_cloudinit_removal` if one is ever left behind) → resolve the IP the same way `decommission-vm.yml` does (`ansible_host` if set, else extracted from `health_check_url`) → capture the current SSH host key fingerprint → convert the active NetworkManager connection to a persistent static profile (fire-and-forget + `meta: reset_connection` + `wait_for_connection`, the same disruptive-change pattern `provision_vm_reconnect` uses, even though the IP itself isn't changing — bringing the connection up can still drop the current SSH session) → remove the `cloud-init`/`cloud-utils-growpart` packages → remove the VM's cloud-init drive from its Proxmox config, delegated to the VM's actual node rather than the generically-resolved `proxmox_api_host` (unlike `pvesh`, raw `qm` commands aren't cluster-aware — a real incident converting `adguard101`, which isn't hosted on `pve1`) → a real `qm shutdown`/`qm start` cycle rather than a soft in-guest reboot, since a soft reboot doesn't cycle QEMU and so never actually applies the pending drive removal (another real incident, caught via the Proxmox UI still showing the drive queued for removal after a soft reboot) → assert the SSH host key fingerprint is *identical* to before the reboot (the actual proof, not an inference) and that the NetworkManager connection is still `manual` → run this host's own `health_check` → remove the safety snapshot only once all of that passes.

Each host is wrapped in `block`/`rescue`: a failure records the reason and calls `meta: end_play`, stopping the run before touching the next (unrelated) service, but still reaching the final report play — matching `pve-updates.yml`'s "halt the whole run, don't cascade" posture rather than `vm-updates.yml`'s "skip this one, continue to the next." A snapshot left behind after a failure is the rollback path. Recommended rollout: run against one host at a time (`-e target_hosts=patchmon101` first — nothing else in this repo depends on it; avoid `adguard101`, LAN-wide DNS, and `lpkat101`, which every other playbook's Katello registration depends on, as the first test) before trusting the default of all 5 at once.

## Requirements

- SemaphoreUI (or plain `ansible-playbook`) with the following installed:
  - Collections: `ansible-galaxy collection install -r collections/requirements.yml`
  - Python packages: `pip install -r requirements.txt`
- A Proxmox API token with sufficient privileges to list VMs, create/delete snapshots, and migrate VMs
- SSH access to all target hosts, and to the Proxmox nodes, using a key stored in Semaphore's Key Store (never committed to this repo)
- `rclone` installed and configured with a `pcloud:` remote on `smb101` (for `pcloud-backups.yml`) and on `pve1` (for `vm-backups.yml`)

## Secrets

No credentials are stored in this repository. The Proxmox API token is read via `lookup('env', 'PROXMOX_TOKEN_SECRET')` in `inventory/proxmox.yml`, injected by Semaphore through a Variable Group. SSH keys live exclusively in Semaphore's Key Store. `GITHUB_PUSH_TOKEN` (a GitHub PAT with write access, used only by `update-semaphore-peer.yml` to push its own Semaphore version-bump commit) follows the same env-var-via-Variable-Group pattern.

`SMTP_USER`/`SMTP_PASSWORD` (an app password, not the account's real password, since `smtp_host` is hardcoded to `smtp.gmail.com` in `group_vars/all.yml`) and `NOTIFY_EMAIL_TO` follow the same pattern and are required by every playbook's `send_report` role — without them, every report email in this repo fails to send.

`katello_promote` and `cv-retention.yml` pass `KATELLO_HAMMER_USERNAME`/`KATELLO_HAMMER_PASSWORD` explicitly on every `hammer` command (`no_log: true`, since the password would otherwise appear in the command's argv in Semaphore's task log) rather than relying on lpkat101's local hammer CLI config file — its automatic/interactive credential loading proved unreliable (a real production incident: correct credentials in `/root/.hammer/cli_config.yml`, confirmed byte-for-byte via `-u`/`-p`, still got rejected through the config-driven `InteractiveBasicAuth` path) and explicit `-u`/`-p` never failed once confirmed working. Rotate the Foreman admin password by updating this env var in Semaphore's Variable Group — no need to touch lpkat101 itself. `katello_register_host` (used by `provision-vm.yml`) reuses the same two vars.

`PHPIPAM_API_USERNAME`/`PHPIPAM_API_PASSWORD` (phpIPAM credentials for the `ansible` App's `User token` security mode — see [Provisioning a new fleet VM](#provisioning-a-new-fleet-vm)) follow the same env-var-via-Variable-Group pattern, read via `phpipam_api_username`/`phpipam_api_password` in `group_vars/all.yml`. `phpipam_next_ip` exchanges them for a session token once per run (`no_log: true` throughout, same rationale as every other credential in this repo).

