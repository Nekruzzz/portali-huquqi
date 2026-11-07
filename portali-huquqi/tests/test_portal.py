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
    txt = r.get_data(as_text=True)  # the client is in Russian here; the real Russian text has "Статья 39"
    assert r.status_code == 200 and ("Моддаи 39" in txt or "Статья 39" in txt)
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


def test_peer_portal_features():
    c = APP.test_client()
    # topics browsing
    assert c.get("/topics").status_code == 200
    assert c.get("/topic/finance").status_code == 200 and c.get("/topic/nope").status_code == 404
    assert "Кодекси андози" in c.get("/topic/finance").get_data(as_text=True)
    # suggestions and registry-number lookup
    sug = c.get("/api/suggest?q=андози").get_json()
    assert sug["items"] and sug["items"][0]["code"].startswith("02/")
    code = sug["items"][0]["code"]
    hit = c.get("/api/documents?q=" + code).get_json()
    assert hit["total"] == 1 and hit["items"][0]["code"] == code
    # home tabs and bento render
    home = c.get("/").get_data(as_text=True)
    assert 'id="panel-closing"' in home and "tile-feature" in home and "topic-grid" in home
    # document page: stage tracker, code, views counter
    did = doc_id("Кодекси андози")
    before = c.get(f"/api/documents/{did}").get_json()["views"]
    page = c.get(f"/doc/{did}").get_data(as_text=True)
    assert 'class="stages' in page and code in page and "propose-here" in page
    assert c.get(f"/api/documents/{did}").get_json()["views"] == before + 1
    # article preselected from the per-article button
    u = make_active_user("receipt@example.tj")
    page = u.get(f"/doc/{did}", query_string={"article": "Моддаи 2. Мӯҳлатҳо"}).get_data(as_text=True)
    assert "<option selected>Моддаи 2. Мӯҳлатҳо</option>" in page
    # proposal receipt number
    r = u.post(f"/doc/{did}/comment", data={"csrf_token": csrf(u, f"/doc/{did}"), "body": "Пешниҳоди санҷишӣ барои рақам"},
               follow_redirects=True)
    assert code + "/" in r.get_data(as_text=True)
    # act status banner
    act = doc_id("Тартиби пешбурди")
    assert "status-banner" in c.get(f"/doc/{act}").get_data(as_text=True)
    # sorting by most discussed works
    assert c.get("/api/documents?sort=discussed").status_code == 200


def test_qa_regressions():
    c = APP.test_client()
    # same-site redirects only (backslash tricks, schemes, protocol-relative)
    for nxt in ("//evil.com", "/\\evil.com", "https://evil.com", "javascript:alert(1)", "/%5Cevil.com"):
        loc = c.get("/lang/ru", query_string={"next": nxt}).headers["Location"]
        assert "evil" not in loc and "javascript" not in loc, (nxt, loc)
    assert c.get("/lang/tj", query_string={"next": "/acts?year=2018"}).headers["Location"].endswith("/acts?year=2018")
    # e-mail subjects cannot carry injected headers
    with APP.test_request_context():
        import flask
        portal.queue_mail("x@y.tj", "ru", "mail_answer_s", "mail_answer_b", footer=False,
                          subject="Hi\r\nBcc: victim@evil.tj", answer="ok", site="P")
        portal.get_db().commit()
        flask.g.pop("mail_queued", None)
    import sqlite3
    conn = sqlite3.connect(portal.DB_PATH)
    subj = conn.execute("SELECT subject FROM outbox WHERE to_email='x@y.tj'").fetchone()[0]
    conn.close()
    assert "\n" not in subj and "\r" not in subj
    # one h1 per page and a heading before result lists
    for path in ("/", "/drafts", "/acts", "/search?q=x", "/topics", "/topic/health", "/stats"):
        html = c.get(path).get_data(as_text=True)
        assert html.count("<h1") == 1, path
    assert '<h2 class="result-count"' in c.get("/drafts").get_data(as_text=True)
    # flash messages render as dismissible toasts
    u = make_active_user("toast@example.tj")
    did = doc_id("Кодекси андози")
    r = u.post(f"/doc/{did}/comment", data={"csrf_token": csrf(u, f"/doc/{did}"), "body": "Санҷиши паём"}, follow_redirects=True)
    assert 'class="flash flash-success toast"' in r.get_data(as_text=True)
    # helpful empty states in the account
    acc = make_active_user("empty@example.tj").get("/account").get_data(as_text=True)
    assert "empty-rich" in acc
    # security headers
    h = c.get("/").headers
    assert h["X-Frame-Options"] == "DENY" and "Permissions-Policy" in h
    assert "Strict-Transport-Security" in c.get("/", headers={"X-Forwarded-Proto": "https"}).headers


def test_legacy_redirects():
    c = APP.test_client()
    law = doc_id("Дар бораи санадҳои меъёрии ҳуқуқӣ»")
    r = c.get("/publicadliya/view_qonunhoview.php?showdetail=&asosi_id=19892&language=ru")
    assert r.status_code == 301 and r.headers["Location"].endswith(f"/doc/{law}?lang=ru")
    assert c.get("/publicadliya/view_qonunhoview.php?asosi_id=1").status_code == 404
    assert c.get("/publicadliya/view_qonunhoview.php?asosi_id=abc").status_code == 404
    assert c.get("/publicadliya/dashboard.php?language=tj").headers["Location"].endswith("/acts?lang=tj")
    assert c.get("/publicadliya/view_detailssrch.php").headers["Location"].endswith("/acts")
    assert c.get("/publicria/view_rnaqshalist.php").headers["Location"].endswith("/plans")
    assert c.get("/publicria/view_rloihalist.php").headers["Location"].endswith("/drafts")
    assert c.get("/publicria/ibtido.php").headers["Location"].endswith("/")
    assert c.get("/publicria/").status_code == 301
    # plans and drafts by their old register numbers
    import sqlite3
    plan, draft = doc_id("соддагардонии иҷозатномадиҳӣ"), doc_id("Лоиҳаи Қонун «Дар бораи дастрасӣ")
    conn = sqlite3.connect(portal.DB_PATH)
    conn.execute("UPDATE documents SET legacy_id=118 WHERE id=?", (plan,))
    conn.execute("UPDATE documents SET legacy_id=89 WHERE id=?", (draft,))
    conn.commit()
    conn.close()
    assert c.get("/publicria/view_rnaqshaview.php?showdetail=&reestr_id=118").headers["Location"].endswith(f"/doc/{plan}")
    assert c.get("/publicria/view_rloihaview.php?reestr_id=89").headers["Location"].endswith(f"/doc/{draft}")
    assert c.get("/publicria/v_peshnihodloihalist.php?showmaster=view_rloiha&fk_reestr_id=89"
                 ).headers["Location"].endswith(f"/doc/{draft}#comments")
    assert c.get("/publicria/view_rloihaview.php?reestr_id=118").status_code == 404  # 118 is a plan, not a draft


OLD_ACT_HTML = """<H2><A class=anchor-ogl name=A000000001></A>ҚОНУНИ ҶУМҲУРИИ ТОҶИКИСТОН</H2>
<P class=dname>ДАР БОРАИ САНҶИШ</P>
<DIV class=doc-info-change><P>(Қонуни ҶТ аз 23.05.2021 № <A class="outer" href="view_qonunhoview.php?showdetail=&asosi_id=19892#A000000004">1826</A>)</P></DIV>
<H4><A class=anchor-ogl name=A000000003></A>БОБИ 1. МУҚАРРАРОТИ УМУМӢ</H4>
<H6><A class=anchor-ogl name=A000000004></A>Моддаи 1. Мафҳумҳои асосӣ</H6>
<P>Дар Қонуни мазкур мафҳумҳои заминҷунбӣ истифода мешаванд.</P>
<TABLE><TR><TD colspan=2>Ҷадвал</TD></TR></TABLE>
<script>alert(1)</script><P onclick="x()" style="color:red">Матн</P><A href="javascript:alert(2)">bad</A>"""


def admin_client():
    a = APP.test_client()
    login(a, "admin@test.tj", "admin-password-1")
    return a


def test_html_act_text_versions_and_export():
    a = admin_client()
    law = doc_id("Дар бораи санадҳои меъёрии ҳуқуқӣ»")
    base = {"csrf_token": csrf(a, "/manage/documents/new"), "kind": "act", "doc_type": "law", "published": "1",
            "doc_date": "2017-05-30", "number": "1416", "title_tj": "Қонуни ҶТ «Дар бораи санҷиш»",
            "body_format": "html", "body_tj": OLD_ACT_HTML, "legacy_id": "19898", "revision_date": "2021-05-23",
            "repeal_date": "", "classifier_tj": "Ҳуқуқи маъмурӣ", "keywords_tj": "заминҷунбӣ, бехатарӣ"}
    r = a.post("/manage/documents/new", data=base)
    new_id = int(re.search(r"/documents/(\d+)/edit", r.headers["Location"]).group(1))
    c = APP.test_client()
    page = c.get(f"/doc/{new_id}").get_data(as_text=True)
    # sanitised, structured, old anchors kept, links to other old acts resolved to their new pages
    assert "<script" not in page.split("legal-html")[1].split("</section>")[0]
    assert "onclick" not in page and "javascript:alert" not in page and "color:red" not in page
    assert 'id="A000000004"' in page and 'href="#A000000004"' in page  # anchor + table of contents
    assert f'href="/doc/{law}#A000000004"' in page
    assert 'class="lt-dname"' in page and 'colspan="2"' in page
    assert "Ҳуқуқи маъмурӣ" in page and "23.05.2021" in page and "заминҷунбӣ</a>" in page
    # search engines: canonical address, both languages, structured data for the act
    assert f'rel="canonical" href="http://localhost/doc/{new_id}?lang=tj"' in page and 'hreflang="ru"' in page
    assert '"@type": "Legislation"' in page and '"legislationIdentifier": "1416"' in page
    assert "Таҳрирҳо" not in page  # no earlier wordings yet
    # old address of this act now finds it
    assert c.get("/publicadliya/view_qonunhoview.php?asosi_id=19898").headers["Location"].endswith(f"/doc/{new_id}")
    # the same old number cannot go to a second act
    r = a.post("/manage/documents/new", data={**base, "title_tj": "Дубора"})
    assert r.status_code == 400
    # change the text: the previous wording is kept and can be shown, dated and compared
    edit = {**base, "csrf_token": csrf(a, f"/manage/documents/{new_id}/edit"), "keep_version": "1",
            "revision_date": "2023-01-10",
            "body_tj": OLD_ACT_HTML.replace("мафҳумҳои заминҷунбӣ", "мафҳумҳои нави заминҷунбӣ")}
    a.post(f"/manage/documents/{new_id}/edit", data=edit)
    page = c.get(f"/doc/{new_id}").get_data(as_text=True)
    assert "Таҳрирҳо" in page and "23.05.2021 — 10.01.2023" in page
    vid = int(re.search(r"\?v=(\d+)#text", page).group(1))
    old = c.get(f"/doc/{new_id}?v={vid}").get_data(as_text=True)
    assert "Шумо таҳрири пешинаро мебинед" in old and "мафҳумҳои нави" not in old
    on_date = c.get(f"/doc/{new_id}?on=2022-02-01").get_data(as_text=True)
    assert "Шумо таҳрири пешинаро мебинед" in on_date
    assert "мафҳумҳои нави" in c.get(f"/doc/{new_id}?on=2024-01-01").get_data(as_text=True)
    diff = c.get(f"/doc/{new_id}/compare?a={vid}").get_data(as_text=True)
    assert "<ins>нави</ins>" in diff
    assert c.get(f"/doc/{new_id}/compare?a=99999").status_code == 404
    # downloads
    txt = c.get(f"/doc/{new_id}.txt").get_data(as_text=True)
    assert "Моддаи 1. Мафҳумҳои асосӣ" in txt and "<" not in txt.split("\n", 3)[3].split("http")[0]
    r = c.get(f"/doc/{new_id}.docx")
    assert r.status_code == 200 and r.data[:2] == b"PK"
    import io
    import docx
    words = "\n".join(p.text for p in docx.Document(io.BytesIO(r.data)).paragraphs)
    assert "Моддаи 1. Мафҳумҳои асосӣ" in words and "мафҳумҳои нави" in words
    assert "мафҳумҳои нави" not in "\n".join(
        p.text for p in docx.Document(io.BytesIO(c.get(f"/doc/{new_id}.docx?v={vid}").data)).paragraphs)
    api = c.get(f"/api/documents/{new_id}").get_json()
    assert api["legacy_id"] == 19898 and api["reference"]["keywords"]["tj"] == ["заминҷунбӣ", "бехатарӣ"]


def test_full_text_search():
    c = APP.test_client()
    # word inside the text of an act, found only with the full-text option, with the passage marked
    html = c.get("/acts?q=маблағгузорӣ&fulltext=1").get_data(as_text=True)
    assert "1414" in html and "<mark>" in html
    # prefix match: "санад" finds "санадҳои"; "ӣ" may be typed as "и"
    assert "1414" in c.get("/acts?q=санад меъёри").get_data(as_text=True)
    r = c.get("/api/documents?q=портали+интернетии").get_json()
    assert r["total"] >= 1
    # typed on a Russian keyboard: ч for ҷ, х for ҳ, и for ӣ
    assert "1414" in c.get("/acts?q=Чумхурии+Точикистон+меъёрии+хукуки").get_data(as_text=True)
    # the passage keeps the original spelling
    html = c.get("/acts?q=маблағгузори&fulltext=1").get_data(as_text=True)
    assert "<mark>маблағгузорӣ</mark>" in html or "<mark>маблағгузори" in html
    # punctuation alone does not break the search
    assert c.get("/search?q=%22%28%2A").status_code == 200
    assert c.get("/search?q=-").status_code == 200
    # suggestions use the same index
    assert c.get("/api/suggest?q=Тартиби").get_json()["total"] >= 1


def test_draft_previous_text_and_phone_filters():
    a = admin_client()
    draft = doc_id("Лоиҳаи Қонун «Дар бораи дастрасӣ")
    page = a.get(f"/manage/documents/{draft}/edit").get_data(as_text=True)
    assert 'name="prev_body_tj"' in page and 'name="body_format"' in page
    import sqlite3
    conn = sqlite3.connect(portal.DB_PATH)
    conn.execute("UPDATE documents SET prev_body_tj=? WHERE id=?", ("Моддаи 1. Матни пештара\n\nСархат.", draft))
    conn.commit()
    conn.close()
    html = APP.test_client().get(f"/doc/{draft}").get_data(as_text=True)
    assert 'id="prev-text"' in html and "Матни пештара" in html
    assert 'data-filters-label="Филтрҳо"' in APP.test_client().get("/acts").get_data(as_text=True)
    # acts that never went through consultation here show no four-step tracker; their code uses the adoption year
    law = APP.test_client().get(f"/doc/{doc_id('Дар бораи санадҳои меъёрии ҳуқуқӣ»')}").get_data(as_text=True)
    assert 'class="stages' not in law and "03/17-" in law


def test_legacy_import_from_jsonl():
    import json
    import sqlite3
    import legacy_import
    folder = tempfile.mkdtemp()
    os.makedirs(os.path.join(folder, "files"))
    with open(os.path.join(folder, "files", "ria.pdf"), "wb") as f:
        f.write(b"%PDF-1.4 test")
    recs = [
        {"kind": "act", "legacy_id": 700001, "type": "Қонун", "number": "77", "date": "01/02/2020",
         "title": {"tj": "Қонуни воридотии санҷишӣ", "ru": "Импортный закон"}, "status": "Действует",
         "mj_number": "5", "mj_date": "03.02.2020", "adopted_by": {"tj": "Маҷлиси Олӣ"},
         "publication": {"tj": "Маълумот нест", "ru": "Нет данных"},
         "body": {"tj": "<H6><A name=A1></A>Моддаи 1. Ҳадаф</H6><P>Матни аввал фосфоритӣ.</P>"},
         "versions": [{"valid_from": "2020-02-01", "valid_to": "2021-01-01", "body": {"tj": "<p>Таҳрири кӯҳна</p>"}}]},
        {"kind": "act", "legacy_id": 700002, "number": "88", "date": "2021-01-01",
         "title": {"tj": "Қонуни ҶТ оид ба ворид намудани тағйирот"}, "amends": [700001],
         "body": {"tj": '<p>Ба <a href="view_qonunhoview.php?showdetail=&asosi_id=700001#A1">моддаи 1</a> тағйир.</p>'}},
        {"kind": "plan", "legacy_id": 900, "title": {"tj": "Нақшаи воридотӣ"}, "developer": "Вазорати санҷиш",
         "affected": "Ҳама", "problem": "Мушкилот", "published": "15.07.2025", "consult_end": "15.08.2025",
         "accepted": "Ҳа", "contacts": "ш. Душанбе, 123"},
        {"kind": "draft", "legacy_id": 901, "plan_legacy_id": 900, "title": {"tj": "Лоиҳаи воридотӣ"},
         "consult_start": "16.07.2025", "consult_end": "15.08.2025", "accepted": "Дар барраси",
         "prev_body": {"tj": "<p>Матни пештара</p>"}, "body": {"tj": "<p>Матни нав</p>"},
         "files": [{"name": "ТТТ.pdf", "path": "files/ria.pdf"}]},
        {"kind": "act", "title": {"tj": "без номера"}},
    ]
    path = os.path.join(folder, "records.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\nnot json\n")
    counts = legacy_import.import_jsonl(portal.DB_PATH, path, portal.UPLOAD_DIR, log=lambda *_: None)
    assert counts["inserted"] == 4 and counts["errors"] == 2 and counts["files"] == 1
    assert counts["links"] == 1 and counts["versions"] == 1
    again = legacy_import.import_jsonl(portal.DB_PATH, path, portal.UPLOAD_DIR, log=lambda *_: None)
    assert again["inserted"] == 0 and again["updated"] == 4 and again["files"] == 0
    conn = sqlite3.connect(portal.DB_PATH)
    conn.row_factory = sqlite3.Row
    portal.fts_sync(conn, again["ids"])
    portal.refs_sync(conn, again["ids"])
    conn.commit()
    act = conn.execute("SELECT * FROM documents WHERE kind='act' AND legacy_id=700001").fetchone()
    assert act["doc_date"] == "2020-02-01" and act["mj_reg_date"] == "2020-02-03" and act["doc_type"] == "law"
    assert act["publication_tj"] is None and act["legal_status"] == "in_force" and act["body_format"] == "html"
    draft = conn.execute("SELECT * FROM documents WHERE kind='draft' AND legacy_id=901").fetchone()
    plan = conn.execute("SELECT * FROM documents WHERE kind='plan' AND legacy_id=900").fetchone()
    assert draft["related_id"] == plan["id"] and plan["outcome"] == "proceed" and draft["outcome"] is None
    conn.close()
    c = APP.test_client()
    amending = c.get(c.get("/publicadliya/view_qonunhoview.php?asosi_id=700002").headers["Location"]).get_data(as_text=True)
    assert f'href="/doc/{act["id"]}#A1"' in amending
    page = c.get(f"/doc/{act['id']}").get_data(as_text=True)
    assert "Қонуни ҶТ оид ба ворид намудани тағйирот" in page  # amended-by list
    assert "Таҳрирҳо" in page
    # links inside texts, both ways
    assert 'id="references"' in page and "Ҳуҷҷатҳое, ки ба ин санад ишора мекунанд" in page
    assert "Ҳуҷҷатҳое, ки дар матн зикр шудаанд" in amending
    assert "фосфоритӣ" in c.get("/acts?q=фосфорити&fulltext=1").get_data(as_text=True)
    plan_page = c.get(f"/doc/{plan['id']}").get_data(as_text=True)
    assert "Тамоси таҳиякунанда" in plan_page and "ш. Душанбе, 123" in plan_page
    assert "ТТТ.pdf" in c.get(f"/doc/{draft['id']}").get_data(as_text=True)


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
