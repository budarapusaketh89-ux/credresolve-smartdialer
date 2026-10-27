from app.simulation import run_assignment_scenarios, run_simulation


def test_agent_drop_reduces_capacity_at_configured_time():
    result = run_simulation(
        agent_count=20,
        duration_seconds=60,
        answer_rate=0.3,
        avg_talk_seconds=20,
        mode="PREDICTIVE",
        max_calls=100,
        agent_drop_fraction=0.4,
        agent_drop_at_second=30,
        seed=7,
    )
    assert result["agent_drops"] == 8
    assert result["agents_remaining_at_drop"] == 12
    assert result["available_agents"] == 12
    assert result["capacity_invariant_holds"]


def test_simulation_is_seeded_and_drains_all_call_lifecycles():
    inputs = dict(
        agent_count=6, duration_seconds=12, answer_rate=0.55,
        avg_talk_seconds=4, mode="PROGRESSIVE", max_calls=20,
        provider_failure_rate=0.2, seed=11,
    )
    first = run_simulation(**inputs)
    second = run_simulation(**inputs)
    assert first == second
    assert first["termination"] == "completed"
    assert first["capacity_invariant_holds"]
    assert sum(first["terminal_call_states"].values()) == first["call_attempts"]


def test_simulation_provider_failure_and_progressive_capacity():
    result = run_simulation(
        agent_count=3, duration_seconds=1, answer_rate=1,
        avg_talk_seconds=2, mode="PROGRESSIVE", max_calls=10,
        provider_failure_rate=1, seed=3,
    )
    assert result["call_attempts"] == 3
    assert result["provider_failures"] == 3
    assert result["completed_calls"] == 0
    assert result["capacity_invariant_holds"]


def test_assignment_scenarios_are_reproducible_and_d_report_conditions_change():
    first = run_assignment_scenarios(seed=9)
    second = run_assignment_scenarios(seed=9)
    assert first == second
    assert first["result_kind"] == "modeled_simulation_not_telecom_forecast"
    assert set(first["scenarios"]) == {"A", "B", "C", "D"}
    assert first["scenarios"]["A"]["answered_calls"] > 0
    assert first["scenarios"]["B"]["answered_calls"] > 0
    assert first["scenarios"]["C"]["answered_calls"] > 0
    scenario_d = first["scenarios"]["D"]
    assert {decision["answer_rate"] for decision in scenario_d["pacing_and_safety_decisions"]} >= {
        0.2, 0.5, 0.1
    }
    assert {decision["avg_talk_seconds"] for decision in scenario_d["pacing_and_safety_decisions"]} >= {
        120, 90, 180
    }


def test_provider_latency_is_separate_from_setup_delay():
    fast = run_simulation(
        agent_count=1, duration_seconds=1, answer_rate=1, avg_talk_seconds=1,
        mode="PROGRESSIVE", max_calls=1, setup_seconds=2,
        provider_latency_seconds=0, seed=4,
    )
    delayed = run_simulation(
        agent_count=1, duration_seconds=1, answer_rate=1, avg_talk_seconds=1,
        mode="PROGRESSIVE", max_calls=1, setup_seconds=2,
        provider_latency_seconds=5, seed=4,
    )
    assert fast["provider_accepted_calls"] == 1
    fast_accept_time = next(
        point["second"] for point in fast["agent_utilization_over_time"]
        if point["provider_accepted_calls"] == 1
    )
    delayed_accept_time = next(
        point["second"] for point in delayed["agent_utilization_over_time"]
        if point["provider_accepted_calls"] == 1
    )
    assert delayed_accept_time - fast_accept_time == 4


def test_agent_drop_immediately_reduces_predictive_approval_capacity():
    result = run_simulation(
        agent_count=20, duration_seconds=35, answer_rate=0.3, avg_talk_seconds=50,
        mode="PREDICTIVE", max_calls=200, agent_drop_fraction=0.4,
        agent_drop_at_second=30, seed=17,
    )
    drop_decision = next(
        decision for decision in result["pacing_and_safety_decisions"]
        if decision["second"] == 30
    )
    assert drop_decision["staffed_agents"] == 12
    assert drop_decision["safety_approved_calls"] <= drop_decision["available_agents"]
    assert drop_decision["available_agents"] <= 12
    assert result["capacity_invariant_holds"]
