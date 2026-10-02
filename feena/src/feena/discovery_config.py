"""Operator-authored boundaries for a browser journey discovery job."""
from pydantic import Field, field_validator

from .simulation_config import BrowserAssertion, StrictModel, local_path


class DiscoveryGoal(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]*$", max_length=80)
    goal: str = Field(min_length=1, max_length=4000)
    start_path: str = "/"
    assertions: list[BrowserAssertion] = Field(min_length=1, max_length=50)
    max_steps: int = Field(default=20, ge=1, le=50)
    timeout_seconds: int = Field(default=60, ge=1, le=120)

    _path = field_validator("start_path")(local_path)
