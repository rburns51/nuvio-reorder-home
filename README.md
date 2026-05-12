# Nuvio Reorder Home

A small standalone web app for reordering a Nuvio Home layout without installing the full NuvioSync manager.

The app lets a user sign in to Nuvio, pick a profile, load the TV or Mobile Home layout, reorder rows, hide or show rows, and save the updated layout directly back to Nuvio.

## Why this exists

Nuvio Home layouts can become large and hard to manage. This tool gives users a focused reorder page with no durable account system, no saved target records, and no manifest proxy management.

It is meant for one job: reorder Home rows safely and clearly.

## Features

- Temporary Nuvio login
- Profile selector
- Separate TV and Mobile Home layout editing
- Drag and drop reorder
- Arrow based reorder
- Optional unlocked row number editing
- A-Z check view for spotting duplicates or odd rows
- Search and filters
- Duplicate raw order warnings
- Hide or show Home rows
- Bulk visibility actions for visible catalog rows
- Remove orphan collection Home rows
- Save to TV, Mobile, or both
- Optional apply Profile 1 layout to profiles that inherit Profile 1 add-ons
- No manifest removal, no manifest proxy, no catalog exclusion database

## Security and privacy model

This app forwards the Nuvio email and password to Nuvio/Supabase auth during login. It does not save the password.

After login, the app stores the Nuvio access token and refresh token in an encrypted, HttpOnly browser cookie. The cookie is signed and encrypted with `FLASK_SECRET`, marked `Secure` in production, and expires after the configured session lifetime. Logging out clears the cookie.

The server does not keep a database of users, saved accounts, saved targets, passwords, profile selections, or Home layouts.

Important trust note: if you use someone else's hosted copy, that server still receives your Nuvio email and password during the login request. Self-host this repo if you do not want to trust another operator.

## Public deployment hardening included

- Standalone `.env` only
- No dependency on a parent NuvioSync `.env`
- Production mode by default
- Debug responses off by default
- Generic login errors
- Login and API rate limits
- Encrypted HttpOnly session cookie
- CSRF token required for authenticated mutation routes
- No-store cache headers for dynamic pages and API responses
- `X-Robots-Tag: noindex, nofollow, noarchive`
- `/robots.txt` blocks crawlers
- CSP, HSTS, frame-deny, nosniff, no-referrer, and permissions policy headers
- Docker image runs as a non-root user
- Compose drops Linux capabilities and uses `no-new-privileges`
- Optional read-only container filesystem with `/tmp` tmpfs

## What this app cannot do

- It cannot manage add-on installs.
- It cannot remove catalogs from manifests.
- It cannot manage a manifest proxy.
- It cannot permanently save user accounts.
- It cannot change anything outside the Nuvio Home layout APIs used by this app.

## Quick start with Docker Compose

Clone the repo:

```bash
git clone https://github.com/rburns51/nuvio-reorder-home.git
cd nuvio-reorder-home
```

Create your environment file:

```bash
cp .env.example .env
nano .env
```

Required values:

```env
NUVIO_ANON_KEY=your_publishable_nuvio_or_supabase_anon_key
FLASK_SECRET=your_long_random_secret
NUVIO_REORDER_HOST=nuvio-reorder.example.com
```

Generate a strong Flask secret:

```bash
python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(64))
PY
```

Start the app:

```bash
docker compose up -d --build
docker logs -f nuvio-reorder
```

Check the service:

```bash
docker inspect --format='{{json .State.Health}}' nuvio-reorder
curl -I https://your-domain.example.com
curl https://your-domain.example.com/robots.txt
```

## Traefik setup

The included compose file expects an existing external Traefik network.

Set these in `.env`:

```env
NUVIO_REORDER_HOST=nuvio-reorder.example.com
TRAEFIK_NETWORK=traefik_proxy
TRAEFIK_CERTRESOLVER=myresolver
```

The container does not publish a host port by default. Traefik should reach it through the Docker network on port `7862`.

If you want local direct testing, temporarily add a ports section yourself:

```yaml
ports:
  - "7862:7862"
```

Then test:

```bash
curl http://127.0.0.1:7862/health
```

Remove the port mapping again before running it publicly.

## Environment variables

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `NUVIO_ANON_KEY` | Yes | empty | Publishable Nuvio/Supabase anon key. Never use a service role key. |
| `NUVIO_PROJECT_URL` | No | Nuvio project URL | Supabase project URL used for auth and RPC calls. |
| `FLASK_SECRET` | Yes | empty | Encrypts temporary session cookies. Use a long random value. |
| `NUVIO_REORDER_HOST` | Yes for Traefik | empty | Public hostname for the Traefik route. |
| `TRAEFIK_NETWORK` | No | `traefik_proxy` | External Docker network used by Traefik. |
| `TRAEFIK_CERTRESOLVER` | No | `myresolver` | Traefik TLS cert resolver name. |
| `PERMANENT_SESSION_LIFETIME_HOURS` | No | `8` | Max temporary login session duration. |
| `SESSION_COOKIE_SECURE` | No | `true` | Requires HTTPS for session cookie. |
| `SESSION_COOKIE_SAMESITE` | No | `Lax` | Cookie SameSite policy. `Strict` also works for most deployments. |
| `LOGIN_RATE_LIMIT_CALLS` | No | `5` | Login attempts allowed per window per IP and email hash. |
| `LOGIN_RATE_LIMIT_WINDOW` | No | `300` | Login rate window in seconds. |
| `LOGIN_LOCKOUT_SECONDS` | No | `900` | Lockout after repeated login attempts. |
| `API_RATE_LIMIT_CALLS` | No | `180` | API calls allowed per window per IP. |
| `API_RATE_LIMIT_WINDOW` | No | `60` | API rate window in seconds. |
| `NUVIO_API_DEBUG` | No | `false` | Keep false publicly. Debug logging redacts sensitive fields but should not be used publicly. |
| `PUBLIC_ERROR_DETAILS` | No | `false` | Keep false publicly. |
| `HEALTH_REVEALS_CONFIG` | No | `false` | Keep false publicly. |

## GitHub repo hygiene

Before pushing a public repo, run:

```bash
git status --short
git ls-files | egrep '(^\.env$|backup|bak|patch_|phase3|secret|key|token)' || true
grep -RIn --exclude-dir=.git --exclude='.env.example' \
  -E 'FLASK_SECRET=|NUVIO_ANON_KEY=.+|password|token|secret|service_role|SUPABASE_SERVICE' .
```

Expected results:

- `.env` should not be tracked by git.
- Patch scripts should not be tracked by git.
- Backup folders should not be tracked by git.
- Mentions of password or token in source code are normal when they describe login/session logic.
- Actual `FLASK_SECRET` values should only appear in your local ignored `.env`.

## Updating

```bash
git pull
docker compose up -d --build
docker logs --tail=100 nuvio-reorder
```

## Troubleshooting

### The public domain works but `curl http://127.0.0.1:7862` fails

That is expected when the compose file does not publish a host port. Traefik can still reach the container through the Docker network.

### Login fails immediately

Check that `.env` has a publishable anon key:

```bash
grep -q '^NUVIO_ANON_KEY=.' .env && echo present || echo missing
```

Do not use a service role key.

### Theme toggle does nothing

Hard refresh the browser after updating:

```text
Cmd + Shift + R
```

The theme preference is stored in browser localStorage under `nuvio-sync-theme`.

### I pasted `FLASK_SECRET` somewhere public

Rotate it immediately:

```bash
NEW_SECRET="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(64))
PY
)"
python3 - <<PY
from pathlib import Path
new_secret = """$NEW_SECRET"""
p = Path('.env')
lines = p.read_text().splitlines()
out = []
for line in lines:
    out.append('FLASK_SECRET=' + new_secret if line.startswith('FLASK_SECRET=') else line)
p.write_text('\n'.join(out) + '\n')
PY
docker compose up -d --build
```

Existing sessions will be invalidated, which is what you want after rotating the secret.
