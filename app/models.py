from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Boolean, BigInteger
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from .database import Base


class Monitor(Base):
    __tablename__ = "monitors"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, nullable=False)
    interval_seconds = Column(Integer, default=30)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    code_version = Column(String, nullable=True)

    # is_up is raw liveness (status API / root GET). health is what the UI shows:
    # "up" (green), "degraded" (yellow) or "down" (red); see compute_health().
    is_up = Column(Boolean, default=None)
    health = Column(String, nullable=True)
    last_state_change_ts = Column(BigInteger)

    checks = relationship("Check", back_populates="monitor", cascade="all, delete-orphan")
    events = relationship("StateEvent", back_populates="monitor", cascade="all, delete-orphan")
    query_statuses = relationship("QueryStatus", back_populates="monitor", cascade="all, delete-orphan")
    query_failures = relationship("QueryFailure", back_populates="monitor", cascade="all, delete-orphan")


class Check(Base):
    __tablename__ = "checks"

    id = Column(Integer, primary_key=True, index=True)
    monitor_id = Column(Integer, ForeignKey("monitors.id"))
    status_code = Column(Integer)
    # Nullable: a latency sample is only recorded on checks that actually timed a
    # request. ARAX nodes measure latency via a separate /query probe fired every
    # QUERY_INTERVAL, so their between-probe status polls carry no latency sample.
    response_time_ms = Column(Integer, nullable=True)
    checked_at = Column(DateTime(timezone=True), server_default=func.now())
    error_message = Column(String, nullable=True)
    code_version = Column(String, nullable=True)
    # Body of the response, kept only for failed checks (for the failure log).
    response_body = Column(String, nullable=True)

    monitor = relationship("Monitor", back_populates="checks")


class StateEvent(Base):
    __tablename__ = "state_events"

    id = Column(Integer, primary_key=True, index=True)
    monitor_id = Column(Integer, ForeignKey("monitors.id"))
    # is_up is False only for "down"; status adds the "degraded" distinction.
    # Rows written before status existed have it NULL (derive from is_up).
    is_up = Column(Boolean)
    status = Column(String, nullable=True)
    changed_at_ts = Column(BigInteger)

    monitor = relationship("Monitor", back_populates="events")


class QueryStatus(Base):
    """Latest result of one ARAX /query health probe (one row per monitor per
    query kind). `ok` is the confirmed state: it only flips to False after
    QUERY_FAIL_THRESHOLD consecutive failures, and is NULL until first checked."""
    __tablename__ = "query_statuses"

    id = Column(Integer, primary_key=True, index=True)
    monitor_id = Column(Integer, ForeignKey("monitors.id"), index=True)
    kind = Column(String, nullable=False)
    ok = Column(Boolean, nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    error = Column(String, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    checked_ts = Column(BigInteger, nullable=False)

    monitor = relationship("Monitor", back_populates="query_statuses")


class QueryFailure(Base):
    """One failed /query probe, with the response that came back. Successful
    probes aren't logged."""
    __tablename__ = "query_failures"

    id = Column(Integer, primary_key=True, index=True)
    monitor_id = Column(Integer, ForeignKey("monitors.id"), index=True)
    kind = Column(String, nullable=False)
    checked_ts = Column(BigInteger, nullable=False, index=True)
    http_status = Column(Integer, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    error = Column(String, nullable=True)
    response = Column(String, nullable=True)

    monitor = relationship("Monitor", back_populates="query_failures")
