"""PitLane demo: end-to-end track booking workflow with scripted models (no API key needed).

Every request goes through the real path: queue -> worker (lease) -> supervisor -> specialist -> tools -> DB.
The closing summary is computed from the databases, and the demo fails loudly if it does not hold.
"""
import json
import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.memory import RunStore
from app.pitlane_db import PitLaneDb
from app.providers import demo_providers
from app.worker import Worker


def print_section(title: str) -> None:
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}\n")


def trace(event: dict) -> None:
    """Print each delegation and tool call as it happens."""
    if event["kind"] == "delegate":
        print(f"      supervisor -> {event['tool']}")
    elif event["agent"] != "supervisor":
        text = json.dumps(event["result"], default=str)
        print(f"        [{event['agent']}] {event['tool']} -> {text[:100]}{'...' if len(text) > 100 else ''}")


def demo() -> None:
    print_section("PITLANE TRACK SESSION AGENT — DEMO")

    # Two separate databases: agent runtime (queue, runs, memory) and PitLane domain (business data).
    print("1. Initializing the two databases (agent runtime + PitLane domain)...")
    store = RunStore(":memory:")
    store.migrate()
    db = PitLaneDb(":memory:")
    db.migrate()
    print("   ✓ Databases ready")

    print("\n2. Drivers:")
    for driver in db.list_drivers():
        print(f"   - {driver['name']} ({driver['license_level']})")
    print("\n3. Cars:")
    for car in db.list_cars():
        print(f"   - {car['name']} (requires {car['min_license_level']})")

    worker = Worker(store, db, demo_providers(), worker_id="demo-worker", lease_seconds=30.0, on_step=trace)

    def ask(driver: str, request: str) -> str:
        print(f"\n   {driver}: {request!r}")
        thread_id = store.create_thread(driver)
        run_id = store.enqueue(thread_id, request, "mock")
        (done_id, outcome), = worker.run_until_idle()
        assert done_id == run_id and outcome == "succeeded", (done_id, outcome)
        reply = store.load_history(thread_id)[-1]["text"]
        print(f"      run {outcome} -> agent replied: {reply}")
        return reply

    print_section("SCENARIO A: eligible driver books the GT3 for 16:00")
    ask("Alice Johnson", "Book the GT3 for tomorrow at 16:00")

    print_section("SCENARIO B: novice driver asks for the Formula-3 (needs advanced)")
    reply_b = ask("Charlie Brown", "Book the Formula-3 for tomorrow at 17:00")

    print_section("SCENARIO C: another driver wants the slot Alice already holds")
    reply_c = ask("Bob Smith", "Book the GT3 for tomorrow at 16:00")

    print_section("SCENARIO D: Alice cancels, then Bob can take the freed slot")
    ask("Alice Johnson", "Please cancel my booking")
    ask("Bob Smith", "Book the GT3 for tomorrow at 16:00")

    print_section("FINAL STATE (read from the databases)")
    bookings = db.list_bookings()
    print("   Active bookings:")
    for b in bookings:
        print(f"   ✓ {b['driver_name']}: {b['car_name']} at {b['slot_time']}")
    all_rows = db.conn.execute(
        "SELECT b.id, d.name, b.status FROM booking b JOIN driver d ON d.id = b.driver_id ORDER BY b.id").fetchall()
    print("   All booking rows: " + ", ".join(f"#{r['id']} {r['name']} [{r['status']}]" for r in all_rows))
    notes = db.conn.execute(
        "SELECT d.name, n.message FROM notification n JOIN driver d ON d.id = n.driver_id ORDER BY n.id").fetchall()
    print("   Notifications:")
    for n in notes:
        print(f"   → {n['name']}: {n['message']}")

    # The claims below are checked, not just printed.
    assert [(b["driver_name"], b["slot_time"]) for b in bookings] == [("Bob Smith", "2026-09-20T16:00:00")]
    assert [(r["id"], r["status"]) for r in all_rows] == [(1, "cancelled"), (2, "active")]
    assert "not book" in reply_b.lower() or "not booked" in reply_b.lower(), reply_b
    assert "already booked" in reply_c, reply_c
    assert [n["name"] for n in notes] == ["Alice Johnson", "Alice Johnson", "Bob Smith"]
    assert not any(n["name"] == "Charlie Brown" for n in notes)

    print_section("DEMO COMPLETE")
    print("✓ Eligible booking placed, confirmation recorded")
    print("✓ Ineligible driver (novice -> advanced car) was not booked and not notified")
    print("✓ Second active booking on a taken slot was rejected by the database")
    print("✓ Cancelling freed the slot, which could then be booked again")
    print()


if __name__ == "__main__":
    demo()
