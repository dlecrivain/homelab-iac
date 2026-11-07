# homelab-iac

Ansible automation for a homelab built on Proxmox VE, Foreman/Katello and Podman, driven by two [SemaphoreUI](https://semaphoreui.com) instances: `semaphore103` on a VM outside the Kubernetes cluster, and `semaphore-bck` in it. VMs are discovered from the Proxmox API, so there are no static host lists to maintain for them.

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
| `container-updates.yml` | Updates the Podman/Quadlet containers of every host that defines `podman_units`, except `semaphore103`, which `update-semaphore-peer.yml` covers, see [Updating the Semaphores](#updating-the-semaphores). |
| `cleanup-snapshots.yml` | Removes leftover safety snapshots matching `snapshot_label` (default `ansible_patching`; pass `-e snapshot_label=ansible_container` for the container-update ones). |
| `kubernetes-stop.yml`, `kubernetes-start.yml` | Shut down and start again the VMs of the Proxmox pool `kubernetes`, the Talos nodes of the test cluster, by hand, for a change that needs all the memory of the Proxmox nodes. They are not scheduled; with no such pool they do nothing. |
| `talos-create-vm.yml`, `talos-place-vm.yml` | Create a node of the Talos test cluster on `pve1` from the Talos ISO and reserve its address in phpIPAM, then move it, installed, to its own Proxmox node. `talos_vm_name` names the node, see [Kubernetes test cluster](#kubernetes-test-cluster-talos). |
| `phpipam-reserve-address.yml` | Reserves an address in phpIPAM under `reserve_name` (the next free one, or `reserve_ip` when it is given), or reports the one already reserved under that name. Used for the addresses a Kubernetes service needs, such as the load balancer of the ingress or of the DNS server. |
| `pve-updates.yml` | Evacuates, patches and reboots each Proxmox node in turn, halts the whole run on the first failure, and rebalances the cluster once every node is done. Before a host is touched it checks that the pods of its Talos nodes would find room on the other workers (`-e kubernetes_capacity_check=false` skips it). |
| `talos-updates.yml` | Brings the Talos nodes to `talos_version`, the control planes first and one node at a time (`talosctl upgrade`: the node drains itself, reboots into the new image and comes back), checking the cluster before and after each node and stopping at the first problem. Runs on `semaphore103`. The report gives the version of each node before and after. `-e talos_updates_check_only=true` only reports, `-e talos_updates_nodes=<names>` restricts it. Before each node it checks that its pods would find room on the other workers, and it renews the client certificate of `talosctl` on `semaphore103` by itself when less than a year is left. See [docs/kubernetes.md](docs/kubernetes.md). |
| `kubernetes-updates.yml` | Brings Kubernetes to `kubernetes_version` with `talosctl upgrade-k8s`, after a dry run, and reports the version before and after. Runs on `semaphore103`, after `talos-updates.yml`. |
| `certificate-expiry.yml` | Reads when each certificate that nothing else watches ends (the three Proxmox hosts, Katello, the Kubernetes API and the kubelets, the authorities of Proxmox, Talos and Kubernetes, the client certificate of `talosctl`) and mails, and fails, only when one is near its end, over, or unreadable. Runs on `semaphore103`, weekly. See [docs/kubernetes.md](docs/kubernetes.md). |
| `kubernetes-rebalance.yml` | Spreads the pods over the three workers again by running the Descheduler (`k8s/descheduler`) once: the pods of the workers that are fuller than the average are evicted, one by one through the eviction API, and the scheduler puts them where there is room. Runs on `semaphore103`, last in `full-updates.yml`, the only window in which pods are moved. The report gives, for each worker, the pods and what they request before and after, and the pods that moved. `-e kubernetes_rebalance_check_only=true` only measures. See [docs/kubernetes.md](docs/kubernetes.md). |
| `full-updates.yml` | Runs `container-updates.yml`, `vm-updates.yml`, `pve-updates.yml`, `talos-updates.yml`, `kubernetes-updates.yml` and `kubernetes-rebalance.yml`. Each stage runs only if the previous one had no problem, otherwise later stages report `SKIPPED`. Sends one combined report. |
| `update-semaphore-peer.yml` | OS-patches and container-updates `semaphore103`, bumps its Semaphore image to the latest patch or minor release and waits for its web service to answer. `peer_host` is a required extra var and must be `semaphore103`; the playbook is run from `semaphore-bck`, every Wednesday. |
| `check-image-updates.yml` | Daily check of Docker Hub for the images in `image_update_watch_list`, of GitHub releases for `node_exporter` and `smartctl_exporter`, and a scan of the remaining containers. Same-major bumps are written into `group_vars/all.yml` and pushed. The mail lists what is pending, what needs a decision and what is a new major. See [Container image versions](#container-image-versions). |

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
| `pcloud-backups.yml` | `rclone` backups to pCloud. With `pcloud_backup_source` passed as an extra var it runs that one job, otherwise the host (`nfs101`, or `pcloud_backup_host`) runs the jobs of its own `pcloud_backup_jobs` in parallel: the doorbell's files, the Immich library and the archives of the configurations, which it reads where they are. See [Backups](#backups-1). |

### Provisioning

| Playbook | Purpose |
|---|---|
| `provision-vm.yml` | Clones the golden template, resolves a static IP through phpIPAM, reconfigures the clone, registers it with Katello, updates it, installs Podman and git, commits its `host_vars` file and rebalances. See [Provisioning a new fleet VM](#provisioning-a-new-fleet-vm). |
| `decommission-vm.yml` | The reverse: final safety backup, Katello and phpIPAM cleanup, destruction of the VM, removal of its `host_vars` file. See [Decommissioning a fleet VM](#decommissioning-a-fleet-vm). |
| `provision-semaphore-peer.yml` | Deploys Semaphore through Podman Quadlet onto a fresh VM, identically to `semaphore103`. It is how `semaphore103` would be rebuilt, from `semaphore-bck`. |

### Service configuration

| Playbook | Purpose |
|---|---|
| `sync-paperless.yml` | Keeps Paperless-ngx in step with the cloud storage folder that stays the master copy, run from `nfs101`: new files are uploaded, moved or renamed files are recognised by checksum and re-tagged, files that disappeared are tagged `a-purger`, and documents nothing could give a type are tagged `a-classer` and listed in an email. See [Paperless](#paperless). |
| `configure-smb.yml` | Installs Samba on `smb_host` (`nfs101`) and shares one folder, on its own volume, for the Aqara doorbell: SMB 3 only, one account, reachable from the addresses of `smb_allowed_hosts` only, and announced by NetBIOS and mDNS. See [SMB share for the Aqara doorbell](#smb-share-for-the-aqara-doorbell). |
| `configure-nfs.yml` | Serves the files of the Kubernetes test cluster over NFSv4 from `nfs101`, on a volume of its own, to the nodes of the cluster and to the hosts that read a share. See [NFS server for the Kubernetes test cluster](#nfs-server-for-the-kubernetes-test-cluster). |
| `configure-postgres.yml` | Runs the Postgres databases of the Kubernetes test cluster as rootless containers on `nfs101`, one per application, each on its own port and reachable by the nodes only, with a nightly dump. |
| `configure-mariadb.yml` | Runs the MariaDB databases of the Kubernetes test cluster (phpIPAM's) as rootless containers on `nfs101`, same arrangement as `configure-postgres.yml`: one container per application on the local disk, its own port reachable by the nodes and the probe only, passwords generated on the host, and a nightly dump at 03:40. |
| `upgrade-postgres.yml` | Moves one of those databases to the major version its entry in `postgres_instances` names (`-e pg_name=<instance>`): a dump, a new container with its own data directory, a restore, and a check that every table has the same number of rows. The data of the previous version is kept as the way back. See [Paperless](docs/paperless.md). |
| `upgrade-semaphore-db.yml` | Moves the database of `semaphore103` to the Postgres major version that `roles/semaphore_provision/files/semaphore-db.container` names (`-e peer_host=semaphore103`), by a dump and a restore: snapshot of the VM, dump, new container on a volume of its own, restore, check that every table has the same number of rows, then Semaphore starts again. The previous volume, the dump, the previous Quadlet and the snapshot are kept as the way back. Run from `semaphore-bck`, never from `semaphore103` itself, since Semaphore and its database are stopped while it runs. |
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
- `k8s/`: what runs on the Kubernetes test cluster, applied by Argo CD rather than by a playbook, see [Kubernetes workloads](#kubernetes-workloads-argo-cd).
- `docs/kubernetes.md`: the operations guide of the Kubernetes test cluster: what runs, how it works together, how to operate and update it, and exercises.
- `host_vars/`: per-VM variables (health checks, `podman_units`, backup and placement flags) and per-node connection and network details (`pve1`, `pve2`, `pve3`).
- `scripts/renew-client-credentials`: renews the `talosconfig` and the admin `kubeconfig` of a workstation before their certificates end, see [docs/kubernetes.md](docs/kubernetes.md).
- `scripts/semaphore-ctl`: small client for the Semaphore API, see [Operating Semaphore from a terminal](#operating-semaphore-from-a-terminal).
- `scripts/katello-host-delete`: deletes the Katello record of a host whose VM no longer exists, for the leftover of a provisioning run that failed after the registration. It refuses while a Proxmox VM of that name exists.
- `kickstart/` and `scripts/render-kickstart`, `scripts/check-kickstart`: the golden template's installer and its checks, see [Building the golden template](#building-the-golden-template).
- `tasks/`: task files shared by several playbooks: `commit_and_push.yml` (every playbook that commits back to this repository), `check_unit_for_update.yml` (the read-only scan in `check-image-updates.yml`) and `talos_update_node.yml` (one node of `talos-updates.yml`) and `kubernetes_capacity_gate.yml` (whether the other workers can take the pods of the Talos nodes that are about to stop) and `talos_renew_config.yml` (renews the client certificate of `talosctl` before it ends).
- `roles/`, by area:
  - **Patching**: `check_updates`, `apply_updates`, `reboot_and_wait`, `health_check`, `host_status`, `proxmox_snapshot`, `cleanup_snapshot`.
  - **Containers**: `podman_update` (pull and restart a Quadlet unit), `image_retention` (keep the two most recent images per repository), and one `*_configure` role per service that still runs on a VM (`paperless`, `homeassistant`, `grafana`), plus `paperless_sync` (the script that keeps Paperless in step with the cloud storage).
  - **Proxmox**: `pve_placement` (where an evacuated VM goes), `pve_evacuate_host`, `pve_migrate_vm`, `pve_rebalance`, `pve_mesh`, `pve_corosync`, `pve_clone_vm`, `pve_destroy_vm`, `resolve_proxmox_api_host`.
  - **Backups**: `vzdump_backup`, `pcloud_backup_job`.
  - **Katello**: `katello_promote`, `katello_cv_retention`, `katello_register_host`, `katello_deregister_host`, `katello_repo_cleanup`.
  - **Provisioning**: `provision_vm_reconnect`, `semaphore_provision`, `phpipam_login`, `phpipam_next_ip`, `phpipam_remove_address`, `lvm_grow` (grows one or several logical volumes of the template's volume group, adding a virtual disk when needed).
  - **File shares**: `smb_configure` (Samba on `nfs101`, for the Aqara doorbell).
  - **Assistant**: `claude_assistant`, `claude_readonly_access` (the read-only account on the VMs).
  - **Monitoring**: `node_exporter_install`, `smartctl_exporter_install`, `prometheus_target`.
  - **Compliance**: `openscap_scan`.
  - **Reporting**: `send_report` (shared HTML mail skeleton), `capture_start_time`, `capture_host_list`.

## How it works

### Dynamic inventory and node resolution

`inventory/proxmox.yml` uses the `community.proxmox.proxmox` plugin with an API token that is never stored in this repository (see [Secrets](#secrets)). VMs are grouped automatically (`proxmox_all_qemu`, per-node groups) and expose their node and vmid as host facts, which the snapshot and migration roles use to target the right node. The VMs of the Proxmox pool `kubernetes` (the Talos nodes) also form the group `proxmox_pool_kubernetes`: it is excluded from the default targets of every playbook that reaches the fleet over SSH, and `group_vars/proxmox_pool_kubernetes.yml` keeps them out of the rebalance and has the host stop them instead of migrating them. The VMs of the pool `standby` (group `proxmox_pool_standby`) are kept powered off as the way back for an application that moved to the cluster: they are left out of the same playbooks, since nothing answers on them, but not out of `vm-backups.yml`, which does not need them to run. The three Proxmox nodes are not VMs, so they are a small static list, each with its own `host_vars/pve{1,2,3}.yml`.

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

`update-semaphore-peer.yml` imports `container-updates.yml`, scoped to `peer_host`. It is not a stage of `full-updates.yml`, but it keeps reading and writing the same shared fact, so a run on its own behaves like the others.

### Updating the Semaphores

`semaphore103` stays on a VM outside the Kubernetes cluster and `semaphore-bck` runs in it, see [Semaphore of the cluster](#semaphore-of-the-cluster). The one that patches the Proxmox hosts, stops and starts the Talos nodes and starts the cluster again must not depend on the cluster, and a task that drains the node of its own pod would be killed with it. `semaphore-bck` therefore only runs templates that do not touch the nodes: "Update semaphore103" and "Test email".

1. `semaphore103` is excluded from `vm-updates.yml` and `container-updates.yml`: it cannot safely patch and reboot itself while it may be running the automation. A template on `semaphore-bck` runs `update-semaphore-peer.yml -e peer_host=semaphore103` every Wednesday at 16:00. It patches the OS and the Podman images of `semaphore103`, then waits for its web service (`/api/ping`) to answer.
2. `semaphore-bck` is patched by no playbook: its image is in `k8s/semaphore-bck/semaphore.yaml` and follows Renovate and Argo CD like every application, and a Talos node has no operating system to patch.

`peer_host` has no default and must be `semaphore103`: the first task of the playbook refuses to run otherwise, so a template created without arguments cannot patch the host that is running it.

**Semaphore version bumps.** `update-semaphore-peer.yml` checks Docker Hub for the latest Semaphore release, keeps plain `vMAJOR.MINOR.PATCH` tags within the major line currently running on `semaphore103`, picks the highest one numerically, and applies it by updating the live `Image=` line and restarting `semaphore.service`. Patch and minor bumps are automatic, since semver treats minor releases as backward-compatible. A new major is a deliberate action: pass `-e semaphore_image_tag=vX.Y.Z` once.

Whenever a bump happens, a further play compares the version recorded in `roles/semaphore_provision/files/semaphore.container` (read with `slurp` and a Jinja `regex_search` on the controller, because Semaphore's container ships a BusyBox `grep` without PCRE) with what `semaphore103` actually runs, and if they differ it updates the `Image=` line, commits and pushes to `origin/main`. Comparing against the repository's recorded value rather than "did a bump happen in this run" makes it self-healing after an interrupted run. It uses `playbook_dir`, the per-template checkout every other automated push here uses, and authenticates with `GITHUB_PUSH_TOKEN`, which appears briefly in the push command's arguments on that host, an accepted tradeoff for a single-tenant homelab.

**Reporting.** `update-semaphore-peer.yml` tracks a stage status (`OK`, `SKIPPED`, `PROBLEM`) like the other playbooks and sends its own email. Its leading play sets `is_orchestrated_run` so the nested `container-updates.yml` import does not send a second one.

### Container image versions

Every container image is pinned to an explicit version in `group_vars/all.yml` (the `*_version` variables), or by digest for the database image of Immich. No unit runs a floating tag. Quadlet pulls the image named in the unit file when it starts, so a pinned version is what makes a restart predictable.

`container-updates.yml` first re-renders each host's Quadlet files from those variables (Step 2.5, one `*_configure` role per host), so a version bump committed to git reaches the host before the pull. The units restart only if a file changed.

`check-image-updates.yml` runs daily. For the images in `image_update_watch_list` it writes the highest same-major version found on Docker Hub into `group_vars/all.yml` and pushes it, and a new major is only reported. The scan of the remaining containers pulls their pinned tag.

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
- **`pve_rebalance_pinned: true`** in a VM's `host_vars` stops the optional rebalance from moving it. It is set on `nfs101` and `lpkat101`, whose disks are large enough that every move copies hundreds of gigabytes over the mesh and writes them to a target NVMe. Such a VM is still evacuated when its node is patched (stopped instead, if it has `pve_shutdown_for_maintenance`), and counts as load on the node where it sits.
- **`pve_shutdown_for_maintenance: true`** shuts a VM down for the host's reboot instead of migrating it, for a VM whose disks cannot move or that is too large for the memory of the other nodes. It is set on `lpkat101`, on `pve1`. The VM is stopped after the host has installed its updates, since `lpkat101` is the repository the nodes install from, and `pve_shutdown_timeout` gives a VM like Katello more than a minute to stop. It starts again with the host (`onboot`), the run then waits for its health check, and it is also started if the run is blocked after it was stopped.

### Backups

**VM backups.** `vm-backups.yml` migrates each VM to `vm_backup_node` (`pve1`, the only node with the `Stockage_SSD` storage) if it is not there, runs `vzdump` in snapshot mode with zstd, keeps `vm_backup_local_retention` archives locally, uploads the archive to `vm_backup_pcloud_base_path/<vm>` and keeps `vm_backup_pcloud_retention` copies there, then migrates the VM back. The new archive is uploaded before the previous copy is removed when it fits in pCloud's free space with `vm_backup_pcloud_free_margin_gib` to spare, so a failed upload never leaves the VM without an off-site backup. When the quota is too tight, the previous copy is removed first to make room.

**pCloud copies.** `pcloud-backups.yml` has two modes, selected by whether `pcloud_backup_source` is passed as an extra var:

- **Single job**: `pcloud_backup_source`, `pcloud_backup_dest`, `pcloud_backup_mode` (`sync` or `copy`, default `sync`) and `pcloud_backup_extra_args` are passed as extra vars, and one `rclone` command runs through the `pcloud_backup_job` role. Each backup is its own Semaphore template on the same playbook, with its own schedule and extra vars.
- **All jobs**: with no extra vars, the jobs in the host's `pcloud_backup_jobs` (its `host_vars`) run in parallel, each launched with `async` and `poll: 0` and then awaited with `async_status`, so one failure does not stop the others. This mode launches `rclone` directly, because `async` applies to a single module task and not to `include_role`.

A host can also list `pcloud_backup_mounts` (an NFS `src` and a mount `path`): each one is mounted read-only before the jobs run, so `rclone` reads it as an ordinary folder. A share that cannot be mounted stops the play, and a host that is down at boot does not hold up the one that mounts it (`nofail`).

### Provisioning a Semaphore on a VM

`provision-semaphore-peer.yml`, run from the other Semaphore against the new VM (it is how `semaphore103` would be rebuilt, from `semaphore-bck`), copies the seven Quadlet unit files (the database is Postgres 18, whose image keeps its cluster one level below the volume it is given, so the volume is mounted on `/var/lib/postgresql`; a new major version is reached with `upgrade-semaphore-db.yml`) (`semaphore-network.network`, four `.volume` files, `semaphore-db.container`, `semaphore.container`, identical whatever the host since nothing in them is host-specific), generates fresh `SEMAPHORE_DB_PASS`, `POSTGRES_PASSWORD` (kept in sync) and `SEMAPHORE_ADMIN_PASSWORD` secrets, clones this repository into `/home/deploy/repos/homelab-iac` (bind-mounted into the container as `/repos`), then enables and starts both services.

It is safe to re-run: if `/etc/semaphore/app.env` already exists the secrets and env files are left alone, since regenerating them on an initialized Postgres volume would desync the stored password.

Not handled by the playbook, and needing a manual checklist once the containers are up:

- Creating the VM on the Proxmox side.
- Recreating Semaphore's own data, which lives in each instance's Postgres and not in this repository: the SSH key in the Key Store, the Variable Group, the repository connection (pointing at `/repos/homelab-iac`), and at minimum the two templates this design needs, "Full updates" and the templates that touch the nodes on `semaphore103`, and "Update semaphore103" (`update-semaphore-peer.yml -e peer_host=semaphore103`) on `semaphore-bck`.
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
- **phpIPAM API app**: Administration, Server Management, phpIPAM settings: enable the **API** module (off by default); then Administration, API: create an app (`phpipam_api_app_id`, default `ansible`) with `Read/Write` permissions and security **`User token`**. The two SSL modes require the application to see HTTPS, which the proxy header handling of phpIPAM now allows since Traefik ends TLS, but the app was created in `User token` mode and stays there. `User token` mode logs in with real phpIPAM credentials (`phpipam_api_username`, `phpipam_api_password`) to obtain a short-lived session token, once per run. `phpipam_subnet_cidr` (`192.168.1.0/24`) must already exist as a subnet in phpIPAM.

### Decommissioning a fleet VM

`decommission-vm.yml` (`-e new_vm_name=<hostname>`, optionally `-e decom_skip_backup=true`) is the reverse of `provision-vm.yml`. It takes no confirmation flag, only the name, and every step fails loudly rather than guessing:

1. Resolve the VM's vmid, node and IP from `hostvars`, failing if the name is not a known host. The IP is `ansible_host`, falling back to the address in `health_check_url`. It reads `hostvars[new_vm_name]['ansible_host']` with bracket notation, which resolves to undefined where dot notation would raise for a host without that attribute.
2. Migrate to `vm_backup_node` if needed, then take a final `vzdump` to pCloud (`pve_migrate_vm` and `vzdump_backup`, the same roles `vm-backups.yml` uses), as a last-resort rollback. Skipped entirely, migration included, when `decom_skip_backup` is set.
3. Deregister from Katello (`katello_deregister_host`), looking the host up by exact name and failing if it is not found.
4. Remove its IP from phpIPAM (`phpipam_remove_address`), a no-op if already absent.
5. Destroy the VM (`pve_destroy_vm`: graceful `qm shutdown`, confirm stopped, then `qm destroy --purge`).
6. Remove `host_vars/<new_vm_name>.yml` and commit and push the deletion.

### Monitoring

Prometheus, Alertmanager, the exporters they use and Grafana run on the Kubernetes test cluster, from the charts `k8s/prometheus` and `k8s/grafana` that Argo CD applies. They ran on the VMs `prometheus102` and `grafana102` until these were decommissioned with `decommission-vm.yml`; their final backups are in the cloud storage. Every fleet VM and all three nodes run `node_exporter` (CPU, memory, mountpoints, uptime; a native binary with a systemd unit, not a container, and not installed through `dnf` or `apt`, since non-Katello repositories are disabled). The nodes also run `smartctl_exporter` for SMART and disk health. `prometheus-pve-exporter` runs once, as a pod, and covers the whole cluster in one scrape, since Proxmox's API is cluster-aware whichever node it connects through.

**Which services matter reuses `podman_units`.** `node_exporter_install` builds a `--collector.systemd.unit-include` regex from each host's own `podman_units`, using the same `{{ service_name | default(name) }}.service` expression `podman_update` uses, so `node_exporter` reports only the containers this repository tracks for that host.

**Targets are files of this repository**, `k8s/prometheus/targets/<job>-<host>.json`, one for each host and job. `prometheus_target` writes or removes the file and stages it, and the playbook that called it commits it: `provision-vm.yml` and `decommission-vm.yml` with the `host_vars` of the VM, `configure-monitoring.yml` at its end. Argo CD gives the committed files to Prometheus, which reads them again by itself. `configure-monitoring.yml` registers every existing VM and node, and installs the exporter versions pinned in `group_vars/all.yml`.

**Alerts.** The configuration of Alertmanager holds the SMTP login, so it is the Secret `alertmanager-config` of the namespace `monitoring`, made by hand since this repository is public. `k8s/prometheus/files/alert_rules.yml` defines alerts for a node down, mountpoints and the root filesystem filling up, a tracked container or service failing, SMART status and critical warnings, NVMe wear, spare capacity, media errors and temperature, `nvme_data` filling up, the cluster losing quorum, the PVE exporter being down, a mesh link down or MTU wrong, and a node cut off from the mesh. A change of the files of `k8s/prometheus/files` is picked up by Prometheus through a sidecar that reloads it.

**Probes.** `node_exporter` only says that a VM is up, not that the service on it works. `blackbox-exporter` makes the request itself every minute: the job `blackbox_http` of `k8s/prometheus/files/prometheus.yml` lists the pages that must answer 2xx with a certificate that is valid for their name (Paperless, Argo CD, phpIPAM and Home Assistant), and `blackbox_tcp` the ports that must accept a connection (the databases and the NFS of `nfs101`). `EndpointDown` fires after five minutes of failure, `CertificateExpiresSoon` when a certificate has less than 14 days left, and `ProbeExporterDown` when the exporter itself cannot be scraped, since a probe that is not scraped has nothing to report. They come from the nodes of the cluster, which `nfs101` already lets in.

**Who watches the watcher.** Prometheus and Alertmanager cannot warn about their own failure, so `watch-monitoring.yml` (Semaphore template 54, every 20 minutes) asks each of them whether it is ready and mails when one has not answered for six minutes. It runs outside the cluster, so it moves with Semaphore if Semaphore moves.

**Retention and disk wear.** Prometheus keeps a year of samples (`retention` in `k8s/prometheus/values.yaml`), so the history of SMART wear is not trimmed after a month. The database, about 4 GB for the three months it holds, is on NFS, in the folder `data` of the share `prometheus` of `nfs101`: Prometheus documents local disks as the supported storage, and the choice was made for a single instance that always writes alone (`Recreate`) and a snapshot each night. `CronJob prometheus-snapshot` asks Prometheus for a snapshot at 01:40 and mirrors it into the folder `backup` of the share, `nfs101` sends that mirror to `pcloud:Backups/Prometheus`, so a corruption costs one night at most. `k8s/prometheus/files/disk_wear.rules.yml` turns the SMART data into one wear figure and an estimated end-of-life date per disk, and the *Disk wear* dashboard shows them in one table, each end-of-life estimate as "date (Ny Nd)" so the column filters and sorts as text in chronological order. The NVMe endurance ratings it needs are written in that file, by the name smartctl reports: a new NVMe model needs its rating added there, or the dashboard falls back to the drive's own `percentage_used`. The images are in `k8s/prometheus/values.yaml`, where Renovate proposes the new ones as pull requests.

**Implementation notes.**

- Dashboards are vendored in `k8s/grafana/dashboards/`, and Grafana's data source in `k8s/grafana/files/datasource.yml`, so a restart cannot discard a hand edit or pull in an unreviewed upstream change: a dashboard is changed in the file and committed. The data source has a fixed `uid: prometheus`, which every dashboard refers to, and points at the Service `prometheus` of the cluster. Grafana's own state (the administrator, the preferences) is in the Postgres `grafana` of `nfs101`, port 5434, and its logins are the Secrets `grafana-env` and `grafana-database`, made by hand since this repository is public.
- The Proxmox dashboard's storage legend shows `{{node}} - {{storage}}`, so `nvme_data` is distinguishable across the three nodes.

### Immich

Immich runs on the Kubernetes test cluster (`k8s/immich`, see [Kubernetes workloads](#kubernetes-workloads-argo-cd)). The server, the machine-learning service and Valkey are pods of the `immich` namespace, and everything that has to survive is on `nfs101`.

- **Photos and videos.** The `immich` share of `nfs101` is mounted at `/data` in the server pod, so the paths stored in the database are the same as they were on the VM that ran Immich before. `library` holds the originals, `thumbs` and `encoded-video` can be generated again. The machine-learning pod keeps its models in `model-cache` of the same share, and the dumps taken before a synchronisation go to `presync-dumps`.
- **Database.** The instance `immich` of `postgres_instances` in `host_vars/nfs101.yml` (port 5435). It runs the image of `immich_postgres_image`, Postgres 14 with the vector extensions, which is why it does not use `postgres_image`, created with `--data-checksums` and 128 MB of shared memory (`initdb_args` and `shm_size` of `postgres_configure`). Its password is generated on `nfs101` and put in the Secret `immich-secrets` (key `DB_PASSWORD`) by hand, never in this repository: `kubectl -n immich create secret generic immich-secrets --from-literal=DB_PASSWORD="$(ssh deploy@192.168.1.130 "grep ^POSTGRES_PASSWORD= /var/lib/postgres/immich.env | cut -d= -f2")"`.
- **Versions.** The tags of the server and of the machine-learning service are in `k8s/immich` and follow the same policy as the other applications. The first start of a newer version migrates the database and a migration does not roll back: `presync-dump.yaml` takes a dump before every synchronisation, which is the way back.
- **Copies.** `nfs101` copies the `library` folder of the share to the cloud storage every night (`pcloud_backup_jobs`). The database has its own nightly dump on `nfs101`, the dump of Immich itself in `backups`, and the dumps taken before a synchronisation.
- **Uploads.** The read timeout of the `websecure` entry point of Traefik is lifted (`k8s/values/traefik.yaml`): its default of 60 seconds covers the whole request and would cut the upload of a long video.

### AdGuard Home

AdGuard Home, the DNS server and filter of the devices of the LAN, runs on the Kubernetes test cluster (`k8s/adguard`, see [Kubernetes workloads](#kubernetes-workloads-argo-cd)). The DHCP of the Freebox gives its address, `192.168.1.114`, to the devices as their DNS server, so that address stayed the same through the move: the Service `adguard-dns` asks Cilium for it (`lbipam.cilium.io/ips`, the address is in the pool of `k8s/cilium-lb` and is `talos-lb103` in phpIPAM), and a worker announces it on the LAN like the one of Traefik. The servers and the nodes of the cluster do not use it, they ask the Freebox directly, so the cluster does not depend on its own DNS to start.

- **Source addresses.** The Service has `externalTrafficPolicy: Local`, so that AdGuard sees the address of each device: its log, its statistics and its limit of requests per client depend on it. Without it every request would come from the address of a node.
- **Files.** The `adguard` share of `nfs101` holds `conf` (the configuration, with the filters, the rules and the account of the interface) and `work` (the query log of 90 days, the statistics and the downloaded filters). One replica, replaced as a whole on an update, so an update cuts the DNS for a few seconds and the loss of a node for a minute or two.
- **Interface.** Behind Traefik at `adguard.lecrivaindaniel.fr`, like every application.
- **Copies.** `nfs101` packs `conf` every night into a dated archive kept 14 days (`config_archives`), and copies the archives to the cloud storage (`Backups/AdGuard`). The query log is not copied.
- **Monitoring.** The probe `dns_lan` of blackbox-exporter asks `192.168.1.114` for the address of `lecrivaindaniel.fr` every minute, and `EndpointDown` mails after five minutes without an answer.

### Semaphore of the cluster

`semaphore-bck`, the second Semaphore, runs on the Kubernetes test cluster (`k8s/semaphore-bck`, see [Kubernetes workloads](#kubernetes-workloads-argo-cd)) and `semaphore103` stays on a VM outside it, see [Updating the Semaphores](#updating-the-semaphores). It is published at `semaphore-bck.lecrivaindaniel.fr`.

- **Database.** The instance `semaphore-bck` of `postgres_instances` in `host_vars/nfs101.yml` (port 5436), like the other applications, with its nightly dump. It was restored from the Postgres 16 container of the VM that ran it before, with `pg_dump -Fc --no-owner --no-acl` and `pg_restore`.
- **Secret.** `semaphore-bck-secrets` holds `SEMAPHORE_DB_PASS` (the password generated on `nfs101`, in `/var/lib/postgres/semaphore-bck.env`) and the three keys of Semaphore's `config.json`: `SEMAPHORE_COOKIE_HASH`, `SEMAPHORE_COOKIE_ENCRYPTION` and `SEMAPHORE_ACCESS_KEY_ENCRYPTION`. The last one encrypts the keys and the variables stored in the database, so it must never change and a copy has to be kept outside the cluster. The Secret is made by hand, once, since this repository is public.
- **Pod.** Nothing in it is kept: the configuration comes from the environment and the checkouts of the repository are in an `emptyDir`. One replica, replaced as a whole on an update, never two at once, since two instances would both start the schedules.
- **Templates.** The tasks of this instance run in a pod of the cluster, so it must never run a template that stops or drains a node, nor one that has to work while the cluster is down. Those are the templates of `semaphore103`. `semaphore-bck` has "Update semaphore103" and "Test email".
- **From a terminal.** `semaphore-ctl` talks to it with `SEMAPHORE_URL=https://semaphore-bck.lecrivaindaniel.fr`, its own token file in `SEMAPHORE_TOKEN_FILE` and its own list of templates in `SEMAPHORE_ALLOW_FILE`.

### Paperless

The document archive designed in [docs/paperless.md](docs/paperless.md) runs on the Kubernetes test cluster, see [Kubernetes workloads](#kubernetes-workloads-argo-cd). The database password and the application secret key are in the Secret `paperless-secrets`, made by hand and never in this repository. The first start of a new version runs the database migrations, which takes a few minutes. The administrator account is created by hand, once, with `kubectl -n paperless exec -it deploy/paperless -- python3 manage.py createsuperuser`.

The cloud storage folder stays the only place where documents are filed and renamed; Paperless is an index kept in step with it, which can be rebuilt from the folder. `sync-paperless.yml` runs `paperless-sync` on `nfs101`, the host that copies to the cloud storage with its token, so that credential is not copied to more hosts. It lists the folder, downloads the files whose identifier, size or modification time changed since the last run, computes their SHA-256 and compares it with the checksum Paperless stores for every original. A state file under `/var/lib/paperless-sync` remembers which document each file became.

- A **new file** is uploaded with a tag per folder (the leading number is dropped), the title and, when the name carries one, the date (`YYYY-MM[-DD]_...`, `YYYY_MM`, `MM_YYYY`, `Epson_DDMMYYYY...`), and the correspondent and type that the rules give it.
- A **moved or renamed file** is recognised by its checksum, not uploaded again: the document gets the tags of the new path, and the correspondent and type that the rules give the new name, so renaming a file is how a type is changed.
- A file **deleted or replaced** leaves its document in place with the tag `a-purger`, because the API user cannot delete. Nothing is ever deleted from Paperless, nor written to the folder.
- A new document that nothing gave a type, neither the rules nor Paperless's own matching, gets the tag `a-classer` and the playbook emails the list, with the number of documents that carry the tag and a link to them. A renamed file that now has a type loses the tag.
- Files Paperless cannot import (Office documents, archives) are listed in the run's output as skipped; export them to PDF to have them imported.

The rules are a JSON file next to the documents (`paperless_sync_rules`), not in this repository, since they name the owner's correspondents; `roles/paperless_sync/files/rules.example.json` shows the format. Each rule has selectors (`file`, a regular expression on the lower-case, accent-free name; `tags`, `tags_any`; `unless`) and the first rule of a list that satisfies them wins. A correspondent rule can carry `match`, the regular expression given to Paperless when it creates the correspondent. The API token is of a Paperless user without administrator or delete rights, placed by hand in `paperless_sync_token_file`; `paperless_sync_dry_run` reports what would be done and changes nothing. A run uploads at most 100 new files, so a folder copied by mistake cannot flood the archive, and it fails, and so does the email, when an upload never shows up in Paperless two days later. The Semaphore template runs the playbook every night.

### SMB share for the Aqara doorbell

The chime of the Aqara Video Doorbell G4 (the indoor unit, "Doorbell Repeater", which also holds its storage) can back its event clips up to an SMB share from the Aqara Home app. `nfs101` provides that share and copies the files to the cloud storage itself. The share used to be on a small VM of its own, `smb101`, so that a connected device that is not trusted wrote to a host that held nothing else; it was merged into `nfs101` to have one host less, and the separation is replaced by what is listed below: SELinux lets Samba read only the files labelled for it, which the data of the cluster are not, the share is on a volume of its own, and only the two devices can reach it. The Freebox's own share was not used: it has a single login for the whole disk and no per-folder rights.

`configure-smb.yml` runs the `smb_configure` role:

- **Volume**: the files live on `smb_data_lv`, mounted on `smb_share_path` with `nodev,nosuid,noexec` and labelled `samba_share_t`, so they cannot fill the system's volumes. It is created at 1 GiB and grown by `lvm_grow` to `smb_data_size_gb`, which adds a virtual disk to the VM when the volume group is too small.
- **Account**: one Samba account, `smb_writer_user`, with no shell, which is the one typed into the device. Its password is placed by hand in `/root/.config/smb/<account>-password` (mode 0600) before the first run, which stops with the command to use when it is missing. It is not in this repository.
- **Access**: SMB 3 at the least (`smb_min_protocol`), no guest access, and `hosts allow` plus one firewalld rule per address of `smb_allowed_hosts` for the `samba` and `mdns` services, so that only the devices of the doorbell can reach or find the share. `nmbd` (NetBIOS) and Avahi (mDNS) announce it under the names it had on its own host (`smb_netbios_name`, `smb_service_name`), since devices find a server through one or the other, and `smb_listen_address` gives the host the address the share used to have, as a second address, for a device that kept it. The router has to give those devices a fixed address, or the list stops matching.
- **Copy to the cloud storage**: `pcloud-backups.yml` copies the files from this host, like the others, with the pCloud token in `/root/.config/rclone/rclone.conf` (`rclone authorize "pcloud"` on a machine with a browser, put there by hand, mode 0600), on the same Semaphore schedule. The job is in `pcloud_backup_jobs` in `host_vars/nfs101.yml`: files younger than 10 minutes wait for the next run, since the device may still be writing them, and nothing is ever deleted from the cloud storage. The host faces a device that is not trusted and holds the pCloud token, which is the price of having one host: if the token leaks, it is revoked at pCloud and `rclone.conf` is made again.
- **Retention**: the Aqara Home app deletes the clips from the share after the delay chosen in it (90 days). The copy to the cloud storage keeps them.


### Home Assistant

Home Assistant Container and the Matter Server (the matter.js server that the Home Assistant Matter Server add-on is built on) run on the Kubernetes test cluster, from `k8s/homeassistant/`. They ran on the VM `homeassistant101`, which replaced Home Assistant OS (its host has no `dnf`, Podman, Katello agent or SSH, so this repository could not manage it), until that VM was decommissioned with `decommission-vm.yml`; its final backup is in the cloud storage.

- **One pod on the host network.** Both containers are in one pod and talk over `localhost`, as they did on the VM. Device discovery (mDNS, SSDP, DHCP), the HomeKit bridge and the Thread and Matter devices need the LAN itself, so the pod is on the network of its node, in the only namespace of the cluster that is `privileged`, since the PodSecurity baseline refuses `hostNetwork`. It has the address of the worker it runs on (it prefers `talos-wk103` and can start on another one), and everything that reaches it does so by name. Only one instance may run: HomeKit and Matter would clash, hence the `Recreate` strategy and a single replica.
- **Storage.** The configuration is on the share `homeassistant` of `nfs101` and the data of the Matter fabric in its `matter` folder, which must belong to uid 1000, the user of the Matter image. The history is in a Postgres of its own on `nfs101` (port 5433), never in a SQLite file on NFS, whose locking is not reliable: `recorder: db_url: !env_var HA_RECORDER_URL` in `configuration.yaml`, and the address with its password is in the Secret `homeassistant-secrets`, made by hand since this repository is public.
- **Images.** The tags are in the manifest and Renovate proposes the new ones as pull requests that are merged by hand, since a monthly release can break an integration. The Matter fabric is the one of the add-on (vendor 0x134B, fabric 2): the server names its folder after them, so `VENDOR_ID` and `FABRIC_ID` must stay as they are.
- **Thread and Matter routes** arrive in router advertisements, so the workers set `accept_ra_rt_info_max_plen` to 64 (a `SysctlConfig` document, with the other settings of the nodes).
- **Reverse proxy.** Traefik publishes it as `homeassistant.lecrivaindaniel.fr`. Home Assistant answers HTTP 400 to every proxied request until `use_x_forwarded_for` and `trusted_proxies` are set, and since 2026.10 it keeps its HTTP settings in `.storage/http` and ignores `http:` in `configuration.yaml`, so they belong to the instance's own state: they trust the six nodes, since Traefik reaches the pod from the address of its own node, and the pod network.
- **Backup.** Each night `pcloud-backups.yml`, run on `nfs101`, packs the configuration and the Matter data into `/srv/backups/homeassistant/homeassistant-DATE.tar.gz` (14 days, without the internal backups, the caches, the logs and the history), then copies it to `pcloud:Backups/HomeAssistant`. The history is dumped by the Postgres of `nfs101` itself every night. The archives hold the credentials of the integrations: the folder is closed to everyone but root on `smb101`.

### Kubernetes test cluster (Talos)

How to operate the cluster, what to do when a part is down and how Talos is updated is in [docs/kubernetes.md](docs/kubernetes.md).

The VMs of the Proxmox pool `kubernetes` are the nodes of a test cluster running [Talos Linux](https://www.talos.dev), which has no SSH and no package manager, so the playbooks that reach the fleet over SSH leave the pool out (see [Dynamic inventory and node resolution](#dynamic-inventory-and-node-resolution)). `talos_nodes` in `group_vars/all.yml` lists the nodes: name, VM id, role, Proxmox node, memory and disk.

- **ISO.** The nodes are installed from `Stockage_SSD:iso/talos-<version>-qemu-guest-agent.iso` on `pve1`, the only node with an ISO storage. It is built by [Image Factory](https://factory.talos.dev) with the `siderolabs/qemu-guest-agent` extension: posting the extension list to `https://factory.talos.dev/schematics` returns a schematic id, and `pve1` downloads the image itself with `pvesh create /nodes/pve1/storage/Stockage_SSD/download-url --content iso --filename talos-<version>-qemu-guest-agent.iso --url https://factory.talos.dev/image/<schematic id>/<version>/metal-amd64.iso`.
- **Creating a node.** `talos-create-vm.yml` takes `talos_vm_name`, reserves the address of the node in phpIPAM (it reuses the one already reserved for that name), creates the pool and the VM on `pve1` and starts it from the ISO. It reports the address the node got from DHCP, where it waits for its configuration in maintenance mode, and the reserved address to put in that configuration. Nodes are created one at a time: `pve1` already carries the most memory, and the playbook stops when less than 2 GiB would be left there.
- **Installing it.** `talosctl apply-config` from a workstation writes Talos to the disk and sets the reserved address. The generated configuration holds the secrets of the cluster, so it stays outside this repository.
- **Moving it.** `talos-place-vm.yml` takes the same variable, ejects the ISO, whose storage exists only on `pve1`, and migrates the node with its disk to the Proxmox node that `talos_nodes` gives it.
- **Nothing is stopped on a schedule.** One control plane and one worker run on each Proxmox node, numbered after it, so a Proxmox node can be updated or lost while the other two keep the quorum of etcd and the pods. Its two Talos VMs are stopped with it (`group_vars/proxmox_pool_kubernetes.yml`) and started again after, never migrated. `kubernetes-stop.yml` and `kubernetes-start.yml` remain for a change that needs all the memory, run by hand. `Backup - VM` migrates the VMs to `pve1` one at a time, since `pve1` holds `lpkat101`, which may be stopped for the backup if the room runs short.

### Kubernetes workloads (Argo CD)

[Argo CD](https://argo-cd.readthedocs.io) runs in the cluster, compares what `k8s/` describes with what runs, and applies the difference. A change reaches the cluster by being committed to `main`, and going back is a `git revert`. Nothing in this directory holds a secret, since the repository is public.

- `k8s/bootstrap/argocd-values.yaml` holds the values of the one-time `helm install` of Argo CD (chart `argo/argo-cd`, pinned in the command below). `k8s/bootstrap/root-application.yaml` is the only Application created by hand: it watches `k8s/apps`.
- `k8s/paperless/` holds the plain manifests of Paperless-ngx on the cluster: the application with its files on the `nfs101` share and its database in the Postgres of `nfs101`, and a Valkey for its queue. The Secret `paperless-secrets` (the password of the database and the application secret key) is made by hand, once, since this repository is public.
- `k8s/apps/` has one Application per component of the cluster. Adding a file installs the component. Removing one removes the Application but not what it deployed, which is deleted by hand: a cascade would also take down Cilium, for one. Each pins a chart version or a tag; the Helm ones read their values from `k8s/values/` through a second source of the same repository.
- `k8s/phpipam/` holds the manifests of phpIPAM, the address manager the playbooks call as an API (`phpipam_base_url`): the web interface and the cron job that scans the subnets every hour, as two Deployments, with their configuration files (the two overrides that make phpIPAM read `X-Forwarded-Proto` behind Traefik) in a ConfigMap. The database is the MariaDB `phpipam` of `nfs101`, and its password is in the Secret `phpipam-secrets`, made by hand like the others. The pods get no extra capability: the PodSecurity baseline of the cluster refuses `NET_ADMIN` and `NET_RAW`, and containerd already gives the raw sockets that ping needs. A new release of phpIPAM usually needs its database upgraded from the interface before it works, so Renovate proposes it as a pull request, and a dump is taken first with `systemctl --user start mariadb-dump-phpipam` on `nfs101`. It ran on the VM `phpipam102` until it was decommissioned with `decommission-vm.yml`.
- `k8s/descheduler/` holds the Descheduler, which spreads the pods over the workers again. Its CronJob is suspended for good: only `kubernetes-rebalance.yml`, at the end of `full-updates.yml`, creates a Job from it, so pods are moved in that window and never otherwise. `policy.yaml` has the rules (nothing is protected by name, since pods only move in that window), `rbac.yaml` the account of the Descheduler and the restricted account `rebalancer` that the playbook uses. See [docs/kubernetes.md](docs/kubernetes.md).
- `renovate.json` keeps the versions of `k8s/` up to date: [Renovate](https://docs.renovatebot.com) reads the image tags of the manifests and the chart versions and tags of the Applications, between 03:00 and 04:00, and waits three days after a release. A minor or patch version of Paperless-ngx and its Valkey, and of a chart, is merged by itself, and Argo CD deploys the merge; Cilium, which is synchronised by hand, and every new major version are left as a pull request to review. `k8s/paperless/presync-dump.yaml` takes a dump of the Paperless database before each of its synchronisations, so that the first start of a newer version, which migrates the database, can be undone by going back to the dump. Going back to the previous version is a `git revert`. Argo CD mails when an application has been updated and is healthy, when a synchronisation fails and when an application is degraded, through the mailbox of the alerts: the Secret `argocd-notifications-secret` (`email-username`, `email-password`) and the `notifications.argoproj.io/subscribe.<trigger>.email` annotations of the `default` AppProject (recipient) are made by hand, since this repository is public. Renovate does not touch `group_vars/all.yml`: the versions of the Podman services are bumped by `check-image-updates.yml`.
- Components synchronise by themselves and heal manual changes, except Cilium, which is synchronised by hand after its diff has been read. A replica count changed by hand is therefore set back, unless the Application lists it in `ignoreDifferences`.

Installing it on a new cluster: `helm install argocd argo/argo-cd --version 10.11.7 --namespace argocd --create-namespace -f k8s/bootstrap/argocd-values.yaml`, then `kubectl apply -f k8s/bootstrap/root-application.yaml`. A change of the values is applied with the same command and `helm upgrade`. The first password of the `admin` account is in the `argocd-initial-admin-secret` Secret. The interface is on port 30081 of any node until Traefik publishes it.

### Ingress and certificates (Traefik)

[Traefik](https://traefik.io) is the ingress controller of the Kubernetes test cluster, in front of the interfaces of the cluster, of the applications that run on it, and of the web interfaces of the VMs.

- **Address.** Its Service takes `talos-lb101` (`192.168.1.128`) from the pool of `k8s/cilium-lb`, and the workers answer ARP for it (Cilium L2 announcements, enabled in `k8s/values/cilium.yaml`). The addresses come from phpIPAM, reserved with `phpipam-reserve-address.yml`; `talos-lb102` (`192.168.1.129`) is kept spare and `talos-lb103` (`192.168.1.114`) is the DNS server of the LAN, see [AdGuard Home](#adguard-home). Turning the announcements on needs the Cilium operator and agents to restart once after the manual synchronisation of the `cilium` Application.
- **Names.** The zone at OVH holds one record for the lab, the wildcard `*.lecrivaindaniel.fr` that points to Traefik (`192.168.1.128`), so no name is declared one by one and none shows in the zone. A name under `k8s.lecrivaindaniel.fr` goes there too, and Traefik chooses the route by the name. AdGuard filters the requests of the devices in DHCP and holds no record.
- **Certificates.** cert-manager and the OVH webhook answer the DNS-01 challenge of Let's Encrypt in the OVH zone, through the `le-staging` and `le-prod` issuers. `k8s/ingress/certificate.yaml` asks for `*.lecrivaindaniel.fr` and `*.k8s.lecrivaindaniel.fr` and makes it the default certificate of Traefik, so a new interface needs only an Ingress, with no certificate of its own and no name of its own in the public logs of the certificate authorities. It is issued by `le-prod`; `le-staging`, whose certificates browsers do not trust, is there to try a change without using the production limits of Let's Encrypt.
- **Secret.** The webhook reads the `ovh-credentials` Secret of the `cert-manager` namespace, with the keys `applicationKey`, `applicationSecret` and `applicationConsumerKey`. It holds OVH API credentials that may edit the zone, and it is created by hand, once, since this repository is public: `kubectl -n cert-manager create secret generic ovh-credentials --from-literal=applicationKey=... --from-literal=applicationSecret=... --from-literal=applicationConsumerKey=...`.
- **Order.** The Applications install in sync waves: cert-manager, then its OVH webhook and Traefik, then `ingress` and `edge`, which need the CRDs of both.
- **Interfaces on VMs (`k8s/edge`).** A small Helm chart publishes the web interfaces that run on VMs under their own names: the three Proxmox nodes and both Semaphores. Each entry of `sites` in `k8s/edge/values.yaml` becomes a Service without pods, of type `ExternalName`, whose name is the address of its VM, and an Ingress, so adding a site is adding a line (an `EndpointSlice` would not do: Argo CD does not manage them, and `allowExternalNameServices` is on in `k8s/values/traefik.yaml`). They are served by the default wildcard certificate, and a Middleware gives them security headers. The Proxmox interfaces speak HTTPS with certificates of the CA of their cluster, whose public certificate is in `k8s/edge/files/pve-root-ca.pem`: a `ServersTransport` checks each node against it, and the certificates carry the address Traefik connects to. The read timeout of the `websecure` entry point is lifted in `k8s/values/traefik.yaml`, since its default of 60 seconds covers the whole request and cuts a long upload. Traefik has no application firewall.

### NFS server for the Kubernetes test cluster

`nfs101` holds the files of the applications that move to the cluster, until a NAS takes over. It is a VM like the others, created by `provision-vm.yml` (cloned on `pve1`, where the golden template is, then placed by the final rebalance) and configured by `configure-nfs.yml` (the `nfs_configure` role, with its variables in `host_vars/nfs101.yml`). It is separate from `smb101`, which faces the doorbell and must not carry the data of the cluster.

- **Volume.** The files live on `lv_nfs`, mounted on `/srv/nfs`, grown by `lvm_grow` to `nfs_data_size_gb` on a virtual disk of its own. That disk has `backup=0`, so `vm-backups.yml` copies the system disk and leaves the data out: it would not fit in the cloud storage, and it is protected otherwise. The same host copies the Immich share to the cloud storage, and Paperless is rebuilt from it.
- **Shares.** `nfs_shares` lists them: each is a directory of `/srv/nfs`, exported on its own. The nodes of `nfs_clients` read and write with `no_root_squash`, since the volumes of a pod are set up by root on the node, and the hosts in a share's `readers` mount it read-only with root squashed, unless the share sets `readers_root_squash: false` for files that only root can read, such as a configuration with its secrets. Only NFSv4 is served, so the one port opened is 2049/tcp, and only to those addresses.
- **CIS profile.** The hardened template masks `nfs-server`, which the role switches on, so OpenSCAP lists the NFS rules as failed on this host.
- **Databases.** `configure-postgres.yml` (the `postgres_configure` role, `postgres_instances` in `host_vars/nfs101.yml`) runs one rootless Postgres container per application on the local disk of `nfs101`, never on NFS: a hiccup of the mount could damage the files. Each has its own port, opened to the nodes only, and a password generated on the host the first time. The Secret an application reads it from is made by hand from that file, since this repository is public. A systemd timer dumps each database at 03:30 into `/var/backups/postgres`, seven days are kept, and the system disk of the VM is part of `vzdump`. A new image of the same major version, pinned by digest in `postgres_image`, is applied by running the playbook again. A major version needs a dump and a restore, which `upgrade-postgres.yml` does; from Postgres 18 the data of each major version is in a directory of its own (`data18`), since the image keeps its cluster in a directory named after the version.
- **MariaDB.** `configure-mariadb.yml` (the `mariadb_configure` role, `mariadb_instances` in `host_vars/nfs101.yml`) does the same for the applications that need MariaDB, today phpIPAM: a rootless container on the local disk of `nfs101`, its own port 3306 opened to the nodes and the probe, passwords generated in `/var/lib/mariadb/<name>.env`, a dump at 03:40 into `/var/backups/mariadb` kept seven days, and the image pinned by digest in `mariadb_image`.
- **Mounting from a pod.** A volume of type `nfs` with the server `nfs101` and the path `/srv/nfs/<share>`, with `nfsvers=4.2`. The mount is hard: if `nfs101` stops, the pods that use it wait for it.

### claude101

`claude101` runs Claude Code as a service that the owner drives from a phone through Remote Control. A request is the authorization: it reads, edits this repository, commits, pushes and starts playbooks without asking again for each step, then reports what it did. [docs/claude101.md](docs/claude101.md) gives the design and the reasons for its limits. `configure-claude.yml` applies the `claude_assistant` role to `claude_host`:

- **Installation**: Claude Code comes from Anthropic's signed dnf repository, whose key is imported only when its fingerprint matches the published one. The repository stays disabled, like every repository outside Katello, and is enabled for the one install of `claude_code_version`, so the host moves only when that variable changes.
- **Privileges**: the service runs as an unprivileged user that has no sudo and no ssh key of its own to another host (it reads the VMs only through `fleet-ro`, below). Its permission rules and its `auto` permission mode are in `/etc/claude-code/managed-settings.json`, owned by root, where deny rules apply in every mode and bypass mode is disabled.
- **Playbooks**: the assistant types `semaphore-ctl`, which runs the repository's script as another user, `claude-semaphore`, through one sudoers rule. That user alone reads the Semaphore token (`/etc/claude-assistant/semaphore.token`), and the template ids it may start are `claude_assistant_semaphore_allowed_templates` in the host's `host_vars` (ids are per Semaphore instance, `claude_assistant_semaphore_host`; `'*'` allows every template).
- **Reading the VMs**: `fleet-ro <vm> <verb>` shows the state of a VM without any privilege on it. `configure-claude.yml` creates a user, `claude-fleet`, that holds an SSH key the assistant cannot read, and the command reaches only the hosts of `/etc/claude-fleet/hosts` (the VMs of the inventory, `claude101` excepted), always as `claude_readonly_user`, through one sudoers rule like `semaphore-ctl`. `configure-claude-access.yml` then creates that account on every VM: a locked password, the key authorized from `claude101` only with `restrict`, and a login shell that hands the words of the command to `claude-ro-tools` through one sudoers rule, never to a shell. The script has a fixed list of verbs (`overview`, `updates`, `units`, `unit`, `logs`, `journal`, `containers`, `container-logs`, `network`, `firewall`) whose arguments are checked against a pattern and whose output is capped. Where the hardened template lets only `deploy` in over SSH (`AllowUsers` in `00-fleet.conf`), the account is added to that line, since the CIS profile wants a single `AllowUsers` line. The Proxmox nodes have no `sudo` and are left out. Logs can hold a secret; the instruction file tells the assistant to copy none.
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

`scripts/semaphore-ctl` is a small client for the Semaphore API: it lists templates and recent tasks, starts a task (`run <template> --var name=value` fills in a survey variable such as `new_vm_name`), waits for it, and reads its log, or just the lines around its errors (`log <task> --errors`). It is meant for an operator or an assistant working from a terminal on an account that has the Task Runner role, which can start and read tasks but not edit templates. `copy-template <source> <name> <playbook>` and `add-schedule <template> '<cron>' <name>` create a template as a copy of an existing one and a schedule for it, which needs the Manager role while it runs; neither edits or deletes anything. `set-schedule <schedule> '<cron>'` replaces the cron expression of an existing schedule and changes nothing else, `delete-schedule <schedule>` removes a schedule and `delete-template <template>` a template that is no longer used, each showing it first (`--dry-run` only shows it). `environments` lists the names, never the values, of the variables and secrets of each variable group, and `delete-env-secret <environment> <name>` removes one secret that nothing uses any more, which needs the Manager role and leaves the rest of the group as it is.

The API token is read from a file that must not be readable by anyone else (`~/.config/semaphore/semaphore.token`) and is passed to `curl` through a configuration descriptor, so it never appears on a command line. Starting a task is limited to the template ids listed in `~/.config/semaphore/allowed-templates`, one per line, where a line with just `*` allows every template; with no such file nothing can be started, while reading stays available.

## Requirements

- SemaphoreUI, or plain `ansible-playbook`, with:
  - Collections: `ansible-galaxy collection install -r collections/requirements.yml`
  - Python packages: `pip install -r requirements.txt`
- A Proxmox API token with enough privileges to list VMs, create and delete snapshots, and migrate VMs.
- SSH access to all target hosts and to the Proxmox nodes, using a key stored in Semaphore's Key Store (never committed to this repository).
- `rclone` with a `pcloud:` remote on `pve1` (for `vm-backups.yml`) and on `nfs101` (for `pcloud-backups.yml` and `sync-paperless.yml`, which install the package: the doorbell's files, the Immich library and the documents). The remote holds a pCloud token, so its `rclone.conf` is copied by hand to `/root/.config/rclone/` (mode 0600) on each of them and never stored here.
- For `sync-paperless.yml`, on `nfs101`: the API token of a Paperless user without administrator or delete rights, in `/root/.config/paperless/token` (mode 0600). It is placed by hand and never stored here.

## Secrets

No credentials are stored in this repository. The Proxmox API token is read with `lookup('env', 'PROXMOX_TOKEN_SECRET')` in `inventory/proxmox.yml` and injected by Semaphore through a Variable Group. SSH keys live only in Semaphore's Key Store. `GITHUB_PUSH_TOKEN` (a GitHub personal access token with write access to this repository) is used by the playbooks that commit back to it, `check-image-updates.yml`, `update-semaphore-peer.yml`, `provision-vm.yml` and `decommission-vm.yml`, and is injected the same way.

`SMTP_USER`, `SMTP_PASSWORD` and `NOTIFY_EMAIL_TO` follow the same pattern and are required by every playbook's `send_report` role. `SMTP_PASSWORD` is an app password, not the account's real password, since `smtp_host` is `smtp.gmail.com`. Each Semaphore instance holds its own Variable Group, so a rotated password has to be updated on both.

`katello_promote` and `cv-retention.yml` pass `KATELLO_HAMMER_USERNAME` and `KATELLO_HAMMER_PASSWORD` explicitly on every `hammer` command (`no_log: true`, since the password would otherwise appear in the command's arguments in Semaphore's task log) rather than relying on the credentials in `lpkat101`'s hammer configuration file. Rotating the Foreman admin password is an update of that variable, with nothing to change on `lpkat101`. `katello_register_host` reuses the same two variables.

`PHPIPAM_API_USERNAME` and `PHPIPAM_API_PASSWORD` (the credentials for the `ansible` app's `User token` mode, see [Provisioning a new fleet VM](#provisioning-a-new-fleet-vm)) are read through `phpipam_api_username` and `phpipam_api_password`. `phpipam_next_ip` exchanges them for a session token once per run.

`PROXMOX_TOKEN_SECRET` is also exposed as `proxmox_token_secret` in `group_vars/all.yml`; the exporter of Proxmox on the cluster reads the same token from the Secret `pve-exporter-config`, made by hand, with no second token to create or rotate.
