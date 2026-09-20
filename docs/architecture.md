# PitLane System Architecture

```mermaid
flowchart TD
    U[User Request] --> Q[Agent Queue]
    Q --> W[Worker]

    W --> S[Supervisor Agent]

    S --> V[Vehicle Specialist]
    S --> B[Booking Specialist]

    V --> R[Read-only Tools]
    B --> T[Side-effect Tools]

    R --> D[(pitlane.db)]
    T --> D

    W --> A[(agent.db)]
    A --> Q

    T --> I[Idempotency Layer]
    I --> A

    W --> L[Lease & Recovery]
    L --> A
