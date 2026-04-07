"""Progress reporting interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(slots=True)
class ProgressEvent:
    """Represents a single progress update."""

    session_id: str
    progress_percent: int
    bytes_written: int
    eta_seconds: Optional[int]
    message: str


class ProgressReporter(Protocol):
    """Protocol for receiving progress events."""

    def __call__(self, event: ProgressEvent) -> None:
        ...
