"""
Cloud persistence for V2 (Cloudflare Containers) — syncs /app/data/ to R2
via an HTTP callback to the user-v2 Worker, which owns the R2 binding.

V1 stores everything under /app/data/ (auth.json, secrets.enc, bots/<sym>/
config|position|timer.json, dna_bot/, jobs.json, trade_logs/) on a persistent
Docker volume. On CF Containers the local disk is ephemeral — on container
restart, redeploy, or eviction, that state vanishes.

This module bridges the gap without changing the rest of the engine:
  - `restore_sync()` runs BEFORE the FastAPI app boots. GETs
    `<STATE_ENDPOINT>/restore?user_id=<id>` → extracts the returned tarball
    to /app/data/. 404 response = new user with no prior state (fine).
  - `start_background_flush()` runs during the FastAPI lifespan. Every
    `flush_interval_s` seconds, if any file under /app/data/ changed, tar
    the whole directory and POST to `<STATE_ENDPOINT>/flush?user_id=<id>`.
    The tar is small (typical <1 MB).
  - SIGTERM flush: on container shutdown, make one last upload before the
    process exits so the most recent state persists.

The Worker side uses the R2 binding directly (no S3 API tokens needed).
See deploy/cf-containers/user-v2/src/index.ts for the /__state/* handlers.

Only writes occur if the V2_CLOUD_PERSISTENCE flag is set. On V1 (Photon),
this module is imported but no-ops.

Single-writer guarantee: Cloudflare Durable Objects hold exactly one
Container instance per idFromName() hash. So there's never a concurrent
write conflict on `<user_id>.tar.gz` — no distributed-lock needed.

Config (via Settings or env vars):
  V2_CLOUD_PERSISTENCE=1        — master switch
  USER_ID                       — required; used as the R2 object key prefix
  STATE_ENDPOINT                — internal URL the container can reach the
                                   user-v2 Worker on (e.g. http://user-v2.internal)
  V2_SHARED_TOKEN               — same token the Worker uses to authenticate
                                   service-binding calls; container presents it
                                   on the X-Internal-Token header.
  V2_FLUSH_INTERVAL_S=30        — default upload cadence (optional).
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import signal
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tradeautonom.cloud_persistence")

_DATA_DIR = Path(os.environ.get("CLOUD_PERSISTENCE_DATA_DIR", "/app/data"))

# Minimum interval between full uploads (seconds). If files change more
# frequently than this we coalesce — one upload at the end of the window.
DEFAULT_FLUSH_INTERVAL_S = 30.0

# Files under /app/data/ we refuse to include in the snapshot, even if
# present. Keeps each user's tarball clean of logs, tmp, and other data
# that doesn't belong in state restore.
_EXCLUDED_PATTERNS = (
    # Engine logs and trade journals get ingested to CF history separately;
    # no need to re-sync them on every restore.
    "trade_logs/",
    "logs/",
    # Anything transient.
    ".pyc",
    "__pycache__",
    ".tmp",
)


# ── Config (read lazily to avoid cycles with app.config) ──────────

class _Config:
    user_id: str = ""
    state_endpoint: str = ""
    shared_token: str = ""
    enabled: bool = False
    flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S

    @classmethod
    def load(cls) -> "_Config":
        # Prefer app.config.Settings when available for consistency with the
        # rest of the app. Fall back to os.environ so that entrypoint.sh can
        # run `python -c "from app.cloud_persistence import restore_sync; ..."`
        # BEFORE app.server is fully importable.
        try:
            from app.config import Settings  # noqa: WPS433
            s = Settings()
            cls.user_id = getattr(s, "user_id", "") or os.environ.get("USER_ID", "")
            cls.state_endpoint = getattr(s, "state_endpoint", "") or os.environ.get("STATE_ENDPOINT", "")
            cls.shared_token = getattr(s, "v2_shared_token", "") or os.environ.get("V2_SHARED_TOKEN", "")
            cls.flush_interval_s = float(getattr(s, "v2_flush_interval_s", DEFAULT_FLUSH_INTERVAL_S) or DEFAULT_FLUSH_INTERVAL_S)
            cls.enabled = bool(getattr(s, "v2_cloud_persistence", False))
        except Exception:
            cls.user_id = os.environ.get("USER_ID", "")
            cls.state_endpoint = os.environ.get("STATE_ENDPOINT", "")
            cls.shared_token = os.environ.get("V2_SHARED_TOKEN", "")
            cls.flush_interval_s = float(os.environ.get("V2_FLUSH_INTERVAL_S", str(DEFAULT_FLUSH_INTERVAL_S)))
            cls.enabled = os.environ.get("V2_CLOUD_PERSISTENCE", "0") in ("1", "true", "True")
        return cls


def set_runtime_credentials(user_id: str, shared_token: str) -> None:
    """Inject user_id + shared_token at runtime (from request headers).

    The container is started by `startAndWaitForPorts` with global envVars
    (V2_CLOUD_PERSISTENCE=1, STATE_ENDPOINT=...) but USER_ID cannot be
    global because it's per-user. Workers set X-User-Id + X-Internal-Token
    on every proxied request. A FastAPI middleware picks these up once and
    calls this function; cloud_persistence then has what it needs.
    """
    changed = False
    if user_id and _Config.user_id != user_id:
        _Config.user_id = user_id
        os.environ["USER_ID"] = user_id
        changed = True
    if shared_token and _Config.shared_token != shared_token:
        _Config.shared_token = shared_token
        os.environ["V2_SHARED_TOKEN"] = shared_token
        changed = True
    if changed:
        logger.info(
            "cloud_persistence: runtime creds set (user_id=%s, token=%s)",
            user_id, "present" if shared_token else "missing",
        )


def _endpoint_base() -> str:
    """Normalised endpoint URL, no trailing slash."""
    return (_Config.state_endpoint or "").rstrip("/")


def _all_creds_present() -> bool:
    return all([
        _Config.user_id,
        _Config.state_endpoint,
        _Config.shared_token,
    ])


# ── Public API ─────────────────────────────────────────────────────

def restore_sync() -> None:
    """Restore /app/data/ from R2 before the app boots. Blocking on purpose.

    Called from entrypoint.sh. If no tarball exists (new user), we leave the
    directory untouched. Errors are logged but do not prevent boot — a
    corrupt/unreadable tarball should NOT block a user's ability to reach
    the lock screen.
    """
    _Config.load()
    if not _Config.enabled:
        logger.info("cloud_persistence disabled — skipping restore")
        return
    if not _all_creds_present():
        logger.warning(
            "cloud_persistence: missing config (user_id=%r endpoint=%r token=%s) — skipping restore",
            _Config.user_id,
            _Config.state_endpoint,
            "present" if _Config.shared_token else "missing",
        )
        return

    url = f"{_endpoint_base()}/__state/restore?user_id={_Config.user_id}"
    logger.info("cloud_persistence: restore begin endpoint=%s", url)

    try:
        req = urllib.request.Request(url, headers={
            "X-Internal-Token": _Config.shared_token,
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.info("cloud_persistence: no prior state for user_id=%s — fresh start", _Config.user_id)
        else:
            logger.warning("cloud_persistence: restore HTTP %s — continuing with empty state", exc.code)
        return
    except Exception as exc:
        logger.warning("cloud_persistence: restore failed (%s) — continuing with empty state", exc)
        return

    size = len(body)
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tf:
            _safe_extract(tf, _DATA_DIR)
        logger.info("cloud_persistence: restore complete — %d bytes extracted to %s (http=%d)", size, _DATA_DIR, status)
    except Exception as exc:
        logger.error("cloud_persistence: tarball extraction failed: %s — state may be partial", exc)


async def start_background_flush() -> Optional[asyncio.Task]:
    """Start the periodic flush task. Returns the task handle (or None if disabled)."""
    _Config.load()
    if not _Config.enabled:
        logger.info("cloud_persistence disabled — no background flush")
        return None
    if not _all_creds_present():
        logger.warning("cloud_persistence: missing config — no background flush")
        return None
    task = asyncio.create_task(_flush_loop())
    _register_shutdown_handler()
    logger.info(
        "cloud_persistence: background flush started (interval=%.1fs, endpoint=%s, user_id=%s)",
        _Config.flush_interval_s, _Config.state_endpoint, _Config.user_id,
    )
    return task


async def flush(reason: str = "periodic") -> bool:
    """Tar /app/data/ and upload via HTTP POST. Returns True if upload happened."""
    _Config.load()
    if not _Config.enabled:
        return False
    if not _all_creds_present():
        return False
    try:
        buf = _make_tarball()
    except Exception as exc:
        logger.warning("cloud_persistence: tar failed (%s) — flush aborted", exc)
        return False
    if buf is None:
        return False

    body = buf.getvalue()
    size = len(body)
    if size == 0:
        return False

    url = f"{_endpoint_base()}/__state/flush?user_id={_Config.user_id}"
    try:
        # Run the blocking HTTP in a thread pool so we don't block the loop.
        loop = asyncio.get_event_loop()
        resp_code = await loop.run_in_executor(None, _http_post, url, body)
        if 200 <= resp_code < 300:
            logger.info("cloud_persistence: flush ok reason=%s size=%d http=%d", reason, size, resp_code)
            return True
        logger.warning("cloud_persistence: flush HTTP %s — size=%d reason=%s", resp_code, size, reason)
        return False
    except Exception as exc:
        logger.warning("cloud_persistence: flush upload failed (%s, %d bytes, reason=%s)", exc, size, reason)
        return False


def _http_post(url: str, body: bytes) -> int:
    """Blocking HTTP POST with tar body. Returns status code."""
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/gzip",
            "X-Internal-Token": _Config.shared_token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _http_post_with_body(url: str, body: bytes) -> tuple[int, str, str]:
    """Same as _http_post but returns (status, response_body, server_header).

    Diagnostic helper for Phase F.4 — lets us see if the 403 is from our
    Worker (which returns {"error":"Forbidden","presented_masked":...})
    or from a Cloudflare edge WAF/Access rule (different body shape).
    """
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/gzip",
            "X-Internal-Token": _Config.shared_token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body_txt = resp.read().decode("utf-8", errors="replace")[:500]
            server = resp.headers.get("server", "")
            return (resp.status, body_txt, server)
    except urllib.error.HTTPError as exc:
        body_txt = ""
        server = ""
        try:
            body_txt = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        try:
            server = exc.headers.get("server", "") if exc.headers else ""
        except Exception:
            pass
        return (exc.code, body_txt, server)


# ── Internals ─────────────────────────────────────────────────────

_shutdown_handler_registered = False
_last_flush_signature: tuple[int, int] | None = None


def _register_shutdown_handler() -> None:
    global _shutdown_handler_registered
    if _shutdown_handler_registered:
        return
    _shutdown_handler_registered = True

    async def _do_flush():
        await flush(reason="shutdown")

    def _handler(signum, _frame):
        logger.warning("cloud_persistence: signal %s received — final flush", signum)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_do_flush())
                return
        except RuntimeError:
            pass
        asyncio.run(_do_flush())

    try:
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
    except ValueError:
        # Signal handlers can only be set on the main thread.
        pass


async def _flush_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_Config.flush_interval_s)
        except asyncio.CancelledError:
            break

        sig = _signature_of(_DATA_DIR)
        global _last_flush_signature
        if sig == _last_flush_signature:
            continue
        ok = await flush(reason="periodic")
        if ok:
            _last_flush_signature = sig


def _signature_of(root: Path) -> tuple[int, int]:
    """Cheap change-detection signature: (max_mtime, total_bytes)."""
    if not root.exists():
        return (0, 0)
    max_mtime = 0
    total = 0
    for p in root.rglob("*"):
        if _is_excluded(p):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime > max_mtime:
            max_mtime = int(st.st_mtime)
        total += st.st_size
    return (max_mtime, total)


def _make_tarball() -> Optional[io.BytesIO]:
    """Create an in-memory gzipped tar of _DATA_DIR, excluding transient bits."""
    if not _DATA_DIR.exists():
        return None
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for p in sorted(_DATA_DIR.rglob("*")):
            if _is_excluded(p):
                continue
            try:
                arcname = str(p.relative_to(_DATA_DIR))
            except ValueError:
                continue
            try:
                tf.add(str(p), arcname=arcname, recursive=False)
            except (OSError, FileNotFoundError):
                continue
    return buf


def _is_excluded(p: Path) -> bool:
    s = str(p)
    for pat in _EXCLUDED_PATTERNS:
        if pat in s:
            return True
    return False


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    """Safer tar extraction that refuses path-traversal entries."""
    dest_resolved = dest.resolve()
    for member in tf.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest_resolved)):
            logger.warning("cloud_persistence: refusing to extract %s (path traversal)", member.name)
            continue
        tf.extract(member, str(dest))


__all__ = ["restore_sync", "start_background_flush", "flush"]
