"use strict";

const loginView = document.querySelector("#login-view");
const dashboardView = document.querySelector("#dashboard-view");
const loginForm = document.querySelector("#login-form");
const loginNotice = document.querySelector("#login-notice");
const dashboardNotice = document.querySelector("#dashboard-notice");

function showNotice(element, message, kind = "error") {
  element.textContent = message;
  element.dataset.kind = kind;
}

function clearNotice(element) {
  element.textContent = "";
  delete element.dataset.kind;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  const result = await response.json().catch(() => ({}));
  if (response.status === 401) {
    showLogin();
    throw new Error(result.error || "Please sign in again.");
  }
  if (!response.ok) {
    throw new Error(result.error || "The request could not be completed.");
  }
  return result;
}

function showLogin() {
  dashboardView.hidden = true;
  loginView.hidden = false;
}

function showDashboard(email) {
  loginView.hidden = true;
  dashboardView.hidden = false;
  document.querySelector("#owner-email").textContent = email;
}

function formatMoney(cents) {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
  }).format(cents / 100);
}

function formatDate(value) {
  if (!value) return "—";
  const parsed = new Date(value.replace(" ", "T") + (value.endsWith("Z") ? "" : "Z"));
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString();
}

function makeCell(text) {
  const cell = document.createElement("td");
  cell.textContent = text || "—";
  return cell;
}

function renderPlans(plans) {
  const container = document.querySelector("#plans-list");
  container.replaceChildren();
  for (const plan of plans) {
    const article = document.createElement("article");
    article.className = "plan";

    const header = document.createElement("div");
    header.className = "plan-top";
    const title = document.createElement("h3");
    title.textContent = plan.name;
    const status = document.createElement("span");
    status.className = `plan-status${plan.paypal_configured ? " ready" : ""}`;
    status.textContent = plan.paypal_configured ? "PayPal plan ready" : "PayPal setup needed";
    header.append(title, status);

    const description = document.createElement("p");
    description.textContent = plan.description;

    const form = document.createElement("form");
    form.className = "plan-form";
    form.dataset.planId = plan.id;
    form.innerHTML = `
      <label>Plan name<input name="name" maxlength="80" required></label>
      <label>Monthly price (USD)<input name="price" type="number" min="1" max="9999.99" step="0.01" required></label>
      <label>Description<input name="description" maxlength="500" required></label>
      <label><span>Billing status</span><select name="active"><option value="true">Active</option><option value="false">Hidden</option></select></label>
      <button class="button button-secondary" type="submit">Save plan</button>
    `;
    form.elements.name.value = plan.name;
    form.elements.price.value = (plan.price_cents / 100).toFixed(2);
    form.elements.description.value = plan.description;
    form.elements.active.value = String(plan.active);
    form.addEventListener("submit", savePlan);
    article.append(header, description, form);
    container.append(article);
  }
}

async function savePlan(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  clearNotice(dashboardNotice);
  try {
    await api(`/api/admin/plans/${encodeURIComponent(form.dataset.planId)}`, {
      method: "PUT",
      body: JSON.stringify({
        name: form.elements.name.value.trim(),
        description: form.elements.description.value.trim(),
        price_cents: Math.round(Number(form.elements.price.value) * 100),
        active: form.elements.active.value === "true",
      }),
    });
    showNotice(dashboardNotice, "Plan saved. New subscriptions use the updated PayPal plan.", "success");
    await loadDashboard();
  } catch (error) {
    showNotice(dashboardNotice, error.message);
  } finally {
    button.disabled = false;
  }
}

function renderSubscriptions(subscriptions) {
  const table = document.querySelector("#subscriptions-table");
  table.replaceChildren();
  if (!subscriptions.length) {
    const row = document.createElement("tr");
    const empty = document.createElement("td");
    empty.className = "empty";
    empty.colSpan = 6;
    empty.textContent = "No subscriptions yet. Verified PayPal activity will appear here.";
    row.append(empty);
    table.append(row);
    return;
  }
  for (const subscription of subscriptions) {
    const row = document.createElement("tr");
    row.append(
      makeCell(subscription.subscriber_email || "Email not provided"),
      makeCell(subscription.plan_name || "—"),
    );
    const statusCell = document.createElement("td");
    const pill = document.createElement("span");
    pill.className = `pill${subscription.status === "APPROVAL_PENDING" ? " pending" : ""}`;
    pill.textContent = subscription.status;
    statusCell.append(pill);
    row.append(statusCell, makeCell(formatDate(subscription.created_at)), makeCell(formatDate(subscription.last_payment_at)));

    const actionCell = document.createElement("td");
    if (["ACTIVE", "APPROVAL_PENDING", "SUSPENDED"].includes(subscription.status)) {
      const cancelButton = document.createElement("button");
      cancelButton.className = "button button-danger";
      cancelButton.type = "button";
      cancelButton.textContent = "Cancel";
      cancelButton.addEventListener("click", () => cancelSubscription(subscription.paypal_subscription_id, cancelButton));
      actionCell.append(cancelButton);
    } else {
      actionCell.textContent = "—";
    }
    row.append(actionCell);
    table.append(row);
  }
}

async function cancelSubscription(subscriptionId, button) {
  if (!window.confirm("Cancel this PayPal subscription? The customer will not be billed again.")) return;
  button.disabled = true;
  clearNotice(dashboardNotice);
  try {
    await api(`/api/admin/subscriptions/${encodeURIComponent(subscriptionId)}/cancel`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    showNotice(dashboardNotice, "Subscription cancellation was sent to PayPal.", "success");
    await loadDashboard();
  } catch (error) {
    showNotice(dashboardNotice, error.message);
  } finally {
    button.disabled = false;
  }
}

function renderPayments(payments) {
  const table = document.querySelector("#payments-table");
  table.replaceChildren();
  if (!payments.length) {
    const row = document.createElement("tr");
    const empty = document.createElement("td");
    empty.className = "empty";
    empty.colSpan = 5;
    empty.textContent = "No verified payment events have been received yet.";
    row.append(empty);
    table.append(row);
    return;
  }
  for (const payment of payments) {
    const row = document.createElement("tr");
    row.append(
      makeCell(formatDate(payment.created_at)),
      makeCell(payment.subscriber_email),
      makeCell(payment.paypal_payment_id),
      makeCell(payment.amount && payment.currency ? `${payment.amount} ${payment.currency}` : "—"),
      makeCell(payment.status),
    );
    table.append(row);
  }
}

function fillSettings(settings) {
  const form = document.querySelector("#settings-form");
  for (const [key, value] of Object.entries(settings)) {
    if (form.elements[key]) form.elements[key].value = value;
  }
}

async function loadDashboard() {
  const data = await api("/api/admin/overview");
  document.querySelector("#active-subscriptions").textContent = data.stats.active_subscriptions;
  document.querySelector("#total-subscriptions").textContent = data.stats.total_subscriptions;
  document.querySelector("#total-paid").textContent = new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
  }).format(data.stats.total_paid);
  document.querySelector("#paypal-mode").textContent =
    `PayPal ${data.paypal_mode}${data.paypal_ready ? " · ready" : " · setup required"}`;
  renderPlans(data.plans);
  renderSubscriptions(data.subscriptions);
  renderPayments(data.payments);
  fillSettings(data.settings);
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearNotice(loginNotice);
  const button = loginForm.querySelector("button");
  button.disabled = true;
  try {
    const result = await api("/api/admin/login", {
      method: "POST",
      body: JSON.stringify({
        email: loginForm.elements.email.value,
        password: loginForm.elements.password.value,
      }),
    });
    loginForm.reset();
    showDashboard(result.email);
    await loadDashboard();
  } catch (error) {
    showNotice(loginNotice, error.message);
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#logout-button").addEventListener("click", async () => {
  try {
    await api("/api/admin/logout", { method: "POST", body: JSON.stringify({}) });
    showLogin();
  } catch (error) {
    showNotice(dashboardNotice, error.message);
  }
});

document.querySelector("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button");
  button.disabled = true;
  clearNotice(dashboardNotice);
  try {
    const settings = Object.fromEntries(new FormData(form).entries());
    await api("/api/admin/site-settings", {
      method: "PUT",
      body: JSON.stringify(settings),
    });
    showNotice(dashboardNotice, "Website settings saved.", "success");
  } catch (error) {
    showNotice(dashboardNotice, error.message);
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#paypal-setup-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  clearNotice(dashboardNotice);
  try {
    const result = await api("/api/admin/paypal/setup", {
      method: "POST",
      body: JSON.stringify({}),
    });
    showNotice(
      dashboardNotice,
      result.configured.length
        ? `Created PayPal billing plans for: ${result.configured.join(", ")}. Finish webhook setup before going live.`
        : "PayPal plans are already configured. Set up the webhook before going live.",
      "success",
    );
    await loadDashboard();
  } catch (error) {
    showNotice(dashboardNotice, error.message);
  } finally {
    button.disabled = false;
  }
});

api("/api/admin/session")
  .then((result) => {
    if (!result.authenticated) return;
    showDashboard(result.email);
    return loadDashboard();
  })
  .catch((error) => showNotice(loginNotice, error.message));
