"""Session management utilities for USB writer implementations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, Iterable, Optional

from .models import WriteIntent, WriteSession, WriteStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SessionUpdate:
    """Lightweight update payload."""

    progress_percent: Optional[int] = None
    bytes_written: Optional[int] = None
    eta_seconds: Optional[int] = None
    message: Optional[str] = None
    status: Optional[WriteStatus] = None
    notification_id: Optional[str] = None

class DeviceBusyError(RuntimeError):
    """Raised when attempting to start a write on a busy device."""

    def __init__(self, device_id: str, owner_session: str) -> None:
        super().__init__(f"Device {device_id} is busy (session {owner_session} is active)")
        self.device_id = device_id
        self.owner_session = owner_session


class SessionStore:
    """Thread-safe in-memory session registry."""

    def __init__(self) -> None:
        self._sessions: Dict[str, WriteSession] = {}
        self._lock = Lock()
        self._device_claims: Dict[str, str] = {}

    def create(self, session_id: str, intent: WriteIntent, *, force: bool = False) -> WriteSession:
        now = _utcnow()
        session = WriteSession(
            session_id=session_id,
            status=WriteStatus.PENDING,
            progress_percent=0,
            bytes_written=0,
            eta_seconds=None,
            message="Pending",
            started_at=now,
            updated_at=now,
            device_id=intent.device_id,
            iso_source=intent.iso_source,
            artifacts={},
        )
        with self._lock:
            if intent.device_id:
                self._claim_device_locked(intent.device_id, session_id, force=force)
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Optional[WriteSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def list(self) -> Iterable[WriteSession]:
        with self._lock:
            return list(self._sessions.values())

    def update(self, session_id: str, update: SessionUpdate) -> Optional[WriteSession]:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return None

            values = asdict(session)
            if update.progress_percent is not None:
                values["progress_percent"] = max(0, min(100, update.progress_percent))
            if update.bytes_written is not None:
                values["bytes_written"] = max(0, update.bytes_written)
            if update.eta_seconds is not None:
                values["eta_seconds"] = max(0, update.eta_seconds)
            if update.message is not None:
                values["message"] = update.message
            if update.status is not None:
                values["status"] = update.status
            if update.notification_id is not None:
                values["notification_id"] = update.notification_id

            values["updated_at"] = _utcnow()
            session = WriteSession(**values)
            self._sessions[session_id] = session

            if session.status in (WriteStatus.COMPLETED, WriteStatus.FAILED, WriteStatus.CANCELLED):
                self._release_device_locked(session_id)

            return session

    def mark_completed(self, session_id: str, message: str = "Completed") -> Optional[WriteSession]:
        return self.update(
            session_id,
            SessionUpdate(
                status=WriteStatus.COMPLETED,
                progress_percent=100,
                message=message,
            ),
        )

    def mark_failed(self, session_id: str, message: str) -> Optional[WriteSession]:
        return self.update(
            session_id,
            SessionUpdate(
                status=WriteStatus.FAILED,
                message=message,
            ),
        )

    def mark_cancelled(self, session_id: str, message: str = "Cancelled") -> Optional[WriteSession]:
        return self.update(
            session_id,
            SessionUpdate(
                status=WriteStatus.CANCELLED,
                message=message,
            ),
        )

    def is_device_busy(self, device_id: str) -> bool:
        with self._lock:
            owner = self._device_claims.get(device_id)
            if not owner:
                return False
            session = self._sessions.get(owner)
            if not session:
                # Cleanup dangling claim
                del self._device_claims[device_id]
                return False
            if session.status in (WriteStatus.COMPLETED, WriteStatus.FAILED, WriteStatus.CANCELLED):
                del self._device_claims[device_id]
                return False
            return True

    def get_active_session_for_device(self, device_id: str) -> Optional[WriteSession]:
        with self._lock:
            owner = self._device_claims.get(device_id)
            if not owner:
                return None
            return self._sessions.get(owner)

    def release(self, session_id: str) -> None:
        with self._lock:
            self._release_device_locked(session_id)

    # Internal helpers -------------------------------------------------

    def _claim_device_locked(self, device_id: str, session_id: str, *, force: bool) -> None:
        owner = self._device_claims.get(device_id)
        if owner and owner != session_id:
            if not force:
                raise DeviceBusyError(device_id, owner)
        self._device_claims[device_id] = session_id

    def _release_device_locked(self, session_id: str) -> None:
        to_remove = [dev for dev, owner in self._device_claims.items() if owner == session_id]
        for device_id in to_remove:
            self._device_claims.pop(device_id, None)
