# Daylook web funnel (Vercel)

The public front door: an Umax-style onboarding funnel (hook → 3-question quiz →
"analyzing" beat → personalized loss reveal → 3 plans → Stripe checkout → invite
code + download). Static HTML + two dependency-free serverless functions — no
build step, no framework, nothing to `npm install`.

```
web/
├── index.html        # the funnel (6 screens)
├── success.html      # post-checkout: verifies payment, shows invite code + install steps
└── api/
    ├── checkout.js   # POST {plan} -> Stripe Checkout Session URL (inline prices)
    └── success.js    # GET ?session_id -> {token} after server-side payment verification
```

Everything here runs on free tiers: Vercel Hobby (hosting + functions) and
Stripe **test mode** (full checkout flow, card `4242 4242 4242 4242`, any future
expiry/CVC — no real money moves). Flipping to live = swapping one env var.

## Deploy (one time, ~5 minutes)

1. **Stripe test key** — dashboard.stripe.com → toggle **Test mode** → Developers
   → API keys → copy the **secret** key (`sk_test_...`). Never the live one.
2. **Vercel** — sign up free with GitHub, install the CLI, then from the repo root:

   ```bash
   npm i -g vercel
   vercel login                      # interactive — run it yourself
   cd web
   vercel deploy --prod             # root of the project = this folder
   ```

3. **Env vars** (Vercel dashboard → Project → Settings → Environment Variables,
   or `vercel env add`):

   | Name | Value |
   |---|---|
   | `STRIPE_SECRET_KEY` | `sk_test_...` (test mode) |
   | `INVITE_TOKEN_POOL` | a comma-separated handful of `cl_...` tokens from `relay/tokens.txt` |

   The pool tokens must also be in the relay's `INVITE_TOKENS` — the funnel hands
   out codes, the relay honors them. Redeploy after setting env vars.

## Test it like a stranger

Open the deployed URL in a private window and run the whole journey:
quiz → reveal → pick a plan → pay with `4242 4242 4242 4242` → land on
success → copy the code → download the `.dmg` → paste the code → "start
tracking". If any step needs you to know something a stranger wouldn't,
that's a bug in the funnel.

## Going live later (real money)

1. Activate the Stripe account, swap `STRIPE_SECRET_KEY` to the live key.
2. That's it for the funnel. (Relay must already be on the paid-key upstream —
   see `DISTRIBUTION.md` §5 launch gates.)

## Student freebies worth grabbing

[GitHub Student Developer Pack](https://education.github.com/pack): a free
`.me`/`.tech` domain (Namecheap/get.tech) to put in front of the Vercel URL,
plus DigitalOcean/Heroku credits if the relay ever outgrows Cloud Run's free
tier. Vercel Hobby itself is free without the pack.

## Notes

- Token assignment is a deterministic hash of the Stripe session id — same
  buyer refreshing gets the same code, no database. Fine at ~10 users;
  a KV store keyed on session id is the upgrade.
- `docs/index.html` (GitHub Pages) is the older static landing page; this
  funnel supersedes it once deployed. Keep Pages as a fallback download page
  or retire it.
