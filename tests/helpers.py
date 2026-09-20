"""Shared test helpers: a controllable clock and a tiny way to push a request through the whole stack."""
from app.memory import RunStore
from app.pitlane_db import PitLaneDb
from app.providers import ModelTurn, ToolCall, demo_providers
from app.worker import Worker


class FakeClock:
    """Deterministic time, so lease expiry is tested by advancing the clock, not by sleeping."""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Scripted:
    """A provider that plays back fixed turns and records which tools it was offered."""

    model = "test"

    def __init__(self, turns: list[ModelTurn]):
        self.turns, self.offered = list(turns), []

    def generate(self, system, contents, tools):
        self.offered.append([t.__name__ for t in tools])
        return self.turns.pop(0)


def calls(name: str, **args) -> ModelTurn:
    return ModelTurn(text=None, tool_calls=[ToolCall(name, args)])


def fresh_stores(clock=None):
    store = RunStore(":memory:", clock=clock) if clock else RunStore(":memory:")
    store.migrate()
    db = PitLaneDb(":memory:")
    db.migrate()
    return store, db


def enqueue(store: RunStore, driver: str, text: str, **kw) -> tuple[str, str]:
    thread_id = store.create_thread(driver)
    return thread_id, store.enqueue(thread_id, text, "mock", **kw)


def run_request(store: RunStore, db: PitLaneDb, driver: str, text: str, providers: dict | None = None):
    """Whole path: queue -> worker (lease) -> supervisor -> specialists -> tools -> DB. Returns (run, events)."""
    thread_id, run_id = enqueue(store, driver, text)
    events: list[dict] = []
    worker = Worker(store, db, providers or demo_providers(), worker_id="test-worker", lease_seconds=30.0,
                    on_step=events.append)
    worker.run_until_idle()
    return store.get_run(run_id), events


def booking_tools(events: list[dict]) -> list[str]:
    return [e["tool"] for e in events if e["agent"] == "booking" and e["kind"] == "tool"]
