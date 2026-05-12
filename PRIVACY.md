# Privacy Policy

## What this app stores

The app stores a temporary encrypted browser cookie after login. That cookie contains the Nuvio access token, refresh token, user id, email, issue time, and CSRF token.

The app does not store a durable database of users, accounts, passwords, targets, profile selections, or Home layouts.

## What this app sees

During login, the app receives the Nuvio email and password and forwards them to Nuvio/Supabase auth. The password is not saved by this app.

While using the tool, the app requests Nuvio profiles, add-ons, collections, and Home layout settings needed to display and save the reorder page.

## Logs

Production logs are intended to avoid secrets. API debug logging is disabled by default.

Keep `NUVIO_API_DEBUG=false` on public deployments.

## Cookies

The session cookie is HttpOnly, encrypted, and marked Secure in production. Logging out clears the cookie.

## Third-party requests

The default page uses Bootstrap Icons from jsDelivr. If you do not want any third-party CDN requests, vendor the icon files locally or remove the icon stylesheet from `templates/index.html`.

## Self-hosting

If you do not want to trust someone else's hosted copy with your Nuvio login request, self-host this repo.
