from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import random
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import func, select

from app.models import Agent, Borrower, Campaign, DialCall, OutboxMessage, ProviderEventRecord
from app.providers import FastMockProvider, ProviderResult, UnreliableMockProvider
from app.schemas import DialResponse, PacingDecision
from app.services import (
    ALLOWED_AGENT_TRANSITIONS,
    ALLOWED_TRANSITIONS,
    calculate_predictive_proposal,
    dispatch_outbox,
    get_pacing_decision,
    handle_provider_event,
    is_legal_call_transition,
    reserve_and_dial,
    transition_agent,
)


def seed_campaign(db, *, agents=3, borrowers=6, mode="PREDICTIVE", answer_rate=0.5):
    campaign = Campaign(
        name="test", mode=mode, answer_rate=answer_rate, avg_talk_seconds=30,
        provider_healthy=True,
    )
    db.add(campaign)
    db.flush()
    db.add_all([Agent(name=f"agent-{i}") for i in range(agents)])
    db.add_all([Borrower(name=f"borrower-{i}", phone=f"+100000{i}") for i in range(borrowers)])
    db.commit()
    return campaign


def test_predictive_formula_accounts_for_capacity_connected_and_setup():
    assert calculate_predictive_proposal(20, 3, 4, 2, 10, 0.5) == 4
    assert calculate_predictive_proposal(20, 0, 2, 1, 3, 0.5) == 0


def test_call_transition_matrix_is_enforced_for_every_state_pair():
    for current in ALLOWED_TRANSITIONS:
        for target in ALLOWED_TRANSITIONS:
            assert is_legal_call_transition(current, target) == (
                target in ALLOWED_TRANSITIONS[current]
            )
    assert not is_legal_call_transition("QUEUED", "CONNECTED")
    assert not is_legal_call_transition("COMPLETED", "FAILED")


def test_agent_state_transition_matrix_rejects_invalid_moves():
    agent = Agent(name="agent", status="AVAILABLE")
    transition_agent(agent, "RESERVED")
    transition_agent(agent, "DIALING")
    transition_agent(agent, "CONNECTED")
    transition_agent(agent, "WRAP_UP")
    transition_agent(agent, "AVAILABLE")
    assert agent.status == "AVAILABLE"

    with pytest.raises(ValueError, match="Illegal agent transition"):
        transition_agent(agent, "CONNECTED")
    for current, valid_targets in ALLOWED_AGENT_TRANSITIONS.items():
        for target in ALLOWED_AGENT_TRANSITIONS:
            candidate = Agent(name="matrix-agent", status=current)
            if target in valid_targets or target == current:
                transition_agent(candidate, target)
                assert candidate.status == target
            else:
                with pytest.raises(ValueError):
                    transition_agent(candidate, target)


def test_mock_provider_reuses_accepted_idempotency_key():
    provider = FastMockProvider()
    first = provider.initiate(7, "call-7")
    replay = provider.initiate(99, "call-7")
    assert first.accepted and replay.accepted
    assert replay.provider_call_id == first.provider_call_id == "fast-7"


def test_fast_provider_latency_and_controllable_failure():
    random_values = iter([0.0, 0.9])
    observed_delays = []
    provider = FastMockProvider(
        latency_seconds=0.025,
        failure_rate=0.5,
        random_value=lambda: next(random_values),
        sleep=observed_delays.append,
    )
    failed = provider.initiate(7, "call-7")
    accepted = provider.initiate(7, "call-7")
    replay = provider.initiate(99, "call-7")
    assert failed.accepted is False
    assert accepted.accepted and replay.accepted
    assert replay.provider_call_id == accepted.provider_call_id
    assert observed_delays == [0.025, 0.025, 0.025]


def test_unreliable_provider_has_controllable_seeded_failure_and_recovers():
    random_source = random.Random(1)
    observed_delays = []
    provider = UnreliableMockProvider(
        random_value=random_source.random,
        latency_seconds=0.2,
        sleep=observed_delays.append,
    )
    first = provider.initiate(7, "call-7")
    retry = provider.initiate(7, "call-7")
    assert first.accepted is False
    assert first.error == "simulated provider timeout"
    assert retry.accepted is True
    assert provider.initiate(99, "call-7").provider_call_id == retry.provider_call_id
    assert observed_delays == [0.2, 0.2, 0.2]


def test_progressive_and_predictive_dialing_are_safety_capped(session_factory):
    with session_factory() as db:
        progressive = seed_campaign(db, agents=2, mode="PROGRESSIVE")
        decision = get_pacing_decision(db, progressive, 20)
        assert decision["proposed_calls"] == 2
        calls = reserve_and_dial(db, progressive, 20)
        assert len(calls.calls) == 2
        assert calls.proposed_calls == 2
        assert calls.safety_approved_calls == 2
        assert all(call.status == "INITIATED" for call in calls.calls)
        pacing_response = PacingDecision.model_validate(decision)
        assert pacing_response.proposed_calls == 2
        with pytest.warns(DeprecationWarning, match="Use proposed_calls"):
            assert pacing_response.approved_calls == 2
        assert PacingDecision.model_json_schema()["properties"]["approved_calls"]["deprecated"]
        dial_response = DialResponse(
            proposed_calls=calls.proposed_calls,
            safety_approved_calls=calls.safety_approved_calls,
            created=len(calls.calls),
            calls=[
                {
                    "id": call.id,
                    "agent_id": call.agent_id,
                    "borrower_id": call.borrower_id,
                    "status": call.status,
                    "provider": call.provider,
                }
                for call in calls.calls
            ],
        )
        assert dial_response.safety_approved_calls == dial_response.created == 2

    with session_factory() as db:
        predictive = seed_campaign(db, agents=4, mode="PREDICTIVE")
        decision = get_pacing_decision(db, predictive, 20)
        assert decision["proposed_calls"] > decision["available_agents"]
        calls = reserve_and_dial(db, predictive, 20)
        assert len(calls.calls) == 4
        assert calls.proposed_calls > calls.safety_approved_calls
        assert calls.safety_approved_calls == 4
        available = db.scalar(select(func.count(Agent.id)).where(Agent.status == "AVAILABLE"))
        assert available == 0
        assert db.scalar(select(func.count(DialCall.id)).where(
            DialCall.campaign_id == predictive.id,
            DialCall.status.in_(("RESERVED", "INITIATED", "RINGING", "ANSWERED", "CONNECTED"))
        )) == 4


def test_safety_gate_counts_existing_connected_and_setup_calls(session_factory):
    with session_factory() as db:
        campaign = seed_campaign(db, agents=1, borrowers=2, mode="PROGRESSIVE")
        agent = db.scalar(select(Agent))
        borrower = db.scalar(select(Borrower))
        db.add(DialCall(
            campaign_id=campaign.id,
            borrower_id=borrower.id,
            agent_id=agent.id,
            status="CONNECTED",
            provider="mock-fast",
        ))
        db.commit()

        assert get_pacing_decision(db, campaign, 5)["proposed_calls"] == 1
        assert reserve_and_dial(db, campaign, 5).calls == []
        assert db.scalar(select(func.count(DialCall.id))) == 1


def test_provider_failure_retries_then_releases_both_reservations(
    session_factory, monkeypatch
):
    class AlwaysFails:
        def initiate(self, call_id, idempotency_key):
            raise TimeoutError("provider timeout")

    from app import services
    monkeypatch.setitem(services.PROVIDERS, "mock-fast", AlwaysFails())
    with session_factory() as db:
        campaign = seed_campaign(db, agents=1, borrowers=1)
        call = reserve_and_dial(db, campaign, 1).calls[0]
        message = db.scalar(select(OutboxMessage).where(OutboxMessage.call_id == call.id))
        assert message.status == "RETRY"
        assert message.attempts == 1
        assert message.last_error == "TimeoutError: provider timeout"
        assert db.scalar(select(Agent.status)) == "RESERVED"

        for _ in range(2):
            message.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
            dispatch_outbox(db, limit=1)
            db.refresh(message)
        db.refresh(call)
        assert message.status == "FAILED"
        assert call.status == "FAILED"
        assert db.scalar(select(Agent.status)) == "AVAILABLE"
        assert db.scalar(select(Borrower.status)) == "QUEUED"


def test_temporary_provider_outage_recovers_within_retry_policy(session_factory, monkeypatch):
    class RecoveringProvider:
        def __init__(self):
            self.results = iter([
                ProviderResult(False, error="temporary outage"),
                ProviderResult(True, provider_call_id="recovered-call"),
            ])

        def initiate(self, call_id, idempotency_key):
            return next(self.results)

    from app import services
    monkeypatch.setitem(services.PROVIDERS, "mock-fast", RecoveringProvider())
    with session_factory() as db:
        campaign = seed_campaign(db, agents=1, borrowers=1)
        call = reserve_and_dial(db, campaign, 1).calls[0]
        message = db.scalar(select(OutboxMessage).where(OutboxMessage.call_id == call.id))
        assert message.status == "RETRY"
        message.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

        outcome = dispatch_outbox(db, limit=1)
        db.refresh(message)
        db.refresh(call)
        assert outcome["sent"] == 1
        assert message.attempts == 2
        assert message.status == "SENT"
        assert call.status == "INITIATED"


def test_expired_processing_lease_is_reclaimed_and_completed(session_factory, monkeypatch):
    from app import services
    monkeypatch.setattr(services, "dispatch_outbox", lambda db, limit=100: {})
    with session_factory() as db:
        campaign = seed_campaign(db, agents=1, borrowers=1)
        call = reserve_and_dial(db, campaign, 1).calls[0]
        outbox = db.scalar(select(OutboxMessage).where(OutboxMessage.call_id == call.id))
        claim_time = datetime.now(timezone.utc) + timedelta(seconds=1)
        claimed = services._claim_outbox(db, 1, claim_time)
        assert claimed == [(outbox.id, f"call-{call.id}", call.id)]
        db.refresh(outbox)
        assert outbox.status == "PROCESSING"
        assert outbox.attempts == 1

        result = dispatch_outbox(db, 1, now=claim_time + timedelta(seconds=31))
        db.refresh(outbox)
        db.refresh(call)
        assert result["sent"] == 1
        assert outbox.status == "SENT"
        assert outbox.attempts == 2
        assert call.status == "INITIATED"


def test_provider_acceptance_before_crash_can_duplicate_external_delivery(
    session_factory, monkeypatch
):
    class NonIdempotentProvider:
        def __init__(self):
            self.accepted_keys = []

        def initiate(self, call_id, idempotency_key):
            self.accepted_keys.append(idempotency_key)
            return ProviderResult(True, provider_call_id=f"provider-call-{len(self.accepted_keys)}")

    from app import services
    provider = NonIdempotentProvider()
    monkeypatch.setitem(services.PROVIDERS, "mock-fast", provider)
    monkeypatch.setattr(services, "dispatch_outbox", lambda db, limit=100: {})
    with session_factory() as db:
        campaign = seed_campaign(db, agents=1, borrowers=1)
        call = reserve_and_dial(db, campaign, 1).calls[0]
        outbox = db.scalar(select(OutboxMessage).where(OutboxMessage.call_id == call.id))
        claim_time = datetime.now(timezone.utc) + timedelta(seconds=1)
        services._claim_outbox(db, 1, claim_time)

        first_delivery = provider.initiate(call.id, outbox.idempotency_key)
        assert first_delivery.accepted
        result = dispatch_outbox(db, 1, now=claim_time + timedelta(seconds=31))

        assert result["sent"] == 1
        assert provider.accepted_keys == [outbox.idempotency_key, outbox.idempotency_key]
        assert outbox.idempotency_key == f"call-{call.id}"


def test_subprocess_crash_leaves_claim_for_restarted_worker(
    session_factory, monkeypatch, tmp_path
):
    from app import services
    monkeypatch.setattr(services, "dispatch_outbox", lambda db, limit=100: {})
    with session_factory() as db:
        campaign = seed_campaign(db, agents=1, borrowers=1)
        call = reserve_and_dial(db, campaign, 1).calls[0]
        outbox = db.scalar(select(OutboxMessage).where(OutboxMessage.call_id == call.id))
        message_id = outbox.id
        database_path = db.get_bind().url.database
        db.commit()

    claim_time = datetime.now(timezone.utc) + timedelta(seconds=1)
    script = (
        "import os, sys; "
        "from datetime import datetime; "
        "from app.database import SessionLocal; "
        "from app.services import _claim_outbox; "
        "db = SessionLocal(); "
        "_claim_outbox(db, 1, datetime.fromisoformat(sys.argv[1])); "
        "db.close(); os._exit(0)"
    )
    environment = os.environ.copy()
    environment["DATABASE_URL"] = f"sqlite:///{Path(database_path).as_posix()}"
    project_root = Path(__file__).resolve().parents[1]
    crashed_worker = subprocess.run(
        [sys.executable, "-c", script, claim_time.isoformat()],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert crashed_worker.returncode == 0, crashed_worker.stderr

    with session_factory() as restarted_worker_db:
        outbox = restarted_worker_db.get(OutboxMessage, message_id)
        assert outbox.status == "PROCESSING"
        assert outbox.attempts == 1
        recovered = dispatch_outbox(
            restarted_worker_db, 1, now=claim_time + timedelta(seconds=31)
        )
        restarted_worker_db.refresh(outbox)
        assert recovered["sent"] == 1
        assert outbox.status == "SENT"
        assert outbox.attempts == 2


def test_terminal_provider_event_releases_capacity(session_factory):
    with session_factory() as db:
        campaign = seed_campaign(db, agents=1, borrowers=1)
        call = reserve_and_dial(db, campaign, 1).calls[0]
        event_time = datetime.now(timezone.utc)
        handle_provider_event(db, "event-answer", call.id, "ANSWERED", event_time)
        completed = handle_provider_event(
            db, "event-complete", call.id, "COMPLETED", event_time + timedelta(seconds=10)
        )
        assert completed["status"] == "COMPLETED"
        assert db.get(DialCall, call.id).status == "COMPLETED"
        assert db.scalar(select(Agent.status)) == "AVAILABLE"
        assert db.scalar(select(Borrower.status)) == "DONE"


def test_answered_persists_across_worker_restart_before_completed(session_factory):
    with session_factory() as db:
        campaign = seed_campaign(db, agents=1, borrowers=1)
        call = reserve_and_dial(db, campaign, 1).calls[0]
        call_id = call.id
        answered = handle_provider_event(db, "answered-before-crash", call_id, "ANSWERED")
        assert answered["status"] == "ANSWERED"

    with session_factory() as restarted_worker_db:
        call = restarted_worker_db.get(DialCall, call_id)
        assert call.status == "ANSWERED"
        completed = handle_provider_event(
            restarted_worker_db, "completed-after-restart", call_id, "COMPLETED"
        )
        assert completed["status"] == "COMPLETED"
        assert call.status == "COMPLETED"


def test_timestamp_free_events_use_only_legal_transition_order(session_factory):
    with session_factory() as db:
        campaign = seed_campaign(db, agents=1, borrowers=1)
        call = reserve_and_dial(db, campaign, 1).calls[0]
        ringing = handle_provider_event(db, "no-time-ring", call.id, "RINGING")
        answered = handle_provider_event(db, "no-time-answer", call.id, "ANSWERED")
        repeated_ringing = handle_provider_event(db, "no-time-ring-late", call.id, "RINGING")

        assert ringing["status"] == "RINGING"
        assert answered["status"] == "ANSWERED"
        assert repeated_ringing["status"] == "ignored_out_of_order"
        assert db.get(DialCall, call.id).status == "ANSWERED"


def test_provider_events_reject_out_of_order_and_ignore_duplicates(session_factory):
    with session_factory() as db:
        campaign = seed_campaign(db, agents=1, borrowers=1)
        call = reserve_and_dial(db, campaign, 1).calls[0]
        start = datetime.now(timezone.utc)
        ringing = handle_provider_event(db, "event-ring", call.id, "RINGING", start)
        duplicate = handle_provider_event(db, "event-ring", call.id, "RINGING", start)
        old_answer = handle_provider_event(
            db, "event-old-answer", call.id, "ANSWERED", start - timedelta(seconds=1)
        )
        assert ringing["status"] == "RINGING"
        assert duplicate["duplicate"] is True
        assert old_answer["status"] == "ignored_out_of_order"
        assert db.get(DialCall, call.id).status == "RINGING"

        applied = handle_provider_event(
            db, "event-answer", call.id, "ANSWERED", start + timedelta(seconds=1)
        )
        assert applied["status"] == "ANSWERED"
        assert db.get(DialCall, call.id).status == "ANSWERED"
        assert db.scalar(select(func.count(ProviderEventRecord.id))) == 3


def test_completed_answered_ringing_callback_sequence_cannot_regress_state(session_factory):
    with session_factory() as db:
        campaign = seed_campaign(db, agents=1, borrowers=1)
        call = reserve_and_dial(db, campaign, 1).calls[0]
        ringing = handle_provider_event(db, "sequence-ringing", call.id, "RINGING")
        answered = handle_provider_event(db, "sequence-initial-answer", call.id, "ANSWERED")
        completed = handle_provider_event(db, "sequence-completed", call.id, "COMPLETED")
        late_answer = handle_provider_event(db, "sequence-answer", call.id, "ANSWERED")
        late_ringing = handle_provider_event(db, "sequence-late-ring", call.id, "RINGING")
        assert ringing["status"] == "RINGING"
        assert answered["status"] == "ANSWERED"
        assert completed["status"] == "COMPLETED"
        assert late_answer["status"] == "ignored_out_of_order"
        assert late_ringing["status"] == "ignored_out_of_order"
        assert db.get(DialCall, call.id).status == "COMPLETED"


def test_concurrent_requests_never_double_reserve_agents_or_borrowers(session_factory):
    with session_factory() as db:
        campaign = seed_campaign(db, agents=5, borrowers=5)
        campaign_id = campaign.id

    def dial_batch(_):
        with session_factory() as db:
            campaign = db.get(Campaign, campaign_id)
            return len(reserve_and_dial(db, campaign, 5).calls)

    with ThreadPoolExecutor(max_workers=8) as pool:
        created = sum(pool.map(dial_batch, range(8)))

    with session_factory() as db:
        calls = list(db.scalars(select(DialCall)))
        assert created == 5
        assert len({call.agent_id for call in calls}) == 5
        assert len({call.borrower_id for call in calls}) == 5
        assert db.scalar(select(func.count(Agent.id)).where(Agent.status == "AVAILABLE")) == 0


def test_provider_callback_racing_dispatch_does_not_regress_call_state(
    session_factory, monkeypatch
):
    from app import services

    class CallbackBeforeAcknowledgementProvider:
        def __init__(self, db):
            self.db = db

        def initiate(self, call_id, idempotency_key):
            handle_provider_event(self.db, "callback-during-initiate", call_id, "RINGING")
            return ProviderResult(True, provider_call_id="callback-race")

    with session_factory() as db:
        campaign = seed_campaign(db, agents=1, borrowers=1)
        monkeypatch.setitem(
            services.PROVIDERS,
            "mock-fast",
            CallbackBeforeAcknowledgementProvider(db),
        )
        batch = reserve_and_dial(db, campaign, 1)
        call = batch.calls[0]
        agent = db.get(Agent, call.agent_id)
        outbox = db.scalar(select(OutboxMessage).where(OutboxMessage.call_id == call.id))
        assert call.status == "RINGING"
        assert agent.status == "DIALING"
        assert outbox.status == "SENT"
