# claude101: Claude Code assistant on the cluster

Status: proposal, nothing is deployed.

## Goal

A VM (`claude101`) running the unmodified `claude` binary, reachable from a phone. When the owner sends a request, Claude Code acts on it without asking again for each step: it reads, edits the repository, commits, pushes, launches playbooks to test and correct a situation, then reports what it changed. It does nothing unless a message arrives: no timer, cron job or loop ever starts a session.

The request itself is the authorization. Because no step is confirmed, the limits below are enforced by the system and not by the model.

## How it is driven

Phase 0 uses Remote Control: `claude remote-control` runs as a systemd service and the Claude app on the phone attaches to it. The VM only makes outbound HTTPS connections and opens no inbound port. Transcripts of Remote Control sessions are stored by Anthropic.

A self-hosted chat (Mattermost behind BunkerWeb, with a custom Claude Code channel) can replace the app later if it turns out to be needed. Channels are a research preview and a custom channel needs a development flag, so it is not part of phase 0.

## Authentication

The owner's Claude subscription is used through the official client only: one interactive `claude auth login` as the service user, credentials kept in that user's `~/.claude/.credentials.json` (mode 0600). No SDK program of our own uses the subscription: the legal page asks developers building services, including with the Agent SDK, to use an API key. `claude setup-token` is not used, since its token cannot start Remote Control sessions.

The documentation gives no lifetime for a login and states that an unattended session stops when the login can no longer be refreshed. Phase 0 measures how long the login actually lasts before anything else is built on it.

## Permission mode

The service starts with `--permission-mode auto`: a classifier reviews each action and blocks what goes beyond the request, targets unknown infrastructure or looks driven by content Claude read (logs, web pages, files). Auto mode lowers the risk of prompt injection but does not remove it. If the classifier blocks repeatedly, Claude Code asks again and the question appears in the app. Bypass mode is not used.

## Limits that do not depend on the model

- **Unprivileged user.** The service runs as a dedicated user with no sudo and no ssh key to any other host. The fleet's `deploy` account has passwordless sudo everywhere, so an ssh key would be root everywhere.
- **Root-owned managed settings** hold the `deny` rules, so the service user cannot edit them. Deny rules apply in every permission mode. They forbid at least: `sudo`, `ssh`, `scp`, force pushes, branch deletion, reading credential and token files, direct calls to the Semaphore and Proxmox APIs with `curl` or `wget`, and editing the managed settings.
- **Repository.** A deploy key dedicated to this VM, valid for this repository only. Branch protection on `main` on GitHub rejects force pushes and deletions, so the limit holds even if a client-side rule is bypassed. Before each push the clone runs `ansible-playbook --syntax-check` on the changed playbooks.
- **Playbooks** run only through Semaphore, with a token for a dedicated account that has the Task Runner role. `scripts/semaphore-ctl` applies an allowlist of template ids. The token is readable only by a root-owned wrapper that the service user may call through one sudoers entry, so the allowlist cannot be skipped by calling the API directly. The allowlist excludes decommissioning, backup deletion and anything that destroys a VM. With Task Runner it cannot create templates: a new playbook needs a template created by the owner, an existing playbook can be edited and re-run.
- **Audit.** Session transcripts stay on the VM, every change is a commit, every run is a Semaphore task. The final message of each request lists the commits and task ids it produced.

## Notification

The answer to a request is the notification. Whether the phone app also pushes a notification when a long request finishes, and not only when it asks a question, is checked in phase 0.

## Provisioning

`claude101` is created from the hardened template 9001 with `provision-vm.yml` and a `host_vars/claude101.yml`; its address comes from the static server range through phpIPAM. A role (`claude_assistant`) creates the service user, installs the client, writes the managed settings and the systemd unit, and clones the repository. Three steps are manual because they involve secrets: the interactive login, adding the deploy key to GitHub, and creating the Semaphore token.

## Phases

0. Provision the VM, log in, run Remote Control, make a harmless fix end to end (edit, push, Semaphore run, report). Observe login persistence for a week.
1. Adjust the deny rules and the template allowlist from what phase 0 showed.
2. Optional: self-hosted chat in place of the app.

## Open points

- How the client is installed on a host whose only package source is the Katello content views.
- Which templates go in the allowlist.
- Whether the owner wants the Manager role later, accepting that Claude could then create templates and run any playbook on the fleet.
