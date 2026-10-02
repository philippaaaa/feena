"""Open-core seam: third-party check packs register via Python entry points.

The open-source runner ships the built-in checks. A separate package (e.g. a proprietary
``feena-pro`` pack built from the corpus) adds checks without forking, by declaring:

    [project.entry-points."feena.checks"]
    graphql_introspection = "feena_pro.checks:graphql_introspection"

Each entry point resolves to ``callable(cfg, sandbox) -> list[Finding]``, the same contract as
the built-ins. Plugin checks run under the same sandbox guard as everything else.
"""
from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import entry_points

GROUP = "feena.checks"


def load_plugin_checks() -> list[tuple[str, Callable]]:
    out = []
    for ep in entry_points(group=GROUP):
        try:
            out.append((ep.name, ep.load()))
        except Exception:  # noqa: BLE001, S112 - ignore unavailable optional plugins
            continue  # a broken plugin must never break the run
    return out


DECIDER_GROUP = "feena.deciders"


def load_decider(name: str):
    """Load a fast decider registered under ``feena.deciders`` (e.g. a Jev adapter):

        [project.entry-points."feena.deciders"]
        jev = "my_pkg.jev:JevDecider"

    The entry point resolves to a zero-arg callable returning an object with ``name``,
    ``available`` and ``decide(DecisionRequest) -> Decision`` (see ``feena.llm.Decider``).
    """
    for ep in entry_points(group=DECIDER_GROUP):
        if ep.name == name:
            return ep.load()()
    raise ValueError(f"No decider plugin named {name!r} in entry-point group {DECIDER_GROUP!r}")
