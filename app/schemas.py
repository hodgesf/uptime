from pydantic import BaseModel
from datetime import datetime


class MonitorOut(BaseModel):
    id: int
    url: str
    interval_seconds: int
    created_at: datetime
    is_up: bool
    last_state_change: datetime

    class Config:
        from_attributes = True


class CheckOut(BaseModel):
    id: int
    status_code: int
    response_time_ms: int
    checked_at: datetime

    class Config:
        from_attributes = True