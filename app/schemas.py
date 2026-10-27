from enum import Enum
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime

class AgentStatus(str, Enum):
    OFFLINE = "OFFLINE"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    DIALING = "DIALING"
    CONNECTED = "CONNECTED"
    WRAP_UP = "WRAP_UP"
    PAUSED = "PAUSED"

class CallStatus(str, Enum):
    QUEUED = "QUEUED"
    RESERVED = "RESERVED"
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    CONNECTED = "CONNECTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    status: AgentStatus = AgentStatus.AVAILABLE

class BorrowerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=5, max_length=30)

class CampaignCreate(BaseModel):
    name: str
    mode: str = Field(pattern="^(PROGRESSIVE|PREDICTIVE)$")
    answer_rate: float = Field(default=0.3, ge=0.0, le=1.0)
    avg_talk_seconds: int = Field(default=120, ge=1, le=3600)

class DialRequest(BaseModel):
    campaign_id: int
    max_calls: int = Field(default=10, ge=1, le=100)

class DialCallResponse(BaseModel):
    id: int
    agent_id: int | None
    borrower_id: int
    status: CallStatus
    provider: str

class DialResponse(BaseModel):
    proposed_calls: int
    safety_approved_calls: int
    created: int
    calls: list[DialCallResponse]

class ProviderEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    call_id: int
    status: CallStatus
    occurred_at: Optional[datetime] = None

class SimulationRequest(BaseModel):
    agent_count: int = Field(default=20, ge=1, le=500)
    duration_seconds: int = Field(default=120, ge=1, le=86400)
    answer_rate: float = Field(default=0.3, ge=0.0, le=1.0)
    avg_talk_seconds: int = Field(default=120, ge=1, le=3600)
    mode: str = Field(default="PREDICTIVE", pattern="^(PROGRESSIVE|PREDICTIVE)$")
    max_calls: int = Field(default=100, ge=1, le=10000)
    setup_seconds: int = Field(default=2, ge=1, le=300)
    provider_latency_seconds: float = Field(default=0.0, ge=0.0, le=300)
    provider_failure_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    agent_drop_fraction: float = Field(default=0.0, ge=0.0, le=1.0)
    agent_drop_at_second: int | None = Field(default=None, ge=1, le=86400)
    rate_changes: list["SimulationRateChange"] = Field(default_factory=list)
    seed: int = Field(default=1, ge=0, le=2147483647)

class SimulationRateChange(BaseModel):
    at_second: int = Field(ge=1, le=86400)
    answer_rate: float = Field(ge=0.0, le=1.0)
    avg_talk_seconds: int = Field(ge=1, le=3600)

class SimulationScenariosRequest(BaseModel):
    seed: int = Field(default=1, ge=0, le=2147483647)
    mode: str = Field(default="PREDICTIVE", pattern="^(PROGRESSIVE|PREDICTIVE)$")

class PacingDecision(BaseModel):
    available_agents: int
    active_calls: int
    ringing_calls: int
    connected_calls: int
    setup_calls: int
    capacity: int
    observed_answer_rate: float
    requested_calls: int
    proposed_calls: int
    approved_calls: int = Field(
        deprecated="Use proposed_calls; pacing previews do not approve reservations."
    )
    reason: str
