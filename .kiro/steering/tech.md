---
inclusion: always
---
# Technical rules

## Stack (keep it small)
Python 3.12, Flask 3, Jinja templates, SQLite (WAL) with FTS5, vanilla JavaScript, one CSS file.
Dependencies are only those in `portali-huquqi/requirements.txt` (Flask, gunicorn, tzdata, nh3,
python-docx). Do not add a frontend framework, build step, ORM or CDN; ask before adding any dependency.

## Run and test
```bash
cd portali-huquqi
pip install -r requirements.txt
python app.py                    # http://127.0.0.1:5000, creates instance/portal.db with demo content
python tests/test_portal.py      # all tests must print "ok"; run before every commit
```
Add a test in `tests/test_portal.py` for every new feature or bug fix. Tests run against the demo seed.

## Must-follow rules
1. **Two languages.** Never hard-code interface text. Add a key to `STRINGS` in `i18n.py` with
   `_s("Tajik", "Russian")` and use `t('key')` in templates. Database fields come in `_tj` / `_ru`
   pairs; read them with `L(row, 'field')`, which falls back to Tajik.
2. **Schema changes** go in `schema.sql` *and* in `MIGRATIONS` in `app.py` (existing databases are
   upgraded on start). Indexes on columns that very old databases may lack go in `INDEXES` in `app.py`.
3. **Legal texts as HTML** (`body_format='html'`) are cleaned with `legaltext.sanitize()` before they
   are stored, and shown through `render_body()`. Never mark user or imported HTML `|safe` any other way.
   Keep the old anchors (`A000000064`): links between acts point to them.
4. **Search index.** Anything that changes `documents` outside the editor must call `fts_sync(conn, ids)`
   and `refs_sync(conn, ids)` afterwards (the import command does). If you change how text is indexed,
   bump `FTS_VERSION` so existing databases re-index. Tajik letters are folded by `fold()` (ӣ ӯ ҷ ҳ қ ғ ё).
5. **Performance.** Rows of `documents` carry whole legal texts (the full register is ≈26,000 acts,
   ~1.8 GB). Never scan all documents on a non-indexed column; restrict to the needed `kind`
   (`kinds=("plan", "draft")` in `search_documents`) and check the query uses an index.
6. **Security.** Every POST form includes `{{ csrf() }}`. The Content-Security-Policy allows only
   same-origin scripts and styles: no inline `<script>`, `onclick=` or `style=` attributes (JSON-LD data
   blocks are fine). Keep `safe_next()` for redirects. Do not log or commit passwords, `.env`,
   `instance/`, `backups/` or personal data of users.
7. **Old addresses** under `/publicadliya/` and `/publicria/` must keep redirecting (see `legacy_redirect`).
8. **The 30-day consultation** (`CONSULT_MIN_DAYS`), roles and approval flow follow the Procedure; do not
   relax them.
9. **Accessibility.** Works without JavaScript (JS only enhances), visible focus, contrast mode
   (`html.contrast`), no horizontal scroll at 390 px, `aria-*` on custom controls.

## Deployment
- Production: one Ubuntu server with Docker — `docker compose up -d --build` runs the app (gunicorn),
  a scheduler (mail every 10 min, reminders hourly) and Caddy (automatic HTTPS). Settings in `.env`
  (template `.env.example`); nightly `backup.sh`.
- Vercel (`vercel.json`) is a demo only: its database lives in `/tmp` and is reset regularly.

## Git
Work on a feature branch, never directly on `main`; merge through a pull request after the tests pass.
Clear commit messages that say what changed and why.
