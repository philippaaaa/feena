"""Which pages could a diff have changed? ("blast radius")

Builds a reverse import graph of the repo's front-end files, walks it from every changed file up
to the files that *are* routes (Next.js app/pages, SvelteKit, Nuxt, Remix flat routes, plus
Flask/FastAPI decorators and the templates they render), and reports the affected URLs with the
import chain that links each one to the diff.

What it is for: telling the exploratory agents where to spend their step budget, and telling the
macro cache which shortcuts not to trust this run. What it is *not*: a filter. Static imports
miss global CSS cascade, context/store consumers, feature flags, dynamic routes defined in code,
and backend changes. Those show up as ``global_reasons`` or ``unmapped`` so the caller can widen
the run instead of silently skipping pages.

Source never reaches the agents: they only receive the list of URLs, which keeps Feena's
"agents can't see your code" property intact.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")
STYLE_EXTS = (".css", ".scss", ".sass", ".less")
GRAPH_EXTS = JS_EXTS + STYLE_EXTS
SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".next", ".nuxt", ".svelte-kit", "out",
             "coverage", ".turbo", ".vercel", ".feena", ".ui-agent", "__pycache__", ".venv",
             "venv"}
# Changes here can affect every page without any import edge pointing at them.
GLOBAL_FILES = re.compile(
    r"(^|/)(package\.json|next\.config\.[cm]?[jt]s|vite\.config\.[cm]?[jt]s|nuxt\.config\.[jt]s|"
    r"svelte\.config\.js|tailwind\.config\.[cm]?[jt]s|postcss\.config\.[cm]?[jt]s|"
    r"middleware\.[jt]s|\.env[\w.]*|tsconfig\.json)$")

_IMPORT_RES = [
    re.compile(r"""\bimport\s+(?:[\w*{}\s,$]+?\s+from\s+)?['"]([^'"]+)['"]"""),
    re.compile(r"""\bexport\s+(?:\*|\{[^}]*\})\s*(?:as\s+\w+\s*)?from\s+['"]([^'"]+)['"]"""),
    re.compile(r"""\b(?:require|import)\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""@import\s+(?:url\()?['"]([^'"]+)['"]"""),
]
_PY_ROUTE = re.compile(
    r"""^\s*@[\w.]+\.(?:route|get|post|put|patch|delete|api_route)\(\s*['"]([^'"]+)['"]""")
_PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s+\w+")
_PY_TEMPLATE = re.compile(r"""render_template(?:_string)?\(\s*['"]([^'"]+\.html?)['"]""")


@dataclass
class RouteHit:
    route: str
    file: str
    via: list[str] = field(default_factory=list)   # changed file ... -> route file
    distance: int = 0


@dataclass
class BlastRadius:
    base: str
    changed: list[str] = field(default_factory=list)
    routes: list[RouteHit] = field(default_factory=list)
    global_reasons: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    all_routes: list[str] = field(default_factory=list)

    @property
    def is_global(self) -> bool:
        return bool(self.global_reasons)

    @property
    def affected_routes(self) -> list[str]:
        return [h.route for h in self.routes]

    def focus_routes(self, limit: int = 8) -> list[str]:
        return [h.route for h in sorted(self.routes, key=lambda h: (h.distance, h.route))][:limit]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    def render_markdown(self) -> str:
        lines = ["## Blast radius", "", (f"Diff against `{self.base}`: "
                 f"{len(self.changed)} changed file(s), {len(self.routes)} affected route(s).")]
        if self.global_reasons:
            lines += ["", "**Global changes** (may affect every page): "
                      + ", ".join(f"`{r}`" for r in self.global_reasons)]
        if self.routes:
            lines += ["", "| Route | Distance | Via |", "|---|---|---|"]
            for h in sorted(self.routes, key=lambda h: (h.distance, h.route)):
                via = " → ".join(f"`{p}`" for p in h.via) if h.via else f"`{h.file}`"
                lines.append(f"| `{h.route}` | {h.distance} | {via} |")
        if self.unmapped:
            lines += ["", "Changed files not linked to any route (coverage unknown, widen the "
                      "run or check by hand): " + ", ".join(f"`{u}`" for u in self.unmapped)]
        return "\n".join(lines)


# --------------------------------------------------------------------------- route patterns


_DYN = re.compile(r"^(\[\[?\.\.\.[^\]]+\]\]?|\[[^\]]+\]|<[^>]+>|\{[^}]+\}|:\w+|\$\w+)$")


def route_matches(pattern: str, path: str) -> bool:
    """Does a concrete path match a route pattern such as ``/tasks/[id]`` or ``/t/<int:id>``?"""
    pat = [s for s in pattern.strip("/").split("/") if s]
    got = [s for s in path.split("?")[0].strip("/").split("/") if s]
    for i, seg in enumerate(pat):
        if seg.startswith(("[...", "[[...")):
            return len(got) >= i + (0 if seg.startswith("[[") else 1)
        if i >= len(got):
            return False
        if not _DYN.match(seg) and seg != got[i]:
            return False
    return len(got) == len(pat)


def is_concrete(route: str) -> bool:
    return not any(_DYN.match(s) for s in route.strip("/").split("/") if s)


def _normalize_py_route(route: str) -> str:
    route = re.sub(r"<(?:\w+:)?(\w+)>", r"[\1]", route)   # flask <int:id>
    route = re.sub(r"\{(\w+)(?::[^}]*)?\}", r"[\1]", route)   # fastapi {id}
    return route if route.startswith("/") else "/" + route


# --------------------------------------------------------------------------- route detection


def route_for_file(rel: str) -> tuple[str, str] | None:
    """(kind, route) if ``rel`` is a page or layout file for a known framework, else None.

    kind is "page" or "layout" (a layout affects every route under its prefix).
    """
    p = PurePosixPath(rel)
    parts = list(p.parts)
    name = p.name
    stem = p.stem

    def clean(segs: list[str]) -> str:
        keep = [s for s in segs if not (s.startswith("(") and s.endswith(")"))
                and not s.startswith("@")]
        return "/" + "/".join(keep)

    for root in (("src", "app"), ("app",)):
        n = len(root)
        if tuple(parts[:n]) == root and len(parts) > n:
            inner = parts[n:-1]
            if re.fullmatch(r"page\.(tsx|jsx|ts|js|mdx)", name):
                return "page", clean(inner)
            if re.fullmatch(r"(layout|template)\.(tsx|jsx|ts|js)", name):
                return "layout", clean(inner)
            if root == ("app",) and len(parts) >= 3 and parts[1] == "routes" and p.suffix in JS_EXTS:
                return "page", _remix_route(stem)          # Remix flat routes
            if root == ("app",) and name in ("root.tsx", "root.jsx"):
                return "layout", "/"
    for root in (("src", "pages"), ("pages",)):
        n = len(root)
        if tuple(parts[:n]) == root and len(parts) > n and p.suffix in JS_EXTS:
            inner = parts[n:-1]
            if inner[:1] == ["api"]:
                return None
            if stem in ("_app", "_document"):
                return "layout", "/"
            if stem.startswith("_"):
                return None
            segs = inner + ([] if stem == "index" else [stem])
            return "page", "/" + "/".join(segs)
    if tuple(parts[:2]) == ("src", "routes"):
        inner = parts[2:-1]
        if name.startswith("+page."):
            return "page", clean(inner)
        if name.startswith("+layout."):
            return "layout", clean(inner)
    return None


def _remix_route(stem: str) -> str:
    segs = []
    for s in stem.split("."):
        if s in ("_index", "index") or s.startswith("_"):
            continue
        segs.append(f"[{s[1:]}]" if s.startswith("$") else s.rstrip("_"))
    return "/" + "/".join(segs)


# --------------------------------------------------------------------------- import graph


def _strip_json_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"(^|[^:\"'])//[^\n]*", r"\1", text)
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _aliases(repo: Path) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    for cfg in ("tsconfig.json", "jsconfig.json"):
        f = repo / cfg
        if not f.exists():
            continue
        try:
            opts = json.loads(_strip_json_comments(f.read_text())).get("compilerOptions", {})
        except (ValueError, AttributeError):
            continue
        base = opts.get("baseUrl", ".")
        for alias, targets in (opts.get("paths") or {}).items():
            prefix = alias.rstrip("*")
            out.append((prefix, [str(PurePosixPath(base) / t.rstrip("*")) for t in targets]))
    return sorted(out, key=lambda a: -len(a[0]))


def _resolve_spec(spec: str, importer: str, files: set[str],
                  aliases: list[tuple[str, list[str]]]) -> str | None:
    bases: list[str] = []
    if spec.startswith("."):
        bases.append(str(PurePosixPath(importer).parent / spec))
    else:
        for prefix, targets in aliases:
            if spec.startswith(prefix):
                bases += [t.rstrip("/") + "/" + spec[len(prefix):] for t in targets]
                break
        else:
            if spec.startswith(("~/", "$lib/")):
                rest = spec.split("/", 1)[1]
                bases += [f"src/{rest}", f"src/lib/{rest}", rest]
            else:
                return None  # a package import
    for b in bases:
        b = _norm(b)
        for cand in (b, *(b + e for e in GRAPH_EXTS), *(f"{b}/index{e}" for e in JS_EXTS)):
            if cand in files:
                return cand
    return None


def _norm(p: str) -> str:
    out: list[str] = []
    for seg in PurePosixPath(p).parts:
        if seg == "..":
            if out:
                out.pop()
        elif seg != ".":
            out.append(seg)
    return "/".join(out)


def _repo_files(repo: Path) -> list[str]:
    try:
        res = subprocess.run(["git", "ls-files", "-z", "-co", "--exclude-standard"], cwd=repo,
                             capture_output=True, text=True, check=True)
        files = sorted({f for f in res.stdout.split("\0") if f})
    except (OSError, subprocess.CalledProcessError):
        files = [str(p.relative_to(repo)).replace("\\", "/") for p in repo.rglob("*")
                 if p.is_file()]
    return [f for f in files if not (set(PurePosixPath(f).parts) & SKIP_DIRS)]


def build_reverse_graph(repo: Path, files: list[str]) -> dict[str, set[str]]:
    """importee -> {importers}"""
    fileset = set(files)
    aliases = _aliases(repo)
    rev: dict[str, set[str]] = {}
    for f in files:
        if not f.endswith(GRAPH_EXTS):
            continue
        try:
            text = (repo / f).read_text(errors="ignore")
        except OSError:
            continue
        for rx in _IMPORT_RES:
            for spec in rx.findall(text):
                target = _resolve_spec(spec, f, fileset, aliases)
                if target and target != f:
                    rev.setdefault(target, set()).add(f)
    return rev


# --------------------------------------------------------------------------- python routes


def _python_routes(repo: Path, files: list[str]) -> tuple[dict[str, list[tuple[str, int, int]]],
                                                        dict[str, set[str]]]:
    """file -> [(route, def_start, def_end)], template name -> {routes}."""
    by_file: dict[str, list[tuple[str, int, int]]] = {}
    templates: dict[str, set[str]] = {}
    for f in files:
        if not f.endswith(".py"):
            continue
        try:
            lines = (repo / f).read_text(errors="ignore").splitlines()
        except OSError:
            continue
        pending: list[str] = []
        spans: list[tuple[str, int, int]] = []
        spans_start = 0
        i = 0
        while i < len(lines):
            m = _PY_ROUTE.match(lines[i])
            if m:
                if not pending:
                    spans_start = i + 1
                pending.append(_normalize_py_route(m.group(1)))
                i += 1
                continue
            if pending and _PY_DEF.match(lines[i]):
                indent = len(lines[i]) - len(lines[i].lstrip())
                j = i + 1
                while j < len(lines) and (not lines[j].strip()
                                          or len(lines[j]) - len(lines[j].lstrip()) > indent):
                    j += 1
                body = "\n".join(lines[i:j])
                for r in pending:
                    spans.append((r, spans_start, j))
                    for t in _PY_TEMPLATE.findall(body):
                        templates.setdefault(PurePosixPath(t).name, set()).add(r)
                pending = []
                i = j
                continue
            i += 1
        if spans:
            by_file[f] = spans
    return by_file, templates


# --------------------------------------------------------------------------- git


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          check=True).stdout


def changed_lines(repo: Path, base: str) -> tuple[list[str], dict[str, set[int]]]:
    """Files changed since the merge-base with ``base`` (incl. uncommitted), and new-side
    line numbers touched per file."""
    # Resolve a user-supplied ref before using it in commands accepting options. A ref such
    # as --output=... must never become a git option; subsequent commands receive only OIDs.
    if not base or base.startswith("-") or "\0" in base:
        raise ValueError("base must be a valid Git commit ref")
    oid = _git(repo, "rev-parse", "--verify", "--end-of-options", base + "^{commit}").strip()
    mb = _git(repo, "merge-base", oid, "HEAD").strip()
    names = [n for n in _git(repo, "diff", "--name-only", "-z", "--no-renames", mb, "--")
             .split("\0") if n]
    untracked = [n for n in _git(repo, "ls-files", "-z", "-o", "--exclude-standard")
                 .split("\0") if n]
    names = sorted(set(names) | set(untracked))
    lines: dict[str, set[int]] = {}
    current = None
    for row in _git(repo, "diff", "-U0", "--no-renames", mb, "--").splitlines():
        if row.startswith("+++ "):
            current = row[6:] if row.startswith("+++ b/") else None
        elif row.startswith("@@") and current:
            m = re.search(r"\+(\d+)(?:,(\d+))?", row)
            if m:
                start, count = int(m.group(1)), int(m.group(2) or 1)
                lines.setdefault(current, set()).update(range(start, start + max(count, 1)))
    for u in untracked:
        lines[u] = {-1}   # whole file
    return names, lines


# --------------------------------------------------------------------------- main entry


def compute(repo: str | Path, base: str = "main") -> BlastRadius:
    repo = Path(repo).resolve()
    changed, touched = changed_lines(repo, base)
    return analyze(repo, base, changed, touched)


def analyze(repo: Path, base: str, changed: list[str],
            touched: dict[str, set[int]] | None = None) -> BlastRadius:
    touched = touched or {}
    # Include deleted paths: deleted page files still identify the affected route and
    # existing importers can still link to a removed component.
    files = sorted(set(_repo_files(repo)) | set(changed))
    br = BlastRadius(base=base, changed=list(changed))

    route_files: dict[str, tuple[str, str]] = {}
    for f in files:
        r = route_for_file(f)
        if r:
            route_files[f] = r
    py_routes, templates = _python_routes(repo, files)
    br.all_routes = sorted({r for k, r in route_files.values() if k == "page"}
                           | {r for spans in py_routes.values() for r, _, _ in spans})

    hits: dict[str, RouteHit] = {}

    def add(route: str, file: str, via: list[str]) -> None:
        old = hits.get(route)
        if old is None or len(via) - 1 < old.distance:
            hits[route] = RouteHit(route=route, file=file, via=via, distance=len(via) - 1)

    rev = build_reverse_graph(repo, files)
    for c in changed:
        reached = False
        if GLOBAL_FILES.search(c):
            br.global_reasons.append(c)
            reached = True
        # Python: changed lines inside a decorated view function.
        for route, start, end in py_routes.get(c, []):
            lines = touched.get(c)
            if lines is None or -1 in lines or any(start <= n <= end for n in lines):
                add(route, c, [c])
                reached = True
        # Templates rendered by a route.
        for route in templates.get(PurePosixPath(c).name, ()) if c.endswith((".html", ".htm")) \
                else ():
            add(route, c, [c])
            reached = True
        # JS/CSS: BFS up the reverse import graph to route/layout files.
        queue: deque[list[str]] = deque([[c]])
        seen = {c}
        while queue:
            chain = queue.popleft()
            node = chain[-1]
            if node in route_files:
                kind, route = route_files[node]
                reached = True
                if kind == "page":
                    add(route, node, chain)
                else:
                    if route == "/":
                        br.global_reasons.append(f"{c} (via root layout {node})")
                    for f, (k2, r2) in route_files.items():
                        if k2 == "page" and (r2 + "/").startswith(route.rstrip("/") + "/"):
                            add(r2, f, chain + [f])
            for parent in sorted(rev.get(node, ())):
                if parent not in seen:
                    seen.add(parent)
                    queue.append(chain + [parent])
        if not reached and c.endswith(GRAPH_EXTS + (".py", ".html", ".htm")):
            br.unmapped.append(c)

    br.routes = sorted(hits.values(), key=lambda h: (h.distance, h.route))
    br.global_reasons = sorted(set(br.global_reasons))
    return br


def goal_hint(br: BlastRadius, limit: int = 8) -> str:
    """One sentence for an agent's goal. URLs only: no file names, no source."""
    if br.is_global or not br.routes:
        return ""
    routes = br.focus_routes(limit)
    return ("This change most likely affects these pages: " + ", ".join(routes)
            + ". Spend most of your steps on them (bracketed segments are IDs: open a real "
              "item to get there), but don't ignore what links into them.")
