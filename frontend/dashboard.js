const ACTIVE_CALL_STATES = new Set(["RESERVED", "INITIATED", "RINGING", "ANSWERED", "CONNECTED"]);
const ANSWERED_CALL_STATES = new Set(["ANSWERED", "CONNECTED"]);
const numberFormat = new Intl.NumberFormat();

const byId = (id) => document.getElementById(id);

function setMetric(id, value, note) {
  const element = byId(id);
  element.textContent = value === null ? "Unavailable" : numberFormat.format(value);
  element.classList.toggle("is-unavailable", value === null);
  if (note) {
    const noteElement = id === "metric-rejections" ? byId("rejections-note") : element.parentElement.querySelector(".metric-note");
    noteElement.textContent = note;
  }
}

function setConnection(state, label) {
  const status = byId("connection-status");
  status.dataset.state = state;
  byId("connection-label").textContent = label;
}

async function getJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`The API returned an unreadable response (${response.status}).`);
  }
  if (!response.ok) {
    const detail = payload && typeof payload.detail === "string" ? payload.detail : `Request failed (${response.status}).`;
    throw new Error(detail);
  }
  return payload;
}

function setTableError(bodyId, columns, message) {
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.className = "table-empty";
  cell.colSpan = columns;
  cell.textContent = message;
  row.append(cell);
  byId(bodyId).replaceChildren(row);
}

async function loadHealth() {
  try {
    await getJson("/health");
    setConnection("connected", "API connected");
  } catch {
    setConnection("error", "API unavailable");
  }
}

async function loadAgents() {
  try {
    const agents = await getJson("/agents");
    if (!Array.isArray(agents)) throw new Error("The agent response had an unexpected format.");
    setMetric("metric-agents", agents.filter((agent) => agent.status === "AVAILABLE").length);
  } catch (error) {
    setMetric("metric-agents", null, `Could not load agents: ${error.message}`);
  }
}

function renderCalls(calls) {
  const active = calls.filter((call) => ACTIVE_CALL_STATES.has(call.status)).length;
  const answered = calls.filter((call) => ANSWERED_CALL_STATES.has(call.status)).length;
  setMetric("metric-active", active);
  setMetric("metric-answered", answered);
  byId("calls-count").textContent = `${numberFormat.format(calls.length)} records`;

  if (!calls.length) {
    setTableError("calls-body", 5, "No call records found.");
    return;
  }

  const body = byId("calls-body");
  body.replaceChildren();
  calls.slice(-8).reverse().forEach((call) => {
    const row = document.createElement("tr");
    const values = [`#${call.id}`, `#${call.campaign_id}`, call.agent_id === null ? "—" : `#${call.agent_id}`];
    values.forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    });
    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = "status-pill";
    badge.dataset.status = call.status;
    badge.textContent = call.status;
    statusCell.append(badge);
    row.append(statusCell);
    const providerCell = document.createElement("td");
    providerCell.textContent = call.provider;
    row.append(providerCell);
    body.append(row);
  });
}

async function loadCalls() {
  try {
    const calls = await getJson("/calls");
    if (!Array.isArray(calls)) throw new Error("The call response had an unexpected format.");
    renderCalls(calls);
  } catch (error) {
    setMetric("metric-active", null, `Could not load calls: ${error.message}`);
    setMetric("metric-answered", null, `Could not load calls: ${error.message}`);
    byId("calls-count").textContent = "Unavailable";
    setTableError("calls-body", 5, `Could not load call records: ${error.message}`);
  }
}

async function refreshDashboard() {
  const button = byId("refresh-button");
  button.disabled = true;
  button.querySelector("span").textContent = "Refreshing…";
  try {
    await Promise.all([loadHealth(), loadAgents(), loadCalls()]);
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "Refresh";
  }
}

function setUnavailableSimulation() {
  setMetric("metric-rejections", null, "Unavailable until a simulation runs");
  byId("capacity-badge").dataset.state = "unavailable";
  byId("capacity-badge").textContent = "No result";
  byId("safety-empty").hidden = false;
  byId("safety-summary").hidden = true;
  setTableError("decisions-body", 6, "No simulation results yet.");
}

function renderSimulation(result) {
  setMetric("metric-rejections", result.safety_rejections, "From the latest modeled simulation");
  byId("safety-empty").hidden = true;
  byId("safety-summary").hidden = false;
  byId("result-proposed").textContent = numberFormat.format(result.calls_proposed);
  byId("result-reserved").textContent = numberFormat.format(result.calls_reserved);
  byId("result-rejected").textContent = numberFormat.format(result.safety_rejections);
  byId("result-provider-accepted").textContent = numberFormat.format(result.provider_accepted_calls);
  byId("result-provider-failures").textContent = numberFormat.format(result.provider_failures);
  byId("result-answered").textContent = numberFormat.format(result.answered_calls);

  const invariant = byId("result-invariant");
  invariant.textContent = result.capacity_invariant_holds ? "Holds" : "Violated";
  invariant.className = result.capacity_invariant_holds ? "status-safe" : "status-unsafe";
  const badge = byId("capacity-badge");
  badge.dataset.state = result.capacity_invariant_holds ? "safe" : "unsafe";
  badge.textContent = result.capacity_invariant_holds ? "Capacity safe" : "Capacity exceeded";

  const proposed = Math.max(0, result.calls_proposed);
  const reservedWidth = proposed ? (result.calls_reserved / proposed) * 100 : 0;
  const rejectedWidth = proposed ? (result.safety_rejections / proposed) * 100 : 0;
  byId("bar-approved").style.width = `${Math.min(100, reservedWidth)}%`;
  byId("bar-rejected").style.width = `${Math.min(100, rejectedWidth)}%`;
  byId("bar-approved").title = `${numberFormat.format(result.calls_reserved)} calls reserved`;
  byId("bar-rejected").title = `${numberFormat.format(result.safety_rejections)} safety rejections`;
  byId("decisions-description").textContent = `${result.mode} · seed ${result.seed} · ${result.elapsed_seconds} simulated seconds. Decisions are from the latest simulation only.`;

  const decisions = result.pacing_and_safety_decisions.slice(-8).reverse();
  if (!decisions.length) {
    setTableError("decisions-body", 6, "The simulation completed without pacing decisions.");
    return;
  }
  const body = byId("decisions-body");
  body.replaceChildren();
  decisions.forEach((decision) => {
    const row = document.createElement("tr");
    const cells = [
      `${numberFormat.format(decision.second)}s`,
      "Pacing decision",
      numberFormat.format(decision.proposed_calls),
      numberFormat.format(decision.safety_approved_calls),
      numberFormat.format(decision.safety_rejected_calls),
      numberFormat.format(decision.available_agents),
    ];
    cells.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      if (index === 1) {
        const badge = document.createElement("span");
        badge.className = "status-pill";
        badge.dataset.status = decision.safety_rejected_calls > 0 ? "RINGING" : "COMPLETED";
        badge.textContent = value;
        cell.replaceChildren(badge);
      }
      row.append(cell);
    });
    body.append(row);
  });
}

function numberField(formData, name) {
  const raw = formData.get(name);
  return raw === "" ? null : Number(raw);
}

function getSimulationPayload(form) {
  const data = new FormData(form);
  return {
    agent_count: numberField(data, "agent_count"),
    duration_seconds: numberField(data, "duration_seconds"),
    answer_rate: numberField(data, "answer_rate"),
    avg_talk_seconds: numberField(data, "avg_talk_seconds"),
    mode: data.get("mode"),
    max_calls: numberField(data, "max_calls"),
    setup_seconds: numberField(data, "setup_seconds"),
    provider_latency_seconds: numberField(data, "provider_latency_seconds"),
    provider_failure_rate: numberField(data, "provider_failure_rate"),
    agent_drop_fraction: numberField(data, "agent_drop_fraction"),
    agent_drop_at_second: numberField(data, "agent_drop_at_second"),
    seed: numberField(data, "seed"),
  };
}

function showSimulationMessage(message, state) {
  const element = byId("simulation-message");
  element.textContent = message;
  element.dataset.state = state;
}

async function submitSimulation(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = byId("run-simulation");
  const payload = getSimulationPayload(form);

  form.querySelector('[name="agent_drop_at_second"]').setCustomValidity("");
  if (payload.agent_drop_fraction > 0 && payload.agent_drop_at_second === null) {
    form.querySelector('[name="agent_drop_at_second"]').setCustomValidity("Set the second at which agents should drop.");
  }
  if (payload.agent_drop_fraction > 0 && payload.agent_drop_at_second > payload.duration_seconds) {
    form.querySelector('[name="agent_drop_at_second"]').setCustomValidity("The drop second must be within the simulation duration.");
  }
  if (!form.reportValidity()) return;

  button.disabled = true;
  button.querySelector("span").textContent = "Running…";
  showSimulationMessage("Simulation is running. Please wait for the backend result.", "loading");
  try {
    const result = await postSimulation(payload);
    renderSimulation(result);
    showSimulationMessage("Simulation completed successfully.", "success");
  } catch (error) {
    showSimulationMessage(`Simulation failed: ${error.message}`, "error");
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "Run Simulation";
  }
}

async function postSimulation(payload) {
  const response = await fetch("/simulate", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(payload),
  });
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error(`The API returned an unreadable response (${response.status}).`);
  }
  if (!response.ok) {
    const detail = result && typeof result.detail === "string" ? result.detail : `Request failed (${response.status}).`;
    throw new Error(detail);
  }
  if (!result || result.result_kind !== "modeled_simulation" || !Array.isArray(result.pacing_and_safety_decisions)) {
    throw new Error("The API returned an unexpected simulation result.");
  }
  return result;
}

byId("simulation-form").addEventListener("submit", submitSimulation);
byId("refresh-button").addEventListener("click", refreshDashboard);
setUnavailableSimulation();
refreshDashboard();
