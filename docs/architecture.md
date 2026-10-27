# Architecture and design decisions

## Request and provider flow

1. A dial request loads campaign settings and computes a pacing proposal.
2. The Safety Controller starts a write transaction, reloads the campaign, recalculates availability, and independently caps the number of calls to reserve.
3. Each approved call conditionally changes one `AVAILABLE` agent and one `QUEUED` borrower to `RESERVED`; it persists the call and its unique outbox row in the same transaction.
4. After reservation commit, a worker claims outbox messages using a processing lease and invokes the selected provider outside the reservation transaction.
5. Accepted calls become `INITIATED`. Failures are retried with bounded exponential backoff; exhausted calls become `FAILED` and release reservations.
6. Provider events are deduplicated by event ID, checked against the lifecycle state machine, and applied with agent/borrower state changes in a transaction.

The mock provider adapter is intentionally small. A production adapter must define network timeouts, provider-side idempotency, error classification, credentials management, and reconciliation behavior.

## State machines

Call transitions:

```text
QUEUED:     RESERVED, CANCELLED
RESERVED:   INITIATED, FAILED, CANCELLED
INITIATED:  RINGING, ANSWERED, FAILED, CANCELLED
RINGING:    ANSWERED, FAILED, CANCELLED
ANSWERED:   CONNECTED, COMPLETED, FAILED
CONNECTED:  COMPLETED, FAILED
COMPLETED, FAILED, CANCELLED: terminal
```

The common successful path is `QUEUED -> RESERVED -> INITIATED -> RINGING -> ANSWERED -> CONNECTED -> COMPLETED`. The implementation permits `INITIATED -> ANSWERED`, `ANSWERED -> COMPLETED`, and `ANSWERED -> FAILED` for providers that omit intermediate callbacks or calls that end before a separate connected event. Terminal states cannot transition again. Illegal transitions are recorded as ignored; when provider event timestamps are supplied, events older than the latest applied timestamp for that call are also ignored.

A provider callback may arrive before the worker commits its successful initiate response. In that window, `RESERVED -> RINGING` and `RESERVED -> ANSWERED` are accepted, and outbox completion does not move a call backward to `INITIATED`. The agent state transition helper rejects moves outside its declared transition table.

Agent state flow for a successful call:

```text
AVAILABLE -> RESERVED -> DIALING -> CONNECTED -> AVAILABLE
```

Provider rejection after retries exhausts restores `AVAILABLE` and `QUEUED`. An event does not make an agent available if that agent has since entered a non-call state such as `OFFLINE` or `PAUSED`.

Outbox states:

```text
PENDING -> PROCESSING -> SENT
                    \-> RETRY -> PROCESSING
                    \-> FAILED (attempt limit reached)
```

An expired `PROCESSING` lease is eligible for recovery. This recovers database work after a worker crash, but cannot determine whether a provider performed an external action just before the crash.

## Pacing and independent safety

The agent pool is shared across campaigns, so connected calls and setup reservations used in this capacity estimate are counted globally. The progressive proposal is:

```text
min(requested calls, currently AVAILABLE agents)
```

For predictive pacing, let `r` be the configured answer rate clamped to `[0.05, 0.95]`, `C` be staffed agents in capacity-bearing states, `A` be connected calls, `S` be calls being set up, and `V` be currently available agents:

```text
expected_unfilled_capacity = max(0, C - A - S * r)
attempts_to_fill = ceil(expected_unfilled_capacity / r)
overdial_limit = ceil(V * max_predictive_overdial_ratio)
proposal = min(requested calls, attempts_to_fill, overdial_limit)
```

The predictive formula is an explainable heuristic, not a trained forecast. The pacing endpoint reports `proposed_calls`; its legacy `approved_calls` field is deprecated and mirrors the proposal only for compatibility. The dial endpoint reports `proposed_calls` and `safety_approved_calls` separately. The latter is the number reserved in the transaction and can still be followed by provider rejection. Actual dialing rechecks provider health, live available-agent count, and staffed capacity minus all connected/setup calls within the Safety Controller transaction. It then conditionally reserves each agent and borrower. Thus an over-dial proposal can never create more simultaneous reserved calls than available capacity in that transaction, even if a persisted agent state is inconsistent with an active call.

## Transactions and concurrency

For SQLite, reservation and outbox writes use `BEGIN IMMEDIATE`, serializing competing writers before availability and borrower state are read. Conditional status updates provide a second guard against duplicate reservations. A threaded test verifies distinct agent and borrower assignments for competing requests.

SQLite is a single-writer database. This strategy is suitable for the local demo, not horizontal high-throughput workers. PostgreSQL row locking is requested by the ORM, but this prototype has no production migration suite, robust retry policy for serialization failures, or tested multi-host deployment. A production deployment should use a transactional database, explicit transaction isolation, migration tooling, lock/lease monitoring, and contention tests against that database.

The existing `smartdialer.db` is upgraded additively at application startup: `create_all` adds the outbox table, and missing nullable event-time/applied columns are added to the provider-events table. This is a minimal demo migration, not a replacement for versioned production migrations.

## Event delivery and idempotency

Provider event IDs are unique in storage. Duplicate IDs are ignored. The call state machine rejects illegal transitions; optional provider timestamps additionally prevent older events from regressing the call. Callback-before-initiation-ack ordering is covered by a test; outbox completion must not regress the callback-applied call state. When timestamps are absent, event ordering can only be inferred from legal state transitions; callbacks with no sequence/timestamp cannot be reliably sorted. The tests do not establish ordering correctness across providers that omit timestamps or sequence numbers.

Outbox rows use a stable `call-{id}` idempotency key and claim leases. Provider invocation is outside the database transaction to avoid holding database locks across network calls. An expired `PROCESSING` lease is reclaimed. Deterministic tests cover a crash before provider invocation and the ambiguous case where a provider accepted before a crash; a non-idempotent fake demonstrates that the later retry can create a duplicate external delivery. The fast mock's accepted-key cache is process-local and the unreliable mock has a controllable RNG for tests. Exactly-once external calls are not claimed: provider-side durable idempotency is required to close the acceptance/commit gap. Explicit provider errors receive bounded exponential retries, but repeated worker crashes can cause repeated lease reclaims beyond that provider-error attempt limit and require reconciliation.

## Simulation assumptions and assignment scenarios

`POST /simulate` uses a local seeded random generator and an event-step simulation. Every in-flight call reserves one agent; setup consumes capacity; an answer moves through the call lifecycle; talk duration is exponentially sampled and capped at ten times the configured average; provider failures and non-answers release their agent; an agent drop removes a fraction of workforce and fails calls held by dropped agents. New calls stop after the dialing window and active calls are drained to terminal states. Provider latency is modeled separately from setup delay; latency is rounded to simulation ticks and dispatch has at least one tick of delay. This is a reproducible behavioral model, not a telephony performance prediction.

`POST /simulate/scenarios` runs reproducible A–D examples: 20% answer rate/120-second average talk time, 50%/90 seconds, 70%/180 seconds, and a changing-rate/talk-time case. Results include proposal opportunities (not unique call intents), reserved calls, provider-accepted calls, answers, completions, failures, safety rejections, utilization samples, and per-tick decisions. Changing conditions affect calls when their setup completes. These are modeled results, not real telecom forecasts.

## Run and verify

From the repository root on Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pytest -q
uvicorn app.main:app --reload
python -m app.worker
$env:DATABASE_URL = "sqlite:///./loadtest.sqlite"
python scripts/load_test.py --workload mixed --mode PREDICTIVE --requests 100 --concurrency 10
python scripts/load_test.py --workload dial --mode PROGRESSIVE --requests 100 --concurrency 10
```

Run the application and worker in separate terminals. Set `DATABASE_URL` to the same disposable SQLite file for the app and load script; the dial workloads create their own test records before the timed phase. `--workload health`, `dial`, and `mixed` select health-only, call reservation/dial, or alternating request workloads. The dial workload includes SQLite reservation contention and mock provider dispatch but is still only a local smoke/load test, not a production provider or multi-host throughput guarantee.

## Remaining production limitations

- No authentication/authorization or campaign-level access control.
- Mock telecom integrations only; no real callback signature validation or provider reconciliation.
- SQLite cannot provide scalable multi-writer throughput; concurrency evidence is limited to local SQLite tests.
- No versioned migrations, agent presence API, lease expiry for agent reservations, or wrap-up workflow.
- Simulation rates and exponential durations are assumptions, and predictive pacing is heuristic.

See [architecture-diagram.md](architecture-diagram.md) for Mermaid component/state diagrams and [architecture-decision-record.md](architecture-decision-record.md) for the stack rationale, scaling path, worked pacing example, and least-confident area.
