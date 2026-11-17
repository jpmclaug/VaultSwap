# VaultSwap 🔄🃏

**VaultSwap** is a mobile-first MTG pod binder, trade feed, and Mighty Meeple buylist snapshot application. Built for speed when standing around a local game store, VaultSwap lets playgroups share trade binders privately while tracking cards bound for the local store buylist.

---

## ⚡ Key Features

- **Private by Default**: All imported cards default to `private`. You choose what cards to share with your pod.
- **Row-Level Security**: Your personal binder is scoped strictly to your account; only cards explicitly tagged `for_trade` or `meeple_bound` appear in the shared Pod Feed.
- **HTMX One-Tap Toggles**: Flip cards between **Private**, **Trade**, and **Meeple** with zero full-page reloads.
- **Light-Touch on Mighty Meeple**:
  - **Zero Meeple Traffic on Import**: ManaBox CSV uploads and quick-list pastes only hit Scryfall's `/cards/collection` batch endpoint (up to 75 cards per request).
  - **On-Tag Snapshots**: Mighty Meeple's retail stock/price and buylist cash/credit quotes are fetched *only* when a card is tagged for trade/buylist.
  - **24-Hour Pod Cache Sharing**: If any pod member tagged the same printing within 24 hours, the cached quote is reused instantly with zero external HTTP traffic.
  - **Sequential Rate-Limit Worker**: Bulk status changes queue live Mighty Meeple requests sequentially with polite 1.5s–2.0s jittered delays.
- **Admin Invite-Only Access**: Admins manage the pod allowlist at `/admin/users` (bootstrapped via `ADMIN_EMAILS`).
- **Incremental Google Drive Sync**: Pick large ManaBox CSVs from Google Drive via Google Picker (`drive.file` scope). Streams rows without loading the file into memory, skips unchanged files by MD5 checksum, and tracks each user's newest card created date so subsequent syncs only add newly created cards.
- **Cross-Printing Duplicate Spotting**: Groups by `oracle_id` across different sets (e.g. 3 copies of *Rhystic Study* across 3 printings) so you can easily spot tradeable extras.
- **Pod Feed Prioritization**: Shared pod cards are ordered with **Meeple Bound** first, ensuring friends see cards before they leave the playgroup forever.

---

## 🛠 Tech Stack

- **Backend**: Python 3.12+ / Flask / SQLAlchemy (Neon Serverless PostgreSQL with local SQLite fallback)
- **Frontend**: Mobile-first Jinja2 templates + HTMX + Tailwind CSS
- **Authentication**: Authlib & Google OAuth 2.0 (with local dev login mode)
- **APIs & Scraping**: Scryfall API batch collection & Mighty Meeple / BinderPOS portal

---

## 🚀 Getting Started

### 1. Prerequisites
- Python 3.12+ (or a virtual environment with packages from `requirements.txt`)
- (Optional) Neon Serverless PostgreSQL database & Google OAuth credentials

### 2. Environment Configuration
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Configure your credentials in `.env`:
```env
SECRET_KEY=your-secret-key-here
PORT=5055

# Neon Serverless PostgreSQL
DATABASE_URL=postgresql://user:pass@ep-sample.us-east-1.aws.neon.tech/neondb?sslmode=require
DATABASE_URL_POOLED=postgresql://user:pass@ep-sample-pooler.us-east-1.aws.neon.tech/neondb?sslmode=require
NEON_AUTH_BASE_URL=

# Google OAuth 2.0 Credentials
GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret
APP_BASE_URL=

# Set false in production
DEV_LOGIN_ENABLED=true

# Admin & Invite-Only Access Control
ADMIN_EMAILS=jpmclaug@gmail.com
INVITE_ONLY=true

# Google Drive Picker (optional; requires Drive API & Picker API enabled in GCP)
GOOGLE_API_KEY=
GOOGLE_CLOUD_PROJECT_NUMBER=
```

### 3. Run Locally

- **Windows (Double-Click)**: Run `start.bat`
- **PowerShell**:
  ```powershell
  .\start_app.ps1
  ```
- **CLI / Linux / macOS**:
  ```bash
  python app.py
  ```

Open [http://localhost:5055](http://localhost:5055) in your mobile browser or developer tools responsive view.

To stop the server: double-click `stop.bat` or run `.\stop_app.ps1`.

---

## 🧪 Testing

Run the full pytest suite:
```bash
pytest -v
```

All 18 tests verify row-level scoping, 75-card Scryfall collection batching, zero Mighty Meeple traffic on import, 24-hour pod cache sharing, sequential rate-limit guard, duplicate filtering, invite-only admin access, demo removal, and incremental Google Drive streaming imports.

---

## 🐳 Production Deployment (Google Cloud Run)

VaultSwap includes a production-ready `Dockerfile` optimized for Google Cloud Run:

```bash
gcloud run deploy vaultswap \
  --source . \
  --platform managed \
  --region us-east1 \
  --allow-unauthenticated \
  --set-env-vars DATABASE_URL="postgresql://...",DATABASE_URL_POOLED="postgresql://...-pooler..."
```
