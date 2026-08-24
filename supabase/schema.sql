-- Schema voor Supabase. Plak dit in de SQL Editor van je project.
-- De applicatie maakt deze tabellen ook zelf aan bij de eerste start;
-- dit bestand is er zodat je kunt zien wat er staat en het kunt versioneren.

CREATE TABLE IF NOT EXISTS leads (
    id            BIGSERIAL PRIMARY KEY,
    osm_type      TEXT NOT NULL,
    osm_id        TEXT NOT NULL,
    name          TEXT NOT NULL,
    niche         TEXT,
    street        TEXT,
    housenumber   TEXT,
    postcode      TEXT,
    city          TEXT,
    phone         TEXT,
    email         TEXT,
    website       TEXT,
    opening_hours TEXT,
    lat           DOUBLE PRECISION,
    lon           DOUBLE PRECISION,
    source        TEXT,
    first_seen    TIMESTAMPTZ,
    raw           TEXT,
    UNIQUE (osm_type, osm_id)
);
CREATE TABLE IF NOT EXISTS audits (
    id             BIGSERIAL PRIMARY KEY,
    lead_id        BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    checked_at     TIMESTAMPTZ,
    reachable      INTEGER, final_url TEXT, status_code INTEGER, https INTEGER,
    mobile_ready   INTEGER, title TEXT, description TEXT, load_ms INTEGER,
    html_bytes     BIGINT, has_contact INTEGER, copyright_year INTEGER,
    platform       TEXT, score INTEGER, segment TEXT, findings TEXT,
    UNIQUE (lead_id)
);
CREATE TABLE IF NOT EXISTS demos (
    id         BIGSERIAL PRIMARY KEY,
    lead_id    BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    slug       TEXT NOT NULL,
    path       TEXT,
    url        TEXT,
    html       TEXT,
    created_at TIMESTAMPTZ,
    UNIQUE (lead_id)
);
CREATE TABLE IF NOT EXISTS outreach_log (
    id         BIGSERIAL PRIMARY KEY,
    lead_id    BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    channel    TEXT NOT NULL, template TEXT, to_addr TEXT, subject TEXT, body TEXT,
    status     TEXT NOT NULL, error TEXT, send_after TIMESTAMPTZ, sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS runs (
    id          BIGSERIAL PRIMARY KEY,
    started_at  TIMESTAMPTZ, finished_at TIMESTAMPTZ, trigger TEXT, status TEXT,
    discovered  INTEGER DEFAULT 0, audited INTEGER DEFAULT 0, demos INTEGER DEFAULT 0,
    queued      INTEGER DEFAULT 0, sent INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
    error       TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS suppression (value TEXT PRIMARY KEY, reason TEXT, added_at TIMESTAMPTZ);

CREATE INDEX IF NOT EXISTS idx_audits_score ON audits(score DESC);
CREATE INDEX IF NOT EXISTS idx_outreach_lead ON outreach_log(lead_id, created_at);
CREATE INDEX IF NOT EXISTS idx_outreach_status ON outreach_log(status, send_after);
CREATE INDEX IF NOT EXISTS idx_demos_slug ON demos(slug);

-- Row Level Security: de tabellen worden alleen benaderd door de serverless
-- functies met de databaserol, nooit rechtstreeks vanuit een browser. Zet RLS
-- aan zonder policies, dan kan de anon-sleutel er sowieso niet bij.
ALTER TABLE leads        ENABLE ROW LEVEL SECURITY;
ALTER TABLE audits       ENABLE ROW LEVEL SECURITY;
ALTER TABLE demos        ENABLE ROW LEVEL SECURITY;
ALTER TABLE outreach_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs         ENABLE ROW LEVEL SECURITY;
ALTER TABLE meta         ENABLE ROW LEVEL SECURITY;
ALTER TABLE suppression  ENABLE ROW LEVEL SECURITY;
