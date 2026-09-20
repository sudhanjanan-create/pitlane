"""PitLane track booking tools, split between supervisor and specialist agents."""
from datetime import datetime, timezone

from app.idempotency import notification_dedupe_key
from app.pitlane_db import PitLaneDb
from app.tools.dispatch import dispatch


class Toolset:
    """Base class for tool sets."""
    SIDE_EFFECTS: tuple[str, ...] = ()     # run through PitLaneDb.once with an idempotency key
    DELEGATES: tuple[str, ...] = ()        # hand work to another agent
    TOOL_NAMES: tuple[str, ...] = ()

    def functions(self) -> dict:
        return {n: getattr(self, n) for n in self.TOOL_NAMES}

    def call(self, name: str, args: dict) -> dict:
        return dispatch(self.functions(), name, args)


class VehicleSpecialistTools(Toolset):
    """Read-only. The vehicle specialist can inspect vehicles and eligibility, never change anything."""

    TOOL_NAMES = ("list_cars", "get_car_details", "check_driver_eligibility", "list_track_slots")

    def __init__(self, db: PitLaneDb):
        self.db = db

    def list_cars(self) -> dict:
        """List all available cars at the track facility.

        Use when the user asks "what cars are available", "what cars do you have", or "list cars".
        This is read-only and changes nothing. Returns info on every car including license requirements.

        Returns:
            {"cars": [{"car_id", "name", "min_license_level", "available"}]}.
        """
        cars = self.db.list_cars()
        return {"cars": [{"car_id": c["id"], "name": c["name"], 
                         "min_license_level": c["min_license_level"], 
                         "available": bool(c["available"])} for c in cars]}

    def get_car_details(self, car_id: int) -> dict:
        """Get detailed information about a specific car including its minimum license level.

        Use when you need the requirements for a particular car. Read-only: changes nothing.

        Args:
            car_id: The car ID from list_cars.

        Returns:
            {"car_id", "name", "min_license_level", "available"}, or an error if unknown.
        """
        car = self.db.get_car(car_id)
        if car is None:
            return {"error": "unknown_car", "hint": "Use list_cars to find the car_id first."}
        return {"car_id": car["id"], "name": car["name"], 
                "min_license_level": car["min_license_level"], "available": bool(car["available"])}

    def list_track_slots(self) -> dict:
        """List all track time slots, showing which are available and which are booked.

        Use when the user asks "what time slots are available", "when can I book", or "show me slots".
        This is read-only and changes nothing. Shows all upcoming slots and their booking status.

        Returns:
            {"slots": [{"slot_id", "slot_time", "duration_minutes", "booked"}]}.
        """
        slots = self.db.list_track_slots()
        return {"slots": [{"slot_id": s["id"], "slot_time": s["slot_time"], 
                          "duration_minutes": s["duration_minutes"], 
                          "booked": bool(s["booked"])} for s in slots]}

    def check_driver_eligibility(self, driver_name: str, car_id: int) -> dict:
        """Check whether a specific driver is eligible to drive a car based on license level.

        Use BEFORE attempting a booking. Read-only: changes nothing. The decision comes from
        comparing the driver's license level against the car's minimum requirement.

        Args:
            driver_name: The driver's name.
            car_id: The car ID from list_cars.

        Returns:
            {"eligible": bool, "reason": str, "driver_name": str, "car_name": str}.
        """
        driver = self.db.get_driver(driver_name)
        if driver is None:
            return {"error": "unknown_driver", "hint": f"Driver '{driver_name}' not found."}
        
        car = self.db.get_car(car_id)
        if car is None:
            return {"error": "unknown_car", "hint": "Use list_cars to find the car_id first."}
        
        eligible, reason = self.db.check_driver_eligibility(driver["id"], car_id)
        return {"eligible": eligible, "reason": reason, "driver_name": driver_name, "car_name": car["name"]}


class BookingSpecialistTools(Toolset):
    """The booking agent. Bound to ONE driver. Can change bookings and send notifications."""

    TOOL_NAMES = ("get_driver_info", "check_driver_eligibility", "book_track_session",
                  "cancel_track_session", "send_notification")
    SIDE_EFFECTS = ("book_track_session", "cancel_track_session", "send_notification")

    def __init__(self, db: PitLaneDb, driver_name: str, clock=lambda: datetime.now(timezone.utc)):
        self.db, self.driver_name, self.clock = db, driver_name, clock

    def _driver(self) -> dict:
        d = self.db.get_driver(self.driver_name)
        if d is None:
            raise LookupError(f"driver {self.driver_name} not found")
        return d

    def get_driver_info(self) -> dict:
        """Get information about the current driver, including their bookings.

        Use for "tell me my bookings", "what have I booked". Read-only: changes nothing.

        Returns:
            {"driver_name", "license_level", "bookings": [{"booking_id", "car_name", "slot_time"}]}.
        """
        driver = self._driver()
        bookings = self.db.list_bookings(driver["id"])
        return {
            "driver_name": driver["name"],
            "license_level": driver["license_level"],
            "bookings": [{"booking_id": b["id"], "car_name": b["car_name"], "slot_time": b["slot_time"]} 
                        for b in bookings]
        }

    def check_driver_eligibility(self, car_id: int) -> dict:
        """Check whether THIS driver's license allows the given car. Read-only: changes nothing.

        Use BEFORE book_track_session. (book_track_session re-checks the same rule itself and refuses
        ineligible bookings, so skipping this call cannot get a booking through.)

        Args:
            car_id: The car ID from list_cars.

        Returns:
            {"eligible": bool, "reason": str, "driver_name": str, "car_name": str}, or an error if the car is unknown.
        """
        car = self.db.get_car(car_id)
        if car is None:
            return {"error": "unknown_car", "hint": "Use list_cars to find the car_id first."}
        eligible, reason = self.db.check_driver_eligibility(self._driver()["id"], car_id)
        return {"eligible": eligible, "reason": reason, "driver_name": self.driver_name, "car_name": car["name"]}

    def book_track_session(self, car_id: int, slot_time: str) -> dict:
        """Book a track session for this driver in the specified car at the specified time. CHANGES DATA.

        Use only when the driver has requested a booking and eligibility is confirmed. The booking
        itself refuses drivers whose license is too low for the car, whatever was checked before.
        If the same driver books the same slot twice, the second attempt returns the
        existing booking (safe to retry).

        Args:
            car_id: Integer car ID from list_cars.
            slot_time: ISO 8601 datetime string from list_track_slots (e.g. "2026-09-20T14:00:00").

        Returns:
            {"booking_id", "driver_name", "car_name", "slot_time", "status": "booked" | "already_booked"},
            or an error like not_eligible, slot_already_booked, unknown_car, or invalid_time.
        """
        # Verify slot exists and convert to slot_id
        slot = self.db.find_slot_by_time(slot_time)
        if slot is None:
            return {"error": "invalid_time", "hint": f"No track slot at {slot_time}. Use list_track_slots."}
        
        # Verify car exists
        car = self.db.get_car(car_id)
        if car is None:
            return {"error": "unknown_car", "hint": "Use list_cars to find a valid car_id."}
        
        driver = self._driver()
        
        # Book it (this enforces the unique constraint on track_slot_id)
        result, newly_created = self.db.book_session(driver["id"], car_id, slot["id"])
        if "error" in result:
            return result
        
        # Format response
        return {
            "booking_id": result["id"],
            "driver_name": result["driver_name"],
            "car_name": result["car_name"],
            "slot_time": result["slot_time"],
            "status": "booked" if newly_created else "already_booked"
        }

    def cancel_track_session(self, booking_id: int) -> dict:
        """Cancel a track session booking. CHANGES DATA: removes the booking.

        Use only when the driver has asked to cancel. Cancelling the same booking twice is safe.
        A driver can only cancel their own bookings.

        Args:
            booking_id: The booking ID from get_driver_info or book_track_session.

        Returns:
            {"booking_id", "status": "cancelled" | "already_cancelled"}, or an error.
        """
        booking = self.db.get_booking(booking_id)
        if booking is not None and booking["driver_id"] != self._driver()["id"]:
            return {"error": "not_your_booking", "hint": "You can only cancel your own bookings."}
        result, changed = self.db.cancel_booking(booking_id)
        if "error" in result:
            return result
        return result

    def send_notification(self, message: str) -> dict:
        """Send the driver a notification message. CHANGES DATA: the message is recorded.

        Use to confirm bookings or send important updates. The same message on the same day
        is sent only once. Never use it to answer a question; reply in the chat instead.

        Args:
            message: 1 to 200 characters.

        Returns:
            {"notification_id", "status": "sent", "duplicate": bool}.
        """
        if not message.strip() or len(message) > 200:
            return {"error": "invalid_message", "hint": "message must be 1 to 200 characters."}
        
        dedupe_key = notification_dedupe_key(self.driver_name, message, self.clock().date())
        
        notification_id, created = self.db.record_notification(self._driver()["id"], message, dedupe_key)
        return {"notification_id": notification_id, "status": "sent", "duplicate": not created}
