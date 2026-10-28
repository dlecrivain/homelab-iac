# homelab-iac

Ansible automation for a homelab built on Proxmox VE, Foreman/Katello and Podman, driven by two [SemaphoreUI](https://semaphoreui.com) instances. VMs are discovered from the Proxmox API, so there are no static host lists to maintain for them.

## Overview

The repository covers the maintenance cycle of a three-node Proxmox cluster and the VMs it runs: Rocky Linux 10 managed through Katello, with services deployed as rootless or root Podman containers described by [Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html) files.

1. **Content views.** The Katello content views (`CV_Rocky_10` for the VMs, `CV_Proxmox` for the hypervisors) are promoted and published by their own playbook on its own schedule, so re-running a patch stage never publishes a new version. Their history is capped at `cv_retention_count` versions.
2. **OS patching.** Each VM is checked for updates, snapshotted if there are any, patched, rebooted and health-checked. A VM with nothing to install but a reboot pending (`dnf needs-restarting -r`: a kernel or core library installed but not running, as on a VM provisioned after its packages were updated) goes through the same steps, so it does not stay on the old version until the next release.
3. **Container updates.** Quadlet files are re-rendered from the pinned versions in `group_vars/all.yml`, then each container is snapshotted, updated and health-checked, and old images are pruned.
4. **Snapshots.** A safety snapshot is removed as soon as the post-update health check passes. One left behind by a failed check is swept up by a separately scheduled run.
5. **Proxmox hosts.** Each node is evacuated by live migration, patched and rebooted in turn, then the cluster is rebalanced.
6. **Cluster network.** The direct 10G links between the three nodes and the corosync configuration are declared in this repository.
7. **Backups.** Every VM gets a weekly `vzdump` uploaded to pCloud, and the photo library is copied there with `rclone`.
8. **Semaphore itself.** The two instances patch each other, so neither has to update itself.
9. **Version tracking.** A daily check reports new image and binary versions and bumps the pinned ones.

## Playbooks

### Patching and updates

| Playbook | Purpose |
|---|---|
| `katello-publish.yml` | Promotes the current latest version of `CV_Rocky_10` and `CV_Proxmox` to Production, publishes and promotes a new one to Test, then purges versions beyond `cv_retention_count`. Runs on its own schedule, early enough that the patch playbooks see the content it published. |
| `vm-updates.yml` | Checks, patches and reboots every VM, except the two Semaphore hosts, which cannot safely reboot themselves mid-run. |
| `container-updates.yml` | Updates the Podman/Quadlet containers of every host that defines `podman_units`. `semaphore104` is included but aligned to `semaphore103`'s exact image digests instead of pulling on its own, see [Mutual Semaphore updates](#mutual-semaphore-updates). |
| `cleanup-snapshots.yml` | Removes leftover safety snapshots matching `snapshot_label` (default `ansible_patching`; pass `-e snapshot_label=ansible_container` for the container-update ones). |
| `pve-updates.yml` | Evacuates, patches and reboots each Proxmox node in turn, halts the whole run on the first failure, and rebalances the cluster once every node is done. |
| `full-updates.yml` | Runs `container-updates.yml`, `vm-updates.yml`, `pve-updates.yml` and finally patches `semaphore104`. Each stage runs only if the previous one had no problem, otherwise later stages report `SKIPPED`. Sends one combined report. |
| `update-semaphore-peer.yml` | OS-patches and container-updates one Semaphore instance, bumps its Semaphore image to the latest patch or minor release and waits for its web service to answer. `peer_host` is a required extra var (`semaphore103` or `semaphore104`), and the playbook is always run from the other instance. |
| `check-image-updates.yml` | Daily check of Docker Hub for the images in `image_update_watch_list`, of GitHub releases for `node_exporter`, `smartctl_exporter` and Immich, and a scan of the remaining containers. Same-major bumps and Immich patch releases are written into `group_vars/all.yml` and pushed. The mail lists what is pending, what needs a decision and what is a new major. See [Container image versions](#container-image-versions). |

### Cluster operations

| Playbook | Purpose |
|---|---|
| `pve-rebalance.yml` | Rebalances VMs across the nodes by live migration and applies the plan. `-e rebalance_dry_run=true` only prints it. |
| `pve-rebalance-test.yml` | The same playbook with the dry-run flag set, so it shows the plan without moving anything. |
| `migrate-vm.yml` | Live-migrates one VM (`-e migrate_vm_name=<host> -e migrate_to=<node>`) through the same role the other playbooks use, and reports which network carried the migration. |
| `configure-pve-mesh.yml` | Applies the declared 10G mesh to the nodes, see [Network and cluster](#network-and-cluster). |
| `configure-pve-corosync.yml` | Renders and installs the cluster's corosync configuration, see [Network and cluster](#network-and-cluster). |

### Backups

| Playbook | Purpose |
|---|---|
| `vm-backups.yml` | `vzdump` (zstd) of every VM, three at a time: each VM is migrated to `vm_backup_node` if it is not already there, backed up to `Stockage_SSD`, uploaded to pCloud, then migrated straight back. A final rebalance only runs if a migrate-back failed and left something stranded. |
| `pcloud-backups.yml` | `rclone` backups to pCloud. With `pcloud_backup_source` passed as an extra var it runs that one job, otherwise each host (`immich102` and `smb101`) runs the jobs of its own `pcloud_backup_jobs` in parallel, straight from the files on its own disk. See [Backups](#backups-1). |

### Provisioning

| Playbook | Purpose |
|---|---|
| `provision-vm.yml` | Clones the golden template, resolves a static IP through phpIPAM, reconfigures the clone, registers it with Katello, updates it, installs Podman and git, commits its `host_vars` file and rebalances. See [Provisioning a new fleet VM](#provisioning-a-new-fleet-vm). |
| `decommission-vm.yml` | The reverse: final safety backup, Katello and phpIPAM cleanup, destruction of the VM, removal of its `host_vars` file. See [Decommissioning a fleet VM](#decommissioning-a-fleet-vm). |
| `provision-semaphore-peer.yml` | Deploys Semaphore through Podman Quadlet onto a fresh peer VM, identically to the existing instance. |

### Service configuration

| Playbook | Purpose |
|---|---|
| `configure-bunkerweb.yml` | Deploys `bunkerweb102`'s global settings and one reverse-proxy file per site, from `bunkerweb_services` in its `host_vars`. See [Configuring BunkerWeb](#configuring-bunkerweb). |
| `configure-adguard.yml` | Deploys the AdGuard Home Quadlet on `adguard_host` (`adguard102`), with the sysctl, lingering and firewall settings a rootless container needs on ports 53 and 80. |
| `configure-phpipam.yml` | Deploys the phpIPAM stack (MariaDB, web, cron) on `phpipam_host` (`phpipam102`). |
| `configure-immich.yml` | Deploys Immich's four Quadlet units on `immich_host` (`immich102`) from `immich_version`, as the connecting user like `adguard_configure`. |
| `configure-paperless.yml` | Deploys Paperless-ngx's three Quadlet units (server, Postgres, Valkey) on `paperless_host` (`paperless101`) from `paperless_version`, as the connecting user like `configure-immich.yml`. See [Paperless](#paperless). |
| `sync-paperless.yml` | Keeps Paperless-ngx in step with the cloud storage folder that stays the master copy, run from `immich102`: new files are uploaded, moved or renamed files are recognised by checksum and re-tagged, files that disappeared are tagged `a-purger`, and documents nothing could give a type are tagged `a-classer` and listed in an email. See [Paperless](#paperless). |
| `configure-smb.yml` | Installs Samba on `smb_host` (`smb101`) and shares one folder, on its own volume, for the Aqara doorbell: SMB 3 only, one account, reachable from the addresses of `smb_allowed_hosts` only, and announced by NetBIOS and mDNS. See [SMB share for the Aqara doorbell](#smb-share-for-the-aqara-doorbell). |
| `configure-homeassistant.yml` | Deploys Home Assistant and the Matter Server as Quadlet units on `homeassistant_host` (`homeassistant101`) from `homeassistant_version` and `matter_server_version`. See [Home Assistant](#home-assistant). |
| `configure-prometheus.yml` | Deploys Prometheus, Alertmanager and the PVE exporter on `prometheus102`. |
| `configure-grafana.yml` | Deploys Grafana on `grafana_host` with its datasource and dashboards. |
| `configure-monitoring.yml` | Installs `node_exporter` and `smartctl_exporter` and registers every VM and node with Prometheus. Safe to re-run, and the way a new exporter version reaches the hosts. See [Monitoring](#monitoring). |
| `configure-claude-access.yml` | Creates on every VM the read-only account (`claude_readonly_user`) that the assistant of `claude101` reads it with through `fleet-ro`: a login shell that only forwards a fixed list of verbs to one root-owned script. Run after `configure-claude.yml`, which creates the key. See [claude101](#claude101). |
| `configure-claude.yml` | Installs Claude Code on `claude_host` (`claude101`) and sets up its service user, managed permission rules, Semaphore access and repository clone. See [claude101](#claude101). |

### Compliance

| Playbook | Purpose |
|---|---|
| `openscap-scan.yml` | Scans every fleet VM except `lpkat101` against the CIS Level 2 Server profile with OpenSCAP and mails one HTML report: score, change since the previous scan, passed and failed rules per host, and the failed rules the most hosts share. Meant for a monthly schedule. See [Compliance scanning](#compliance-scanning). |

### Katello and utilities

| Playbook | Purpose |
|---|---|
| `cv-retention.yml` | Purges content view versions beyond `cv_retention_count` on its own. `katello-publish.yml` does the same right after publishing. |
| `katello-repo-cleanup.yml` | Disables every enabled repository that Katello does not manage, fleet-wide. Both provisioning playbooks already do this for new hosts. |
| `katello-repo-sync.yml` | Synchronizes one Katello repository immediately (`-e katello_sync_repo` and `katello_sync_product`, default `EPEL 10` in `Rocky Linux 10`). |
| `test-email.yml` | Sends one test email to check the SMTP credentials. |

## Repository structure

- **Playbooks**: at the top level, listed above.
- `ansible.cfg`: silences interpreter discovery warnings.
- `requirements.txt` and `collections/requirements.yml`: Python packages for the inventory plugin, and the Ansible collections (`community.proxmox`, `community.general`, `ansible.posix`).
- `inventory/proxmox.yml`: dynamic Proxmox inventory for the VMs (API token read from the environment).
- `group_vars/all.yml`: shared variables (Proxmox API host, Katello organization, SMTP, retention and placement tuning, pinned image and binary versions, the mesh and corosync topology).
- `host_vars/`: per-VM variables (health checks, `podman_units`, backup and placement flags) and per-node connection and network details (`pve1`, `pve2`, `pve3`).
- `scripts/semaphore-ctl`: small client for the Semaphore API, see [Operating Semaphore from a terminal](#operating-semaphore-from-a-terminal).
- `scripts/katello-host-delete`: deletes the Katello record of a host whose VM no longer exists, for the leftover of a provisioning run that failed after the registration. It refuses while a Proxmox VM of that name exists.
- `kickstart/` and `scripts/render-kickstart`, `scripts/check-kickstart`: the golden template's installer and its checks, see [Building the golden template](#building-the-golden-template).
- `tasks/`: task files shared by several playbooks: `commit_and_push.yml` (every playbook that commits back to this repository) and `check_unit_for_update.yml` (the read-only scan in `check-image-updates.yml`).
- `roles/`, by area:
  - **Patching**: `check_updates`, `apply_updates`, `reboot_and_wait`, `health_check`, `host_status`, `proxmox_snapshot`, `cleanup_snapshot`.
  - **Containers**: `podman_update` (pull and restart a Quadlet unit), `podman_align_image` (force an exact image digest), `image_retention` (keep the two most recent images per repository), and one `*_configure` role per service (`bunkerweb`, `adguard`, `phpipam`, `immich`, `paperless`, `homeassistant`, `prometheus`, `prometheus_pve_exporter`, `grafana`), plus `paperless_sync` (the script that keeps Paperless in step with the cloud storage).
  - **Proxmox**: `pve_placement` (where an evacuated VM goes), `pve_evacuate_host`, `pve_migrate_vm`, `pve_rebalance`, `pve_mesh`, `pve_corosync`, `pve_clone_vm`, `pve_destroy_vm`, `resolve_proxmox_api_host`.
  - **Backups**: `vzdump_backup`, `pcloud_backup_job`.
  - **Katello**: `katello_promote`, `katello_cv_retention`, `katello_register_host`, `katello_deregister_host`, `katello_repo_cleanup`.
  - **Provisioning**: `provision_vm_reconnect`, `semaphore_provision`, `phpipam_login`, `phpipam_next_ip`, `phpipam_remove_address`, `lvm_grow` (grows one or several logical volumes of the template's volume group, adding a virtual disk when needed).
  - **File shares**: `smb_configure` (Samba on `smb101`, for the Aqara doorbell).
  - **Assistant**: `claude_assistant`, `claude_readonly_access` (the read-only account on the VMs).
  - **Monitoring**: `node_exporter_install`, `smartctl_exporter_install`, `prometheus_target`.
  - **Compliance**: `openscap_scan`.
  - **Reporting**: `send_report` (shared HTML mail skeleton), `capture_start_time`, `capture_host_list`.

## How it works

### Dynamic inventory and node resolution

`inventory/proxmox.yml` uses the `community.proxmox.proxmox` plugin with an API token that is never stored in this repository (see [Secrets](#secrets)). VMs are grouped automatically (`proxmox_all_qemu`, per-node groups) and expose their node and vmid as host facts, which the snapshot and migration roles use to target the right node. The three Proxmox nodes are not VMs, so they are a small static list, each with its own `host_vars/pve{1,2,3}.yml`.

Every VM also has an explicit `ansible_host` in its `host_vars` file, so no playbook depends on name resolution: servers are never resolved through DNS.

Playbooks that run `pvesh` or `qm` delegate to `proxmox_api_host`. A three-node cluster answers these commands identically from any member, so those playbooks start with `resolve_proxmox_api_host`, which probes SSH on `pve1`, then `pve2`, then `pve3` and keeps the first that answers, failing clearly only if none does. `pve-updates.yml` picks its own delegate per node instead: any node other than the one being rebooted.

### Per-host configuration

Each host that needs container updates or health checks declares them in `host_vars/<hostname>.yml`:

```yaml
health_check_url: "http://192.168.1.x:PORT"
health_check_podman_user: deploy   # or root, or another user whose containers run rootless
health_check_validate_certs: false # optional, only for an https URL with a self-signed certificate
health_check_retries: 30           # optional, for a slow service (default: 5 retries, 10s apart)
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

This keeps the playbooks generic: no host-specific logic lives in the roles. Placement and backup behaviour is set the same way, see [Placement and rebalancing](#placement-and-rebalancing).

### Safety snapshots

`vm-updates.yml` and `container-updates.yml` take a Proxmox snapshot before changing anything (`ansible_patching` and `ansible_container`), on ZFS storage where it costs almost nothing until the data changes. The snapshot is removed as soon as the post-update health check passes. If the check fails it stays in place for a manual rollback decision, and `cleanup-snapshots.yml` does not touch it until it has been dealt with.

Per-host work in both playbooks is wrapped in a `block`/`rescue`: an unhandled failure on one host (a registry timeout, a network error) is recorded as `host_overall_status: PROBLEM` with its reason in the report, and the run continues with the next host. This also keeps one failure from killing the whole `full-updates.yml` chain.

`pve-updates.yml` is stricter, since it patches the hypervisors: if a node fails, a `rescue` marks it `BLOCKED` and halts the entire run so no further node is touched, and the final rebalance is skipped.

### Orchestrated run

`import_playbook`, used by `full-updates.yml`, does not support `when:`, so there is no native way to run playbook B only if A succeeded. Instead `container-updates.yml`, `vm-updates.yml`, `pve-updates.yml` and `update-semaphore-peer.yml` read and write a shared fact on `localhost`, `orchestration_should_run`:

- Each stage's work is wrapped in `when: hostvars['localhost'].orchestration_should_run | default(true)`. The default means each playbook behaves normally when run alone from its own Semaphore template.
- After running, each stage combines the incoming gate with its own outcome, so a stage that was itself skipped cannot re-open the gate for the next one.

**Reports.** Run alone, each playbook sends its own email, marked `SKIPPED (previous stage had a problem)` if it was skipped. Run through `full-updates.yml`, the individual emails are suppressed (`is_orchestrated_run`) and replaced by one combined email: a summary table (stage, status, duration) followed by each stage's report. Per-stage durations use `capture_start_time` with a stage-scoped fact name alongside the global one.

`update-semaphore-peer.yml` imports `container-updates.yml` a second time, scoped to `peer_host`. Reusing that file's fact names would overwrite the fleet-wide values from the first import, so `full-updates.yml` copies them into `fleet_container_updates_*` right after the first import. It also pins an explicit `target_hosts` on its own import that excludes `semaphore104`, so that host is not processed twice.

### Mutual Semaphore updates

`semaphore103` and `semaphore104` are both excluded from `vm-updates.yml`: neither can safely patch and reboot itself while it may be running the automation. Each one patches the other through `update-semaphore-peer.yml`, from two independently scheduled templates:

1. A template on `semaphore104` runs `update-semaphore-peer.yml -e peer_host=semaphore103` weekly. It patches `semaphore103`'s OS and Podman images, then waits for its Semaphore web service (`/api/ping`) to answer.
2. `full-updates.yml` runs on `semaphore103` on its own schedule, timed to start after the first job is done, so it finds `semaphore103` already up to date. It runs the whole fleet and finishes with `import_playbook: update-semaphore-peer.yml` and `peer_host: semaphore104`, `image_reference_host: semaphore103`, gated by the same `orchestration_should_run` fact, so `semaphore104` is patched last and only if nothing upstream failed.

`peer_host` has no default and must name one of the two instances: the playbook's first task refuses to run otherwise, so a template created without arguments cannot patch the host that is running it. There is no direct hand-off between the instances, only two schedules timed so each prerequisite is done when it is needed.

**Identical image versions.** `semaphore104` must run exactly what `semaphore103` runs, which a normal tag pull does not guarantee. `container-updates.yml` therefore treats it differently in Step 3: a leading play reads the digest each of `semaphore103`'s units (`semaphore`, `semaphore_db`) is actually running and saves it on `localhost`, and for `semaphore104` the `podman_align_image` role pulls and tags that exact digest instead of resolving a tag, restarting only if something changed. It fills the same `podman_update_summary` fact as `podman_update`, so `semaphore104` appears as a normal row in the report, or as an explicit "alignment skipped" row if `semaphore103` was not healthy enough to read from. The scan of `check-image-updates.yml` leaves both Semaphore hosts out so it cannot move `semaphore104`'s tags off the aligned digests.

**Semaphore version bumps.** When run for `peer_host: semaphore103`, `update-semaphore-peer.yml` checks Docker Hub for the latest Semaphore release, keeps plain `vMAJOR.MINOR.PATCH` tags within the major line currently running, picks the highest one numerically, and applies it by updating the live `Image=` line and restarting `semaphore.service`. Patch and minor bumps are automatic, since semver treats minor releases as backward-compatible. A new major is a deliberate action: pass `-e semaphore_image_tag=vX.Y.Z` once. This only runs in the `semaphore103`-facing direction; `semaphore104` follows through the digest alignment.

Whenever a bump happens, a further play compares the version recorded in `roles/semaphore_provision/files/semaphore.container` (read with `slurp` and a Jinja `regex_search` on the controller, because Semaphore's container ships a BusyBox `grep` without PCRE) with what the peer actually runs, and if they differ it updates the `Image=` line, commits and pushes to `origin/main`. Comparing against the repository's recorded value rather than "did a bump happen in this run" makes it self-healing after an interrupted run. It uses `playbook_dir`, the per-template checkout every other automated push here uses, and authenticates with `GITHUB_PUSH_TOKEN`, which appears briefly in the push command's arguments on that host, an accepted tradeoff for a single-tenant homelab.

**Reporting.** `update-semaphore-peer.yml` tracks a stage status (`OK`, `SKIPPED`, `PROBLEM`) like the other playbooks. Through `full-updates.yml` it becomes the fourth row of the combined email. Run alone it sends its own email, and its leading play sets `is_orchestrated_run` so the nested `container-updates.yml` import does not send a second one.

### Container image versions

Every container image is pinned to an explicit version in `group_vars/all.yml` (the `*_version` variables), or by digest for Immich's database and cache. No unit runs a floating tag. Quadlet pulls the image named in the unit file when it starts, so a pinned version is what makes a restart predictable.

`container-updates.yml` first re-renders each host's Quadlet files from those variables (Step 2.5, one `*_configure` role per host), so a version bump committed to git reaches the host before the pull. The units restart only if a file changed.

`check-image-updates.yml` runs daily. For the images in `image_update_watch_list` it writes the highest same-major version found on Docker Hub into `group_vars/all.yml` and pushes it, and a new major is only reported. Immich is stricter: its patch releases are applied the same way, but minor and major releases are reported and left to a reviewed edit of `immich_version`, because its database migrations do not roll back. The scan of the remaining containers pulls their pinned tag.

### Commits made by playbooks

Five playbooks commit back to this repository: `check-image-updates.yml` (version bumps), `update-semaphore-peer.yml` (the Semaphore version recorded for provisioning), `provision-vm.yml` and `decommission-vm.yml` (a VM's `host_vars` file). They all include `tasks/commit_and_push.yml`, which sets the identity (`git_commit_name`, `git_commit_email`), commits, and pushes to `git_repository` through `git_push_url`, which carries `GITHUB_PUSH_TOKEN`. The commit message is passed as an argument, not through a shell string. A failed push is reported with the token redacted from git's output.

### Network and cluster

The three Proxmox nodes are cabled to each other directly with 10G DAC cables, without a switch: one `/31` per pair, plus a `/32` loopback per node. `pve_mesh_links` in `group_vars/all.yml` maps each node pair to its subnet, and each node's ports (PCI path, peer, address) and loopback are in its `host_vars/pve*.yml`.

`configure-pve-mesh.yml` first asserts that the declared topology is consistent: addresses are unique, every cabled pair appears in `pve_mesh_links`, and each link is claimed by exactly two nodes. The `pve_mesh` role then names the ports persistently (systemd `.link` files matched on PCI path, so the files are identical on every node), writes their addressing to `/etc/network/interfaces.d/mesh.conf`, configures FRR's OpenFabric so each node reaches the others' loopbacks over either path, and enables forwarding. If one cable fails, traffic flows through the third node. The interface definitions are written but never applied automatically, so a run is safe on a live cluster and reports what is left to apply.

Live migration uses the direct link of the node pair, falling back to the management network for a pair that has none. `migrate-vm.yml` reports which network carried a migration.

Corosync runs two rings: ring 0 on the management network, ring 1 over the mesh through each node's loopback, preferred while it is up (`link_mode: passive`, higher `knet_link_priority`). `configure-pve-corosync.yml` renders `/etc/pve/corosync.conf` from `pve_corosync_*` variables and writes it once from one node, and pmxcfs replicates it. The role refuses to run if the rendered node count differs from the running cluster's, if the content changed without `pve_corosync_config_version` being raised (corosync would ignore it), or if the cluster is not quorate, and it keeps a copy of the replaced file in `/var/backups/corosync`. Restoring that copy does not roll back by itself: its version is lower than the running one, so a rollback also raises the version.

### Placement and rebalancing

Evacuating a node and rebalancing the cluster follow the same rules. Memory is checked against `pve_safety_threshold`. Disk is the sum of every disk in the VM's own configuration, since the cluster API reports only the boot disk, and is checked against the free space of `pve_clone_storage`, which is local to each node: a live migration copies the disks. When no node stays under the memory threshold an evacuation still places the VM, since a planned window needs somewhere to put it, and the placement is reported as a warning, in the run log and in the `pve-updates.yml` email. The exception is a placement that would take a node past 100% of its memory, counting the VM's whole allocation: the evacuation then stops before it moves anything and says which VM does not fit, and the run is blocked.

The rebalance builds one plan that is complete: a VM is moved only if that narrows the gap in memory usage between its node and the target, judged after the moves already in the plan, so applying a plan leaves nothing for a second one and two VMs of the same size are never swapped.

- **`pve_anti_affinity_groups`** lists VMs that must not share a node (the two Semaphore instances). Both the evacuation and the rebalance honour it. Proxmox's own HA affinity rules would require HA-managed VMs on shared or replicated storage, which this cluster does not use.
- **`pve_rebalance_pinned: true`** in a VM's `host_vars` stops the optional rebalance from moving it. It is set on `immich102` and `lpkat101`, whose disks are large enough that every move copies hundreds of gigabytes over the mesh and writes them to a target NVMe. Such a VM is still evacuated when its node is patched (stopped instead, if it has `pve_shutdown_for_maintenance`), and counts as load on the node where it sits.
- **`pve_shutdown_for_maintenance: true`** shuts a VM down for the host's reboot instead of migrating it, for a VM whose disks cannot move or that is too large for the memory of the other nodes. It is set on `lpkat101` and `paperless101`, both on `pve1`. The VM is stopped after the host has installed its updates, since `lpkat101` is the repository the nodes install from, and `pve_shutdown_timeout` gives a VM like Katello more than a minute to stop. It starts again with the host (`onboot`), the run then waits for its health check, and it is also started if the run is blocked after it was stopped.

### Backups

**VM backups.** `vm-backups.yml` migrates each VM to `vm_backup_node` (`pve1`, the only node with the `Stockage_SSD` storage) if it is not there, runs `vzdump` in snapshot mode with zstd, keeps `vm_backup_local_retention` archives locally, uploads the archive to `vm_backup_pcloud_base_path/<vm>` and keeps `vm_backup_pcloud_retention` copies there, then migrates the VM back. The new archive is uploaded before the previous copy is removed when it fits in pCloud's free space with `vm_backup_pcloud_free_margin_gib` to spare, so a failed upload never leaves the VM without an off-site backup. When the quota is too tight, the previous copy is removed first to make room.

**pCloud copies.** `pcloud-backups.yml` has two modes, selected by whether `pcloud_backup_source` is passed as an extra var:

- **Single job**: `pcloud_backup_source`, `pcloud_backup_dest`, `pcloud_backup_mode` (`sync` or `copy`, default `sync`) and `pcloud_backup_extra_args` are passed as extra vars, and one `rclone` command runs through the `pcloud_backup_job` role. Each backup is its own Semaphore template on the same playbook, with its own schedule and extra vars.
- **All jobs**: with no extra vars, the jobs in each host's `pcloud_backup_jobs` (its `host_vars`) run in parallel, each launched with `async` and `poll: 0` and then awaited with `async_status`, so one failure does not stop the others. This mode launches `rclone` directly, because `async` applies to a single module task and not to `include_role`.

A host can also list `pcloud_backup_mounts` (an NFS `src` and a mount `path`): each one is mounted read-only before the jobs run, so `rclone` reads it as an ordinary folder. A share that cannot be mounted stops the play, and a host that is down at boot does not hold up the one that mounts it (`nofail`).

### Provisioning a new Semaphore peer

`provision-semaphore-peer.yml`, run from `semaphore103` against the new VM, copies the seven Quadlet unit files (`semaphore-network.network`, four `.volume` files, `semaphore-db.container`, `semaphore.container`, identical on both instances since nothing in them is host-specific), generates fresh `SEMAPHORE_DB_PASS`, `POSTGRES_PASSWORD` (kept in sync) and `SEMAPHORE_ADMIN_PASSWORD` secrets, clones this repository into `/home/deploy/repos/homelab-iac` (bind-mounted into the container as `/repos`), then enables and starts both services.

It is safe to re-run: if `/etc/semaphore/app.env` already exists the secrets and env files are left alone, since regenerating them on an initialized Postgres volume would desync the stored password.

Not handled by the playbook, and needing a manual checklist once the containers are up:

- Creating the VM on the Proxmox side.
- Recreating Semaphore's own data, which lives in each instance's Postgres and not in this repository: the SSH key in the Key Store, the Variable Group, the repository connection (pointing at `/repos/homelab-iac`), and at minimum the two templates this design needs, "Update semaphore103" (`update-semaphore-peer.yml -e peer_host=semaphore103`) on `semaphore104` and "Full updates" on `semaphore103`, timed to run after it.
- Changing `SEMAPHORE_ADMIN_PASSWORD` from its generated value in Semaphore's UI on first login.

### Provisioning a new fleet VM

`provision-vm.yml` (`-e new_vm_name=<hostname>`) works from a golden template that has no cloud-init and no regenerating SSH host key, so each VM has one stable identity. Everything cloud-init used to do at boot happens once, in this playbook, right after cloning:

0. Fail before touching anything if `new_vm_name` is already taken: an existing Proxmox VM of that name (checked through the dynamic inventory's `hostvars`), an existing `host_vars/<new_vm_name>.yml`, or an existing Katello host of that exact name. This keeps two same-named VMs from existing, which `decommission-vm.yml` could not tell apart.
1. Resolve a free IP through phpIPAM (`phpipam_next_ip`), starting from `phpipam_search_start_ip`, with a TCP:22 liveness probe as a safety net: phpIPAM's data is not assumed to be current, so a candidate that is actually alive is written back into phpIPAM and the search retries (`phpipam_max_retries`) before failing.
2. Full-clone the template on `pve_clone_node` (`qm clone --full --storage pve_clone_storage`), start it and connect at its built-in IP (`provision_template_ip`, `192.168.1.200`), where every fresh clone boots.
3. Reconfigure the clone (`provision_vm_reconnect`): switch the network to the resolved IP with `nmcli`, set the hostname and regenerate the SSH host key. The IP change and the key change both invalidate the SSH session, so it is one fire-and-forget async command, followed by clearing that IP's stale `known_hosts` entry on the controller (`accept-new` only covers hosts never seen before, not a changed key at a reused IP), then `meta: reset_connection` and `wait_for_connection`.
4. Register with Katello (`katello_register_host`, which first adds the Katello server's name to `/etc/hosts` since the lab domain has no DNS server, then uses `hammer host-registration generate-command`) and confirm Katello registered it under the exact hostname, failing on any mismatch: that is what lets `decommission-vm.yml` find it by name alone. Then disable every enabled repository that Katello does not manage (`katello_repo_cleanup`), worked out by diffing the repository IDs in `/etc/yum.repos.d/redhat.repo` against `dnf repolist all` rather than from a fixed list, since the vendor repositories enabled by default vary. Left enabled, they can conflict with Katello's content-view repositories on tightly version-locked packages such as `selinux-policy-targeted`. It fails instead of disabling everything if `redhat.repo` is empty. Then apply OS updates and install `podman` and `git`.
5. Register the IP as used in phpIPAM, then write `host_vars/<new_vm_name>.yml` (`ansible_host: <resolved IP>`) into this repository, commit and push it. Servers are never resolved through AdGuard (DHCP clients only) or any other DNS, so this is how later playbooks find the VM.
6. Rebalance the cluster, since the clone always lands on `pve_clone_node` first.

**Manual prerequisites:**

- **Golden template** (Proxmox side): no cloud-init drive; UEFI (`bios: ovmf`) with `efidisk0` permanently attached, which `qm clone --full` inherits; a `deploy` user with passwordless sudo and `authorized_keys` containing the Semaphore automation key, Katello/Foreman's key and your own; SSH host keys generated once at template build time; static `192.168.1.200/24` through a NetworkManager profile with no `connection.interface-name` or `802-3-ethernet.mac-address` binding, because every clone gets a fresh MAC and sometimes a different interface name, and a bound profile would not activate. Match by device type only (`nmcli con modify eth0-static connection.interface-name "" 802-3-ethernet.mac-address ""`). Then `qm template <vmid>` and set `pve_template_vmid`. [Building the golden template](#building-the-golden-template) produces exactly this, hardened.
- **phpIPAM API app**: Administration, Server Management, phpIPAM settings: enable the **API** module (off by default); then Administration, API: create an app (`phpipam_api_app_id`, default `ansible`) with `Read/Write` permissions and security **`User token`**. The two SSL modes require a real HTTPS connection and fail with `503 SSL connection is required for API` over plain HTTP, which is what `phpipam101` serves. `User token` mode logs in with real phpIPAM credentials (`phpipam_api_username`, `phpipam_api_password`) to obtain a short-lived session token, once per run. `phpipam_subnet_cidr` (`192.168.1.0/24`) must already exist as a subnet in phpIPAM.

### Decommissioning a fleet VM

`decommission-vm.yml` (`-e new_vm_name=<hostname>`, optionally `-e decom_skip_backup=true`) is the reverse of `provision-vm.yml`. It takes no confirmation flag, only the name, and every step fails loudly rather than guessing:

1. Resolve the VM's vmid, node and IP from `hostvars`, failing if the name is not a known host. The IP is `ansible_host`, falling back to the address in `health_check_url`. It reads `hostvars[new_vm_name]['ansible_host']` with bracket notation, which resolves to undefined where dot notation would raise for a host without that attribute.
2. Migrate to `vm_backup_node` if needed, then take a final `vzdump` to pCloud (`pve_migrate_vm` and `vzdump_backup`, the same roles `vm-backups.yml` uses), as a last-resort rollback. Skipped entirely, migration included, when `decom_skip_backup` is set.
3. Deregister from Katello (`katello_deregister_host`), looking the host up by exact name and failing if it is not found.
4. Remove its IP from phpIPAM (`phpipam_remove_address`), a no-op if already absent.
5. Destroy the VM (`pve_destroy_vm`: graceful `qm shutdown`, confirm stopped, then `qm destroy --purge`).
6. Remove `host_vars/<new_vm_name>.yml` and commit and push the deletion.

### Configuring BunkerWeb

`bunkerweb102` (WAF and reverse proxy, `bunkerweb-all-in-one` image) is configured entirely by `configure-bunkerweb.yml` (the `bunkerweb_configure` role) from `bunkerweb_services` in `host_vars/bunkerweb102.yml`: a list of proxied sites (name, domain, and any setting beyond the `USE_REVERSE_PROXY=yes` baseline). Each site's `{{ domain }}_*` variables go into their own file under `/etc/bunkerweb/conf.d/<name>.env`. Global settings (embedded services, `MULTISITE`, Let's Encrypt, DNS resolvers, LAN whitelist) go into `/etc/bunkerweb/global.env`, and `SERVER_NAME` is generated from the same list. Adding or removing a site is an edit of that list and a re-run: a removed entry's `conf.d` file is found with `ansible.builtin.find`, compared with the list and deleted, so a decommissioned VM leaves no stale reverse-proxy entry. Each service needs a matching `EnvironmentFile=` line in the Quadlet, which the template generates from the same list.

Secrets (OVH DNS-01 credentials, admin password, see [Secrets](#secrets)) are templated into a separate `/etc/bunkerweb/secrets.env` (`mode: '0600'`), never committed.

### Monitoring

Two dedicated VMs run it: `prometheus102` (Prometheus, Alertmanager and `prometheus-pve-exporter`) and `grafana102` (Grafana), provisioned by `provision-vm.yml` like every other fleet VM. Every fleet VM and all three nodes run `node_exporter` (CPU, memory, mountpoints, uptime; a native binary with a systemd unit, not a container, and not installed through `dnf` or `apt`, since non-Katello repositories are disabled). The nodes also run `smartctl_exporter` for SMART and disk health. `prometheus-pve-exporter` runs once on `prometheus102` and covers the whole cluster in one scrape, since Proxmox's API is cluster-aware whichever node it connects through.

**Which services matter reuses `podman_units`.** `node_exporter_install` builds a `--collector.systemd.unit-include` regex from each host's own `podman_units`, using the same `{{ service_name | default(name) }}.service` expression `podman_update` uses, so `node_exporter` reports only the containers this repository tracks for that host.

**Targets are per-host `file_sd` files** (`/etc/prometheus/file_sd/{node_exporter,smartctl_exporter}_targets.d/<name>.json` on `prometheus102`), added and removed by `prometheus_target` and live-reloaded by Prometheus. `provision-vm.yml` registers a new VM automatically and `decommission-vm.yml` removes it, both gated on `'prometheus102' in hostvars` so they do nothing during `prometheus102`'s own bootstrap. `configure-monitoring.yml` registers every existing VM and node, and installs the exporter versions pinned in `group_vars/all.yml`.

**Alerts.** Alertmanager reuses the SMTP credentials of `send_report`, templated into `/etc/alertmanager/alertmanager.yml` with `no_log: true`, since it only reads static YAML. `roles/prometheus_configure/files/alert_rules.yml` defines alerts for a node down, mountpoints and the root filesystem filling up, a tracked container or service failing, SMART status and critical warnings, NVMe wear, spare capacity, media errors and temperature, `nvme_data` filling up, the cluster losing quorum, the PVE exporter being down, a mesh link down or MTU wrong, and a node cut off from the mesh. Grafana's dashboard provider reloads the directory without a restart.

**Retention and disk wear.** Prometheus keeps a year of samples (`--storage.tsdb.retention.time=1y`), so the history of SMART wear is not trimmed after a month. The TSDB grows by 65 to 85 MB a day on the current fleet, roughly 37 GB for the year, so `host_vars/prometheus102.yml` sets `prometheus_var_size_gb: 60` and `configure-prometheus.yml` grows `/var` to that size through the `lvm_grow` role (it adds a second virtual disk to the volume group when the template's disk has no room left, and does nothing once the volume is big enough); the `DiskAlmostFull` alert fires at 90 %. `roles/prometheus_configure/templates/disk_wear.rules.yml.j2` turns the SMART data into one wear figure and an estimated end-of-life date per disk, and the *Disk wear* dashboard shows them in one table, each end-of-life estimate as "date (Ny Nd)" so the column filters and sorts as text in chronological order. The NVMe endurance ratings it needs are `disk_rated_endurance_tbw` in `group_vars/all.yml`: a new NVMe model needs its rating added there, or the dashboard falls back to the drive's own `percentage_used`.

**Bootstrap order.** Add `PROXMOX_TOKEN_SECRET` and `GRAFANA_ADMIN_PASSWORD` to Semaphore's Variable Group, then run `provision-vm.yml -e new_vm_name=prometheus102`, `configure-prometheus.yml`, `provision-vm.yml -e new_vm_name=grafana102` (it registers itself with Prometheus, which now exists in the inventory), `configure-grafana.yml`, and `configure-monitoring.yml` once to register the rest of the fleet.

**Implementation notes.**

- Cockpit listens on port 9090 on the Rocky 10 golden template, the port Prometheus uses, so `prometheus_configure` disables `cockpit.socket` on `prometheus102`.
- `prom/alertmanager` and `prompve/prometheus-pve-exporter` run as a non-root user (GID 65534 and GID 101), so their volume-mounted secret files are group-owned by that GID with mode `0640` or `0750` instead of `root:root 0600`. `grafana.env` needs no such handling: Grafana reads it through `EnvironmentFile=`, consumed by Podman as root before the container starts.
- Each Quadlet container has its own network namespace: `PublishPort=` exposes a port to the LAN but not to sibling containers over `127.0.0.1`. A shared `monitoring-network.network` Quadlet, the pattern `semaphore_provision` uses, gives Prometheus, Alertmanager and the exporter DNS resolution by container name.
- Dashboards are vendored in `roles/grafana_configure/files/` so a redeploy cannot discard a hand edit or pull in an unreviewed upstream change. `grafana_configure` gives the provisioned datasource a fixed `uid: prometheus`, which every dashboard refers to.
- The Proxmox dashboard's storage legend shows `{{node}} - {{storage}}`, so `nvme_data` is distinguishable across the three nodes.

**Known limitations.** The smartctl dashboard aggregates across nodes when "All" is selected in the instance picker, because showing each node separately would mean vendoring the dashboard and rewriting about 24 panel queries, and losing automatic upstream updates. The Proxmox dashboard has no per-node instance picker, since one exporter covers the cluster and its `instance` label is always `pve-exporter:9221`; per-node dimensions come from the `name`, `node` and `id` labels. Proxmox's own `pve-firewall`, distinct from the `firewalld` through which `node_exporter_install` opens `9100/tcp`, is not configured here: if it is enabled on the nodes, ports `9100` and `9633` need opening there too.

### Immich

`immich102` runs Immich's four containers (server, machine learning, Valkey and Postgres) rootless as the connecting user `deploy`, like `adguard102`, from `immich_version`. The `immich_configure` role enables lingering, opens 2283/tcp, creates `/var/lib/immich/photos` and `/var/lib/immich/postgres` and hands an empty Postgres directory to the container's own user. `lvm_grow_targets` in `host_vars/immich102.yml` (`/var` at 500 GB for the library, `/home` at 24 GB for the rootless images, which the template's 2 GB cannot hold) is applied by `configure-immich.yml` through `lvm_grow`.

The photo and video library is also exported read-only over NFSv4 to the addresses in `immich_library_readers` (`library_export.yml`, included by the role): `smb101`, which copies it to the cloud storage. Only 2049/tcp is open, and only to those addresses, and `root_squash` stays on since every file is readable by everyone, so a reader has no more access than an unprivileged user.

The database password is in two files the repository does not keep, `/var/lib/immich/.env` and `/var/lib/immich/postgres.env`, owned by the connecting user with mode 0600. The role stops with a clear message when one is missing, so a new host has to receive them before its first run. Data that comes from another host is copied with `rsync --numeric-ids` and `--usermap`/`--groupmap` from the previous owners to the new user: files owned by the old user become `deploy`, and the Postgres files go to the user's sub-uid base plus 998, which is uid 999 of the container's postgres user (`/etc/subuid` gives the base).

### Paperless

`paperless101` runs the document archive designed in [docs/paperless.md](docs/paperless.md): Paperless-ngx, Postgres and Valkey as rootless Quadlet units of the connecting user `deploy`, from `paperless_version`. The `paperless_configure` role enables lingering, opens 8000/tcp, creates `/var/lib/paperless` and hands an empty Postgres directory to the container's own user. `lvm_grow_targets` in `host_vars/paperless101.yml` makes room for the images in `/home`. BunkerWeb publishes it as `paperless_domain`.

The database password and the application secret key are generated by the role on the host the first time, with `openssl rand`, and written to `/var/lib/paperless/postgres.env` and `/var/lib/paperless/.env` (mode 0600). They are never displayed and the repository does not hold them. The first start installs the OCR language pack and runs the database migrations, which takes a few minutes. The administrator account is created by hand, once, with `podman exec -it paperless_server python3 manage.py createsuperuser`.

The cloud storage folder stays the only place where documents are filed and renamed; Paperless is an index kept in step with it, which can be rebuilt from the folder. `sync-paperless.yml` runs `paperless-sync` on `immich102`, the one host that holds the cloud storage remote, so that credential is never copied elsewhere. It lists the folder, downloads the files whose identifier, size or modification time changed since the last run, computes their SHA-256 and compares it with the checksum Paperless stores for every original. A state file under `/var/lib/paperless-sync` remembers which document each file became.

- A **new file** is uploaded with a tag per folder (the leading number is dropped), the title and, when the name carries one, the date (`YYYY-MM[-DD]_...`, `YYYY_MM`, `MM_YYYY`, `Epson_DDMMYYYY...`), and the correspondent and type that the rules give it.
- A **moved or renamed file** is recognised by its checksum, not uploaded again: the document gets the tags of the new path, and the correspondent and type that the rules give the new name, so renaming a file is how a type is changed.
- A file **deleted or replaced** leaves its document in place with the tag `a-purger`, because the API user cannot delete. Nothing is ever deleted from Paperless, nor written to the folder.
- A new document that nothing gave a type, neither the rules nor Paperless's own matching, gets the tag `a-classer` and the playbook emails the list, with the number of documents that carry the tag and a link to them. A renamed file that now has a type loses the tag.
- Files Paperless cannot import (Office documents, archives) are listed in the run's output as skipped; export them to PDF to have them imported.

The rules are a JSON file next to the documents (`paperless_sync_rules`), not in this repository, since they name the owner's correspondents; `roles/paperless_sync/files/rules.example.json` shows the format. Each rule has selectors (`file`, a regular expression on the lower-case, accent-free name; `tags`, `tags_any`; `unless`) and the first rule of a list that satisfies them wins. A correspondent rule can carry `match`, the regular expression given to Paperless when it creates the correspondent. The API token is of a Paperless user without administrator or delete rights, placed by hand in `paperless_sync_token_file`; `paperless_sync_dry_run` reports what would be done and changes nothing. A run uploads at most 100 new files, so a folder copied by mistake cannot flood the archive, and it fails, and so does the email, when an upload never shows up in Paperless two days later. The Semaphore template runs the playbook every night.

### SMB share for the Aqara doorbell

The chime of the Aqara Video Doorbell G4 (the indoor unit, "Doorbell Repeater", which also holds its storage) can back its event clips up to an SMB share from the Aqara Home app. `smb101` is a small VM, created with `provision-vm.yml`, that provides that share and copies the files to the cloud storage itself, so that a connected device that is not trusted writes to a host that holds nothing else. The Freebox's own share was not used: it has a single login for the whole disk and no per-folder rights.

`configure-smb.yml` runs the `smb_configure` role:

- **Volume**: the files live on `smb_data_lv`, mounted on `smb_share_path` with `nodev,nosuid,noexec` and labelled `samba_share_t`, so they cannot fill the system's volumes. It is created at 1 GiB and grown by `lvm_grow` to `smb_data_size_gb`, which adds a virtual disk to the VM when the volume group is too small.
- **Account**: one Samba account, `smb_writer_user`, with no shell, which is the one typed into the device. Its password is placed by hand in `/root/.config/smb/<account>-password` (mode 0600) before the first run, which stops with the command to use when it is missing. It is not in this repository.
- **Access**: SMB 3 at the least (`smb_min_protocol`), no guest access, and `hosts allow` plus one firewalld rule per address of `smb_allowed_hosts` for the `samba` and `mdns` services, so that only the devices of the doorbell can reach or find the share. `nmbd` (NetBIOS) and Avahi (mDNS) announce it, since devices find a server through one or the other. The router has to give those devices a fixed address, or the list stops matching.
- **Copy to the cloud storage**: `pcloud-backups.yml` copies the files from this host, with its own `rclone` and its own pCloud token (`rclone authorize "pcloud"` on a machine with a browser, put in `/root/.config/rclone/rclone.conf` by hand, mode 0600), on the same Semaphore schedule as the other copies to that storage. The job is in `pcloud_backup_jobs` in `host_vars/smb101.yml`: files younger than 10 minutes wait for the next run, since the device may still be writing them, and nothing is ever deleted from the cloud storage. A token of its own can be revoked without touching the other hosts, which is worth it since this host faces a device that is not trusted.
- **Retention**: the Aqara Home app deletes the clips from the share after the delay chosen in it (90 days). The copy to the cloud storage keeps them.


### Home Assistant

`homeassistant101` runs Home Assistant Container and the Matter Server (the matter.js server that the Home Assistant Matter Server add-on is built on) as two rootful Quadlet units with host networking, deployed by `homeassistant_configure`. Home Assistant OS, which it replaces, cannot be managed by this repository: its host has no `dnf`, Podman, Katello agent or SSH, so a VM named in `proxmox_all_qemu` would have had to be excluded from most playbooks. The VM is built from the hardened template like the others and the usual policies apply to it:

- **Images**: `homeassistant_version` and `matter_server_version` are pinned in `group_vars/all.yml`. `check-image-updates.yml` watches Home Assistant on Docker Hub and the Matter Server on ghcr.io (`ghcr_repo` in `image_update_watch_list`), bumps within a major line (for Home Assistant the year, so a new year is only reported), and `container-updates.yml` applies the bump after a VM snapshot like for every other container.
- **OS and backups**: `vm-updates.yml` patches it and `vm-backups.yml` backs the whole VM up to pCloud. Home Assistant Container has no backup feature of its own, so the VM backup is the backup.
- **Network**: host networking is needed for device discovery and the HomeKit bridge. Firewalld trusts the local network as a whole, because discovery replies and device events come from addresses the stateful firewall has never seen it contact, and nothing outside it. The Matter Server listens on `127.0.0.1` only.
- **CIS**: Thread and Matter devices are reached through routes that border routers announce in router advertisements, so `roles/homeassistant_configure` sets `accept_ra` to 2, on the interface that carries the default route as well because an interface keeps the values it was created with, and the two matching rules are waived for this host in `host_vars/homeassistant101.yml`.
- **Reverse proxy**: BunkerWeb publishes it as `homeassistant.lecrivaindaniel.fr` (`bunkerweb_services` in `host_vars/bunkerweb102.yml`). Home Assistant answers HTTP 400 to every proxied request until `use_x_forwarded_for` and `trusted_proxies` (BunkerWeb's address) are set. Since 2026.10 it keeps its HTTP settings in `.storage/http` and ignores `http:` in `configuration.yaml`, so these two settings belong to the instance's own state rather than to this repository and travel with the VM backup.

### claude101

`claude101` runs Claude Code as a service that the owner drives from a phone through Remote Control. A request is the authorization: it reads, edits this repository, commits, pushes and starts playbooks without asking again for each step, then reports what it did. [docs/claude101.md](docs/claude101.md) gives the design and the reasons for its limits. `configure-claude.yml` applies the `claude_assistant` role to `claude_host`:

- **Installation**: Claude Code comes from Anthropic's signed dnf repository, whose key is imported only when its fingerprint matches the published one. The repository stays disabled, like every repository outside Katello, and is enabled for the one install of `claude_code_version`, so the host moves only when that variable changes.
- **Privileges**: the service runs as an unprivileged user that has no sudo and no ssh key of its own to another host (it reads the VMs only through `fleet-ro`, below). Its permission rules and its `auto` permission mode are in `/etc/claude-code/managed-settings.json`, owned by root, where deny rules apply in every mode and bypass mode is disabled.
- **Playbooks**: the assistant types `semaphore-ctl`, which runs the repository's script as another user, `claude-semaphore`, through one sudoers rule. That user alone reads the Semaphore token (`/etc/claude-assistant/semaphore.token`), and the template ids it may start are `claude_assistant_semaphore_allowed_templates` in the host's `host_vars` (ids are per Semaphore instance, `claude_assistant_semaphore_host`; `'*'` allows every template).
- **Reading the VMs**: `fleet-ro <vm> <verb>` shows the state of a VM without any privilege on it. `configure-claude.yml` creates a user, `claude-fleet`, that holds an SSH key the assistant cannot read, and the command reaches only the hosts of `/etc/claude-fleet/hosts` (the VMs of the inventory, `claude101` excepted), always as `claude_readonly_user`, through one sudoers rule like `semaphore-ctl`. `configure-claude-access.yml` then creates that account on every VM: a locked password, the key authorized from `claude101` only with `restrict`, and a login shell that hands the words of the command to `claude-ro-tools` through one sudoers rule, never to a shell. The script has a fixed list of verbs (`overview`, `updates`, `units`, `unit`, `logs`, `journal`, `containers`, `container-logs`, `network`, `firewall`) whose arguments are checked against a pattern and whose output is capped. Where the hardened template lets only `deploy` in over SSH (`AllowUsers` in `00-fleet.conf`), a file that sorts before it adds the account. The Proxmox nodes have no `sudo` and are left out. Logs can hold a secret; the instruction file tells the assistant to copy none.
- **Monitoring**: `curl` and `wget` are denied, so `monitoring-ctl` is the assistant's only way to Prometheus and Grafana. It sends GET requests to the two fixed addresses: `alerts`, `targets` (the ones that are down), `query '<PromQL>'` and `grafana-health`.
- **Repository**: the clone reads over HTTPS and pushes with a deploy key generated on the host. A `pre-push` hook syntax-checks every playbook. Branch protection on GitHub, which has to reject force pushes and deletions of `main`, is the limit that does not depend on the host.

Three steps involve a secret and are done by hand once. The end of each run lists the ones still open:

1. Add the public key that the run prints to the repository as a deploy key with write access.
2. Create a Semaphore user with the Task Runner role, and place its API token in `/etc/claude-assistant/semaphore.token` (owner `claude-semaphore`, mode 0600).
3. Log in with `sudo -iu claude`, `claude auth login`, then start `claude remote-control` once in the clone to accept its two confirmation prompts, and run the playbook again so that the service starts.

### Compliance scanning

`openscap-scan.yml` runs the `openscap_scan` role on each VM in turn. The role installs `openscap-scanner` and `scap-security-guide` from the Katello content view, scans the host against `openscap_profile` (`cis_fleet`: `cis`, the CIS Red Hat Enterprise Linux 10 Benchmark for Level 2 - Server, minus the rules of `kickstart/cis-waivers.yml`) in the Rocky Linux 10 datastream `openscap_datastream`, and keeps the XCCDF results and the full HTML report in `openscap_report_dir` for `openscap_report_retention_days`. `roles/openscap_scan/files/summarize_xccdf.py` reduces the results to a score, the counts per verdict and the failed rules with their titles, which come from the datastream because the results file only carries rule identifiers.

The report email uses the same skeleton as every other report. A host is flagged as a warning when its score is below `openscap_score_target` or has dropped by more than `openscap_regression_points` since the previous scan, which `last-summary.json` in the report directory remembers. A warning does not fail the run: only a host that could not be scanned does, since an unhardened fleet starts well below any target. The role writes the XCCDF tailoring file (`openscap_tailoring_file`) on each host at every scan, from `kickstart/cis-waivers.yml` and the host's own `openscap_waived_rules` (a rule and its reason), so the monthly score keeps measuring what each host can follow and hosts that were not built from the template are measured the same way. `lpkat101` runs Rocky Linux 9, whose datastream is a different file, so it is left out of the default targets.

### Building the golden template

A partition layout cannot be converted in place, so the template is installed again from the Rocky Linux 10 ISO with `kickstart/rocky10-cis.ks.j2`. The layout is plain LVM (`vg_system`, no thin pool) with a separate logical volume for each mount point the CIS Level 2 - Server profile asks for (`/home`, `/tmp`, `/var`, `/var/tmp`, `/var/log`, `/var/log/audit`, with the `nodev`, `nosuid` and `noexec` options the profile lists) and `/dev/shm` configured in `/etc/fstab`. The disk is 64 GB, which costs nothing on the sparse `nvme_data` storage until it is written, and about 15 % of it stays free in the volume group, so growing a volume later is one `lvextend -r`, and a bigger disk only needs a `pvresize` first.

The kickstart installs the packages the profile wants and removes those it forbids, sets the services, boot arguments (`audit=1`, `audit_backlog_limit`) and boot loader password, creates `deploy` with passwordless sudo and its keys, allows ssh for `deploy` only, and writes the static `eth0-static` profile that `provision-vm.yml` expects. The installer uses that same static address, `provision_template_ip`, so no DHCP server is involved: it must be free, and it is not registered in phpIPAM because every fresh clone of the template boots at it in turn. The first boot relabels the whole file system (`/.autorelabel`), because the installer labels files with the policy of its own release, and an ISO older than the mirror's packages leaves `sshd-session` with a generic label that SELinux then refuses to start sessions from. It also installs a one-shot unit, `cis-remediate.service`: on the next boot it asks `oscap` for the remediation script of the tailored profile, applies it, scans the host (`/var/lib/cis/report.html`) and reboots once. The SCAP Security Guide content gives the install-time rules as kickstart fragments and the other rules as scripts, so no rule is written by hand.

`kickstart/cis-waivers.yml` lists the rules the fleet cannot follow, each with its reason: the `overlay` module Podman needs, the IPv4 forwarding netavark enables, the passwordless sudo Ansible relies on, the journal upload without a log server, the file ownership and world-writable rules that container storage breaks, Red Hat's package signing key that Rocky does not use, the rsyslog rule, since journald is the only logger, and the maximum password age of `deploy`, whose password must never expire or its ssh key logins would stop. `scripts/render-kickstart` turns them into the XCCDF tailoring file that the template is remediated with, so the scan measures the same profile.

1. Copy `kickstart/local.example.yml` to `kickstart/local.yml` (git ignores it) and fill in the root and boot loader password hashes and the three public keys.
2. `scripts/render-kickstart` writes `kickstart/build/rocky10-cis.ks` and `kickstart/build/ks.iso`, a CD-ROM image labelled `OEMDRV` that holds the kickstart as `ks.cfg`. Anaconda reads a `ks.cfg` from a volume with that label by itself, so the installer needs no boot argument.
3. Copy `ks.iso` to an ISO storage next to the Rocky Linux 10 ISO (`Stockage_SSD:iso/` on `pve1`), and create the build VM like the current template (`machine: q35`, `bios: ovmf` with an `efidisk0`, `cpu: x86-64-v3`, 2 cores, 4096 MB while building, a **64 GB** `scsi0` on `nvme_data` with `discard=on,iothread=1`, `scsihw: virtio-scsi-single`, `net0` on `vmbr0`, `agent: 1`, `boot: order=scsi0;ide2`) with the Rocky Linux 10 ISO as `ide2` and `ks.iso` as `ide3`. Start it: the boot menu counts down by itself, and the installation then runs unattended. The repositories in the kickstart are the Rocky 10 mirrors, so the VM needs internet access through the gateway.
4. The installer reboots by itself. The first boot relabels the file system and reboots, the second applies the remediation and reboots once more, a few minutes in all. `ssh deploy@192.168.1.200`, read `/var/log/cis-remediate.log` and `/var/lib/cis/report.html`, and check `/var/lib/cis/done` exists.
5. Run `sudo seal-template` (it resets the machine ID and removes the build's history and caches, then powers the VM off), remove the ISO, set the memory back to 2048 MB, `qm template <vmid>` and set `pve_template_vmid`.

`scripts/check-kickstart --datastream ssg-rl10-ds.xml --oscap <oscap> --repoquery-host <user@host>` verifies the rendered kickstart without installing anything: its syntax (`ksvalidator -v RHEL10`), that every rule of the profile is handled by exactly one mechanism (waived, an install-time fragment, a mount option, an accounted-for rule without an automatic fix, or the first-boot script that `oscap` actually generates), the invariants the fleet's playbooks depend on (passwordless sudo for `deploy`, the overlay module, an unbound network profile) and that every package it names exists in the repositories of that host. The datastream and `oscap` can be unpacked from the `scap-security-guide` and `openscap` RPMs of the Rocky mirror without installing them. It only proves what can be proved without booting the result: the first real install, followed by `openscap-scan.yml`, is the final test.

### Operating Semaphore from a terminal

`scripts/semaphore-ctl` is a small client for the Semaphore API: it lists templates and recent tasks, starts a task (`run <template> --var name=value` fills in a survey variable such as `new_vm_name`), waits for it, and reads its log, or just the lines around its errors (`log <task> --errors`). It is meant for an operator or an assistant working from a terminal on an account that has the Task Runner role, which can start and read tasks but not edit templates. `copy-template <source> <name> <playbook>` and `add-schedule <template> '<cron>' <name>` create a template as a copy of an existing one and a schedule for it, which needs the Manager role while it runs; neither edits or deletes anything. `set-schedule <schedule> '<cron>'` is the one command that edits: it replaces the cron expression of an existing schedule and changes nothing else.

The API token is read from a file that must not be readable by anyone else (`~/.config/semaphore/semaphore.token`) and is passed to `curl` through a configuration descriptor, so it never appears on a command line. Starting a task is limited to the template ids listed in `~/.config/semaphore/allowed-templates`, one per line, where a line with just `*` allows every template; with no such file nothing can be started, while reading stays available.

## Requirements

- SemaphoreUI, or plain `ansible-playbook`, with:
  - Collections: `ansible-galaxy collection install -r collections/requirements.yml`
  - Python packages: `pip install -r requirements.txt`
- A Proxmox API token with enough privileges to list VMs, create and delete snapshots, and migrate VMs.
- SSH access to all target hosts and to the Proxmox nodes, using a key stored in Semaphore's Key Store (never committed to this repository).
- `rclone` with a `pcloud:` remote on `pve1` (for `vm-backups.yml`), on `immich102` (for `pcloud-backups.yml`, which installs the package) and on `smb101` (for the copy of the doorbell's files, installed by `configure-smb.yml`). The remote holds a pCloud token, so its `rclone.conf` is copied by hand to `/root/.config/rclone/` (mode 0600) on `immich102` and never stored here.
- For `sync-paperless.yml`, on `immich102`: the API token of a Paperless user without administrator or delete rights, in `/root/.config/paperless/token` (mode 0600). It is placed by hand and never stored here.

## Secrets

No credentials are stored in this repository. The Proxmox API token is read with `lookup('env', 'PROXMOX_TOKEN_SECRET')` in `inventory/proxmox.yml` and injected by Semaphore through a Variable Group. SSH keys live only in Semaphore's Key Store. `GITHUB_PUSH_TOKEN` (a GitHub personal access token with write access to this repository) is used by the playbooks that commit back to it, `check-image-updates.yml`, `update-semaphore-peer.yml`, `provision-vm.yml` and `decommission-vm.yml`, and is injected the same way.

`SMTP_USER`, `SMTP_PASSWORD` and `NOTIFY_EMAIL_TO` follow the same pattern and are required by every playbook's `send_report` role. `SMTP_PASSWORD` is an app password, not the account's real password, since `smtp_host` is `smtp.gmail.com`. Each Semaphore instance holds its own Variable Group, so a rotated password has to be updated on both.

`katello_promote` and `cv-retention.yml` pass `KATELLO_HAMMER_USERNAME` and `KATELLO_HAMMER_PASSWORD` explicitly on every `hammer` command (`no_log: true`, since the password would otherwise appear in the command's arguments in Semaphore's task log) rather than relying on the credentials in `lpkat101`'s hammer configuration file. Rotating the Foreman admin password is an update of that variable, with nothing to change on `lpkat101`. `katello_register_host` reuses the same two variables.

`PHPIPAM_API_USERNAME` and `PHPIPAM_API_PASSWORD` (the credentials for the `ansible` app's `User token` mode, see [Provisioning a new fleet VM](#provisioning-a-new-fleet-vm)) are read through `phpipam_api_username` and `phpipam_api_password`. `phpipam_next_ip` exchanges them for a session token once per run. `PHPIPAM_DB_PASSWORD` and `PHPIPAM_DB_ROOT_PASSWORD` must be set before the first `configure-phpipam.yml` run, or the role would reset the database passwords to empty.

`BUNKERWEB_OVH_APPLICATION_KEY`, `BUNKERWEB_OVH_APPLICATION_SECRET`, `BUNKERWEB_OVH_CONSUMER_KEY` (OVH API credentials for the Let's Encrypt DNS-01 challenge) and `BUNKERWEB_ADMIN_PASSWORD` are read through `bunkerweb_ovh_*` and `bunkerweb_admin_password`. Unlike the secrets above, `bunkerweb_configure` writes them to a file on the target (`/etc/bunkerweb/secrets.env`, `mode: '0600'`, `no_log: true` on the templating task), because the BunkerWeb Quadlet reads its configuration only through `EnvironmentFile=`. If they are missing, nothing fails loudly: Let's Encrypt renewal and the admin login break at the next attempt.

`PROXMOX_TOKEN_SECRET` is also exposed as `proxmox_token_secret` in `group_vars/all.yml`, so `prometheus_pve_exporter_configure` reuses the same Proxmox API token, with no second token to create or rotate. `GRAFANA_ADMIN_PASSWORD` is read as `grafana_admin_password`. `prometheus_pve_exporter_configure` (writing `/etc/pve-exporter/pve.yml`) and `grafana_configure` (writing `/etc/grafana/grafana.env`) write their secret to a `mode: '0600'` file with `no_log: true`, for the same reason as `bunkerweb_configure`: the container reads its configuration only from a mounted file.
