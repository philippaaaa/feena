"""Load and validate feena.yaml into typed config objects."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, field_validator

from .discovery_config import DiscoveryGoal
from .simulation_config import BrowserScenario


class TargetConfig(BaseModel):
    compose: str
    service: str
    port: int
    healthcheck: str = "/"
    boot_timeout: int = 120


class UserConfig(BaseModel):
    label: str
    email: str
    password: str


class ScopeConfig(BaseModel):
    include: list[str] = Field(default_factory=lambda: ["/"])
    exclude: list[str] = Field(default_factory=list)


class RunConfig(BaseModel):
    agents: list[str] = Field(default_factory=lambda: ["regular", "clumsy", "hostile"])
    budget_seconds: int = 300
    max_steps: int = 40
    headed: bool = False


class ReportConfig(BaseModel):
    out_dir: str = "./.feena"
    format: str = "markdown"  # "markdown" | "github"


class AttestConfig(BaseModel):
    enabled: bool = False
    key: str = "./feena_signing.key"   # Ed25519 private key; create with `feena keygen`


class CorpusConfig(BaseModel):
    enabled: bool = False                # opt-in only
    stack: str = "unknown"               # e.g. "nextjs-postgres"; the one non-pattern field
    endpoint: str | None = None          # used only by the explicit `feena corpus upload`


class OutcomeStep(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH"] = "GET"
    path: str
    json_body: dict[str, Any] | None = None
    expected_status: int = Field(default=200, ge=200, lt=300)
    expected_json: dict[str, Any] = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def local_path(cls, value: str) -> str:
        parts = urlsplit(value)
        if (not value.startswith("/") or value.startswith("//") or "\\" in value
                or parts.scheme or parts.netloc or parts.fragment
                or any(ord(c) < 32 for c in value)):
            raise ValueError("outcome paths must be target-relative, e.g. /api/tasks/1")
        return value


class OutcomeConfig(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    user: int | None = Field(default=1, ge=1, le=2)
    identity_path: str = "/api/me"
    steps: list[OutcomeStep] = Field(min_length=1)

    @field_validator("identity_path")
    @classmethod
    def local_identity_path(cls, value: str) -> str:
        return OutcomeStep.local_path(value)


class Config(BaseModel):
    target: TargetConfig
    users: list[UserConfig] = Field(default_factory=list)
    scope: ScopeConfig = Field(default_factory=ScopeConfig)
    run: RunConfig = Field(default_factory=RunConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    attest: AttestConfig = Field(default_factory=AttestConfig)
    corpus: CorpusConfig = Field(default_factory=CorpusConfig)
    outcomes: list[OutcomeConfig] = Field(default_factory=list)
    scenarios: list[BrowserScenario] = Field(default_factory=list)
    discovery: list[DiscoveryGoal] = Field(default_factory=list)

    @field_validator("discovery")
    @classmethod
    def unique_discovery_goals(cls, value: list[DiscoveryGoal]) -> list[DiscoveryGoal]:
        if len({goal.name for goal in value}) != len(value):
            raise ValueError("discovery goal names must be unique")
        return value

    @field_validator("scenarios")
    @classmethod
    def unique_scenarios(cls, value: list[BrowserScenario]) -> list[BrowserScenario]:
        if len({s.name for s in value}) != len(value):
            raise ValueError("scenario names must be unique")
        return value

    @field_validator("outcomes")
    @classmethod
    def unique_outcomes(cls, value: list[OutcomeConfig]) -> list[OutcomeConfig]:
        if len({o.name for o in value}) != len(value):
            raise ValueError("outcome names must be unique")
        return value

    # Resolved at load time so relative paths in the yaml behave predictably.
    root: Path = Field(default=Path("."), exclude=True)

    @property
    def compose_path(self) -> Path:
        return (self.root / self.target.compose).resolve()

    @property
    def out_path(self) -> Path:
        return (self.root / self.report.out_dir).resolve()


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(
            f"No config at {path}. Copy feena.example.yaml to feena.yaml and edit it."
        )
    data = yaml.safe_load(path.read_text()) or {}
    cfg = Config(**data)
    cfg.root = path.parent
    return cfg
