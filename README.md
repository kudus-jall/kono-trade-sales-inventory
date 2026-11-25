# Kono Trade

A Flask and PostgreSQL-ready starting point for the Sierra Leone trading-business management system. The existing `aes_gcm_assignment_demo.py` file is unrelated and has been left unchanged.

## Run locally with SQLite

1. Install Python 3.10 or later.
2. In this folder, create a virtual environment and install dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

3. Keep `DATABASE_URL` unset to use a local SQLite database. Set a persistent secret key before running the app:

   ```powershell
   $env:SECRET_KEY = "replace-this-with-a-long-random-secret"
   python app.py
   ```

4. Open http://127.0.0.1:5000 and create the first Owner/Admin account.

The local SQLite database is created in `instance\sales.db`; set `SALES_APP_DATABASE` to use another path. Local SQLite data is never copied into PostgreSQL. If `DATABASE_URL` is set, the app uses PostgreSQL instead of opening the SQLite file.

## Set up a new Supabase PostgreSQL database

1. Create a **new Supabase project**. This app does not import, migrate, or inspect the existing SQLite database.
2. In the Supabase Dashboard, open **Connect** and copy a PostgreSQL connection string. Keep its database password private. For app hosting on an IPv4-only network, Supabase recommends the shared **Session pooler**; direct connections are suitable where IPv6 is available. The app disables prepared statements to remain compatible with poolers.
3. Set `DATABASE_URL` in the process environment or hosting provider's secret settings. Use the SSL-enabled URI from Supabase. Never commit it, place it in a tracked file, or paste it into logs. Reserved characters in the password must be percent-encoded in the URI.
4. Install dependencies and start the app. At startup it creates the current tables and indexes idempotently in the PostgreSQL connection's active schema (normally `public`). A transaction-scoped advisory lock serializes schema initialization across multiple Gunicorn workers. No sample business or real data is inserted.

For local PowerShell testing against the new project, paste the connection string at the prompt so it is not echoed, then start the app:

```powershell
$env:DATABASE_URL = Read-Host "Supabase PostgreSQL connection URI"
python app.py
```

Remove the variable when finished with `Remove-Item Env:DATABASE_URL`. Keep `DATABASE_URL` unset for local SQLite development. The backend continues to enforce business scoping and owner/attendant permissions; Supabase Row Level Security is not configured, so the database credential must remain server-side.

## Render hosting

Install dependencies from `requirements.txt` and use this Render start command:

```text
gunicorn --bind 0.0.0.0:$PORT app:app
```

Configure `DATABASE_URL` and a long random `SECRET_KEY` in Render's environment settings. Also set `COOKIE_SECURE=true` when serving the public app over HTTPS. Keep provider credentials in Render's secret environment settings, not in source control. Gunicorn is the production WSGI server; `python app.py` is only for local development.

Render's local filesystem may be ephemeral. Business logos currently live under `instance\uploads`; configure persistent storage or move uploads to private object storage before relying on logos in a hosted service. This repository does not deploy the app, create Supabase resources, configure secrets, or connect to a hosted database.

Run the automated workflow checks with `python -m unittest discover -s tests -v`.

## Included in this MVP

- Business registration, sign-in, password hashing, CSRF protection, and owner/attendant roles.
- Business-scoped products, customers, suppliers, purchases, sales, customer payments, and expenses.
- Inventory updates on purchases and sales; purchase entries update weighted average unit cost.
- Product-specific purchase/sale units convert into a stored base-stock unit (for example 1 bag = 50 kg). Conversion factors are snapshotted on transactions; purchase cost is entered per selected purchase unit and alternate selling prices are derived from the base-unit price.
- Credit balances, unique transaction references, printable receipts, basic reports, and an owner-only activity log.
- Split sale payments recorded by method, with payment details printed on the sale receipt.
- Supplier debt payments with outstanding-balance checks, payment history, and printable receipts.
- Customer debt payments by owners and attendants, with outstanding-balance checks, duplicate-submit protection, and role-scoped balance visibility.
- Owner-recorded sales returns with quantity limits, stock-condition handling, debt/refund adjustments, replacement stock/cost, receipts, and audit entries.
- Owner sale voids with required reasons, preserved sale/audit history, base-stock restoration and reversal ledger entries for sale payments already received. Sales with return records cannot be voided.
- Supplier returns tied to original purchases with quantity and stock checks, refund/debt-adjustment/replacement handling, and audit history.
- One-time email password recovery with expiry, per-address throttling, and session invalidation.
- Owner-only CSV exports for sales, purchases, sales and supplier returns, supplier payments, inventory, customer/supplier balances, expenses, and payments; historical datasets accept a start/end date range, exports neutralize spreadsheet formulas, and current inventory/balances are explicitly snapshots.
- Persistent in-app notifications for sale confirmations, low-stock transitions, unusual sale-size prompts, and owner-only overdue customer/supplier reminders. Notifications are private to each user and no external delivery service is used.
- Owner-editable business identity (name, type, address, phone, and validated PNG/JPEG logo) shown on receipts. Owners can select the business currency before any financial transaction exists; the backend and form lock currency changes once transactions have been recorded.
- Printable and downloadable PDF receipts for sales, customer payments, supplier payments, and customer/supplier returns, with business identity and payment details.
- Encrypted AES-GCM database backups: SQLite locally and PostgreSQL custom-format dumps when `DATABASE_URL` is configured.
- Read-only local rules-based business insights, plus an optional owner-consented OpenAI-compatible narrative provider; the app computes and displays authoritative figures and AI responses containing numeric claims are discarded.
- Low-stock overview and a responsive layout.

## Not included yet

This is an initial usable MVP, not the complete SRS. Historical CSV datasets can be filtered by dates from the Reports page; inventory and customer/supplier balances are current snapshots and do not support historical filtering. Amounts in CSVs are integer minor units.

Notifications are in-app only. Sale confirmations are private to the selling user; operational low-stock alerts are visible to owners and attendants; unusual-sale prompts and overdue balance reminders are owner-only. Overdue reminders are refreshed when the Notifications page is opened and use the overall customer/supplier account balance, not invoice-level payment allocation, so they are prompts rather than authoritative due notices. Due dates are optional, and no background scheduler, email, SMS, or push delivery is configured.

Supplier return refunds are capped by recorded payments to the supplier; debt adjustments reduce the supplier payable, while replacements do not. Return financial treatment uses prorated original transaction values and the ledger rules described on the workflow screens; reconcile before relying on reports for accounting. Password recovery requires an SMTP service and a trusted public base URL; configure `SMTP_HOST`, `SMTP_PORT` (default 587; port 465 uses implicit TLS), `SMTP_FROM`, optional `SMTP_USERNAME`/`SMTP_PASSWORD`, and `PUBLIC_BASE_URL`. Before hosting real business data, complete security and privacy review, enforce HTTPS, configure secure key management and tested off-site backups, use a managed production database, and conduct a deployment and accounting review. This MVP is not production-ready.

### Currency, business settings, and receipts

Set the business name, type, address, phone, logo, and preferred currency in the owner-only **Settings** page. Currency can be changed only while the business has no sales, purchases, expenses, payments, or returns. Once a financial transaction exists, the server rejects currency changes and the form explains the lock; no values are converted or relabeled. To try another currency in a demo after recording transactions, create a fresh business workspace. Logos are limited to PNG/JPEG uploads up to 2 MB; images are decoded and normalized before storage. Receipt pages support browser printing and downloadable PDFs. Install the added image/PDF dependencies with the normal `python -m pip install -r requirements.txt` setup command.

### Optional external AI insights

The provider is disabled by default. To make it available, set these **process environment variables** before starting Flask; keep the API key in a secret manager or protected shell prompt, never in source code, a committed `.env` file, or GitHub:

```powershell
$env:AI_PROVIDER = "openai-compatible"
$env:AI_BASE_URL = "https://api.openai.com/v1"
$env:AI_MODEL = "provider-model-name"
$secureKey = Read-Host "AI provider API key" -AsSecureString
$env:AI_API_KEY = [System.Net.NetworkCredential]::new("", $secureKey).Password
python app.py
```

In the Owner/Admin **Settings** page, read the data-sharing notice, explicitly enable AI, and check the consent box. Configuration alone does not enable AI or send data. After consent, an authenticated user must explicitly choose **Ask with AI**; ordinary local insights, reports, and exports never call the provider. Disabling AI in Settings prevents further requests.

The replaceable adapter currently supports OpenAI-compatible HTTPS chat-completions endpoints. `AI_PROVIDER`, `AI_BASE_URL`, `AI_MODEL`, and `AI_API_KEY` configure it; credentials are read from the environment and are not stored in the database. An AI request sends only an allow-listed topic, selected date period, currency, and application-computed aggregate sales/stock metrics. Owners' requests may also include combined customer and supplier balance totals, recorded expenses, cost, and estimated profit. Attendant requests contain only that attendant's sales totals plus shared stock counts. The request excludes the user's free-text question, names, contact details, account-level data, product names, transaction rows, IDs, and credentials.

All exact figures are calculated and rendered by the app. The provider is instructed to produce narrative only; the app rejects responses containing digits, number words, percentages, or currency figures. Qualitative explanations can still be wrong, and should be treated as uncertain rather than advice. Sales trends use recorded return-adjusted totals; stock alerts are counts, not item-level recommendations. No data is sent when configuration is absent, consent is disabled, the question is outside supported topics, or the user's role lacks permission. Provider pricing, data retention, and processing terms vary; review the selected provider's terms and costs before enabling it. This integration is optional and does not affect non-AI reports.

## Manual PostgreSQL backups for Supabase Free

Supabase Free does not include scheduled Dashboard database backups. Supabase recommends exporting Free-tier data regularly with PostgreSQL tools and keeping an off-site copy. The app backup script uses `pg_dump` and `pg_restore` from the PostgreSQL client tools; install those tools on the computer that will create the backup.

Set `DATABASE_URL` from the Supabase Dashboard's **Connect** dialog and load a 32-byte URL-safe base64 `BACKUP_ENCRYPTION_KEY` from a password manager or secret manager. Keep that key separately from the `.kbackup` files; losing it makes encrypted backups unrecoverable. Do not put either value in a project `.env` file or Git. For `pg_dump` backups, use a direct PostgreSQL connection as recommended by Supabase for backup/restore tools; the app server can use the session pooler where needed. Then create a manual backup:

```powershell
$env:DATABASE_URL = Read-Host "Supabase PostgreSQL connection URI"
$env:BACKUP_DIR = Join-Path $env:USERPROFILE "KonoTradeBackups"
# Load BACKUP_ENCRYPTION_KEY into this process from your password manager/secret manager.
python .\backup_db.py backup
```

The script writes encrypted `.kbackup` files to `BACKUP_DIR` (30 retained by default; change `BACKUP_RETENTION_COUNT` if needed). PostgreSQL backups are validated with `pg_restore --list` before encryption. The tool does not send the dump to a third party or configure automatic scheduling; copy the resulting encrypted backup to a separate protected location.

To restore a PostgreSQL backup, use a **new, empty target database** and set its `DATABASE_URL` and encryption key in the process environment. The exact database name from the target URI is required as an extra confirmation:

```powershell
python .\backup_db.py restore "C:\path\to\kono-trade-YYYYMMDD-HHMMSS.kbackup" --confirm-target "postgres"
```

PostgreSQL restore does not drop existing objects; it is intended for a new, empty target and fails on conflicts. SQLite restore remains local-only, validates integrity and foreign keys, and saves an encrypted safety backup before replacement. Test backup and restore procedures on a separate project before relying on them. For Supabase's current Free-tier backup guidance, see [Supabase Database Backups](https://supabase.com/docs/guides/platform/backups).

## Checks

```powershell
python -m unittest discover -s tests -v
```
