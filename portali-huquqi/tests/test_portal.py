"""End-to-end checks with Flask's test client.  Run:  python -m pytest tests  (or python tests/test_portal.py)"""
import os
import re
import sys
import tempfile
from datetime import date, timedelta

TMP = tempfile.mkdtemp()
os.environ["INSTANCE_DIR"] = TMP
os.environ["DB_PATH"] = os.path.join(TMP, "test.db")
os.environ["REQUIRE_APPROVAL"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as portal  # noqa: E402
from seed import seed_demo  # noqa: E402

portal.init_db()
seed_demo(portal.DB_PATH)
portal.create_admin("admin@test.tj", "admin-password-1")
APP = portal.app
APP.config["TESTING"] = True


def csrf(client, path="/"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client, email, pw):
    portal._hits.clear()  # tests log in many times from one address
    return client.post("/login", data={"email": email, "password": pw, "csrf_token": csrf(client, "/login")})


def doc_id(title_fragment):
    import sqlite3
    conn = sqlite3.connect(portal.DB_PATH)
    row = conn.execute("SELECT id FROM documents WHERE title_tj LIKE ?", (f"%{title_fragment}%",)).fetchone()
    conn.close()
    return row[0]


def test_public_pages_both_languages():
    c = APP.test_client()
    for lang in ("tj", "ru"):
        c.get(f"/lang/{lang}")
        for path in ("/", "/plans", "/drafts", "/acts", "/search?q=портал", "/about", "/register",
                     "/register?type=individual", "/register?type=legal_entity", "/register?type=state_body",
                     "/login", "/feed.xml", "/sitemap.xml", "/robots.txt", "/api/documents"):
            r = c.get(path)
            assert r.status_code == 200, (lang, path, r.status_code)
    assert c.get("/doc/99999").status_code == 404
    assert c.get("/manage").status_code == 302


def test_search_and_filters():
    c = APP.test_client()
    html = c.get("/acts?q=1414").get_data(as_text=True)
    assert "санадҳои меъёрии ҳуқуқӣ" in html
    # Cyrillic case-insensitive search
    assert "Тартиби пешбурди" in c.get("/search?q=ТАРТИБИ").get_data(as_text=True)
    r = c.get("/api/documents?status=consultation").get_json()
    assert r["total"] >= 2 and all(i["status"] == "consultation" for i in r["items"])
    r = c.get("/api/documents?kind=act&year=2018").get_json()
    assert {i["number"] for i in r["items"]} == {"617", "355"}


def test_document_page_shows_ria_and_comments():
    c = APP.test_client()
    did = doc_id("Лоиҳаи Қонун «Дар бораи дастрасӣ")
    html = c.get(f"/doc/{did}").get_data(as_text=True)
    assert "Таҳлили таъсири танзимкунӣ" in html
    assert "Намуна А." in html  # commenter shown as surname + initial
    assert "Роҳи санад" in html  # lifecycle link to the plan
    api = c.get(f"/api/documents/{did}").get_json()
    assert api["details"]["effective_date"]["ru"] and len(api["proposals"]) == 2


def test_registration_approval_and_comment_flow():
    c = APP.test_client()
    token = csrf(c, "/register?type=individual")
    r = c.post("/register?type=individual", data={
        "csrf_token": token, "type": "individual", "full_name": "Каримов Фаридун Сафарович",
        "email": "farid@example.tj", "phone": "+992 900 00 00 00", "address": "ш. Душанбе",
        "password": "very-secret-1", "password2": "very-secret-1", "consent": "1"})
    assert r.status_code == 302
    acc = c.get("/account").get_data(as_text=True)
    assert "Дар баррасӣ" in acc and "Чопи шакли дархост" in acc
    app_html = c.get("/account/application").get_data(as_text=True)
    assert "Замимаи 3" in app_html and "Каримов Фаридун Сафарович" in app_html

    did = doc_id("Лоиҳаи Қонун «Дар бораи дастрасӣ")
    # pending users cannot comment
    c.post(f"/doc/{did}/comment", data={"csrf_token": csrf(c), "body": "Пешниҳоди санҷишӣ дар бораи моддаи 1"})
    assert "Пешниҳоди санҷишӣ" not in c.get(f"/doc/{did}").get_data(as_text=True)

    # admin approves
    a = APP.test_client()
    assert login(a, "admin@test.tj", "admin-password-1").status_code == 302
    import sqlite3
    conn = sqlite3.connect(portal.DB_PATH)
    uid = conn.execute("SELECT id FROM users WHERE email='farid@example.tj'").fetchone()[0]
    conn.close()
    assert "farid@example.tj" in a.get("/manage").get_data(as_text=True)
    a.post(f"/manage/users/{uid}", data={"csrf_token": csrf(a, "/manage"), "action": "approve", "next": "/manage"})

    # now comment works
    c.post(f"/doc/{did}/comment", data={"csrf_token": csrf(c), "body": "Пешниҳоди санҷишӣ дар бораи моддаи 1"})
    assert "Пешниҳоди санҷишӣ" in c.get(f"/doc/{did}").get_data(as_text=True)

    # too short comment rejected
    c.post(f"/doc/{did}/comment", data={"csrf_token": csrf(c), "body": "ok"})
    # comment on closed consultation rejected
    closed = doc_id("хизматрасонии электронӣ")
    c.post(f"/doc/{closed}/comment", data={"csrf_token": csrf(c), "body": "Дер шуд, аммо пешниҳод дорам"})
    assert "Дер шуд" not in c.get(f"/doc/{closed}").get_data(as_text=True)

    # user cannot reach management
    assert c.get("/manage").status_code == 403


def test_csrf_required():
    c = APP.test_client()
    c.post("/login", data={"email": "admin@test.tj", "password": "admin-password-1"})
    assert c.get("/manage").status_code == 302  # no token -> not logged in


def test_developer_workflow_and_30_day_rule():
    # register a state body, admin approves and grants developer role
    c = APP.test_client()
    c.post("/register?type=state_body", data={
        "csrf_token": csrf(c, "/register?type=state_body"), "type": "state_body",
        "org_name": "Вазорати санҷишӣ", "full_name": "Ализода Б.", "position": "Сардори раёсат",
        "address": "ш. Душанбе", "email": "dev@gov.tj", "phone": "+992 37 000 00 00",
        "password": "developer-pass", "password2": "developer-pass", "consent": "1"})
    a = APP.test_client()
    login(a, "admin@test.tj", "admin-password-1")
    import sqlite3
    conn = sqlite3.connect(portal.DB_PATH)
    uid = conn.execute("SELECT id FROM users WHERE email='dev@gov.tj'").fetchone()[0]
    conn.close()
    tok = csrf(a, "/manage")
    a.post(f"/manage/users/{uid}", data={"csrf_token": tok, "action": "approve", "next": "/manage"})
    a.post(f"/manage/users/{uid}", data={"csrf_token": tok, "action": "make_developer", "next": "/manage"})
    assert "Замимаи 1" in a.get(f"/manage/users/{uid}/application").get_data(as_text=True)

    d = APP.test_client()
    login(d, "dev@gov.tj", "developer-pass")
    assert d.get("/manage").status_code == 200
    start = date.today()
    base = {"csrf_token": csrf(d, "/manage/documents/new"), "kind": "plan", "doc_type": "law",
            "title_tj": "Нақшаи санҷишии Қонун", "consult_start": start.isoformat(), "published": "1",
            "d_affected_tj": "Ҳамаи шаҳрвандон"}
    r = d.post("/manage/documents/new", data={**base, "consult_end": (start + timedelta(days=10)).isoformat()})
    assert r.status_code == 400 and "30" in r.get_data(as_text=True)
    r = d.post("/manage/documents/new", data={**base, "consult_end": (start + timedelta(days=30)).isoformat()})
    assert r.status_code == 302
    new_id = int(re.search(r"/documents/(\d+)/edit", r.headers["Location"]).group(1))
    page = APP.test_client().get(f"/doc/{new_id}").get_data(as_text=True)
    assert "Нақшаи санҷишии Қонун" in page and "Вазорати санҷишӣ" in page and "Ҳамаи шаҳрвандон" in page
    # developer cannot post to the acts register
    r = d.post("/manage/documents/new", data={**base, "kind": "act"})
    assert r.status_code == 400
    # developer cannot edit someone else's document
    other = doc_id("хизматрасонии электронӣ")
    assert d.get(f"/manage/documents/{other}/edit").status_code == 404


def test_upload_and_download():
    import io
    a = APP.test_client()
    login(a, "admin@test.tj", "admin-password-1")
    did = doc_id("Тартиби пешбурди")
    page = a.get(f"/manage/documents/{did}/edit").get_data(as_text=True)
    tok = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    data = {"csrf_token": tok, "title_tj": "Тартиби пешбурди Портали интернетии иттилооти ҳуқуқии Ҷумҳурии Тоҷикистон",
            "doc_type": "resolution", "number": "617", "doc_date": "2018-12-29", "published": "1",
            "files": [(io.BytesIO(b"%PDF-1.4 test"), "Тартиб_617.pdf"), (io.BytesIO(b"MZ"), "virus.exe")]}
    r = a.post(f"/manage/documents/{did}/edit", data=data, content_type="multipart/form-data")
    assert r.status_code == 302
    html = APP.test_client().get(f"/doc/{did}").get_data(as_text=True)
    m = re.search(r'href="(/files/[^"]+\.pdf)"', html)
    assert m and "virus" not in html
    r = APP.test_client().get(m.group(1))
    assert r.status_code == 200 and r.data.startswith(b"%PDF")


def outbox(to=None):
    import sqlite3
    conn = sqlite3.connect(portal.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM outbox" + (" WHERE to_email=?" if to else "") + " ORDER BY id",
                        (to,) if to else ()).fetchall()
    conn.close()
    return rows


def make_active_user(email, pw="user-password-1", acc="individual"):
    import sqlite3
    from werkzeug.security import generate_password_hash
    conn = sqlite3.connect(portal.DB_PATH)
    conn.execute("INSERT INTO users (account_type, full_name, email, password_hash, status, lang) "
                 "VALUES (?, 'Тестов Тест', ?, ?, 'active', 'ru')", (acc, email, generate_password_hash(pw)))
    conn.commit()
    conn.close()
    c = APP.test_client()
    login(c, email, pw)
    return c


def test_new_public_pages():
    c = APP.test_client()
    for lang in ("tj", "ru"):
        c.get(f"/lang/{lang}")
        for path in ("/stats", "/developers", "/news", "/contact", "/forgot", "/acts?legal=in_force",
                     "/drafts?dev=Вазорати+адлия+(намуна)", "/search?q=Портали&fulltext=1"):
            r = c.get(path)
            assert r.status_code == 200, (lang, path, r.status_code)
    # full-text search finds words that only appear in the body
    assert "1414" in c.get("/search?q=маблағгузорӣ&fulltext=1&kind=act").get_data(as_text=True)
    did = doc_id("Қонуни Ҷумҳурии Тоҷикистон «Дар бораи санадҳои")
    r = c.get(f"/doc/{did}.txt")
    assert r.status_code == 200 and "Моддаи 39" in r.get_data(as_text=True)
    assert "Ахбори" in c.get(f"/doc/{did}").get_data(as_text=True)


def test_follow_vote_and_notifications():
    u = make_active_user("follower@example.tj")
    did = doc_id("Лоиҳаи Қонун «Дар бораи дастрасӣ")
    u.post(f"/doc/{did}/follow", data={"csrf_token": csrf(u, f"/doc/{did}")})
    assert "Не следить" in u.get(f"/doc/{did}?lang=ru").get_data(as_text=True)
    u.post(f"/doc/{did}/vote", data={"csrf_token": csrf(u, f"/doc/{did}"), "value": "1"})
    api = APP.test_client().get(f"/api/documents/{did}").get_json()
    assert api["votes"]["for"] == 1
    # article-level proposal
    u.post(f"/doc/{did}/comment", data={"csrf_token": csrf(u, f"/doc/{did}"), "article_ref": "Моддаи 2. Дастрасии ройгон",
                                         "body": "Предлагаю уточнить срок размещения"})
    assert "§ Моддаи 2" in u.get(f"/doc/{did}").get_data(as_text=True)
    # admin sets conclusion -> follower gets an update e-mail in Russian
    a = APP.test_client()
    login(a, "admin@test.tj", "admin-password-1")
    page = a.get(f"/manage/documents/{did}/edit").get_data(as_text=True)
    tok = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    import sqlite3
    conn = sqlite3.connect(portal.DB_PATH)
    conn.row_factory = sqlite3.Row
    d = dict(conn.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone())
    conn.close()
    form = {k: (v if v is not None else "") for k, v in d.items() if k in portal.TEXT_COLS + portal.DATE_COLS}
    form.update(csrf_token=tok, published="1", outcome="proceed", outcome_note_ru="Работа продолжается")
    r = a.post(f"/manage/documents/{did}/edit", data=form)
    assert r.status_code == 302, r.get_data(as_text=True)[:500]
    mails = outbox("follower@example.tj")
    assert any("Изменения по документу" in m["subject"] for m in mails), [m["subject"] for m in mails]
    # developer response to the proposal notifies the author
    import sqlite3
    conn = sqlite3.connect(portal.DB_PATH)
    cid = conn.execute("SELECT id FROM comments WHERE body LIKE 'Предлагаю%'").fetchone()[0]
    conn.close()
    a.post(f"/manage/comments/{cid}", data={"csrf_token": tok, "decision": "accepted", "response": "Учтено"})
    assert any("ответили" in m["subject"] for m in outbox("follower@example.tj"))
    # CSV export of proposals
    r = a.get(f"/manage/documents/{did}/proposals.csv")
    assert r.status_code == 200 and "Предлагаю уточнить" in r.data.decode("utf-8-sig")


def test_register_subscription_gets_new_documents():
    u = make_active_user("subscriber@example.tj")
    u.post("/subscribe", data={"csrf_token": csrf(u, "/plans"), "kind": "plan", "next": "/plans"})
    assert "Отменить подписку" in u.get("/plans?lang=ru").get_data(as_text=True)
    a = APP.test_client()
    login(a, "admin@test.tj", "admin-password-1")
    start = date.today()
    a.post("/manage/documents/new", data={
        "csrf_token": csrf(a, "/manage/documents/new"), "kind": "plan", "title_tj": "Нақшаи обунаи санҷишӣ",
        "consult_start": start.isoformat(), "consult_end": (start + timedelta(days=31)).isoformat(), "published": "1"})
    assert any("Нақшаи обунаи санҷишӣ" in m["subject"] for m in outbox("subscriber@example.tj"))


def test_password_reset_and_profile():
    make_active_user("reset@example.tj", "old-password-1")
    c = APP.test_client()
    c.post("/forgot", data={"csrf_token": csrf(c, "/forgot"), "email": "reset@example.tj"})
    mail = outbox("reset@example.tj")[-1]
    url = re.search(r"http\S+/reset/\S+", mail["body"]).group(0)
    path = url.split("localhost", 1)[-1]
    r = c.post(path, data={"csrf_token": csrf(c, path), "password": "new-password-1", "password2": "new-password-1"})
    assert r.status_code == 302
    assert login(APP.test_client(), "reset@example.tj", "old-password-1").status_code == 401
    u = APP.test_client()
    assert login(u, "reset@example.tj", "new-password-1").status_code == 302
    # token is single-use
    assert c.get(path).status_code == 302
    r = u.post("/account/edit", data={"csrf_token": csrf(u, "/account/edit"), "action": "profile",
                                      "full_name": "Нав Ном", "phone": "+992 11 111 11 11", "address": "Хуҷанд"})
    assert r.status_code == 302 and "Нав Ном" in u.get("/account").get_data(as_text=True)


def test_contact_news_settings_import():
    c = APP.test_client()
    c.post("/contact", data={"csrf_token": csrf(c, "/contact"), "name": "Али", "email": "ali@example.tj",
                             "subject": "Савол", "body": "Чӣ тавр бақайдгирӣ кунам?"})
    a = APP.test_client()
    login(a, "admin@test.tj", "admin-password-1")
    page = a.get("/manage/messages").get_data(as_text=True)
    assert "Чӣ тавр бақайдгирӣ" in page
    mid = re.search(r"/manage/messages/(\d+)", page).group(1)
    a.post(f"/manage/messages/{mid}", data={"csrf_token": csrf(a, "/manage/messages"), "answer": "Ба саҳифаи бақайдгирӣ равед."})
    assert any("Савол" in m["subject"] for m in outbox("ali@example.tj"))

    a.post("/manage/news/new", data={"csrf_token": csrf(a, "/manage/news/new"), "title_tj": "Портали нав оғоз шуд",
                                     "body_tj": "Матни хабар", "published": "1"})
    assert "Портали нав оғоз шуд" in APP.test_client().get("/").get_data(as_text=True)

    import io
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>'
    a.post("/manage/settings", data={"csrf_token": csrf(a, "/manage/settings"), "operator_tj": "Оператори санҷишӣ",
                                     "logo": (io.BytesIO(svg), "logo.svg")}, content_type="multipart/form-data")
    home = APP.test_client().get("/").get_data(as_text=True)
    assert "/branding/logo" in home and "Оператори санҷишӣ" in home
    r = APP.test_client().get("/branding/logo")
    assert r.status_code == 200 and "default-src 'none'" in r.headers["Content-Security-Policy"]
    bad = a.post("/manage/settings", data={"csrf_token": csrf(a, "/manage/settings"),
                                           "logo": (io.BytesIO(b"MZ"), "x.exe")}, content_type="multipart/form-data")
    assert bad.status_code == 302

    csv_text = ("kind,doc_type,number,doc_date,title_tj,title_ru,legal_status\n"
                "act,law,9001,2020-01-15,Қонуни воридотӣ,Импортированный закон,in_force\n"
                "act,law,9001,2020-01-15,Қонуни воридотӣ,Импортированный закон,in_force\n"
                "bad,,,,,,\n")
    r = a.post("/manage/import", data={"csrf_token": csrf(a, "/manage/import"),
                                       "file": (io.BytesIO(csv_text.encode()), "acts.csv")},
               content_type="multipart/form-data", follow_redirects=True)
    assert "1" in r.get_data(as_text=True)
    assert APP.test_client().get("/api/documents?q=9001").get_json()["total"] == 1


def test_act_amendments_and_repeal():
    a = APP.test_client()
    login(a, "admin@test.tj", "admin-password-1")
    target = doc_id("Қонуни воридотӣ")
    base = {"csrf_token": csrf(a, "/manage/documents/new"), "kind": "act", "doc_type": "law", "published": "1",
            "doc_date": "2024-01-03"}
    a.post("/manage/documents/new", data={**base, "title_tj": "Қонуни тағйирдиҳанда", "number": "9002",
                                          "amends": str(target)})
    a.post("/manage/documents/new", data={**base, "title_tj": "Қонуни бекоркунанда", "number": "9003",
                                          "repeals": str(target)})
    page = APP.test_client().get(f"/doc/{target}").get_data(as_text=True)
    assert "Қонуни тағйирдиҳанда" in page and "Қонуни бекоркунанда" in page and "Эътиборашро гум кардааст" in page
    assert APP.test_client().get(f"/api/documents/{target}").get_json()["legal_status"] == "repealed"


def test_return_to_developer():
    a = APP.test_client()
    login(a, "admin@test.tj", "admin-password-1")
    did = doc_id("Нақшаи санҷишии Қонун")
    a.post(f"/manage/documents/{did}/return", data={"csrf_token": csrf(a, "/manage"), "reason": "Банди 8 пур нашудааст"})
    assert APP.test_client().get(f"/doc/{did}").status_code == 404
    assert any("баргардонида" in m["subject"] for m in outbox("dev@gov.tj"))
    d = APP.test_client()
    login(d, "dev@gov.tj", "developer-pass")
    assert "Банди 8 пур нашудааст" in d.get("/manage").get_data(as_text=True)


def test_reminders_and_migration():
    import sqlite3
    u = make_active_user("remind@example.tj")
    plan = doc_id("соддагардонии иҷозатномадиҳӣ")
    u.post(f"/doc/{plan}/follow", data={"csrf_token": csrf(u, f"/doc/{plan}")})
    conn = sqlite3.connect(portal.DB_PATH)
    conn.execute("UPDATE documents SET consult_end=? WHERE id=?", ((date.today() + timedelta(days=2)).isoformat(), plan))
    conn.commit()
    conn.close()
    assert portal.send_reminders() >= 1
    assert portal.send_reminders() == 0  # only once
    assert any("скоро завершится" in m["subject"] for m in outbox("remind@example.tj"))

    # an old (first-release) database gets the new columns
    old = os.path.join(TMP, "old.db")
    conn = sqlite3.connect(old)
    conn.executescript("CREATE TABLE users (id INTEGER PRIMARY KEY, account_type TEXT NOT NULL, full_name TEXT NOT NULL,"
                       " email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, role TEXT DEFAULT 'user',"
                       " status TEXT DEFAULT 'pending', created_at TEXT);"
                       "CREATE TABLE documents (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, title_tj TEXT NOT NULL,"
                       " doc_date TEXT, consult_end TEXT);"
                       "CREATE TABLE comments (id INTEGER PRIMARY KEY, document_id INTEGER, user_id INTEGER, body TEXT, created_at TEXT);")
    conn.close()
    saved = portal.DB_PATH
    portal.DB_PATH = old
    try:
        portal.init_db()
        conn = sqlite3.connect(old)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
        ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        conn.close()
        assert {"legal_status", "returned_note"} <= cols and {"lang", "notify_email"} <= ucols
    finally:
        portal.DB_PATH = saved


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback
                print("FAIL", name)
                traceback.print_exc()
    sys.exit(1 if failed else 0)
