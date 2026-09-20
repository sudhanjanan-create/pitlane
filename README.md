# PitLane — Motorsport Track Session Agent

A small multi-agent service for booking sessions at a fictional motorsport track. It demonstrates a
supervisor/specialist agent design, a durable job queue with leases, idempotent side effects, and crash
recovery — all with a **scripted model provider, so no API key is needed**.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # only pytest; the app itself uses just the standard library

pytest                             # the test suite
python -m scripts.demo             # end-to-end scenarios through the real queue/worker/agents
python -m scripts.crash_demo       # real crash + lease expiry + replay; prints PASS only if every check holds
```

Python 3.10+ (uses `X | None` type syntax). Everything runs from a clean checkout: the demos use in-memory or
temp-dir databases and leave no files behind. If you ran an earlier version of this project against on-disk
`agent.db` / `pitlane.db` files, delete them: the `booking` uniqueness rule changed (see below) and
`CREATE TABLE IF NOT EXISTS` will not alter an existing table.

## Domain

- **Drivers** have a license level: `novice < intermediate < advanced`.
- **Cars** have a minimum license level.
- **Track slots** are 60-minute sessions (12 seeded: 2026-09-20 14:00 through 2026-09-21 01:00).
- **Bookings** put one driver in one car for one slot.
- **Notifications** are confirmation messages recorded for a driver.

Seed data: 4 drivers (Alice/Diana advanced, Bob intermediate, Charlie novice), 4 cars (Beginner Kart, GT3 Porsche,
Touring Car, Formula-3), 12 slots.

## Architecture

```
 driver request
      │  enqueue()
      ▼
┌─────────────────────────────┐        agent.db  (runtime database)
│ durable queue: run table    │◀──────  thread, message, run, run_step, tool_call
│ queued → running → succeeded│
└─────────────┬───────────────┘
              │ claim_next(): atomic, takes a LEASE
              ▼
┌─────────────────────────────┐
│ Worker (heartbeat, reaper)  │  reap_expired(): dead worker's run → queued again
└─────────────┬───────────────┘
              ▼
┌─────────────────────────────┐
│ SUPERVISOR agent            │  tools: ask_vehicle_specialist, ask_booking_specialist
│ (no domain tools of its own)│  (delegation only)
└───────┬─────────────┬───────┘
        ▼             ▼
┌───────────────┐ ┌─────────────────────────────────────────┐
│ VEHICLE       │ │ BOOKING specialist (bound to ONE driver) │
│ specialist    │ │ read-only: get_driver_info,              │
│ READ-ONLY     │ │            check_driver_eligibility      │
│ list_cars     │ │ side effects: book_track_session,        │
│ get_car_details│ │   cancel_track_session, send_notification│
│ list_track_slots│ └───────────────────┬─────────────────────┘
│ check_driver_ │                     │
│  eligibility  │                     │
└───────┬───────┘                     │
        └───────────────┬─────────────┘
                        ▼
              pitlane.db  (domain database)
   driver, car, track_slot, booking, notification, idempotency
```

Each agent is an ordinary loop (`provider.generate` → tool calls → results) with its **own system prompt, its own
provider, and its own tool set**. The supervisor's delegation runs the specialist's whole loop inside one tool call.

## Two separate SQLite databases

| File | Owns | Tables |
|---|---|---|
| `pitlane.db` | business data + idempotency keys | `driver`, `car`, `track_slot`, `booking`, `notification`, `idempotency` |
| `agent.db` | conversations, runs, the job queue | `thread`, `message`, `run`, `run_step`, `tool_call` |

No table appears in both (a test checks this). Idempotency keys live in `pitlane.db` on purpose: a side effect and
its key commit in **one transaction**, so there is no window where one exists without the other.
(`thread.student_id` is a legacy column name; it stores the driver's name.)

## Tools

Eight tool names (nine bindings: `check_driver_eligibility` exists for both specialists).

| Tool | Agent | Kind | Purpose |
|---|---|---|---|
| `list_cars` | vehicle | read-only | cars and their minimum license |
| `get_car_details` | vehicle | read-only | one car's details |
| `list_track_slots` | vehicle | read-only | slots and whether each is booked |
| `check_driver_eligibility(driver_name, car_id)` | vehicle | read-only | does a driver's license meet a car's minimum |
| `get_driver_info` | booking | read-only | this driver's license level and active bookings |
| `check_driver_eligibility(car_id)` | booking | read-only | same rule, for the driver this agent is bound to |
| `book_track_session(car_id, slot_time)` | booking | **side effect** | create a booking |
| `cancel_track_session(booking_id)` | booking | **side effect** | cancel one of *this driver's* bookings |
| `send_notification(message)` | booking | **side effect** | record a message for the driver |

Every tool docstring says when to use it and what (if anything) it changes; write tools also list their error results.

## Business rules

**1. One active booking per track slot — enforced by the database.**
`schema/pitlane.sql` defines a partial unique index:

```sql
CREATE UNIQUE INDEX booking_one_active_per_slot ON booking (track_slot_id) WHERE status = 'active';
```

SQLite itself rejects a second active booking for a slot, even from a raw `INSERT` that bypasses all Python
(tested). It is *partial* so cancelled bookings stay as history without blocking the slot: after a cancellation
the slot can be booked again (tested). Two workers racing for one slot: exactly one wins (tested with threads).

**2. License eligibility — enforced by the booking operation itself.**
`PitLaneDb.book_session` checks `check_driver_eligibility` inside its transaction and returns
`{"error": "not_eligible"}` for an under-licensed driver. Because the rule lives in the operation, **a model or
script that never calls the eligibility tool still cannot get an ineligible booking through** (tested at the
service layer, at the tool layer, and end-to-end with a scripted model that skips the check). The intended
workflow still runs in order — the booking specialist does `get_driver_info → check_driver_eligibility →
book_track_session (only if eligible) → send_notification (only if booked)` — but safety does not depend on it.

## Multi-agent design and least privilege

- **Supervisor** — only tools are the two delegations; it makes no domain call itself.
- **Vehicle specialist** — read-only. Its tool set contains no side-effect tool, so it is never even offered one,
  and if its model tries to call `book_track_session` / `cancel_track_session` / `send_notification` the dispatcher
  answers `unknown_tool` and nothing changes (tested with a model that tries all three).
- **Booking specialist** — bound to one driver at construction. It cannot cancel another driver's booking
  (`not_your_booking`, tested).

## Durable execution

1. **Enqueue** — the user message and a `queued` run are saved together.
2. **Claim** — `claim_next` atomically sets `running`, `lease_owner`, `lease_until`, `attempts + 1`.
3. **Execute** — `execute_run` writes every model step and tool result to `agent.db` as it goes.
4. **Heartbeat** — the lease is extended between steps; a worker that lost its lease stops.
5. **Complete** — the reply and `succeeded` are written together, and only if this worker still owns the lease
   (a "zombie" worker cannot complete a run someone else took over — tested).
6. **Recovery** — `reap_expired` requeues a `running` run whose lease has expired, or marks it `dead` once
   `max_attempts` is used up. A live lease is never stolen (tested).

## Idempotency

Side effects are protected in three independent layers:

1. **Run-level key** — `sha256(run_id, step_seq, tool_name, args)`. The supervisor's call gets a key; the
   specialist's calls get keys derived from it, so a *replayed delegation* derives the same keys. `PitLaneDb.once`
   looks the key up and either returns the stored result (`replayed=True`, nothing done) or runs the effect and
   stores the key **in the same transaction**. A crashed effect rolls back, leaving no key (tested).
2. **Booking is repeat-safe** — the same driver booking the same slot again returns the existing booking.
3. **Notification dedupe** — `notification.dedupe_key` is unique: same driver + same message + same day is one
   notification, even under a different run key (tested).

## Crash-and-replay demo

`python -m scripts.crash_demo` performs a real one (nothing is inserted by hand):

1. A real run is enqueued in an on-disk `agent.db` (temp directory; two separate DB files).
2. **Worker-1 runs in a child process.** It claims the run under a lease and executes the actual workflow, including
   the real `book_track_session` and `send_notification` tool calls, both of which commit to `pitlane.db`.
3. Immediately after the notification commits, the child dies via `os._exit(137)` — no cleanup, no `finally`. The
   supervisor never recorded the delegation and the run was never completed. (Crash injection is a small `on_step`
   hook in `app/crash.py`. It only observes step events and kills the process; it does not alter the
   worker, queue, tool or idempotency code paths that the demo exercises.)
4. The demo checks the wreckage: run still `running` under worker-1, exactly one booking and one notification, both
   idempotency keys stored, and that another worker **cannot** take the run while the lease is live.
5. The lease expires; the reaper requeues the run; **worker-2** (fresh DB connections) claims it and replays the
   persisted workflow. The booking and notification tools run again but return their stored results.
6. It verifies: run `succeeded` on attempt 2, replay occurred (both side-effect tools returned stored results, none
   ran fresh), exactly one booking, exactly one notification, no new idempotency keys, one reply recorded.

`PASS` is printed only if all 18 checks hold; otherwise it prints `FAIL` and exits non-zero. The same scenario also
runs in-process (with a fake clock and a `SimulatedCrash`) in the test suite, with two more crash points: before
any side effect, and *between* the booking and the notification.

## Tests

`pytest` — 52 tests, no API key, no network. `tests/test_pitlane.py` covers database and queue basics;
`tests/test_workflow.py` drives the real stack:

| Area | What is proven |
|---|---|
| Eligibility | eligible driver books; ineligible is refused at service layer, at tool layer, and when a model skips the check; the default script checks eligibility first and stops if ineligible |
| Slot constraint | raw SQL cannot double-book; cancelled slot can be re-booked; concurrent bookings → one winner |
| Idempotency | side effects run once per key; keys survive reconnect; failed effect leaves no key; notification dedupe; repeat booking is safe |
| Least privilege | vehicle specialist is offered only read tools and cannot mutate anything even when its model tries; supervisor has delegation only; no cross-driver cancel |
| Delegation | two separate specialist loops with their own providers; run persisted step by step with keys |
| Lease recovery | live lease not stolen; heartbeat; dead-letter after `max_attempts`; zombie worker cannot complete |
| Crash and replay | crash after both effects / between them / before any; plus the shipped `crash_demo` and `demo` scripts as subprocesses |
| Clean machine | both DBs initialise from nothing, `migrate()` is repeatable, tables disjoint, `config.open_stores` works |

## Limitations (what this does *not* do)

- **Scripted models only.** The scripts are fixed (the booking script reads the car id and slot time out of the
  request text; the vehicle specialist's "eligible?" answer is hard-coded to Alice). No live LLM provider is included.
- **Replay relies on the specialist being deterministic.** The supervisor's steps are persisted and resumed from
  `agent.db`, but a specialist's *internal* steps are not: on replay the specialist loop re-runs from its start and
  gets the same idempotency keys because the scripted provider repeats itself. A real, non-deterministic LLM could pick
  different arguments and so different keys; layers 2–3 above and the slot constraint are the backstop there.
- `car.available` is displayed but not enforced when booking.
- No authentication: the driver name on a thread is trusted.
- Concurrency is tested with threads; the crash demo uses two OS processes, but there is no long-running multi-process
  worker pool.

## Files

```
schema/pitlane.sql, schema/agent.sql   the two schemas
app/pitlane_db.py                      domain DB: every SQL statement for pitlane.db, incl. business rules, once()
app/memory.py                          agent DB: queue, leases, runs, steps
app/tools/pitlane_tools.py             the tools, split by agent
app/tools/dispatch.py                  model tool call → Python call, argument validation
app/agents.py                          supervisor + specialist loops, run_tool (idempotency wrapper)
app/runner.py                          resumable execution of one claimed run
app/worker.py                          claim / execute / record outcome
app/providers.py                       scripted (no-key) model provider
app/idempotency.py                     stable keys
app/crash.py                           crash injection hook (demo + tests only)
app/config.py, app/db.py               DB opening helpers
scripts/demo.py, scripts/crash_demo.py
tests/test_pitlane.py, tests/test_workflow.py, tests/helpers.py, tests/conftest.py
```

## Roll number

Replace `weekend-pitlane.zip` with `weekend-<roll-no>.zip` before submission.
