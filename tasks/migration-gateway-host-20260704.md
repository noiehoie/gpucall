# Gateway Host Migration Report (fleet-regulation compliance)

Date: 2026-07-04 JST
Decision executed: gpucall production moves to the fleet's designated web-services host; the stock-trading host returns to its dedicated duty.
Constraint honored: **zero disruption to the other web services on the destination host** — all work was user-space or gpucall-scoped; the only shared-resource change was one additive nginx vhost applied via `nginx -t` + graceful reload (twice `-t` rejected a bad directive before any reload, so existing vhosts were never at risk).

## What moved

| Component | Before | After |
| --- | --- | --- |
| Gateway v2.0.71 | old host, nohup | new host, `~/bin/gpucall-gateway-start.sh` (admission limits 4/6/8 pinned), Tailscale bind :18088 |
| Config + credentials | old host | rsync'd; object-store endpoint rewritten to the new public host |
| State (validation evidence, jobs, idempotency, audit, inboxes, receipts) | old host | pre-copy + final delta rsync during cutover |
| Object store (MinIO) | docker on old host, caddy TLS | docker `gpucall-minio` on new host (127.0.0.1:19000), nginx vhost + fresh Let's Encrypt cert; **597 objects intact post-move** |
| systemd user services | panopticon / recipe-admin-watch / sovereignty-reap.timer | recreated on new host (linger enabled) |
| Caller (external news pipeline) | pointed at old host | `.env` gateway URL switched, one line |

## Verification (all on the new host)

```text
setup status:      OOB readiness: onboarding-ready / Panopticon evidence-fresh / synthetic dry-run ok
sovereignty report: bucket gpucall-oob, objects: 597, error: None
route revalidation (config hash changed with the endpoint):
  infer-rank-text-items-standard @ modal-h200-qwen3-32b   passed
  4 × light routes @ modal-t4-qwen25-0.5b                 passed
  vision draft @ modal-vision-catalog-a10g-florence       passed
caller canary:     extract_json / translate / summarize / rank / vision = 5/5 GO
```

Remaining routes (30B-A3B, 235B flagship) revalidate automatically via the
watch service's drift re-validation; manual smoke optional.

## Cutover window

Old gateway stopped → final state delta → fossil containers stopped → new
gateway up: **under 3 minutes** of gateway unavailability. No other service
on the destination host was restarted or reloaded non-gracefully.

## Decommission state (rollback window until 2026-07-11)

- Old host: gateway stopped, services disabled, MinIO container stopped
  (data retained), caddy stopped+disabled (it served only the gpucall
  hostname; 443 freed for the trading host's own future use).
- New host: fossil v2.0.9 `gpucalluser-*` containers stopped, not removed;
  a fresh-install config backup sits at `~/.config/gpucall.fresh-install-backup`.
- After 2026-07-11: delete old-host gpucall state + containers, remove
  fossil containers and their compose dir, drop the config backup.

## Fleet documentation

`fleet.md` updated (new-host gpucall operations note, container table,
old-host reversion) and pushed to the fleet's llm-brain origin.
