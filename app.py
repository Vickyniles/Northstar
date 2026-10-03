from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import time
from email.utils import parseaddr
from datetime import timedelta
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, request, send_from_directory, session
from waitress import serve


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("northstar")

DEFAULT_PLANS = (
    {
        "id": "studio",
        "name": "Studio",
        "description": "For small teams doing their best work together.",
        "price_cents": 1200,
    },
    {
        "id": "collective",
        "name": "Collective",
        "description": "More room and support for growing teams.",
        "price_cents": 2400,
    },
)

DEFAULT_SITE_SETTINGS = {
    "brand_name": "Northstar",
    "contact_email": "hello@northstar.studio",
    "hero_title": "Thoughtful digital, built to move you forward.",
    "hero_copy": "We partner with ambitious people to make useful software, memorable brands, and a little more room for the work that matters.",
    "contact_heading": "Have a good one in mind?",
    "contact_copy": "Tell us what you’re working on. We’d love to hear about it—and help you figure out what comes next.",
}

PAYPAL_SCOPES = {
    "BILLING.SUBSCRIPTION.CREATED": "APPROVAL_PENDING",
    "BILLING.SUBSCRIPTION.ACTIVATED": "ACTIVE",
    "BILLING.SUBSCRIPTION.UPDATED": "ACTIVE",
    "BILLING.SUBSCRIPTION.CANCELLED": "CANCELLED",
    "BILLING.SUBSCRIPTION.SUSPENDED": "SUSPENDED",
    "BILLING.SUBSCRIPTION.EXPIRED": "EXPIRED",
}

app = Flask(__name__, static_folder=None)
app.config.update(
    SECRET_KEY=os.getenv("APP_SECRET_KEY") or secrets.token_urlsafe(48),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.getenv("APP_ENV", "development").lower() == "production",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=32 * 1024,
)

_paypal_token: str | None = None
_paypal_token_expires_at = 0.0
_login_failures: dict[str, list[float]] = {}
_checkout_attempts: dict[str, list[float]] = {}


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def database_path() -> Path:
    configured = Path(os.getenv("DB_PATH", str(BASE_DIR / "northstar.sqlite3")))
    return configured if configured.is_absolute() else BASE_DIR / configured


def connect_db() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db() -> None:
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS plans (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                price_cents INTEGER NOT NULL CHECK(price_cents > 0),
                currency TEXT NOT NULL DEFAULT 'USD',
                paypal_plan_id TEXT,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS site_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                paypal_subscription_id TEXT PRIMARY KEY,
                plan_id TEXT,
                paypal_plan_id TEXT,
                subscriber_email TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_payment_at TEXT
            );
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paypal_event_id TEXT NOT NULL UNIQUE,
                paypal_payment_id TEXT,
                paypal_subscription_id TEXT,
                subscriber_email TEXT,
                amount TEXT,
                currency TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS webhook_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        for plan in DEFAULT_PLANS:
            db.execute(
                """
                INSERT OR IGNORE INTO plans (id, name, description, price_cents)
                VALUES (?, ?, ?, ?)
                """,
                (plan["id"], plan["name"], plan["description"], plan["price_cents"]),
            )
        for key, value in DEFAULT_SITE_SETTINGS.items():
            db.execute(
                "INSERT OR IGNORE INTO site_settings (key, value) VALUES (?, ?)",
                (key, value),
            )


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return f"scrypt${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, salt_hex, expected_hex = stored.split("$", 2)
        if algorithm != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=2**14,
            r=8,
            p=1,
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("owner_email") != os.getenv("OWNER_EMAIL", "").strip().lower():
            return jsonify(error="Owner sign-in required."), 401
        return view(*args, **kwargs)

    return wrapped


def require_same_origin():
    origin = request.headers.get("Origin")
    if not origin:
        return False
    return urlsplit(origin).netloc.lower() == urlsplit(request.host_url).netloc.lower()


def origin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not require_same_origin():
            return jsonify(error="This request must come from this website."), 403
        return view(*args, **kwargs)

    return wrapped


def paypal_base_url() -> str:
    mode = os.getenv("PAYPAL_MODE", "sandbox").strip().lower()
    if mode not in {"sandbox", "live"}:
        raise ValueError("PAYPAL_MODE must be either sandbox or live.")
    return "https://api-m.paypal.com" if mode == "live" else "https://api-m.sandbox.paypal.com"


def paypal_credentials() -> tuple[str, str]:
    client_id = os.getenv("PAYPAL_CLIENT_ID", "").strip()
    client_secret = os.getenv("PAYPAL_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("PayPal API credentials are not configured.")
    return client_id, client_secret


def paypal_access_token() -> str:
    global _paypal_token, _paypal_token_expires_at
    if _paypal_token and time.time() < _paypal_token_expires_at:
        return _paypal_token

    client_id, client_secret = paypal_credentials()
    credentials = f"{client_id}:{client_secret}".encode("utf-8")
    request_data = Request(
        f"{paypal_base_url()}/v1/oauth2/token",
        data=b"grant_type=client_credentials",
        headers={
            "Authorization": f"Basic {base64.b64encode(credentials).decode('ascii')}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    response = paypal_http(request_data)
    _paypal_token = response["access_token"]
    _paypal_token_expires_at = time.time() + max(int(response.get("expires_in", 300)) - 60, 30)
    return _paypal_token


def paypal_http(outgoing: Request) -> dict[str, Any]:
    try:
        with urlopen(outgoing, timeout=20) as response:
            body = response.read()
    except HTTPError as error:
        detail = error.read(2048).decode("utf-8", errors="replace")
        logger.warning("PayPal request failed with HTTP %s: %s", error.code, detail[:500])
        raise RuntimeError("PayPal could not complete the request. Check the server logs.") from error
    except (URLError, TimeoutError) as error:
        logger.warning("PayPal connection failed: %s", error)
        raise RuntimeError("PayPal is temporarily unreachable. Please try again.") from error
    try:
        return json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("PayPal returned an invalid response.") from error


def paypal_api(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {paypal_access_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    outgoing = Request(f"{paypal_base_url()}{path}", data=body, headers=headers, method=method)
    return paypal_http(outgoing)


def get_or_create_paypal_product() -> str:
    with connect_db() as db:
        row = db.execute(
            "SELECT value FROM site_settings WHERE key = 'paypal_product_id'"
        ).fetchone()
    if row:
        return row["value"]
    product = paypal_api(
        "POST",
        "/v1/catalogs/products",
        {
            "name": "Northstar Subscriptions",
            "description": "Recurring subscriptions for the Northstar service.",
            "type": "SERVICE",
        },
    )
    product_id = product.get("id")
    if not product_id:
        raise RuntimeError("PayPal did not return a product ID.")
    with connect_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO site_settings (key, value) VALUES ('paypal_product_id', ?)",
            (product_id,),
        )
    return product_id


def create_paypal_plan(product_id: str, plan: sqlite3.Row | dict[str, Any]) -> str:
    amount = f"{int(plan['price_cents']) / 100:.2f}"
    result = paypal_api(
        "POST",
        "/v1/billing/plans",
        {
            "product_id": product_id,
            "name": plan["name"][:127],
            "description": plan["description"][:127],
            "status": "ACTIVE",
            "billing_cycles": [
                {
                    "frequency": {"interval_unit": "MONTH", "interval_count": 1},
                    "tenure_type": "REGULAR",
                    "sequence": 1,
                    "total_cycles": 0,
                    "pricing_scheme": {
                        "fixed_price": {"value": amount, "currency_code": "USD"}
                    },
                }
            ],
            "payment_preferences": {
                "auto_bill_outstanding": True,
                "setup_fee_failure_action": "CANCEL",
                "payment_failure_threshold": 3,
            },
        },
    )
    plan_id = result.get("id")
    if not plan_id:
        raise RuntimeError("PayPal did not return a billing plan ID.")
    return plan_id


def public_plan(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "price_cents": row["price_cents"],
        "currency": row["currency"],
        "active": bool(row["active"]),
        "available": bool(row["active"] and row["paypal_plan_id"]),
    }


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'",
    )
    if request.path.startswith("/api/admin") or request.path in {
        "/",
        "/admin",
        "/admin/",
        "/app.js",
        "/admin.js",
    }:
        response.headers["Cache-Control"] = "no-store"
    if os.getenv("APP_ENV", "development").lower() == "production":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.get("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/admin")
@app.get("/admin/")
def admin_page():
    return send_from_directory(BASE_DIR, "admin.html")


@app.get("/app.js")
def frontend_script():
    return send_from_directory(BASE_DIR, "app.js", mimetype="text/javascript")


@app.get("/admin.js")
def admin_script():
    return send_from_directory(BASE_DIR, "admin.js", mimetype="text/javascript")


@app.get("/api/plans")
def list_public_plans():
    with connect_db() as db:
        rows = db.execute(
            "SELECT * FROM plans WHERE active = 1 ORDER BY CASE id WHEN 'studio' THEN 1 WHEN 'collective' THEN 2 ELSE 3 END"
        ).fetchall()
    return jsonify(plans=[public_plan(row) for row in rows])


@app.get("/api/site-settings")
def get_public_site_settings():
    with connect_db() as db:
        rows = db.execute(
            "SELECT key, value FROM site_settings WHERE key IN (?, ?, ?, ?, ?, ?)",
            tuple(DEFAULT_SITE_SETTINGS),
        ).fetchall()
    settings = {row["key"]: row["value"] for row in rows}
    return jsonify(settings=settings)


@app.post("/api/admin/login")
@origin_required
def owner_login():
    client = request.remote_addr or "unknown"
    now = time.time()
    attempts = [stamp for stamp in _login_failures.get(client, []) if now - stamp < 900]
    _login_failures[client] = attempts
    if len(attempts) >= 5:
        return jsonify(error="Too many sign-in attempts. Try again in 15 minutes."), 429

    owner_email = os.getenv("OWNER_EMAIL", "").strip().lower()
    password_hash = os.getenv("OWNER_PASSWORD_HASH", "").strip()
    if not owner_email or not password_hash:
        return jsonify(error="Owner account is not configured on the server."), 503
    payload = request.get_json(silent=True) or {}
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    if len(email) > 254 or len(password) > 1024:
        return jsonify(error="Invalid email or password."), 400
    valid_email = hmac.compare_digest(email, owner_email)
    valid_password = verify_password(password, password_hash)
    if not valid_email or not valid_password:
        attempts.append(now)
        _login_failures[client] = attempts
        if len(_login_failures) > 10000:
            _login_failures.clear()
            _login_failures[client] = attempts
        return jsonify(error="Email or password is incorrect."), 401

    _login_failures.pop(client, None)
    session.clear()
    session.permanent = True
    session["owner_email"] = owner_email
    return jsonify(email=owner_email)


@app.post("/api/admin/logout")
@origin_required
@login_required
def owner_logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/admin/session")
def owner_session():
    owner_email = os.getenv("OWNER_EMAIL", "").strip().lower()
    if session.get("owner_email") != owner_email or not owner_email:
        session.clear()
        return jsonify(authenticated=False)
    return jsonify(authenticated=True, email=session["owner_email"])


@app.get("/api/admin/overview")
@login_required
def admin_overview():
    with connect_db() as db:
        plans = db.execute("SELECT * FROM plans ORDER BY id").fetchall()
        subscriptions = db.execute(
            """
            SELECT s.*, p.name AS plan_name
            FROM subscriptions s LEFT JOIN plans p ON p.id = s.plan_id
            ORDER BY s.updated_at DESC LIMIT 200
            """
        ).fetchall()
        payments = db.execute(
            "SELECT * FROM payments ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
        counts = db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM subscriptions WHERE status = 'ACTIVE') AS active_subscriptions,
                (SELECT COUNT(*) FROM subscriptions) AS total_subscriptions,
                (SELECT COALESCE(SUM(CAST(amount AS REAL)), 0) FROM payments WHERE status = 'COMPLETED') AS total_paid
            """
        ).fetchone()
        settings = {
            row["key"]: row["value"]
            for row in db.execute(
                "SELECT key, value FROM site_settings WHERE key IN (?, ?, ?, ?, ?, ?)",
                tuple(DEFAULT_SITE_SETTINGS),
            ).fetchall()
        }
    return jsonify(
        stats={
            "active_subscriptions": counts["active_subscriptions"],
            "total_subscriptions": counts["total_subscriptions"],
            "total_paid": round(float(counts["total_paid"]), 2),
        },
        plans=[
            {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                "price_cents": row["price_cents"],
                "currency": row["currency"],
                "active": bool(row["active"]),
                "paypal_configured": bool(row["paypal_plan_id"]),
            }
            for row in plans
        ],
        subscriptions=[dict(row) for row in subscriptions],
        payments=[dict(row) for row in payments],
        settings=settings,
        paypal_mode=os.getenv("PAYPAL_MODE", "sandbox").strip().lower(),
        paypal_ready=bool(
            os.getenv("PAYPAL_CLIENT_ID", "").strip()
            and os.getenv("PAYPAL_CLIENT_SECRET", "").strip()
            and os.getenv("PAYPAL_WEBHOOK_ID", "").strip()
        ),
    )


@app.post("/api/admin/paypal/setup")
@origin_required
@login_required
def setup_paypal_plans():
    try:
        product_id = get_or_create_paypal_product()
        with connect_db() as db:
            plans = db.execute("SELECT * FROM plans WHERE active = 1").fetchall()
        configured = []
        for plan in plans:
            if plan["paypal_plan_id"]:
                continue
            provider_plan_id = create_paypal_plan(product_id, plan)
            with connect_db() as db:
                db.execute(
                    "UPDATE plans SET paypal_plan_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (provider_plan_id, plan["id"]),
                )
            configured.append(plan["id"])
        return jsonify(configured=configured, product_id=product_id)
    except (RuntimeError, ValueError) as error:
        return jsonify(error=str(error)), 502


@app.put("/api/admin/plans/<plan_id>")
@origin_required
@login_required
def update_plan(plan_id: str):
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    description = str(payload.get("description", "")).strip()
    active = payload.get("active")
    try:
        price_cents = int(payload.get("price_cents"))
    except (TypeError, ValueError):
        return jsonify(error="Price must be an amount in cents."), 400
    if not name or len(name) > 80 or not description or len(description) > 500:
        return jsonify(error="Plan name and description are required (maximum 80 and 500 characters)."), 400
    if price_cents < 100 or price_cents > 999999 or not isinstance(active, bool):
        return jsonify(error="Plan price must be between $1.00 and $9,999.99, and active must be true or false."), 400

    with connect_db() as db:
        current = db.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if not current:
        return jsonify(error="Plan not found."), 404

    changed = (
        current["name"] != name
        or current["description"] != description
        or current["price_cents"] != price_cents
    )
    provider_plan_id = current["paypal_plan_id"]
    if active and (changed or not provider_plan_id):
        try:
            product_id = get_or_create_paypal_product()
            provider_plan_id = create_paypal_plan(
                product_id,
                {
                    "name": name,
                    "description": description,
                    "price_cents": price_cents,
                },
            )
        except (RuntimeError, ValueError) as error:
            return jsonify(error=str(error)), 502
    elif changed:
        provider_plan_id = None

    with connect_db() as db:
        db.execute(
            """
            UPDATE plans
            SET name = ?, description = ?, price_cents = ?, active = ?,
                paypal_plan_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (name, description, price_cents, int(active), provider_plan_id, plan_id),
        )
    return jsonify(ok=True)


@app.put("/api/admin/site-settings")
@origin_required
@login_required
def update_site_settings():
    payload = request.get_json(silent=True) or {}
    unknown = set(payload) - set(DEFAULT_SITE_SETTINGS)
    if unknown:
        return jsonify(error="One or more site setting fields are not editable."), 400
    for key, value in payload.items():
        if not isinstance(value, str) or not value.strip() or len(value) > 500:
            return jsonify(error=f"{key} must contain 1 to 500 characters."), 400
        if key == "contact_email" and (
            len(value) > 254
            or parseaddr(value)[1] != value
            or value.count("@") != 1
            or "." not in value.rsplit("@", 1)[-1]
        ):
            return jsonify(error="Enter a valid contact email address."), 400
    with connect_db() as db:
        for key, value in payload.items():
            db.execute(
                "INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                (key, value.strip()),
            )
    return jsonify(ok=True)


@app.post("/api/admin/subscriptions/<path:subscription_id>/cancel")
@origin_required
@login_required
def cancel_subscription(subscription_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", subscription_id):
        return jsonify(error="Invalid subscription ID."), 400
    with connect_db() as db:
        row = db.execute(
            "SELECT status FROM subscriptions WHERE paypal_subscription_id = ?",
            (subscription_id,),
        ).fetchone()
    if not row:
        return jsonify(error="Subscription not found."), 404
    if row["status"] in {"CANCELLED", "EXPIRED", "CANCELLED_BY_ADMIN"}:
        return jsonify(ok=True, status=row["status"])
    try:
        paypal_api(
            "POST",
            f"/v1/billing/subscriptions/{subscription_id}/cancel",
            {"reason": "Cancelled by the Northstar account owner."},
        )
    except (RuntimeError, ValueError) as error:
        return jsonify(error=str(error)), 502
    with connect_db() as db:
        db.execute(
            """
            UPDATE subscriptions
            SET status = 'CANCELLED_BY_ADMIN', updated_at = CURRENT_TIMESTAMP
            WHERE paypal_subscription_id = ?
            """,
            (subscription_id,),
        )
    return jsonify(ok=True, status="CANCELLED_BY_ADMIN")


@app.post("/api/checkout")
def create_checkout():
    client = request.remote_addr or "unknown"
    now = time.time()
    attempts = [stamp for stamp in _checkout_attempts.get(client, []) if now - stamp < 900]
    if len(attempts) >= 10:
        _checkout_attempts[client] = attempts
        return jsonify(error="Too many checkout attempts. Please wait a little and try again."), 429
    attempts.append(now)
    _checkout_attempts[client] = attempts
    if len(_checkout_attempts) > 10000:
        _checkout_attempts.clear()
        _checkout_attempts[client] = attempts

    payload = request.get_json(silent=True) or {}
    plan_id = str(payload.get("plan_id", "")).strip()
    with connect_db() as db:
        plan = db.execute(
            "SELECT * FROM plans WHERE id = ? AND active = 1", (plan_id,)
        ).fetchone()
    if not plan:
        return jsonify(error="That subscription plan is unavailable."), 404
    if not plan["paypal_plan_id"]:
        return jsonify(error="Subscriptions are not ready yet. Please try again later."), 503

    public_base = os.getenv("PUBLIC_BASE_URL", request.host_url).rstrip("/")
    with connect_db() as db:
        brand = db.execute(
            "SELECT value FROM site_settings WHERE key = 'brand_name'"
        ).fetchone()
    try:
        result = paypal_api(
            "POST",
            "/v1/billing/subscriptions",
            {
                "plan_id": plan["paypal_plan_id"],
                "custom_id": f"northstar:{plan_id}",
                "application_context": {
                    "brand_name": brand["value"] if brand else DEFAULT_SITE_SETTINGS["brand_name"],
                    "user_action": "SUBSCRIBE_NOW",
                    "return_url": f"{public_base}/?subscription=success",
                    "cancel_url": f"{public_base}/?subscription=cancelled",
                },
            },
        )
    except (RuntimeError, ValueError) as error:
        return jsonify(error=str(error)), 502

    subscription_id = result.get("id")
    approval_url = next(
        (
            link.get("href")
            for link in result.get("links", [])
            if link.get("rel") == "approve" and link.get("href")
        ),
        None,
    )
    if not subscription_id or not approval_url:
        logger.error("PayPal did not return a subscription ID and approval URL.")
        return jsonify(error="PayPal did not return an approval link. Please try again."), 502
    with connect_db() as db:
        db.execute(
            """
            INSERT OR IGNORE INTO subscriptions
                (paypal_subscription_id, plan_id, paypal_plan_id, status)
            VALUES (?, ?, ?, 'APPROVAL_PENDING')
            """,
            (subscription_id, plan["id"], plan["paypal_plan_id"]),
        )
    return jsonify(approval_url=approval_url)


@app.post("/api/webhooks/paypal")
def paypal_webhook():
    webhook_id = os.getenv("PAYPAL_WEBHOOK_ID", "").strip()
    if not webhook_id:
        return jsonify(error="PayPal webhook is not configured."), 503
    event = request.get_json(silent=True)
    if not isinstance(event, dict):
        return jsonify(error="Invalid webhook payload."), 400
    headers = {
        "auth_algo": request.headers.get("Paypal-Auth-Algo"),
        "cert_url": request.headers.get("Paypal-Cert-Url"),
        "transmission_id": request.headers.get("Paypal-Transmission-Id"),
        "transmission_sig": request.headers.get("Paypal-Transmission-Sig"),
        "transmission_time": request.headers.get("Paypal-Transmission-Time"),
    }
    if any(not value for value in headers.values()):
        return jsonify(error="PayPal signature headers are incomplete."), 400
    try:
        verification = paypal_api(
            "POST",
            "/v1/notifications/verify-webhook-signature",
            {
                **headers,
                "webhook_id": webhook_id,
                "webhook_event": event,
            },
        )
    except (RuntimeError, ValueError) as error:
        return jsonify(error=str(error)), 502
    if verification.get("verification_status") != "SUCCESS":
        return jsonify(error="PayPal webhook signature was not valid."), 400

    event_id = event.get("id")
    event_type = event.get("event_type", "")
    resource = event.get("resource") or {}
    if not event_id or not isinstance(resource, dict):
        return jsonify(error="PayPal webhook payload is missing required fields."), 400

    with connect_db() as db:
        try:
            db.execute(
                "INSERT INTO webhook_events (event_id, event_type) VALUES (?, ?)",
                (event_id, event_type),
            )
        except sqlite3.IntegrityError:
            return jsonify(ok=True, duplicate=True)

        if event_type.startswith("BILLING.SUBSCRIPTION."):
            subscription_id = resource.get("id")
            if subscription_id:
                paypal_plan_id = resource.get("plan_id")
                subscriber = resource.get("subscriber") or {}
                email = subscriber.get("email_address")
                status = PAYPAL_SCOPES.get(
                    event_type, str(resource.get("status", "UNKNOWN")).upper()
                )
                plan = db.execute(
                    "SELECT id FROM plans WHERE paypal_plan_id = ?",
                    (paypal_plan_id,),
                ).fetchone()
                db.execute(
                    """
                    INSERT INTO subscriptions (
                        paypal_subscription_id, plan_id, paypal_plan_id,
                        subscriber_email, status
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(paypal_subscription_id) DO UPDATE SET
                        plan_id = COALESCE(excluded.plan_id, subscriptions.plan_id),
                        paypal_plan_id = COALESCE(excluded.paypal_plan_id, subscriptions.paypal_plan_id),
                        subscriber_email = COALESCE(excluded.subscriber_email, subscriptions.subscriber_email),
                        status = excluded.status,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        subscription_id,
                        plan["id"] if plan else None,
                        paypal_plan_id,
                        email,
                        status,
                    ),
                )
        elif event_type.startswith("PAYMENT.SALE."):
            sale_id = resource.get("id")
            subscription_id = resource.get("billing_agreement_id") or resource.get("subscription_id")
            amount = resource.get("amount") or {}
            subscription = (
                db.execute(
                    "SELECT subscriber_email FROM subscriptions WHERE paypal_subscription_id = ?",
                    (subscription_id,),
                ).fetchone()
                if subscription_id
                else None
            )
            payment_status = {
                "PAYMENT.SALE.COMPLETED": "COMPLETED",
                "PAYMENT.SALE.DENIED": "DENIED",
                "PAYMENT.SALE.REFUNDED": "REFUNDED",
            }.get(event_type, event_type.rsplit(".", 1)[-1])
            db.execute(
                """
                INSERT INTO payments (
                    paypal_event_id, paypal_payment_id, paypal_subscription_id,
                    subscriber_email, amount, currency, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    sale_id,
                    subscription_id,
                    subscription["subscriber_email"] if subscription else None,
                    amount.get("total"),
                    amount.get("currency"),
                    payment_status,
                ),
            )
            if subscription_id:
                db.execute(
                    """
                    UPDATE subscriptions
                    SET last_payment_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE paypal_subscription_id = ?
                    """,
                    (subscription_id,),
                )
    return jsonify(ok=True)


@app.get("/health")
def health():
    return jsonify(status="ok")


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == "hash-password":
        import getpass

        password = getpass.getpass("Choose an owner password (at least 14 characters): ")
        if len(password) < 14:
            raise SystemExit("Password must be at least 14 characters.")
        print(hash_password(password))
        return

    if os.getenv("APP_ENV", "development").lower() == "production":
        if not os.getenv("APP_SECRET_KEY"):
            raise SystemExit("APP_SECRET_KEY must be set in production.")
        if not os.getenv("OWNER_EMAIL") or not os.getenv("OWNER_PASSWORD_HASH"):
            raise SystemExit("OWNER_EMAIL and OWNER_PASSWORD_HASH must be set in production.")
        if not os.getenv("PUBLIC_BASE_URL", "").startswith("https://"):
            raise SystemExit("PUBLIC_BASE_URL must use HTTPS in production.")
    init_db()
    production = os.getenv("APP_ENV", "development").lower() == "production"
    serve(
        app,
        host=os.getenv("HOST", "0.0.0.0" if production else "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        threads=8,
    )


if __name__ == "__main__":
    main()
