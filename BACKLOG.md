# Backlog

## 1. BunkerWeb reverse proxy
- Deploy BunkerWeb as reverse proxy in front of all services
- Once live, update all `health_check_url` values in `host_vars/` to go through BunkerWeb
- Add BunkerWeb's own host as a priority update target at the start of `vm-updates.yml`, before patching the other VMs

## Done
- ~~Weekly VM backup to pCloud~~ — shipped as `vm-backups.yml`: `vzdump` (ZSTD) every VM to `Stockage_SSD` on `pve1` (migrating it there first if needed), uploads to pCloud, keeps 5 backups locally and 1 on pCloud, rebalances the cluster afterwards if anything was moved.
