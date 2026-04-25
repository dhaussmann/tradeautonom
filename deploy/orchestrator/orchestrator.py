"""
TradeAutonom Orchestrator — manages per-user Docker containers on the NAS.

Runs as a lightweight FastAPI service alongside the user containers.
The CF Worker authenticates users and proxies requests here.
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import docker
import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("orchestrator")

# ── Config ────────────────────────────────────────────────────

ORCH_TOKEN = os.environ.get("ORCH_TOKEN", "")
TRADEAUTONOM_IMAGE = os.environ.get("TRADEAUTONOM_IMAGE", "tradeautonom:v3")
BASE_PORT = int(os.environ.get("BASE_PORT", "9001"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "/app/data/orchestrator_state.json"))
CONTAINER_PREFIX = "ta-user-"
DOCKER_HOST_IP = os.environ.get("DOCKER_HOST_IP", "172.17.0.1")
CONTAINER_MEM_LIMIT = os.environ.get("CONTAINER_MEM_LIMIT", "512m")
CONTAINER_CPU_QUOTA = int(os.environ.get("CONTAINER_CPU_QUOTA", "50000"))  # 0.5 cores (period=100000)
WATCHDOG_INTERVAL = int(os.environ.get("WATCHDOG_INTERVAL", "60"))
WATCHDOG_MAX_RESTARTS = int(os.environ.get("WATCHDOG_MAX_RESTARTS", "3"))
WATCHDOG_WINDOW_S = int(os.environ.get("WATCHDOG_WINDOW_S", "300"))  # 5 min
SHARED_CODE_DIR = os.environ.get("SHARED_CODE_DIR", "/volume1/docker/tradeautonom/app")

DEFAULT_ENV = {
    "APP_HOST": "0.0.0.0",
    "GRVT_ENV": "prod",
}


class OrchestratorState:
    """Persists user->container mappings to disk."""

    def __init__(self, path: Path):
        self.path = path
        self.containers: dict[str, dict] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self.containers = json.loads(self.path.read_text())
                logger.info("Loaded state: %d containers", len(self.containers))
            except Exception as e:
                logger.warning("Failed to load state: %s", e)
                self.containers = {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.containers, indent=2))

    def get(self, user_id: str) -> Optional[dict]:
        return self.containers.get(user_id)

    def set(self, user_id: str, port: int, container_name: str, status: str = "running"):
        self.containers[user_id] = {
            "port": port,
            "container_name": container_name,
            "status": status,
            "created_at": int(time.time()),
        }
        self._save()

    def update_status(self, user_id: str, status: str):
        if user_id in self.containers:
            self.containers[user_id]["status"] = status
            self._save()

    def remove(self, user_id: str):
        self.containers.pop(user_id, None)
        self._save()

    def all(self) -> dict[str, dict]:
        return dict(self.containers)

    def record_restart(self, user_id: str):
        """Track restart timestamps for crash-loop detection."""
        if user_id not in self.containers:
            return
        restarts = self.containers[user_id].setdefault("restarts", [])
        restarts.append(int(time.time()))
        # Keep only restarts within the window
        cutoff = int(time.time()) - WATCHDOG_WINDOW_S
        self.containers[user_id]["restarts"] = [t for t in restarts if t > cutoff]
        self._save()

    def restart_count_in_window(self, user_id: str) -> int:
        if user_id not in self.containers:
            return 0
        cutoff = int(time.time()) - WATCHDOG_WINDOW_S
        restarts = self.containers[user_id].get("restarts", [])
        return len([t for t in restarts if t > cutoff])

    def next_port(self) -> int:
        # Ports tracked in orchestrator state
        used = {c["port"] for c in self.containers.values()}
        # Also check actual Docker port bindings to avoid conflicts
        # with containers not managed by orchestrator
        if docker_client:
            try:
                for c in docker_client.containers.list(all=True):
                    ports = c.attrs.get("HostConfig", {}).get("PortBindings") or {}
                    for bindings in ports.values():
                        if bindings:
                            for b in bindings:
                                try:
                                    used.add(int(b["HostPort"]))
                                except (KeyError, ValueError, TypeError):
                                    pass
            except Exception as e:
                logger.warning("Could not enumerate Docker ports: %s", e)
        port = BASE_PORT
        while port in used:
            port += 1
        return port


state = OrchestratorState(STATE_FILE)
docker_client: docker.DockerClient | None = None
_watchdog_task: asyncio.Task | None = None


async def _watchdog_loop():
    """Periodically check all containers and auto-restart crashed ones."""
    logger.info("Watchdog started (interval=%ds, max_restarts=%d in %ds window)",
                WATCHDOG_INTERVAL, WATCHDOG_MAX_RESTARTS, WATCHDOG_WINDOW_S)
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)
        if not docker_client:
            continue
        for user_id, info in state.all().items():
            container_name = info.get("container_name", "")
            if info.get("status") == "stopped":
                continue  # user explicitly stopped — don't auto-restart
            try:
                c = docker_client.containers.get(container_name)
                if c.status in ("exited", "dead"):
                    recent = state.restart_count_in_window(user_id)
                    if recent >= WATCHDOG_MAX_RESTARTS:
                        if info.get("status") != "crash_loop":
                            logger.error("Watchdog: %s in crash loop (%d restarts in %ds) — not restarting",
                                         container_name, recent, WATCHDOG_WINDOW_S)
                            state.update_status(user_id, "crash_loop")
                        continue
                    logger.warning("Watchdog: restarting %s (was %s, restarts=%d)",
                                   container_name, c.status, recent)
                    c.start()
                    state.record_restart(user_id)
                    state.update_status(user_id, "running")
                elif c.status == "running" and info.get("status") == "crash_loop":
                    # Container recovered (manually restarted) — clear crash_loop
                    state.update_status(user_id, "running")
            except docker.errors.NotFound:
                logger.warning("Watchdog: container %s not found for user %s", container_name, user_id)
            except Exception as e:
                logger.warning("Watchdog: error checking %s: %s", container_name, e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global docker_client, _watchdog_task
    try:
        docker_client = docker.from_env()
        docker_client.ping()
        logger.info("Docker connected. Image: %s", TRADEAUTONOM_IMAGE)
    except Exception as e:
        logger.error("Docker connection failed: %s", e)
        docker_client = None
    _watchdog_task = asyncio.create_task(_watchdog_loop())
    yield
    if _watchdog_task:
        _watchdog_task.cancel()
        try:
            await _watchdog_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="TradeAutonom Orchestrator", version="2.0.0", lifespan=lifespan)


@app.middleware("http")
async def verify_orch_token(request: Request, call_next):
    if request.url.path == "/orch/health":
        return await call_next(request)
    token = request.headers.get("X-Orch-Token", "")
    if not ORCH_TOKEN or token != ORCH_TOKEN:
        return Response(
            content=json.dumps({"error": "Unauthorized"}),
            status_code=401,
            media_type="application/json",
        )
    return await call_next(request)


@app.get("/orch/health")
async def health():
    docker_ok = False
    if docker_client:
        try:
            docker_client.ping()
            docker_ok = True
        except Exception:
            pass

    containers_summary = {"total": 0, "running": 0, "stopped": 0, "crash_loop": 0, "exited": 0}
    container_details = []
    for user_id, info in state.all().items():
        containers_summary["total"] += 1
        docker_status = "unknown"
        uptime_s = None
        try:
            c = docker_client.containers.get(info["container_name"])
            docker_status = c.status
            if c.status == "running":
                # Parse started_at for uptime
                started = c.attrs.get("State", {}).get("StartedAt", "")
                if started:
                    try:
                        st = datetime.fromisoformat(started.replace("Z", "+00:00"))
                        uptime_s = int((datetime.now(timezone.utc) - st).total_seconds())
                    except Exception:
                        pass
        except Exception:
            docker_status = "not_found"

        orch_status = info.get("status", "unknown")
        if docker_status == "running":
            containers_summary["running"] += 1
        elif orch_status == "crash_loop":
            containers_summary["crash_loop"] += 1
        elif orch_status == "stopped":
            containers_summary["stopped"] += 1
        else:
            containers_summary["exited"] += 1

        container_details.append({
            "user_id": user_id[:8],
            "container": info.get("container_name", ""),
            "port": info.get("port"),
            "orch_status": orch_status,
            "docker_status": docker_status,
            "uptime_s": uptime_s,
            "restarts_recent": state.restart_count_in_window(user_id),
        })

    return {
        "status": "ok" if docker_ok else "degraded",
        "docker": docker_ok,
        "watchdog": {"interval_s": WATCHDOG_INTERVAL, "max_restarts": WATCHDOG_MAX_RESTARTS, "window_s": WATCHDOG_WINDOW_S},
        "resources": {"mem_limit": CONTAINER_MEM_LIMIT, "cpu_quota": CONTAINER_CPU_QUOTA},
        "containers": containers_summary,
        "details": container_details,
    }


@app.get("/orch/containers")
async def list_containers():
    result = {}
    for user_id, info in state.all().items():
        actual_status = "unknown"
        try:
            c = docker_client.containers.get(info["container_name"])
            actual_status = c.status
        except Exception:
            actual_status = "not_found"
        result[user_id] = {**info, "docker_status": actual_status}
    return result


@app.post("/orch/containers")
async def create_container(request: Request):
    body = await request.json()
    user_id = body.get("user_id")
    if not user_id:
        raise HTTPException(400, "user_id required")

    existing = state.get(user_id)
    if existing:
        return {"status": "already_exists", **existing}

    if not docker_client:
        raise HTTPException(503, "Docker not available")

    port = body.get("port") or state.next_port()
    container_name = f"{CONTAINER_PREFIX}{user_id[:8]}"
    extra_env = body.get("env_vars", {})

    env = {**DEFAULT_ENV, "APP_PORT": str(port), "USER_ID": user_id, **extra_env}

    # Remove stale container with same name (from a previous failed attempt)
    try:
        stale = docker_client.containers.get(container_name)
        logger.warning("Removing stale container %s (status=%s)", container_name, stale.status)
        stale.remove(force=True)
    except Exception:
        pass

    try:
        container = docker_client.containers.run(
            TRADEAUTONOM_IMAGE,
            name=container_name,
            detach=True,
            restart_policy={"Name": "unless-stopped"},
            ports={f"{port}/tcp": ("0.0.0.0", port)},
            environment=env,
            volumes={
                f"ta-data-{user_id[:8]}": {"bind": "/app/data", "mode": "rw"},
                SHARED_CODE_DIR: {"bind": "/app/app", "mode": "ro"},
            },
            dns=["1.1.1.1", "1.0.0.1"],
            mem_limit=CONTAINER_MEM_LIMIT,
            cpu_period=100000,
            cpu_quota=CONTAINER_CPU_QUOTA,
            ulimits=[docker.types.Ulimit(name="nofile", soft=65536, hard=65536)],
        )
        state.set(user_id, port, container_name, "running")
        logger.info("Created container %s on port %d for user %s", container_name, port, user_id)
        return {"status": "created", "port": port, "container_name": container_name, "container_id": container.id}
    except Exception as e:
        logger.error("Failed to create container for %s: %s", user_id, e)
        raise HTTPException(500, f"Container creation failed: {e}")


@app.post("/orch/containers/{user_id}/start")
async def start_container(user_id: str):
    info = state.get(user_id)
    if not info:
        raise HTTPException(404, "No container for this user")
    try:
        c = docker_client.containers.get(info["container_name"])
        c.start()
        state.update_status(user_id, "running")
        return {"status": "started"}
    except Exception as e:
        raise HTTPException(500, f"Start failed: {e}")


@app.post("/orch/containers/{user_id}/stop")
async def stop_container(user_id: str):
    info = state.get(user_id)
    if not info:
        raise HTTPException(404, "No container for this user")
    try:
        c = docker_client.containers.get(info["container_name"])
        c.stop(timeout=30)
        state.update_status(user_id, "stopped")
        return {"status": "stopped"}
    except Exception as e:
        raise HTTPException(500, f"Stop failed: {e}")


@app.get("/orch/containers/{user_id}/logs")
async def container_logs(user_id: str, tail: int = Query(default=100, ge=1, le=5000)):
    """Get recent logs from a user's container."""
    info = state.get(user_id)
    if not info:
        raise HTTPException(404, "No container for this user")
    if not docker_client:
        raise HTTPException(503, "Docker not available")
    try:
        c = docker_client.containers.get(info["container_name"])
        logs = c.logs(tail=tail, timestamps=True).decode("utf-8", errors="replace")
        return {
            "container": info["container_name"],
            "status": c.status,
            "tail": tail,
            "logs": logs,
        }
    except docker.errors.NotFound:
        raise HTTPException(404, f"Container {info['container_name']} not found in Docker")
    except Exception as e:
        raise HTTPException(500, f"Failed to get logs: {e}")


@app.get("/orch/containers/{user_id}/stats")
async def container_stats(user_id: str):
    """Get CPU/RAM usage stats for a user's container."""
    info = state.get(user_id)
    if not info:
        raise HTTPException(404, "No container for this user")
    if not docker_client:
        raise HTTPException(503, "Docker not available")
    try:
        c = docker_client.containers.get(info["container_name"])
        if c.status != "running":
            return {"container": info["container_name"], "status": c.status, "stats": None}
        raw = c.stats(stream=False)
        # Parse CPU
        cpu_delta = raw["cpu_stats"]["cpu_usage"]["total_usage"] - raw["precpu_stats"]["cpu_usage"]["total_usage"]
        system_delta = raw["cpu_stats"]["system_cpu_usage"] - raw["precpu_stats"]["system_cpu_usage"]
        num_cpus = raw["cpu_stats"].get("online_cpus", 1)
        cpu_pct = (cpu_delta / system_delta * num_cpus * 100) if system_delta > 0 else 0.0
        # Parse Memory
        mem_usage = raw["memory_stats"].get("usage", 0)
        mem_limit = raw["memory_stats"].get("limit", 1)
        mem_pct = (mem_usage / mem_limit * 100) if mem_limit > 0 else 0.0
        return {
            "container": info["container_name"],
            "status": c.status,
            "stats": {
                "cpu_pct": round(cpu_pct, 2),
                "mem_usage_mb": round(mem_usage / 1024 / 1024, 1),
                "mem_limit_mb": round(mem_limit / 1024 / 1024, 1),
                "mem_pct": round(mem_pct, 1),
            },
        }
    except docker.errors.NotFound:
        raise HTTPException(404, f"Container {info['container_name']} not found in Docker")
    except Exception as e:
        raise HTTPException(500, f"Failed to get stats: {e}")


@app.post("/orch/containers/{user_id}/reset-crashloop")
async def reset_crashloop(user_id: str):
    """Clear crash_loop status so watchdog will restart the container again."""
    info = state.get(user_id)
    if not info:
        raise HTTPException(404, "No container for this user")
    if info.get("restarts"):
        info["restarts"] = []
    state.update_status(user_id, "running")
    return {"status": "reset", "user_id": user_id}


@app.delete("/orch/containers/{user_id}")
async def delete_container(user_id: str):
    info = state.get(user_id)
    if not info:
        raise HTTPException(404, "No container for this user")
    try:
        c = docker_client.containers.get(info["container_name"])
        c.stop(timeout=10)
        c.remove(v=True)
    except docker.errors.NotFound:
        pass
    except Exception as e:
        logger.warning("Error removing container %s: %s", info["container_name"], e)
    state.remove(user_id)
    return {"status": "deleted"}


# ── Phase F.4 M5: state migration endpoints ────────────────────
#
# Pair with the user-v2 Worker /__state/{flush,restore} endpoints. The
# Worker drives the flow: it asks the Orchestrator to package + return
# the user's /app/data/, then POSTs the tarball to its own R2-backed
# /__state/flush. For rollback (V2 → V1), the inverse happens.

@app.get("/orch/export-state/{user_id}")
async def export_state(user_id: str):
    """Stream a tar.gz snapshot of /app/data/ from the user's container.

    The container should be quiesced (all bots IDLE/HOLDING) before this
    is called; we do NOT pause it ourselves because that would break
    HOLDING bots and lose the persistent fill subscription. The Worker
    is responsible for the IDLE pre-flight check.
    """
    info = state.get(user_id)
    if not info:
        raise HTTPException(404, f"No container registered for user {user_id}")
    if not docker_client:
        raise HTTPException(503, "Docker not available")
    container_name = info["container_name"]
    try:
        c = docker_client.containers.get(container_name)
    except docker.errors.NotFound:
        raise HTTPException(404, f"Container {container_name} not in Docker")

    # Use docker exec to tar /app/data — works for both running and
    # stopped containers (docker exec on stopped fails, so we start
    # briefly if needed).
    started_for_export = False
    if c.status != "running":
        try:
            c.start()
            started_for_export = True
            logger.info("export-state: started %s briefly for tar (was %s)", container_name, c.status)
            await asyncio.sleep(2)
        except Exception as e:
            raise HTTPException(500, f"Could not start container for export: {e}")

    try:
        # exec_run with stream=True returns a generator of bytes — perfect
        # for streaming a tar.gz back to the caller without buffering the
        # whole archive in RAM (typical /app/data is < 5 MB but be safe).
        # Force tar to ignore inflight writes (bot might still be writing
        # position.json mid-stream); we accept the risk of a half-written
        # file in exchange for not pausing the container.
        exec_id = docker_client.api.exec_create(
            container_name,
            cmd=["tar", "czf", "-", "--warning=no-file-changed", "-C", "/app", "data"],
            stdout=True,
            stderr=False,
        )["Id"]

        def _stream():
            for chunk in docker_client.api.exec_start(exec_id, stream=True):
                yield chunk

        return StreamingResponse(
            _stream(),
            media_type="application/gzip",
            headers={
                "X-User-Id": user_id,
                "X-Container-Name": container_name,
            },
        )
    except Exception as e:
        raise HTTPException(500, f"Export failed: {e}")
    finally:
        # Don't restore previous state immediately — the streaming response
        # is still being consumed. The watchdog will catch any anomalies.
        if started_for_export:
            logger.info("export-state: container %s left running (started for export)", container_name)


@app.post("/orch/import-state/{user_id}")
async def import_state(user_id: str, request: Request):
    """Receive a tar.gz blob and extract it into the user's container's
    /app/data/. Used for V2 → V1 rollback migration.

    Body: raw application/gzip tarball. The tarball MUST be rooted at
    `/app/data/` files (no leading `data/` directory). _STATE_VERSION
    files are stripped — they belong to V2 only.
    """
    info = state.get(user_id)
    if not info:
        raise HTTPException(404, f"No container registered for user {user_id}")
    if not docker_client:
        raise HTTPException(503, "Docker not available")
    container_name = info["container_name"]
    try:
        c = docker_client.containers.get(container_name)
    except docker.errors.NotFound:
        raise HTTPException(404, f"Container {container_name} not in Docker")

    body = await request.body()
    if not body:
        raise HTTPException(400, "Empty body — expected tar.gz")
    logger.info("import-state: received %d bytes for %s", len(body), container_name)

    # Stop the container so we can replace /app/data atomically.
    was_running = c.status == "running"
    if was_running:
        try:
            c.stop(timeout=10)
        except Exception as e:
            raise HTTPException(500, f"Could not stop container before import: {e}")

    try:
        # Stage the tarball into a tempfile, then cp it into the container
        # and extract. We avoid put_archive() because it expects tar
        # (uncompressed) and the v2 tar is gzip-compressed.
        import tempfile
        import shutil

        tmp_path = f"/tmp/ta-import-{user_id[:8]}-{int(time.time())}.tar.gz"
        with open(tmp_path, "wb") as f:
            f.write(body)

        # Start container briefly (stopped state can't exec) — we don't
        # care about uvicorn coming up, just need the FS available.
        c.start()
        await asyncio.sleep(2)

        try:
            # Wipe old /app/data, recreate, extract new tar (skip
            # _STATE_VERSION which is V2 metadata)
            wipe_id = docker_client.api.exec_create(
                container_name,
                cmd=["sh", "-c", "rm -rf /app/data && mkdir -p /app/data"],
            )["Id"]
            docker_client.api.exec_start(wipe_id)

            # docker cp the tarball into the container
            with open(tmp_path, "rb") as f:
                tar_bytes = f.read()
            # Use docker put_archive with a proper tar wrapper around the
            # gzip blob. Easier: just docker cp via the daemon.
            # Use the exec approach: write to a known path inside.
            mkdir_id = docker_client.api.exec_create(
                container_name,
                cmd=["mkdir", "-p", "/tmp"],
            )["Id"]
            docker_client.api.exec_start(mkdir_id)

            # Copy via docker cp — the docker SDK doesn't expose a clean
            # cp that takes raw bytes for gzip, so we shell out.
            import subprocess
            cp_proc = subprocess.run(
                ["/usr/bin/docker", "cp", tmp_path, f"{container_name}:/tmp/restore.tar.gz"],
                capture_output=True,
                timeout=30,
            )
            if cp_proc.returncode != 0:
                raise RuntimeError(f"docker cp failed: {cp_proc.stderr.decode(errors='replace')}")

            extract_id = docker_client.api.exec_create(
                container_name,
                cmd=["sh", "-c",
                     "cd /app/data && tar xzf /tmp/restore.tar.gz --exclude=_STATE_VERSION && rm -f /tmp/restore.tar.gz"],
            )["Id"]
            docker_client.api.exec_start(extract_id)

            logger.info("import-state: extracted into %s:/app/data", container_name)

            return {
                "status": "ok",
                "user_id": user_id,
                "container_name": container_name,
                "bytes_imported": len(body),
            }
        finally:
            try:
                shutil.os.remove(tmp_path)
            except Exception:
                pass
    except Exception as e:
        raise HTTPException(500, f"Import failed: {e}")
    finally:
        # If the container was running before, it's already running again
        # (we started it for the extract). If it was stopped, stop it
        # again — caller is responsible for starting it via /start.
        if not was_running:
            try:
                c.stop(timeout=10)
            except Exception:
                pass


@app.api_route("/orch/proxy/{user_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_to_container(user_id: str, path: str, request: Request):
    info = state.get(user_id)
    if info:
        port = info["port"]
    else:
        # Fallback: use port hint from Worker (for containers not managed by orchestrator)
        port_hint = request.headers.get("x-container-port")
        if not port_hint:
            raise HTTPException(503, "No container for this user")
        port = int(port_hint)
    target_url = f"http://{DOCKER_HOST_IP}:{port}/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    accept = request.headers.get("accept", "")
    is_sse = "text/event-stream" in accept

    headers = dict(request.headers)
    for h in ["host", "x-orch-token", "x-user-id", "connection", "transfer-encoding"]:
        headers.pop(h, None)

    body = await request.body()

    try:
        if is_sse:
            client = httpx.AsyncClient(timeout=None)
            req = client.build_request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body if body else None,
            )
            resp = await client.send(req, stream=True)

            async def stream_gen():
                try:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                finally:
                    await resp.aclose()
                    await client.aclose()

            return StreamingResponse(
                stream_gen(),
                status_code=resp.status_code,
                headers=dict(resp.headers),
                media_type=resp.headers.get("content-type", "text/event-stream"),
            )
        else:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                resp = await client.request(
                    method=request.method,
                    url=target_url,
                    headers=headers,
                    content=body if body else None,
                )
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    headers=dict(resp.headers),
                )
    except httpx.ConnectError:
        raise HTTPException(502, f"Container on port {port} not reachable")
    except Exception as e:
        raise HTTPException(502, f"Proxy error: {e}")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("ORCH_PORT", "8090"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
