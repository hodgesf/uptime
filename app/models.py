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
    

    is_up = Column(Boolean, default=None)
    last_state_change_ts = Column(BigInteger)

    checks = relationship("Check", back_populates="monitor", cascade="all, delete-orphan")
    events = relationship("StateEvent", back_populates="monitor", cascade="all, delete-orphan")


class Check(Base):
    __tablename__ = "checks"

    id = Column(Integer, primary_key=True, index=True)
    monitor_id = Column(Integer, ForeignKey("monitors.id"))
    status_code = Column(Integer)
    response_time_ms = Column(Integer)
    checked_at = Column(DateTime(timezone=True), server_default=func.now())

    monitor = relationship("Monitor", back_populates="checks")


class StateEvent(Base):
    __tablename__ = "state_events"

    id = Column(Integer, primary_key=True, index=True)
    monitor_id = Column(Integer, ForeignKey("monitors.id"))
    is_up = Column(Boolean)
    changed_at_ts = Column(BigInteger)

    monitor = relationship("Monitor", back_populates="events")