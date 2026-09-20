"""PitLane crash demo: a real worker crash, a real lease expiry, a real replay.

What happens (nothing here inserts booking or notification rows by hand):

  1. A real run is enqueued in agent.db (on disk, in a temp dir).
  2. Worker-1 runs in a CHILD PROCESS. It claims the run under a lease and executes the actual workflow:
     supervisor -> vehicle specialist -> supervisor -> booking specialist -> eligibility check ->
     book_track_session -> send_notification. Both side effects go through the real tool path and commit
     to pitlane.db. Right after send_notification commits, the child dies with os._exit(): no cleanup, no
     `finally`, no marking the run done. The supervisor never records the delegation's result.
  3. The run is left 'running' under a dead worker's lease. Nobody can take it while the lease is live.
  4. The lease expires. A reaper requeues the run.
  5. Worker-2 (separate DB connections) claims it and replays the persisted workflow from agent.db.
     The booking and notification tools run again, but their idempotency keys are already in pitlane.db,
     so they return the stored results instead of acting.
  6. The run reaches 'succeeded'. We then check the databases. `PASS` is printed only if EVERY check holds.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.crash import crash_after
from app.memory import RunStore
from app.pitlane_db import PitLaneDb
from app.providers import demo_providers
from app.worker import Worker

CRASH_EXIT_CODE = 137
LEASE_SECONDS = 2.0
DRIVER = "Bob Smith"                       # intermediate license: exactly meets the GT3's requirement
REQUEST = "Book the GT3 for tomorrow at 16:00"


def print_section(title: str) -> None:
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}\n")


def describe(event: dict, who: str) -> str | None:
    if event["kind"] == "delegate":
        return f"[{who}] supervisor -> {event['tool']}"
    if event["agent"] == "supervisor":
        return None
    text = json.dumps(event["result"], default=str)
    replay = "  (REPLAYED: stored result, nothing re-done)" if event.get("replayed") else ""
    return f"[{who}]   {event['agent']}.{event['tool']} -> {text[:70]}{'...' if len(text) > 70 else ''}{replay}"


# ---------------------------------------------------------------- worker 1: runs in a child process and dies
def worker1_main(agent_path: str, pitlane_path: str) -> None:
    store, db = RunStore(agent_path), PitLaneDb(pitlane_path)
    die = crash_after("booking", "send_notification", lambda: os._exit(CRASH_EXIT_CODE))

    def on_step(event: dict) -> None:
        line = describe(event, "worker-1")
        if line:
            print(line, flush=True)          # flush: os._exit() will not
        die(event)                           # after send_notification has committed, the process is gone

    worker = Worker(store, db, demo_providers(), worker_id="worker-1", lease_seconds=LEASE_SECONDS, on_step=on_step)
    worker.run_once()
    print("[worker-1] finished without crashing (the crash hook never fired)", flush=True)


# ---------------------------------------------------------------- the orchestrator
def crash_demo() -> bool:
    print_section("PITLANE CRASH & RECOVERY DEMO")
    results: list[bool] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        results.append(bool(ok))
        print(f"   {'✓' if ok else '✗'} {label}" + (f"  [{detail}]" if detail else ""))

    with tempfile.TemporaryDirectory(prefix="pitlane-crash-") as tmp:
        agent_path, pitlane_path = str(Path(tmp) / "agent.db"), str(Path(tmp) / "pitlane.db")

        # -------------------------------------------------- 1. enqueue a real run
        print("1. Enqueue a real run (two separate SQLite files: agent.db and pitlane.db)")
        store, db = RunStore(agent_path), PitLaneDb(pitlane_path)
        store.migrate()
        db.migrate()
        thread_id = store.create_thread(DRIVER)
        run_id = store.enqueue(thread_id, REQUEST, "mock")
        print(f"   {DRIVER}: {REQUEST!r} -> run {run_id[:8]} is {store.get_run(run_id)['status']}")

        # -------------------------------------------------- 2. worker-1 runs in a child process and crashes
        print("\n2. Worker-1 (child process) claims the run and executes the real workflow...")
        child = subprocess.run(
            [sys.executable, "-m", "scripts.crash_demo", "--worker1", agent_path, pitlane_path],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        for line in child.stdout.splitlines():
            print("   " + line)
        if child.returncode != CRASH_EXIT_CODE and child.stderr.strip():
            print("   child stderr:\n" + "\n".join("     " + ln for ln in child.stderr.strip().splitlines()[-8:]))

        print("\n3. State left behind by the dead worker (read straight from the databases)")
        run = store.get_run(run_id)
        idem = db.conn.execute("SELECT tool_name FROM idempotency ORDER BY created_at, key").fetchall()
        delegations = [s for s in run["steps"] if s["kind"] == "tool" and s["tool_name"] == "ask_booking_specialist"]
        replies = [m for m in store.load_history(thread_id) if m["role"] == "model"]
        check("worker-1 process died hard", child.returncode == CRASH_EXIT_CODE, f"exit code {child.returncode}")
        check("run is still 'running', leased to worker-1 (never marked done)",
              run["status"] == "running" and run["lease_owner"] == "worker-1" and run["attempts"] == 1,
              f"status={run['status']} owner={run['lease_owner']} attempts={run['attempts']}")
        check("booking already committed by the real tool path", db.count("booking") == 1)
        check("notification already committed by the real tool path", db.count("notification") == 1)
        check("both side effects have idempotency keys stored",
              sorted(r["tool_name"] for r in idem) == ["book_track_session", "send_notification"],
              ", ".join(r["tool_name"] for r in idem))
        check("agent.db never learned the booking delegation finished", not delegations and not replies)

        # -------------------------------------------------- 4. lease protects the run until it expires
        print("\n4. The dead worker's lease is honoured until it expires")
        store2, db2 = RunStore(agent_path), PitLaneDb(pitlane_path)      # fresh connections: a different process
        events: list[dict] = []
        worker2 = Worker(store2, db2, demo_providers(), worker_id="worker-2", lease_seconds=30.0, on_step=events.append)
        lease_left = run["lease_until"] - time.time()
        if lease_left > 0:
            check("worker-2 cannot steal a run whose lease is still live", worker2.run_once() is None,
                  f"{lease_left:.1f}s of lease left")
            print(f"   ... waiting {lease_left:.1f}s for worker-1's lease to expire")
            time.sleep(lease_left + 0.2)
        else:
            print("   (lease had already expired by the time the child was inspected; nothing to wait for)")

        print("\n5. Reaper requeues the expired run; worker-2 reclaims it and replays the persisted workflow")
        reaped = store2.reap_expired()
        requeued = store2.get_run(run_id)
        check("expired lease detected and run requeued", reaped == [run_id] and requeued["status"] == "queued"
              and requeued["error_code"] == "lease_expired", f"status={requeued['status']} error={requeued['error_code']}")
        outcomes = worker2.run_until_idle()
        for event in events:
            line = describe(event, "worker-2")
            if line:
                print("   " + line)

        # -------------------------------------------------- 6. verify
        print("\n6. Verify the final state")
        final = store2.get_run(run_id)
        bookings = db2.list_bookings()
        notes = db2.conn.execute("SELECT driver_id, message FROM notification").fetchall()
        idem_after = db2.conn.execute("SELECT tool_name FROM idempotency").fetchall()
        fresh_effects = [e for e in events if e["kind"] == "tool" and e["agent"] == "booking"
                         and e["tool"] in ("book_track_session", "send_notification") and not e["replayed"]]
        replayed = sorted(e["tool"] for e in events if e["kind"] == "tool" and e["agent"] == "booking" and e["replayed"])
        replies = [m for m in store2.load_history(thread_id) if m["role"] == "model"]

        check("worker-2 finished the run", outcomes == [(run_id, "succeeded")], str(outcomes))
        check("final run status is 'succeeded'", final["status"] == "succeeded" and final["lease_owner"] is None,
              f"status={final['status']}")
        check("replay occurred: run took 2 attempts", final["attempts"] == 2, f"attempts={final['attempts']}")
        check("replay occurred: booking + notification tools returned stored results",
              replayed == ["book_track_session", "send_notification"], ", ".join(replayed))
        check("no side effect was executed a second time", not fresh_effects)
        check("exactly ONE booking (no duplicate)", len(bookings) == 1, f"{len(bookings)} booking(s)")
        check("the booking is the right one", len(bookings) == 1 and bookings[0]["driver_name"] == DRIVER
              and bookings[0]["car_name"] == "GT3 Porsche" and bookings[0]["slot_time"] == "2026-09-20T16:00:00"
              and bookings[0]["status"] == "active")
        check("exactly ONE notification (no duplicate)", len(notes) == 1, f"{len(notes)} notification(s)")
        check("replay created no new idempotency keys", len(idem_after) == 2, f"{len(idem_after)} keys")
        check("the agent's reply was recorded exactly once", len(replies) == 1 and replies[0]["text"].strip() != "",
              replies[0]["text"] if replies else "none")

    ok = all(results) and len(results) > 0
    print_section("RESULT")
    if ok:
        print(f"All {len(results)} checks held.\n")
        print("PASS")
    else:
        print(f"{results.count(False)} of {len(results)} checks FAILED.\n")
        print("FAIL")
    print()
    return ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker1", nargs=2, metavar=("AGENT_DB", "PITLANE_DB"),
                        help="internal: run the doomed first worker in this process")
    args = parser.parse_args()
    if args.worker1:
        worker1_main(*args.worker1)
        sys.exit(0)          # only reached if the crash hook did not fire
    sys.exit(0 if crash_demo() else 1)
