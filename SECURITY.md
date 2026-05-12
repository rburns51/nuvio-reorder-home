# Security Policy

## Supported version

The `main` branch is the supported version.

## Reporting security issues

Please do not post sensitive vulnerabilities publicly before the maintainer has time to respond.

Open a GitHub issue with a high-level description, or contact the maintainer privately if you have a practical exploit or credential exposure concern.

## Security model

Nuvio Reorder Home is a temporary-session utility. It does not maintain a durable account database.

During login, the app receives the user's Nuvio email and password and forwards them to Nuvio/Supabase auth. The password is not written to disk or saved in a database.

After successful login, the app stores the Nuvio access token and refresh token in an encrypted, HttpOnly browser cookie. The cookie is encrypted using `FLASK_SECRET`, marked `Secure` in production, and protected by SameSite.

Authenticated write routes require a CSRF token.

## Public hosting recommendations

For public hosting, use:

- HTTPS only
- A long random `FLASK_SECRET`
- `FLASK_PRODUCTION=true`
- `NUVIO_API_DEBUG=false`
- `PUBLIC_ERROR_DETAILS=false`
- `HEALTH_REVEALS_CONFIG=false`
- `SESSION_COOKIE_SECURE=true`
- No published Docker host port unless you know why you need it
- A reverse proxy such as Traefik or Caddy
- Docker secret hygiene, including no parent project `.env` file

## Known trust limitation

A hosted copy of this app receives Nuvio credentials during login. That is unavoidable for password based login.

Users who do not want to trust a hosted copy should self-host the repo.

## Out of scope

This app does not attempt to manage Nuvio add-on installation, manifest proxy state, or manifest level catalog removal.
