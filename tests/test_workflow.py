"""Behaviour tests. These drive the real stack (queue -> worker -> supervisor -> specialist -> tool -> SQLite)
rather than calling database helpers in isolation."""
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app import config
from app.agents import SupervisorTools, run_specialist, run_tool, VEHICLE_SPECIALIST_SYSTEM
from app.crash import SimulatedCrash, crash_after
from app.idempotency import idempotency_key
from app.memory import RunStore
from app.pitlane_db import PitLaneDb
from app.providers import ModelTurn, demo_providers
from app.tools.pitlane_tools import BookingSpecialistTools, VehicleSpecialistTools
from app.worker import Worker
from tests.helpers import FakeClock, Scripted, booking_tools, calls, enqueue, fresh_stores, run_request

ROOT = Path(__file__).resolve().parent.parent
SLOT_16 = "2026-09-20T16:00:00"
SLOT_17 = "2026-09-20T17:00:00"


@pytest.fixture
def db():
    d = PitLaneDb(":memory:")
    d.migrate()
    return d


# ======================================================================== eligibility
class TestEligibility:
    def test_eligible_driver_can_book(self, db):
        bob = BookingSpecialistTools(db, "Bob Smith")               # intermediate license, GT3 needs intermediate
        result = bob.book_track_session(car_id=1, slot_time=SLOT_16)
        assert result["status"] == "booked" and result["driver_name"] == "Bob Smith"
        assert db.count("booking") == 1

    def test_ineligible_driver_cannot_book_at_service_layer(self, db):
        result, created = db.book_session(3, 2, 1)                   # Charlie (novice) -> Formula-3 (advanced)
        assert created is False
        assert result["error"] == "not_eligible" and "advanced" in result["hint"]
        assert db.count("booking") == 0

    def test_booking_tool_refuses_ineligible_driver_with_no_prior_eligibility_check(self, db):
        charlie = BookingSpecialistTools(db, "Charlie Brown")
        result = charlie.book_track_session(car_id=2, slot_time=SLOT_17)   # straight to booking, no check
        assert result["error"] == "not_eligible"
        assert db.count("booking") == 0 and db.count("notification") == 0

    def test_scripted_model_that_skips_eligibility_cannot_bypass_the_rule(self):
        """The booking specialist's model goes straight to book_track_session for an ineligible driver."""
        store, db = fresh_stores()
        providers = demo_providers()
        providers["booking"] = Scripted([
            calls("book_track_session", car_id=2, slot_time=SLOT_17),
            ModelTurn(text="Booked!"),
        ])
        run, events = run_request(store, db, "Charlie Brown", "Book the Formula-3 for tomorrow at 17:00", providers)

        assert booking_tools(events) == ["book_track_session"]            # eligibility check was skipped...
        attempt = next(e for e in events if e.get("tool") == "book_track_session")
        assert attempt["result"]["error"] == "not_eligible"                # ...and the tool refused anyway
        assert db.count("booking") == 0

    def test_default_booking_workflow_checks_eligibility_before_booking(self):
        store, db = fresh_stores()
        run, events = run_request(store, db, "Alice Johnson", "Book the GT3 for tomorrow at 16:00")
        assert run["status"] == "succeeded"
        assert booking_tools(events) == ["get_driver_info", "check_driver_eligibility",
                                         "book_track_session", "send_notification"]
        assert db.count("booking") == 1 and db.count("notification") == 1

    def test_default_booking_workflow_stops_when_driver_is_ineligible(self):
        store, db = fresh_stores()
        run, events = run_request(store, db, "Charlie Brown", "Book the Formula-3 for tomorrow at 17:00")
        assert run["status"] == "succeeded"
        assert booking_tools(events) == ["get_driver_info", "check_driver_eligibility"]   # no book, no notify
        assert db.count("booking") == 0 and db.count("notification") == 0
        assert store.load_history(run["thread_id"])[-1]["text"].startswith("Not booked")


# ======================================================================== database-enforced slot rule
class TestTrackSlotConstraint:
    def test_database_itself_rejects_a_second_active_booking_for_a_slot(self, db):
        insert = "INSERT INTO booking (driver_id, car_id, track_slot_id, status, created_at) VALUES (?, 1, 1, 'active', 0)"
        db.conn.execute(insert, (1,))
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            db.conn.execute(insert, (2,))                            # raw SQL: no Python business logic involved
        assert db.count("booking") == 1

    def test_cancelled_booking_frees_the_slot_for_someone_else(self, db):
        first, _ = db.book_session(1, 1, 1)
        db.cancel_booking(first["id"])
        second, created = db.book_session(2, 1, 1)
        assert created is True and second["status"] == "active"
        statuses = sorted(r["status"] for r in db.conn.execute("SELECT status FROM booking"))
        assert statuses == ["active", "cancelled"]

    def test_concurrent_bookings_for_the_same_slot_only_one_wins(self, tmp_path):
        path = str(tmp_path / "pitlane.db")
        PitLaneDb(path).migrate()
        barrier, outcomes = threading.Barrier(2), []

        def attempt(driver_id):
            mine = PitLaneDb(path)                                   # own connection, like a separate worker
            barrier.wait()
            outcomes.append(mine.book_session(driver_id, 1, 1))

        threads = [threading.Thread(target=attempt, args=(d,)) for d in (1, 2)]
        [t.start() for t in threads]
        [t.join() for t in threads]

        assert sorted(created for _, created in outcomes) == [False, True]
        assert PitLaneDb(path).count("booking") == 1


# ======================================================================== idempotent side effects
class TestIdempotentSideEffects:
    def test_booking_tool_runs_once_per_key(self, db):
        tools = BookingSpecialistTools(db, "Alice Johnson")
        key = idempotency_key("run-1", 4, "book_track_session", {"car_id": 1, "slot_time": SLOT_16})
        args = {"car_id": 1, "slot_time": SLOT_16}

        first, replayed1 = run_tool(tools, db, key, "book_track_session", args)
        second, replayed2 = run_tool(tools, db, key, "book_track_session", args)

        assert (replayed1, replayed2) == (False, True)
        assert second == first and db.count("booking") == 1

    def test_notification_tool_runs_once_per_key(self, db):
        tools = BookingSpecialistTools(db, "Alice Johnson")
        args = {"message": "Your track session is confirmed"}

        first, replayed1 = run_tool(tools, db, "key-1", "send_notification", args)
        second, replayed2 = run_tool(tools, db, "key-1", "send_notification", args)

        assert first["status"] == "sent" and (replayed1, replayed2) == (False, True)
        assert second == first and db.count("notification") == 1

    def test_same_message_same_day_is_one_notification_even_under_a_new_key(self, db):
        """Second layer: the notification's own dedupe key, independent of the run's idempotency key."""
        tools = BookingSpecialistTools(db, "Alice Johnson")
        args = {"message": "Reminder: track day tomorrow"}
        run_tool(tools, db, "key-a", "send_notification", args)
        again, replayed = run_tool(tools, db, "key-b", "send_notification", args)
        assert replayed is False and again["duplicate"] is True
        assert db.count("notification") == 1

    def test_once_does_not_rerun_the_effect(self, db):
        ran = []
        effect = lambda: ran.append(1) or {"n": len(ran)}
        assert db.once("k", "t", effect) == ({"n": 1}, True)
        assert db.once("k", "t", effect) == ({"n": 1}, False)
        assert ran == [1]

    def test_idempotency_keys_survive_a_reconnect(self, tmp_path):
        path = str(tmp_path / "pitlane.db")
        first = PitLaneDb(path)
        first.migrate()
        first.once("k", "t", lambda: {"v": 1})
        second = PitLaneDb(path)                                     # a different process would do the same
        result, fresh = second.once("k", "t", lambda: {"v": 2})
        assert (result, fresh) == ({"v": 1}, False)

    def test_failed_effect_leaves_no_key_so_a_retry_can_still_run(self, db):
        def boom():
            raise RuntimeError("crashed mid-effect")

        with pytest.raises(RuntimeError):
            db.once("k", "t", boom)
        assert db.count("idempotency") == 0
        assert db.once("k", "t", lambda: {"ok": True}) == ({"ok": True}, True)

    def test_booking_the_same_slot_twice_for_the_same_driver_returns_the_existing_booking(self, db):
        tools = BookingSpecialistTools(db, "Alice Johnson")
        first = tools.book_track_session(car_id=1, slot_time=SLOT_16)
        again = tools.book_track_session(car_id=1, slot_time=SLOT_16)
        assert first["status"] == "booked" and again["status"] == "already_booked"
        assert again["booking_id"] == first["booking_id"] and db.count("booking") == 1


# ======================================================================== least privilege
class TestLeastPrivilege:
    def test_vehicle_specialist_is_only_offered_read_only_tools(self, db):
        model = Scripted([ModelTurn(text="nothing to do")])
        run_specialist("vehicle", VEHICLE_SPECIALIST_SYSTEM, VehicleSpecialistTools(db), db=db, provider=model,
                       task="hello", parent_key="k")
        offered = set(model.offered[0])
        assert offered == {"list_cars", "get_car_details", "check_driver_eligibility", "list_track_slots"}
        assert not offered & {"book_track_session", "cancel_track_session", "send_notification"}

    def test_vehicle_specialist_cannot_change_anything_even_if_its_model_tries(self, db):
        db.book_session(1, 1, 1)                                     # an existing booking that must survive
        model = Scripted([
            calls("book_track_session", car_id=1, slot_time=SLOT_17),
            calls("cancel_track_session", booking_id=1),
            calls("send_notification", message="hi"),
            ModelTurn(text="done"),
        ])
        events = []
        run_specialist("vehicle", VEHICLE_SPECIALIST_SYSTEM, VehicleSpecialistTools(db), db=db, provider=model,
                       task="please book", parent_key="k", on_step=events.append)

        assert [e["result"]["error"] for e in events] == ["unknown_tool"] * 3
        assert db.count("booking") == 1 and db.get_booking(1)["status"] == "active"
        assert db.count("notification") == 0 and db.count("idempotency") == 0

    def test_supervisor_has_no_domain_tools_only_delegation(self, db):
        supervisor = SupervisorTools(db, demo_providers(), "Alice Johnson")
        assert set(supervisor.functions()) == {"ask_vehicle_specialist", "ask_booking_specialist"}
        assert supervisor.SIDE_EFFECTS == ()

    def test_a_driver_cannot_cancel_someone_elses_booking(self, db):
        alice_booking, _ = db.book_session(1, 1, 1)
        result = BookingSpecialistTools(db, "Bob Smith").cancel_track_session(booking_id=alice_booking["id"])
        assert result["error"] == "not_your_booking"
        assert db.get_booking(alice_booking["id"])["status"] == "active"


# ======================================================================== real delegation, real persistence
class TestDelegation:
    def test_supervisor_delegates_to_two_separate_agent_loops(self):
        store, db = fresh_stores()
        providers = demo_providers()
        run, events = run_request(store, db, "Alice Johnson", "Book the GT3 for tomorrow at 16:00", providers)

        delegated = [e["tool"] for e in events if e["kind"] == "delegate"]
        assert delegated == ["ask_vehicle_specialist", "ask_booking_specialist"]
        assert {e["agent"] for e in events if e["kind"] == "tool"} == {"supervisor", "vehicle", "booking"}
        # each specialist ran its own model loop with its own provider
        assert providers["vehicle"].calls and providers["booking"].calls
        # the supervisor made no domain tool call itself
        supervisor_tools = {e["tool"] for e in events if e["kind"] == "tool" and e["agent"] == "supervisor"}
        assert supervisor_tools == {"ask_vehicle_specialist", "ask_booking_specialist"}

    def test_run_is_persisted_step_by_step_with_idempotency_keys(self):
        store, db = fresh_stores()
        run, _ = run_request(store, db, "Alice Johnson", "Book the GT3 for tomorrow at 16:00")
        assert [(s["kind"], s["tool_name"]) for s in run["steps"]] == [
            ("model", None), ("tool", "ask_vehicle_specialist"),
            ("model", None), ("tool", "ask_booking_specialist"), ("model", None)]
        keys = [r[0] for r in store.conn.execute("SELECT idempotency_key FROM tool_call")]
        assert len(keys) == 2 and all(keys) and len(set(keys)) == 2


# ======================================================================== queue, lease, recovery
class TestLeaseRecovery:
    def test_a_live_lease_cannot_be_taken_by_another_worker(self):
        clock = FakeClock()
        store, _ = fresh_stores(clock)
        enqueue(store, "Alice Johnson", "Book the GT3")
        assert store.claim_next("w1", 10.0) is not None
        clock.advance(9.0)                                            # still inside the lease
        assert store.reap_expired() == []
        assert store.claim_next("w2", 10.0) is None

    def test_heartbeat_keeps_a_slow_but_alive_worker_from_being_reaped(self):
        clock = FakeClock()
        store, _ = fresh_stores(clock)
        _, run_id = enqueue(store, "Alice Johnson", "Book the GT3")
        store.claim_next("w1", 10.0)
        clock.advance(6.0)
        assert store.heartbeat(run_id, "w1", 10.0) is True
        assert store.heartbeat(run_id, "someone-else", 10.0) is False
        clock.advance(6.0)                                            # past the ORIGINAL expiry
        assert store.reap_expired() == []

    def test_run_is_dead_lettered_after_max_attempts(self):
        clock = FakeClock()
        store, _ = fresh_stores(clock)
        _, run_id = enqueue(store, "Alice Johnson", "Book the GT3", max_attempts=2)
        for worker in ("w1", "w2"):
            assert store.claim_next(worker, 10.0) is not None
            clock.advance(11.0)
            store.reap_expired()
        run = store.get_run(run_id)
        assert run["status"] == "dead" and run["error_code"] == "lease_expired" and run["attempts"] == 2

    def test_a_worker_that_lost_its_lease_cannot_complete_the_run(self):
        clock = FakeClock()
        store, _ = fresh_stores(clock)
        thread_id, run_id = enqueue(store, "Alice Johnson", "Book the GT3")
        store.claim_next("w1", 10.0)
        clock.advance(11.0)
        store.reap_expired()
        store.claim_next("w2", 10.0)

        assert store.complete(run_id, "w1", "late answer from a zombie") is False
        assert store.complete(run_id, "w2", "real answer") is True
        replies = [m["text"] for m in store.load_history(thread_id) if m["role"] == "model"]
        assert replies == ["real answer"]


# ======================================================================== crash and replay
def crash_then_recover(tmp_path, driver, crash_agent, crash_tool):
    """Worker-1 dies right after `crash_agent`.`crash_tool` finishes; worker-2 recovers. Real files, real workers."""
    agent_path, pitlane_path = str(tmp_path / "agent.db"), str(tmp_path / "pitlane.db")
    clock = FakeClock()
    store, db = RunStore(agent_path, clock=clock), PitLaneDb(pitlane_path)
    store.migrate()
    db.migrate()
    thread_id, run_id = enqueue(store, driver, "Book the GT3 for tomorrow at 16:00")

    def die():
        raise SimulatedCrash()

    worker1 = Worker(store, db, demo_providers(), worker_id="w1", lease_seconds=10.0,
                     on_step=crash_after(crash_agent, crash_tool, die))
    with pytest.raises(SimulatedCrash):
        worker1.run_once()

    # worker-2 = new connections, as if a new process had started
    store2, db2 = RunStore(agent_path, clock=clock), PitLaneDb(pitlane_path)
    events = []
    worker2 = Worker(store2, db2, demo_providers(), worker_id="w2", lease_seconds=10.0, on_step=events.append)
    return store, db, store2, db2, worker2, events, thread_id, run_id, clock


class TestCrashAndReplay:
    def test_crash_after_both_side_effects_then_replay_completes_exactly_once(self, tmp_path):
        store, db, store2, db2, worker2, events, thread_id, run_id, clock = crash_then_recover(
            tmp_path, "Bob Smith", "booking", "send_notification")

        # what the dead worker left behind: real side effects, an unfinished run
        run = store.get_run(run_id)
        assert run["status"] == "running" and run["lease_owner"] == "w1" and run["attempts"] == 1
        assert db.count("booking") == 1 and db.count("notification") == 1
        assert not [s for s in run["steps"] if s["tool_name"] == "ask_booking_specialist"]
        assert not [m for m in store.load_history(thread_id) if m["role"] == "model"]

        # nobody can take it while the dead worker's lease is live; once it expires, worker-2 recovers it
        assert worker2.run_once() is None
        clock.advance(11.0)
        assert worker2.run_once() == (run_id, "succeeded")

        final = store2.get_run(run_id)
        assert final["status"] == "succeeded" and final["attempts"] == 2
        assert db2.count("booking") == 1 and db2.count("notification") == 1
        assert db2.count("idempotency") == 2                          # replay added no new keys
        replayed = sorted(e["tool"] for e in events if e["kind"] == "tool" and e["agent"] == "booking" and e["replayed"])
        assert replayed == ["book_track_session", "send_notification"]
        assert not [e for e in events if e["kind"] == "tool" and e["agent"] == "booking" and not e["replayed"]
                    and e["tool"] in ("book_track_session", "send_notification")]
        assert [m["role"] for m in store2.load_history(thread_id)] == ["user", "model"]

    def test_crash_between_the_booking_and_the_notification(self, tmp_path):
        """Booking already committed, notification not yet sent: replay must not rebook, and must send once."""
        store, db, store2, db2, worker2, events, thread_id, run_id, clock = crash_then_recover(
            tmp_path, "Bob Smith", "booking", "book_track_session")
        assert db.count("booking") == 1 and db.count("notification") == 0

        clock.advance(11.0)
        assert worker2.run_once() == (run_id, "succeeded")

        assert db2.count("booking") == 1 and db2.count("notification") == 1
        by_tool = {e["tool"]: e["replayed"] for e in events if e["kind"] == "tool" and e["agent"] == "booking"}
        assert by_tool["book_track_session"] is True                  # replayed from the stored result
        assert by_tool["send_notification"] is False                  # genuinely new: it had not happened yet

    def test_crash_before_any_side_effect(self, tmp_path):
        store, db, store2, db2, worker2, events, thread_id, run_id, clock = crash_then_recover(
            tmp_path, "Bob Smith", "vehicle", "list_track_slots")
        assert db.count("booking") == 0 and db.count("notification") == 0

        clock.advance(11.0)
        assert worker2.run_once() == (run_id, "succeeded")
        assert db2.count("booking") == 1 and db2.count("notification") == 1
        assert store2.get_run(run_id)["attempts"] == 2

    def test_crash_demo_script_runs_a_real_crash_and_passes(self):
        """The shipped demo: real child process, hard exit, lease expiry, replay, 18 checks, prints PASS."""
        done = subprocess.run([sys.executable, "-m", "scripts.crash_demo"], cwd=ROOT, capture_output=True,
                              text=True, timeout=120)
        assert done.returncode == 0, done.stdout + done.stderr
        lines = [ln for ln in done.stdout.splitlines() if ln.strip()]
        assert lines[-1] == "PASS" and "FAIL" not in done.stdout and "✗" not in done.stdout
        assert "exit code 137" in done.stdout and "REPLAYED" in done.stdout


# ======================================================================== clean machine
class TestCleanMachine:
    def test_two_separate_databases_initialise_from_nothing_and_migrate_is_repeatable(self, tmp_path):
        agent_path, pitlane_path = tmp_path / "agent.db", tmp_path / "pitlane.db"
        for _ in range(2):                                            # second pass must not duplicate the seed data
            store, db = RunStore(str(agent_path)), PitLaneDb(str(pitlane_path))
            store.migrate()
            db.migrate()
        assert agent_path.exists() and pitlane_path.exists() and agent_path != pitlane_path
        assert (db.count("driver"), db.count("car"), db.count("track_slot")) == (4, 4, 12)

        tables = lambda conn: {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"run", "run_step", "tool_call", "thread", "message"} <= tables(store.conn)
        assert {"booking", "notification", "idempotency", "driver"} <= tables(db.conn)
        assert not tables(store.conn) & tables(db.conn)               # nothing shared between the two

    def test_config_opens_both_databases_on_an_empty_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "AGENT_DB", str(tmp_path / "a.db"))
        monkeypatch.setattr(config, "PITLANE_DB", str(tmp_path / "p.db"))
        store, db = config.open_stores()
        assert (tmp_path / "a.db").exists() and (tmp_path / "p.db").exists()
        assert db.count("driver") == 4 and set(config.make_providers()) == {"supervisor", "vehicle", "booking"}

    def test_demo_script_runs_on_a_clean_checkout(self):
        done = subprocess.run([sys.executable, "-m", "scripts.demo"], cwd=ROOT, capture_output=True, text=True,
                              timeout=120)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "DEMO COMPLETE" in done.stdout
