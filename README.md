# Cycling Coach — self-hosted, single athlete

A LAN-only web app: a personal cycling coach that **learns you**, tracks your
training journey, pulls your live **Strava** data, maintains a **standing
week-ahead plan** on the dashboard, pushes structured workouts to your **Wahoo**
ELEMNT, and plans **bike routes** with brouter. Runs on the **Claude Agent SDK**
using your **Claude subscription** (no per-token API billing). Generic and
publishable — nothing about any athlete is baked in; the coach interviews you on
first run and remembers everything after.

Strava and Wahoo both use the same one-time OAuth connect flow (authorize once,
tokens persist on the volume and auto-refresh).

Single athlete per deployment (no multi-tenant).

```
coach-app/
  app/        FastAPI server, Agent SDK coach, Strava + Wahoo OAuth, brouter routes
  web/        dashboard + chat + route builder (single static page)
  brouter/    bundled GPX routing script
  data/       (mounted) credentials, the coach's memory, snapshot, routes — persists
  Dockerfile  Python + the claude CLI (CLI only for setup-token) + brouter deps
  docker-compose.yml
```

## How it works

- **First run** — memory is empty, so the coach doesn't know you. Open the chat
  and it interviews you: your discipline, goal + target event, experience, key
  numbers (optional — it learns the rest from Strava), home location, units,
  constraints. When it has enough, it **writes its own memory** (`goal.md`,
  `profile.md`, `preferences.md`, starts `journey.md`) and starts coaching.
- **Memory is yours, curated by the coach** — Markdown files on the volume that
  ARE the coach's understanding of you. It reads them every conversation and
  updates them when you tell it something durable (a new FTP, a changed goal, a
  completed key session, a correction). Question-only chats that teach it nothing
  new leave memory untouched.
- **Dashboard** reads a cached Strava snapshot (fast). A **background sync** pulls
  recent rides on an interval and rewrites the snapshot.
- **Week ahead** — the coach maintains `week_plan.md` (a standing, athlete-facing
  training week) and the dashboard renders it. It's distinct from the chat: the
  card shows your *current plan*, updated when the plan changes — not your last
  message. Ask "plan my week" to (re)generate it.
- **Wahoo workouts** — ask the coach to build a structured session and it pushes
  the plan to your ELEMNT (appears when scheduled within 6 days).
- **Routes** — ask the coach in chat ("give me a quiet 2h ride from home"), or use
  the manual **Plan a route** panel. GPX files save to the volume and download
  from the dashboard.

## One-time setup

### 1. Register Strava + Wahoo apps and configure `.env`
Copy `.env.example` to `.env`. Both integrations use OAuth — create a developer
app for each and copy its client id/secret:
- Strava: https://www.strava.com/settings/api
- Wahoo: https://developers.wahooligan.com

Set the public URL the athlete's browser reaches this app at — it must match the
callback/redirect registered in **both** apps:
```
COACH_PUBLIC_URL=http://truenas.local:8080      # default; the OAuth callback host

STRAVA_CLIENT_ID=...
STRAVA_CLIENT_SECRET=...
WAHOO_CLIENT_ID=...
WAHOO_CLIENT_SECRET=...
```
Register the callbacks as:
- Strava "Authorization Callback Domain": `truenas.local` (host only)
- Wahoo "Redirect URI": `http://truenas.local:8080/api/wahoo/callback`

### 2. Create the data dir (writable by the container user)
The container runs as a non-root user (uid 10001) — required, because the Claude
CLI refuses to run headless as root. Give it ownership of the volume once:
```
mkdir -p data && sudo chown -R 10001:10001 data
```

### 3. Build
```
docker compose build
```

### 4. Authenticate (once) — uses your subscription, NOT an API key
The Agent SDK can't read `claude login` creds, so mint a long-lived
subscription token:
```
docker compose run --rm coach claude setup-token
```
Follow the prompt; it prints a token. Persist it on the volume:
```
echo '<the-token>' > ./data/claude-home/oauth_token
```
(or paste into `CLAUDE_CODE_OAUTH_TOKEN` in `.env`). Survives restarts (~1 yr).

### 5. Run + connect
```
docker compose up -d
```
Open `http://truenas.local:8080` and say hello — the coach takes it from there.
The dashboard shows **Connect Strava** / **Connect Wahoo** banners; click each
once to authorize (or visit `/api/strava/connect` and `/api/wahoo/connect`).
Tokens persist on the volume and refresh automatically.

## Verify
```
curl http://localhost:8080/api/health
# {"ok":true,"sdk":true,"authenticated":true,
#  "strava_configured":true,"strava_connected":true,
#  "wahoo_configured":true,"wahoo_connected":true,"onboarded":false,...}
```
- `authenticated:false` → step 4 didn't land; check `./data/claude-home/oauth_token`.
- `strava_configured:false` / `wahoo_configured:false` → the app's client id/secret
  aren't set in `.env`.
- `*_connected:false` → app credentials are set but you haven't authorized yet;
  click the connect banner (or hit `/api/<svc>/connect`).
- `onboarded:false` → expected before your first chat; flips to true once the
  coach has written `goal.md`.
- `FATAL: cannot create /data...` on startup → the `./data` dir isn't writable by
  uid 10001; rerun step 2's `chown`.

## Safe to publish
This repo is safe to commit to a public host. Secrets and per-deployment state
never enter version control:
- `.gitignore` excludes `.env` and the entire `data/` volume (OAuth token, Strava
  token, the athlete's private memory, generated routes, snapshot).
- `.dockerignore` keeps the same out of the build context / image.
- `docker-compose.yml` and the Dockerfile contain **no secrets** — every credential
  is read from the environment with empty defaults.
- The image runs as a **non-root user** with `no-new-privileges`.

Before your first commit, sanity-check: `git status` should not list anything
under `data/` or a real `.env`.

## Notes
- Generic by design: no athlete, sport bias, location, or units are hard-coded —
  all learned and stored in `/data/memory`. Wipe the volume to reset to a fresh
  coach for a new athlete.
- **No UI auth** (LAN trust). Don't expose 8080 to the internet; if you must, put
  an authenticating reverse proxy in front.
- Chat is **stateless per request** with history replayed as context. For longer/
  cheaper continuity, switch `coach.stream_reply` to `ClaudeSDKClient` with a
  persisted `session_id` (TODO in `coach.py`).
- Strava estimated power for bikes without a meter is treated as unreliable by the
  coach.
