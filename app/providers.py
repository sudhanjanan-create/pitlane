"""Model providers. The agent only knows `generate`. Scripted for demo without API key."""
import re
from dataclasses import dataclass, field
from typing import Any


class AgentError(Exception):
    """A run could not finish. `retryable` says whether trying again later could work."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class ModelTurn:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    raw: Any = None


class RoutedMock:
    """Several scripted conversations in one mock: picks a script by a phrase in the current request.

    Routes are tried in order, so put the more specific phrase first. A turn is either a ModelTurn or a
    function `contents -> ModelTurn`; the function form lets a script react to earlier tool results
    (e.g. book only if the eligibility check said yes)."""

    model = "mock"

    def __init__(self, routes: dict[str, list[ModelTurn]], slow: float = 0.0):
        self.routes, self.slow = routes, slow
        self.calls: list[list[dict]] = []

    def generate(self, system: str, contents: list[dict], tools: list) -> ModelTurn:
        import time

        self.calls.append([dict(c) for c in contents])
        last_user = max(i for i, c in enumerate(contents) if c["role"] == "user")
        request = contents[last_user]["text"]
        position = sum(1 for c in contents[last_user:] if c["role"] == "model")
        if self.slow:
            time.sleep(self.slow)
        for phrase, turns in self.routes.items():
            if phrase.lower() in request.lower():
                if position >= len(turns):
                    return ModelTurn(text="(mock) Done.")
                turn = turns[position]
                return turn(contents) if callable(turn) else turn
        return ModelTurn(text="(mock) I don't have a script for that request.")


def _call(name, **args):
    """Helper to create a tool call turn."""
    return ModelTurn(text=None, tool_calls=[ToolCall(name, args)], tokens_in=100, tokens_out=10)


_CAR = re.compile(r"car\s+(\d+)", re.I)
_SLOT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_BOOKING_ID = re.compile(r"booking\s+(\d+)", re.I)


def _task(contents: list[dict]) -> str:
    return next(c["text"] for c in contents if c["role"] == "user")


def _last_result(contents: list[dict]) -> dict:
    return next((c["result"] for c in reversed(contents) if c["role"] == "tool"), {})


def _say(text: str) -> ModelTurn:
    return ModelTurn(text=text, tokens_in=100, tokens_out=20)


def _relay(contents: list[dict]) -> ModelTurn:
    """Supervisor's last turn: tell the driver what the specialist reported (never more)."""
    return _say(_last_result(contents).get("answer", "(no answer)"))


# ---- booking specialist: get_driver_info -> check_driver_eligibility -> book (only if eligible)
#      -> send_notification (only if booked). Each step reads the previous tool result.
def _check_eligibility(contents):
    car = _CAR.search(_task(contents))
    return _call("check_driver_eligibility", car_id=int(car.group(1)) if car else 1)


def _book_if_eligible(contents):
    verdict = _last_result(contents)
    if not verdict.get("eligible"):
        return _say(f"Not booked: {verdict.get('reason') or verdict.get('hint') or 'eligibility unconfirmed'}.")
    task = _task(contents)
    car, slot = _CAR.search(task), _SLOT.search(task)
    if not slot:
        return _say("Not booked: the request did not include a slot time.")
    return _call("book_track_session", car_id=int(car.group(1)) if car else 1, slot_time=slot.group(0))


def _confirm_if_booked(contents):
    booked = _last_result(contents)
    if "booking_id" not in booked:
        return _say(f"Not booked: {(booked.get('hint') or booked.get('error') or 'unknown error').rstrip('.')}.")
    return _call("send_notification",
                 message=f"Your track session is confirmed: {booked['car_name']} at {booked['slot_time']}")


def _report_booking(contents):
    sent = "error" not in _last_result(contents)
    return _say("Booking confirmed. Confirmation sent." if sent else "Booked, but the confirmation could not be sent.")


# ---- booking specialist, cancel: cancel_track_session -> send_notification (only if it worked)
def _cancel(contents):
    m = _BOOKING_ID.search(_task(contents))
    return _call("cancel_track_session", booking_id=int(m.group(1)) if m else 1)


def _confirm_if_cancelled(contents):
    done = _last_result(contents)
    if "error" in done:
        return _say(f"Not cancelled: {(done.get('hint') or done['error']).rstrip('.')}.")
    return _call("send_notification", message="Your booking has been cancelled.")


def _report_cancel(contents):
    return _say("Cancelled and notification sent." if "error" not in _last_result(contents)
                else "Cancelled, but the notification could not be sent.")


def demo_providers(slow: float = 0.0) -> dict:
    """Scripted models for PitLane demo: booking requests and availability checks."""
    return {
        "supervisor": RoutedMock({
            # "cancel" first: routing is by substring, and a cancel request may also mention a car or booking.
            "cancel": [
                _call("ask_booking_specialist", request="Cancel booking 1."),
                _relay,
            ],
            # Book GT3 for 16:00
            "GT3": [
                _call("ask_vehicle_specialist", question="Is there a GT3 available? When do you have slots?"),
                _call("ask_booking_specialist", request="Book the GT3 (car 1) for the 2026-09-20T16:00:00 slot."),
                _relay,
            ],
            # Book the Formula-3 for 17:00 (needs an advanced license)
            "Formula": [
                _call("ask_booking_specialist", request="Book the Formula-3 (car 2) for the 2026-09-20T17:00:00 slot."),
                _relay,
            ],
            # Check what's available
            "available": [
                _call("ask_vehicle_specialist", question="What cars and time slots do you have available?"),
                ModelTurn(text="We have several cars available: GT3 (intermediate), Formula-3 (advanced), Beginner Kart (novice), and Touring Car (intermediate). Many time slots are open throughout the day."),
            ],
            # Check eligibility
            "eligible": [
                _call("ask_vehicle_specialist", question="Am I eligible to drive the Formula-3?"),
                ModelTurn(text="I need to check your license level against the Formula-3 requirements."),
            ],
        }, slow),
        "vehicle": RoutedMock({
            # Vehicle specialist responses for availability queries
            "GT3": [
                _call("list_cars"),
                _call("list_track_slots"),
                ModelTurn(text="The GT3 Porsche is available. It requires an intermediate license. We have slots at 14:00, 15:00, 16:00, 17:00, 18:00, 19:00, 20:00, 21:00, 22:00, 23:00, 00:00, and 01:00 tomorrow."),
            ],
            "available": [
                _call("list_cars"),
                _call("list_track_slots"),
                ModelTurn(text="Cars: GT3 Porsche (intermediate), Formula-3 (advanced), Beginner Kart (novice), Touring Car (intermediate). All are available. Many time slots open tomorrow from 14:00 to 01:00."),
            ],
            "eligible": [
                _call("check_driver_eligibility", driver_name="Alice Johnson", car_id=2),
                ModelTurn(text="Alice Johnson has an advanced license and the Formula-3 requires advanced, so she is eligible."),
            ],
            "slot": [
                _call("list_track_slots"),
                ModelTurn(text="Slots available: 14:00, 15:00, 16:00, 17:00, 18:00, 19:00, 20:00, 21:00, 22:00, 23:00, 00:00, and 01:00 tomorrow."),
            ],
        }, slow),
        "booking": RoutedMock({
            "cancel": [_cancel, _confirm_if_cancelled, _report_cancel],
            "book": [
                _call("get_driver_info"),
                _check_eligibility,
                _book_if_eligible,
                _confirm_if_booked,
                _report_booking,
            ],
            "info": [
                _call("get_driver_info"),
                ModelTurn(text="Here are your current bookings."),
            ],
        }, slow),
    }
