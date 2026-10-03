import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as northstar


class NorthstarBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        self.previous = {
            key: os.environ.get(key)
            for key in (
                "DB_PATH",
                "OWNER_EMAIL",
                "OWNER_PASSWORD_HASH",
                "PAYPAL_CLIENT_ID",
                "PAYPAL_CLIENT_SECRET",
                "PAYPAL_WEBHOOK_ID",
                "PAYPAL_MODE",
                "PUBLIC_BASE_URL",
            )
        }
        os.environ["DB_PATH"] = str(self.db_path)
        os.environ["OWNER_EMAIL"] = "owner@example.com"
        os.environ["OWNER_PASSWORD_HASH"] = northstar.hash_password("a-unique-test-password")
        os.environ.pop("PAYPAL_CLIENT_ID", None)
        os.environ.pop("PAYPAL_CLIENT_SECRET", None)
        os.environ.pop("PAYPAL_WEBHOOK_ID", None)
        os.environ["PAYPAL_MODE"] = "sandbox"
        os.environ["PUBLIC_BASE_URL"] = "http://localhost"
        northstar.init_db()
        northstar.app.config.update(TESTING=True, SECRET_KEY="test-session-secret")
        self.client = northstar.app.test_client()
        self.origin = {"Origin": "http://localhost"}

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        northstar._login_failures.clear()
        northstar._checkout_attempts.clear()
        self.temp_dir.cleanup()

    def login(self):
        return self.client.post(
            "/api/admin/login",
            headers=self.origin,
            json={"email": "owner@example.com", "password": "a-unique-test-password"},
        )

    def webhook_headers(self):
        return {
            "Paypal-Auth-Algo": "SHA256withRSA",
            "Paypal-Cert-Url": "https://api.paypal.com/cert",
            "Paypal-Transmission-Id": "transmission-id",
            "Paypal-Transmission-Sig": "signature",
            "Paypal-Transmission-Time": "2026-01-01T00:00:00Z",
        }

    def test_public_pages_and_default_plans_are_served(self):
        home = self.client.get("/")
        admin = self.client.get("/admin")
        self.assertEqual(home.status_code, 200)
        self.assertEqual(admin.status_code, 200)
        home.close()
        admin.close()
        response = self.client.get("/api/plans")
        self.assertEqual(response.status_code, 200)
        plans = response.get_json()["plans"]
        self.assertEqual([(p["id"], p["price_cents"]) for p in plans], [("studio", 1200), ("collective", 2400)])
        self.assertFalse(any(plan["available"] for plan in plans))

    def test_owner_authentication_protects_admin_data_and_logout(self):
        unauthorized = self.client.get("/api/admin/overview")
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(self.client.get("/api/admin/session").get_json(), {"authenticated": False})
        self.assertEqual(self.login().status_code, 200)
        overview = self.client.get("/api/admin/overview")
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.get_json()["stats"]["total_subscriptions"], 0)
        logged_out = self.client.post("/api/admin/logout", headers=self.origin, json={})
        self.assertEqual(logged_out.status_code, 200)
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 401)

    def test_login_rejects_cross_origin_requests(self):
        response = self.client.post(
            "/api/admin/login",
            headers={"Origin": "https://attacker.example"},
            json={"email": "owner@example.com", "password": "a-unique-test-password"},
        )
        self.assertEqual(response.status_code, 403)

    def test_checkout_does_not_start_until_paypal_is_configured(self):
        response = self.client.post("/api/checkout", json={"plan_id": "studio"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("PAYPAL_CLIENT_SECRET", response.get_data(as_text=True))
        unknown = self.client.post("/api/checkout", json={"plan_id": "missing"})
        self.assertEqual(unknown.status_code, 404)

    def test_plan_price_is_not_saved_if_paypal_cannot_create_the_new_plan(self):
        self.assertEqual(self.login().status_code, 200)
        response = self.client.put(
            "/api/admin/plans/studio",
            headers=self.origin,
            json={
                "name": "Studio",
                "description": "New description.",
                "price_cents": 1500,
                "active": True,
            },
        )
        self.assertEqual(response.status_code, 502)
        with northstar.connect_db() as db:
            plan = db.execute("SELECT price_cents FROM plans WHERE id = 'studio'").fetchone()
        self.assertEqual(plan["price_cents"], 1200)

    def test_plan_price_change_publishes_a_new_provider_plan(self):
        self.assertEqual(self.login().status_code, 200)
        with patch.object(northstar, "get_or_create_paypal_product", return_value="P-PRODUCT"):
            with patch.object(northstar, "create_paypal_plan", return_value="P-STUDIO-NEW") as create_plan:
                response = self.client.put(
                    "/api/admin/plans/studio",
                    headers=self.origin,
                    json={
                        "name": "Studio",
                        "description": "A refreshed Studio plan.",
                        "price_cents": 1500,
                        "active": True,
                    },
                )
        self.assertEqual(response.status_code, 200)
        create_plan.assert_called_once()
        with northstar.connect_db() as db:
            plan = db.execute(
                "SELECT price_cents, paypal_plan_id FROM plans WHERE id = 'studio'"
            ).fetchone()
        self.assertEqual((plan["price_cents"], plan["paypal_plan_id"]), (1500, "P-STUDIO-NEW"))

    def test_owner_can_edit_site_settings_and_invalid_email_is_rejected(self):
        self.assertEqual(self.login().status_code, 200)
        response = self.client.put(
            "/api/admin/site-settings",
            headers=self.origin,
            json={"brand_name": "Busaka Studio"},
        )
        self.assertEqual(response.status_code, 200)
        public_settings = self.client.get("/api/site-settings").get_json()["settings"]
        self.assertEqual(public_settings["brand_name"], "Busaka Studio")

        invalid_email = self.client.put(
            "/api/admin/site-settings",
            headers=self.origin,
            json={"contact_email": "not-an-email"},
        )
        self.assertEqual(invalid_email.status_code, 400)

    def test_hidden_plan_with_changed_price_cannot_reactivate_with_old_paypal_price(self):
        self.assertEqual(self.login().status_code, 200)
        hidden = self.client.put(
            "/api/admin/plans/studio",
            headers=self.origin,
            json={
                "name": "Studio",
                "description": "Updated description.",
                "price_cents": 1500,
                "active": False,
            },
        )
        self.assertEqual(hidden.status_code, 200)
        with northstar.connect_db() as db:
            plan = db.execute("SELECT price_cents, active, paypal_plan_id FROM plans WHERE id = 'studio'").fetchone()
        self.assertEqual((plan["price_cents"], plan["active"], plan["paypal_plan_id"]), (1500, 0, None))

        reactivated = self.client.put(
            "/api/admin/plans/studio",
            headers=self.origin,
            json={
                "name": "Studio",
                "description": "Updated description.",
                "price_cents": 1500,
                "active": True,
            },
        )
        self.assertEqual(reactivated.status_code, 502)
        with northstar.connect_db() as db:
            plan = db.execute("SELECT active FROM plans WHERE id = 'studio'").fetchone()
        self.assertEqual(plan["active"], 0)

    def test_checkout_creates_a_paypal_subscription_and_pending_record(self):
        with northstar.connect_db() as db:
            db.execute("UPDATE plans SET paypal_plan_id = 'P-STUDIO' WHERE id = 'studio'")
        os.environ["PAYPAL_CLIENT_ID"] = "test-client"
        os.environ["PAYPAL_CLIENT_SECRET"] = "test-secret"
        with patch.object(
            northstar,
            "paypal_api",
            return_value={
                "id": "I-SUBSCRIPTION",
                "links": [{"rel": "approve", "href": "https://www.sandbox.paypal.com/checkoutnow?token=I-SUBSCRIPTION"}],
            },
        ) as provider:
            response = self.client.post("/api/checkout", json={"plan_id": "studio"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["approval_url"].startswith("https://www.sandbox.paypal.com/"))
        self.assertEqual(provider.call_args.args[1], "/v1/billing/subscriptions")
        with northstar.connect_db() as db:
            row = db.execute(
                "SELECT status, plan_id FROM subscriptions WHERE paypal_subscription_id = 'I-SUBSCRIPTION'"
            ).fetchone()
        self.assertEqual((row["status"], row["plan_id"]), ("APPROVAL_PENDING", "studio"))

    def test_webhook_signature_is_verified_and_events_are_idempotent(self):
        os.environ["PAYPAL_WEBHOOK_ID"] = "WH-OWNER"
        event = {
            "id": "WH-EVENT-1",
            "event_type": "BILLING.SUBSCRIPTION.ACTIVATED",
            "resource": {
                "id": "I-SUBSCRIPTION",
                "plan_id": "P-STUDIO",
                "subscriber": {"email_address": "customer@example.com"},
            },
        }
        with northstar.connect_db() as db:
            db.execute("UPDATE plans SET paypal_plan_id = 'P-STUDIO' WHERE id = 'studio'")
        with patch.object(northstar, "paypal_api", return_value={"verification_status": "SUCCESS"}) as verify:
            response = self.client.post(
                "/api/webhooks/paypal",
                data=json.dumps(event),
                content_type="application/json",
                headers=self.webhook_headers(),
            )
            duplicate = self.client.post(
                "/api/webhooks/paypal",
                data=json.dumps(event),
                content_type="application/json",
                headers=self.webhook_headers(),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(duplicate.get_json()["duplicate"], True)
        self.assertEqual(verify.call_count, 2)
        with northstar.connect_db() as db:
            row = db.execute(
                "SELECT status, plan_id, subscriber_email FROM subscriptions WHERE paypal_subscription_id = 'I-SUBSCRIPTION'"
            ).fetchone()
        self.assertEqual(
            (row["status"], row["plan_id"], row["subscriber_email"]),
            ("ACTIVE", "studio", "customer@example.com"),
        )

    def test_unverified_webhook_cannot_change_subscription(self):
        os.environ["PAYPAL_WEBHOOK_ID"] = "WH-OWNER"
        with northstar.connect_db() as db:
            db.execute("UPDATE plans SET paypal_plan_id = 'P-STUDIO' WHERE id = 'studio'")
        event = {
            "id": "WH-EVENT-2",
            "event_type": "BILLING.SUBSCRIPTION.ACTIVATED",
            "resource": {"id": "I-SUBSCRIPTION", "plan_id": "P-STUDIO"},
        }
        with patch.object(northstar, "paypal_api", return_value={"verification_status": "FAILURE"}):
            response = self.client.post(
                "/api/webhooks/paypal",
                data=json.dumps(event),
                content_type="application/json",
                headers=self.webhook_headers(),
            )
        self.assertEqual(response.status_code, 400)
        with northstar.connect_db() as db:
            row = db.execute(
                "SELECT 1 FROM subscriptions WHERE paypal_subscription_id = 'I-SUBSCRIPTION'"
            ).fetchone()
        self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
