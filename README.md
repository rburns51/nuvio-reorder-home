
# Nuvio Reorder Home Only

Standalone, temporary-login Reorder Home tool for Nuvio.

## What it does

- Logs into Nuvio with a temporary encrypted browser cookie.
- Does not save Nuvio passwords or durable account records.
- Loads profiles from the logged-in Nuvio account.
- Loads TV or Mobile Home layout rows for a selected profile.
- Reorders rows with drag/drop, arrows, or unlocked position numbers.
- Hides/shows rows through Home layout visibility only.
- Removes orphan collection Home rows.
- Saves to TV only, Mobile only, or TV + Mobile.
- Can apply a Profile 1 layout to profiles that inherit Profile 1 add-ons.

## Phase 3 public hardening

Included hardening:

- Standalone local `.env` only, not the main NuvioSync environment file.
- Whitelisted environment variables only.
- `FLASK_PRODUCTION=true` and `NUVIO_API_DEBUG=false` by default.
- Encrypted HttpOnly session cookie.
- Secure cookie in production.
- CSRF token required for authenticated API calls.
- Generic login errors.
- Login and API rate limiting.
- No-store browser cache headers.
- `X-Robots-Tag: noindex, nofollow, noarchive`.
- `/robots.txt` blocks indexing.
- CSP, HSTS, frame-deny, no-sniff, no-referrer, and permissions-policy headers.
- Public `/health` endpoint does not reveal config by default.

## Quick start

```bash
cd reorder-home-only
cp .env.example .env
nano .env

docker compose up -d --build
docker logs -f nuvio-reorder
```

Health check:

```bash
curl -fsS http://127.0.0.1:7862/health && echo
curl -I https://nuvio-reorder.sedation.us
```

## Required `.env` values

```env
NUVIO_ANON_KEY=your_publishable_anon_key
FLASK_SECRET=a_long_random_secret
```

Generate a safe Flask secret:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
```

## Security model

The app receives the user's Nuvio email/password only during login, forwards them to Nuvio/Supabase auth, and does not store the password. After login, the app keeps a temporary encrypted session cookie in the user's browser. Logging out clears that cookie.

The server does not keep a database of users, targets, passwords, or profile selections.
