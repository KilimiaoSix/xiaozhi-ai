from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import threading
from typing import Any

from presence_agent.state import PresenceState


@dataclass(frozen=True)
class PresenceSnapshot:
    state: PresenceState
    previous_state: PresenceState
    changed: bool
    reason: str
    observed_at: datetime
    metrics: dict[str, Any]
    revision: int


class LatestSnapshot:
    def __init__(self, observed_at: datetime) -> None:
        self._lock = threading.Lock()
        self._snapshot = PresenceSnapshot(
            state=PresenceState.STARTING,
            previous_state=PresenceState.STARTING,
            changed=False,
            reason="initializing",
            observed_at=observed_at,
            metrics={},
            revision=0,
        )

    def publish(
        self,
        state: PresenceState,
        reason: str,
        observed_at: datetime,
        metrics: dict[str, Any],
    ) -> PresenceSnapshot:
        with self._lock:
            current = self._snapshot
            if state is current.state:
                updated = PresenceSnapshot(
                    state=current.state,
                    previous_state=current.previous_state,
                    changed=current.changed,
                    reason=current.reason,
                    observed_at=observed_at,
                    metrics=deepcopy(metrics),
                    revision=current.revision,
                )
            else:
                updated = PresenceSnapshot(
                    state=state,
                    previous_state=current.state,
                    changed=True,
                    reason=reason,
                    observed_at=observed_at,
                    metrics=deepcopy(metrics),
                    revision=current.revision + 1,
                )
            self._snapshot = updated
            return self._copy(updated)

    def read(self) -> PresenceSnapshot:
        with self._lock:
            return self._copy(self._snapshot)

    @staticmethod
    def _copy(snapshot: PresenceSnapshot) -> PresenceSnapshot:
        return PresenceSnapshot(
            state=snapshot.state,
            previous_state=snapshot.previous_state,
            changed=snapshot.changed,
            reason=snapshot.reason,
            observed_at=snapshot.observed_at,
            metrics=deepcopy(snapshot.metrics),
            revision=snapshot.revision,
        )
