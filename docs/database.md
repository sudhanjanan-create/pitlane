# PitLane Database Design

PitLane uses two separate SQLite databases.

## Domain Database

The domain database stores:

- Drivers
- Cars
- Track slots
- Bookings
- Notifications

Schema:

[View pitlane.sql](../schema/pitlane.sql)

## Agent Database

The agent database stores durable execution state:

- Queued runs
- Worker leases
- Execution attempts
- Idempotency records
- Stored tool results

Schema:

[View agent.sql](../schema/agent.sql)
