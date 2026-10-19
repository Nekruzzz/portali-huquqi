# Portali huquqi — modern version

A rebuild of the Internet portal of legal information of the Republic of Tajikistan
(portali-huquqi.tj), in Tajik and Russian, following:

- Law No. 1414 of 30.05.2017 "On normative legal acts", Articles 34–39
- Procedure for maintaining the portal, Government Resolution No. 617 of 29.12.2018 (amended 2020 No. 84, 2024 No. 478)
- RIA Instruction, Government Resolution No. 355 of 31.07.2018

## What it does

### Required by the Law and the Procedure

| Requirement (source) | How the portal implements it |
|---|---|
| Three registers: plans, drafts + RIA, adopted acts (Law art. 39.2, item 5) | `/plans`, `/drafts`, `/acts` with search, filters (type, year, status, developer, legal status) and sorting |
| Plan notification fields (art. 34.2–34.3, items 7–8) | Developer, affected persons, deadlines; problem, goal, measures, alternatives, costs when business is affected |
| Authorised body may return a notification (item 9) | "Return to developer" with a reason: document is hidden, developer is e-mailed and sees the reason |
| RIA content (art. 36.1) | Structured RIA fields + automatic summary table of proposals |
| Consultation ≥ 30 calendar days (art. 35.1, item 13) | Enforced in the editor; countdown; commenting opens/closes automatically |
| Developer reviews proposals (art. 36.4) | Decision + public reply per proposal; CSV export of all proposals for the RIA |
| Conclusion within 5 days (art. 35.6, 38; items 14, 18) | Admin-only conclusion; dashboard lists drafts awaiting it |
| Developer informs participants of the decision (item 12, art. 35.4) | Outcome field; everyone who commented or follows the document is e-mailed |
| Adopted act entered within 10 days (item 23) | Dashboard lists adopted drafts with no act yet |
| Ministry of Justice registration of departmental acts (item 24) | Registration number and date fields on acts |
| Only authorised persons post (items 10, 27) | Roles: `developer` (own plans/drafts) and `admin` (everything, incl. acts) |
| Registration of state bodies, legal entities, individuals (items 30–33, Annexes 1–3) | Three forms with the annex fields; approval/rejection with reason; printable pre-filled application |
| Free search, view, download, print (items 26, 28) | No login to read; attachments; `.txt` download; print layout |
| Tajik and Russian (art. 39.1) | Whole interface and all content in both; Tajik fallback |

### Added to make the portal useful day to day

- **Notifications by e-mail** — follow a document; subscribe to new plans/drafts (all, or one developer); reminders 3 days before a consultation closes; developer is told about new proposals; account approved/rejected; reply to your proposal.
- **Proposals tied to an article** — choose the article (taken from the draft's headings) or the RIA.
- **Support / oppose votes** on plans and drafts during consultation.
- **Adopted acts**: legal status (in force / partly / repealed), effective date, official publication source, "amended by" and "repealed by" history. Marking an act as repealed by another happens automatically.
- **Statistics** page (`/stats`) and **developers directory** (`/developers`).
- **News and announcements**, **feedback form** with an admin inbox and e-mail replies.
- **Accounts**: password reset by e-mail, profile editing, password change, admin temporary password.
- **Branding settings**: upload the operator's logo, operator name, address, phone, e-mail.
- **CSV import** of existing acts (admin page or `flask --app app import-csv file.csv`).
- **Accessibility**: larger text (A+/A−), high-contrast mode, works without JavaScript, keyboard friendly.
- **Open data**: JSON API, Atom feed, sitemap. Audit log of all admin actions.

### Ideas adopted from comparable portals

| Portal | Idea | Here |
|---|---|---|
| regulations.gov (USA) | "Comments due soon", "Newly posted", comment tracking numbers | Home tabs *Closing soon / New / Most discussed*; every proposal gets a receipt number |
| regulation.gov.ru (Russia) | Project ID, stage indicator, view and vote counters | Registry code (`02/26-00007`, searchable), 4-step stage tracker, views counter |
| EU "Have your say" | Browse by topic, stage timeline | 12 policy topics with Settings-style icons, `/topics`, `/topic/<slug>` |
| Open NLA (Kazakhstan) | "Comment" button next to each clause | "Propose" button beside every article of a draft |
| legislation.gov.uk (UK) | Banner on whether the text is up to date | Status banner on adopted acts (in force / amended / repealed) |

Design: bento-grid home page, Apple Watch-style countdown rings, Spotlight-style search suggestions
(`/api/suggest`), segmented controls with a sliding thumb, scroll reveal (off under Reduce Motion).

## Run locally

```bash
pip install -r requirements.txt
python app.py            # creates instance/portal.db, adds demo content, prints an admin login
```
Open http://127.0.0.1:5000. Set `ADMIN_PASSWORD=...` before the first run to choose the demo admin's password.

Demo items are titled "Намуна / Демо" — delete them in Идоракунӣ → Ҳуҷҷатҳо. The acts No. 1414, 355, 617 and 98 are real.

Existing databases are upgraded automatically when the new version starts.

## Logo

Log in as admin → Идоракунӣ → Танзимот → upload the logo (PNG, SVG, JPG or WEBP, up to 1 MB).
It appears in the header, footer and browser tab. Use the official file supplied by the organisation that operates the portal.

## Deploy on Vercel

1. Push this folder to a GitHub repository and import it in Vercel (Add New → Project). No build settings are needed: Vercel finds `app.py`.
2. In Project → Settings → Environment Variables add:
   - `SECRET_KEY` — any long random string (required, otherwise logins drop)
   - `ADMIN_PASSWORD` — password for the admin account; `ADMIN_EMAIL` optional (default `admin@portal.local`)
   - `CRON_SECRET` — any random string, enables the daily reminder job in `vercel.json`
   - `BASE_URL` — your site address, e.g. `https://your-app.vercel.app`
3. Redeploy.

**Important:** Vercel has no permanent disk. The database and uploaded files live in `/tmp` and are reset whenever
Vercel recycles the function (often within minutes to hours), and demo content is recreated. This is fine for showing the
portal; for real use the app needs a hosted database (e.g. Neon Postgres) and file storage (Vercel Blob), or a host with a
persistent disk (Render, Railway, a VPS — see below).

## Production

```bash
export SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
export COOKIE_SECURE=1                 # behind HTTPS
export BASE_URL=https://yourdomain.tj  # used for links in e-mails sent by cron jobs
flask --app app init-db
flask --app app create-admin admin@example.tj
gunicorn -w 4 -b 127.0.0.1:8000 app:app
```

E-mail (optional — without it, messages are written to the log):

```bash
export SMTP_HOST=smtp.example.tj SMTP_PORT=587 SMTP_USER=portal@example.tj SMTP_PASSWORD=... SMTP_FROM=portal@example.tj
# SMTP_SSL=1 for port 465
```

Cron jobs:

```cron
0 9 * * *  cd /srv/legal-portal && venv/bin/flask --app app send-reminders
*/10 * * * * cd /srv/legal-portal && venv/bin/flask --app app send-mail
```

Other environment variables: `DB_PATH`, `INSTANCE_DIR`, `REQUIRE_APPROVAL` (`1` default: every new account waits
for approval; `0`: individuals are activated immediately), `PORTAL_TZ` (default `Asia/Dushanbe`).

Back up `instance/` (database, uploaded files, logo).

## Workflow

1. A ministry registers as **State body**, prints and signs the application; the authorised body approves it and clicks *Make developer*.
2. The developer posts a **plan** (consultation ≥ 30 days). If items 7–8 are not met, the authorised body returns it with a reason.
3. The developer posts the **draft** with RIA, linked to the plan. Subscribers are e-mailed.
4. Registered users comment on specific articles and vote; the developer answers each proposal.
5. After the window closes, the authorised body writes the **conclusion**; the developer records the **outcome**; participants are e-mailed.
6. The authorised body adds the adopted **act**, links it to the draft and to any acts it amends or repeals.

## CSV import format

```
kind,doc_type,number,doc_date,title_tj,title_ru,developer_tj,developer_ru,summary_tj,summary_ru,body_tj,body_ru,publication_tj,legal_status,source_url
act,law,1414,2017-05-30,"Қонуни ҶТ «Дар бораи санадҳои меъёрии ҳуқуқӣ»","Закон РТ «О нормативных правовых актах»",...
```
`doc_type`: constitutional_law, code, law, decree, resolution, order, dept_act, local_act, other.
`legal_status`: in_force, partly, repealed. Duplicates are skipped.

## Tests

```bash
python tests/test_portal.py
```

## Notes

- Fonts load from Google Fonts with system fallbacks; for an offline or privacy-strict deployment, self-host Noto Sans / Noto Serif in `static/`.
- Passport data is optional online (it is collected on the signed paper form).
