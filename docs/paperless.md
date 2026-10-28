# paperless101: document archive

Status: deployed. The cloud storage folder is the master copy and `sync-paperless.yml` keeps Paperless in step with it every night.

## Goal

A VM (`paperless101`) running paperless-ngx, the maintained successor of paperless-ng, that indexes the scanned documents kept in one tree of the owner's cloud storage. Each document gets an OCR text layer, a date, a correspondent, a type and tags, so it can be found by content instead of by folder. The assistant (see [claude101](claude101.md)) or this repository's tooling can classify documents through paperless-ngx's REST API, but only when the owner asks.

## Scope

Only the scanned-documents tree of the cloud storage (`Documents/Documents_numerises` and its subfolders) is imported, whole. Nothing else of the storage is read.

The tree today is small: 631 objects and about 490 MiB, 603 of them PDF, a handful of images, two word-processor files, one spreadsheet, and sync or system files that are skipped. It goes three folder levels deep.

## Stack

All containers run rootless as the connecting user, as Quadlet units, like the other container hosts of this repository, with pinned versions in `group_vars/all.yml` (`paperless_version`, plus the PostgreSQL and Valkey versions) that `check-image-updates.yml` can bump.

- `paperless-ngx` (web, task queue and OCR in one container, port 8000);
- PostgreSQL, as the database;
- Valkey, as the broker.

Gotenberg and Tika, which are only needed to import office documents, are left out: the few office files are exported to PDF by the owner or skipped.

Sizing: 2 vCPU and 4 GB of RAM (OCR is the heavy part), `/var` as the template provides it, and `/home` grown with `lvm_grow` for the rootless images. Data lives under `/var/lib/paperless/{data,media,consume,export,pgdata}`.

Secrets (database password, application secret key) are in 0600 environment files that the repository does not keep, like Immich's. The administrator account is created by hand with the application's own command. The OCR languages are French plus whatever a sample of the corpus shows.

## Network and access

BunkerWeb publishes `paperless.<domain>` with the existing wildcard certificate, restricted to the LAN and the VPN like the other services. Nothing is exposed to the Internet. A monitoring target and the usual backups apply, as for every VM of the template: weekly full backup of the VM to the cloud storage, plus paperless-ngx's own `document_exporter` if a portable export is wanted.

## Synchronisation

Documents are filed, renamed and deleted only in the cloud storage folder, so there is one place to keep tidy. Paperless is an index of that folder and can be rebuilt from it. Paperless always keeps its own copy of a file and cannot index a folder in place, so a job keeps the two in step.

The cloud storage remote is configured on `smb101`, which copies to it, so `sync-paperless.yml` runs `paperless-sync` there and that credential is not copied to more hosts. The script compares the folder with Paperless by SHA-256, the checksum Paperless stores for each original, and keeps a state file of which document each file became:

1. A new file is uploaded with its tags (one per folder, the numbering stripped), its title and date from the name, and the correspondent and type the rules give it.
2. A moved or renamed file is recognised by its checksum and its document is re-tagged and re-classified. Renaming a file is how its type is changed.
3. A deleted or replaced file leaves its document in place, tagged `a-purger`: the API user cannot delete.
4. A new document that no rule and no matching of Paperless could give a type is tagged `a-classer`, and an email lists those documents with the number that carry the tag.
5. Files Paperless cannot import are listed as skipped in the run's output.

An API token for a dedicated, non-administrator user without delete rights is created by the owner and placed on `smb101` by hand, in a 0600 file. paperless-ngx refuses a duplicate by checksum.

## Classification

Correspondents and types come from rules kept next to the documents in the cloud storage, not in this repository: they match the file name and the folder tags. Anything they do not cover is left to paperless-ngx's own matching, which learns from the documents that already have a type, and then to the owner through the `a-classer` tag.

Handwriting, such as an approval written on a quote, and poor scans are not read reliably by the OCR. Sensitive documents are read through the assistant only if the owner has not excluded them, and the assistant is not needed for the nightly synchronisation, which reads no content.

## Operations

- `configure-paperless.yml` and a `paperless_configure` role deploy the units, like `configure-immich.yml`;
- the VM is provisioned with `provision-vm.yml`, which also registers it with Katello, node_exporter and Prometheus;
- `vm-updates.yml` patches the host and `container-updates.yml` the containers, each after a snapshot;
- a health check polls the login page.

## Decisions

- The VM is named `paperless101`.
- No subfolder of the tree is excluded.
- The cloud storage is the only place to file documents: Paperless never writes to it and nothing is deleted from Paperless, only tagged.
- OCR languages: French, and English for the rare documents in that language.
- Office files and archives are not imported; the owner exports to PDF the ones wanted in the archive.
- The synchronisation runs every night, and its token is the one of the non-administrator user the assistant also uses.
