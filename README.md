# homelab-iac

Ansible automation for a homelab built on Proxmox VE, Foreman/Katello and Podman, driven by two [SemaphoreUI](https://semaphoreui.com) instances. VMs are discovered from the Proxmox API, so there are no static host lists to maintain for them.

## Overview

The repository covers the maintenance cycle of a three-node Proxmox cluster and the VMs it runs: Rocky Linux 10 managed through Katello, with services deployed as rootless or root Podman containers described by [Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html) files.

1. **Content views.** The Katello content views (`CV_Rocky_10` for the VMs, `CV_Proxmox` for the hypervisors) are promoted and published by their own playbook on its own schedule, so re-running a patch stage never publishes a new version. Their history is capped at `cv_retention_count` versions.
2. **OS patching.** Each VM is checked for updates, snapshotted if there are any, patched, rebooted and health-checked.
3. **Container updates.** Quadlet files are re-rendered from the pinned versions in `group_vars/all.yml`, then each container is snapshotted, updated and health-checked, and old images are pruned.
4. **Snapshots.** A safety snapshot is removed as soon as the post-update health check passes. One left behind by a failed check is swept up by a separately scheduled run.
5. **Proxmox hosts.** Each node is evacuated by live migration, patched and rebooted in turn, then the cluster is rebalanced.
6. **Cluster network.** The direct 10G links between the three nodes and the corosync configuration are declared in this repository.
7. **Backups.** Every VM gets a weekly `vzdump` uploaded to pCloud, and the photo library and Home Assistant data are copied there with `rclone`.
8. **Semaphore itself.** The two instances patch each other, so neither has to update itself.
9. **Version tracking.** A daily check reports new image and binary versions and bumps the pinned ones.

## Playbooks

### Patching and updates

| Playbook | Purpose |
|---|---|
| `katello-publish.yml` | Promotes the current latest version of `CV_Rocky_10` and `CV_Proxmox` to Production, publishes and promotes a new one to Test, then purges versions beyond `cv_retention_count`. Runs on its own schedule, early enough that the patch playbooks see the content it published. |
| `vm-updates.yml` | Checks, patches and reboots every VM, except the two Semaphore hosts, which cannot safely reboot themselves mid-run. |
| `container-updates.yml` | Updates the Podman/Quadlet containers of every host that defines `podman_units`. `semaphore102` is included but aligned to `semaphore101`'s exact image digests instead of pulling on its own, see [Mutual Semaphore updates](#mutual-semaphore-updates). |
| `cleanup-snapshots.yml` | Removes leftover safety snapshots matching `snapshot_label` (default `ansible_patching`; pass `-e snapshot_label=ansible_container` for the container-update ones). |
| `pve-updates.yml` | Evacuates, patches and reboots each Proxmox node in turn, halts the whole run on the first failure, and rebalances the cluster once every node is done. |
| `full-updates.yml` | Runs `container-updates.yml`, `vm-updates.yml`, `pve-updates.yml` and finally patches `semaphore102`. Each stage runs only if the previous one had no problem, otherwise later stages report `SKIPPED`. Sends one combined report. |
| `update-semaphore-peer.yml` | OS-patches and container-updates one Semaphore instance, bumps its Semaphore image to the latest patch or minor release and waits for its web service to answer. `peer_host` is a required extra var (`semaphore101` or `semaphore102`), and the playbook is always run from the other instance. |
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
| `pcloud-backups.yml` | `rclone` backups to pCloud. With `pcloud_backup_source` passed as an extra var it runs that one job, otherwise it runs the three built-in jobs (Home Assistant, Immich Daniel, Immich Marine) in parallel. See [Backups](#backups-1). |

### Provisioning

| Playbook | Purpose |
|---|---|
| `provision-vm.yml` | Clones the golden template, resolves a static IP through phpIPAM, reconfigures the clone, registers it with Katello, updates it, installs Podman and git, commits its `host_vars` file and rebalances. See [Provisioning a new fleet VM](#provisioning-a-new-fleet-vm). |
| `decommission-vm.yml` | The reverse: final safety backup, Katello and phpIPAM cleanup, destruction of the VM, removal of its `host_vars` file. See [Decommissioning a fleet VM](#decommissioning-a-fleet-vm). |
| `remove-cloud-init.yml` | Removes cloud-init from the VMs that predate `provision-vm.yml`, converting their network to a persistent static profile. See [Removing cloud-init from a legacy VM](#removing-cloud-init-from-a-legacy-vm). |
| `provision-semaphore-peer.yml` | Deploys Semaphore through Podman Quadlet onto a fresh peer VM, identically to the existing instance. |

### Service configuration

| Playbook | Purpose |
|---|---|
| `configure-bunkerweb.yml` | Deploys `bunkerweb101`'s global settings and one reverse-proxy file per site, from `bunkerweb_services` in its `host_vars`. See [Configuring BunkerWeb](#configuring-bunkerweb). |
| `configure-adguard.yml` | Deploys the AdGuard Home Quadlet on `adguard101`. |
| `configure-phpipam.yml` | Deploys the phpIPAM stack (MariaDB, web, cron) on `phpipam101`. |
| `configure-immich.yml` | Deploys Immich's four Quadlet units on `immich101` from `immich_version`. |
| `configure-prometheus.yml` | Deploys Prometheus, Alertmanager and the PVE exporter on `prometheus101`. |
| `configure-grafana.yml` | Deploys Grafana on `grafana101` with its datasource and dashboards. |
| `configure-monitoring.yml` | Installs `node_exporter` and `smartctl_exporter` and registers every VM and node with Prometheus. Safe to re-run, and the way a new exporter version reaches the hosts. See [Monitoring](#monitoring). |

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
- `tasks/`: task files shared by several playbooks: `commit_and_push.yml` (every playbook that commits back to this repository) and `check_unit_for_update.yml` (the read-only scan in `check-image-updates.yml`).
- `roles/`, by area:
  - **Patching**: `check_updates`, `apply_updates`, `reboot_and_wait`, `health_check`, `host_status`, `proxmox_snapshot`, `cleanup_snapshot`.
  - **Containers**: `podman_update` (pull and restart a Quadlet unit), `podman_align_image` (force an exact image digest), `image_retention` (keep the two most recent images per repository), and one `*_configure` role per service (`bunkerweb`, `adguard`, `phpipam`, `immich`, `prometheus`, `prometheus_pve_exporter`, `grafana`).
  - **Proxmox**: `pve_placement` (where an evacuated VM goes), `pve_evacuate_host`, `pve_migrate_vm`, `pve_rebalance`, `pve_mesh`, `pve_corosync`, `pve_clone_vm`, `pve_destroy_vm`, `resolve_proxmox_api_host`.
  - **Backups**: `vzdump_backup`, `pcloud_backup_job`.
  - **Katello**: `katello_promote`, `katello_cv_retention`, `katello_register_host`, `katello_deregister_host`, `katello_repo_cleanup`.
  - **Provisioning**: `provision_vm_reconnect`, `remove_cloud_init`, `semaphore_provision`, `phpipam_login`, `phpipam_next_ip`, `phpipam_remove_address`.
  - **Monitoring**: `node_exporter_install`, `smartctl_exporter_install`, `prometheus_target`.
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
health_check_podman_user: deploy   # or root, or a specific user like "immich"
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

`update-semaphore-peer.yml` imports `container-updates.yml` a second time, scoped to `peer_host`. Reusing that file's fact names would overwrite the fleet-wide values from the first import, so `full-updates.yml` copies them into `fleet_container_updates_*` right after the first import. It also pins an explicit `target_hosts` on its own import that excludes `semaphore102`, so that host is not processed twice.

### Mutual Semaphore updates

`semaphore101` and `semaphore102` are both excluded from `vm-updates.yml`: neither can safely patch and reboot itself while it may be running the automation. Each one patches the other through `update-semaphore-peer.yml`, from two independently scheduled templates:

1. A template on `semaphore102` runs `update-semaphore-peer.yml -e peer_host=semaphore101` weekly. It patches `semaphore101`'s OS and Podman images, then waits for its Semaphore web service (`/api/ping`) to answer.
2. `full-updates.yml` runs on `semaphore101` on its own schedule, timed to start after the first job is done, so it finds `semaphore101` already up to date. It runs the whole fleet and finishes with `import_playbook: update-semaphore-peer.yml` and `peer_host: semaphore102`, `image_reference_host: semaphore101`, gated by the same `orchestration_should_run` fact, so `semaphore102` is patched last and only if nothing upstream failed.

`peer_host` has no default and must name one of the two instances: the playbook's first task refuses to run otherwise, so a template created without arguments cannot patch the host that is running it. There is no direct hand-off between the instances, only two schedules timed so each prerequisite is done when it is needed.

**Identical image versions.** `semaphore102` must run exactly what `semaphore101` runs, which a normal tag pull does not guarantee. `container-updates.yml` therefore treats it differently in Step 3: a leading play reads the digest each of `semaphore101`'s units (`semaphore`, `semaphore_db`) is actually running and saves it on `localhost`, and for `semaphore102` the `podman_align_image` role pulls and tags that exact digest instead of resolving a tag, restarting only if something changed. It fills the same `podman_update_summary` fact as `podman_update`, so `semaphore102` appears as a normal row in the report, or as an explicit "alignment skipped" row if `semaphore101` was not healthy enough to read from. The scan of `check-image-updates.yml` leaves both Semaphore hosts out so it cannot move `semaphore102`'s tags off the aligned digests.

**Semaphore version bumps.** When run for `peer_host: semaphore101`, `update-semaphore-peer.yml` checks Docker Hub for the latest Semaphore release, keeps plain `vMAJOR.MINOR.PATCH` tags within the major line currently running, picks the highest one numerically, and applies it by updating the live `Image=` line and restarting `semaphore.service`. Patch and minor bumps are automatic, since semver treats minor releases as backward-compatible. A new major is a deliberate action: pass `-e semaphore_image_tag=vX.Y.Z` once. This only runs in the `semaphore101`-facing direction; `semaphore102` follows through the digest alignment.

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

Evacuating a node and rebalancing the cluster follow the same rules. Memory is checked against `pve_safety_threshold`. Disk is the sum of every disk in the VM's own configuration, since the cluster API reports only the boot disk, and is checked against the free space of `pve_clone_storage`, which is local to each node: a live migration copies the disks. When no node stays under the memory threshold an evacuation still places the VM, since a planned window needs somewhere to put it, and the placement is reported as a warning, in the run log and in the `pve-updates.yml` email.

The rebalance builds one plan that is complete: a VM is moved only if that narrows the gap in memory usage between its node and the target, judged after the moves already in the plan, so applying a plan leaves nothing for a second one and two VMs of the same size are never swapped.

- **`pve_anti_affinity_groups`** lists VMs that must not share a node (the two Semaphore instances). Both the evacuation and the rebalance honour it. Proxmox's own HA affinity rules would require HA-managed VMs on shared or replicated storage, which this cluster does not use.
- **`pve_rebalance_pinned: true`** in a VM's `host_vars` stops the optional rebalance from moving it. It is set on `immich101` and `lpkat101`, whose disks are large enough that every move copies hundreds of gigabytes over the mesh and writes them to a target NVMe. Such a VM is still evacuated when its node is patched, and counts as load on the node where it sits.
- **`pve_shutdown_for_maintenance: true`** shuts a VM down for the maintenance window instead of migrating it, for a VM whose disks cannot move. No VM uses it today.

### Backups

**VM backups.** `vm-backups.yml` migrates each VM to `vm_backup_node` (`pve1`, the only node with the `Stockage_SSD` storage) if it is not there, runs `vzdump` in snapshot mode with zstd, keeps `vm_backup_local_retention` archives locally, uploads the archive to `vm_backup_pcloud_base_path/<vm>` and keeps `vm_backup_pcloud_retention` copies there, then migrates the VM back. The new archive is uploaded before the previous copy is removed when it fits in pCloud's free space with `vm_backup_pcloud_free_margin_gib` to spare, so a failed upload never leaves the VM without an off-site backup. When the quota is too tight, the previous copy is removed first to make room.

**pCloud copies.** `pcloud-backups.yml` has two modes, selected by whether `pcloud_backup_source` is passed as an extra var:

- **Single job**: `pcloud_backup_source`, `pcloud_backup_dest`, `pcloud_backup_mode` (`sync` or `copy`, default `sync`) and `pcloud_backup_extra_args` are passed as extra vars, and one `rclone` command runs through the `pcloud_backup_job` role. Each backup is its own Semaphore template on the same playbook, with its own schedule and extra vars.
- **All jobs**: with no extra vars, the three jobs in `pcloud_backup_jobs` run in parallel, each launched with `async` and `poll: 0` and then awaited with `async_status`, so one failure does not stop the others. This mode launches `rclone` directly, because `async` applies to a single module task and not to `include_role`.

### Provisioning a new Semaphore peer

`provision-semaphore-peer.yml`, run from `semaphore101` against the new VM, copies the seven Quadlet unit files (`semaphore-network.network`, four `.volume` files, `semaphore-db.container`, `semaphore.container`, identical on both instances since nothing in them is host-specific), generates fresh `SEMAPHORE_DB_PASS`, `POSTGRES_PASSWORD` (kept in sync) and `SEMAPHORE_ADMIN_PASSWORD` secrets, clones this repository into `/home/deploy/repos/homelab-iac` (bind-mounted into the container as `/repos`), then enables and starts both services.

It is safe to re-run: if `/etc/semaphore/app.env` already exists the secrets and env files are left alone, since regenerating them on an initialized Postgres volume would desync the stored password.

Not handled by the playbook, and needing a manual checklist once the containers are up:

- Creating the VM on the Proxmox side.
- Recreating Semaphore's own data, which lives in each instance's Postgres and not in this repository: the SSH key in the Key Store, the Variable Group, the repository connection (pointing at `/repos/homelab-iac`), and at minimum the two templates this design needs, "Update semaphore101" (`update-semaphore-peer.yml -e peer_host=semaphore101`) on `semaphore102` and "Full updates" on `semaphore101`, timed to run after it.
- Changing `SEMAPHORE_ADMIN_PASSWORD` from its generated value in Semaphore's UI on first login.

### Provisioning a new fleet VM

`provision-vm.yml` (`-e new_vm_name=<hostname>`) works from a golden template that has no cloud-init and no regenerating SSH host key, so each VM has one stable identity. Everything cloud-init used to do at boot happens once, in this playbook, right after cloning:

0. Fail before touching anything if `new_vm_name` is already taken: an existing Proxmox VM of that name (checked through the dynamic inventory's `hostvars`), an existing `host_vars/<new_vm_name>.yml`, or an existing Katello host of that exact name. This keeps two same-named VMs from existing, which `decommission-vm.yml` could not tell apart.
1. Resolve a free IP through phpIPAM (`phpipam_next_ip`), starting from `phpipam_search_start_ip`, with a TCP:22 liveness probe as a safety net: phpIPAM's data is not assumed to be current, so a candidate that is actually alive is written back into phpIPAM and the search retries (`phpipam_max_retries`) before failing.
2. Full-clone the template on `pve_clone_node` (`qm clone --full --storage pve_clone_storage`), start it and connect at its built-in IP (`provision_template_ip`, `192.168.1.200`), where every fresh clone boots.
3. Reconfigure the clone (`provision_vm_reconnect`): switch the network to the resolved IP with `nmcli`, set the hostname and regenerate the SSH host key. The IP change and the key change both invalidate the SSH session, so it is one fire-and-forget async command, followed by clearing that IP's stale `known_hosts` entry on the controller (`accept-new` only covers hosts never seen before, not a changed key at a reused IP), then `meta: reset_connection` and `wait_for_connection`.
4. Register with Katello (`katello_register_host`, through `hammer host-registration generate-command`) and confirm Katello registered it under the exact hostname, failing on any mismatch: that is what lets `decommission-vm.yml` find it by name alone. Then disable every enabled repository that Katello does not manage (`katello_repo_cleanup`), worked out by diffing the repository IDs in `/etc/yum.repos.d/redhat.repo` against `dnf repolist all` rather than from a fixed list, since the vendor repositories enabled by default vary. Left enabled, they can conflict with Katello's content-view repositories on tightly version-locked packages such as `selinux-policy-targeted`. It fails instead of disabling everything if `redhat.repo` is empty. Then apply OS updates and install `podman` and `git`.
5. Register the IP as used in phpIPAM, then write `host_vars/<new_vm_name>.yml` (`ansible_host: <resolved IP>`) into this repository, commit and push it. Servers are never resolved through AdGuard (DHCP clients only) or any other DNS, so this is how later playbooks find the VM.
6. Rebalance the cluster, since the clone always lands on `pve_clone_node` first.

**Manual prerequisites:**

- **Golden template** (Proxmox side): no cloud-init drive; UEFI (`bios: ovmf`) with `efidisk0` permanently attached, which `qm clone --full` inherits; a `deploy` user with passwordless sudo and `authorized_keys` containing the Semaphore automation key, Katello/Foreman's key and your own; SSH host keys generated once at template build time; static `192.168.1.200/24` through a NetworkManager profile with no `connection.interface-name` or `802-3-ethernet.mac-address` binding, because every clone gets a fresh MAC and sometimes a different interface name, and a bound profile would not activate. Match by device type only (`nmcli con modify eth0-static connection.interface-name "" 802-3-ethernet.mac-address ""`). Then `qm template <vmid>` and set `pve_template_vmid`.
- **phpIPAM API app**: Administration, Server Management, phpIPAM settings: enable the **API** module (off by default); then Administration, API: create an app (`phpipam_api_app_id`, default `ansible`) with `Read/Write` permissions and security **`User token`**. The two SSL modes require a real HTTPS connection and fail with `503 SSL connection is required for API` over plain HTTP, which is what `phpipam101` serves. `User token` mode logs in with real phpIPAM credentials (`phpipam_api_username`, `phpipam_api_password`) to obtain a short-lived session token, once per run. `phpipam_subnet_cidr` (`192.168.1.0/24`) must already exist as a subnet in phpIPAM.

### Decommissioning a fleet VM

`decommission-vm.yml` (`-e new_vm_name=<hostname>`, optionally `-e decom_skip_backup=true`) is the reverse of `provision-vm.yml`. It takes no confirmation flag, only the name, and every step fails loudly rather than guessing:

1. Resolve the VM's vmid, node and IP from `hostvars`, failing if the name is not a known host. The IP is `ansible_host`, falling back to the address in `health_check_url`. It reads `hostvars[new_vm_name]['ansible_host']` with bracket notation, which resolves to undefined where dot notation would raise for a host without that attribute.
2. Migrate to `vm_backup_node` if needed, then take a final `vzdump` to pCloud (`pve_migrate_vm` and `vzdump_backup`, the same roles `vm-backups.yml` uses), as a last-resort rollback. Skipped entirely, migration included, when `decom_skip_backup` is set.
3. Deregister from Katello (`katello_deregister_host`), looking the host up by exact name and failing if it is not found.
4. Remove its IP from phpIPAM (`phpipam_remove_address`), a no-op if already absent.
5. Destroy the VM (`pve_destroy_vm`: graceful `qm shutdown`, confirm stopped, then `qm destroy --purge`).
6. Remove `host_vars/<new_vm_name>.yml` and commit and push the deletion.

### Removing cloud-init from a legacy VM

`remove-cloud-init.yml` (`-e target_hosts=<hostname>`, default `adguard101,phpipam101,lpkat101,immich101,semaphore102,smb101`, `serial: 1`) is a one-time cleanup for VMs that predate the cloud-init-free golden template and still regenerate their SSH host key on every reboot. `semaphore102` and `smb101` are in the default list because the playbook is only ever run from `semaphore101`, the same non-self-reference reasoning as `update-semaphore-peer.yml`.

Per host, in order: take a Proxmox snapshot (`ansible_cloudinit_removal`, distinct from the patching labels so it cannot collide with a concurrent run) and resolve the IP like `decommission-vm.yml`; capture the SSH host key fingerprint; convert the active NetworkManager connection to a persistent static profile (fire-and-forget, then `meta: reset_connection` and `wait_for_connection`, the same disruptive-change pattern as `provision_vm_reconnect`); remove the `cloud-init` and `cloud-utils-growpart` packages; remove the VM's cloud-init drive from its Proxmox config, delegated to the VM's actual node because raw `qm` commands, unlike `pvesh`, are not cluster-aware; run a real `qm shutdown` and `qm start` cycle, since a soft reboot does not cycle QEMU and never applies the pending drive removal; assert that the SSH host key fingerprint is identical to before and that the NetworkManager connection is still `manual`; run the host's `health_check`; and remove the snapshot only once all of that passes.

Each host is wrapped in a `block`/`rescue` that records the reason and calls `meta: end_play`, stopping before the next host but still reaching the report, the same posture as `pve-updates.yml`. A snapshot left behind after a failure is the rollback path. Run against one host at a time first, and not on `adguard101` (LAN DNS) or `lpkat101` (every Katello registration depends on it) as the first test.

### Configuring BunkerWeb

`bunkerweb101` (WAF and reverse proxy, `bunkerweb-all-in-one` image) is configured entirely by `configure-bunkerweb.yml` (the `bunkerweb_configure` role) from `bunkerweb_services` in `host_vars/bunkerweb101.yml`: a list of proxied sites (name, domain, and any setting beyond the `USE_REVERSE_PROXY=yes` baseline). Each site's `{{ domain }}_*` variables go into their own file under `/etc/bunkerweb/conf.d/<name>.env`. Global settings (embedded services, `MULTISITE`, Let's Encrypt, DNS resolvers, LAN whitelist) go into `/etc/bunkerweb/global.env`, and `SERVER_NAME` is generated from the same list. Adding or removing a site is an edit of that list and a re-run: a removed entry's `conf.d` file is found with `ansible.builtin.find`, compared with the list and deleted, so a decommissioned VM leaves no stale reverse-proxy entry. Each service needs a matching `EnvironmentFile=` line in the Quadlet, which the template generates from the same list.

Secrets (OVH DNS-01 credentials, admin password, see [Secrets](#secrets)) are templated into a separate `/etc/bunkerweb/secrets.env` (`mode: '0600'`), never committed.

### Monitoring

Two dedicated VMs run it: `prometheus101` (Prometheus, Alertmanager and `prometheus-pve-exporter`) and `grafana101` (Grafana), provisioned by `provision-vm.yml` like every other fleet VM. Every fleet VM and all three nodes run `node_exporter` (CPU, memory, mountpoints, uptime; a native binary with a systemd unit, not a container, and not installed through `dnf` or `apt`, since non-Katello repositories are disabled). The nodes also run `smartctl_exporter` for SMART and disk health. `prometheus-pve-exporter` runs once on `prometheus101` and covers the whole cluster in one scrape, since Proxmox's API is cluster-aware whichever node it connects through.

**Which services matter reuses `podman_units`.** `node_exporter_install` builds a `--collector.systemd.unit-include` regex from each host's own `podman_units`, using the same `{{ service_name | default(name) }}.service` expression `podman_update` uses, so `node_exporter` reports only the containers this repository tracks for that host.

**Targets are per-host `file_sd` files** (`/etc/prometheus/file_sd/{node_exporter,smartctl_exporter}_targets.d/<name>.json` on `prometheus101`), added and removed by `prometheus_target` and live-reloaded by Prometheus. `provision-vm.yml` registers a new VM automatically and `decommission-vm.yml` removes it, both gated on `'prometheus101' in hostvars` so they do nothing during `prometheus101`'s own bootstrap. `configure-monitoring.yml` registers every existing VM and node, and installs the exporter versions pinned in `group_vars/all.yml`.

**Alerts.** Alertmanager reuses the SMTP credentials of `send_report`, templated into `/etc/alertmanager/alertmanager.yml` with `no_log: true`, since it only reads static YAML. `roles/prometheus_configure/files/alert_rules.yml` defines alerts for a node down, mountpoints and the root filesystem filling up, a tracked container or service failing, SMART status and critical warnings, NVMe wear, spare capacity, media errors and temperature, `nvme_data` filling up, the cluster losing quorum, the PVE exporter being down, a mesh link down or MTU wrong, and a node cut off from the mesh. Dashboards (Node Exporter Full #1860, a Proxmox dashboard #10347, a smartctl_exporter dashboard #22381) are fetched from grafana.com at deploy time instead of vendored, which keeps them current. Grafana's dashboard provider reloads the directory without a restart.

**Bootstrap order.** Add `PROXMOX_TOKEN_SECRET` and `GRAFANA_ADMIN_PASSWORD` to Semaphore's Variable Group, then run `provision-vm.yml -e new_vm_name=prometheus101`, `configure-prometheus.yml`, `provision-vm.yml -e new_vm_name=grafana101` (it registers itself with Prometheus, which now exists in the inventory), `configure-grafana.yml`, and `configure-monitoring.yml` once to register the rest of the fleet.

**Implementation notes.**

- Cockpit listens on port 9090 on the Rocky 10 golden template, the port Prometheus uses, so `prometheus_configure` disables `cockpit.socket` on `prometheus101`.
- `prom/alertmanager` and `prompve/prometheus-pve-exporter` run as a non-root user (GID 65534 and GID 101), so their volume-mounted secret files are group-owned by that GID with mode `0640` or `0750` instead of `root:root 0600`. `grafana.env` needs no such handling: Grafana reads it through `EnvironmentFile=`, consumed by Podman as root before the container starts.
- Each Quadlet container has its own network namespace: `PublishPort=` exposes a port to the LAN but not to sibling containers over `127.0.0.1`. A shared `monitoring-network.network` Quadlet, the pattern `semaphore_provision` uses, gives Prometheus, Alertmanager and the exporter DNS resolution by container name.
- grafana.com dashboard exports reference their datasource as a literal `${DS_xxx}` or `${datasource}` token that only the interactive import wizard resolves. `grafana_configure` gives the provisioned datasource a fixed `uid: prometheus` and rewrites every such placeholder in the downloaded dashboards to point at it.
- Downloaded dashboards get their default time range set to `now-1h`, since an export defaulting to `now-1y` renders empty on a short history.
- The Proxmox dashboard's storage legend shows `{{node}} - {{storage}}`, so `nvme_data` is distinguishable across the three nodes.

**Known limitations.** The smartctl dashboard aggregates across nodes when "All" is selected in the instance picker, because showing each node separately would mean vendoring the dashboard and rewriting about 24 panel queries, and losing automatic upstream updates. The Proxmox dashboard has no per-node instance picker, since one exporter covers the cluster and its `instance` label is always `pve-exporter:9221`; per-node dimensions come from the `name`, `node` and `id` labels. Proxmox's own `pve-firewall`, distinct from the `firewalld` through which `node_exporter_install` opens `9100/tcp`, is not configured here: if it is enabled on the nodes, ports `9100` and `9633` need opening there too.

### Operating Semaphore from a terminal

`scripts/semaphore-ctl` is a small client for the Semaphore API: it lists templates and recent tasks, starts a task, waits for it, and reads its log, or just the lines around its errors (`log <task> --errors`). It is meant for an operator or an assistant working from a terminal on an account that has the Task Runner role, which can start and read tasks but not edit templates.

The API token is read from a file that must not be readable by anyone else (`~/.config/semaphore/semaphore101.token`) and is passed to `curl` through a configuration descriptor, so it never appears on a command line. Starting a task is limited to the template ids listed in `~/.config/semaphore/allowed-templates`, one per line; with no such file nothing can be started, while reading stays available.

## Requirements

- SemaphoreUI, or plain `ansible-playbook`, with:
  - Collections: `ansible-galaxy collection install -r collections/requirements.yml`
  - Python packages: `pip install -r requirements.txt`
- A Proxmox API token with enough privileges to list VMs, create and delete snapshots, and migrate VMs.
- SSH access to all target hosts and to the Proxmox nodes, using a key stored in Semaphore's Key Store (never committed to this repository).
- `rclone` configured with a `pcloud:` remote on `smb101` (for `pcloud-backups.yml`) and on `pve1` (for `vm-backups.yml`).

## Secrets

No credentials are stored in this repository. The Proxmox API token is read with `lookup('env', 'PROXMOX_TOKEN_SECRET')` in `inventory/proxmox.yml` and injected by Semaphore through a Variable Group. SSH keys live only in Semaphore's Key Store. `GITHUB_PUSH_TOKEN` (a GitHub personal access token with write access to this repository) is used by the playbooks that commit back to it, `check-image-updates.yml`, `update-semaphore-peer.yml`, `provision-vm.yml` and `decommission-vm.yml`, and is injected the same way.

`SMTP_USER`, `SMTP_PASSWORD` and `NOTIFY_EMAIL_TO` follow the same pattern and are required by every playbook's `send_report` role. `SMTP_PASSWORD` is an app password, not the account's real password, since `smtp_host` is `smtp.gmail.com`. Each Semaphore instance holds its own Variable Group, so a rotated password has to be updated on both.

`katello_promote` and `cv-retention.yml` pass `KATELLO_HAMMER_USERNAME` and `KATELLO_HAMMER_PASSWORD` explicitly on every `hammer` command (`no_log: true`, since the password would otherwise appear in the command's arguments in Semaphore's task log) rather than relying on the credentials in `lpkat101`'s hammer configuration file. Rotating the Foreman admin password is an update of that variable, with nothing to change on `lpkat101`. `katello_register_host` reuses the same two variables.

`PHPIPAM_API_USERNAME` and `PHPIPAM_API_PASSWORD` (the credentials for the `ansible` app's `User token` mode, see [Provisioning a new fleet VM](#provisioning-a-new-fleet-vm)) are read through `phpipam_api_username` and `phpipam_api_password`. `phpipam_next_ip` exchanges them for a session token once per run. `PHPIPAM_DB_PASSWORD` and `PHPIPAM_DB_ROOT_PASSWORD` must be set before the first `configure-phpipam.yml` run, or the role would reset the database passwords to empty.

`BUNKERWEB_OVH_APPLICATION_KEY`, `BUNKERWEB_OVH_APPLICATION_SECRET`, `BUNKERWEB_OVH_CONSUMER_KEY` (OVH API credentials for the Let's Encrypt DNS-01 challenge) and `BUNKERWEB_ADMIN_PASSWORD` are read through `bunkerweb_ovh_*` and `bunkerweb_admin_password`. Unlike the secrets above, `bunkerweb_configure` writes them to a file on the target (`/etc/bunkerweb/secrets.env`, `mode: '0600'`, `no_log: true` on the templating task), because the BunkerWeb Quadlet reads its configuration only through `EnvironmentFile=`. If they are missing, nothing fails loudly: Let's Encrypt renewal and the admin login break at the next attempt.

`PROXMOX_TOKEN_SECRET` is also exposed as `proxmox_token_secret` in `group_vars/all.yml`, so `prometheus_pve_exporter_configure` reuses the same Proxmox API token, with no second token to create or rotate. `GRAFANA_ADMIN_PASSWORD` is read as `grafana_admin_password`. `prometheus_pve_exporter_configure` (writing `/etc/pve-exporter/pve.yml`) and `grafana_configure` (writing `/etc/grafana/grafana.env`) write their secret to a `mode: '0600'` file with `no_log: true`, for the same reason as `bunkerweb_configure`: the container reads its configuration only from a mounted file.
