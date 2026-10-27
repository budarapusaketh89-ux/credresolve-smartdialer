# Architecture and state diagrams

## Component and event flow

```mermaid
flowchart LR
    Campaign[Campaign] --> Mode{Dialing mode}
    Mode -->|Progressive| Progressive[Progressive pacing]
    Mode -->|Predictive| Predictive[Predictive pacing]
    Progressive --> Proposal[Call proposal]
    Predictive --> Proposal
    Proposal --> Safety[Independent Safety Controller]
    Safety -->|approved reservations| Allocator[Call Allocator]
    Allocator -->|transaction| DB[(SQLite database)]
    Allocator --> Outbox[(Transactional outbox)]
    Outbox --> Worker[Outbox worker]
    Worker -->|provider selection| Fast[FastMockProvider]
    Worker -->|provider selection| Unreliable[UnreliableMockProvider]
    Fast -->|provider result| Worker
    Unreliable -->|provider result| Worker
    Worker -->|acknowledgment and retry state| DB
    Fast -->|provider callback| Callback[Provider-event endpoint]
    Unreliable -->|provider callback| Callback
    Callback -->|deduplicate and validate transition| DB
    Callback -->|call state changes| Agent[Agent and borrower state]
    DB --> Safety
```

Pacing computes a proposal only. The Safety Controller rereads live capacity in the reservation transaction and controls whether the Call Allocator can reserve agents and borrowers. Provider delivery happens after the reservation and outbox transaction commits.

## Agent state machine

```mermaid
stateDiagram-v2
    [*] --> AVAILABLE
    OFFLINE --> AVAILABLE
    AVAILABLE --> RESERVED
    AVAILABLE --> PAUSED
    AVAILABLE --> OFFLINE
    RESERVED --> DIALING
    RESERVED --> CONNECTED
    RESERVED --> AVAILABLE
    RESERVED --> PAUSED
    RESERVED --> OFFLINE
    DIALING --> CONNECTED
    DIALING --> AVAILABLE
    DIALING --> PAUSED
    DIALING --> OFFLINE
    CONNECTED --> WRAP_UP
    CONNECTED --> AVAILABLE
    CONNECTED --> OFFLINE
    WRAP_UP --> AVAILABLE
    WRAP_UP --> PAUSED
    WRAP_UP --> OFFLINE
    PAUSED --> AVAILABLE
    PAUSED --> OFFLINE
```

## Call state machine

```mermaid
stateDiagram-v2
    [*] --> QUEUED
    QUEUED --> RESERVED
    QUEUED --> CANCELLED
    RESERVED --> INITIATED
    RESERVED --> RINGING
    RESERVED --> ANSWERED
    RESERVED --> FAILED
    RESERVED --> CANCELLED
    INITIATED --> RINGING
    INITIATED --> ANSWERED
    INITIATED --> FAILED
    INITIATED --> CANCELLED
    RINGING --> ANSWERED
    RINGING --> FAILED
    RINGING --> CANCELLED
    ANSWERED --> CONNECTED
    ANSWERED --> COMPLETED
    ANSWERED --> FAILED
    CONNECTED --> COMPLETED
    CONNECTED --> FAILED
    COMPLETED --> [*]
    FAILED --> [*]
    CANCELLED --> [*]
```

The state diagrams reflect transition tables in `app/services.py`; they do not imply that callbacks without timestamps or sequence numbers can be perfectly ordered.
