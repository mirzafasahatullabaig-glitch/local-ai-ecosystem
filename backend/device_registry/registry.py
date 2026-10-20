"""Tracks which devices exist, what they can do, and whether they are alive.

Staleness is derived from last_seen against a caller-supplied ``now_ms`` so it
is testable without sleeping. Devices are always chosen by capability, never by
a hardcoded name.
"""
from __future__ import annotations

from schemas.messages import Manifest, now_ms
from backend.storage.sqlite_repo import Repository

STALE_MS = 15_000  # a device with no heartbeat for 15 s is stale


class RegistrationError(ValueError):
    """Raised when a manifest cannot be registered."""


class DeviceRegistry:
    def __init__(self, repo: Repository, stale_ms: int = STALE_MS) -> None:
        self._repo = repo
        self._stale_ms = stale_ms

    def register(self, manifest: Manifest) -> dict:
        """Upsert a device from its manifest. Rejects protocol != 1."""
        if manifest.protocol_version != 1:
            raise RegistrationError(
                f"unsupported protocol_version {manifest.protocol_version}; expected 1"
            )
        data = manifest.model_dump(mode="json")
        data["online"] = True
        self._repo.upsert_device(manifest.device_id, data)
        return data

    def heartbeat(self, device_id: str, at_ms: int | None = None) -> None:
        """Mark the device seen now (keeps it fresh)."""
        self._repo.set_device_last_seen(device_id, at_ms if at_ms is not None else now_ms())

    def mark_offline(self, device_id: str) -> None:
        dev = self._repo.get_device(device_id)
        if dev is not None:
            dev["online"] = False
            self._repo.upsert_device(device_id, dev)

    def list(self, now: int | None = None) -> list[dict]:
        """All devices, each annotated with a computed ``stale`` flag."""
        now = now if now is not None else now_ms()
        out = []
        for dev in self._repo.list_devices():
            dev["stale"] = (now - dev.get("last_seen", 0)) > self._stale_ms
            out.append(dev)
        return out

    def find_by_capability(self, capability: str, now: int | None = None) -> list[dict]:
        """Live (non-stale, online) devices offering a capability by name."""
        now = now if now is not None else now_ms()
        matches = []
        for dev in self.list(now=now):
            if dev.get("stale") or not dev.get("online", True):
                continue
            names = {c["name"] for c in dev.get("capabilities", [])}
            if capability in names:
                matches.append(dev)
        return matches
