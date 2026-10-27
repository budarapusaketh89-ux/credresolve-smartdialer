from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select
from .database import upgrade_schema, get_db
from .models import Agent, Borrower, Campaign, DialCall
from .schemas import (
    AgentCreate,
    BorrowerCreate,
    CampaignCreate,
    DialResponse,
    DialRequest,
    PacingDecision,
    ProviderEvent,
    SimulationRequest,
    SimulationScenariosRequest,
)
from .services import reserve_and_dial, get_pacing_decision, handle_provider_event, dispatch_outbox
from .simulation import run_assignment_scenarios, run_simulation

upgrade_schema()
app = FastAPI(title="CredResolve SmartDialer", version="0.1.0",
              description="Educational SmartDialer prototype with pacing and a safety gate.")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/agents")
def create_agent(payload: AgentCreate, db: Session = Depends(get_db)):
    obj = Agent(name=payload.name, status=payload.status.value)
    db.add(obj); db.commit(); db.refresh(obj)
    return {"id": obj.id, "name": obj.name, "status": obj.status}

@app.get("/agents")
def list_agents(db: Session = Depends(get_db)):
    return [{"id": a.id, "name": a.name, "status": a.status} for a in db.scalars(select(Agent).order_by(Agent.id))]

@app.post("/borrowers")
def create_borrower(payload: BorrowerCreate, db: Session = Depends(get_db)):
    obj = Borrower(name=payload.name, phone=payload.phone)
    db.add(obj); db.commit(); db.refresh(obj)
    return {"id": obj.id, "name": obj.name, "status": obj.status}

@app.get("/borrowers")
def list_borrowers(db: Session = Depends(get_db)):
    return [{"id": b.id, "name": b.name, "phone": b.phone, "status": b.status} for b in db.scalars(select(Borrower).order_by(Borrower.id))]

@app.post("/campaigns")
def create_campaign(payload: CampaignCreate, db: Session = Depends(get_db)):
    obj = Campaign(name=payload.name, mode=payload.mode, answer_rate=payload.answer_rate,
                   avg_talk_seconds=payload.avg_talk_seconds)
    db.add(obj); db.commit(); db.refresh(obj)
    return {"id": obj.id, "name": obj.name, "mode": obj.mode, "answer_rate": obj.answer_rate}

@app.get("/campaigns/{campaign_id}/pacing", response_model=PacingDecision)
def pacing(campaign_id: int, requested_calls: int = 10, db: Session = Depends(get_db)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign: raise HTTPException(404, "Campaign not found")
    return get_pacing_decision(db, campaign, requested_calls)

@app.post("/campaigns/{campaign_id}/dial", response_model=DialResponse)
def dial(campaign_id: int, payload: DialRequest, provider: str = "mock-fast", db: Session = Depends(get_db)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign: raise HTTPException(404, "Campaign not found")
    try:
        batch = reserve_and_dial(db, campaign, payload.max_calls, provider)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "proposed_calls": batch.proposed_calls,
        "safety_approved_calls": batch.safety_approved_calls,
        "created": len(batch.calls),
        "calls": [
            {
                "id": call.id,
                "agent_id": call.agent_id,
                "borrower_id": call.borrower_id,
                "status": call.status,
                "provider": call.provider,
            }
            for call in batch.calls
        ],
    }

@app.get("/calls")
def list_calls(db: Session = Depends(get_db)):
    return [{"id": c.id, "campaign_id": c.campaign_id, "borrower_id": c.borrower_id,
             "agent_id": c.agent_id, "status": c.status, "provider": c.provider}
            for c in db.scalars(select(DialCall).order_by(DialCall.id))]

@app.post("/provider-events")
def provider_event(payload: ProviderEvent, db: Session = Depends(get_db)):
    try:
        return handle_provider_event(
            db, payload.event_id, payload.call_id, payload.status.value, payload.occurred_at
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc))

@app.post("/simulate")
def simulate(payload: SimulationRequest):
    return run_simulation(**payload.model_dump())

@app.post("/simulate/scenarios")
def simulate_scenarios(payload: SimulationScenariosRequest):
    return run_assignment_scenarios(seed=payload.seed, mode=payload.mode)

@app.post("/outbox/dispatch")
def dispatch_pending(limit: int = 100, db: Session = Depends(get_db)):
    if not 1 <= limit <= 500:
        raise HTTPException(422, "limit must be between 1 and 500")
    return dispatch_outbox(db, limit)
