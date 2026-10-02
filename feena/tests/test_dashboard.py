import json

import pytest

from feena.dashboard import create_app

TOKEN = "a" * 32
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def dashboard(tmp_path):
    config = tmp_path / "feena.yaml"
    config.write_text("target: {compose: '', service: web, port: 3000}\n")
    app = create_app(config, TOKEN)
    return app.test_client(), tmp_path / ".feena" / "suite-runs"


def save_run(root, external=None):
    folder = root / ("a" * 32)
    evidence = folder / "evidence" / "checkout"
    evidence.mkdir(parents=True)
    (evidence / "final.png").write_bytes(b"test-image")
    (evidence / "secret.txt").write_text("never expose")
    (folder / "summary.json").write_text(json.dumps({"version": 1, "status": "passed",
        "created_at": "2026-10-02T12:00:00Z", "suite": "release", "counts": {"passed": 1},
        "results": [{"name": "checkout", "profile": "normal", "status": "passed",
                     "artifacts": str(external or evidence)}]}))
    return folder, evidence


def test_private_endpoints_require_authentication(dashboard):
    client, _ = dashboard
    assert client.get("/").status_code == 200
    for path in ("/api/project", "/api/runs", "/api/runs/" + "a" * 32):
        assert client.get(path).status_code == 401
    assert client.post("/api/billing/checkout").status_code == 401
    assert client.get("/api/project", headers=HEADERS).status_code == 200
    assert client.get("/api/runs", headers=HEADERS).headers["Cache-Control"] == "no-store"


def test_run_details_and_evidence_allowlist(dashboard):
    client, root = dashboard
    save_run(root)
    rows = client.get("/api/runs", headers=HEADERS).json
    assert len(rows) == 1
    detail = client.get("/api/runs/" + "a" * 32, headers=HEADERS).json
    assert "artifacts" not in detail["results"][0]
    assert detail["results"][0]["evidence"] == [
        {"name": "final.png", "path": "evidence/checkout/final.png"}]
    prefix = "/api/runs/" + "a" * 32 + "/evidence/"
    assert client.get(prefix + "evidence/checkout/final.png", headers=HEADERS).data == b"test-image"
    assert client.get(prefix + "evidence/checkout/secret.txt", headers=HEADERS).status_code == 404
    assert client.get(prefix + "../../config.yaml", headers=HEADERS).status_code == 404


def test_external_and_symlink_evidence_rejected(dashboard, tmp_path):
    client, root = dashboard
    external = tmp_path / "outside"
    external.mkdir()
    (external / "final.png").write_bytes(b"private")
    folder, evidence = save_run(root)
    (evidence / "final.png").unlink()
    (evidence / "final.png").symlink_to(external / "final.png")
    detail = client.get("/api/runs/" + folder.name, headers=HEADERS).json
    assert detail["results"][0]["evidence"] == []
    assert client.post("/billing/webhook").status_code == 404


def test_short_access_key_rejected(tmp_path):
    with pytest.raises(ValueError):
        create_app(tmp_path / "unused", "short")
