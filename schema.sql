-- VaultSwap PostgreSQL Schema (Neon Serverless PostgreSQL)

-- 1. Users (Google OAuth)
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    google_sub VARCHAR(255) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    display_name VARCHAR(100) NOT NULL,
    avatar_url TEXT,
    discord_handle VARCHAR(100),
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Canonical Card & Scryfall Cache
CREATE TABLE IF NOT EXISTS cards (
    scryfall_id UUID PRIMARY KEY,
    oracle_id UUID NOT NULL,
    name VARCHAR(255) NOT NULL,
    set_code VARCHAR(10) NOT NULL,
    collector_number VARCHAR(20) NOT NULL,
    set_name VARCHAR(255),
    image_uri_normal TEXT,
    image_uri_small TEXT,
    mana_cost VARCHAR(100),
    type_line VARCHAR(255),
    usd_price NUMERIC(10, 2),
    usd_foil_price NUMERIC(10, 2),
    scryfall_updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cards_oracle_id ON cards(oracle_id);
CREATE INDEX IF NOT EXISTS idx_cards_name_lower ON cards(LOWER(name));

-- 3. User Binders (Private by default, selective public flags)
CREATE TABLE IF NOT EXISTS binder_items (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scryfall_id UUID NOT NULL REFERENCES cards(scryfall_id),
    quantity INTEGER NOT NULL DEFAULT 1,
    finish VARCHAR(20) NOT NULL DEFAULT 'nonfoil', -- 'nonfoil', 'foil', 'etched'
    condition VARCHAR(10) DEFAULT 'NM',

    -- Visibility & Trade Status
    status VARCHAR(20) NOT NULL DEFAULT 'private', -- 'private', 'for_trade', 'meeple_bound'

    -- Point-in-Time Mighty Meeple Snapshot (Populated ONLY on tag)
    mm_in_stock BOOLEAN,
    mm_retail_price NUMERIC(10, 2),
    mm_buylist_cash NUMERIC(10, 2),
    mm_buylist_credit NUMERIC(10, 2),
    mm_checked_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_binder_user_recent ON binder_items(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_binder_pod_feed ON binder_items(status, updated_at DESC) WHERE status != 'private';

-- 4. Google Drive Incremental Imports
CREATE TABLE IF NOT EXISTS drive_imports (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    drive_file_id VARCHAR(128) NOT NULL,
    file_name VARCHAR(255) NOT NULL,
    mime_type VARCHAR(100),
    drive_md5 VARCHAR(64),
    drive_modified_time TIMESTAMPTZ,
    date_column VARCHAR(100),
    max_card_created_at TIMESTAMPTZ,
    boundary_fingerprints TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    phase VARCHAR(40),
    rows_scanned INTEGER NOT NULL DEFAULT 0,
    rows_new INTEGER NOT NULL DEFAULT 0,
    rows_imported INTEGER NOT NULL DEFAULT 0,
    cards_imported INTEGER NOT NULL DEFAULT 0,
    rows_skipped_no_date INTEGER NOT NULL DEFAULT 0,
    unresolved TEXT,
    error TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_drive_imports_user_id ON drive_imports(user_id);
CREATE INDEX IF NOT EXISTS idx_drive_imports_user_watermark ON drive_imports(user_id, max_card_created_at DESC);
