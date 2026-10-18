"""
Purpose: Pydantic schemas and type definitions for Feedback and Agent runtime interactions.
Architecture/Context: Used across FastAPI endpoints for structured request and response models.
Dependencies/Side Effects: Pydantic base models.

DEMO SOFTWARE DISCLAIMER:
This code is provided strictly as a demonstration and reference implementation.
It comes with NO WARRANTY, NO GUARANTEE, and NO SUPPORT of any kind, either expressed or implied.
Use and deployment in any environment is entirely at your own discretion and risk.
"""

import uuid
from typing import (
    Literal,
)

from pydantic import (
    BaseModel,
    Field,
)


class Feedback(BaseModel):
    """Represents feedback for a conversation."""

    score: int | float
    text: str | None = ""
    log_type: Literal["feedback"] = "feedback"
    service_name: Literal["telecom-agent"] = "telecom-agent"
    user_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
