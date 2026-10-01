"""Validated, reproducible browser journeys and environmental variations."""
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def local_path(value: str) -> str:
    parts = urlsplit(value)
    if (not value.startswith("/") or value.startswith("//") or "\\" in value
            or parts.scheme or parts.netloc or parts.fragment
            or any(ord(c) < 32 for c in value)):
        raise ValueError("use a target-relative path, e.g. /checkout")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NetworkProfile(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    effect: Literal["normal", "delay", "abort", "drop_response"] = "normal"
    path: str = "/"
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] | None = None
    delay_ms: int = Field(default=500, ge=0, le=30000)
    occurrence: int = Field(default=1, ge=1, le=1000)

    _path = field_validator("path")(local_path)


class BrowserStep(StrictModel):
    action: Literal["goto", "click", "fill", "press", "back", "reload", "new_tab",
                    "switch_tab", "offline", "online", "wait"]
    target: str = ""
    value: str = ""
    tab: str = Field(default="main", pattern=r"^[a-z][a-z0-9_-]*$")
    wait_ms: int = Field(default=0, ge=0, le=30000)

    @model_validator(mode="after")
    def validate_action(self):
        if self.action == "goto":
            local_path(self.target)
        if self.action in ("click", "fill", "press") and not self.target:
            raise ValueError("interaction actions require a selector")
        if self.action == "press" and not self.value:
            raise ValueError("press requires a key in value")
        return self


class BrowserAssertion(StrictModel):
    kind: Literal["visible", "text", "count", "json"]
    target: str = Field(min_length=1)
    expected: Any = True
    # Assertions inspect a final response, including expected denials and errors.
    status_code: int = Field(default=200, ge=200, lt=600)

    @model_validator(mode="after")
    def validate_assertion(self):
        if self.kind == "json":
            local_path(self.target)
            if not isinstance(self.expected, dict) or not self.expected:
                raise ValueError("JSON assertions require a nonempty expected object")
        if self.kind == "count" and (type(self.expected) is not int or self.expected < 0):
            raise ValueError("count requires a nonnegative integer")
        if self.kind == "text" and not isinstance(self.expected, str):
            raise ValueError("text requires a string")
        if self.kind == "visible" and self.expected is not True:
            raise ValueError("visible expects true; use count: 0 for absence")
        return self


class BrowserScenario(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    goal: str = Field(min_length=1)
    steps: list[BrowserStep] = Field(min_length=1, max_length=100)
    assertions: list[BrowserAssertion] = Field(min_length=1, max_length=50)
    profiles: list[NetworkProfile] = Field(
        default_factory=lambda: [NetworkProfile(name="normal")], min_length=1, max_length=20)
    viewport_width: int = Field(default=1280, ge=240, le=3840)
    viewport_height: int = Field(default=800, ge=240, le=2160)
    timeout_ms: int = Field(default=3000, ge=100, le=30000)

    @model_validator(mode="after")
    def unique_profiles(self):
        if len({p.name for p in self.profiles}) != len(self.profiles):
            raise ValueError("profile names must be unique within a scenario")
        tabs = {"main"}
        for step in self.steps:
            if step.action == "new_tab":
                if step.tab in tabs:
                    raise ValueError("new_tab must use a new tab name")
                tabs.add(step.tab)
            elif step.action == "switch_tab" and step.tab not in tabs:
                raise ValueError("switch_tab must reference an existing tab")
        return self
