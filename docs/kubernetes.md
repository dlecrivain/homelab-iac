# Kubernetes test cluster: operations guide

This guide is for whoever is at the keyboard, with or without an assistant. It says what runs, how the parts work
together, how to look at them, what to do when one breaks, how the system is updated, and gives exercises to practise
on. The cluster is a test one: the applications of the fleet still run on their Podman VMs.

Nothing here needs a login on a node. Talos has no SSH and no shell; everything goes through two APIs, from the laptop.

## 1. What is there

| Name | Role | Address | VM | Proxmox node |
|---|---|---|---|---|
| `talos-cp101` | control plane | 192.168.1.124 | 120 | pve1 |
| `talos-wk101` | worker | 192.168.1.125 | 121 | pve1 |
| `talos-cp102` | control plane | 192.168.1.127 | 122 | pve2 |
| `talos-wk102` | worker | 192.168.1.126 | 123 | pve2 |
| `talos-cp103` | control plane | 192.168.1.113 | 124 | pve3 |
| `talos-wk103` | worker | 192.168.1.118 | 125 | pve3 |
| `nfs101` | NFS server, Postgres next to it | 192.168.1.130 | 113 | pve2 |

Talos v1.14.5, Kubernetes v1.37.2. Pods get addresses in `10.244.0.0/16` (a /24 per node), Services in `10.96.0.0/12`.
`192.168.1.128` (`talos-lb101`) is the address of Traefik, `192.168.1.129` (`talos-lb102`) is kept spare and `192.168.1.114` (`talos-lb103`) is the DNS server of the LAN. All the
addresses are reserved in phpIPAM. The six Talos VMs form the Proxmox pool `kubernetes`, which the fleet playbooks leave
out. There is one control plane and one worker on each Proxmox node, numbered after it (101 on pve1, 102 on pve2, 103 on
pve3). When a Proxmox node is updated or lost, its two Talos VMs are stopped with it, not migrated: etcd keeps its quorum
with the two other control planes, the API keeps answering, and the pods of the lost worker start again on the two others,
which hold them with room to spare. Nothing is stopped on a schedule any more.

| Component | What it does | Where |
|---|---|---|
| Talos Linux | The operating system: nothing but what Kubernetes needs, read-only, configured through an API | every node |
| etcd | The database of the cluster: every object Kubernetes knows about | control planes |
| kube-apiserver | The door: every command and every component goes through it | control planes |
| kube-scheduler, kube-controller-manager | Choose the node of a new pod; keep what runs equal to what was asked | control planes |
| kubelet, containerd | Start and watch the pods of a node | every node |
| Cilium | Pod network (VXLAN tunnel between nodes), replaces kube-proxy, gives and announces the addresses of LoadBalancer services | every node, operator in `kube-system` |
| CoreDNS | Names inside the cluster (`service.namespace.svc`) | `kube-system` |
| Traefik | Receives the HTTPS requests on 192.168.1.128 and sends each to the right service by name | `traefik`, 2 pods |
| cert-manager, OVH webhook | Obtain and renew Let's Encrypt certificates, proving the name through a DNS record at OVH | `cert-manager` |
| Argo CD | Compares `k8s/` of this repository with the cluster and corrects the difference | `argocd` |
| `edge` | Routes of Traefik to the web interfaces of the VMs (Proxmox, Semaphore): a Service without pods points at each VM | `edge` |
| metrics-server, kubelet-serving-cert-approver | Consumption of nodes and pods; approves the certificates of the kubelets so that logs and metrics can be fetched securely | `kube-system`, `kubelet-serving-cert-approver` |
| AdGuard Home | The DNS server and filter of the devices of the LAN, at 192.168.1.114 through a LoadBalancer Service of Cilium; its files are on `nfs101` | `adguard` |
| Semaphore (`semaphore-bck`) | The second Semaphore, which updates `semaphore103` every week; its database is on `nfs101`, `semaphore103` stays on a VM outside the cluster | `semaphore-bck` |
| Paperless-ngx | The document archive, moved here as a pilot: files on `nfs101`, database in its Postgres, Valkey for the queue | `paperless` |
| Immich | The photo and video library: server, machine learning and Valkey as pods, photos on `nfs101` and database in its Postgres | `immich` |
| phpIPAM | The address manager the playbooks call as an API, and its subnet scan; database in the MariaDB of `nfs101` | `phpipam` |
| Home Assistant | The home automation, with its Matter server, in one pod on the host network of a worker; configuration on `nfs101`, history in its Postgres | `homeassistant` |
| Prometheus, Alertmanager | Scrape the exporters and the probes, raise and mail the alerts; the database is on `nfs101`, with a snapshot every night | `monitoring` |
| Grafana | The dashboards of Prometheus, provisioned from `k8s/grafana`; its state is in a Postgres of `nfs101` | `monitoring` |
| `nfs101` | Files of the applications over NFSv4, and their Postgres and MariaDB databases on its own disk | VM outside the cluster |

## 2. How you operate it

Three clients run on the laptop. None logs in to a node.

- **`talosctl`** talks to the Talos API of a node (port 50000) with the certificate in `talosconfig`. It does what SSH
  would on another system: logs, services, reboot, etcd, updates.
- **`kubectl`** talks to the Kubernetes API (port 6443 of a control plane) with the credentials in `kubeconfig`.
  Freelens is the same thing in a window, with a terminal built in.
- **Argo CD** has an interface at https://argocd.k8s.lecrivaindaniel.fr (port 30081 of any node over plain HTTP if Traefik
  is down). The `admin` password is in `~/.config/talos/maquette/argocd-admin.password`.

The files, all on the laptop and none in git:

| File | What it is |
|---|---|
| `~/.config/talos/maquette/secrets.yaml` | The keys and certificates authorities of the whole cluster. Without it a node cannot be administered |
| `~/.config/talos/maquette/talosconfig` | Credentials and endpoints of `talosctl` |
| `~/.config/talos/maquette/controlplane.yaml`, `worker.yaml`, `node-*.yaml`, `patch-*.yaml` | Machine configuration of the nodes and the patches that made them |
| `~/.kube/maquette.yaml` | Credentials of `kubectl` and Freelens, a copy of `kubeconfig` of the same folder |
| `~/.config/talos/maquette/env.sh` | `. ~/.config/talos/maquette/env.sh` points both clients at the maquette |

Back this folder up somewhere safe and encrypted, outside git. The clients are in `~/.local/bin`; keep their versions in
line with the cluster. If a node does not answer its API, its console in Proxmox shows the Talos dashboard (addresses,
state of the services, recent logs); it is not a shell. A node can be restarted from Proxmox.

## 3. How the parts work together

**A page is requested** (`https://argocd.k8s.lecrivaindaniel.fr`):

1. The browser resolves the name. The wildcard record `*` at OVH answers 192.168.1.128. AdGuard, which runs on the cluster, only filters,
   it holds no record, but it caches answers.
2. Cilium makes one worker answer ARP for 192.168.1.128. If that worker goes, another takes over within seconds.
3. The request reaches a Traefik pod, which presents the wildcard certificate and reads the name (`Ingress` objects).
4. Traefik sends it to the Service of the application, Cilium picks a pod and carries the packets, through the tunnel when
   the pod is on another node.

**A certificate is obtained** (`Certificate` `wildcard-k8s`, for `*.lecrivaindaniel.fr` and `*.k8s.lecrivaindaniel.fr`; no application has a certificate of its own):

1. cert-manager asks Let's Encrypt, which wants a proof of control of the name.
2. The OVH webhook adds the TXT records `_acme-challenge` and `_acme-challenge.k8s` in the zone, using the `ovh-credentials`
   Secret, and removes them afterwards.
3. The certificate is stored in the Secret `wildcard-k8s-tls`, which Traefik uses by default. It lasts 90 days and is renewed
   before it ends.

**A change is made** (a file of `k8s/` is edited):

1. The change is committed and pushed to `main`.
2. Argo CD notices it (every two minutes, or at once with a refresh), renders the chart or the manifests and applies them.
3. Kubernetes replaces the pods. Cilium is the exception: it is synchronised by hand after reading its difference.

**A node starts**: Proxmox starts the VM, Talos boots from its disk, brings the network up with its fixed address, starts
containerd and kubelet, and the node registers with the API. Cilium starts on it, and only then are the pods scheduled.

## 4. What happens when a part is down

| Down | Effect | Recover |
|---|---|---|
| One worker | Its pods are rescheduled on the two others, after about five minutes when it is lost, at once when it is stopped by Talos (it drains itself). Traefik keeps its second pod | Start the VM |
| One control plane | With three etcd members, the quorum holds: `kubectl` and Argo CD keep answering | Start it again |
| Two control planes | The quorum is lost: `kubectl` and Argo CD stop answering and nothing can be changed. Running pods keep running | Start one of them |
| One Proxmox node (update or loss) | Its control plane and its worker go with it, the cluster keeps working on the two others. `nfs101` is the single point: the files and the databases of Paperless, phpIPAM and Home Assistant wait for it if its host is the one that stops | Wait for the Proxmox node, or for `nfs101` to start on another one |
| Argo CD | Applications keep running, nothing is corrected or updated | Start its pods |
| Traefik | The pages are not served. Argo CD stays reachable on port 30081 | Look at the pods and the service |
| cert-manager or the OVH webhook | Existing certificates keep working until they end | Look at the pods and the Certificate |
| `nfs101` | Pods that use it wait for it (hard NFS mount) | Start the VM, check `exportfs -v` |
| phpIPAM or its MariaDB | The playbooks that reserve, look up or free an address fail (`provision-vm.yml`, `decommission-vm.yml`, `talos-create-vm.yml`, `phpipam-reserve-address.yml`); nothing else depends on it. | Start the pods or the VM; a dump of its database is in `/var/backups/mariadb` on `nfs101` |
| The OVH zone or Let's Encrypt | Only new certificates fail | Wait, or look at the challenge |
| The laptop | Nothing stops. You cannot operate the cluster until the files of section 2 are available | Restore them from the backup |

Prometheus mails you for the failures that a user would notice: Paperless, phpIPAM, Home Assistant or Argo CD not answering
over HTTPS for five minutes, the Postgres, MariaDB or NFS port of `nfs101` closed, a certificate with less than 14 days left.
It runs on the cluster itself, so a run of Semaphore outside it (`watch-monitoring.yml`, every 20 minutes) mails when Prometheus or
Alertmanager does not answer.

## 5. First reflexes

Both clients are used from a terminal of the laptop after `. ~/.config/talos/maquette/env.sh`.

```sh
kubectl get nodes -o wide                      # are all four Ready?
kubectl get pods -A | grep -v -E 'Running|Completed'   # what is not running
kubectl top nodes                              # memory and CPU
kubectl get events -A --sort-by=.lastTimestamp | tail -20
kubectl -n argocd get applications             # is Argo CD happy?
kubectl describe pod <name> -n <namespace>     # why a pod does not start (look at Events)
kubectl logs <pod> -n <namespace> --tail=50    # what the application says

talosctl health                                # the cluster as Talos sees it
talosctl service                               # services of the default node
talosctl -n 192.168.1.126 dmesg | tail -30     # what a node says
talosctl -n 192.168.1.124 logs kubelet         # logs of one service
talosctl -n 192.168.1.124,192.168.1.127,192.168.1.113 etcd status
talosctl get members                           # the nodes of the cluster
```

`-n` chooses the node (a comma separated list also works). The default is `talos-cp101`.

## 6. Routine operations

- **Stop and start the nodes by hand**: Semaphore templates "Kubernetes - Stop" (45) and "Kubernetes - Start" (46), for
  a change that needs all the memory of the Proxmox nodes. They are not scheduled.
- **Add or rebuild a node**: run "Talos - Create VM" (47) with `talos_vm_name=<name>` from `talos_nodes` in
  `group_vars/all.yml`. The node boots from the ISO on `pve1` and reports the address it got from DHCP. Write its
  configuration with `talosctl machineconfig patch` from `controlplane.yaml` or `worker.yaml` and a patch with its
  hostname and its reserved address (`LinkConfig` named `ens18`), send it with
  `talosctl apply-config --insecure -n <dhcp address> -f <file>`, wait until it has installed itself, then run "Talos -
  Place VM" (48). The reserved address is reused when a node is rebuilt. If Kubernetes was updated since the configuration
  files were made, generate them again first (section 7).
- **Change a version or a setting**: edit the Application in `k8s/apps/` or its file in `k8s/values/`, commit, push. Renovate
  does it for the versions (section 7).
- **Change Cilium**: after the push, read the difference (`argocd app diff cilium` or the interface) and synchronise by
  hand. Restart the operator and the agents if the ConfigMap changed.
- **Remove a component**: delete its files from `k8s/`. Argo CD removes the Application but not what it deployed, because
  a cascade would take Cilium down too: delete the namespace and the cluster-wide objects by hand.
- **Secrets**: the Secret `ovh-credentials` of `cert-manager` was made by hand from OVH API credentials and is not in
  git. If the cluster is rebuilt, make it again with the three keys `applicationKey`, `applicationSecret` and
  `applicationConsumerKey`. `paperless-secrets` of `paperless` (`PAPERLESS_DBPASS`, `PAPERLESS_SECRET_KEY`) is made the same
  way, from `/var/lib/postgres/paperless.env` on `nfs101` and the key Paperless already used.
- **Certificates** renew themselves. Check with `kubectl -n traefik get certificate`.

## 7. Updates and changing the system

**There is no package manager.** The root file system of Talos is read-only and there is no `dnf`, Katello or OpenSCAP on
a node. The system is replaced as a whole image, and anything else runs as a pod.

**Updating Talos.** A node is updated by pointing it at an installer image:

```sh
talosctl upgrade -n <node> --image factory.talos.dev/metal-installer/<schematic id>:<version> --wait
```

- Since Talos 1.14 the installer images come from the Image Factory, under `metal-installer`. The machine configuration of
  this cluster carries the older `installer` path, which worked for the installation: check which one is accepted, and
  change `UnattendedInstallConfig` of the nodes accordingly, before the first update.
- The node cordons itself, drains its pods, stops, writes the new image next to the running one, reboots once with it
  (`kexec`), checks itself, makes the change permanent, rejoins the cluster and uncordons itself.
- If the image cannot be fetched, the update is cancelled. If the new system does not start, the node reboots on the previous
  image by itself. `talosctl rollback` goes back by hand.
- Talos refuses to update a control plane if that would break the etcd quorum, and updates only one at a time. With three
  control planes, taking one down keeps the quorum, so one at a time is allowed.
- Use a `talosctl` of the same version as the cluster. Update one node, check, then the next. `talosctl dmesg -f` or
  `--wait` shows progress.
- `talos-updates.yml` does this for all the nodes, see **Updates from Semaphore** below. `talos_version` and the schematic are in
  `group_vars/all.yml`.

**Updating what runs on the cluster.** Renovate reads `renovate.json` and proposes the new versions of the images in `k8s/` and
of the charts and tags of the Applications, between 03:00 and 04:00, three days after a release. Minor and patch versions of
Paperless-ngx and its Valkey, and of the charts, are merged by themselves, and Argo CD, which polls git, deploys
the merge. Cilium and the major versions stay as pull requests. Before Paperless is replaced, a hook of Argo CD dumps its
database (`k8s/paperless/presync-dump.yaml`, kept two weeks in `/srv/nfs/paperless/backups`), and a failed dump stops the
deployment. To go back, revert the merge in git; if the new version migrated the database, restore the dump taken before it
(`pg_restore` into the Postgres of `nfs101`). The Podman services keep `check-image-updates.yml`.

**Mail about updates.** Two mails come for a version that is merged by itself. GitHub sends the first when Renovate opens the
pull request, which is the moment the new version is known (the repository has to be watched for pull requests in the
notification settings of GitHub). Argo CD sends the second, from the same mailbox as the alerts, once it has synchronised the
application and the application is healthy: it says which version replaces which (the title of the Renovate commit for an image,
the chart versions for a chart). It also mails when a
synchronisation fails and when an application is degraded. The `root` application, which only lists the others, sends no
mail for an update, since each of them sends its own. Changing the condition of a trigger (`when` or `oncePer`) makes every
application send its mail once more, since Argo CD records what it has sent under a hash of that condition; changing the text
of a template does not. The settings are in `k8s/bootstrap/argocd-values.yaml` and are
applied with `helm upgrade`. The login of the mailbox is the Secret `argocd-notifications-secret` (`email-username`,
`email-password`) and the recipient is the annotations `notifications.argoproj.io/subscribe.on-deployed.email`,
`...on-sync-failed.email` and `...on-health-degraded.email` of the `default` AppProject, which every application belongs to.
Both are made by hand because the repository is public.

**Updating Kubernetes.** The Kubernetes version is separate from the one of Talos, which an update of Talos does not
change. The versions a Talos release supports are in its support matrix. One command updates the whole cluster:

```sh
talosctl --nodes <a control plane> upgrade-k8s --to <version> --dry-run
talosctl --nodes <a control plane> upgrade-k8s --to <version>
```

- It pulls the new images on the nodes, then patches the machine configuration of every control plane with the new
  versions of the API server, the controller manager and the scheduler, which Talos turns into new static pods, then
  updates the kubelet of every node and waits for it, then applies the bootstrap manifests again and prunes what is no
  longer needed. Talos does not disrupt anything by itself, but restarting a kubelet may restart workloads. If the command
  fails it can be run again.
- The dry run lists the nodes, the changes and the APIs about to be deprecated, and changes nothing.
- **The live machine configuration of the nodes changes.** The full files kept in `~/.config/talos/maquette`
  (`controlplane.yaml`, `worker.yaml`, `node-*.yaml`) still name the old versions after that: do not apply them again to a
  running node, and generate them again (`talosctl gen config ... --kubernetes-version <version>`, with the same
  `secrets.yaml` and patches) before adding or rebuilding a node.
- This cluster runs no kube-proxy, since Cilium replaces it, while the command has a step for it. What that step does here
  is to be seen on the first real run.

**Adding something to the system.**

- **A system extension** (a guest agent, drivers, tools) is added to the image. List it in a schematic and upload that to the
  Image Factory (`https://factory.talos.dev/schematics`, which returns an ID), then update every node to the installer image of
  the new ID. The ID in use is shown by `talosctl get extensions`. The ISO and the installer of the nodes should use the same
  schematic. The current one only has `siderolabs/qemu-guest-agent`. Not every extension exists for every Talos version.
- **Kernel arguments** go in the same schematic (`extraKernelArgs`).
- **A kernel module** that the kernel or an extension provides is loaded with a `KernelModuleConfig` document in the machine
  configuration (`name: <module>`); **sysctls** with a `SysctlConfig` document (`params:`). Both are sent with
  `talosctl apply-config` or `talosctl patch machineconfig`.
- **Anything else** (an agent, a service) runs as a pod, usually a DaemonSet when it must be on every node.


**Updates from Semaphore.** `full-updates.yml` ends with two stages, `talos-updates.yml` then `kubernetes-updates.yml`, each of
which is also a template of `semaphore103` to run alone. They bring the nodes to `talos_version` and the cluster to
`kubernetes_version`, two variables of `group_vars/all.yml` that Renovate proposes as pull requests, never merged by itself:
the run of the Friday after the merge applies the new version, and the mail of the run says, for each node and each component of
Kubernetes, the version it had and the one it has now (or that there was nothing to do).

- **Where and with what.** They run on `semaphore103`, which is outside the cluster: a task that drains a node must not run in a
  pod of the cluster, and the Semaphore that starts the cluster again cannot depend on it. `talosctl` is the one of its official
  image, run by podman, with a client configuration that is only on that VM, in `/etc/talos/talosconfig` (owner root, mode 0600),
  never in the repository. It is made once, from a machine that holds the admin configuration, and lasts five years:
  `talosctl config new semaphore.talosconfig --roles os:admin --crt-ttl 43800h`, then copied to the VM. Without that file both
  stages are skipped and say so in the report.
- **What they do.** `talos-updates.yml` reads the version of each node and updates the ones that differ, the control planes first
  and one node at a time, with `talosctl upgrade`: the node cordons itself, drains its pods, reboots into the new image and
  uncordons itself. The health of the cluster is checked before each node and after it, and at the first problem nothing more is
  touched. A node that is already on the target, or newer, is left alone. `kubernetes-updates.yml` reads the version of the
  components of the control planes and of the kubelets, asks Talos for its plan (`--dry-run`) and then runs `upgrade-k8s`.
  A stage that finds nothing to do changes nothing and says so.
- **Applications with one replica** are cut for a minute or two while their pod starts on another node, once per node.
- **By hand.** `-e talos_updates_check_only=true` (and `kubernetes_updates_check_only=true`) only reads and reports.
  `-e talos_updates_nodes=talos-wk103` restricts the update to some nodes: use it for the first real update, on one worker, and
  look at the mail before letting the whole run do the others.
- **Only `semaphore103`.** The Semaphore of the cluster must never run them, since its own pod would be drained with the node.

## 8. Backups and recovery

- Back up the folder of section 2. Without `secrets.yaml` the cluster cannot be administered.
- Take a snapshot of etcd before a risky change: `talosctl -n 192.168.1.124 etcd snapshot <file>`, and keep it off the cluster.
  Restoring one goes through `talosctl bootstrap --recover-from=<file>` on an empty control plane, see the Talos
  documentation. It has not been tried here.
- What can be rebuilt: the VMs (templates 47 and 48 and the configuration of the folder), the applications (everything in
  `k8s/`), the certificates. What cannot: the Secrets made by hand and the data of the applications, which live on `nfs101`.
- `nfs101` is saved by `vzdump` for its system disk only. Its data disk has `backup=0`: the data is protected by the copies
  to the cloud storage and by the Postgres dumps in `/var/backups/postgres` on its system disk.

## 9. Exercises

Do them on this cluster, in order. Each one says what to do, what you should see, and how to undo it. Levels 0 and 1 cannot
hurt anything; from level 2 on, the cluster has fewer parts for a while. Do not do two exercises at the same time.

**Level 0: look, do not touch**

1. Run the commands of section 5. Find the four nodes, the two Traefik pods, the five Argo CD pods and where each runs
   (`kubectl get pods -A -o wide`). Which pods are on the control planes? (Only the system ones.)
2. Read some logs: `kubectl -n traefik logs deploy/traefik --tail=20`, `talosctl service kubelet`, `talosctl -n 192.168.1.125
   dmesg | tail`. Open the console of a Talos VM in Proxmox and find the same information on the dashboard.
3. Follow a request: `curl -v https://argocd.k8s.lecrivaindaniel.fr`. Find the address 192.168.1.128 in
   `kubectl -n traefik get svc traefik`, the host in `kubectl get ingress -A`, and the certificate dates in the output.

**Level 1: workloads**

1. Create, scale and break things:
   `kubectl create namespace practice`,
   `kubectl -n practice create deployment web --image=nginx:1.27 --replicas=2`, then `kubectl -n practice get pods -o wide`.
   Scale to 4 (`kubectl -n practice scale deployment web --replicas=4`). Delete one pod and watch another appear. Expect
   the pods to be spread over the three workers.
2. A bad image: `kubectl -n practice set image deployment/web nginx=nginx:does-not-exist`. Read why in
   `kubectl -n practice describe pod` (`ImagePullBackOff`), then `kubectl -n practice rollout undo deployment/web`.
3. Publish it: write a Service on port 80 and an Ingress with `ingressClassName: traefik`, the annotations
   `traefik.ingress.kubernetes.io/router.entrypoints: websecure` and `.../router.tls: "true"`, and the host
   `practice.k8s.lecrivaindaniel.fr` (the wildcard certificate covers it). Open it in the browser: the certificate must be
   trusted. Copy `k8s/ingress/argocd.yaml` as a model.
4. Storage: a pod with a volume of type `nfs` (server `192.168.1.130`, path `/srv/nfs/paperless`) writes a file; a second pod
   scheduled on the other worker (`nodeName` or anti-affinity) reads it. Remove the file afterwards.
5. Argo CD heals: `kubectl -n traefik scale deployment traefik --replicas=1`, then watch
   `kubectl -n traefik get pods -w`: within about two minutes Argo CD puts the second one back.
6. A change through git: add a small `Application` in `k8s/apps/` that deploys a manifest from a folder of `k8s/`,
   commit and push, watch it appear, then delete the files and notice that the deployed objects stay. Delete them by hand.
7. Clean up: `kubectl delete namespace practice`.

**Level 2: nodes**

1. Drain and return a worker: `kubectl drain talos-wk102 --ignore-daemonsets --delete-emptydir-data`, watch the pods move to
   the other worker, then `kubectl uncordon talos-wk102`.
2. Reboot a worker: `talosctl -n 192.168.1.126 reboot`. The node goes `NotReady`, then `Ready`; time it.
3. A hard failure: stop the VM of `talos-wk102` in Proxmox. The node becomes `NotReady` in under a minute, its pods are
   evicted after about five minutes. Start the VM again.

**Level 3: control plane (three members, one fault tolerated)**

1. Look: `talosctl -n 192.168.1.124,192.168.1.127,192.168.1.113 etcd status` and `talosctl etcd members`. All three must be in step.
2. Take a snapshot: `talosctl -n 192.168.1.124 etcd snapshot ~/etcd-maquette.snapshot`, and keep it.
3. Lose one member: `talosctl -n 192.168.1.127 reboot`. `kubectl get nodes` keeps answering during the reboot, since two of
   three members are enough. Explain why, then try the same on two at once only on a day when you can wait for the API to
   return: the quorum is lost, `kubectl` stops answering, but `curl https://argocd.k8s.lecrivaindaniel.fr` still does, since the
   pods already running keep serving.

**Level 4: certificates**

1. Read the state: `kubectl -n traefik get certificate`, `kubectl -n traefik describe certificate wildcard-k8s`, and the dates
   with `openssl s_client -connect 192.168.1.128:443 -servername x.k8s.lecrivaindaniel.fr </dev/null | openssl x509 -noout -dates`.
2. Watch the whole process without using the production limits of Let's Encrypt: create a `Certificate` for
   `test.k8s.lecrivaindaniel.fr` with the `le-staging` issuer in the `practice` namespace, and follow
   `kubectl -n practice get certificate,certificaterequest,order,challenge -w`. It should be ready in about two minutes.
   Delete it afterwards. Never delete `wildcard-k8s-tls` to try a renewal: it would use a production issuance.

**Level 5: rebuild**

1. Delete the VM of `talos-wk102` in Proxmox, then rebuild it: template 47 with `talos_vm_name=talos-wk102`, the patch and
   `talosctl apply-config` of section 6, template 48. Check that the node is `Ready` and that Argo CD is healthy. The reserved
   address is reused.

**Level 6: update a node's system (a worker, today)**

1. Read the schematic ID with `talosctl -n 192.168.1.126 get extensions`.
2. Run `talosctl -n 192.168.1.126 upgrade --image factory.talos.dev/metal-installer/<ID>:v1.14.5 --wait` (the same version: it
   exercises the mechanism). Watch the node cordon, drain, reboot and uncordon. Check the path of the installer image first,
   see section 7.

**Level 7: update Kubernetes by a patch version**

1. Take an etcd snapshot (level 3), check that the nodes and the pods are healthy, and avoid the stop windows of section 1.
2. Run the dry run of section 7 and read it: which components, on which nodes, from which version to which.
3. Run the real command and follow `kubectl get nodes -w` in another terminal. Expect the API to blink on each control plane
   and the nodes to show the new version one after the other.
4. Check `kubectl version`, `kubectl get pods -A`, `talosctl health` and `kubectl -n argocd get applications`. Update the
   `kubectl` client to the same version, and generate the configuration files again as section 7 says.

## 10. Not tried yet

The loss of a node and of the etcd quorum, an update of Talos or of Kubernetes, the restore of an etcd snapshot, a renewal of
the wildcard certificate and the behaviour of Argo CD when git is unreachable.
