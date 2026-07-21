// GET /api/success?session_id=cs_... -> {token} once the checkout session is paid.
//
// Verifies the session against Stripe server-side (the client can't fake it), then
// hands out an invite token from INVITE_TOKEN_POOL (comma-separated cl_... tokens
// that are also in the relay's INVITE_TOKENS). Assignment is a deterministic hash
// of the session id: the same buyer refreshing gets the same token, and no
// database is needed. Collisions between different buyers are possible but
// acceptable at ~10 users; a KV store keyed on session id is the scale-up.

export default async function handler(req, res) {
  const key = process.env.STRIPE_SECRET_KEY;
  const pool = (process.env.INVITE_TOKEN_POOL || "")
    .split(",").map((t) => t.trim()).filter(Boolean);
  if (!key) return res.status(500).json({ error: "STRIPE_SECRET_KEY is not configured" });
  if (!pool.length) return res.status(500).json({ error: "INVITE_TOKEN_POOL is not configured" });

  const id = String(req.query.session_id || "");
  if (!/^cs_[a-zA-Z0-9_]+$/.test(id)) return res.status(400).json({ error: "bad session id" });

  const r = await fetch(
    `https://api.stripe.com/v1/checkout/sessions/${encodeURIComponent(id)}`,
    { headers: { Authorization: `Bearer ${key}` } },
  );
  const session = await r.json();
  if (!r.ok) return res.status(502).json({ error: session.error?.message || "Stripe error" });
  if (session.payment_status !== "paid") {
    return res.status(402).json({ error: "payment not completed" });
  }

  let h = 0;
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return res.status(200).json({ token: pool[h % pool.length] });
}
