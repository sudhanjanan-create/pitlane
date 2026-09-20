"""pitlane.db: drivers, cars, track slots, bookings, notifications. Every SQL statement lives here."""
import json
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from app.db import connect, transaction

SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "pitlane.sql"


class PitLaneDb:
    def __init__(self, path: str = ":memory:", clock: Callable[[], float] = time.time):
        self.conn = connect(path)
        self.clock = clock

    def transaction(self):
        return transaction(self.conn)

    def migrate(self) -> None:
        """Initialize schema and seed demo data."""
        self.conn.executescript(SCHEMA.read_text())
        if self.conn.execute("SELECT count(*) FROM driver").fetchone()[0]:
            return
        with self.transaction() as c:
            # Seed drivers
            c.executemany("INSERT INTO driver (name, license_level, created_at) VALUES (?, ?, ?)", [
                ("Alice Johnson", "advanced", self.clock()),
                ("Bob Smith", "intermediate", self.clock()),
                ("Charlie Brown", "novice", self.clock()),
                ("Diana Martinez", "advanced", self.clock()),
            ])
            # Seed cars
            c.executemany("INSERT INTO car (name, min_license_level, available, created_at) VALUES (?, ?, ?, ?)", [
                ("GT3 Porsche", "intermediate", 1, self.clock()),
                ("Formula-3", "advanced", 1, self.clock()),
                ("Beginner Kart", "novice", 1, self.clock()),
                ("Touring Car", "intermediate", 1, self.clock()),
            ])
            # Seed track slots
            now = datetime.fromisoformat("2026-09-20T14:00:00")  # demo day
            slots = []
            for i in range(12):
                slot_time = now + timedelta(hours=i)
                slots.append((slot_time.isoformat(), 60, self.clock()))
            c.executemany("INSERT INTO track_slot (slot_time, duration_minutes, created_at) VALUES (?, ?, ?)", slots)

    # ================================================================== reads

    def get_driver(self, name: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM driver WHERE name = ?", (name,)).fetchone()
        return dict(r) if r else None

    def list_drivers(self) -> list[dict]:
        rows = self.conn.execute("SELECT id, name, license_level FROM driver ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def get_car(self, car_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM car WHERE id = ?", (car_id,)).fetchone()
        return dict(r) if r else None

    def list_cars(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, name, min_license_level, available FROM car ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def get_track_slot(self, slot_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM track_slot WHERE id = ?", (slot_id,)).fetchone()
        return dict(r) if r else None

    def list_track_slots(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, slot_time, duration_minutes, "
            "(SELECT COUNT(*) FROM booking WHERE track_slot_id = track_slot.id AND status = 'active') as booked "
            "FROM track_slot ORDER BY slot_time").fetchall()
        return [dict(r) for r in rows]

    def find_slot_by_time(self, slot_time: str) -> dict | None:
        """Find a track slot by its ISO 8601 time string."""
        r = self.conn.execute(
            "SELECT id, slot_time, duration_minutes FROM track_slot WHERE slot_time = ?", (slot_time,)).fetchone()
        return dict(r) if r else None

    def get_booking(self, booking_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM booking WHERE id = ?", (booking_id,)).fetchone()
        return dict(r) if r else None

    def list_bookings(self, driver_id: int | None = None) -> list[dict]:
        if driver_id:
            rows = self.conn.execute(
                "SELECT b.id, b.driver_id, d.name as driver_name, b.car_id, c.name as car_name, "
                "b.track_slot_id, t.slot_time, b.status "
                "FROM booking b "
                "JOIN driver d ON d.id = b.driver_id "
                "JOIN car c ON c.id = b.car_id "
                "JOIN track_slot t ON t.id = b.track_slot_id "
                "WHERE b.driver_id = ? AND b.status = 'active' "
                "ORDER BY t.slot_time", (driver_id,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT b.id, b.driver_id, d.name as driver_name, b.car_id, c.name as car_name, "
                "b.track_slot_id, t.slot_time, b.status "
                "FROM booking b "
                "JOIN driver d ON d.id = b.driver_id "
                "JOIN car c ON c.id = b.car_id "
                "JOIN track_slot t ON t.id = b.track_slot_id "
                "WHERE b.status = 'active' "
                "ORDER BY t.slot_time").fetchall()
        return [dict(r) for r in rows]

    def get_driver_by_id(self, driver_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM driver WHERE id = ?", (driver_id,)).fetchone()
        return dict(r) if r else None

    def check_driver_eligibility(self, driver_id: int, car_id: int) -> tuple[bool, str]:
        """Check if a driver meets the license level for a car. Returns (eligible, reason).

        Single source of truth for the license rule: used by the read-only eligibility tools AND enforced
        inside book_session, so a booking cannot skip it."""
        driver = self.get_driver_by_id(driver_id)
        if not driver:
            return False, "Driver not found"
        car = self.get_car(car_id)
        if not car:
            return False, "Car not found"

        # License level hierarchy: novice < intermediate < advanced
        levels = {"novice": 0, "intermediate": 1, "advanced": 2}
        if levels[driver["license_level"]] < levels[car["min_license_level"]]:
            return False, f"Driver has {driver['license_level']} license but car requires {car['min_license_level']}"
        return True, "Eligible"

    def count(self, table: str) -> int:
        assert table.isidentifier()
        return self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    # ================================================================== safe writes

    _BOOKING_SELECT = (
        "SELECT b.id, b.driver_id, d.name as driver_name, b.car_id, c.name as car_name, "
        "b.track_slot_id, t.slot_time, b.status "
        "FROM booking b "
        "JOIN driver d ON d.id = b.driver_id "
        "JOIN car c ON c.id = b.car_id "
        "JOIN track_slot t ON t.id = b.track_slot_id "
        "WHERE b.id = ?")

    def book_session(self, driver_id: int, car_id: int, track_slot_id: int) -> tuple[dict, bool]:
        """
        Book a track session. Returns (result_dict, newly_created).

        Business rules enforced here, whoever the caller is (model, script, or test):
        - license eligibility: the driver's license must meet the car's minimum -> error "not_eligible";
        - one active booking per slot: enforced by the partial unique index in the schema. A different
          driver's active booking -> error "slot_already_booked".
        - repeat-safe: the same driver re-booking the same slot returns the existing booking,
          newly_created=False.

        On success the result is the full booking row (same shape whether new or existing).
        """
        with self.transaction() as c:
            eligible, reason = self.check_driver_eligibility(driver_id, car_id)
            if not eligible:
                return {"error": "not_eligible", "hint": reason}, False

            existing = c.execute(
                "SELECT id FROM booking WHERE driver_id = ? AND track_slot_id = ? AND status = 'active'",
                (driver_id, track_slot_id)).fetchone()
            if existing:
                return dict(c.execute(self._BOOKING_SELECT, (existing["id"],)).fetchone()), False

            try:
                cur = c.execute(
                    "INSERT INTO booking (driver_id, car_id, track_slot_id, status, created_at) "
                    "VALUES (?, ?, ?, 'active', ?)",
                    (driver_id, car_id, track_slot_id, self.clock()))
            except sqlite3.IntegrityError as e:
                if "UNIQUE constraint failed" in str(e):
                    return {"error": "slot_already_booked", "hint": "This track slot is already booked."}, False
                raise
            return dict(c.execute(self._BOOKING_SELECT, (cur.lastrowid,)).fetchone()), True

    def cancel_booking(self, booking_id: int) -> tuple[dict, bool]:
        """Cancel a booking. Safe to repeat (idempotent)."""
        with self.transaction() as c:
            booking = c.execute("SELECT status FROM booking WHERE id = ?", (booking_id,)).fetchone()
            if not booking:
                return {"error": "unknown_booking", "hint": "Booking not found."}, False
            
            if booking["status"] == "cancelled":
                return {"booking_id": booking_id, "status": "already_cancelled"}, False
            
            c.execute("UPDATE booking SET status = 'cancelled' WHERE id = ?", (booking_id,))
            return {"booking_id": booking_id, "status": "cancelled"}, True

    def record_notification(self, driver_id: int, message: str, dedupe_key: str) -> tuple[int, bool]:
        """Record a notification. Safe to repeat (returns existing if already recorded)."""
        cur = self.conn.execute(
            "INSERT INTO notification (driver_id, message, dedupe_key, created_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (dedupe_key) DO NOTHING", (driver_id, message, dedupe_key, self.clock()))
        if cur.rowcount == 1:
            return cur.lastrowid, True
        return self.conn.execute("SELECT id FROM notification WHERE dedupe_key = ?", (dedupe_key,)).fetchone()[0], False

    def once(self, key: str, tool_name: str, effect: Callable[[], dict]) -> tuple[dict, bool]:
        """Run a side effect at most once per idempotency key; the effect and its key commit together."""
        with self.transaction() as c:
            row = c.execute("SELECT result FROM idempotency WHERE key = ?", (key,)).fetchone()
            if row is not None:
                return json.loads(row["result"]), False
            result = effect()
            c.execute("INSERT INTO idempotency (key, tool_name, result, created_at) VALUES (?, ?, ?, ?)",
                      (key, tool_name, json.dumps(result, default=str), self.clock()))
            return result, True
