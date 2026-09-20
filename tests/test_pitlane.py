"""Comprehensive tests for PitLane track booking system."""
import time
import pytest
from app.pitlane_db import PitLaneDb
from app.memory import RunStore
from app.providers import demo_providers
from app.worker import Worker
from app.idempotency import idempotency_key


class TestDatabaseInitialization:
    """Test 1: Database initialization and schema."""
    
    def test_domain_db_migrate(self):
        """Test that domain database initializes correctly."""
        db = PitLaneDb(":memory:")
        db.migrate()
        
        assert db.count("driver") == 4
        assert db.count("car") == 4
        assert db.count("track_slot") == 12
        assert db.count("booking") == 0
    
    def test_agent_db_migrate(self):
        """Test that agent database initializes correctly."""
        store = RunStore(":memory:")
        store.migrate()
        
        # Create a thread and verify it stores correctly
        thread_id = store.create_thread("test_student")
        thread = store.get_thread(thread_id)
        assert thread is not None
        assert thread["student_id"] == "test_student"
    
    def test_seed_data(self):
        """Test 2: Seed data is present."""
        db = PitLaneDb(":memory:")
        db.migrate()
        
        drivers = db.list_drivers()
        assert len(drivers) == 4
        assert any(d["name"] == "Alice Johnson" for d in drivers)
        
        cars = db.list_cars()
        assert len(cars) == 4
        assert any(c["name"] == "GT3 Porsche" for c in cars)


class TestReadOnlyTools:
    """Tests 3-5: Read-only tool functionality."""
    
    def test_list_cars(self):
        """Test 3: list_cars tool."""
        db = PitLaneDb(":memory:")
        db.migrate()
        
        cars = db.list_cars()
        assert len(cars) == 4
        car_names = [c["name"] for c in cars]
        assert "GT3 Porsche" in car_names
        assert "Formula-3" in car_names
    
    def test_list_track_slots(self):
        """Test 4: list_track_slots tool."""
        db = PitLaneDb(":memory:")
        db.migrate()
        
        slots = db.list_track_slots()
        assert len(slots) == 12
        # All should be unbooked initially
        assert all(s["booked"] == False for s in slots)
    
    def test_check_driver_eligibility(self):
        """Test 5: check_driver_eligibility tool."""
        db = PitLaneDb(":memory:")
        db.migrate()
        
        # Alice (advanced) should be eligible for GT3 (intermediate)
        eligible, reason = db.check_driver_eligibility(1, 1)
        assert eligible is True
        
        # Charlie (novice) should NOT be eligible for Formula-3 (advanced)
        eligible, reason = db.check_driver_eligibility(3, 2)
        assert eligible is False
        assert "advanced" in reason


class TestBooking:
    """Tests 6-7: Booking functionality."""
    
    def test_successful_booking(self):
        """Test 6: Successful booking creation."""
        db = PitLaneDb(":memory:")
        db.migrate()
        
        # Alice books GT3 for slot 1
        result, newly_created = db.book_session(1, 1, 1)
        
        assert newly_created is True
        assert "id" in result
        assert result["driver_name"] == "Alice Johnson"
        assert result["car_name"] == "GT3 Porsche"
        assert result["status"] == "active"
    
    def test_conflicting_booking_rejected(self):
        """Test 7: Conflicting booking rejected by database constraint."""
        db = PitLaneDb(":memory:")
        db.migrate()
        
        # Alice books GT3 for slot 1
        result1, created1 = db.book_session(1, 1, 1)
        assert created1 is True
        
        # Bob tries to book GT3 for same slot 1 (different driver, same slot)
        result2, created2 = db.book_session(2, 1, 1)
        
        assert created2 is False
        assert "error" in result2
        assert result2["error"] == "slot_already_booked"


class TestCancellation:
    """Test 8: Cancellation functionality."""
    
    def test_cancel_booking(self):
        """Test 8: Cancellation of booking."""
        db = PitLaneDb(":memory:")
        db.migrate()
        
        # Create a booking
        result, _ = db.book_session(1, 1, 1)
        booking_id = result["id"]
        
        # Verify booking exists
        assert db.count("booking") == 1
        
        # Cancel it
        cancel_result, changed = db.cancel_booking(booking_id)
        assert changed is True
        assert cancel_result["status"] == "cancelled"
        
        # Booking should still be in DB but marked cancelled
        booking = db.get_booking(booking_id)
        assert booking["status"] == "cancelled"


class TestNotifications:
    """Test 9: Notification creation."""
    
    def test_notification_creation(self):
        """Test 9: Notification recording."""
        db = PitLaneDb(":memory:")
        db.migrate()
        
        driver_id = 1
        message = "Your booking is confirmed"
        dedupe_key = "test-key-1"
        
        notif_id, created = db.record_notification(driver_id, message, dedupe_key)
        
        assert created is True
        assert notif_id > 0
        
        # Verify it was stored
        row = db.conn.execute("SELECT * FROM notification WHERE id = ?", (notif_id,)).fetchone()
        assert row is not None
        assert row["message"] == message


class TestIdempotency:
    """Tests 10-11: Idempotency."""
    
    def test_idempotent_booking_replay(self):
        """Test 10: Booking replay with same key returns existing result."""
        db = PitLaneDb(":memory:")
        db.migrate()
        
        key = idempotency_key("run-1", 1, "book_track_session", {"driver_id": 1, "car_id": 1, "slot_id": 1})
        
        def create_booking():
            return db.book_session(1, 1, 1)[0]
        
        # First call
        result1, fresh1 = db.once(key, "book_track_session", create_booking)
        assert fresh1 is True
        assert "id" in result1
        booking_id = result1["id"]
        
        # Second call with same key should return cached result
        result2, fresh2 = db.once(key, "book_track_session", create_booking)
        assert fresh2 is False
        assert result2["id"] == booking_id  # Same booking
        
        # Verify only one booking exists
        assert db.count("booking") == 1
    
    def test_idempotent_notification_replay(self):
        """Test 11: Notification replay with same key returns existing result."""
        db = PitLaneDb(":memory:")
        db.migrate()
        
        key = "notif-test-key"
        message = "Test notification"
        driver_id = 1
        
        # First notification
        notif1, created1 = db.record_notification(driver_id, message, key)
        assert created1 is True
        
        # Second with same key
        notif2, created2 = db.record_notification(driver_id, message, key)
        assert created2 is False
        assert notif1 == notif2  # Same notification
        
        # Only one should exist
        assert db.count("notification") == 1


class TestQueueAndWorker:
    """Tests 12-14: Queue and worker."""
    
    def test_queue_insertion(self):
        """Test 12: Queue insertion."""
        store = RunStore(":memory:")
        store.migrate()
        
        thread_id = store.create_thread("test_driver")
        run_id = store.enqueue(thread_id, "Book a car", "mock")
        
        run = store.get_run(run_id)
        assert run["status"] == "queued"
        assert run["thread_id"] == thread_id
    
    def test_worker_claim(self):
        """Test 13: Worker claims run with lease."""
        store = RunStore(":memory:")
        store.migrate()
        
        thread_id = store.create_thread("test_driver")
        run_id = store.enqueue(thread_id, "Book a car", "mock")
        
        claimed = store.claim_next("worker-1", lease_seconds=60.0)
        assert claimed is not None
        assert claimed.run_id == run_id
        assert claimed.attempts == 1
        
        run = store.get_run(run_id)
        assert run["status"] == "running"
        assert run["lease_owner"] == "worker-1"
    
    def test_lease_expiry_and_recovery(self):
        """Test 14: an expired lease is reaped, and a second worker can claim and own the run."""
        from tests.helpers import FakeClock

        clock = FakeClock()
        store = RunStore(":memory:", clock=clock)
        store.migrate()

        thread_id = store.create_thread("test_driver")
        run_id = store.enqueue(thread_id, "Book a car", "mock")

        claimed = store.claim_next("worker-1", lease_seconds=10.0)
        assert claimed is not None

        clock.advance(11.0)                       # worker-1 "dies": its lease runs out

        assert store.reap_expired() == [run_id]
        assert store.get_run(run_id)["status"] == "queued"

        reclaimed = store.claim_next("worker-2", lease_seconds=10.0)
        assert reclaimed is not None and reclaimed.run_id == run_id
        assert reclaimed.attempts == 2
        run = store.get_run(run_id)
        assert run["status"] == "running" and run["lease_owner"] == "worker-2"


class TestMultiAgent:
    """Tests 15-16: Multi-agent delegation."""
    
    def test_supervisor_delegation_exists(self):
        """Test 15: Supervisor has delegation tools."""
        from app.agents import SupervisorTools
        
        db = PitLaneDb(":memory:")
        db.migrate()
        
        providers = demo_providers()
        supervisor = SupervisorTools(db, providers, "Alice Johnson")
        
        # Check that delegation tools exist
        assert "ask_vehicle_specialist" in supervisor.TOOL_NAMES
        assert "ask_booking_specialist" in supervisor.TOOL_NAMES
        assert "ask_vehicle_specialist" in supervisor.DELEGATES
        assert "ask_booking_specialist" in supervisor.DELEGATES
    
    def test_specialist_has_no_write_tools(self):
        """Test 16: the vehicle specialist is only ever given read-only tools."""
        from app.tools.pitlane_tools import VehicleSpecialistTools, BookingSpecialistTools

        db = PitLaneDb(":memory:")
        db.migrate()

        vehicle = VehicleSpecialistTools(db)
        booking = BookingSpecialistTools(db, "Alice Johnson")

        assert vehicle.SIDE_EFFECTS == ()
        assert set(vehicle.functions()) == {"list_cars", "get_car_details", "check_driver_eligibility",
                                            "list_track_slots"}
        # ...and none of the booking specialist's side-effect tools are reachable through it
        assert not set(vehicle.functions()) & set(booking.SIDE_EFFECTS)
        assert set(booking.SIDE_EFFECTS) == {"book_track_session", "cancel_track_session", "send_notification"}


class TestEndToEnd:
    """Tests 17-20: End-to-end scenarios."""
    
    def test_booking_count_after_conflict(self):
        """Test 19: Only one active booking per slot despite conflicts by eligible drivers."""
        db = PitLaneDb(":memory:")
        db.migrate()
        
        # Book slot 1 with Alice
        result1, _ = db.book_session(1, 1, 1)
        
        # Try to book same slot with Bob
        result2, created = db.book_session(2, 1, 1)
        
        # Try to book same slot with Diana (advanced, so eligible: the ONLY thing stopping her is the slot)
        result3, created = db.book_session(4, 1, 1)
        assert created is False and result3["error"] == "slot_already_booked"
        
        # Only one booking should exist
        assert db.count("booking") == 1
        bookings = db.list_bookings()
        assert len(bookings) == 1
        assert bookings[0]["driver_name"] == "Alice Johnson"
    
    def test_notification_count_no_duplication(self):
        """Test 20: Notifications not duplicated after replay."""
        db = PitLaneDb(":memory:")
        db.migrate()
        
        key = "notification-key-1"
        
        # Send notification twice with same key
        notif1, created1 = db.record_notification(1, "Booking confirmed", key)
        notif2, created2 = db.record_notification(1, "Booking confirmed", key)
        
        assert created1 is True
        assert created2 is False
        assert notif1 == notif2
        
        # Only one should exist in DB
        count = db.conn.execute("SELECT COUNT(*) as cnt FROM notification").fetchone()["cnt"]
        assert count == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
