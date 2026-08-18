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
    identity: dict[str, Any] | None
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
            identity=None,
            revision=0,
        )

    def publish(
        self,
        state: PresenceState,
        reason: str,
        observed_at: datetime,
        metrics: dict[str, Any],
        identity: dict[str, Any] | None = None,
    ) -> PresenceSnapshot:
        with self._lock:
            current = self._snapshot
            identity_changed = (
                (current.identity or {}).get("state")
                != (identity or {}).get("state")
            )
            if state is current.state and not identity_changed:
                updated = PresenceSnapshot(
                    state=current.state,
                    previous_state=current.previous_state,
                    changed=current.changed,
                    reason=current.reason,
                    observed_at=observed_at,
                    metrics=deepcopy(metrics),
                    identity=deepcopy(identity),
                    revision=current.revision,
                )
            else:
                presence_changed = state is not current.state
                identity_initialized = current.identity is None and identity is not None
                published_identity = deepcopy(identity)
                if presence_changed and not identity_changed and published_identity is not None:
                    published_identity["previous_state"] = published_identity["state"]
                    published_identity["changed"] = False
                updated = PresenceSnapshot(
                    state=state,
                    previous_state=(
                        current.state if presence_changed else state
                    ),
                    changed=presence_changed,
                    reason=(
                        reason
                        if presence_changed
                        else current.reason
                        if identity_initialized
                        else "identity_changed"
                    ),
                    observed_at=observed_at,
                    metrics=deepcopy(metrics),
                    identity=published_identity,
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
            identity=deepcopy(snapshot.identity),
            revision=snapshot.revision,
        )
