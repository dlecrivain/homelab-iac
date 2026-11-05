# Paperless-ngx: document archive

Status: deployed on the Kubernetes test cluster. The cloud storage folder is the master copy and `sync-paperless.yml` keeps Paperless in step with it every night.

## Goal

An instance of paperless-ngx, the maintained successor of paperless-ng, that indexes the scanned documents kept in one tree of the owner's cloud storage. Each document gets an OCR text layer, a date, a correspondent, a type and tags, so it can be found by content instead of by folder. The assistant (see [claude101](claude101.md)) or this repository's tooling can classify documents through paperless-ngx's REST API, but only when the owner asks.

## Scope

Only the scanned-documents tree of the cloud storage (`Documents/Documents_numerises` and its subfolders) is imported, whole. Nothing else of the storage is read.

The tree today is small: 631 objects and about 490 MiB, 603 of them PDF, a handful of images, two word-processor files, one spreadsheet, and sync or system files that are skipped. It goes three folder levels deep.

## Stack

Paperless-ngx runs on the Kubernetes test cluster, see [kubernetes.md](kubernetes.md), from the manifests of `k8s/paperless/` that Argo CD applies. Their versions are pinned in the manifests.

- a pod with `paperless-ngx` (web, task queue and OCR in one container, port 8000): one replica that is replaced as a whole on an update, since two instances must not write the same files and database;
- a pod with Valkey, the broker, which holds nothing that has to survive a restart;
- PostgreSQL, outside the cluster: a rootless container on `nfs101`, on its local disk and never on NFS, with a nightly dump (`configure-postgres.yml`).

Gotenberg and Tika, which are only needed to import office documents, are left out: the few office files are exported to PDF by the owner or skipped.

Sizing: the pod asks for 1 GiB and may use 2.5 GiB (OCR is the heavy part). The files live on the `nfs101` share `/srv/nfs/paperless/{data,media,export}`, mounted over NFSv4.2.

The database password and the application secret key are in the Secret `paperless-secrets`, made by hand because the repository is public. The administrator account was created by hand with the application's own command. The OCR languages are French plus whatever a sample of the corpus shows.

## Network and access

Traefik publishes `paperless.<domain>` under the wildcard certificate of the zone, which cert-manager obtains through the OVH DNS-01 challenge, and the wildcard record at OVH leads to Traefik. Nothing is exposed to the Internet. The files and the database are on `nfs101`, which the VM backups cover for its system disk (the database dump) and the cloud copies for the rest, since the document folder is the master copy anyway.

## Synchronisation

Documents are filed, renamed and deleted only in the cloud storage folder, so there is one place to keep tidy. Paperless is an index of that folder and can be rebuilt from it. Paperless always keeps its own copy of a file and cannot index a folder in place, so a job keeps the two in step.

The cloud storage remote is configured on `nfs101`, which copies to it, so `sync-paperless.yml` runs `paperless-sync` there and that credential is not copied to more hosts. The script compares the folder with Paperless by SHA-256, the checksum Paperless stores for each original, and keeps a state file of which document each file became:

1. A new file is uploaded with its tags (one per folder, the numbering stripped), its title and date from the name, and the correspondent and type the rules give it.
2. A moved or renamed file is recognised by its checksum and its document is re-tagged and re-classified. Renaming a file is how its type is changed.
3. A deleted or replaced file leaves its document in place, tagged `a-purger`: the API user cannot delete.
4. A new document that no rule and no matching of Paperless could give a type is tagged `a-classer`, and an email lists those documents with the number that carry the tag.
5. Files Paperless cannot import are listed as skipped in the run's output.

An API token for a dedicated, non-administrator user without delete rights is created by the owner and placed on `nfs101` by hand, in a 0600 file. paperless-ngx refuses a duplicate by checksum.

## Classification

Correspondents and types come from rules kept next to the documents in the cloud storage, not in this repository: they match the file name and the folder tags. Anything they do not cover is left to paperless-ngx's own matching, which learns from the documents that already have a type, and then to the owner through the `a-classer` tag.

Handwriting, such as an approval written on a quote, and poor scans are not read reliably by the OCR. Sensitive documents are read through the assistant only if the owner has not excluded them, and the assistant is not needed for the nightly synchronisation, which reads no content.

## Operations

- Argo CD applies `k8s/paperless/`; the application is synchronised by hand. A new version is a change of the image tag in `server.yaml`, and its first start runs the migrations of the database.
- The address the synchronisation talks to is `paperless_sync_url`.
- **Moving Postgres to a new major version.** It is a dump and a restore, done by `upgrade-postgres.yml` (`-e pg_name=paperless`) once the new `major` and `postgres_image` are pushed (`host_vars/nfs101.yml`, `group_vars/all.yml`). The client of the dump job in `k8s/paperless/presync-dump.yaml` moves first, since a `pg_dump` refuses a server newer than itself. Then, with the application stopped so that nothing is written between the dump and the switch: suspend the automatic synchronisation of `root` and `paperless` (their `syncPolicy.automated` is set back by applying `k8s/bootstrap/root-application.yaml`), scale `deploy/paperless` to 0, run the playbook, check the page, and apply `root-application.yaml`, which also sets the replicas back through the self-heal. The playbook stops when the number of rows of the tables differs after the restore. The password in the Secret `paperless-secrets` stays valid, since the new cluster is created from the same environment file. The files of the previous version stay in `/var/lib/postgres/paperless/data` on `nfs101` as the way back: put the previous `major` and image back, run `configure-postgres.yml`, and the previous data is served again as it was at the dump. Delete that directory (`podman unshare rm -rf`) after a week.
- **Recovery.** The database is dumped every night on `nfs101` (seven days) and by Argo CD before each synchronisation (two weeks, on the share), and the documents are in the cloud storage folder, so after a restore the next synchronisation fills the difference.

## Decisions

- It started as a VM, `paperless101`, with Podman Quadlet units. It was moved to the Kubernetes test cluster once its data had been copied and checked, kept powered off for a while as the way back, and then decommissioned with `decommission-vm.yml`.
- No subfolder of the tree is excluded.
- The cloud storage is the only place to file documents: Paperless never writes to it and nothing is deleted from Paperless, only tagged.
- OCR languages: French, and English for the rare documents in that language.
- Office files and archives are not imported; the owner exports to PDF the ones wanted in the archive.
- The synchronisation runs every night, and its token is the one of the non-administrator user the assistant also uses.
