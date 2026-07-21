// POST /api/checkout {plan: "week"|"month"|"year"} -> {url} (Stripe Checkout Session)
//
// Talks to Stripe's REST API directly with fetch — no npm dependency, so the whole
// web/ folder deploys to Vercel with zero build step. Prices are created inline
// (price_data) so nothing has to be pre-configured in the Stripe dashboard.
// With a test-mode STRIPE_SECRET_KEY (sk_test_...) this is a full end-to-end
// checkout using card 4242 4242 4242 4242; swapping in a live key is the only
// change needed to charge real money.

const PLANS = {
  week:  { amount: 500,   interval: "week",  name: "Contextline Weekly" },
  month: { amount: 2000,  interval: "month", name: "Contextline Monthly" },
  year:  { amount: 19200, interval: "year",  name: "Contextline Yearly ($16/mo)" },
};

export default async function handler(req, res) {
  if (req.method !== "POST") return res.status(405).json({ error: "POST only" });

  const key = process.env.STRIPE_SECRET_KEY;
  if (!key) return res.status(500).json({ error: "STRIPE_SECRET_KEY is not configured" });

  const plan = PLANS[(req.body && req.body.plan) || ""];
  if (!plan) return res.status(400).json({ error: "unknown plan" });

  const origin = `https://${req.headers.host}`;
  const form = new URLSearchParams({
    mode: "subscription",
    success_url: `${origin}/success.html?session_id={CHECKOUT_SESSION_ID}`,
    cancel_url: `${origin}/`,
    "line_items[0][quantity]": "1",
    "line_items[0][price_data][currency]": "usd",
    "line_items[0][price_data][unit_amount]": String(plan.amount),
    "line_items[0][price_data][recurring][interval]": plan.interval,
    "line_items[0][price_data][product_data][name]": plan.name,
  });

  const r = await fetch("https://api.stripe.com/v1/checkout/sessions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: form,
  });
  const session = await r.json();
  if (!r.ok) {
    return res.status(502).json({ error: session.error?.message || "Stripe error" });
  }
  return res.status(200).json({ url: session.url });
}
