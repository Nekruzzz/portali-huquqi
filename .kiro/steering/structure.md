---
inclusion: always
---
# Where things are

All application code is in `portali-huquqi/`.

| Path | What it holds |
|---|---|
| `app.py` | The whole Flask app: config, database and migrations, search (FTS5), routes for public pages, accounts, management panel, API, feeds, old-site redirects, CLI commands |
| `schema.sql` | Database tables (`documents`, `document_versions`, `doc_links`, `doc_refs`, `attachments`, `comments`, `votes`, `subscriptions`, `outbox`, `news`, `messages`, `settings`, `audit_log`, `users`) |
| `i18n.py` | All interface strings (Tajik, Russian), document types, RIA and plan field definitions, topics |
| `legaltext.py` | HTML legal texts: sanitising, headings and anchors, links to old-site acts, plain text, comparison of wordings, Word export |
| `legacy_import.py` | Import of old-portal records from JSON lines (`flask --app app import-legacy FILE`) |
| `tools/fetch_old_portal.py` | Fallback copier of the old site's public pages into that JSON-lines format |
| `tools/verify_migration.py` | Compares a sample of imported acts with the old site (title, number, full text) |
| `mailer.py` | E-mail queue and SMTP delivery |
| `seed.py`, `seed_texts.json` | Demo content; full official texts of the real acts in the demo |
| `templates/` | Jinja pages; `base.html` layout, `_macros.html` (cards, badges, pager, `csrf()`), `_icons.html`, `_filters.html`, `document.html` (document page), `manage/` (admin and developer panel), `account/` |
| `public/static/` | `style.css`, `app.js`, favicon (served by Flask locally, by the CDN on Vercel) |
| `tests/test_portal.py` | End-to-end tests with Flask's test client |
| `Dockerfile`, `docker-compose.yml`, `Caddyfile`, `.env.example`, `backup.sh`, `restore.sh` | Production deployment |
| `vercel.json` | Demo deployment |

## Main routes
- Public: `/`, `/plans`, `/drafts`, `/acts`, `/search`, `/doc/<id>` (also `.txt`, `.docx`, `/compare`),
  `/topics`, `/stats`, `/developers`, `/news`, `/about`, `/contact`.
- Accounts: `/register`, `/login`, `/account`. Management: `/manage/...` (developer and admin).
- Open data: `/api/documents`, `/api/documents/<id>`, `/api/suggest`, `/feed.xml`, `/sitemap.xml`.
- Operations: `/healthz`; CLI `flask --app app migration-report`. Static files are linked with
  `static_url('file')` in templates (adds a version for caching), not `url_for('static', ...)`.
- Old site: `/publicadliya/...`, `/publicria/...` → 301 redirects.

## Document model in short
`documents.kind` is `plan`, `draft` or `act`. `legacy_id` is the record number on the old site.
`body_format` is `text` (paragraphs, headings detected by `body_blocks`) or `html` (sanitised).
Plan and draft notification / RIA fields live in the `details` JSON (`{key: {"tj": .., "ru": ..}}`).
Earlier wordings of an act are in `document_versions`; amend/repeal links in `doc_links`; links found
inside texts in `doc_refs`.
