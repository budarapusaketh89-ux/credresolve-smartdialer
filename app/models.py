from sqlalchemy import String, Integer, Float, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime, timezone
from .database import Base

def utcnow():
    return datetime.now(timezone.utc)

class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="AVAILABLE")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

class Borrower(Base):
    __tablename__ = "borrowers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="QUEUED")

class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    answer_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.3)
    avg_talk_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    provider_healthy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    provider_failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

class DialCall(Base):
    __tablename__ = "calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    borrower_id: Mapped[int] = mapped_column(ForeignKey("borrowers.id"), nullable=False)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="QUEUED")
    provider: Mapped[str] = mapped_column(String(30), nullable=False, default="mock-fast")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class ProviderEventRecord(Base):
    __tablename__ = "provider_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class OutboxMessage(Base):
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id"), nullable=False, unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
