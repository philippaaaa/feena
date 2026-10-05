"""Turn a page into a numbered menu of things a user could act on.

Agents used to answer with a free-text selector that went straight into ``page.click``. That is
brittle (the model invents selectors that don't exist) and it only works with models that can
write arbitrary text. Instead we enumerate the interactive nodes in the accessibility tree and ask
the decider to pick one *by number*. Each candidate is addressed by role + accessible name (+ an
ordinal when names repeat), which:

* maps 1:1 to ``page.get_by_role(...)``, so replay and codegen are exact;
* survives CSS/class/test-id churn, so memoized macros stay valid longer;
* turns "what should I click?" into a fixed-shape choice, which is what cheap decision models
  (e.g. a System-1 classifier) can answer. Free-text models can still answer it too.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

import yaml

INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox", "listbox",
    "option", "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "switch", "slider",
    "spinbutton", "treeitem",
})
FILLABLE_ROLES = frozenset({"textbox", "searchbox", "combobox", "spinbutton"})
# Structural nodes that make a page recognisable without including user content. Links, options
# and tree items are left out because their names are usually data (task titles, search hits).
_FINGERPRINT_ROLES = (INTERACTIVE_ROLES - {"link", "option", "treeitem"}) | {
    "heading", "navigation", "main", "form", "dialog"}

MAX_CANDIDATES = 60
_NAME_LIMIT = 80


@dataclass(frozen=True)
class Candidate:
    index: int
    role: str
    name: str
    nth: int = 0          # 0-based ordinal among nodes with the same role + name
    password: bool = False

    @property
    def fillable(self) -> bool:
        return self.role in FILLABLE_ROLES

    def label(self) -> str:
        dup = f" (#{self.nth + 1})" if self.nth else ""
        return f'[{self.index}] {self.role} "{self.name[:_NAME_LIMIT]}"{dup}'

    def selector(self) -> str:
        """A Playwright selector string equivalent to :meth:`locator` (for steps and reports)."""
        name = self.name.replace("\\", "\\\\").replace('"', '\\"')
        base = (f'internal:label="{name}"s >> xpath=self::input[@type="password"]'
                if self.password else f'role={self.role}[name="{name}"s]')
        return f"{base} >> nth={self.nth}"

    def locator(self, page):
        return resolve(page, self.ref())

    def ref(self) -> dict:
        """Serializable address used by macros (no index: indices are per-snapshot)."""
        ref = {"role": self.role, "name": self.name, "nth": self.nth}
        if self.password:
            ref["password"] = True
        return ref


def _ax_tree(page) -> dict:
    """Read modern Playwright ARIA YAML, retaining full accessible names.

    An unavailable snapshot is an error: an empty fingerprint cannot validate replay.
    """
    if hasattr(page, "accessibility"):
        tree = page.accessibility.snapshot()
        if tree:
            return tree
    raw = page.locator("body").aria_snapshot()
    data = yaml.safe_load(raw)
    def convert(items):
        out = []
        for item in items or []:
            if isinstance(item, str):
                label, children = item, None
            elif isinstance(item, dict):
                label, children = next(iter(item.items()))
            else:
                continue
            match = re.match(r'^(\w+)(?:\s+("(?:[^"\\]|\\.)*"))?', str(label))
            if not match:
                continue
            name = json.loads(match[2]) if match[2] else ""
            node = {"role": match[1], "name": name,
                    "disabled": "[disabled]" in str(label)}
            if isinstance(children, list):
                node["children"] = convert(children)
            out.append(node)
        return out
    nodes = convert(data)
    if not nodes:
        raise ValueError("Page accessibility structure unavailable")
    return {"role": "WebArea", "children": nodes}


def _walk(node: dict, out: list[dict]) -> None:
    if node.get("role"):
        out.append(node)
    for child in node.get("children", []) or []:
        _walk(child, out)


def _clean(name: str) -> str:
    return name or ""


def candidates_from_tree(tree: dict, limit: int = MAX_CANDIDATES) -> list[Candidate]:
    nodes: list[dict] = []
    _walk(tree, nodes)
    seen: dict[tuple[str, str], int] = {}
    out: list[Candidate] = []
    for node in nodes:
        role = node.get("role", "")
        if role not in INTERACTIVE_ROLES:
            continue
        name = _clean(node.get("name", ""))
        if not name:
            continue  # unnamed controls can't be addressed reliably (and are an a11y bug)
        key = (role, name)
        nth = seen.get(key, 0)
        seen[key] = nth + 1
        if node.get("disabled"):
            continue
        out.append(Candidate(index=len(out), role=role, name=name, nth=nth))
        if len(out) >= limit:
            break
    return out


def extract_candidates(page, limit: int = MAX_CANDIDATES) -> list[Candidate]:
    candidates = candidates_from_tree(_ax_tree(page), limit)
    # Password inputs deliberately have no implicit ARIA role. Address them by label.
    try:
        passwords = page.locator('input[type="password"]').evaluate_all(r"""els => els.map(el => ({
            name: el.getAttribute('aria-label') ||
              (el.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.textContent || '').join(' ').trim() ||
              Array.from(el.labels || []).map(l => l.textContent).join(' ').trim(),
            disabled: el.disabled, visible: !!el.getClientRects().length
        }))""")
    except AttributeError:
        passwords = []  # small fake pages and older browser adapters
    seen = {}
    for item in passwords:
        name = item["name"]
        nth = seen.get(name, 0)
        seen[name] = nth + 1
        if name and not item["disabled"] and item["visible"] and len(candidates) < limit:
            candidates.append(Candidate(len(candidates), "textbox", name, password=True, nth=nth))
    return candidates


def resolve(page, ref: dict):
    """Locator for a stored ``{"role", "name", "nth"}`` reference."""
    if ref.get("password"):
        loc = page.get_by_label(ref["name"], exact=True).and_(page.locator('input[type="password"]'))
    else:
        loc = page.get_by_role(ref["role"], name=ref["name"], exact=True)
    return loc.nth(int(ref.get("nth", 0)))


def fingerprint_tree(tree: dict, path: str = "") -> str:
    """Stable hash of a page's *structure* (roles + control/heading names), not its content.

    Used to tell whether a memoized macro still lands where it used to. Text nodes are ignored
    so user data (task titles, greetings) doesn't make every page look new.
    """
    nodes: list[dict] = []
    _walk(tree, nodes)
    parts = sorted(f"{n.get('role')}:{_clean(n.get('name', ''))}"
                    for n in nodes if n.get("role") in _FINGERPRINT_ROLES)
    if not parts:
        raise ValueError("Page has no fingerprintable accessibility structure")
    raw = path + "\n" + "\n".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def fingerprint(page) -> str:
    from urllib.parse import urlsplit
    return fingerprint_tree(_ax_tree(page), urlsplit(page.url).path)
