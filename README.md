# CredResolve SmartDialer

Educational SmartDialer prototype using Python, FastAPI, SQLAlchemy, SQLite, and mock telecom providers. It demonstrates pacing proposals, an independent reservation safety controller, a durable provider outbox, provider-event handling, and a deterministic discrete-time simulation. It is not production telephony software.

## Setup and run (Windows PowerShell)

From the repository root:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open the SmartDialer dashboard at http://127.0.0.1:8000/ or the interactive API at http://127.0.0.1:8000/docs. The SQLite database is `smartdialer.db`; startup creates missing tables and adds the provider-event columns needed by this version.

## Dashboard

The responsive dashboard is served by the FastAPI application from `frontend/` and uses same-origin API requests. It displays available agents from `GET /agents` and active/answered call counts and recent records from `GET /calls`. The simulation panel posts its supported settings to `POST /simulate`; pacing, Safety Controller, provider, answer, and capacity-invariant results are taken directly from that response. Both dialing modes control the simulation only and do not initiate live dialing.

There is no campaign-list endpoint or persistent event-history endpoint, so campaign creation is linked to its API documentation, and the dashboard displays decisions from the latest simulation rather than presenting them as a stored event feed. Safety rejections remain unavailable until a simulation has run. The API connection indicator checks `GET /health`.

To run the dashboard locally, use the same commands above and open **http://127.0.0.1:8000/**. No separate frontend build or development server is required.

## Tests

```powershell
python -m pytest -q
```

The tests cover simulation drops and deterministic outcomes, capacity/pacing behavior, provider failures and retries, event ordering and duplicate IDs, and concurrent SQLite reservations.

## Quick-start API flow

1. `POST /agents`, for example `{"name":"Agent A","status":"AVAILABLE"}`.
2. `POST /borrowers`, for example `{"name":"Borrower A","phone":"+911234567890"}`.
3. `POST /campaigns`, for example `{"name":"Test","mode":"PREDICTIVE","answer_rate":0.3,"avg_talk_seconds":120}`.
4. `POST /campaigns/{id}/dial` with `{"campaign_id":1,"max_calls":10}`.
5. Send lifecycle events using `POST /provider-events`, e.g. `{"event_id":"evt-1","call_id":1,"status":"RINGING"}`. Optional `occurred_at` timestamps allow stale provider events to be rejected.

`GET /campaigns/{id}/pacing` previews `proposed_calls`; it does not reserve agents. Its legacy `approved_calls` field is deprecated and mirrors the proposal only for compatibility. `POST /campaigns/{id}/dial` reports both the in-transaction `proposed_calls` and `safety_approved_calls`; only the latter is the count actually reserved by the Safety Controller. The returned calls can still fail at the provider later. `POST /simulate` runs a seeded, local in-memory simulation. For the reported drop scenario, use `agent_count: 20`, `agent_drop_fraction: 0.4`, `agent_drop_at_second: 30`, and a fixed `seed`; the workforce falls by 8 agents at second 30.

`POST /simulate/scenarios` runs deterministic assignment scenarios A–D (20%/120 seconds, 50%/90 seconds, 70%/180 seconds, and changing conditions) with a supplied seed, for example:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/simulate/scenarios `
  -ContentType "application/json" -Body '{"seed":7,"mode":"PREDICTIVE"}'
```

Simulation outputs are modeled results, not real telecom forecasts. Provider latency is modeled separately from setup delay; the discrete-time simulation rounds positive latencies up to ticks and has a one-second minimum dispatch tick. `calls_proposed` totals proposal opportunities across ticks; it is not a count of unique calls. `calls_reserved` and `provider_accepted_calls` distinguish actual simulated reservations from provider acceptance. Results include lifecycle totals, utilization time series, and per-tick pacing/Safety Controller decisions.

## Provider outbox

Reservations and outbox rows commit together. A worker claims due rows, calls the provider outside the reservation transaction, then marks the message sent or schedules exponential retry. Failed messages stop after three attempts and release agent/borrower reservations. Run the worker in another PowerShell window:

```powershell
python -m app.worker
```

`POST /outbox/dispatch` is also available for a bounded manual dispatch. An expired `PROCESSING` lease can be reclaimed by another worker; a crashed worker's network outcome may be unknown. The mock providers cache accepted idempotency keys only for the lifetime of that process. A real provider may duplicate an external call if the worker crashes after the provider accepts it but before the database records success, unless the provider itself supports durable idempotency. This design is at-least-once, not an exactly-once external-call guarantee. Repeated worker crashes can also cause lease reclaims beyond the normal provider-error retry limit; manual reconciliation is necessary if the result is ambiguous.

## Basic load test

Start the app, then use an isolated database for dial workloads so test records do not mix with application data. In PowerShell:

```powershell
$env:DATABASE_URL = "sqlite:///./loadtest.sqlite"
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In a second terminal, set the same database URL and run one of:

```powershell
$env:DATABASE_URL = "sqlite:///./loadtest.sqlite"
python scripts/load_test.py --workload health --requests 500 --concurrency 20
python scripts/load_test.py --workload dial --mode PROGRESSIVE --requests 100 --concurrency 10
python scripts/load_test.py --workload mixed --mode PREDICTIVE --requests 100 --concurrency 10
```

`dial` and `mixed` provision their agents, borrowers, and campaign before timed requests; provisioning time is excluded. Each measured dial request asks for one call and counts a reservation result other than one as a failure. Output includes request count, concurrency, failures/errors, throughput, and p95 latency. The script reports measured results only; local results are not production guarantees and SQLite results do not represent a horizontally scaled deployment.

## Modes and safety boundary

- **Progressive:** proposes up to one call for each currently available agent. It does not intentionally place unanswered calls ahead of agent capacity.
- **Predictive:** estimates attempts using configured answer rate, connected calls, setup reservations, total staffed capacity, requested-call limit, and the configured over-dial ratio. It only proposes calls; it never contacts a provider or grants permission to reserve.
- **Safety Controller:** recalculates current available agents and staffed capacity inside the reservation transaction and subtracts existing active calls. Each call atomically reserves one available agent and one queued borrower before creating its outbox message. No predictive estimate can bypass this final gate.

See [docs/architecture-diagram.md](docs/architecture-diagram.md) for the architecture and state-machine diagrams, [docs/architecture-decision-record.md](docs/architecture-decision-record.md) for technology and scaling decisions, and [docs/architecture.md](docs/architecture.md) for transaction boundaries, assumptions, and limitations.
## Validation Results

### Automated Tests
- Test suite: **31 passed**
- Python compilation: successful using `python -m compileall -q app tests`
- Swagger API smoke test: endpoints verified successfully

### Simulation Results
Predictive simulation executed with seed 7 for 300 simulated seconds per scenario.

| Scenario | Proposed | Reserved | Provider Accepted | Answered | Provider Failures | Safety Rejections | Capacity Safe |
|---|---:|---:|---:|---:|---:|---:|---|
| A | 558 | 356 | 332 | 65 | 24 | 202 | Yes |
| B | 332 | 199 | 191 | 100 | 8 | 133 | Yes |
| C | 169 | 95 | 87 | 58 | 8 | 74 | Yes |
| D | 542 | 353 | 331 | 64 | 22 | 189 | Yes |

All four scenarios reported that the capacity invariant held. Simulation outputs are modeled results, not real-world telephony forecasts.

### Local Load Test
- Workload: mixed
- Requests: 100
- Concurrency: 10
- Duration: 2.852 seconds
- Throughput: 35.06 requests/second
- p95 latency: 1,590.68 ms
- Failures: 0
- Errors: 0

These are local prototype measurements and are not production performance guarantees.