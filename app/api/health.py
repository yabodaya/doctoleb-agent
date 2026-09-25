"""Health endpoints.

/health is liveness: is this process alive? It does no I/O on purpose, so a
database or Redis outage never makes an orchestrator kill a healthy API.
/health/ready (Task 3) is readiness: can this process actually serve traffic?
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]


@router.get("/health")
async def health() -> HealthResponse:
    """Liveness. Touches nothing external."""
    return HealthResponse(status="ok")
