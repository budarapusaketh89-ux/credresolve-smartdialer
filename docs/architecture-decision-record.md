# Architecture decision record: SmartDialer prototype

## Decision and context

The project uses Python, FastAPI, SQLAlchemy, and SQLite with in-process mock telecom adapters. This stack matches the small assignment prototype, is straightforward to run locally, and keeps the API, persistence, and worker behavior inspectable. SQLite avoids operating a separate database for a single-user demonstration. It is not selected as the production database for high write concurrency.

The simpler alternative was a single synchronous API handler that calls the provider and then writes the call status. That couples provider latency and availability to API requests and leaves no durable work record on process failure. The selected transactional outbox adds a small worker and retry state, at the cost of lease/recovery logic and a duplicate-delivery window. A message broker was considered unnecessary for this scale; a broker could later distribute dispatch work but would not remove the need for durable reservation and idempotency.

## Reservation and delivery trade-offs

The Safety Controller recomputes availability and active reservations within the write transaction. Agent, borrower, call, and outbox changes commit together. SQLite uses `BEGIN IMMEDIATE`, which serializes writers and is suitable for the demo but limits write throughput. Provider invocation occurs after commit so no database lock is held across network calls. The outbox lease permits another worker to reclaim work after a crash; if the provider accepted before the crash and has no durable idempotency mechanism, retry can create another external call. The design is at-least-once and does not claim exactly-once provider delivery.

## Scaling path and bottlenecks

- **Around 1,000 agents:** SQLite's single-writer serialization, repeated capacity counts, and row selection become the first likely bottlenecks. Measure query time and lock wait; add workload indexes and move to PostgreSQL with migrations and contention tests before increasing worker concurrency.
- **Around 100,000 agents:** large agent/borrower scans and a single global allocation/transaction path are additional bottlenecks, alongside provider callback and outbox throughput. Use indexed eligibility queues, partition work by campaign or shard key, bounded worker pools, queue/lease observability, and a production database with tested row-locking/claim semantics. Introduce partitioning or separate services only when measured profiles justify them.

These are design risks and staged mitigations, not measured benchmark results.

## Worked proposal example: 17 rather than 10

Suppose a campaign requests up to 20 dials, with 13 agents currently available, 100 staffed agents, 80 connected calls, 6 calls still setting up, and a 50% expected answer rate. The illustrative heuristic computes expected unfilled capacity as `max(0, 100 - 80 - 6 * 0.5) = 17`; attempts needed to fill it are `ceil(17 / 0.5) = 34`. If the configured over-dial ratio is 1.25, the available-agent guard is `ceil(13 * 1.25) = 17`. The proposal is `min(20, 34, 17) = 17`, rather than a fixed 10.

That number is only a pacing proposal. The Safety Controller independently rechecks current available agents and active call reservations in the transaction, then reserves no more than actual capacity (13 in this simplified example). A live agent drop before that transaction can lower approvals further.

## Least-confident area

The least-confident area is real-world pacing calibration: the prototype uses a configured answer-rate estimate and a simple over-dial cap, not a model validated against historical campaign data. Improve it by collecting timestamped, privacy-governed campaign outcomes, testing calibration and abandonment constraints offline, running a shadow-mode comparison, then using staged rollout with hard independent safety limits and rollback monitoring.

## Balancing predictive utilization with progressive safety

Use predictive pacing to suggest additional attempts based on observed answer rates and expected agent availability, but route every suggestion through the same independent reservation-time Safety Controller used by progressive dialing. Keep progressive mode's one-call-per-free-agent proposal available as a deterministic operating mode; never let predictive estimates bypass current-capacity checks.
