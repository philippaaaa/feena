"""Local single-workspace results dashboard with authenticated evidence access."""
import argparse
import json
import os
import re
import secrets
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

from .config import load_config

RUN_ID = re.compile(r"^[a-f0-9]{32}$")


def create_app(config_path: Path, token: str, billing=None):
    if len(token) < 32:
        raise ValueError("Dashboard access key must contain at least 32 characters")
    cfg = load_config(config_path)
    root = (cfg.out_path / "suite-runs").resolve()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 65536

    @app.before_request
    def authenticate():
        if request.path in ("/", "/billing/webhook"):
            return
        if not secrets.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token):
            abort(401)

    @app.after_request
    def headers(response):
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'"})
        return response

    def directory(run_id):
        if not RUN_ID.fullmatch(run_id):
            abort(404)
        path = (root / run_id).resolve()
        if path.parent != root or not path.is_dir():
            abort(404)
        return path

    def summary(run_id):
        folder = directory(run_id)
        path = (folder / "summary.json").resolve()
        if not path.is_relative_to(folder) or path.stat().st_size > 1_000_000:
            abort(404)
        data = json.loads(path.read_text())
        # Only expose evidence under this run, and never disclose arbitrary local paths.
        for row in data.get("results", []):
            row["evidence"] = []
            raw = row.pop("artifacts", None)
            if not raw:
                continue
            artifact_dir = Path(raw).resolve()
            if not artifact_dir.is_relative_to(folder):
                continue
            for item in sorted(artifact_dir.iterdir()):
                allowed = (item.name in ("trace.zip", "actions.json", "manifest.json") or
                           re.fullmatch(r"final(?:-[a-zA-Z0-9_-]+)?\.png", item.name))
                if allowed and item.resolve().is_relative_to(folder) and item.is_file():
                    row["evidence"].append({"name": item.name,
                        "path": item.relative_to(folder).as_posix()})
        data["run_id"] = run_id
        return data

    @app.get("/")
    def index():
        return send_file(Path(__file__).with_name("dashboard.html"))

    @app.get("/api/project")
    def project():
        return jsonify({"suites": [s.model_dump() for s in cfg.suites],
                        "scenarios": [{"name": s.name, "goal": s.goal} for s in cfg.scenarios],
                        "billing": billing.status() if billing else {"enabled": False}})

    @app.get("/api/runs")
    def runs():
        rows = []
        if root.exists():
            folders = sorted((p for p in root.iterdir() if RUN_ID.fullmatch(p.name)),
                             key=lambda p: p.name)
            for folder in folders:
                try:
                    data = summary(folder.name)
                except (OSError, ValueError, TypeError, KeyError):
                    continue
                rows.append({k: data.get(k) for k in ("run_id", "created_at", "suite", "status", "counts")})
        rows.sort(key=lambda r: str(r["created_at"]), reverse=True)
        return jsonify(rows[:100])

    @app.get("/api/runs/<run_id>")
    def run(run_id):
        try:
            return jsonify(summary(run_id))
        except (OSError, ValueError, TypeError, KeyError):
            abort(404)

    @app.get("/api/runs/<run_id>/evidence/<path:filename>")
    def evidence(run_id, filename):
        try:
            data = summary(run_id)
        except (OSError, ValueError, TypeError, KeyError):
            abort(404)
        allowed = {item["path"] for row in data["results"] for item in row["evidence"]}
        if filename not in allowed:
            abort(404)
        folder = directory(run_id)
        path = (folder / filename).resolve()
        if not path.is_relative_to(folder):
            abort(404)
        return send_file(path, as_attachment=path.suffix != ".png")

    @app.post("/api/billing/<action>")
    def billing_action(action):
        if not billing or action not in ("checkout", "portal"):
            abort(404)
        try:
            return jsonify({"url": getattr(billing, action)()})
        except ValueError:
            return jsonify({"error": "Billing action unavailable for this workspace"}), 409
        except Exception:  # noqa: BLE001 - keep provider details out of API responses
            return jsonify({"error": "Stripe could not complete the request"}), 502

    @app.post("/billing/webhook")
    def webhook():
        if billing is None:
            abort(404)
        try:
            billing.receive(request.get_data(), request.headers.get("Stripe-Signature", ""))
        except ValueError:
            abort(400)
        except Exception as exc:  # noqa: BLE001 - signature errors vs transient provider failures
            import stripe
            if isinstance(exc, stripe.SignatureVerificationError):
                abort(400)
            abort(503)
        return jsonify({"received": True})

    return app


def main():
    parser = argparse.ArgumentParser(description="Feena local results dashboard")
    parser.add_argument("--config", type=Path, default=Path("feena.yaml"))
    parser.add_argument("--port", type=int, default=5056)
    args = parser.parse_args()
    token = os.environ.get("FEENA_DASHBOARD_TOKEN", "")
    billing = None
    if os.environ.get("FEENA_STRIPE_TEST_KEY"):
        from .billing import TestBilling
        cfg = load_config(args.config)
        billing = TestBilling(cfg.out_path / "billing.sqlite3", os.environ["FEENA_STRIPE_TEST_KEY"],
                              os.environ.get("FEENA_STRIPE_WEBHOOK_SECRET", ""),
                              os.environ.get("FEENA_STRIPE_PRICE", ""),
                              os.environ.get("FEENA_DASHBOARD_URL", f"http://127.0.0.1:{args.port}"))
    create_app(args.config, token, billing).run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
