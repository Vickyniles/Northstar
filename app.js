"use strict";

const checkoutStatus = document.createElement("p");
checkoutStatus.className = "pricing-note";
checkoutStatus.setAttribute("role", "status");
checkoutStatus.setAttribute("aria-live", "polite");
const pricingGrid = document.querySelector(".pricing-grid");
if (pricingGrid) pricingGrid.insertAdjacentElement("afterend", checkoutStatus);

function showCheckoutMessage(message) {
  checkoutStatus.textContent = message;
}

async function loadSiteContent() {
  const response = await fetch("/api/site-settings");
  if (!response.ok) throw new Error("Site settings could not be loaded.");
  const { settings } = await response.json();
  if (settings.brand_name) {
    document.querySelectorAll(".brand-name").forEach((node) => {
      node.textContent = settings.brand_name.toLowerCase();
    });
    document.title = `${settings.brand_name} — Thoughtful digital, built to move you forward`;
  }
  const title = document.querySelector("[data-site-setting=hero_title]");
  if (title && settings.hero_title) title.textContent = settings.hero_title;
  const heroCopy = document.querySelector("[data-site-setting=hero_copy]");
  if (heroCopy && settings.hero_copy) heroCopy.textContent = settings.hero_copy;
  const contactHeading = document.querySelector("[data-site-setting=contact_heading]");
  if (contactHeading && settings.contact_heading) contactHeading.textContent = settings.contact_heading;
  const contactCopy = document.querySelector("[data-site-setting=contact_copy]");
  if (contactCopy && settings.contact_copy) contactCopy.textContent = settings.contact_copy;
  document.querySelectorAll("[data-contact-email]").forEach((link) => {
    if (settings.contact_email) {
      link.href = `mailto:${settings.contact_email}`;
      if (link.hasAttribute("data-email-label")) link.textContent = settings.contact_email;
    }
  });
}

async function loadPlans() {
  const response = await fetch("/api/plans");
  if (!response.ok) throw new Error("Subscription plans could not be loaded.");
  const { plans } = await response.json();
  for (const plan of plans) {
    const card = document.querySelector(`[data-plan-card="${CSS.escape(plan.id)}"]`);
    if (!card) continue;
    const name = card.querySelector("[data-plan-name]");
    const description = card.querySelector("[data-plan-description]");
    const amount = card.querySelector("[data-plan-amount]");
    const button = card.querySelector("[data-checkout]");
    if (name) name.textContent = plan.name;
    if (description) description.textContent = plan.description;
    if (amount) amount.textContent = new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: plan.currency,
      maximumFractionDigits: 0,
    }).format(plan.price_cents / 100);
    if (button) {
      button.dataset.planId = plan.id;
      button.textContent = plan.available ? "Subscribe with PayPal" : "Subscriptions coming soon";
      if (!plan.available) {
        button.setAttribute("aria-disabled", "true");
        button.classList.add("button-light");
      }
    }
  }
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-checkout]");
  if (!button) return;
  event.preventDefault();
  if (button.getAttribute("aria-disabled") === "true") {
    showCheckoutMessage("Subscriptions are being set up. Please check back soon.");
    return;
  }

  button.setAttribute("aria-busy", "true");
  button.setAttribute("aria-disabled", "true");
  showCheckoutMessage("Connecting to PayPal…");
  try {
    const response = await fetch("/api/checkout", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_id: button.dataset.planId }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || "Checkout could not be started.");
    const approval = new URL(result.approval_url);
    const allowedHost = [
      "www.sandbox.paypal.com",
      "www.paypal.com",
      "paypal.com",
    ].includes(approval.hostname);
    if (approval.protocol !== "https:" || !allowedHost) {
      throw new Error("PayPal returned an unexpected approval link.");
    }
    window.location.assign(approval.href);
  } catch (error) {
    showCheckoutMessage(error.message);
    button.removeAttribute("aria-disabled");
  } finally {
    button.removeAttribute("aria-busy");
  }
});

const query = new URLSearchParams(window.location.search);
if (query.get("subscription") === "success") {
  showCheckoutMessage("Thanks! PayPal is confirming your subscription. You’ll be active as soon as PayPal notifies us.");
  window.history.replaceState({}, "", window.location.pathname + window.location.hash);
} else if (query.get("subscription") === "cancelled") {
  showCheckoutMessage("Checkout was cancelled. You have not started a subscription.");
  window.history.replaceState({}, "", window.location.pathname + window.location.hash);
}

Promise.all([loadSiteContent(), loadPlans()]).catch((error) => {
  showCheckoutMessage(error.message);
});
