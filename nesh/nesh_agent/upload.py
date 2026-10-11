"""Optional outbound upload of observations.

Rows are appended to a spool file as they are written. Every interval the spool
is sealed into a gzip batch in <data>/upload_queue/, and queued batches are
POSTed oldest first. A batch is deleted only after a 2xx reply, so nothing is
lost if the network or the server is down. Outbound only: nothing listens.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import os
import random
import time
from pathlib import Path

import aiohttp

from .contract import CONTRACT_VERSION

log = logging.getLogger("nesh.upload")

MAX_QUEUE_FILES = 20000


class Spool:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "upload_spool.ndjson"
        self.queue_dir = Path(data_dir) / "upload_queue"
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self._seq = 0

    def append(self, lines: list[str]) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError as exc:
            log.error("Could not append to upload spool: %s", exc)

    def seal(self) -> Path | None:
        """Move the current spool into a gzip batch. Returns the batch path."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        self._seq += 1
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        sealing = self.path.with_name(f"upload_spool.{stamp}.{os.getpid()}.{self._seq}.sealing")
        os.replace(self.path, sealing)
        batch = self.queue_dir / f"batch-{stamp}-{self._seq:06d}.ndjson.gz"
        tmp = batch.with_name(batch.name + ".tmp")
        with open(sealing, "rb") as src, gzip.open(tmp, "wb") as dst:
            dst.write(src.read())
        os.replace(tmp, batch)
        sealing.unlink(missing_ok=True)
        self._trim()
        return batch

    def recover(self) -> None:
        """Re-queue any spool left half sealed by a crash."""
        for leftover in self.path.parent.glob("upload_spool.*.sealing"):
            try:
                with open(leftover, encoding="utf-8") as fh:
                    lines = [l.rstrip("\n") for l in fh if l.strip()]
                if lines:
                    self.append(lines)
                leftover.unlink()
            except OSError as exc:
                log.warning("Could not recover %s: %s", leftover, exc)

    def pending(self) -> list[Path]:
        return sorted(self.queue_dir.glob("batch-*.ndjson.gz"))

    def _trim(self) -> None:
        files = self.pending()
        if len(files) > MAX_QUEUE_FILES:
            for old in files[: len(files) - MAX_QUEUE_FILES]:
                log.warning("Upload queue full; dropping oldest batch %s (data is still in /data/obs)", old.name)
                old.unlink(missing_ok=True)


class Uploader:
    def __init__(self, spool: Spool, url: str, home_id: str, token: str = "",
                 interval: float = 300, max_backoff: float = 3600):
        self.spool = spool
        self.url = url
        self.home_id = home_id
        self.token = token
        self.interval = interval
        self.max_backoff = max_backoff
        self.sent_batches = 0

    async def send_pending(self, session: aiohttp.ClientSession) -> bool:
        """Send all queued batches. Returns False at the first failure."""
        for batch in self.spool.pending():
            body = batch.read_bytes()
            headers = {
                "Content-Type": "application/x-ndjson",
                "Content-Encoding": "gzip",
                "X-Nesh-Home-Id": self.home_id,
                "X-Nesh-Contract": CONTRACT_VERSION,
                "X-Nesh-Batch": batch.name,
            }
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            try:
                async with session.post(self.url, data=body, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if 200 <= resp.status < 300:
                        batch.unlink(missing_ok=True)
                        self.sent_batches += 1
                        continue
                    log.warning("Upload of %s refused with HTTP %s; will retry", batch.name, resp.status)
                    return False
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                log.warning("Upload of %s failed (%s); will retry", batch.name, type(exc).__name__)
                return False
        return True

    async def run(self, session: aiohttp.ClientSession, stop: asyncio.Event) -> None:
        self.spool.recover()
        delay = self.interval
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            try:
                self.spool.seal()
                ok = await self.send_pending(session)
            except Exception as exc:
                log.warning("Upload cycle error: %s", exc)
                ok = False
            delay = self.interval if ok else min(max(delay * 2, self.interval), self.max_backoff)
            delay *= random.uniform(0.9, 1.1)
        # final attempt so a clean shutdown does not leave rows only in the spool
        try:
            self.spool.seal()
        except OSError:
            pass
