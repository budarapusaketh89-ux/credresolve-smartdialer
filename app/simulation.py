import random
from dataclasses import dataclass
from math import ceil

from .services import ALLOWED_TRANSITIONS, calculate_predictive_proposal

ACTIVE_STATES = {"RESERVED", "INITIATED", "RINGING", "ANSWERED", "CONNECTED"}


@dataclass
class _SimCall:
    agent_id: int
    provider_due: int
    provider_ok: bool
    state: str = "RESERVED"
    setup_due: int | None = None
    finish_at: int | None = None


def _transition(call: _SimCall, status: str) -> None:
    if status not in ALLOWED_TRANSITIONS.get(call.state, set()):
        raise RuntimeError(f"Illegal simulated call transition: {call.state} -> {status}")
    call.state = status


def run_simulation(
    *,
    agent_count: int,
    duration_seconds: int,
    answer_rate: float,
    avg_talk_seconds: int,
    mode: str,
    max_calls: int,
    setup_seconds: int = 2,
    provider_latency_seconds: float = 0.0,
    provider_failure_rate: float = 0.0,
    agent_drop_fraction: float = 0.0,
    agent_drop_at_second: int | None = None,
    rate_changes: list[dict] | None = None,
    seed: int = 1,
) -> dict:
    rng = random.Random(seed)
    current_answer_rate = max(0.05, min(0.95, answer_rate))
    current_talk_seconds = max(1, avg_talk_seconds)
    changes = sorted(rate_changes or [], key=lambda change: change["at_second"])
    change_index = 0
    agents = set(range(agent_count))
    calls: list[_SimCall] = []
    calls_proposed = calls_reserved = provider_accepted = 0
    answered = completed = provider_failures = safety_rejections = no_answer_calls = 0
    agent_drops = failed_on_drop = 0
    dropped = False
    agents_remaining_at_drop: int | None = None
    peak_active_calls = 0
    current_second = 0
    last_dial_second = duration_seconds
    failure_rate = max(0.0, min(1.0, provider_failure_rate))
    provider_delay_ticks = max(0, ceil(provider_latency_seconds))
    setup_delay_ticks = max(1, setup_seconds)
    decisions: list[dict] = []
    utilization: list[dict] = []

    while current_second < last_dial_second or any(
        call.state in ACTIVE_STATES for call in calls
    ):
        current_second += 1
        while (change_index < len(changes)
               and changes[change_index]["at_second"] <= current_second):
            change = changes[change_index]
            current_answer_rate = max(0.05, min(0.95, change["answer_rate"]))
            current_talk_seconds = max(1, change["avg_talk_seconds"])
            change_index += 1

        if (not dropped and agent_drop_at_second is not None
                and current_second >= agent_drop_at_second):
            drop_count = min(len(agents), int(agent_count * agent_drop_fraction))
            dropped_agents = set(rng.sample(sorted(agents), drop_count))
            agents.difference_update(dropped_agents)
            agent_drops += drop_count
            agents_remaining_at_drop = len(agents)
            dropped = True
            for call in calls:
                if call.agent_id in dropped_agents and call.state in ACTIVE_STATES:
                    _transition(call, "FAILED")
                    failed_on_drop += 1

        for call in calls:
            if call.state == "RESERVED" and call.provider_due <= current_second:
                if not call.provider_ok:
                    _transition(call, "FAILED")
                    provider_failures += 1
                else:
                    _transition(call, "INITIATED")
                    _transition(call, "RINGING")
                    call.setup_due = current_second + setup_delay_ticks
                    provider_accepted += 1
            elif (call.state == "RINGING" and call.setup_due is not None
                  and call.setup_due <= current_second):
                if rng.random() < current_answer_rate:
                    talk_time = min(
                        max(1, round(rng.expovariate(1 / current_talk_seconds))),
                        current_talk_seconds * 10,
                    )
                    _transition(call, "ANSWERED")
                    _transition(call, "CONNECTED")
                    call.finish_at = current_second + talk_time
                    answered += 1
                else:
                    _transition(call, "FAILED")
                    no_answer_calls += 1
            elif (call.state == "CONNECTED" and call.finish_at is not None
                  and call.finish_at <= current_second):
                _transition(call, "COMPLETED")
                completed += 1

        active_calls = [call for call in calls if call.state in ACTIVE_STATES]
        if len(active_calls) > len(agents):
            raise RuntimeError("Simulation exceeded current staffed-agent capacity")
        reserved_agents = {call.agent_id for call in active_calls}
        free_agents = sorted(agents - reserved_agents)
        peak_active_calls = max(peak_active_calls, len(active_calls))
        utilization.append({
            "second": current_second,
            "active_calls": len(active_calls),
            "staffed_agents": len(agents),
            "utilization": round(len(active_calls) / len(agents), 4) if agents else 0.0,
            "provider_accepted_calls": provider_accepted,
            "answered_calls": answered,
            "completed_calls": completed,
        })

        if current_second <= last_dial_second and calls_reserved < max_calls:
            remaining_request = max_calls - calls_reserved
            setup_count = sum(call.state in {"RESERVED", "INITIATED", "RINGING"}
                              for call in active_calls)
            connected_count = sum(call.state in {"ANSWERED", "CONNECTED"}
                                  for call in active_calls)
            if mode == "PROGRESSIVE":
                proposal = min(remaining_request, len(free_agents))
            elif mode == "PREDICTIVE":
                proposal = calculate_predictive_proposal(
                    remaining_request,
                    len(free_agents),
                    connected_count,
                    setup_count,
                    len(agents),
                    current_answer_rate,
                )
            else:
                raise ValueError(f"Unsupported simulation mode: {mode}")

            approved = min(proposal, len(free_agents), remaining_request)
            calls_proposed += proposal
            calls_reserved += approved
            safety_rejections += proposal - approved
            decisions.append({
                "second": current_second,
                "answer_rate": current_answer_rate,
                "avg_talk_seconds": current_talk_seconds,
                "staffed_agents": len(agents),
                "available_agents": len(free_agents),
                "connected_calls": connected_count,
                "setup_calls": setup_count,
                "proposed_calls": proposal,
                "safety_approved_calls": approved,
                "safety_rejected_calls": proposal - approved,
            })
            for agent_id in free_agents[:approved]:
                calls.append(_SimCall(
                    agent_id=agent_id,
                    provider_due=current_second + max(1, provider_delay_ticks),
                    provider_ok=rng.random() >= failure_rate,
                ))

        if current_second > last_dial_second and not any(
            call.state in ACTIVE_STATES for call in calls
        ):
            break
        drain_limit = last_dial_second + max(
            30,
            current_talk_seconds * 10 + setup_delay_ticks + provider_delay_ticks + 1,
        )
        if current_second > drain_limit:
            raise RuntimeError("Simulation exceeded its bounded drain window")

    terminal_states = {
        state: sum(call.state == state for call in calls)
        for state in ("COMPLETED", "FAILED", "CANCELLED")
    }
    return {
        "result_kind": "modeled_simulation",
        "mode": mode,
        "seed": seed,
        "duration_seconds": duration_seconds,
        "elapsed_seconds": current_second,
        "calls_proposed": calls_proposed,
        "calls_reserved": calls_reserved,
        "call_attempts": calls_reserved,
        "provider_accepted_calls": provider_accepted,
        "answered_calls": answered,
        "completed_calls": completed,
        "no_answer_calls": no_answer_calls,
        "provider_failures": provider_failures,
        "failed_on_agent_drop": failed_on_drop,
        "safety_rejections": safety_rejections,
        "agent_drops": agent_drops,
        "agents_remaining_at_drop": agents_remaining_at_drop,
        "available_agents_at_end": len(agents),
        "available_agents": len(agents),
        "peak_active_calls": peak_active_calls,
        "capacity_invariant_holds": peak_active_calls <= agent_count,
        "termination": "completed",
        "terminal_call_states": terminal_states,
        "agent_utilization_over_time": utilization,
        "pacing_and_safety_decisions": decisions,
    }


def run_assignment_scenarios(*, seed: int = 1, mode: str = "PREDICTIVE") -> dict:
    common = {
        "agent_count": 20,
        "duration_seconds": 300,
        "mode": mode,
        "max_calls": 500,
        "provider_latency_seconds": 1,
        "provider_failure_rate": 0.05,
        "seed": seed,
    }
    scenarios = {
        "A": run_simulation(
            **common, answer_rate=0.2, avg_talk_seconds=120
        ),
        "B": run_simulation(
            **common, answer_rate=0.5, avg_talk_seconds=90
        ),
        "C": run_simulation(
            **common, answer_rate=0.7, avg_talk_seconds=180
        ),
        "D": run_simulation(
            **common,
            answer_rate=0.2,
            avg_talk_seconds=120,
            rate_changes=[
                {"at_second": 100, "answer_rate": 0.5, "avg_talk_seconds": 90},
                {"at_second": 200, "answer_rate": 0.1, "avg_talk_seconds": 180},
            ],
        ),
    }
    return {
        "result_kind": "modeled_simulation_not_telecom_forecast",
        "seed": seed,
        "mode": mode,
        "scenarios": scenarios,
    }
