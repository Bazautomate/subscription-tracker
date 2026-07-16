# Subscription Tracker

A small self-hosted web app for tracking recurring subscriptions (AI tools, proxies, etc.) and getting a Discord/email reminder a configurable number of days before each one renews — so you can decide to cancel or keep it before you get charged again.

Currently deployed at **https://subs.baz-n8n.xyz** (protected by HTTP basic auth), running via Coolify on the n8n VM, tunneled through Pangolin.

## Features

- Responsive card-based UI (works on phones), light/dark aware. Each entry shows name, platform, price and a renewal badge; editing is collapsed behind an Edit button.
- Cost summary tiles: total **per month** (yearly subscriptions are divided by 12), the yearly equivalent, active count, and one-time-payment totals.
- **One-time payments** tracked separately from recurring subscriptions (name, platform, price, date paid, notes), with "this month" and all-time totals. These are logged only — they never trigger reminders.
- Automatically computes each subscription's next renewal date from its start date and billing cycle.
- A daily background job (APScheduler) checks all active subscriptions and fires a reminder once a subscription enters its notify window (default: 5 days before renewal). Each renewal period only triggers one notification (tracked via `last_notified`), even if the check runs multiple times or the container restarts.
- Notifications via **Discord webhook** and/or **email (SMTP)** — either or both can be configured; whichever has credentials set will fire.
- A "Send test notification" button in the UI to verify Discord/email wiring without waiting for a real renewal.
- Optional HTTP basic auth (`BASIC_AUTH_USER` / `BASIC_AUTH_PASS`) protecting the whole app, since this is meant to be reachable from the public internet.
- `/healthz` endpoint (exempt from basic auth) for container health checks.

## Project layout

```
app.py                 Flask app: routes, scheduler, notification senders
templates/index.html   The editable table UI (vanilla JS + fetch, no build step)
requirements.txt       Python dependencies
Dockerfile             python:3.12-slim + curl (needed for Docker/Coolify healthchecks)
docker-compose.yml     For running standalone with `docker compose up`
.env.example           Template for required environment variables
data/                  SQLite database lives here (gitignored, persisted via volume)
```

## Data model

Single SQLite table, `subscriptions`:

| column | type | notes |
|---|---|---|
| `id` | integer PK | |
| `name` | text | required |
| `platform` | text | optional, free text (e.g. "OpenAI", "Bright Data") |
| `price` | real | informational only, not used in notification logic |
| `start_date` | text (`YYYY-MM-DD`) | required — the date the subscription began/last renewed from |
| `billing_cycle` | text | `monthly` or `yearly` |
| `notify_days_before` | integer | how many days before renewal to notify (default 5) |
| `active` | integer (bool) | inactive subscriptions are skipped by the renewal check |
| `last_notified` | text (`YYYY-MM-DD`) | the renewal date last notified for, so each period only fires once |

Renewal dates are computed on the fly (`next_renewal_date` in `app.py`): starting from `start_date`, step forward by one billing cycle at a time until the result is today or later.

## Environment variables

See `.env.example`. Copy it to `.env` for local/docker-compose use (Coolify deployments set these directly in its dashboard instead of a `.env` file):

| var | required? | purpose |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | optional | Discord webhook URL. Leave blank to disable Discord notifications. |
| `DISCORD_USER_ID` | optional | Discord user ID to `@`-mention in reminders, so they ping rather than sit unread. Leave blank for no mention. Must be set in Coolify — it is deliberately not hardcoded, since this repo is public. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `NOTIFY_EMAIL_TO` | optional | SMTP credentials for email notifications. All must be set for email to send; leave blank to disable. Note: a plain account password often won't work (e.g. Proton Mail requires an app-specific SMTP token via Proton Mail Bridge, not your login password) — use an SMTP-capable credential like a Gmail app password. |
| `NOTIFY_CHECK_HOUR` | optional (default `9`) | Hour (0–23, server local time) the daily renewal check runs. |
| `BASIC_AUTH_USER`, `BASIC_AUTH_PASS` | optional | If `BASIC_AUTH_USER` is set, the whole app (except `/healthz`) requires HTTP basic auth with these credentials. Leave `BASIC_AUTH_USER` blank to disable auth (e.g. local dev). |
| `DB_PATH` | optional (default `data/subscriptions.db`) | Where the SQLite file lives. |

## Running locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Visit `http://localhost:5000`. Without `BASIC_AUTH_USER` set, there's no login prompt.

## Running with Docker Compose

```bash
cp .env.example .env   # fill in real values
docker compose up -d --build
```

Serves on port 5000 by default (see `docker-compose.yml` to change the host port), with the SQLite DB persisted in `./data`.

## Current deployment

This app runs as part of a small self-hosted infrastructure:

```
GitHub (Bazautomate/subscription-tracker, public repo)
        │  Coolify clones + builds — but only when a deploy is triggered
        │  by hand; pushing to master does NOT deploy (see "Redeploying")
        ▼
n8n VM (see ~/.ssh/config host "n8n") — Coolify-managed
  Project: "vibe code"
  App: subscription-tracker, container port 5000 → host port 8010
  Persistent volume: /app/data (survives redeploys)
        │  newt (tunnel agent, systemd service newt-pangolin.service)
        │  maintains an outbound WireGuard tunnel to Pangolin —
        │  no inbound firewall ports needed on this VM
        ▼
Pangolin VM (see ~/.ssh/config host "pangolin")
  Traefik terminates TLS (Let's Encrypt) for *.baz-n8n.xyz
  Resource: subs.baz-n8n.xyz → site "n8n-server" → target 127.0.0.1:8010
        ▼
https://subs.baz-n8n.xyz  (public, HTTP basic auth protected)
```

### Redeploying after a code change

**Deploys are manual. Pushing to `master` does nothing on its own.**

Coolify's auto-deploy toggle is on, but it is inert: it only acts when GitHub calls its webhook, and GitHub cannot reach this Coolify. There is no webhook registered on the repo, Coolify has no FQDN configured, and its port 8000 is firewalled off from the internet (upstream cloud firewall — the VM itself has no `ufw`/`iptables` rules). Verified 2026-07-16, after two pushes appeared to succeed while nothing deployed. Every other app on this box is deployed from a prebuilt Docker image and is likewise redeployed by hand.

So after pushing, **deploy explicitly** — via the Coolify UI (app → Deploy), or from the VM:

```bash
ssh n8n 'docker exec coolify php artisan tinker --execute="
  \$app = App\Models\Application::where(\"uuid\", \"givsjcr64afax7wqfgq8j0vh\")->first();
  queue_application_deployment(
    application: \$app,
    deployment_uuid: (string) new Visus\Cuid2\Cuid2(7),
    force_rebuild: true,
    is_api: true,
  );
"'
```

Then confirm what actually landed — a finished deploy is not proof the new code is running:

```bash
ssh n8n 'docker exec $(docker ps -q --filter name=givsjcr64afax7wqfgq8j0vh) printenv SOURCE_COMMIT'
```

Note: environment variables are Laravel-encrypted in Coolify's database. Set them through the UI or the `App\Models\EnvironmentVariable` model — **never** with raw SQL, or Coolify will fail to decrypt them. (The columns are `is_runtime` / `is_buildtime`.)

### Gotcha: the running container can drift from git

On 2026-07-15 the Discord `@`-mention was hot-patched directly into the live container's `app.py`, leaving an `app.py.bak` behind, and was never committed — so a rebuild would have silently reverted it. That fix now lives in git as `DISCORD_USER_ID`. Before redeploying, diff the container's files against the repo; a rebuild discards anything patched in by hand.

### Gotcha: Pangolin target IP must be `127.0.0.1`, not `localhost`

When adding/editing the Pangolin Resource's target, use `127.0.0.1` as the target IP, **not** `localhost`. On this VM, `localhost` resolves to `::1` (IPv6) first, and since this Flask app's dev server only binds IPv4 inside its container, the IPv6 path gets accepted at the socket level but then reset mid-connection — producing a 502 at the Pangolin/Traefik layer that looks like a routing problem but is actually just an address-family mismatch. Other resources on this box that happen to use `localhost` work only because those particular backends bind both IPv4 and IPv6.

### Restarting the tunnel

If a newly added Pangolin target doesn't get picked up, restart the tunnel agent on the n8n VM:

```bash
ssh n8n "systemctl restart newt-pangolin"
```

This is a shared tunnel for every app on that VM routed through Pangolin (n8n, NocoDB, MinIO, etc.) — restarting it causes a few seconds of interruption for all of them, not just this app.

## Security notes

- Nothing in this repo is a secret — all credentials (Discord webhook, SMTP, basic auth password) are injected via environment variables, never committed. `.env` is gitignored.
- The repo is public on GitHub (required for Coolify's "public repository" deploy method to `git clone` it without needing a deploy key or GitHub App). Double-check before committing that no real credentials ever end up hardcoded in `app.py` or elsewhere.
- Basic auth is enforced at the application layer (not the proxy layer), so it protects the app the same way regardless of what's in front of it (Coolify direct port, Pangolin tunnel, or anything else in the future).
