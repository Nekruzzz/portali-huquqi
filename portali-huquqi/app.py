"""Portali huquqi — Internet portal of legal information of the Republic of Tajikistan.

Implements the Procedure approved by Government Resolution No. 617 of 29.12.2018
(as amended 2020, 2024) and Articles 34-39 of Law No. 1414 "On normative legal acts":
three registers (plans, drafts + RIA, adopted acts), public consultation of at least
30 calendar days, user registration per Annexes 1-3, developer and authorised-body roles,
plus notifications, statistics, news, feedback and open data.

Run:    python app.py            (development; creates and seeds the database)
        gunicorn app:app         (production)
CLI:    flask --app app init-db | seed | create-admin EMAIL | send-mail | send-reminders | import-csv FILE
"""
import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import uuid
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

import click
from flask import (Flask, Response, abort, flash, g, has_request_context, jsonify, redirect,
                   render_template, request, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import mailer
from i18n import (BUSINESS_FIELDS, DETAIL_FIELDS, DOC_TYPES, KINDS, LANGS, LEGAL_STATUSES, PLAN_FIELDS,
                  RIA_FIELDS, STRINGS, detail_label, tr)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ON_VERCEL = bool(os.environ.get("VERCEL"))
# Vercel functions can only write to /tmp, which is wiped when an instance is recycled.
INSTANCE_DIR = os.environ.get("INSTANCE_DIR") or ("/tmp/portal-instance" if ON_VERCEL else os.path.join(BASE_DIR, "instance"))
UPLOAD_DIR = os.path.join(INSTANCE_DIR, "uploads")
BRAND_DIR = os.path.join(INSTANCE_DIR, "branding")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(BRAND_DIR, exist_ok=True)
DB_PATH = os.environ.get("DB_PATH", os.path.join(INSTANCE_DIR, "portal.db"))

PER_PAGE = 10
COMMENT_MIN, COMMENT_MAX = 5, 3000
CONSULT_MIN_DAYS = 30
REMIND_DAYS = 3
KIND_SLUGS = {"plans": "plan", "drafts": "draft", "acts": "act"}
SLUG_OF_KIND = {v: k for k, v in KIND_SLUGS.items()}
ALLOWED_EXT = {"pdf", "doc", "docx", "odt", "rtf"}
LOGO_EXT = {"png": "image/png", "svg": "image/svg+xml", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
PHONE_RE = re.compile(r"^[0-9+()\-\s]{6,20}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STATUSES = ("upcoming", "consultation", "review", "concluded", "proceed", "adopted", "rejected")
SETTING_KEYS = ("operator_tj", "operator_ru", "contact_address_tj", "contact_address_ru", "contact_phone",
                "contact_email")

# Fields collected per account type (Annexes 1-3 of the Procedure). True = required.
ACCOUNT_FIELDS = {
    "state_body": [("org_name", True), ("full_name", True), ("position", True), ("org_activity", False),
                   ("head_name", False), ("head_phone", False), ("tax_id", False), ("address", True),
                   ("passport", False), ("email", True), ("phone", True)],
    "legal_entity": [("org_name", True), ("full_name", True), ("position", True), ("authority_basis", True),
                     ("org_activity", False), ("head_name", False), ("head_phone", False), ("tax_id", True),
                     ("address", True), ("passport", False), ("email", True), ("phone", True)],
    "individual": [("full_name", True), ("workplace", False), ("email", True), ("phone", True),
                   ("tax_id", False), ("address", True), ("passport", False)],
}
ANNEX_NO = {"state_body": 1, "legal_entity": 2, "individual": 3}


def _secret_key():
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    if ON_VERCEL:
        print("WARNING: set the SECRET_KEY environment variable in Vercel, otherwise logins will not stick.")
    path = os.path.join(INSTANCE_DIR, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as f:
        return f.read().strip()


# Static files live in public/static so Vercel's CDN serves them; Flask serves them locally.
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "public", "static"), static_url_path="/static")
app.config.update(
    SECRET_KEY=_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=21 * 1024 * 1024,
    # Items 31-32 of the Procedure: the authorised body reviews each application.
    # 1 (default) = every new account waits for approval;
    # 0 = individuals are activated at once, organisations still need approval.
    REQUIRE_APPROVAL=os.environ.get("REQUIRE_APPROVAL", "1") == "1",
    TIMEZONE=os.environ.get("PORTAL_TZ", "Asia/Dushanbe"),
    BASE_URL=os.environ.get("BASE_URL", "http://127.0.0.1:5000"),
    # serverless instances may freeze after the response, so send mail before returning there
    MAIL_SYNC=os.environ.get("MAIL_SYNC") == "1" or ON_VERCEL,
)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        # SQLite's LIKE is case-insensitive for ASCII only; make Cyrillic work.
        conn.create_function("ulower", 1, lambda s: s.lower() if isinstance(s, str) else s)
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


MIGRATIONS = {
    "users": [("lang", "TEXT"), ("notify_email", "INTEGER NOT NULL DEFAULT 1"), ("reset_hash", "TEXT"),
              ("reset_expires", "TEXT"), ("admin_note", "TEXT")],
    "documents": [("legal_status", "TEXT"), ("effective_date", "TEXT"), ("publication_tj", "TEXT"),
                  ("publication_ru", "TEXT"), ("mj_reg_number", "TEXT"), ("mj_reg_date", "TEXT"),
                  ("returned_note", "TEXT")],
    "comments": [("article_ref", "TEXT")],
}


def init_db():
    """Create missing tables and add columns introduced after the first release."""
    conn = sqlite3.connect(DB_PATH)
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, cols in MIGRATIONS.items():
        if table in existing:
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    with open(os.path.join(BASE_DIR, "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()


def db_is_empty():
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    finally:
        conn.close()


def audit(action, target=None):
    uid = g.user["id"] if g.get("user") else None
    get_db().execute("INSERT INTO audit_log (user_id, action, target) VALUES (?,?,?)", (uid, action, target))


def get_settings():
    if "settings" not in g:
        g.settings = {r["key"]: r["value"] for r in get_db().execute("SELECT key, value FROM settings")}
    return g.settings


def set_setting(key, value):
    get_db().execute("INSERT INTO settings (key, value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    g.pop("settings", None)


# ---------------------------------------------------------------------------
# Helpers: dates, i18n, status, rate limiting
# ---------------------------------------------------------------------------
def today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(app.config["TIMEZONE"])).date()
    except Exception:  # tz database missing
        return date.today()


def parse_date(s):
    if not s or not DATE_RE.match(s):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def current_lang():
    if not has_request_context():
        return "tj"
    lang = session.get("lang")
    return lang if lang in LANGS else "tj"


def t(key):
    if key not in STRINGS:
        app.logger.warning("missing i18n key: %s", key)
    return tr(key, current_lang())


def L(row, field, lang=None):
    """Localised column with fallback to Tajik (the state language)."""
    lang = lang or current_lang()
    keys = row.keys()
    val = row[f"{field}_{lang}"] if f"{field}_{lang}" in keys else None
    return val or (row[f"{field}_tj"] if f"{field}_tj" in keys else "") or ""


def is_fallback(row, field):
    return current_lang() != "tj" and not row[f"{field}_{current_lang()}"] and bool(row[f"{field}_tj"])


def fmt_date(s):
    d = parse_date(s[:10]) if s else None
    return d.strftime("%d.%m.%Y") if d else ""


def doc_type_label(code, lang=None):
    return DOC_TYPES.get(code, {}).get(lang or current_lang(), "") if code else ""


def status_sql(alias=""):
    p = f"{alias}." if alias else ""
    td = today().isoformat()  # generated here, safe to inline
    return (f"CASE WHEN {p}kind='act' THEN 'act' "
            f"WHEN {p}outcome IS NOT NULL THEN {p}outcome "
            f"WHEN {p}consult_start > '{td}' THEN 'upcoming' "
            f"WHEN {p}consult_end >= '{td}' THEN 'consultation' "
            f"WHEN {p}conclusion_date IS NOT NULL THEN 'concluded' "
            f"ELSE 'review' END")


def consult_info(doc):
    """Days left and progress through the consultation window."""
    start, end = parse_date(doc["consult_start"]), parse_date(doc["consult_end"])
    if not start or not end:
        return None
    td = today()
    total = max((end - start).days, 1)
    done = min(max((td - start).days, 0), total)
    return {"days_left": (end - td).days, "pct": round(100 * done / total), "start": start, "end": end}


_hits = defaultdict(deque)


def rate_limited(key, limit, window):
    now = time.monotonic()
    q = _hits[key]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


def client_ip():
    return request.headers.get("X-Real-IP") or request.remote_addr or "?"


def safe_next(target):
    if target and urlparse(target).netloc == "" and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("index")


def display_name(row):
    """Public name of a commenter: organisation name, or surname + initial."""
    if row["account_type"] in ("legal_entity", "state_body") and row["org_name"]:
        return row["org_name"]
    parts = (row["full_name"] or "").split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1][0]}."
    return parts[0] if parts else "—"


HEADING_RE = re.compile(r"^(Моддаи|Статья|Боби|Глава|БОБИ|ГЛАВА|§)\s*[\dIVXLC]+|^\d+\.\s+[А-ЯЁҲҚҶҒӮӢ\s]{6,}$")


def body_blocks(text):
    """Split plain-text legal bodies into headings (with anchors) and paragraphs."""
    blocks, n = [], 0
    for para in re.split(r"\n\s*\n", (text or "").strip()):
        para = para.strip()
        if not para:
            continue
        first, _, rest = para.partition("\n")
        if HEADING_RE.match(first) and len(first) < 200:
            n += 1
            blocks.append({"type": "h", "text": first, "id": f"s{n}"})
            if rest.strip():
                blocks.append({"type": "p", "text": rest.strip()})
        else:
            blocks.append({"type": "p", "text": para})
    return blocks


def load_details(doc):
    try:
        return json.loads(doc["details"] or "{}")
    except (TypeError, ValueError):
        return {}


def detail_value(details, key):
    v = details.get(key) or {}
    return v.get(current_lang()) or v.get("tj") or ""


def parse_ids(text):
    return sorted({int(x) for x in re.findall(r"\d+", text or "")})


def abs_url(endpoint, **kw):
    """Absolute link for e-mails, also outside a request (cron jobs)."""
    if has_request_context():
        return url_for(endpoint, _external=True, **kw)
    with app.test_request_context(base_url=app.config["BASE_URL"]):
        return url_for(endpoint, _external=True, **kw)


# ---------------------------------------------------------------------------
# E-mail notifications
# ---------------------------------------------------------------------------
def queue_mail(to_email, lang, subj_key, body_key, user_id=None, footer=True, **fmt):
    lang = lang if lang in LANGS else "tj"
    site = tr("site_name", lang)
    subject = tr(subj_key, lang).format(**fmt)
    body = tr(body_key, lang).format(**fmt)
    if footer:
        body += "\n\n--\n" + tr("mail_footer", lang).format(site=site, url=abs_url("account"))
    get_db().execute("INSERT INTO outbox (user_id, to_email, subject, body) VALUES (?,?,?,?)",
                     (user_id, to_email, f"{subject} — {site}", body))
    g.mail_queued = True


def notify_users(users, subj_key, body_key, fmt_for, exclude_id=None):
    """Queue one message per active user who wants e-mail; fmt_for(lang) returns format args."""
    seen = set()
    for u in users:
        if u["id"] in seen or u["id"] == exclude_id or u["status"] != "active" or not u["notify_email"]:
            continue
        seen.add(u["id"])
        lang = u["lang"] or "tj"
        queue_mail(u["email"], lang, subj_key, body_key, user_id=u["id"], **fmt_for(lang))


def doc_title(doc, lang):
    return (doc["title_ru"] if lang == "ru" and doc["title_ru"] else doc["title_tj"]) or ""


def followers_of(doc_id):
    return get_db().execute(
        "SELECT DISTINCT u.* FROM users u WHERE u.id IN ("
        " SELECT user_id FROM subscriptions WHERE document_id=?"
        " UNION SELECT user_id FROM comments WHERE document_id=?)", (doc_id, doc_id)).fetchall()


def notify_status_change(doc, note_tj="", note_ru=""):
    doc = get_document(doc["id"])
    url = abs_url("document", doc_id=doc["id"])
    notify_users(followers_of(doc["id"]), "mail_update_s", "mail_update_b",
                 lambda lg: {"title": doc_title(doc, lg), "status": tr("st_" + doc["status"], lg),
                             "note": (note_ru if lg == "ru" and note_ru else note_tj) or "", "url": url},
                 exclude_id=g.user["id"] if g.get("user") else None)


def notify_new_document(doc):
    doc = get_document(doc["id"])
    if doc["kind"] not in ("plan", "draft") or not doc["published"]:
        return
    subs = get_db().execute(
        "SELECT DISTINCT u.* FROM subscriptions s JOIN users u ON u.id=s.user_id "
        "WHERE s.document_id IS NULL AND s.kind=? AND (s.developer IS NULL OR s.developer=?)",
        (doc["kind"], doc["developer_tj"])).fetchall()
    url = abs_url("document", doc_id=doc["id"])
    notify_users(subs, "mail_newdoc_s", "mail_newdoc_b",
                 lambda lg: {"title": doc_title(doc, lg), "register": tr("reg_title_" + doc["kind"], lg),
                             "developer": (doc["developer_ru"] if lg == "ru" and doc["developer_ru"]
                                           else doc["developer_tj"]) or "",
                             "end": fmt_date(doc["consult_end"]), "url": url},
                 exclude_id=doc["owner_id"])


@app.teardown_request
def kick_mail(_exc=None):
    if g.pop("mail_queued", False):
        if app.config["MAIL_SYNC"] or app.config.get("TESTING"):
            mailer.deliver_pending(DB_PATH)
        else:
            mailer.deliver_async(DB_PATH, "Portali huquqi")


# ---------------------------------------------------------------------------
# Request lifecycle: user, language, CSRF, headers
# ---------------------------------------------------------------------------
@app.before_request
def before():
    lang = request.args.get("lang")
    if lang in LANGS:
        session["lang"] = lang
    g.user = None
    uid = session.get("uid")
    if uid:
        user = get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if user and user["status"] != "blocked":
            g.user = user
        else:
            session.pop("uid", None)
    if request.method == "POST" and not request.path.startswith("/api/"):
        token = request.form.get("csrf_token", "")
        if not token or not secrets.compare_digest(token, session.get("csrf", "")):
            flash(t("err_csrf"), "error")
            return redirect(safe_next(request.referrer and urlparse(request.referrer).path))


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


@app.after_request
def headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
        "script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'")
    if request.path.startswith("/api/"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.context_processor
def inject():
    s = get_settings()
    logo = s.get("logo_file")
    return dict(t=t, L=L, lang=current_lang(), LANGS=LANGS, DOC_TYPES=DOC_TYPES, KINDS=KINDS,
                LEGAL_STATUSES=LEGAL_STATUSES, fmt_date=fmt_date, doc_type_label=doc_type_label,
                csrf_token=csrf_token, SLUG_OF_KIND=SLUG_OF_KIND, consult_info=consult_info, today=today(),
                is_fallback=is_fallback, detail_label=lambda k: detail_label(k, current_lang()),
                site=s, logo_url=url_for("logo", v=s.get("logo_version", "1")) if logo else None,
                operator=s.get("operator_ru") if current_lang() == "ru" and s.get("operator_ru")
                else s.get("operator_tj"),
                contact_address=(s.get("contact_address_ru") if current_lang() == "ru"
                                 and s.get("contact_address_ru") else s.get("contact_address_tj"))
                or t("contact_addr"))


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapped


def roles_required(*roles):
    def deco(view):
        @wraps(view)
        def wrapped(*a, **kw):
            if not g.user:
                return redirect(url_for("login", next=request.path))
            if g.user["role"] not in roles or g.user["status"] != "active":
                abort(403)
            return view(*a, **kw)
        return wrapped
    return deco


def is_admin():
    return bool(g.user and g.user["role"] == "admin" and g.user["status"] == "active")


def can_edit(doc):
    if is_admin():
        return True
    return bool(g.user and g.user["role"] == "developer" and g.user["status"] == "active"
                and doc["owner_id"] == g.user["id"] and doc["kind"] != "act")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def search_documents(kind=None, q="", doc_type="", year="", status="", sort="new", page=1, per_page=PER_PAGE,
                     owner_id=None, include_hidden=False, developer="", legal="", fulltext=False):
    where, params = [], []
    if kind:
        where.append("d.kind=?")
        params.append(kind)
    if not include_hidden:
        where.append("d.published=1")
    if owner_id:
        where.append("d.owner_id=?")
        params.append(owner_id)
    if doc_type in DOC_TYPES:
        where.append("d.doc_type=?")
        params.append(doc_type)
    if re.fullmatch(r"\d{4}", year or ""):
        where.append("substr(d.doc_date,1,4)=?")
        params.append(year)
    if status in STATUSES:
        where.append(f"({status_sql('d')})=?")
        params.append(status)
    if developer:
        where.append("d.developer_tj=?")
        params.append(developer)
    if legal in LEGAL_STATUSES:
        where.append("d.kind='act' AND coalesce(d.legal_status,'in_force')=?")
        params.append(legal)
    haystack = ("coalesce(d.title_tj,'')||' '||coalesce(d.title_ru,'')||' '||coalesce(d.number,'')"
                "||' '||coalesce(d.summary_tj,'')||' '||coalesce(d.summary_ru,'')"
                "||' '||coalesce(d.developer_tj,'')||' '||coalesce(d.developer_ru,'')")
    if fulltext:
        haystack += "||' '||coalesce(d.body_tj,'')||' '||coalesce(d.body_ru,'')"
    for tok in (q or "").lower().split()[:8]:
        tok = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append(f"ulower({haystack}) LIKE ? ESCAPE '\\'")
        params.append(f"%{tok}%")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    order = {"old": "d.doc_date ASC, d.id ASC",
             "closing": "CASE WHEN d.consult_end IS NULL THEN 1 ELSE 0 END, d.consult_end ASC"}.get(
        sort, "d.doc_date DESC, d.id DESC")
    db = get_db()
    total = db.execute(f"SELECT COUNT(*) FROM documents d {clause}", params).fetchone()[0]
    rows = db.execute(
        f"SELECT d.*, {status_sql('d')} AS status, "
        f"(SELECT COUNT(*) FROM comments c WHERE c.document_id=d.id AND c.status='visible') AS n_comments "
        f"FROM documents d {clause} ORDER BY {order} LIMIT ? OFFSET ?",
        params + [per_page, (page - 1) * per_page]).fetchall()
    return rows, total


def search_args():
    a = request.args
    return dict(q=a.get("q", ""), doc_type=a.get("type", ""), year=a.get("year", ""), status=a.get("status", ""),
                sort=a.get("sort", "new"), developer=a.get("dev", ""), legal=a.get("legal", ""),
                fulltext=a.get("fulltext") == "1")


def get_document(doc_id):
    return get_db().execute(f"SELECT d.*, {status_sql('d')} AS status FROM documents d WHERE d.id=?",
                            (doc_id,)).fetchone()


def page_arg():
    try:
        return max(int(request.args.get("page", 1)), 1)
    except ValueError:
        return 1


def developers_list(kind=None):
    sql = "SELECT DISTINCT developer_tj FROM documents WHERE published=1 AND developer_tj IS NOT NULL AND developer_tj<>''"
    params = []
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    return [r[0] for r in get_db().execute(sql + " ORDER BY developer_tj", params)]


def user_subscribed(kind, developer=None):
    if not g.user:
        return False
    return get_db().execute(
        "SELECT 1 FROM subscriptions WHERE user_id=? AND document_id IS NULL AND kind=? AND "
        "coalesce(developer,'')=?", (g.user["id"], kind, developer or "")).fetchone() is not None


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    db = get_db()
    counts = {k: db.execute("SELECT COUNT(*) FROM documents WHERE kind=? AND published=1", (k,)).fetchone()[0]
              for k in KINDS}
    open_now, _ = search_documents(status="consultation", sort="closing", per_page=6)
    latest_acts, _ = search_documents(kind="act", per_page=5)
    news = db.execute("SELECT * FROM news WHERE published=1 ORDER BY created_at DESC LIMIT 3").fetchall()
    return render_template("index.html", counts=counts, open_now=open_now, latest_acts=latest_acts, news=news)


@app.route("/<any(plans, drafts, acts):slug>")
def registry(slug):
    kind = KIND_SLUGS[slug]
    page = page_arg()
    args = search_args()
    rows, total = search_documents(kind, page=page, **args)
    years = [r[0] for r in get_db().execute(
        "SELECT DISTINCT substr(doc_date,1,4) y FROM documents WHERE kind=? AND published=1 "
        "AND doc_date IS NOT NULL ORDER BY y DESC", (kind,))]
    return render_template("registry.html", kind=kind, slug=slug, rows=rows, total=total, page=page,
                           pages=max((total + PER_PAGE - 1) // PER_PAGE, 1), years=years,
                           developers=developers_list(kind), statuses=[] if kind == "act" else STATUSES,
                           subscribed=user_subscribed(kind, args["developer"]))


@app.route("/search")
def search():
    kind = request.args.get("kind") if request.args.get("kind") in KINDS else None
    page = page_arg()
    rows, total = search_documents(kind, page=page, **search_args())
    return render_template("search.html", rows=rows, total=total, page=page, kind=kind,
                           developers=developers_list(), pages=max((total + PER_PAGE - 1) // PER_PAGE, 1))


def article_options(doc, blocks):
    opts = [t("article_general")]
    opts += [b["text"][:120] for b in blocks if b["type"] == "h"]
    if doc["kind"] == "draft":
        opts.append(t("ria"))
    return opts


@app.route("/doc/<int:doc_id>")
def document(doc_id):
    doc = get_document(doc_id)
    if not doc or (not doc["published"] and not can_edit(doc)):
        abort(404)
    db = get_db()
    details = load_details(doc)
    parent = get_document(doc["related_id"]) if doc["related_id"] else None
    if parent and not parent["published"]:
        parent = None
    children = db.execute(f"SELECT d.*, {status_sql('d')} AS status FROM documents d "
                          "WHERE related_id=? AND published=1 ORDER BY doc_date", (doc_id,)).fetchall()
    attachments = db.execute("SELECT * FROM attachments WHERE document_id=? ORDER BY id", (doc_id,)).fetchall()
    show_hidden = can_edit(doc)
    comments = db.execute(
        "SELECT c.*, u.full_name, u.org_name, u.account_type FROM comments c JOIN users u ON u.id=c.user_id "
        "WHERE c.document_id=? " + ("" if show_hidden else "AND c.status='visible' ") +
        "ORDER BY c.created_at", (doc_id,)).fetchall()
    if doc["kind"] == "plan":
        fields = list(PLAN_FIELDS) + (list(BUSINESS_FIELDS) if doc["affects_business"] else [])
    elif doc["kind"] == "draft":
        fields = list(RIA_FIELDS)
    else:
        fields = []
    detail_rows = [(k, detail_value(details, k)) for k in fields if detail_value(details, k)]
    lang = current_lang()
    body = doc[f"body_{lang}"] or doc["body_tj"]
    blocks = body_blocks(body)
    links = {"amended_by": [], "amends": [], "repealed_by": [], "repeals": []}
    for r in db.execute("SELECT l.link_type, d.* FROM doc_links l JOIN documents d ON d.id=l.from_id "
                        "WHERE l.to_id=? AND d.published=1 ORDER BY d.doc_date", (doc_id,)):
        links["amended_by" if r["link_type"] == "amends" else "repealed_by"].append(r)
    for r in db.execute("SELECT l.link_type, d.* FROM doc_links l JOIN documents d ON d.id=l.to_id "
                        "WHERE l.from_id=? AND d.published=1 ORDER BY d.doc_date", (doc_id,)):
        links["amends" if r["link_type"] == "amends" else "repeals"].append(r)
    votes = {r["value"]: r["n"] for r in db.execute(
        "SELECT value, COUNT(*) n FROM votes WHERE document_id=? GROUP BY value", (doc_id,))}
    my_vote = following = None
    if g.user:
        v = db.execute("SELECT value FROM votes WHERE document_id=? AND user_id=?", (doc_id, g.user["id"])).fetchone()
        my_vote = v["value"] if v else 0
        following = db.execute("SELECT 1 FROM subscriptions WHERE document_id=? AND user_id=?",
                               (doc_id, g.user["id"])).fetchone() is not None
    return render_template("document.html", doc=doc, detail_rows=detail_rows, parent=parent,
                           children=children, attachments=attachments, comments=comments,
                           display_name=display_name, blocks=blocks, links=links,
                           body_fallback=bool(body) and not doc[f"body_{lang}"],
                           consult=consult_info(doc), can_edit=can_edit(doc),
                           votes_for=votes.get(1, 0), votes_against=votes.get(-1, 0), my_vote=my_vote,
                           following=following, article_options=article_options(doc, blocks))


@app.route("/doc/<int:doc_id>.txt")
def document_text(doc_id):
    doc = get_document(doc_id)
    if not doc or not doc["published"]:
        abort(404)
    lang = current_lang()
    parts = [L(doc, "title"), " ".join(x for x in (doc_type_label(doc["doc_type"]),
                                                  f"№ {doc['number']}" if doc["number"] else "",
                                                  fmt_date(doc["doc_date"])) if x), "",
             doc[f"body_{lang}"] or doc["body_tj"] or L(doc, "summary"), "",
             url_for("document", doc_id=doc_id, _external=True)]
    name = f"{doc_type_label(doc['doc_type']) or 'doc'}_{doc['number'] or doc_id}.txt".replace(" ", "_")
    return Response("\n".join(parts), mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_quote(name)}"})


def _quote(s):
    from urllib.parse import quote
    return quote(s)


@app.post("/doc/<int:doc_id>/comment")
@login_required
def add_comment(doc_id):
    doc = get_document(doc_id)
    if not doc or not doc["published"]:
        abort(404)
    anchor = url_for("document", doc_id=doc_id) + "#comments"
    if g.user["status"] != "active":
        flash(t("comment_pending"), "error")
        return redirect(anchor)
    if doc["status"] != "consultation":
        flash(t("comment_closed"), "error")
        return redirect(anchor)
    body = (request.form.get("body") or "").strip()
    if not (COMMENT_MIN <= len(body) <= COMMENT_MAX):
        flash(t("comment_length"), "error")
        session["comment_draft"] = body[:COMMENT_MAX]
        return redirect(anchor)
    if rate_limited(f"c:{g.user['id']}", 10, 3600):
        flash(t("comment_rate"), "error")
        return redirect(anchor)
    article = (request.form.get("article_ref") or "").strip()[:200]
    if article == t("article_general"):
        article = ""
    db = get_db()
    cur = db.execute("INSERT INTO comments (document_id, user_id, article_ref, body) VALUES (?,?,?,?)",
                     (doc_id, g.user["id"], article or None, body))
    audit("comment.create", f"doc:{doc_id}")
    # the developer who posted the document hears about new proposals
    if doc["owner_id"] and doc["owner_id"] != g.user["id"]:
        owner = db.execute("SELECT * FROM users WHERE id=?", (doc["owner_id"],)).fetchall()
        url = abs_url("document", doc_id=doc_id) + f"#c{cur.lastrowid}"
        author = display_name(g.user)
        notify_users(owner, "mail_comment_s", "mail_comment_b",
                     lambda lg: {"title": doc_title(doc, lg), "author": author, "body": body[:1000], "url": url})
    db.commit()
    session.pop("comment_draft", None)
    flash(t("comment_saved"), "success")
    return redirect(anchor)


@app.post("/doc/<int:doc_id>/vote")
@login_required
def vote(doc_id):
    doc = get_document(doc_id)
    if not doc or not doc["published"] or doc["kind"] == "act":
        abort(404)
    if g.user["status"] != "active" or doc["status"] != "consultation":
        flash(t("comment_pending") if g.user["status"] != "active" else t("comment_closed"), "error")
        return redirect(url_for("document", doc_id=doc_id))
    value = request.form.get("value")
    db = get_db()
    if value in ("1", "-1"):
        db.execute("INSERT INTO votes (document_id, user_id, value) VALUES (?,?,?) ON CONFLICT(document_id, user_id) "
                   "DO UPDATE SET value=excluded.value, created_at=datetime('now')", (doc_id, g.user["id"], int(value)))
    else:
        db.execute("DELETE FROM votes WHERE document_id=? AND user_id=?", (doc_id, g.user["id"]))
    db.commit()
    flash(t("vote_saved"), "success")
    return redirect(url_for("document", doc_id=doc_id) + "#vote")


@app.post("/doc/<int:doc_id>/follow")
@login_required
def follow(doc_id):
    doc = get_document(doc_id)
    if not doc or not doc["published"]:
        abort(404)
    db = get_db()
    existing = db.execute("SELECT id FROM subscriptions WHERE document_id=? AND user_id=?",
                          (doc_id, g.user["id"])).fetchone()
    if existing:
        db.execute("DELETE FROM subscriptions WHERE id=?", (existing["id"],))
        flash(t("unsubscribed"), "success")
    else:
        db.execute("INSERT INTO subscriptions (user_id, document_id) VALUES (?,?)", (g.user["id"], doc_id))
        flash(t("subscribed"), "success")
    db.commit()
    return redirect(url_for("document", doc_id=doc_id))


@app.post("/subscribe")
@login_required
def subscribe():
    kind = request.form.get("kind")
    if kind not in ("plan", "draft"):
        abort(400)
    dev = (request.form.get("dev") or "").strip()[:300] or None
    db = get_db()
    if user_subscribed(kind, dev):
        db.execute("DELETE FROM subscriptions WHERE user_id=? AND document_id IS NULL AND kind=? "
                   "AND coalesce(developer,'')=?", (g.user["id"], kind, dev or ""))
        flash(t("unsubscribed"), "success")
    else:
        db.execute("INSERT INTO subscriptions (user_id, kind, developer) VALUES (?,?,?)", (g.user["id"], kind, dev))
        flash(t("subscribed"), "success")
    db.commit()
    return redirect(safe_next(request.form.get("next")))


@app.post("/subscriptions/<int:sub_id>/delete")
@login_required
def delete_subscription(sub_id):
    get_db().execute("DELETE FROM subscriptions WHERE id=? AND user_id=?", (sub_id, g.user["id"]))
    get_db().commit()
    flash(t("unsubscribed"), "success")
    return redirect(url_for("account") + "#subscriptions")


@app.route("/files/<path:stored_name>")
def download(stored_name):
    att = get_db().execute("SELECT a.*, d.published, d.owner_id, d.kind FROM attachments a "
                           "JOIN documents d ON d.id=a.document_id WHERE stored_name=?",
                           (stored_name,)).fetchone()
    if not att or (not att["published"] and not can_edit(att)):
        abort(404)
    return send_from_directory(UPLOAD_DIR, att["stored_name"], as_attachment=True,
                               download_name=att["original_name"])


@app.route("/branding/logo")
def logo():
    name = get_settings().get("logo_file")
    if not name or not os.path.exists(os.path.join(BRAND_DIR, name)):
        abort(404)
    resp = send_from_directory(BRAND_DIR, name, mimetype=LOGO_EXT.get(name.rsplit(".", 1)[-1]), max_age=86400)
    # an uploaded SVG must never run scripts
    resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
    return resp


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/stats")
def stats():
    db = get_db()
    counts = {k: db.execute("SELECT COUNT(*) FROM documents WHERE kind=? AND published=1", (k,)).fetchone()[0]
              for k in KINDS}
    open_now = db.execute(f"SELECT COUNT(*) FROM documents d WHERE published=1 AND ({status_sql('d')})='consultation'"
                          ).fetchone()[0]
    c = db.execute("SELECT COUNT(*) total, SUM(decision IS NOT NULL) answered, "
                   "SUM(decision IN ('accepted','partial')) accepted, SUM(decision='accepted') full_n, "
                   "SUM(decision='partial') partial, SUM(decision='rejected') rejected "
                   "FROM comments c JOIN documents d ON d.id=c.document_id WHERE c.status='visible' AND d.published=1"
                   ).fetchone()
    users = db.execute("SELECT COUNT(*) FROM users WHERE status='active' AND role='user'").fetchone()[0]
    by_year = db.execute("SELECT substr(doc_date,1,4) y, COUNT(*) n FROM documents WHERE kind='act' AND published=1 "
                         "AND doc_date IS NOT NULL GROUP BY y ORDER BY y DESC LIMIT 12").fetchall()
    by_dev = db.execute(
        "SELECT developer_tj, max(coalesce(developer_ru,'')) developer_ru, COUNT(*) n, "
        "(SELECT COUNT(*) FROM comments c JOIN documents d2 ON d2.id=c.document_id "
        " WHERE d2.developer_tj=d.developer_tj AND c.status='visible') proposals "
        "FROM documents d WHERE published=1 AND kind IN ('plan','draft') AND developer_tj IS NOT NULL "
        "GROUP BY developer_tj ORDER BY n DESC LIMIT 10").fetchall()
    total = c["total"] or 0
    pct = lambda n: round(100 * (n or 0) / total) if total else 0  # noqa: E731
    max_year = max([r["n"] for r in by_year] or [1])
    max_dev = max([r["n"] for r in by_dev] or [1])
    return render_template("stats.html", counts=counts, open_now=open_now, c=c, users=users, pct=pct,
                           by_year=by_year, by_dev=by_dev, max_year=max_year, max_dev=max_dev)


@app.route("/developers")
def developers():
    rows = get_db().execute(
        f"SELECT developer_tj, max(coalesce(developer_ru,'')) developer_ru, "
        f"SUM(kind='plan') plans, SUM(kind='draft') drafts, SUM(kind='act') acts, "
        f"SUM(({status_sql()})='consultation') open_now "
        f"FROM documents WHERE published=1 AND developer_tj IS NOT NULL AND developer_tj<>'' "
        f"GROUP BY developer_tj ORDER BY open_now DESC, (plans+drafts) DESC, developer_tj").fetchall()
    return render_template("developers.html", rows=rows)


@app.route("/news")
def news_list():
    page = page_arg()
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM news WHERE published=1").fetchone()[0]
    rows = db.execute("SELECT * FROM news WHERE published=1 ORDER BY created_at DESC LIMIT ? OFFSET ?",
                      (PER_PAGE, (page - 1) * PER_PAGE)).fetchall()
    return render_template("news_list.html", rows=rows, page=page, pages=max((total + PER_PAGE - 1) // PER_PAGE, 1))


@app.route("/news/<int:news_id>")
def news_item(news_id):
    n = get_db().execute("SELECT * FROM news WHERE id=?", (news_id,)).fetchone()
    if not n or (not n["published"] and not is_admin()):
        abort(404)
    return render_template("news_item.html", n=n, blocks=body_blocks(L(n, "body")))


@app.route("/contact", methods=["GET", "POST"])
def contact():
    form, errors = {}, []
    if request.method == "POST":
        form = {k: (request.form.get(k) or "").strip() for k in ("name", "email", "subject", "body")}
        if request.form.get("website"):  # honeypot: humans leave it empty
            flash(t("contact_sent"), "success")
            return redirect(url_for("contact"))
        if not all(form.values()):
            errors.append(t("err_required"))
        if form["email"] and not EMAIL_RE.match(form["email"]):
            errors.append(t("err_email"))
        if len(form["body"]) > COMMENT_MAX:
            errors.append(t("comment_length"))
        if not errors and rate_limited(f"m:{client_ip()}", 5, 3600):
            errors.append(t("comment_rate"))
        if not errors:
            get_db().execute("INSERT INTO messages (user_id, name, email, subject, body) VALUES (?,?,?,?,?)",
                             (g.user["id"] if g.user else None, form["name"][:200], form["email"][:200].lower(),
                              form["subject"][:300], form["body"]))
            get_db().commit()
            flash(t("contact_sent"), "success")
            return redirect(url_for("contact"))
    elif g.user:
        form = {"name": g.user["full_name"], "email": g.user["email"]}
    return render_template("contact.html", form=form, errors=errors), (400 if errors else 200)


@app.route("/lang/<code>")
def set_lang(code):
    if code in LANGS:
        session["lang"] = code
        if g.user:
            get_db().execute("UPDATE users SET lang=? WHERE id=?", (code, g.user["id"]))
            get_db().commit()
    return redirect(safe_next(request.args.get("next")))


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
def start_session(user):
    lang = session.get("lang")
    session.clear()
    session.update(uid=user["id"], lang=lang or user["lang"] or "tj")
    session.permanent = True
    g.user = user


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if rate_limited(f"l:{client_ip()}", 10, 900):
            flash(t("login_rate"), "error")
            return render_template("account/login.html"), 429
        email = (request.form.get("email") or "").strip().lower()
        user = get_db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], request.form.get("password", "")):
            flash(t("login_failed"), "error")
            return render_template("account/login.html", email=email), 401
        if user["status"] == "blocked":
            flash(t("login_blocked"), "error")
            return render_template("account/login.html", email=email), 403
        start_session(user)
        audit("login")
        get_db().commit()
        return redirect(safe_next(request.args.get("next") or url_for("account")))
    return render_template("account/login.html")


@app.post("/logout")
def logout():
    lang = session.get("lang")
    session.clear()
    session["lang"] = lang or "tj"
    flash(t("logged_out"), "success")
    return redirect(url_for("index"))


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        if not rate_limited(f"f:{client_ip()}", 5, 3600):
            db = get_db()
            user = db.execute("SELECT * FROM users WHERE email=? AND status<>'blocked'", (email,)).fetchone()
            if user:
                token = secrets.token_urlsafe(32)
                expires = (datetime.utcnow() + timedelta(hours=1)).isoformat(timespec="seconds")
                db.execute("UPDATE users SET reset_hash=?, reset_expires=? WHERE id=?",
                           (_hash_token(token), expires, user["id"]))
                queue_mail(user["email"], user["lang"] or current_lang(), "mail_reset_s", "mail_reset_b",
                           user_id=user["id"], footer=False, url=abs_url("reset", token=token))
                db.commit()
        flash(t("forgot_sent"), "success")  # same answer either way: no account probing
        return redirect(url_for("login"))
    return render_template("account/forgot.html")


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset(token):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE reset_hash=?", (_hash_token(token),)).fetchone()
    if not user or not user["reset_expires"] or user["reset_expires"] < datetime.utcnow().isoformat(timespec="seconds"):
        flash(t("reset_invalid"), "error")
        return redirect(url_for("forgot"))
    if request.method == "POST":
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        if len(pw) < 10 or pw != pw2:
            flash(t("err_password"), "error")
            return render_template("account/reset.html"), 400
        db.execute("UPDATE users SET password_hash=?, reset_hash=NULL, reset_expires=NULL WHERE id=?",
                   (generate_password_hash(pw), user["id"]))
        g.user = user
        audit("user.password_reset", f"user:{user['id']}")
        g.user = None
        db.commit()
        flash(t("reset_done"), "success")
        return redirect(url_for("login"))
    return render_template("account/reset.html")


def collect_account_form(fields):
    form = {name: (request.form.get(name) or "").strip()[:300] for name, _ in fields}
    form["email"] = form["email"].lower()
    errors = []
    missing = {name for name, req in fields if req and not form[name]}
    if missing:
        errors.append(t("err_required"))
    if form["email"] and not EMAIL_RE.match(form["email"]):
        errors.append(t("err_email"))
    for ph in ("phone", "head_phone"):
        if form.get(ph) and not PHONE_RE.match(form[ph]):
            errors.append(t("err_phone"))
            break
    return form, errors, missing


@app.route("/register", methods=["GET", "POST"])
def register():
    acc_type = request.values.get("type")
    if acc_type not in ACCOUNT_FIELDS:
        return render_template("account/register_choose.html")
    fields = ACCOUNT_FIELDS[acc_type]
    form, errors, missing = {}, [], set()
    if request.method == "POST":
        form, errors, missing = collect_account_form(fields)
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        if len(pw) < 10 or pw != pw2:
            errors.append(t("err_password"))
        if not request.form.get("consent"):
            errors.append(t("err_consent"))
        db = get_db()
        if not errors and db.execute("SELECT 1 FROM users WHERE email=?", (form["email"],)).fetchone():
            errors.append(t("err_email_taken"))
        if not errors and rate_limited(f"r:{client_ip()}", 5, 3600):
            errors.append(t("login_rate"))
        if not errors:
            status = "pending"
            if acc_type == "individual" and not app.config["REQUIRE_APPROVAL"]:
                status = "active"
            cols = [name for name, _ in fields]
            cur = db.execute(
                f"INSERT INTO users (account_type, {', '.join(cols)}, password_hash, status, lang) "
                f"VALUES (?, {', '.join('?' * len(cols))}, ?, ?, ?)",
                [acc_type] + [form[c] or None for c in cols] + [generate_password_hash(pw), status, current_lang()])
            user = db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
            start_session(user)
            audit("user.register", f"user:{user['id']}")
            db.commit()
            flash(t("register_done_active" if status == "active" else "register_done_pending"), "success")
            return redirect(url_for("account"))
        return render_template("account/register.html", acc_type=acc_type, fields=fields, form=form,
                               errors=errors, missing=missing), 400
    return render_template("account/register.html", acc_type=acc_type, fields=fields, form=form,
                           errors=[], missing=set())


@app.route("/account")
@login_required
def account():
    db = get_db()
    comments = db.execute(
        f"SELECT c.*, d.title_tj, d.title_ru, d.kind, {status_sql('d')} AS doc_status FROM comments c "
        "JOIN documents d ON d.id=c.document_id WHERE c.user_id=? ORDER BY c.created_at DESC",
        (g.user["id"],)).fetchall()
    docs = []
    if g.user["role"] in ("developer", "admin"):
        docs, _ = search_documents(owner_id=g.user["id"], include_hidden=True, per_page=50)
    subs = db.execute(
        f"SELECT s.*, d.title_tj, d.title_ru, {status_sql('d')} AS doc_status FROM subscriptions s "
        "LEFT JOIN documents d ON d.id=s.document_id WHERE s.user_id=? ORDER BY s.created_at DESC",
        (g.user["id"],)).fetchall()
    return render_template("account/account.html", comments=comments, docs=docs, subs=subs,
                           fields=ACCOUNT_FIELDS[g.user["account_type"]])


@app.route("/account/edit", methods=["GET", "POST"])
@login_required
def account_edit():
    u = g.user
    fields = [(n, r) for n, r in ACCOUNT_FIELDS[u["account_type"]] if n != "email"]
    errors, missing = [], set()
    form = dict(u)
    if request.method == "POST":
        action = request.form.get("action")
        db = get_db()
        if action == "password":
            if not check_password_hash(u["password_hash"], request.form.get("current_password", "")):
                errors.append(t("wrong_password"))
            pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
            if len(pw) < 10 or pw != pw2:
                errors.append(t("err_password"))
            if not errors:
                db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), u["id"]))
                audit("user.password_change", f"user:{u['id']}")
                db.commit()
                flash(t("password_changed"), "success")
                return redirect(url_for("account"))
        else:
            form = {name: (request.form.get(name) or "").strip()[:300] for name, _ in fields}
            missing = {name for name, req in fields if req and not form[name]}
            if missing:
                errors.append(t("err_required"))
            for ph in ("phone", "head_phone"):
                if form.get(ph) and not PHONE_RE.match(form[ph]):
                    errors.append(t("err_phone"))
                    break
            if not errors:
                sets = ", ".join(f"{n}=?" for n, _ in fields)
                db.execute(f"UPDATE users SET {sets}, notify_email=? WHERE id=?",
                           [form[n] or None for n, _ in fields] + [1 if request.form.get("notify_email") else 0, u["id"]])
                audit("user.profile_update", f"user:{u['id']}")
                db.commit()
                flash(t("saved"), "success")
                return redirect(url_for("account"))
            form["notify_email"] = 1 if request.form.get("notify_email") else 0
    return render_template("account/edit.html", fields=fields, form=form, errors=errors, missing=missing), \
        (400 if errors else 200)


@app.route("/account/application")
@login_required
def application():
    return render_application(g.user)


def render_application(user):
    return render_template("account/application.html", u=user, annex=ANNEX_NO[user["account_type"]],
                           fields=ACCOUNT_FIELDS[user["account_type"]])


# ---------------------------------------------------------------------------
# Management (developers = state-body staff; admin = authorised body)
# ---------------------------------------------------------------------------
def manage_scope():
    """SQL fragment restricting developers to their own documents."""
    return ("", []) if is_admin() else ("AND d.owner_id=?", [g.user["id"]])


@app.route("/manage")
@roles_required("developer", "admin")
def manage():
    db = get_db()
    scope, sp = manage_scope()
    stats_ = {k: db.execute(f"SELECT COUNT(*) FROM documents d WHERE kind=? {scope}", [k] + sp).fetchone()[0]
              for k in KINDS}
    awaiting = db.execute(
        f"SELECT c.*, d.title_tj, d.title_ru, u.full_name, u.org_name, u.account_type FROM comments c "
        f"JOIN documents d ON d.id=c.document_id JOIN users u ON u.id=c.user_id "
        f"WHERE c.decision IS NULL AND c.status='visible' {scope} ORDER BY c.created_at LIMIT 50", sp).fetchall()
    returned = db.execute(f"SELECT d.* FROM documents d WHERE returned_note IS NOT NULL {scope}", sp).fetchall()
    awaiting_conclusion, pending_users, awaiting_act, new_messages = [], [], [], 0
    if is_admin():
        awaiting_conclusion = db.execute(
            f"SELECT d.*, {status_sql('d')} AS status FROM documents d WHERE ({status_sql('d')})='review' "
            "ORDER BY consult_end").fetchall()
        pending_users = db.execute("SELECT * FROM users WHERE status='pending' ORDER BY created_at").fetchall()
        awaiting_act = db.execute(
            "SELECT d.* FROM documents d WHERE d.kind='draft' AND d.outcome='adopted' AND NOT EXISTS "
            "(SELECT 1 FROM documents a WHERE a.related_id=d.id AND a.kind='act') ORDER BY d.outcome_date").fetchall()
        new_messages = db.execute("SELECT COUNT(*) FROM messages WHERE status='new'").fetchone()[0]
    return render_template("manage/dashboard.html", stats=stats_, awaiting=awaiting, returned=returned,
                           awaiting_conclusion=awaiting_conclusion, pending_users=pending_users,
                           awaiting_act=awaiting_act, new_messages=new_messages, display_name=display_name)


@app.route("/manage/documents")
@roles_required("developer", "admin")
def manage_documents():
    a = request.args
    page = page_arg()
    kind = a.get("kind") if a.get("kind") in KINDS else None
    rows, total = search_documents(kind, a.get("q", ""), page=page, per_page=25, include_hidden=True,
                                   owner_id=None if is_admin() else g.user["id"])
    return render_template("manage/documents.html", rows=rows, total=total, page=page, kind=kind,
                           pages=max((total + 24) // 25, 1))


def save_uploads(doc_id):
    for f in request.files.getlist("files"):
        if not f or not f.filename:
            continue
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
        if ext not in ALLOWED_EXT:
            flash(f"{t('err_file')} ({f.filename})", "error")
            continue
        stored = f"{uuid.uuid4().hex}.{ext}"
        path = os.path.join(UPLOAD_DIR, stored)
        f.save(path)
        original = secure_filename(f.filename) if not re.search(r"[^\x00-\x7f]", f.filename) else \
            re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", f.filename)[:150]
        get_db().execute("INSERT INTO attachments (document_id, stored_name, original_name, size) VALUES (?,?,?,?)",
                         (doc_id, stored, original, os.path.getsize(path)))


TEXT_COLS = ("doc_type", "number", "title_tj", "title_ru", "developer_tj", "developer_ru", "summary_tj",
             "summary_ru", "body_tj", "body_ru", "outcome_note_tj", "outcome_note_ru", "source_url",
             "publication_tj", "publication_ru", "mj_reg_number")
DATE_COLS = ("doc_date", "dev_deadline", "adopt_deadline", "consult_start", "consult_end", "outcome_date",
             "effective_date", "mj_reg_date")


def save_links(doc_id, link_type, ids):
    db = get_db()
    db.execute("DELETE FROM doc_links WHERE from_id=? AND link_type=?", (doc_id, link_type))
    for target in ids:
        if target == doc_id or not db.execute("SELECT 1 FROM documents WHERE id=? AND kind='act'", (target,)).fetchone():
            continue
        db.execute("INSERT OR IGNORE INTO doc_links (from_id, to_id, link_type) VALUES (?,?,?)",
                   (doc_id, target, link_type))
        if link_type == "repeals":
            db.execute("UPDATE documents SET legal_status='repealed', updated_at=datetime('now') WHERE id=?", (target,))


@app.route("/manage/documents/new", methods=["GET", "POST"])
@app.route("/manage/documents/<int:doc_id>/edit", methods=["GET", "POST"])
@roles_required("developer", "admin")
def edit_document(doc_id=None):
    db = get_db()
    doc = get_document(doc_id) if doc_id else None
    if doc_id and (not doc or not can_edit(doc)):
        abort(404)
    allowed_kinds = KINDS if is_admin() else ("plan", "draft")
    errors = []
    if request.method == "POST":
        f = request.form
        kind = f.get("kind") if not doc else doc["kind"]
        if kind not in allowed_kinds:
            errors.append(t("err_kind_forbidden"))
        vals = {c: (f.get(c) or "").strip() or None for c in TEXT_COLS}
        if vals["doc_type"] and vals["doc_type"] not in DOC_TYPES:
            vals["doc_type"] = "other"
        if vals["source_url"] and not re.match(r"^https?://", vals["source_url"]):
            vals["source_url"] = None
        for c in DATE_COLS:
            vals[c] = f.get(c) if parse_date(f.get(c)) else None
        vals["affects_business"] = 1 if f.get("affects_business") else 0
        vals["published"] = 1 if f.get("published") else 0
        vals["legal_status"] = f.get("legal_status") if kind == "act" and f.get("legal_status") in LEGAL_STATUSES \
            else ("in_force" if kind == "act" else None)
        outcome = f.get("outcome")
        vals["outcome"] = outcome if outcome in ("proceed", "adopted", "rejected") else None
        if vals["outcome"] and not vals["outcome_date"]:
            vals["outcome_date"] = today().isoformat()
        rel = (f.get("related_id") or "").strip()
        vals["related_id"] = None
        if rel:
            parent = get_document(int(rel)) if rel.isdigit() else None
            if not parent or (doc and parent["id"] == doc["id"]):
                errors.append(t("err_related"))
            else:
                vals["related_id"] = parent["id"]
        details = {}
        for key in DETAIL_FIELDS:
            entry = {lg: (f.get(f"d_{key}_{lg}") or "").strip() for lg in LANGS}
            if any(entry.values()):
                details[key] = entry
        vals["details"] = json.dumps(details, ensure_ascii=False)
        if is_admin():
            vals["conclusion_tj"] = (f.get("conclusion_tj") or "").strip() or None
            vals["conclusion_ru"] = (f.get("conclusion_ru") or "").strip() or None
            vals["conclusion_date"] = f.get("conclusion_date") if parse_date(f.get("conclusion_date")) else None
            if (vals["conclusion_tj"] or vals["conclusion_ru"]) and not vals["conclusion_date"]:
                vals["conclusion_date"] = today().isoformat()
        if vals["published"] and doc and doc["returned_note"]:
            vals["returned_note"] = None  # resubmitted after corrections
        if not vals["title_tj"]:
            errors.append(t("err_title"))
        if kind in ("plan", "draft"):
            cs, ce = parse_date(vals["consult_start"]), parse_date(vals["consult_end"])
            if not cs:
                vals["consult_start"] = today().isoformat()
                cs = today()
            if not ce or (ce - cs).days < CONSULT_MIN_DAYS:
                errors.append(t("err_consult_30"))
            if not vals["doc_date"]:
                vals["doc_date"] = vals["consult_start"]
            if not vals["developer_tj"] and g.user["org_name"]:
                vals["developer_tj"] = g.user["org_name"]
        if not errors:
            if doc:
                sets = ", ".join(f"{k}=?" for k in vals)
                db.execute(f"UPDATE documents SET {sets}, updated_at=datetime('now') WHERE id=?",
                           list(vals.values()) + [doc["id"]])
                new_id = doc["id"]
                audit("document.update", f"doc:{new_id}")
            else:
                vals["kind"] = kind
                vals["owner_id"] = g.user["id"]
                cur = db.execute(f"INSERT INTO documents ({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})",
                                 list(vals.values()))
                new_id = cur.lastrowid
                audit("document.create", f"doc:{new_id}")
            if kind == "act":
                save_links(new_id, "amends", parse_ids(f.get("amends")))
                save_links(new_id, "repeals", parse_ids(f.get("repeals")))
            save_uploads(new_id)
            # notifications
            new = get_document(new_id)
            if new["published"] and (not doc or not doc["published"]):
                notify_new_document(new)
            elif doc and doc["published"] and kind != "act":
                changed_conclusion = (new["conclusion_tj"] or new["conclusion_ru"]) and \
                    (new["conclusion_tj"], new["conclusion_ru"]) != (doc["conclusion_tj"], doc["conclusion_ru"])
                changed_outcome = new["outcome"] != doc["outcome"]
                changed_dates = (new["consult_end"], new["consult_start"]) != (doc["consult_end"], doc["consult_start"])
                if changed_conclusion or changed_outcome or changed_dates:
                    note_tj = new["outcome_note_tj"] if changed_outcome else (new["conclusion_tj"] or "")
                    note_ru = new["outcome_note_ru"] if changed_outcome else (new["conclusion_ru"] or "")
                    notify_status_change(new, note_tj or "", note_ru or "")
            db.commit()
            flash(t("saved"), "success")
            return redirect(url_for("edit_document", doc_id=new_id))
        form = dict(f)
        form["kind"] = kind
        form["details"] = details
    else:
        if doc:
            form = dict(doc)
            form["details"] = load_details(doc)
            form["amends"] = ", ".join(str(r[0]) for r in db.execute(
                "SELECT to_id FROM doc_links WHERE from_id=? AND link_type='amends'", (doc_id,)))
            form["repeals"] = ", ".join(str(r[0]) for r in db.execute(
                "SELECT to_id FROM doc_links WHERE from_id=? AND link_type='repeals'", (doc_id,)))
        else:
            k = request.args.get("kind")
            start = today()
            form = {"kind": k if k in allowed_kinds else allowed_kinds[0], "published": 1,
                    "consult_start": start.isoformat(),
                    "consult_end": (start + timedelta(days=CONSULT_MIN_DAYS)).isoformat(),
                    "developer_tj": g.user["org_name"], "details": {}, "legal_status": "in_force"}
    attachments = db.execute("SELECT * FROM attachments WHERE document_id=?", (doc_id,)).fetchall() if doc else []
    comment_count = db.execute("SELECT COUNT(*) FROM comments WHERE document_id=?", (doc_id,)).fetchone()[0] \
        if doc else 0
    return render_template("manage/edit.html", doc=doc, form=form, errors=errors, attachments=attachments,
                           allowed_kinds=allowed_kinds, DETAIL_FIELDS=DETAIL_FIELDS, comment_count=comment_count,
                           RIA_FIELDS=RIA_FIELDS, BUSINESS_FIELDS=BUSINESS_FIELDS), (400 if errors else 200)


@app.post("/manage/documents/<int:doc_id>/return")
@roles_required("admin")
def return_document(doc_id):
    doc = get_document(doc_id)
    if not doc or doc["kind"] == "act":
        abort(404)
    reason = (request.form.get("reason") or "").strip()[:2000]
    if not reason:
        flash(t("err_required"), "error")
        return redirect(url_for("edit_document", doc_id=doc_id))
    db = get_db()
    db.execute("UPDATE documents SET published=0, returned_note=?, updated_at=datetime('now') WHERE id=?",
               (reason, doc_id))
    if doc["owner_id"]:
        owner = db.execute("SELECT * FROM users WHERE id=?", (doc["owner_id"],)).fetchone()
        if owner and owner["id"] != g.user["id"]:
            queue_mail(owner["email"], owner["lang"], "mail_returned_s", "mail_returned_b", user_id=owner["id"],
                       title=doc_title(doc, owner["lang"]), reason=reason,
                       url=abs_url("edit_document", doc_id=doc_id))
    audit("document.return", f"doc:{doc_id}")
    db.commit()
    flash(t("returned_done"), "success")
    return redirect(url_for("edit_document", doc_id=doc_id))


@app.post("/manage/documents/<int:doc_id>/delete")
@roles_required("developer", "admin")
def delete_document(doc_id):
    doc = get_document(doc_id)
    if not doc or not can_edit(doc):
        abort(404)
    db = get_db()
    for a in db.execute("SELECT stored_name FROM attachments WHERE document_id=?", (doc_id,)):
        try:
            os.remove(os.path.join(UPLOAD_DIR, a["stored_name"]))
        except OSError:
            pass
    db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    audit("document.delete", f"doc:{doc_id} {doc['title_tj'][:80]}")
    db.commit()
    flash(t("deleted"), "success")
    return redirect(url_for("manage_documents"))


@app.post("/manage/attachments/<int:att_id>/delete")
@roles_required("developer", "admin")
def delete_attachment(att_id):
    db = get_db()
    att = db.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()
    doc = get_document(att["document_id"]) if att else None
    if not doc or not can_edit(doc):
        abort(404)
    try:
        os.remove(os.path.join(UPLOAD_DIR, att["stored_name"]))
    except OSError:
        pass
    db.execute("DELETE FROM attachments WHERE id=?", (att_id,))
    audit("attachment.delete", f"doc:{doc['id']}")
    db.commit()
    return redirect(url_for("edit_document", doc_id=doc["id"]))


@app.route("/manage/documents/<int:doc_id>/proposals.csv")
@roles_required("developer", "admin")
def proposals_csv(doc_id):
    doc = get_document(doc_id)
    if not doc or not can_edit(doc):
        abort(404)
    rows = get_db().execute(
        "SELECT c.*, u.full_name, u.org_name, u.account_type, u.email FROM comments c JOIN users u ON u.id=c.user_id "
        "WHERE c.document_id=? ORDER BY c.created_at", (doc_id,)).fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["№", t("date"), t("author"), t("kind_label"), t("article_ref"), t("comment_label"),
                t("outcome"), t("response"), t("filter_status")])
    for i, c in enumerate(rows, 1):
        w.writerow([i, c["created_at"], display_name(c) if c["account_type"] != "individual" else c["full_name"],
                    t("acc_" + c["account_type"]), c["article_ref"] or t("article_general"), c["body"],
                    t("decision_" + (c["decision"] or "none")), c["response"] or "",
                    t("hidden_comment") if c["status"] == "hidden" else ""])
    return Response("﻿" + out.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=proposals_{doc_id}.csv"})


@app.post("/manage/comments/<int:comment_id>")
@roles_required("developer", "admin")
def respond_comment(comment_id):
    db = get_db()
    c = db.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
    doc = get_document(c["document_id"]) if c else None
    if not doc or not can_edit(doc):
        abort(404)
    action = request.form.get("action")
    if action in ("hide", "show"):
        if not is_admin():
            abort(403)
        db.execute("UPDATE comments SET status=? WHERE id=?", ("hidden" if action == "hide" else "visible", comment_id))
        audit(f"comment.{action}", f"comment:{comment_id}")
    else:
        decision = request.form.get("decision")
        decision = decision if decision in ("accepted", "partial", "rejected") else None
        response = (request.form.get("response") or "").strip()[:COMMENT_MAX] or None
        db.execute("UPDATE comments SET decision=?, response=?, responded_at=datetime('now') WHERE id=?",
                   (decision, response, comment_id))
        audit("comment.respond", f"comment:{comment_id}")
        if (decision, response) != (c["decision"], c["response"]) and (decision or response):
            author = db.execute("SELECT * FROM users WHERE id=?", (c["user_id"],)).fetchall()
            url = abs_url("document", doc_id=doc["id"]) + f"#c{comment_id}"
            notify_users(author, "mail_response_s", "mail_response_b",
                         lambda lg: {"title": doc_title(doc, lg), "decision": tr("decision_" + (decision or "none"), lg),
                                     "response": response or "—", "url": url})
    db.commit()
    flash(t("saved"), "success")
    return redirect(safe_next(request.form.get("next")) if request.form.get("next")
                    else url_for("document", doc_id=doc["id"]) + f"#c{comment_id}")


@app.route("/manage/users")
@roles_required("admin")
def manage_users():
    status = request.args.get("status", "pending")
    q = (request.args.get("q") or "").strip().lower()
    where, params = [], []
    if status in ("active", "pending", "blocked"):
        where.append("status=?")
        params.append(status)
    if q:
        where.append("ulower(full_name||' '||email||' '||coalesce(org_name,'')) LIKE ?")
        params.append(f"%{q}%")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    users = get_db().execute(f"SELECT * FROM users {clause} ORDER BY created_at DESC LIMIT 200", params).fetchall()
    return render_template("manage/users.html", users=users, status=status, q=q)


@app.post("/manage/users/<int:user_id>")
@roles_required("admin")
def update_user(user_id):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not u or u["id"] == g.user["id"]:
        abort(404)
    action = request.form.get("action")
    if action == "approve":
        db.execute("UPDATE users SET status='active', approved_at=datetime('now'), approved_by=?, admin_note=NULL "
                   "WHERE id=?", (g.user["id"], user_id))
        queue_mail(u["email"], u["lang"], "mail_approved_s", "mail_approved_b", user_id=u["id"], url=abs_url("index"))
    elif action == "reject":
        reason = (request.form.get("reason") or "").strip()[:1000] or "—"
        db.execute("UPDATE users SET status='blocked', admin_note=? WHERE id=?", (reason, user_id))
        queue_mail(u["email"], u["lang"], "mail_rejected_s", "mail_rejected_b", user_id=u["id"], footer=False,
                   reason=reason)
    elif action == "block":
        db.execute("UPDATE users SET status='blocked' WHERE id=?", (user_id,))
    elif action == "unblock":
        db.execute("UPDATE users SET status='active' WHERE id=?", (user_id,))
    elif action == "make_developer" and u["account_type"] == "state_body":
        db.execute("UPDATE users SET role='developer' WHERE id=?", (user_id,))
    elif action == "make_user" and u["role"] != "admin":
        db.execute("UPDATE users SET role='user' WHERE id=?", (user_id,))
    elif action == "reset_password" and u["role"] != "admin":
        pw = secrets.token_urlsafe(9)
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), user_id))
        flash(t("temp_password_is").format(email=u["email"], pw=pw), "success")
    else:
        abort(400)
    audit(f"user.{action}", f"user:{user_id}")
    db.commit()
    if action != "reset_password":
        flash(t("saved"), "success")
    return redirect(safe_next(request.form.get("next")))


@app.route("/manage/users/<int:user_id>/application")
@roles_required("admin")
def user_application(user_id):
    u = get_db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not u:
        abort(404)
    return render_application(u)


@app.route("/manage/audit")
@roles_required("admin")
def manage_audit():
    rows = get_db().execute("SELECT a.*, u.email FROM audit_log a LEFT JOIN users u ON u.id=a.user_id "
                            "ORDER BY a.id DESC LIMIT 300").fetchall()
    return render_template("manage/audit.html", rows=rows)


@app.route("/manage/news")
@roles_required("admin")
def manage_news():
    rows = get_db().execute("SELECT * FROM news ORDER BY created_at DESC").fetchall()
    return render_template("manage/news.html", rows=rows)


@app.route("/manage/news/new", methods=["GET", "POST"])
@app.route("/manage/news/<int:news_id>", methods=["GET", "POST"])
@roles_required("admin")
def edit_news(news_id=None):
    db = get_db()
    n = db.execute("SELECT * FROM news WHERE id=?", (news_id,)).fetchone() if news_id else None
    if news_id and not n:
        abort(404)
    if request.method == "POST":
        if request.form.get("action") == "delete" and n:
            db.execute("DELETE FROM news WHERE id=?", (news_id,))
            audit("news.delete", f"news:{news_id}")
            db.commit()
            flash(t("deleted"), "success")
            return redirect(url_for("manage_news"))
        vals = {k: (request.form.get(k) or "").strip() or None for k in ("title_tj", "title_ru", "body_tj", "body_ru")}
        vals["published"] = 1 if request.form.get("published") else 0
        if not vals["title_tj"]:
            flash(t("err_title"), "error")
            return render_template("manage/news_edit.html", n=n, form=vals), 400
        if n:
            db.execute("UPDATE news SET title_tj=?, title_ru=?, body_tj=?, body_ru=?, published=? WHERE id=?",
                       list(vals.values()) + [news_id])
        else:
            news_id = db.execute("INSERT INTO news (title_tj, title_ru, body_tj, body_ru, published) VALUES (?,?,?,?,?)",
                                 list(vals.values())).lastrowid
        audit("news.save", f"news:{news_id}")
        db.commit()
        flash(t("saved"), "success")
        return redirect(url_for("manage_news"))
    return render_template("manage/news_edit.html", n=n, form=dict(n) if n else {"published": 1})


@app.route("/manage/messages")
@roles_required("admin")
def manage_messages():
    status = request.args.get("status", "new")
    rows = get_db().execute("SELECT * FROM messages " + ("WHERE status=? " if status in ("new", "answered", "closed")
                                                         else "") + "ORDER BY created_at DESC LIMIT 200",
                            (status,) if status in ("new", "answered", "closed") else ()).fetchall()
    return render_template("manage/messages.html", rows=rows, status=status)


@app.post("/manage/messages/<int:msg_id>")
@roles_required("admin")
def answer_message(msg_id):
    db = get_db()
    m = db.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
    if not m:
        abort(404)
    if request.form.get("action") == "close":
        db.execute("UPDATE messages SET status='closed' WHERE id=?", (msg_id,))
    else:
        answer = (request.form.get("answer") or "").strip()[:COMMENT_MAX]
        if not answer:
            flash(t("err_required"), "error")
            return redirect(url_for("manage_messages"))
        db.execute("UPDATE messages SET status='answered', answer=?, answered_at=datetime('now') WHERE id=?",
                   (answer, msg_id))
        user = db.execute("SELECT lang FROM users WHERE id=?", (m["user_id"],)).fetchone() if m["user_id"] else None
        lang = (user["lang"] if user else None) or current_lang()
        queue_mail(m["email"], lang, "mail_answer_s", "mail_answer_b", footer=False,
                   subject=m["subject"], answer=answer, site=tr("site_name", lang))
    audit("message.update", f"message:{msg_id}")
    db.commit()
    flash(t("saved"), "success")
    return redirect(url_for("manage_messages", status=request.form.get("back", "new")))


@app.route("/manage/settings", methods=["GET", "POST"])
@roles_required("admin")
def manage_settings():
    if request.method == "POST":
        for k in SETTING_KEYS:
            set_setting(k, (request.form.get(k) or "").strip()[:500] or None)
        f = request.files.get("logo")
        if request.form.get("remove_logo"):
            old = get_settings().get("logo_file")
            if old:
                try:
                    os.remove(os.path.join(BRAND_DIR, old))
                except OSError:
                    pass
            set_setting("logo_file", None)
        elif f and f.filename:
            ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
            data = f.read(1024 * 1024 + 1)
            if ext not in LOGO_EXT or len(data) > 1024 * 1024 or not data:
                flash(t("err_logo"), "error")
                return redirect(url_for("manage_settings"))
            name = f"logo.{'jpg' if ext == 'jpeg' else ext}"
            for old in os.listdir(BRAND_DIR):
                if old.startswith("logo."):
                    os.remove(os.path.join(BRAND_DIR, old))
            with open(os.path.join(BRAND_DIR, name), "wb") as out:
                out.write(data)
            set_setting("logo_file", name)
            set_setting("logo_version", str(int(time.time())))
        audit("settings.update")
        get_db().commit()
        flash(t("saved"), "success")
        return redirect(url_for("manage_settings"))
    return render_template("manage/settings.html", s=get_settings(), keys=SETTING_KEYS)


def import_rows(reader, owner_id):
    """Insert documents from CSV rows. Returns (imported, skipped, errors)."""
    db = get_db() if has_request_context() else None
    conn = db or sqlite3.connect(DB_PATH)
    n = skipped = errors = 0
    for row in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        kind = row.get("kind") or "act"
        title = row.get("title_tj")
        if kind not in KINDS or not title:
            errors += 1
            continue
        doc_date = row.get("doc_date") if parse_date(row.get("doc_date")) else None
        doc_type = row.get("doc_type") if row.get("doc_type") in DOC_TYPES else ("other" if row.get("doc_type") else None)
        dup = conn.execute("SELECT 1 FROM documents WHERE kind=? AND coalesce(number,'')=? AND coalesce(doc_date,'')=? "
                           "AND coalesce(doc_type,'')=? AND title_tj=?",
                           (kind, row.get("number", ""), doc_date or "", doc_type or "", title)).fetchone()
        if dup:
            skipped += 1
            continue
        legal = row.get("legal_status") if row.get("legal_status") in LEGAL_STATUSES else \
            ("in_force" if kind == "act" else None)
        src = row.get("source_url") if re.match(r"^https?://", row.get("source_url") or "") else None
        conn.execute(
            "INSERT INTO documents (kind, doc_type, number, doc_date, title_tj, title_ru, developer_tj, developer_ru, "
            "summary_tj, summary_ru, body_tj, body_ru, publication_tj, legal_status, source_url, owner_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, doc_type, row.get("number") or None, doc_date, title, row.get("title_ru") or None,
             row.get("developer_tj") or None, row.get("developer_ru") or None, row.get("summary_tj") or None,
             row.get("summary_ru") or None, row.get("body_tj") or None, row.get("body_ru") or None,
             row.get("publication_tj") or None, legal, src, owner_id))
        n += 1
    conn.commit()
    if not db:
        conn.close()
    return n, skipped, errors


@app.route("/manage/import", methods=["GET", "POST"])
@roles_required("admin")
def manage_import():
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash(t("err_required"), "error")
            return redirect(url_for("manage_import"))
        text = f.read().decode("utf-8-sig", errors="replace")
        n, s, e = import_rows(csv.DictReader(io.StringIO(text)), g.user["id"])
        audit("documents.import", f"{n} rows")
        get_db().commit()
        flash(t("import_done").format(n=n, s=s, e=e), "success" if not e else "error")
        return redirect(url_for("manage_import"))
    return render_template("manage/import.html")


# ---------------------------------------------------------------------------
# Open data: JSON API, Atom feed, sitemap
# ---------------------------------------------------------------------------
def doc_json(d, full=False):
    out = {
        "id": d["id"], "kind": d["kind"], "type": d["doc_type"], "number": d["number"], "date": d["doc_date"],
        "title": {"tj": d["title_tj"], "ru": d["title_ru"]},
        "developer": {"tj": d["developer_tj"], "ru": d["developer_ru"]},
        "status": d["status"], "consultation": {"start": d["consult_start"], "end": d["consult_end"]},
        "legal_status": d["legal_status"] if d["kind"] == "act" else None,
        "url": url_for("document", doc_id=d["id"], _external=True),
    }
    if full:
        out.update({
            "summary": {"tj": d["summary_tj"], "ru": d["summary_ru"]},
            "text": {"tj": d["body_tj"], "ru": d["body_ru"]},
            "details": load_details(d), "affects_business": bool(d["affects_business"]),
            "deadlines": {"development": d["dev_deadline"], "adoption": d["adopt_deadline"]},
            "conclusion": {"tj": d["conclusion_tj"], "ru": d["conclusion_ru"], "date": d["conclusion_date"]},
            "outcome": {"value": d["outcome"], "date": d["outcome_date"],
                        "note": {"tj": d["outcome_note_tj"], "ru": d["outcome_note_ru"]}},
            "effective_date": d["effective_date"],
            "publication": {"tj": d["publication_tj"], "ru": d["publication_ru"]},
            "related_id": d["related_id"], "source_url": d["source_url"],
        })
    return out


@app.route("/api/documents")
def api_documents():
    kind = request.args.get("kind") if request.args.get("kind") in KINDS else None
    page = page_arg()
    try:
        per = min(max(int(request.args.get("per_page", 20)), 1), 100)
    except ValueError:
        per = 20
    rows, total = search_documents(kind, page=page, per_page=per, **search_args())
    return jsonify(total=total, page=page, per_page=per, items=[doc_json(r) for r in rows])


@app.route("/api/documents/<int:doc_id>")
def api_document(doc_id):
    d = get_document(doc_id)
    if not d or not d["published"]:
        return jsonify(error="not_found"), 404
    db = get_db()
    out = doc_json(d, full=True)
    out["attachments"] = [{"name": a["original_name"], "size": a["size"],
                           "url": url_for("download", stored_name=a["stored_name"], _external=True)}
                          for a in db.execute("SELECT * FROM attachments WHERE document_id=?", (doc_id,))]
    out["proposals"] = [{"id": c["id"], "date": c["created_at"], "article": c["article_ref"], "text": c["body"],
                         "decision": c["decision"], "response": c["response"]}
                        for c in db.execute("SELECT * FROM comments WHERE document_id=? AND status='visible' "
                                            "ORDER BY created_at", (doc_id,))]
    out["links"] = [{"from": r["from_id"], "to": r["to_id"], "type": r["link_type"]}
                    for r in db.execute("SELECT * FROM doc_links WHERE from_id=? OR to_id=?", (doc_id, doc_id))]
    votes = {r["value"]: r["n"] for r in db.execute(
        "SELECT value, COUNT(*) n FROM votes WHERE document_id=? GROUP BY value", (doc_id,))}
    out["votes"] = {"for": votes.get(1, 0), "against": votes.get(-1, 0)}
    return jsonify(out)


@app.route("/feed.xml")
def feed():
    rows = get_db().execute(f"SELECT d.*, {status_sql('d')} AS status FROM documents d WHERE published=1 "
                            "AND kind IN ('plan','draft') ORDER BY created_at DESC LIMIT 30").fetchall()
    lang = current_lang()
    items = []
    for r in rows:
        link = url_for("document", doc_id=r["id"], _external=True)
        summary = r[f"summary_{lang}"] or r["summary_tj"] or ""
        items.append(
            f"<entry><title>{xml_escape(L(r, 'title'))}</title><link href=\"{xml_escape(link)}\"/>"
            f"<id>{xml_escape(link)}</id><updated>{r['updated_at'].replace(' ', 'T')}Z</updated>"
            f"<summary>{xml_escape(summary)}</summary></entry>")
    updated = rows[0]["updated_at"].replace(" ", "T") + "Z" if rows else datetime.utcnow().isoformat() + "Z"
    xml = (f'<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom" xml:lang="{lang}">'
           f"<title>{xml_escape(t('site_name'))} — {xml_escape(t('nav_drafts'))}</title>"
           f'<link href="{xml_escape(url_for("index", _external=True))}"/>'
           f"<id>{xml_escape(url_for('feed', _external=True))}</id><updated>{updated}</updated>"
           + "".join(items) + "</feed>")
    return Response(xml, mimetype="application/atom+xml")


@app.route("/sitemap.xml")
def sitemap():
    urls = [url_for("index", _external=True)] + [url_for("registry", slug=s, _external=True) for s in KIND_SLUGS]
    urls += [url_for(e, _external=True) for e in ("about", "stats", "developers", "news_list", "contact")]
    urls += [url_for("document", doc_id=r[0], _external=True)
             for r in get_db().execute("SELECT id FROM documents WHERE published=1")]
    body = "".join(f"<url><loc>{xml_escape(u)}</loc></url>" for u in urls)
    return Response(f'<?xml version="1.0" encoding="utf-8"?><urlset '
                    f'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>',
                    mimetype="application/xml")


@app.route("/robots.txt")
def robots():
    return Response(f"User-agent: *\nDisallow: /manage\nDisallow: /account\n"
                    f"Sitemap: {url_for('sitemap', _external=True)}\n", mimetype="text/plain")


@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
def error(e):
    return render_template("error.html", code=e.code), e.code


# ---------------------------------------------------------------------------
# Scheduled jobs and CLI
# ---------------------------------------------------------------------------
def send_reminders():
    """E-mail followers whose consultation closes within REMIND_DAYS (run daily from cron)."""
    with app.test_request_context(base_url=app.config["BASE_URL"]):
        db = get_db()
        limit = (today() + timedelta(days=REMIND_DAYS)).isoformat()
        rows = db.execute(
            "SELECT s.id sid, u.*, d.id doc_id, d.title_tj, d.title_ru, d.consult_end FROM subscriptions s "
            "JOIN users u ON u.id=s.user_id JOIN documents d ON d.id=s.document_id "
            "WHERE s.reminded=0 AND d.published=1 AND d.consult_end BETWEEN ? AND ? AND d.outcome IS NULL",
            (today().isoformat(), limit)).fetchall()
        n = 0
        for r in rows:
            if r["status"] == "active" and r["notify_email"]:
                lg = r["lang"] or "tj"
                queue_mail(r["email"], lg, "mail_reminder_s", "mail_reminder_b", user_id=r["id"],
                           title=r["title_ru"] if lg == "ru" and r["title_ru"] else r["title_tj"],
                           days=(parse_date(r["consult_end"]) - today()).days, end=fmt_date(r["consult_end"]),
                           url=abs_url("document", doc_id=r["doc_id"]) + "#comment-form")
                n += 1
            db.execute("UPDATE subscriptions SET reminded=1 WHERE id=?", (r["sid"],))
        db.commit()
        g.pop("mail_queued", None)
    mailer.deliver_pending(DB_PATH)
    return n


@app.cli.command("init-db")
def cli_init_db():
    init_db()
    click.echo(f"Database ready: {DB_PATH}")


@app.cli.command("seed")
def cli_seed():
    from seed import seed_demo
    init_db()
    seed_demo(DB_PATH)
    click.echo("Demo content added.")


@app.cli.command("create-admin")
@click.argument("email")
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
@click.option("--name", default="Мақоми ваколатдор")
def cli_create_admin(email, password, name):
    init_db()
    create_admin(email, password, name)
    click.echo(f"Admin {email} is ready.")


@app.cli.command("send-mail")
def cli_send_mail():
    init_db()
    click.echo(f"Sent: {mailer.deliver_pending(DB_PATH)}")


@app.cli.command("send-reminders")
def cli_send_reminders():
    init_db()
    click.echo(f"Reminders queued: {send_reminders()}")


@app.cli.command("import-csv")
@click.argument("path", type=click.Path(exists=True))
def cli_import_csv(path):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    admin = conn.execute("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
    conn.close()
    with open(path, encoding="utf-8-sig", newline="") as f:
        n, s, e = import_rows(csv.DictReader(f), admin[0] if admin else None)
    click.echo(f"Imported {n}, skipped {s}, errors {e}")


def create_admin(email, password, name="Мақоми ваколатдор"):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO users (account_type, full_name, org_name, email, password_hash, role, status) "
        "VALUES ('state_body', ?, ?, ?, ?, 'admin', 'active') "
        "ON CONFLICT(email) DO UPDATE SET password_hash=excluded.password_hash, role='admin', status='active'",
        (name, name, email.lower(), generate_password_hash(password)))
    conn.commit()
    conn.close()


@app.route("/cron/reminders")
def cron_reminders():
    """Daily reminder job for Vercel Cron (sends Authorization: Bearer $CRON_SECRET)."""
    secret = os.environ.get("CRON_SECRET")
    if not secret or not secrets.compare_digest(request.headers.get("Authorization", ""), f"Bearer {secret}"):
        abort(404)
    return jsonify(queued=send_reminders())


def bootstrap():
    """Create/upgrade the database, add demo content to an empty one, and make sure an admin exists."""
    init_db()
    if db_is_empty():
        from seed import seed_demo
        seed_demo(DB_PATH)
        print("Demo content added.")
    email = (os.environ.get("ADMIN_EMAIL") or "admin@portal.local").lower()
    pw = os.environ.get("ADMIN_PASSWORD")
    conn = sqlite3.connect(DB_PATH)
    has_admin = conn.execute("SELECT 1 FROM users WHERE role='admin'").fetchone()
    conn.close()
    if pw:
        create_admin(email, pw)  # keeps the admin password in step with the environment variable
    elif not has_admin:
        pw = secrets.token_urlsafe(12)
        create_admin(email, pw)
        print(f"Admin login: {email} / {pw}")


# Keep older databases in step with the code whenever the app is imported.
if ON_VERCEL:
    bootstrap()
else:
    init_db()

if __name__ == "__main__":
    bootstrap()
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 5000
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=port)
