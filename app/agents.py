"""PitLane agents: Supervisor delegates to Vehicle Specialist and Booking Specialist.

    User (driver) ──▶ supervisor ──ask_vehicle_specialist──▶ vehicle agent (read-only)
                                   └─ask_booking_specialist──▶ booking agent (write access)

Each specialist is an ordinary agent loop with its own system prompt, its own provider and its own tool set.
The vehicle specialist is read-only: none of its tools change anything, and it cannot call the booking tools.
The booking specialist is bound to one driver and owns the side-effect tools (book, cancel, notify); it also
has two read-only tools (get_driver_info, check_driver_eligibility). book_track_session enforces the license
rule itself, so the booking is safe even if the model skips the eligibility check.
"""
import time
from collections.abc import Callable

from app.idempotency import idempotency_key
from app.pitlane_db import PitLaneDb
from app.providers import AgentError
from app.tools.pitlane_tools import VehicleSpecialistTools, BookingSpecialistTools, Toolset

SPECIALIST_MAX_STEPS = 12   # counts model turns + tool calls; the booking workflow alone uses 9

SUPERVISOR_SYSTEM = """You are the PitLane Track Booking Agent, assisting driver {driver_name}.
You coordinate track session bookings. Delegate as follows:
- ask_vehicle_specialist for information about cars, track slots, and driver eligibility;
- ask_booking_specialist for booking sessions, cancelling bookings, and sending confirmations.
Give each specialist a complete, specific request. Then answer the driver briefly using only what they reported."""

VEHICLE_SPECIALIST_SYSTEM = """You are the PitLane Vehicle Specialist. You can view:
- available cars and their license requirements
- available track time slots
- driver eligibility for specific cars

You CANNOT make bookings or send messages. Be brief and precise in your reports."""

BOOKING_SPECIALIST_SYSTEM = """You are the PitLane Booking Specialist for driver {driver_name}.
You can book track sessions, cancel bookings, and send confirmation messages.
Always check eligibility BEFORE booking. Report what you did, clearly."""


def run_tool(toolset: Toolset, db: PitLaneDb, key: str, name: str, args: dict) -> tuple[dict, bool]:
    """Run one tool call for any agent. Returns (result, replayed). Never raises, except AgentError.

    Side effects run at most once per key; replayed is True when the stored result was returned
    and nothing was done. Delegations hand the key down, so the specialist's side effects get keys
    derived from it: a replayed delegation replays its side effects safely too.
    """
    try:
        if name in toolset.DELEGATES:
            return toolset.delegate(name, args, key), False
        if name in toolset.SIDE_EFFECTS:
            result, fresh = db.once(key, name, lambda: toolset.call(name, args))
            return result, not fresh
        return toolset.call(name, args), False
    except AgentError:
        raise
    except NotImplementedError:
        return {"error": "not_implemented", "hint": f"{name} is not available yet."}, False
    except Exception as e:
        return {"error": "tool_failed", "hint": f"{name} failed ({type(e).__name__}). Try another way or tell the driver."}, False


def run_specialist(agent: str, system: str, toolset: Toolset, *, db: PitLaneDb, provider, task: str,
                   parent_key: str, on_step: Callable[[dict], None] | None = None) -> dict:
    """A specialist's whole agent loop, run inside one tool call of the supervisor."""
    contents = [{"role": "user", "text": task}]
    functions = list(toolset.functions().values())
    used = []
    seq = 0
    while seq < SPECIALIST_MAX_STEPS:
        turn = provider.generate(system, contents, functions)
        seq += 1
        if not turn.tool_calls:
            return {"agent": agent, "answer": turn.text or "", "tools_used": used}
        contents.append({"role": "model", "text": turn.text, "raw": turn.raw,
                         "tool_calls": [{"name": c.name, "args": c.args} for c in turn.tool_calls]})
        for call in turn.tool_calls:
            seq += 1
            key = idempotency_key(parent_key, seq, call.name, call.args)
            started = time.perf_counter()
            result, replayed = run_tool(toolset, db, key, call.name, call.args)
            used.append(call.name)
            if on_step:
                on_step({"agent": agent, "kind": "tool", "tool": call.name, "args": call.args, "result": result,
                         "ok": "error" not in result, "replayed": replayed,
                         "ms": round((time.perf_counter() - started) * 1000)})
            contents.append({"role": "tool", "name": call.name, "result": result})
    return {"agent": agent, "error": "specialist_step_limit", "tools_used": used,
            "hint": "The specialist could not finish. Tell the driver to try a simpler request."}


class SupervisorTools(Toolset):
    """The supervisor's only tools are the two specialists."""

    TOOL_NAMES = ("ask_vehicle_specialist", "ask_booking_specialist")
    DELEGATES = ("ask_vehicle_specialist", "ask_booking_specialist")

    def __init__(self, db: PitLaneDb, providers: dict, driver_name: str, on_step=None):
        self.db, self.providers, self.driver_name, self.on_step = db, providers, driver_name, on_step

    def ask_vehicle_specialist(self, question: str) -> dict:
        """Ask the vehicle specialist about cars, slots, and eligibility.

        Use for "what cars do you have", "when are slots available", "can I drive this car".
        It cannot make bookings.

        Args:
            question: A complete request, e.g. "Is Alice eligible to drive the GT3?"

        Returns:
            {"agent": "vehicle", "answer": str, "tools_used": [str]}.
        """
        raise RuntimeError("delegations run through delegate()")

    def ask_booking_specialist(self, request: str) -> dict:
        """Ask the booking specialist to book a session, cancel, or send a message. IT CAN CHANGE DATA.

        Use for bookings, cancellations, and confirmations. Include car IDs and slot times.

        Args:
            request: A complete instruction, e.g. "Book the GT3 (car 1) for 2026-09-20T16:00:00"

        Returns:
            {"agent": "booking", "answer": str, "tools_used": [str]}.
        """
        raise RuntimeError("delegations run through delegate()")

    def delegate(self, name: str, args: dict, key: str) -> dict:
        bad = self.call_check(name, args)
        if bad:
            return bad
        if self.on_step:
            self.on_step({"agent": "supervisor", "kind": "delegate", "tool": name, "args": args})
        if name == "ask_vehicle_specialist":
            return run_specialist("vehicle", VEHICLE_SPECIALIST_SYSTEM, VehicleSpecialistTools(self.db), 
                                  db=self.db, provider=self.providers["vehicle"], 
                                  task=args["question"], parent_key=key, on_step=self.on_step)
        return run_specialist("booking", BOOKING_SPECIALIST_SYSTEM.format(driver_name=self.driver_name), 
                              BookingSpecialistTools(self.db, self.driver_name), db=self.db,
                              provider=self.providers["booking"], task=args["request"],
                              parent_key=key, on_step=self.on_step)

    def call_check(self, name: str, args: dict) -> dict | None:
        """Validate a delegation's arguments."""
        field = "question" if name == "ask_vehicle_specialist" else "request"
        if set(args) != {field} or not isinstance(args[field], str) or not args[field].strip():
            return {"error": "invalid_arguments", "hint": f"{name} takes one non-empty string: {field}."}
        return None
