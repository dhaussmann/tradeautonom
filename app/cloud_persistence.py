"""
Cloud persistence for V2 (Cloudflare Containers) — syncs /app/data/ to R2.

V1 stores everything under /app/data/ (auth.json, secrets.enc, bots/<sym>/
config|position|timer.json, dna_bot/, jobs.json, trade_logs/) on a persistent
Docker volume. On CF Containers the local disk is ephemeral — on container
restart, redeploy, or eviction, that state vanishes.

This module bridges the gap without changing the rest of the engine:
  - `restore_sync()` runs BEFORE the FastAPI app boots. Pulls
    `<user_id>.tar.gz` from R2 → extracts to /app/data/. No state on R2
    for a new user is fine; we just start empty.
  - `start_background_flush()` runs during the FastAPI lifespan. Every
    `flush_interval_s` seconds, if any file under /app/data/ changed, tar
    the whole directory and upload. The tar is small (typical <1 MB).
  - SIGTERM flush: on container shutdown, make one last upload before the
    process exits so the most recent state persists.

Only writes occur if the V2_CLOUD_PERSISTENCE flag is set. On V1 (Photon),
this module is imported but no-ops.

Single-writer guarantee: Cloudflare Durable Objects hold exactly one
Container instance per idFromName() hash. So there's never a concurrent
write conflict on `<user_id>.tar.gz` — no distributed-lock needed.

Config (via Settings): user_id, r2_bucket, r2_endpoint, r2_access_key_id,
r2_secret. Missing credentials → module is disabled (logs a warning).
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import signal
import tarfile
import time
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
    r2_bucket: str = ""
    r2_endpoint: str = ""
    r2_access_key_id: str = ""
    r2_secret: str = ""
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
            cls.r2_bucket = getattr(s, "r2_bucket", "") or os.environ.get("R2_BUCKET", "")
            cls.r2_endpoint = getattr(s, "r2_endpoint", "") or os.environ.get("R2_ENDPOINT", "")
            cls.r2_access_key_id = getattr(s, "r2_access_key_id", "") or os.environ.get("R2_ACCESS_KEY_ID", "")
            cls.r2_secret = getattr(s, "r2_secret", "") or os.environ.get("R2_SECRET", "")
            cls.flush_interval_s = float(getattr(s, "v2_flush_interval_s", DEFAULT_FLUSH_INTERVAL_S) or DEFAULT_FLUSH_INTERVAL_S)
            cls.enabled = bool(getattr(s, "v2_cloud_persistence", False))
        except Exception:
            cls.user_id = os.environ.get("USER_ID", "")
            cls.r2_bucket = os.environ.get("R2_BUCKET", "")
            cls.r2_endpoint = os.environ.get("R2_ENDPOINT", "")
            cls.r2_access_key_id = os.environ.get("R2_ACCESS_KEY_ID", "")
            cls.r2_secret = os.environ.get("R2_SECRET", "")
            cls.flush_interval_s = float(os.environ.get("V2_FLUSH_INTERVAL_S", str(DEFAULT_FLUSH_INTERVAL_S)))
            cls.enabled = os.environ.get("V2_CLOUD_PERSISTENCE", "0") in ("1", "true", "True")
        return cls


def _object_key() -> str:
    """S3 key under which the user's state tarball lives."""
    return f"{_Config.user_id}.tar.gz"


def _make_s3_client():
    """Build a boto3 S3 client pointed at R2's S3-compatible endpoint.

    Imported lazily so V1 doesn't need boto3 at runtime even though
    requirements.txt may include it.
    """
    import boto3  # type: ignore
    from botocore.config import Config as BotoConfig  # type: ignore

    return boto3.client(
        "s3",
        endpoint_url=_Config.r2_endpoint,
        aws_access_key_id=_Config.r2_access_key_id,
        aws_secret_access_key=_Config.r2_secret,
        region_name="auto",
        config=BotoConfig(
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=10,
            read_timeout=30,
        ),
    )


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
        logger.warning("cloud_persistence: incomplete R2 credentials — skipping restore")
        return

    key = _object_key()
    bucket = _Config.r2_bucket
    logger.info("cloud_persistence: restore begin bucket=%s key=%s", bucket, key)

    try:
        s3 = _make_s3_client()
        buf = io.BytesIO()
        s3.download_fileobj(bucket, key, buf)
        buf.seek(0)
        size = buf.getbuffer().nbytes
    except Exception as exc:
        if "NoSuchKey" in repr(exc) or getattr(getattr(exc, "response", None), "get", lambda *_: None)("Error", {}).get("Code") == "NoSuchKey":
            logger.info("cloud_persistence: no prior state for user_id=%s — fresh start", _Config.user_id)
        else:
            logger.warning("cloud_persistence: restore failed (%s) — continuing with empty state", exc)
        return

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=buf, mode="r:gz") as tf:
            _safe_extract(tf, _DATA_DIR)
        logger.info("cloud_persistence: restore complete — %d bytes extracted to %s", size, _DATA_DIR)
    except Exception as exc:
        logger.error("cloud_persistence: tarball extraction failed: %s — state may be partial", exc)


async def start_background_flush() -> Optional[asyncio.Task]:
    """Start the periodic flush task. Returns the task handle (or None if disabled)."""
    _Config.load()
    if not _Config.enabled:
        logger.info("cloud_persistence disabled — no background flush")
        return None
    if not _all_creds_present():
        logger.warning("cloud_persistence: incomplete R2 credentials — no background flush")
        return None
    task = asyncio.create_task(_flush_loop())
    # SIGTERM/SIGINT handler: one last flush before shutdown so the most
    # recent state persists. Important because CF Containers SIGTERM on
    # eviction or deploy — we need the final flush to win.
    _register_shutdown_handler()
    logger.info(
        "cloud_persistence: background flush started (interval=%.1fs, bucket=%s, key=%s)",
        _Config.flush_interval_s, _Config.r2_bucket, _object_key(),
    )
    return task


async def flush(reason: str = "periodic") -> bool:
    """Tar /app/data/ and upload to R2. Returns True if upload happened, False if skipped."""
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

    size = buf.getbuffer().nbytes
    try:
        s3 = _make_s3_client()
        buf.seek(0)
        s3.upload_fileobj(
            buf,
            _Config.r2_bucket,
            _object_key(),
            ExtraArgs={"ContentType": "application/gzip"},
        )
        logger.info("cloud_persistence: flush ok reason=%s size=%d", reason, size)
        return True
    except Exception as exc:
        logger.warning("cloud_persistence: flush upload failed (%s, %d bytes, reason=%s)", exc, size, reason)
        return False


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
        # Best effort: schedule the async flush. If the loop is already closed,
        # do a sync variant via a new loop.
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_do_flush())
                return
        except RuntimeError:
            pass
        # No running loop — run synchronously.
        asyncio.run(_do_flush())

    try:
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
    except ValueError:
        # Signal handlers can only be set on the main thread. In uvicorn
        # reload mode this function may be called on a subprocess — silently
        # skip; the parent process holds the real handler.
        pass


async def _flush_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_Config.flush_interval_s)
        except asyncio.CancelledError:
            break

        # Skip if nothing changed since the last flush — cheap by checking
        # (latest mtime, total bytes) signature.
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
                # File disappeared mid-tar (e.g. rotating log) — skip.
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


def _all_creds_present() -> bool:
    return all([
        _Config.user_id,
        _Config.r2_bucket,
        _Config.r2_endpoint,
        _Config.r2_access_key_id,
        _Config.r2_secret,
    ])


__all__ = ["restore_sync", "start_background_flush", "flush"]
