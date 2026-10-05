import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest

stripe = pytest.importorskip("stripe")

from feena.billing import TestBilling as Billing

SECRET = "whsec_test_secret"


def signed_event(event_id="evt_1", live=False, timestamp=None):
    timestamp = int(time.time()) if timestamp is None else timestamp
    raw = json.dumps({"id": event_id, "type": "customer.subscription.updated",
                      "livemode": live, "data": {"object": {"id": "sub_1"}}}).encode()
    signature = hmac.new(SECRET.encode(), str(timestamp).encode() + b"." + raw,
                         hashlib.sha256).hexdigest()
    return raw, f"t={timestamp},v1={signature}"


def billing(tmp_path):
    return Billing(tmp_path / "billing.sqlite3", "sk_test_placeholder", SECRET,
                   "price_test", "http://localhost:5056")


def test_reject_live_keys_and_invalid_redirects(tmp_path):
    with pytest.raises(ValueError):
        Billing(tmp_path / "b", "sk_live_x", SECRET, "price_x", "http://localhost")
    with pytest.raises(ValueError):
        Billing(tmp_path / "b", "sk_test_x", SECRET, "price_x", "http://external.test")


def test_signed_webhook_idempotency_and_current_state(tmp_path):
    b = billing(tmp_path)
    calls = []
    obj = SimpleNamespace(id="sub_1", customer="cus_1", status="active", livemode=False,
                          metadata={"feena_workspace": "local"})
    def retrieve(subscription):
        calls.append(subscription)
        return obj
    b.client = SimpleNamespace(v1=SimpleNamespace(subscriptions=SimpleNamespace(retrieve=retrieve)))
    raw, signature = signed_event()
    b.receive(raw, signature)
    b.receive(raw, signature)
    assert calls == ["sub_1"]
    assert b.status()["statuses"] == ["active"]
    obj.status = "canceled"
    b.receive(*signed_event("evt_older_delivery"))
    assert b.status()["statuses"] == ["canceled"]
    assert b.status()["enforcement"] is False


def test_forged_expired_and_live_webhooks_cannot_update_state(tmp_path):
    b = billing(tmp_path)
    raw, signature = signed_event()
    with pytest.raises(stripe.SignatureVerificationError):
        b.receive(raw + b" ", signature)
    with pytest.raises(stripe.SignatureVerificationError):
        b.receive(*signed_event(timestamp=int(time.time()) - 1000))
    with pytest.raises(ValueError):
        b.receive(*signed_event(live=True))
    assert b.status()["statuses"] == []


def test_checkout_uses_server_price_and_subscription_metadata(tmp_path):
    b = billing(tmp_path)
    calls = []
    def create(params):
        calls.append(params)
        return SimpleNamespace(url="https://checkout.stripe.com/test")
    b.client = SimpleNamespace(v1=SimpleNamespace(checkout=SimpleNamespace(
        sessions=SimpleNamespace(create=create))))
    assert b.checkout() == "https://checkout.stripe.com/test"
    assert calls[0]["subscription_data"]["metadata"] == {"feena_workspace": "local"}
    assert calls[0]["line_items"][0]["price"] == "price_test"
