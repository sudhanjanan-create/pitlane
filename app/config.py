import os

from app.memory import RunStore
from app.pitlane_db import PitLaneDb

AGENT_DB = os.environ.get("AGENT_DB", "agent.db")
PITLANE_DB = os.environ.get("PITLANE_DB", "pitlane.db")


def open_stores() -> tuple[RunStore, PitLaneDb]:
    """Open (and create, on a clean machine) the two separate databases: agent runtime and PitLane domain."""
    store, db = RunStore(AGENT_DB), PitLaneDb(PITLANE_DB)
    store.migrate()
    db.migrate()
    return store, db


def make_providers(mock: bool = True, slow: float = 0.0) -> dict:
    """One provider per agent. Only the scripted provider ships with this project (no API key needed)."""
    if not mock:
        raise NotImplementedError("Only the scripted provider is included; call make_providers(mock=True).")
    from app.providers import demo_providers

    return demo_providers(slow)
