"""Stripe test-mode plumbing; subscription state is not yet a production paywall."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit


class TestBilling:
    def __init__(self, store: Path, key: str, webhook_secret: str, price: str,
                 public_url: str, workspace: str = "local"):
        import stripe

        if not key.startswith("sk_test_") or not webhook_secret.startswith("whsec_"):
            raise ValueError("Billing requires a Stripe test key and webhook signing secret")
        parts = urlsplit(public_url)
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment or parts.path not in ("", "/")):
            raise ValueError("Configure an origin-only billing return URL")
        if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1"):
            raise ValueError("Non-local billing return URLs require HTTPS")
        if not price.startswith("price_"):
            raise ValueError("Configure a Stripe recurring price ID")
        self.client = stripe.StripeClient(key)
        self.secret, self.price = webhook_secret, price
        self.origin, self.workspace = public_url.rstrip("/"), workspace
        self.store = store
        store.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS subscription (
                    id TEXT PRIMARY KEY, customer TEXT, status TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.store, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def checkout(self):
        params = {"mode": "subscription", "line_items": [{"price": self.price, "quantity": 1}],
                  "client_reference_id": self.workspace,
                  "subscription_data": {"metadata": {"feena_workspace": self.workspace}},
                  "success_url": self.origin + "/?billing=returned",
                  "cancel_url": self.origin + "/?billing=cancelled"}
        with self.connect() as db:
            customer = db.execute("SELECT customer FROM subscription LIMIT 1").fetchone()
        if customer and customer[0]:
            params["customer"] = customer[0]
        return self.client.v1.checkout.sessions.create(params).url

    def portal(self):
        with self.connect() as db:
            row = db.execute("SELECT customer FROM subscription LIMIT 1").fetchone()
        if not row or not row[0]:
            raise ValueError("No verified subscription customer yet")
        return self.client.v1.billing_portal.sessions.create(
            {"customer": row[0], "return_url": self.origin}).url

    def receive(self, raw: bytes, signature: str):
        import stripe

        event = stripe.Webhook.construct_event(raw, signature, self.secret, tolerance=300)
        if event.livemode is not False:
            raise ValueError("Live billing events are disabled")
        if event["type"] not in ("customer.subscription.created", "customer.subscription.updated",
                                 "customer.subscription.deleted"):
            return
        with self.connect() as db:
            if db.execute("SELECT 1 FROM events WHERE id=?", (event["id"],)).fetchone():
                return
            # Fetch current Stripe state under the write lock rather than trusting delivery order.
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM events WHERE id=?", (event["id"],)).fetchone():
                return
            obj = self.client.v1.subscriptions.retrieve(event["data"]["object"]["id"])
            if ("feena_workspace" not in obj.metadata or
                    obj.metadata["feena_workspace"] != self.workspace):
                return
            if obj.livemode:
                raise ValueError("Live subscriptions are disabled")
            db.execute("INSERT OR REPLACE INTO subscription VALUES (?,?,?)",
                       (obj.id, obj.customer, obj.status))
            db.execute("INSERT INTO events VALUES (?)", (event["id"],))

    def status(self):
        with self.connect() as db:
            rows = db.execute("SELECT status FROM subscription").fetchall()
        return {"enabled": True, "mode": "test", "statuses": [r[0] for r in rows],
                "enforcement": False}
