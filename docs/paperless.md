# paperless101: document archive

Status: validated by the owner, not yet deployed.

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

## Import

The cloud storage remote is configured on a single host (`immich102`), so the import runs from there and never copies that credential elsewhere.

1. A read-only listing and copy of the tree to a staging directory, excluding the owner's exclusions and the unsupported or system files.
2. A script posts each file to `/api/documents/post_document/` with its title, creation date, tags and correspondent when they can be derived from the path and from the `YYYY-MM_Type_Description` file names.
3. Everything is logged (source path, document, checksum) so an import can be reviewed or undone. paperless-ngx refuses a duplicate by checksum.

An API token for a dedicated, non-administrator user is created by the owner and placed on the import host by hand, in a 0600 file. It is deleted after the import.

## Classification

The tags come from the owner's folder structure, with the numbering stripped, and from what the OCR text shows. Order of work:

1. A pilot import of one small subfolder, to check the OCR quality and the way dates and correspondents are read.
2. A proposed taxonomy (tags, correspondents, document types, storage paths) written down and approved by the owner before it is applied.
3. Import by batches. After each batch the assistant reads the OCR text through the API, proposes corrections and applies them in bulk once approved.
4. Matching rules (keywords or regular expressions) for the correspondents and types that repeat, then paperless-ngx's classifier learns from the labelled documents so new documents are filed automatically.

Handwriting, such as an approval written on a quote, and poor scans are not read reliably by the OCR; for those the assistant looks at the document itself. Sensitive documents are read through the assistant only if the owner has not excluded them.

## Operations

- `configure-paperless.yml` and a `paperless_configure` role deploy the units, like `configure-immich.yml`;
- the VM is provisioned with `provision-vm.yml`, which also registers it with Katello, node_exporter and Prometheus;
- `vm-updates.yml` patches the host and `container-updates.yml` the containers, each after a snapshot;
- a health check polls the login page.

## Phases

0. This document, validated by the owner.
1. Provision the VM, write the role and the playbook, and create the secrets and the administrator account by hand.
2. Pilot import and review.
3. Full import in batches and taxonomy.
4. Matching rules and the classifier.

## Decisions

- The VM is named `paperless101`.
- No subfolder of the tree is excluded.
- The cloud storage stays the master copy: nothing is deleted there.
- OCR languages: French, and English for the rare documents in that language.
- The few office files and archives are exported to PDF by the owner before the import; anything left is skipped.
