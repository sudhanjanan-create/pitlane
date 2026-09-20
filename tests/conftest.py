"""Shared fixtures for tests."""
import pytest
from app.memory import RunStore
from app.pitlane_db import PitLaneDb


@pytest.fixture
def agent_db():
    """Fresh agent database for each test."""
    store = RunStore(":memory:")
    store.migrate()
    return store


@pytest.fixture
def domain_db():
    """Fresh domain database for each test."""
    db = PitLaneDb(":memory:")
    db.migrate()
    return db


@pytest.fixture
def both_dbs(agent_db, domain_db):
    """Both databases together."""
    return {"agent": agent_db, "domain": domain_db}
