# Container Init & Zombie Reaping (tini)

## Background

The `tradeautonom` Docker image runs `python main.py` as the container's entry
command. Docker's `HEALTHCHECK` directive spawns a lightweight Python subprocess
every 30 seconds to probe `/health`. Without a proper init process as PID 1,
those subprocess exits are never collected with `waitpid()` and accumulate as
**zombies**.

Over hours, zombies pile up:

- Each zombie holds a PID slot and a tiny kernel `task_struct`.
- Healthcheck fork overhead (CoW page-tables, Python interpreter warm-up)
  grows under memory pressure.
- The cgroup eventually hits `memory.max` and the OOM-killer kills the
  uvicorn main process.
- Docker's `restart: unless-stopped` policy brings the container back — and
  the cycle repeats.

We observed this on `ta-user-eta9u0ir`: ~100 zombies/hour, ~513 zombies over
5 hours, then an OOM-kill reproduced roughly every 4 hours (03:50, 07:05,
11:02, 17:12 UTC on 2026-04-22).

## Fix

1. Install `tini` in the base image.
2. Set `ENTRYPOINT ["/usr/bin/tini", "--"]` so tini becomes PID 1.
3. `tini` installs a `SIGCHLD` handler and reaps zombies automatically.
4. Docker's `--init` flag (an alternative path using Docker's bundled tini)
   is also set in `docker run` invocations and `init: true` in compose files
   as defense-in-depth.

## Verifying

Inside the container:

```bash
docker exec <container> cat /proc/1/comm
# expected: tini

docker exec <container> sh -c 'ls -d /proc/[0-9]* | while read p; do
    awk "/^State:/" $p/status; done | sort | uniq -c'
# expected: only S (sleeping), no Z (zombie)
```

## Configuration locations

| File | Purpose |
|---|---|
| `deploy/prod/Dockerfile` | `apt-get install tini` + `ENTRYPOINT ["/usr/bin/tini", "--"]` (line ~19, ~34) |
| `deploy/local/Dockerfile` | Same, for local dev image |
| `deploy/prod/docker-compose.yml` | `init: true` under service |
| `deploy/local/docker-compose.yml` | Same |
| `deploy/v3/manage.sh` | `--init --memory 1g --memory-swap 1g` on `cmd_create` + `cmd_update` |
| `deploy/v3/deploy.sh` | `--init` on `cmd_up` for v3 container |
| `deploy/prod/deploy.sh` | `--init` on `cmd_up` for prod container |
| `deploy/v2/deploy.sh` | `--init` on `cmd_up` for v2 container |

## Memory limit

Previously user containers were capped at 512 MiB. With 5+ bots and active
TWAP entries, this was insufficient even without the zombie leak. Limit
raised to **1 GiB**. The Photon host has 31 GiB RAM so 8 user containers at
1 GiB each (8 GiB total) is safe with ~23 GiB headroom.

Runtime tuning without restart:
```bash
docker update --memory 1g --memory-swap 1g <container>
```

## Rollout history (2026-04-22)

- 14:30 UTC: increased `memory.max` from 512 MiB → 1 GiB on all 8 user
  containers via `docker update`. No restart required.
- 14:32 UTC: restarted `ta-user-eta9u0ir` to reap its 522 accumulated zombies
  (container was hung, health endpoint timing out).
- 15:00 UTC: rebuilt `tradeautonom:v3` image with `tini` ENTRYPOINT.
- 15:05 UTC: recreated 6 of 8 user containers (all that were vault-locked,
  zero runtime risk). `ta-user-8hsUAQmD` and `ta-user-t9epq1Rt` retain the
  old image until their HOLDING/PAUSED bots exit to IDLE.

## When to recreate a user container

Only when the bot state permits it. Specifically:

- Prefer IDLE — no position risk.
- Vault-locked container (no keys in RAM) is safe: no bots can be running.
- Container with HOLDING bots: restart is survivable (position state
  persists, `engine.start()` resyncs from the exchange) but interrupts
  WebSocket fill feeds briefly. Not recommended mid-TWAP (ENTERING/EXITING).

Command template:

```bash
docker rm -f ta-user-<suffix>
docker run -d \
  --name ta-user-<suffix> --hostname ta-user-<suffix> \
  --restart unless-stopped --init \
  --memory 1g --memory-swap 1g \
  --ulimit nofile=65536:65536 \
  -p <port>:<port> \
  -e APP_HOST=0.0.0.0 -e APP_PORT=<port> -e GRVT_ENV=prod -e USER_ID=<user_id> \
  -v ta-data-<suffix>:/app/data \
  -v /opt/tradeautonom-v3/app:/app/app:ro \
  tradeautonom:v3
```
