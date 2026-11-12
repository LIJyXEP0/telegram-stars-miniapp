# Stars & Premium standalone checkout

The crypto checkout is a standalone website. The Telegram bot's Mini App only shows a user's linked order history and support; it has no product checkout or crypto-payment controls. Telegram requires XTR for digital goods sold inside Telegram apps, so do not add crypto checkout to the bot or Mini App.

## Current safety mode

The application starts in `DRY_RUN=true`. It accepts no real payment and uses a deterministic mock fulfillment provider. The local-only simulation route is restricted to loopback. Production startup is intentionally refused until the Fragment quote, recipient eligibility, delivery, reconciliation, and status contracts have been verified with a seedless signing method. No mnemonic is included in this code or sent by it.

The old root-level `bot.py`/`fragment.py` files remain local for migration reference and are excluded from Git. Do not run the legacy `bot.py`: its previous Fragment adapter sent `FRAGMENT_WALLET_SEED` in a third-party HTTP request. Use `python -m app.main` for this codebase.

## Setup on Windows PowerShell

1. Open this folder in VS Code.
2. Create `.venv`: `py -3.11 -m venv .venv`
3. Activate it: `Set-ExecutionPolicy -Scope Process Bypass; .\.venv\Scripts\Activate.ps1`
4. Install packages: `python -m pip install -r requirements.txt`
5. Keep the current `.env` private. Ensure it contains `BOT_TOKEN` and `OWNER_ID`; add values from `.env.example` as needed. Do not put a mnemonic in a website form or commit it.
6. Start the test app: `python -m app.main`
7. Open `http://127.0.0.1:8080`. Create a mock order and use its local simulation link. No funds are moved in dry-run mode.
8. Run checks: `pytest -q`

## Docker

Copy or merge `.env.example` into a private `.env`, set `BOT_TOKEN` and `OWNER_ID`, then run `docker compose up --build -d`. The service binds port 8080, stores its separate database under `./data`, and creates daily SQLite backups under `./backups`. The local sample configuration remains dry-run.

For deployment, put the site behind HTTPS, set `PUBLIC_BASE_URL` to its own domain, restrict admin access, configure off-server encrypted backups, and monitor the container. Do not expose the debug simulation route; it only works from loopback and only when `DRY_RUN=true`.

## Telegram Mini App: status and support only

The static Mini App is in `miniapp/` and is served by the Python app at `/miniapp/`. It authenticates each API request using Telegram's signed `initData`. New checkout pages show an order number and a secret tracking code; the buyer enters both in the Mini App to bind the order to their Telegram account. The code is stored only as a SHA-256 hash. Older orders created before this migration have no tracking code and cannot be claimed through the Mini App.

Set `MINI_APP_URL` in `.env` to the public HTTPS URL ending in `/miniapp/`, then restart the bot. At startup it sets the bot's menu button to “Мои заказы”; `/start` also shows the Mini App button. Set `SUPPORT_USERNAME` to the support account username for the support button.

GitHub can store the source and GitHub Pages can publish the static frontend, but Pages cannot run Python, SQLite, the bot, or payment watchers. To use Pages, set `miniapp/config.js` `apiBase` to the separate backend's HTTPS origin, then set `MINI_APP_URL` to the Pages URL and `MINI_APP_ORIGIN` to its origin (for a project Pages site, the origin is `https://<account>.github.io`, without the repository path). The Python app and database must remain on an always-on server; HTTPS and this exact CORS origin are required.

This repository includes `.github/workflows/pages.yml`, which publishes only the `miniapp/` directory after a push to `main` or `master`. After creating the GitHub repository, choose **Settings → Pages → Build and deployment → Source → GitHub Actions**. The first deployment appears under **Actions**; GitHub then shows the Pages URL in **Settings → Pages**. Before publishing, set `miniapp/config.js` `apiBase` to the backend HTTPS origin. Do not upload `.env`; it is ignored by Git.

## Configurable settings

Edit `config.yml` to change products, durations, margin, fee, supported payment assets, expiry, grace period, tolerances, and quantity limits. Secrets and infrastructure values belong in `.env`, never in YAML or source control.

Incoming USDT monitoring uses TronGrid's confirmed TRC-20 account-transfer endpoint and filters the configured token contract. TON monitoring uses TON Center API v3 account transactions; it normalizes standard/raw addresses and reads the transfer comment. Do not change the mainnet USDT contract without independently verifying it.

## Test checklist

- `pytest -q`: Decimal pricing, legal state changes, payment classification, SQLite transition events, concurrent active USDT amount allocation, duplicate transfer idempotency, and mock fulfillment idempotency.
- Open the dry-run storefront and create one Stars and one Premium order.
- Simulate payment from localhost and confirm the order becomes completed after the background worker runs.
- Restart the app and confirm persisted orders remain readable.
- Confirm a remote request to `/dev/simulate/<id>` returns 404.
- Do not run a real customer payment until all open questions below are closed.

## Open questions before production

1. The existing `api-stars.duckdns.org` adapter sends the wallet seed to the service. It is intentionally not used by the new app. The public `fragment-api-lib` v3 documentation describes local signing, but the live quote/eligibility response schema, dependency provenance, wallet type, KYC/cookie requirements, price limits, and reconciliation behavior must be reviewed and tested before enabling it. The app must remain fail-closed until then.
2. The product contract requires a funded hot wallet distinct from the receiving wallets, per-order TON/USDT cost ceilings, fee reserve, and verified recipient quotes. Provide safe configured limits and the intended Fragment account/method; never share a seed in chat.
3. A public website needs its final domain, TLS termination, data location, retention policy, legal terms, and an off-server backup target.
4. Buyers must retain the order number and tracking code from checkout to link new orders in the Mini App. The bot does not yet send automatic buyer notifications; status refreshes in the Mini App while open.
5. Admin actions for late-payment approval, explicit refund recording, user bans, maintenance, and safe retry need policy decisions and operational tests before live sales.
