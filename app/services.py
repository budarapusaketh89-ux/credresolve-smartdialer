from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .models import Agent, Borrower, Campaign, DialCall, OutboxMessage, ProviderEventRecord
from .providers import PROVIDERS

ACTIVE_CALL_STATES = {"RESERVED", "INITIATED", "RINGING", "ANSWERED", "CONNECTED"}
SETUP_CALL_STATES = {"RESERVED", "INITIATED", "RINGING"}
CONNECTED_CALL_STATES = {"ANSWERED", "CONNECTED"}
CAPACITY_AGENT_STATES = {"AVAILABLE", "RESERVED", "DIALING", "CONNECTED", "WRAP_UP"}
MAX_PROVIDER_ATTEMPTS = 3
OUTBOX_LEASE_SECONDS = 30


@dataclass
class DialBatch:
    proposed_calls: int
    safety_approved_calls: int
    calls: list[DialCall]


def active_call_count(db: Session, campaign_id: int | None = None) -> int:
    query = select(func.count(DialCall.id)).where(DialCall.status.in_(ACTIVE_CALL_STATES))
    if campaign_id is not None:
        query = query.where(DialCall.campaign_id == campaign_id)
    return int(db.scalar(query) or 0)


def _pacing_snapshot(db: Session) -> tuple[int, int, int, int]:
    available = int(db.scalar(select(func.count(Agent.id)).where(Agent.status == "AVAILABLE")) or 0)
    connected = int(db.scalar(select(func.count(DialCall.id)).where(
        DialCall.status.in_(CONNECTED_CALL_STATES))) or 0)
    setup = int(db.scalar(select(func.count(DialCall.id)).where(
        DialCall.status.in_(SETUP_CALL_STATES))) or 0)
    capacity = int(db.scalar(select(func.count(Agent.id)).where(
        Agent.status.in_(CAPACITY_AGENT_STATES))) or 0)
    return available, connected, setup, capacity


def calculate_predictive_proposal(
    requested_calls: int,
    available_agents: int,
    connected_calls: int,
    setup_calls: int,
    capacity: int,
    answer_rate: float,
) -> int:
    rate = max(0.05, min(0.95, answer_rate))
    unmet_capacity = max(0.0, capacity - connected_calls - setup_calls * rate)
    expected_attempts = ceil(unmet_capacity / rate)
    over_dial_limit = ceil(available_agents * settings.max_predictive_overdial_ratio)
    return max(0, min(requested_calls, expected_attempts, over_dial_limit))


def get_pacing_decision(db: Session, campaign: Campaign, requested_calls: int) -> dict:
    available, connected, setup, capacity = _pacing_snapshot(db)
    ringing = int(db.scalar(select(func.count(DialCall.id)).where(
        DialCall.status == "RINGING")) or 0)
    answer_rate = max(0.05, min(0.95, campaign.answer_rate))
    if campaign.mode == "PROGRESSIVE":
        proposed = min(max(0, requested_calls), available)
        reason = "Progressive mode proposes no more than one call per currently available agent."
    else:
        proposed = calculate_predictive_proposal(
            max(0, requested_calls), available, connected, setup, capacity, answer_rate
        )
        reason = (
            "Predictive mode estimates attempts from expected answer rate and unfilled agent "
            "capacity; the safety controller independently caps reservations."
        )
    if not campaign.provider_healthy:
        proposed = 0
        reason = "Provider marked unhealthy; new calls blocked."
    return {
        "available_agents": available,
        "active_calls": connected + setup,
        "ringing_calls": ringing,
        "connected_calls": connected,
        "setup_calls": setup,
        "capacity": capacity,
        "observed_answer_rate": answer_rate,
        "requested_calls": requested_calls,
        "proposed_calls": max(0, proposed),
        "approved_calls": max(0, proposed),
        "reason": reason,
    }


def _start_reservation_transaction(db: Session) -> None:
    # Each service operation owns its transaction. Discard only a read transaction
    # opened by a preceding query; callers must commit their work before calling in.
    if db.in_transaction():
        db.rollback()
    connection = db.connection()
    if db.get_bind().dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def safety_approve_calls(db: Session, proposed_calls: int) -> int:
    """Deterministic safety gate: cap proposals by live agent, call, and queue capacity.

    This is deliberately separate from predictive pacing. It never trusts the
    pacing engine's estimate as authorization to place calls.
    """
    proposed = max(0, int(proposed_calls))
    available = int(db.scalar(select(func.count(Agent.id)).where(Agent.status == "AVAILABLE")) or 0)
    staffed = int(db.scalar(select(func.count(Agent.id)).where(
        Agent.status.in_(CAPACITY_AGENT_STATES))) or 0)
    occupied = active_call_count(db)
    queued_borrowers = int(db.scalar(select(func.count(Borrower.id)).where(
        Borrower.status == "QUEUED")) or 0)
    free_capacity = max(0, staffed - occupied)
    # The model has one agent per call, so the configured ringing limit cannot
    # increase this bound; the one-call-per-agent invariant is stricter.
    return max(0, min(proposed, available, free_capacity, queued_borrowers))


def reserve_and_dial(
    db: Session,
    campaign: Campaign,
    max_calls: int,
    provider_name: str = "mock-fast",
) -> DialBatch:
    if provider_name not in PROVIDERS:
        raise ValueError("Unknown provider")

    campaign_id = campaign.id
    _start_reservation_transaction(db)
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise LookupError("Campaign not found")

    decision = get_pacing_decision(db, campaign, max_calls)
    proposed_calls = decision["proposed_calls"]
    allowed = safety_approve_calls(db, proposed_calls)
    if not campaign.provider_healthy:
        allowed = 0

    created: list[DialCall] = []
    for _ in range(allowed):
        agent = db.scalar(select(Agent).where(Agent.status == "AVAILABLE")
                          .order_by(Agent.id).limit(1).with_for_update())
        borrower = db.scalar(select(Borrower).where(Borrower.status == "QUEUED")
                             .order_by(Borrower.id).limit(1).with_for_update())
        if agent is None or borrower is None:
            break

        agent_result = db.execute(
            update(Agent).where(Agent.id == agent.id, Agent.status == "AVAILABLE")
            .values(status="RESERVED", version=Agent.version + 1)
        )
        borrower_result = db.execute(
            update(Borrower).where(Borrower.id == borrower.id, Borrower.status == "QUEUED")
            .values(status="RESERVED")
        )
        if agent_result.rowcount != 1 or borrower_result.rowcount != 1:
            db.rollback()
            _start_reservation_transaction(db)
            campaign = db.get(Campaign, campaign_id)
            if campaign is None:
                raise LookupError("Campaign not found")
            break
        transition_agent(agent, "RESERVED")

        call = DialCall(
            campaign_id=campaign.id,
            borrower_id=borrower.id,
            agent_id=agent.id,
            status="RESERVED",
            provider=provider_name,
        )
        db.add(call)
        db.flush()
        db.add(OutboxMessage(call_id=call.id, idempotency_key=f"call-{call.id}"))
        created.append(call)

    db.commit()
    safety_approved_calls = len(created)
    if created:
        dispatch_outbox(db, limit=len(created))
    for call in created:
        db.refresh(call)
    return DialBatch(
        proposed_calls=proposed_calls,
        safety_approved_calls=safety_approved_calls,
        calls=created,
    )


def _claim_outbox(
    db: Session,
    limit: int,
    now: datetime | None = None,
) -> list[tuple[int, str, int]]:
    _start_reservation_transaction(db)
    now = now or datetime.now(timezone.utc)
    due = select(OutboxMessage).where(
        ((OutboxMessage.status.in_(("PENDING", "RETRY"))) &
         (OutboxMessage.next_attempt_at <= now)) |
        ((OutboxMessage.status == "PROCESSING") & (OutboxMessage.lease_until <= now))
    ).order_by(OutboxMessage.id).limit(max(0, limit))
    messages = list(db.scalars(due.with_for_update(skip_locked=True)))
    claims: list[tuple[int, str, int]] = []
    for message in messages:
        message.status = "PROCESSING"
        message.attempts += 1
        message.lease_until = now + timedelta(seconds=OUTBOX_LEASE_SECONDS)
        call = db.get(DialCall, message.call_id)
        if call is not None:
            claims.append((message.id, message.idempotency_key, call.id))
    db.commit()
    return claims


def _finish_outbox(
    db: Session,
    message_id: int,
    result_error: str | None,
) -> None:
    _start_reservation_transaction(db)
    message = db.get(OutboxMessage, message_id)
    if message is None or message.status != "PROCESSING":
        db.rollback()
        return

    call = db.get(DialCall, message.call_id)
    now = datetime.now(timezone.utc)
    if call is None:
        message.status = "FAILED"
        message.last_error = "Associated call no longer exists"
        message.lease_until = None
        db.commit()
        return

    campaign = db.get(Campaign, call.campaign_id)
    agent = db.get(Agent, call.agent_id) if call.agent_id else None
    borrower = db.get(Borrower, call.borrower_id)

    # A callback can arrive before the provider HTTP request returns. Any state
    # beyond RESERVED is evidence that the provider saw the call; never retry it
    # solely because the initiating request timed out after that callback.
    callback_proves_acceptance = call.status in {
        "INITIATED", "RINGING", "ANSWERED", "CONNECTED", "COMPLETED", "FAILED", "CANCELLED"
    }
    if result_error is None or callback_proves_acceptance:
        message.status = "SENT"
        message.last_error = None
        message.lease_until = None
        if campaign is not None:
            campaign.provider_failure_count = 0
            campaign.provider_healthy = True
        if call.status == "RESERVED":
            transition_call(call, "INITIATED")
            call.updated_at = now
        if agent is not None and agent.status == "RESERVED" and call.status == "INITIATED":
            transition_agent(agent, "DIALING")
    else:
        message.last_error = result_error[:500]
        message.lease_until = None
        if campaign is not None:
            campaign.provider_failure_count = (campaign.provider_failure_count or 0) + 1
            if campaign.provider_failure_count >= max(1, settings.provider_failure_threshold):
                campaign.provider_healthy = False
        if message.attempts < MAX_PROVIDER_ATTEMPTS:
            message.status = "RETRY"
            message.next_attempt_at = now + timedelta(seconds=2 ** message.attempts)
        else:
            message.status = "FAILED"
            if call.status in {"RESERVED", "INITIATED", "RINGING"}:
                transition_call(call, "FAILED")
                call.updated_at = now
            if agent is not None and agent.status in {"RESERVED", "DIALING"}:
                transition_agent(agent, "AVAILABLE")
            if borrower is not None and borrower.status == "RESERVED":
                borrower.status = "QUEUED"
    db.commit()


def dispatch_outbox(
    db: Session,
    limit: int = 100,
    now: datetime | None = None,
) -> dict:
    claims = _claim_outbox(db, limit, now)
    outcomes = {"sent": 0, "retrying": 0, "failed": 0, "errors": []}
    for message_id, idempotency_key, call_id in claims:
        call = db.get(DialCall, call_id)
        error: str | None = None
        try:
            if call is None:
                error = "Associated call no longer exists"
            else:
                result = PROVIDERS[call.provider].initiate(call.id, idempotency_key)
                if not result.accepted:
                    error = result.error or "Provider rejected call"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        _finish_outbox(db, message_id, error)
        refreshed = db.get(OutboxMessage, message_id)
        if refreshed is not None:
            outcomes[{"SENT": "sent", "RETRY": "retrying", "FAILED": "failed"}.get(
                refreshed.status, "retrying"
            )] += 1
            if refreshed.last_error:
                outcomes["errors"].append({
                    "call_id": call_id,
                    "attempt": refreshed.attempts,
                    "status": refreshed.status,
                    "error": refreshed.last_error,
                })
    return outcomes


ALLOWED_TRANSITIONS = {
    "QUEUED": {"RESERVED", "CANCELLED"},
    "RESERVED": {"INITIATED", "RINGING", "ANSWERED", "FAILED", "CANCELLED"},
    "INITIATED": {"RINGING", "ANSWERED", "FAILED", "CANCELLED"},
    "RINGING": {"ANSWERED", "FAILED", "CANCELLED"},
    "ANSWERED": {"CONNECTED", "COMPLETED", "FAILED"},
    "CONNECTED": {"COMPLETED", "FAILED"},
    "COMPLETED": set(), "FAILED": set(), "CANCELLED": set(),
}


def is_legal_call_transition(current: str, target: str) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, set())


def transition_call(call: DialCall, new_status: str) -> None:
    if call.status == new_status:
        return
    if not is_legal_call_transition(call.status, new_status):
        raise ValueError(f"Illegal call transition: {call.status} -> {new_status}")
    call.status = new_status


ALLOWED_AGENT_TRANSITIONS = {
    "OFFLINE": {"AVAILABLE"},
    "AVAILABLE": {"RESERVED", "OFFLINE", "PAUSED"},
    "RESERVED": {"DIALING", "CONNECTED", "AVAILABLE", "OFFLINE", "PAUSED"},
    "DIALING": {"CONNECTED", "AVAILABLE", "OFFLINE", "PAUSED"},
    "CONNECTED": {"AVAILABLE", "WRAP_UP", "OFFLINE"},
    "WRAP_UP": {"AVAILABLE", "PAUSED", "OFFLINE"},
    "PAUSED": {"AVAILABLE", "OFFLINE"},
}


def transition_agent(agent: Agent, new_status: str) -> None:
    if agent.status == new_status:
        return
    if not is_legal_agent_transition(agent.status, new_status):
        raise ValueError(f"Illegal agent transition: {agent.status} -> {new_status}")
    agent.status = new_status


def is_legal_agent_transition(current: str, target: str) -> bool:
    return target in ALLOWED_AGENT_TRANSITIONS.get(current, set())


def handle_provider_event(
    db: Session,
    event_id: str,
    call_id: int,
    status: str,
    occurred_at: datetime | None = None,
) -> dict:
    if not event_id or len(event_id) > 200:
        raise ValueError("event_id must contain 1 to 200 characters")
    if status not in ALLOWED_TRANSITIONS:
        raise ValueError(f"Unknown call status: {status}")
    _start_reservation_transaction(db)
    existing = db.scalar(select(ProviderEventRecord).where(
        ProviderEventRecord.event_id == event_id))
    if existing:
        db.rollback()
        return {"duplicate": True, "call_id": call_id, "status": "ignored"}
    call = db.scalar(select(DialCall).where(DialCall.id == call_id).with_for_update())
    if call is None:
        db.rollback()
        raise LookupError("Call not found")
    current = call.status
    latest_time = db.scalar(select(func.max(ProviderEventRecord.occurred_at)).where(
        ProviderEventRecord.call_id == call_id,
        ProviderEventRecord.occurred_at.is_not(None),
        ProviderEventRecord.applied.is_(True),
    ))
    if occurred_at is not None and latest_time is not None:
        incoming = occurred_at.replace(tzinfo=timezone.utc) if occurred_at.tzinfo is None else occurred_at
        latest = latest_time.replace(tzinfo=timezone.utc) if latest_time.tzinfo is None else latest_time
        if incoming < latest:
            db.add(ProviderEventRecord(
                event_id=event_id, call_id=call_id, status=status, occurred_at=occurred_at,
                applied=False,
            ))
            db.commit()
            return {"duplicate": False, "call_id": call_id, "status": "ignored_out_of_order",
                    "current_status": current}
    if not is_legal_call_transition(current, status):
        db.add(ProviderEventRecord(
            event_id=event_id, call_id=call_id, status=status, occurred_at=occurred_at,
            applied=False,
        ))
        db.commit()
        return {"duplicate": False, "call_id": call_id, "status": "ignored_out_of_order",
                "current_status": current}

    transition_call(call, status)
    call.updated_at = datetime.now(timezone.utc)
    agent = db.get(Agent, call.agent_id) if call.agent_id else None
    borrower = db.get(Borrower, call.borrower_id)
    if status in {"COMPLETED", "FAILED", "CANCELLED"}:
        if agent is not None and agent.status in {"RESERVED", "DIALING", "CONNECTED", "WRAP_UP"}:
            transition_agent(agent, "AVAILABLE")
        if borrower is not None:
            borrower.status = "QUEUED" if status in {"FAILED", "CANCELLED"} else "DONE"
    elif status in {"ANSWERED", "CONNECTED"} and agent is not None:
        transition_agent(agent, "CONNECTED")
    elif status == "RINGING" and agent is not None:
        transition_agent(agent, "DIALING")
    db.add(ProviderEventRecord(
        event_id=event_id, call_id=call_id, status=status, occurred_at=occurred_at,
        applied=True,
    ))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"duplicate": True, "call_id": call_id, "status": "ignored"}
    return {"duplicate": False, "call_id": call_id, "status": status}
