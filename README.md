# Northstar web app

This is a small Python/Flask web app with a SQLite database, private owner dashboard, and PayPal recurring subscriptions. The public site is served by the backend at `/`; do not open `index.html` directly as a file, because checkout and the admin dashboard require the server.

## Run locally

1. Install Python 3.11 or newer.
2. Install the dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env`.
4. Generate an owner password hash:

   ```powershell
   python app.py hash-password
   ```

   Use a unique password with at least 14 characters. Copy the printed hash into `OWNER_PASSWORD_HASH` in `.env`. Set `OWNER_EMAIL` to the email you want to use for the private owner login. Never put the plain password in `.env`, HTML, or JavaScript.

5. Configure `APP_SECRET_KEY` with a long random secret. For example, generate one with:

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

6. Start the app:

   ```powershell
   python app.py
   ```

7. Open `http://127.0.0.1:8000` for the public site and `http://127.0.0.1:8000/admin` for owner sign-in.

The default plans are $12/month (Studio) and $24/month (Collective), in USD. The free Solo card is not a paid PayPal plan.

## Connect your PayPal account

1. Use a PayPal Business account that can receive commercial payments in your country.
2. In the PayPal developer dashboard, create a REST API app associated with that merchant account. Start in sandbox and put its client ID and secret into `.env` as `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`. Do not publish these values or paste them into the HTML.
3. Keep `PAYPAL_MODE=sandbox` while testing. Sign in to `/admin`, choose **Set up PayPal plans**, and verify the plans using PayPal sandbox buyer accounts.
4. Create a PayPal webhook for the deployed HTTPS URL `https://YOUR-DOMAIN/api/webhooks/paypal`. Subscribe to:
   - `BILLING.SUBSCRIPTION.CREATED`
   - `BILLING.SUBSCRIPTION.ACTIVATED`
   - `BILLING.SUBSCRIPTION.UPDATED`
   - `BILLING.SUBSCRIPTION.CANCELLED`
   - `BILLING.SUBSCRIPTION.SUSPENDED`
   - `BILLING.SUBSCRIPTION.EXPIRED`
   - `PAYMENT.SALE.COMPLETED`
   - `PAYMENT.SALE.DENIED`
   - `PAYMENT.SALE.REFUNDED`

   Put the webhook ID from PayPal in `PAYPAL_WEBHOOK_ID`. Webhooks are signature-verified with PayPal before changing the subscription or payment records.
5. Deploy the app behind an HTTPS-capable host with persistent storage for `northstar.sqlite3`. Set `APP_ENV=production`, `HOST=0.0.0.0` (unless your host requires a different bind address), `PUBLIC_BASE_URL=https://YOUR-DOMAIN`, and a new production `APP_SECRET_KEY`. Production startup also requires `OWNER_EMAIL` and `OWNER_PASSWORD_HASH`. Set `PAYPAL_MODE=live` and replace sandbox credentials with the live REST credentials only after end-to-end sandbox testing. Create/verify live billing plans and the live webhook before advertising paid subscriptions.

The merchant account linked to the PayPal REST app is where PayPal sends proceeds. The app never receives or stores card/bank credentials; PayPal handles checkout, billing, refunds, and payouts. The dashboard shows verified payment events, not your PayPal balance or payout schedule. PayPal merchant availability, fees, tax obligations, and payout timing vary by country and account.

## Owner dashboard

The private dashboard at `/admin` lets the configured owner update selected website text, manage the two paid plan prices/descriptions, see subscription and verified payment events, and request subscription cancellation at PayPal. Price changes create a new PayPal billing plan for new subscribers; existing PayPal billing agreements keep their current plan. PayPal webhook events update the local status records. The dashboard is not a substitute for the PayPal merchant console.

The database is local SQLite. Back it up securely, do not put it in version control, and use persistent disk storage in deployment. Configure the production host to allow only trusted hosts and terminate TLS before exposing the app publicly. Publish appropriate terms, cancellation/refund, privacy, and recurring billing disclosures before accepting live subscriptions.
