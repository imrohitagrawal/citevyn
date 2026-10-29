"""The orchestrator as unit tests and the eval harness use it.

They pass bare session ids (``uuid.uuid4()``) and want the ``demo_user`` and
Session rows made for them. The production default refuses that: a missing
Session row there means it was deleted mid-request (#511 review F1), and
``Orchestrator._ensure_user`` raises ``SessionGone``. This subclass only turns
the old convenience back on, explicitly, for code that is not a route.
"""

from __future__ import annotations

from typing import Any

from app.answer.orchestrator import Orchestrator as _ProductionOrchestrator


class Orchestrator(_ProductionOrchestrator):
    """``Orchestrator`` with ``create_missing_session=True`` by default."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("create_missing_session", True)
        super().__init__(*args, **kwargs)


__all__ = ["Orchestrator"]
