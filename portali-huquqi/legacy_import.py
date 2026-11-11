"""Import of records from the previous portal (portali-huquqi.tj, 2018-2025).

Input: JSON lines, one record per line, produced either by tools/fetch_old_portal.py (public pages)
or by a converter from the old database. Re-running is safe: records are matched by
(kind, legacy_id) and updated in place.

Record format (every field except kind, legacy_id and title.tj is optional):

  {"kind": "act", "legacy_id": 19892,
   "type": "Қонун", "number": "1414", "date": "2017-05-30",            # date also as 30.05.2017
   "title": {"tj": "...", "ru": "..."}, "body": {"tj": "<html>", "ru": "<html>"}, "body_format": "html",
   "adopted_by": {"tj": "...", "ru": "..."}, "status": "in_force|partly|repealed" (or Tajik/Russian words),
   "effective_date": ..., "repeal_date": ..., "revision_date": ..., "registered_date": ...,
   "mj_number": "...", "mj_date": ..., "publication": {"tj", "ru"},
   "classifier": {"tj", "ru"}, "keywords": {"tj", "ru"},
   "amends": [asosi_id, ...], "repeals": [...], "amended_by": [...], "repealed_by": [...],
   "versions": [{"valid_from": ..., "valid_to": ..., "body": {"tj", "ru"}, "note": "..."}],
   "files": [{"name": "Шарҳ.pdf", "path": "files/abc.pdf"}]}

  {"kind": "plan", "legacy_id": 122, "developer": {"tj", "ru"}, "type": "Қарор", "title": {"tj", "ru"},
   "affected": {"tj"}, "contacts": {"tj"}, "details": {"problem": {"tj"}, "goal", "measures", "alternatives", "costs"},
   "dev_deadline": ..., "adopt_deadline": ..., "published": "15.07.2025", "consult_end": ..., "consult_done": ...,
   "accepted": "Ҳа|Не|Дар барраси", "files": [...]}

  {"kind": "draft", "legacy_id": 89, "plan_legacy_id": 118, ... same as plan ...,
   "body": {"tj", "ru"}, "prev_body": {"tj", "ru"}, "consult_start": ..., "consult_end": ...,
   "conclusion": {"tj"}, "final_date": ...}
"""
import json
import os
import re
import shutil
import sqlite3
import uuid
from datetime import date

import legaltext

DATE_DMY = re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})$")
DOC_TYPE_WORDS = [  # (code, words at the start of the type or title, Tajik and Russian)
    ("constitutional_law", ("қонуни конститутсионӣ", "қонуни конститутсионии", "конституционный закон")),
    ("code", ("кодекс",)),
    ("law", ("қонун", "закон")),
    ("decree", ("фармони президент", "фармон", "указ")),
    ("resolution", ("қарори ҳукумат", "постановление правительства")),
    ("order", ("фармоиш", "распоряжение")),
    ("dept_act", ("фармоиши вазир", "амр", "приказ", "қарори раёсати", "постановление правления",
                  "дастурамал", "инструкция", "регламент")),
]
LEGAL_WORDS = {"in_force": ("in_force", "амалкунанда", "амал мекунад", "действующ", "действует"),
               "partly": ("partly", "қисман", "частично"),
               "repealed": ("repealed", "эътибор надорад", "бекор", "гум карда", "утратил", "отменен", "отменён")}
ACCEPTED = {"plan": {"ҳа": "proceed", "да": "proceed", "не": "rejected"},
            "draft": {"ҳа": "adopted", "да": "adopted", "не": "rejected"}}
DETAIL_KEYS = ("affected", "problem", "goal", "measures", "alternatives", "costs", "indicators", "effective_date",
               "contacts")


# Policy area guessed from the subject of the title (Tajik and Russian word beginnings), first match wins.
# Deliberately cautious: a record with no clear match gets no topic rather than a wrong one.
# Only used when a record brings no topic of its own; an editor can change it later.
TOPIC_WORDS = [
    ("finance", ("андоз", "буҷет", "бонк", "молия", "суғурта", "гумрук", "налог", "бюджет", "банк",
                 "финанс", "страхов", "таможен", "кредит")),
    ("health", ("тандуруст", "тиббӣ", "дору", "саломат", "здравоохран", "медицин", "лекарств", "санитар")),
    ("education", ("маориф", "таҳсил", "мактаб", "донишгоҳ", "образован", "науке", "научн", "школ", "университет")),
    ("social", ("меҳнат", "нафақа", "шуғл", "маъюб", "ҳифзи иҷтимо", "труд", "пенси", "социальн", "занятост",
                "инвалид")),
    ("environment", ("муҳити зист", "эколог", "ҷангал", "пирях", "окружающ", "охране природ", "лесн", "ледник")),
    ("agriculture", ("кишоварз", "обёр", "деҳқон", "чорво", "захираҳои об", "сельск", "земел", "водн", "ороше",
                     "дехкан", "животновод")),
    ("transport", ("нақлиёт", "роҳҳои автомобил", "ҳавопаймо", "транспорт", "автомобильн", "дорожн", "авиац",
                   "железнодорож")),
    ("digital", ("рақамӣ", "рақамикунон", "интернет", "алоқаи барқ", "электрон", "цифров", "информатиз", "связи")),
    ("energy", ("энерг", "саноат", "нафт", "маъдан", "промышленн", "нефт", "недр")),
    ("economy", ("соҳибкор", "иҷозат", "савдо", "сармоягузор", "рақобат", "иқтисод", "консесси", "предприним",
                 "лиценз", "торгов", "инвестиц", "конкуренц", "экономи", "концесси")),
    ("justice", ("суд", "ҷиноят", "прокурат", "адлия", "амнияти миллӣ", "милитсия", "мудофиа", "уголовн", "юстиц",
                 "национальной безопасност", "милиц", "оборон", "маъмурӣ", "административн", "санадҳои меъёрии ҳуқуқ",
                 "нормативных правовых")),
    ("state", ("интихобот", "хизмати давлатӣ", "конститутс", "выбор", "государственной служб", "конституц")),
]
_TOPIC_RES = [(topic, re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, words)) + ")"))
              for topic, words in TOPIC_WORDS]
_QUOTED = re.compile(r"[«\"“](.+)[»\"”]?", re.S)


def guess_topic(*titles):
    """The subject is what stands in quotes ("Қарори Ҳукумат ... «Дар бораи ...»"); otherwise the whole title."""
    for title in titles:
        title = (title or "").lower()
        m = _QUOTED.search(title)
        subject = m.group(1) if m else title
        for topic, pattern in _TOPIC_RES:
            if pattern.search(subject):
                return topic
    return None


def norm_date(value):
    v = (value or "").strip()[:10]
    m = DATE_DMY.match(v)
    if m:
        v = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        return None


def guess_doc_type(raw, title):
    for text in ((raw or "").strip().lower(), (title or "").strip().lower()):
        if not text:
            continue
        for code, words in DOC_TYPE_WORDS:
            if any(text.startswith(w) for w in words):
                return code
    return "other" if raw or title else None


def legal_status(raw, repeal_date):
    v = (raw or "").strip().lower()
    for code, words in LEGAL_WORDS.items():
        if any(w in v for w in words):
            return code
    return "repealed" if repeal_date else "in_force"


NO_DATA = {"маълумот нест", "нет данных", "-", "—"}


def _lang(rec, key, lang):
    v = rec.get(key)
    if isinstance(v, dict):
        v = {k: ("" if (x or "").strip().lower() in NO_DATA else x) for k, x in v.items()}
    elif isinstance(v, str) and v.strip().lower() in NO_DATA:
        v = ""
    if isinstance(v, dict):
        return (v.get(lang) or "").strip() or None
    if isinstance(v, str) and lang == "tj":  # a plain string is taken as the Tajik value
        return v.strip() or None
    return None


def _row_values(rec):
    kind = rec["kind"]
    fmt = "html" if rec.get("body_format", "html") == "html" else "text"
    clean = legaltext.sanitize if fmt == "html" else (lambda x: x)
    title_tj = _lang(rec, "title", "tj") or _lang(rec, "title", "ru")
    vals = {
        "kind": kind, "legacy_id": int(rec["legacy_id"]), "number": (rec.get("number") or "").strip() or None,
        "doc_date": norm_date(rec.get("date") or rec.get("published")),
        "doc_type": guess_doc_type(rec.get("type"), title_tj),
        "title_tj": title_tj, "title_ru": _lang(rec, "title", "ru"),
        "body_format": fmt,
        "body_tj": clean(_lang(rec, "body", "tj") or "") or None,
        "body_ru": clean(_lang(rec, "body", "ru") or "") or None,
        "summary_tj": _lang(rec, "summary", "tj"), "summary_ru": _lang(rec, "summary", "ru"),
        "published": 1,
        "topic": rec.get("topic") or guess_topic(title_tj, _lang(rec, "title", "ru")),
        "source_url": rec.get("source_url") if re.match(r"^https?://", rec.get("source_url") or "") else None,
    }
    if kind == "act":
        repeal = norm_date(rec.get("repeal_date"))
        vals.update({
            "developer_tj": _lang(rec, "adopted_by", "tj"), "developer_ru": _lang(rec, "adopted_by", "ru"),
            "legal_status": legal_status(rec.get("status"), repeal), "repeal_date": repeal,
            "effective_date": norm_date(rec.get("effective_date")),
            "revision_date": norm_date(rec.get("revision_date")),
            "registered_date": norm_date(rec.get("registered_date")),
            "mj_reg_number": (rec.get("mj_number") or "").strip() or None, "mj_reg_date": norm_date(rec.get("mj_date")),
            "publication_tj": _lang(rec, "publication", "tj"), "publication_ru": _lang(rec, "publication", "ru"),
            "classifier_tj": _lang(rec, "classifier", "tj"), "classifier_ru": _lang(rec, "classifier", "ru"),
            "keywords_tj": _lang(rec, "keywords", "tj"), "keywords_ru": _lang(rec, "keywords", "ru"),
        })
    else:
        details = {}
        for key in DETAIL_KEYS:
            src = (rec.get("details") or {}).get(key) if key not in rec else rec.get(key)
            entry = src if isinstance(src, dict) else ({"tj": src} if src else {})
            entry = {lg: (entry.get(lg) or "").strip() for lg in ("tj", "ru") if (entry.get(lg) or "").strip()}
            if entry:
                details[key] = entry
        accepted = (rec.get("accepted") or "").strip().lower()
        outcome = ACCEPTED[kind].get(accepted)
        start = norm_date(rec.get("consult_start") or rec.get("published"))
        vals.update({
            "developer_tj": _lang(rec, "developer", "tj"), "developer_ru": _lang(rec, "developer", "ru"),
            "details": json.dumps(details, ensure_ascii=False),
            "affects_business": 1 if any(k in details for k in ("problem", "goal", "measures")) else 0,
            "dev_deadline": norm_date(rec.get("dev_deadline")), "adopt_deadline": norm_date(rec.get("adopt_deadline")),
            "consult_start": start, "consult_end": norm_date(rec.get("consult_end")),
            "outcome": outcome, "outcome_date": norm_date(rec.get("final_date") or rec.get("consult_done"))
            if outcome else None,
            "conclusion_tj": _lang(rec, "conclusion", "tj"), "conclusion_ru": _lang(rec, "conclusion", "ru"),
            "prev_body_tj": clean(_lang(rec, "prev_body", "tj") or "") or None,
            "prev_body_ru": clean(_lang(rec, "prev_body", "ru") or "") or None,
        })
        if vals["conclusion_tj"] or vals["conclusion_ru"]:
            vals["conclusion_date"] = vals["outcome_date"] or vals["consult_end"]
    return vals


def _copy_files(conn, doc_id, files, base_dir, upload_dir):
    added = 0
    for f in files or []:
        src = os.path.realpath(os.path.join(base_dir, f.get("path") or ""))
        if not src.startswith(os.path.realpath(base_dir) + os.sep):
            continue  # only files inside the import folder; a record must not pull in other files of the server
        name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", (f.get("name") or os.path.basename(src)))[:150]
        if not f.get("path") or not os.path.isfile(src):
            continue
        size = os.path.getsize(src)
        if conn.execute("SELECT 1 FROM attachments WHERE document_id=? AND original_name=? AND size=?",
                        (doc_id, name, size)).fetchone():
            continue
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else "bin"
        stored = f"{uuid.uuid4().hex}.{ext}"
        shutil.copyfile(src, os.path.join(upload_dir, stored))
        conn.execute("INSERT INTO attachments (document_id, stored_name, original_name, size) VALUES (?,?,?,?)",
                     (doc_id, stored, name, size))
        added += 1
    return added


def import_jsonl(db_path, path, upload_dir, files_base=None, log=print):
    """Import or update every record of the file. Returns counts."""
    files_base = files_base or os.path.dirname(os.path.abspath(path))
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    counts = {"inserted": 0, "updated": 0, "errors": 0, "files": 0, "links": 0, "versions": 0}
    pending = []  # (doc_id, record) for the second pass: links between records
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("kind") not in ("act", "plan", "draft") or not str(rec.get("legacy_id", "")).isdigit():
                    raise ValueError("kind and numeric legacy_id are required")
                vals = _row_values(rec)
                if not vals["title_tj"]:
                    raise ValueError("title is required")
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                counts["errors"] += 1
                log(f"line {n}: {e}")
                continue
            row = conn.execute("SELECT id FROM documents WHERE kind=? AND legacy_id=?",
                               (vals["kind"], vals["legacy_id"])).fetchone()
            if row:
                doc_id = row["id"]
                sets = ", ".join(f"{k}=?" for k in vals)
                conn.execute(f"UPDATE documents SET {sets}, updated_at=datetime('now') WHERE id=?",
                             list(vals.values()) + [doc_id])
                counts["updated"] += 1
            else:
                cur = conn.execute(f"INSERT INTO documents ({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})",
                                   list(vals.values()))
                doc_id = cur.lastrowid
                counts["inserted"] += 1
            if "versions" in rec:
                conn.execute("DELETE FROM document_versions WHERE document_id=?", (doc_id,))
                for v in rec["versions"] or []:
                    clean = legaltext.sanitize if vals["body_format"] == "html" else (lambda x: x)
                    conn.execute(
                        "INSERT INTO document_versions (document_id, body_tj, body_ru, body_format, valid_from, "
                        "valid_to, note) VALUES (?,?,?,?,?,?,?)",
                        (doc_id, clean(_lang(v, "body", "tj") or "") or None, clean(_lang(v, "body", "ru") or "") or None,
                         vals["body_format"], norm_date(v.get("valid_from")), norm_date(v.get("valid_to")),
                         (v.get("note") or "").strip() or None))
                    counts["versions"] += 1
            counts["files"] += _copy_files(conn, doc_id, rec.get("files"), files_base, upload_dir)
            pending.append((doc_id, rec))
            if len(pending) % 500 == 0:
                conn.commit()
                log(f"... {len(pending)} records")
    conn.commit()

    def lookup(kind, legacy):
        r = conn.execute("SELECT id FROM documents WHERE kind=? AND legacy_id=?", (kind, int(legacy))).fetchone()
        return r["id"] if r else None

    for doc_id, rec in pending:
        if rec["kind"] == "draft" and str(rec.get("plan_legacy_id") or "").isdigit():
            plan = lookup("plan", rec["plan_legacy_id"])
            if plan:
                conn.execute("UPDATE documents SET related_id=? WHERE id=?", (plan, doc_id))
        if rec["kind"] != "act":
            continue
        # this act -> others it amends or repeals; others -> this act when they amend or repeal it
        pairs = [(doc_id, lookup("act", x), t) for key, t in (("amends", "amends"), ("repeals", "repeals"))
                 for x in rec.get(key) or [] if str(x).isdigit()]
        pairs += [(lookup("act", x), doc_id, t) for key, t in (("amended_by", "amends"), ("repealed_by", "repeals"))
                  for x in rec.get(key) or [] if str(x).isdigit()]
        for src, dst, link_type in pairs:
            if src and dst and src != dst:
                cur = conn.execute("INSERT OR IGNORE INTO doc_links (from_id, to_id, link_type) VALUES (?,?,?)",
                                   (src, dst, link_type))
                counts["links"] += cur.rowcount
    conn.commit()
    conn.close()
    counts["ids"] = [doc_id for doc_id, _ in pending]
    return counts
