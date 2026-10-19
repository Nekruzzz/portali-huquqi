-- Portali huquqi: schema follows the Procedure approved by Government Resolution No. 617
-- of 29.12.2018 (as amended 8.02.2020 No. 84, 28.08.2024 No. 478) and Articles 34-39 of
-- the Law "On normative legal acts" (No. 1414 of 30.05.2017).
-- Columns added after the first release are also applied to older databases by
-- migrate() in app.py.

CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Annex 1 = state_body, Annex 2 = legal_entity, Annex 3 = individual
    account_type   TEXT NOT NULL CHECK (account_type IN ('individual','legal_entity','state_body')),
    full_name      TEXT NOT NULL,
    position       TEXT,
    workplace      TEXT,
    org_name       TEXT,
    org_activity   TEXT,
    authority_basis TEXT,
    head_name      TEXT,
    head_phone     TEXT,
    tax_id         TEXT,
    address        TEXT,
    passport       TEXT,
    email          TEXT NOT NULL UNIQUE,
    phone          TEXT,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user','developer','admin')),
    status         TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('active','pending','blocked')),
    lang           TEXT,
    notify_email   INTEGER NOT NULL DEFAULT 1,
    reset_hash     TEXT,
    reset_expires  TEXT,
    admin_note     TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    approved_at    TEXT,
    approved_by    INTEGER REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL CHECK (kind IN ('plan','draft','act')),
    doc_type        TEXT,
    number          TEXT,
    doc_date        TEXT,
    title_tj        TEXT NOT NULL,
    title_ru        TEXT,
    developer_tj    TEXT,                  -- developer; for acts: adopting body
    developer_ru    TEXT,
    owner_id        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    related_id      INTEGER REFERENCES documents(id) ON DELETE SET NULL, -- draft -> plan, act -> draft
    summary_tj      TEXT,
    summary_ru      TEXT,
    body_tj         TEXT,
    body_ru         TEXT,
    details         TEXT NOT NULL DEFAULT '{}', -- JSON: notification fields (Art. 34) and RIA (Art. 36)
    affects_business INTEGER NOT NULL DEFAULT 0,
    dev_deadline    TEXT,
    adopt_deadline  TEXT,
    consult_start   TEXT,
    consult_end     TEXT,
    conclusion_tj   TEXT,
    conclusion_ru   TEXT,
    conclusion_date TEXT,
    outcome         TEXT CHECK (outcome IN ('proceed','adopted','rejected')),
    outcome_note_tj TEXT,
    outcome_note_ru TEXT,
    outcome_date    TEXT,
    -- adopted acts
    legal_status    TEXT CHECK (legal_status IN ('in_force','partly','repealed')),
    effective_date  TEXT,
    publication_tj  TEXT,                  -- official source of publication
    publication_ru  TEXT,
    mj_reg_number   TEXT,                  -- state registration with the Ministry of Justice (item 24)
    mj_reg_date     TEXT,
    returned_note   TEXT,                  -- notification returned to the developer (item 9)
    topic           TEXT,                  -- policy area, see TOPICS in i18n.py
    views           INTEGER NOT NULL DEFAULT 0,
    source_url      TEXT,
    published       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_documents_kind_date ON documents (kind, doc_date DESC);
CREATE INDEX IF NOT EXISTS idx_documents_consult ON documents (consult_end);

-- act X amends / repeals act Y
CREATE TABLE IF NOT EXISTS doc_links (
    from_id    INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    to_id      INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    link_type  TEXT NOT NULL CHECK (link_type IN ('amends','repeals')),
    PRIMARY KEY (from_id, to_id, link_type)
);
CREATE INDEX IF NOT EXISTS idx_links_to ON doc_links (to_id);

CREATE TABLE IF NOT EXISTS attachments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    stored_name   TEXT NOT NULL UNIQUE,
    original_name TEXT NOT NULL,
    size          INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS comments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    article_ref  TEXT,                     -- article / RIA section the proposal refers to
    body         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'visible' CHECK (status IN ('visible','hidden')),
    decision     TEXT CHECK (decision IN ('accepted','partial','rejected')),
    response     TEXT,
    responded_at TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_comments_doc ON comments (document_id, created_at);

CREATE TABLE IF NOT EXISTS votes (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    value       INTEGER NOT NULL CHECK (value IN (-1, 1)),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (document_id, user_id)
);

-- document_id set: follow one document; otherwise: new documents in a register (optionally one developer)
CREATE TABLE IF NOT EXISTS subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    kind        TEXT,
    developer   TEXT,
    reminded    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_subs_doc ON subscriptions (document_id);
CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions (user_id);

CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    to_email   TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','sent','failed','logged')),
    attempts   INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at    TEXT
);

CREATE TABLE IF NOT EXISTS news (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title_tj   TEXT NOT NULL,
    title_ru   TEXT,
    body_tj    TEXT,
    body_ru    TEXT,
    published  INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','answered','closed')),
    answer      TEXT,
    answered_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action     TEXT NOT NULL,
    target     TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
