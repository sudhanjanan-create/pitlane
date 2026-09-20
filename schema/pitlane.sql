-- pitlane.db: the motorsport track facility's data. The agent's memory is in agent.db.

CREATE TABLE IF NOT EXISTS driver (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    license_level TEXT NOT NULL CHECK (license_level IN ('novice', 'intermediate', 'advanced')),
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS car (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    min_license_level TEXT NOT NULL CHECK (min_license_level IN ('novice', 'intermediate', 'advanced')),
    available       INTEGER NOT NULL DEFAULT 1 CHECK (available IN (0, 1)),
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS track_slot (
    id              INTEGER PRIMARY KEY,
    slot_time       TEXT NOT NULL UNIQUE,  -- ISO 8601 datetime
    duration_minutes INTEGER NOT NULL DEFAULT 60,
    created_at      REAL NOT NULL
);

-- Business rule: a track slot can have at most one ACTIVE booking.
-- Enforced by the partial unique index below, not by application code: a second active booking for the
-- same slot is rejected by SQLite itself, even from a raw INSERT. Cancelled rows are kept for history and
-- do not block the slot, so a cancelled slot can be booked again.
CREATE TABLE IF NOT EXISTS booking (
    id              INTEGER PRIMARY KEY,
    driver_id       INTEGER NOT NULL REFERENCES driver (id),
    car_id          INTEGER NOT NULL REFERENCES car (id),
    track_slot_id   INTEGER NOT NULL REFERENCES track_slot (id),
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled')),
    created_at      REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS booking_one_active_per_slot
    ON booking (track_slot_id) WHERE status = 'active';   -- KEY CONSTRAINT

CREATE TABLE IF NOT EXISTS notification (
    id          INTEGER PRIMARY KEY,
    driver_id   INTEGER NOT NULL REFERENCES driver (id),
    message     TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL
);

-- Idempotency keys live next to the side effects they guard, so a side effect and its key commit together.
CREATE TABLE IF NOT EXISTS idempotency (
    key         TEXT PRIMARY KEY,
    tool_name   TEXT NOT NULL,
    result      TEXT NOT NULL,
    created_at  REAL NOT NULL
);
