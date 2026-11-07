"""Copy records from the public pages of the previous portal (http://www.portali-huquqi.tj)
into the JSON-lines format read by `flask --app app import-legacy` (see legacy_import.py).

Use this only with the operator's permission and only if a database export is not available.
It waits between requests (default 1.5 s) and can be stopped and resumed: records already
in the output file are skipped.

Examples:
    python tools/fetch_old_portal.py --plans all --drafts all --out old/records.jsonl --files old/files
    python tools/fetch_old_portal.py --act-ids 19892,21650 --out old/records.jsonl
    python tools/fetch_old_portal.py --acts 200 --out old/records.jsonl      # first 200 acts of the list
    python tools/fetch_old_portal.py --acts all --out old/records.jsonl      # everything (about 15 hours)
The old site works over plain http; set http_proxy if the machine needs a proxy.
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://www.portali-huquqi.tj"
OUT_DIR = "."
UA = "portali-huquqi-migration/1.0"
RU_MONTHS = {m: i for i, m in enumerate(("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
                                         "сентября", "октября", "ноября", "декабря"), 1)}
PLAN_MAP = {"user_id": "developer", "guruh": "affected", "muhlati_tahiya": "dev_deadline",
            "muhlati_qabul": "adopt_deadline", "namudihujjat": "type", "muhlati_peshnihod": "consult_end",
            "tavsif1": "problem", "tavsif2": "goal", "tavsif3": "measures", "tavsif4": "alternatives",
            "tavsif5": "costs", "sanai_anjom": "consult_done", "qabulshud": "accepted", "contacts": "contacts",
            "sanainashr": "published"}
DRAFT_MAP = {"rn_id": "plan_legacy_id", "namudihujjat": "type", "sanai_oghoz": "consult_start",
             "sanai_anjom": "consult_end", "qabulshud": "accepted", "user_id": "developer",
             "sanaintiho": "final_date", "sanainashr": "published"}
FILE_FIELDS = {"zamima_mmk": "Хулосаи ММҚ", "huj_ilova": "Ҳуҷҷатҳои иловагӣ", "ria": "ТТТ", "khulosa": "Хулоса",
               "intiho": "Нусхаи ниҳоӣ"}


class Fetcher:
    def __init__(self, delay):
        self.delay, self.last = delay, 0.0
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        self.opener.addheaders = [("User-Agent", UA)]

    def get(self, path, binary=False, tries=4):
        for attempt in range(tries):
            wait = self.delay - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            try:
                with self.opener.open(BASE + path, timeout=120) as r:
                    data = r.read()
                return data if binary else data.decode("utf-8", "replace")
            except OSError as e:
                if attempt == tries - 1:
                    raise
                print(f"  retry {path}: {e}", file=sys.stderr)
                time.sleep(5 * (attempt + 1))


def text(fragment):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


def view_fields(page):
    """PHPMaker view page -> {field: (inner html of the value cell)}."""
    out = {}
    for m in re.finditer(r'<tr id="r_([^"]+)"(.*?)</tr>', page, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", m.group(2), re.S)
        if len(cells) > 1:
            out[m.group(1)] = cells[1]
    return out


def files_in(cell, folder):
    return [(html.unescape(href), text(name)) for href, name in
            re.findall(r'href="(ewfile\d+\.php\?[^"]+)"[^>]*>(.*?)</a>', cell or "", re.S)
            if text(name)] if cell else []


def download(fetcher, folder, href, name, files_dir, rel_prefix):
    safe = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name)[:150]
    target = os.path.join(files_dir, rel_prefix, safe)
    if not os.path.exists(target):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        data = fetcher.get(f"/{folder}/{href}", binary=True)
        with open(target, "wb") as f:
            f.write(data)
    # paths are stored relative to the folder of the output file, where import-legacy looks for them
    return {"name": name, "path": os.path.relpath(target, OUT_DIR)}


def fetch_register(fetcher, kind, limit, done, files_dir):
    lst, view, fmap = (("view_rnaqshalist.php", "view_rnaqshaview.php", PLAN_MAP) if kind == "plan" else
                       ("view_rloihalist.php", "view_rloihaview.php", DRAFT_MAP))
    page = fetcher.get(f"/publicria/{lst}?recperpage=1000")
    ids = sorted({int(x) for x in re.findall(view + r"\?showdetail=&amp;reestr_id=(\d+)", page)}, reverse=True)
    ids = ids if limit == "all" else ids[:int(limit)]
    for rid in ids:
        if (kind, rid) in done:
            continue
        f = view_fields(fetcher.get(f"/publicria/{view}?showdetail=&reestr_id={rid}&language=tj"))
        rec = {"kind": kind, "legacy_id": rid, "body_format": "html",
               "title": {"tj": text(f.get("nom_tj")), "ru": text(f.get("nom_ru"))}}
        for field, key in fmap.items():
            if text(f.get(field)):
                rec[key] = text(f[field])
        if kind == "draft":
            rec["body"] = {"tj": f.get("loiha_tj", ""), "ru": f.get("loiha_ru", "")}
            rec["prev_body"] = {"tj": f.get("pesh_loiha_tj", ""), "ru": f.get("pesh_loiha_ru", "")}
        if files_dir:
            rec["files"] = [download(fetcher, "publicria", href, name, files_dir, f"{kind}-{rid}")
                            for field in FILE_FIELDS for href, name in files_in(f.get(field), "publicria")]
        yield rec


def act_ids_from_list(fetcher, limit):
    ids, start = [], 1
    while True:
        page = fetcher.get(f"/publicadliya/view_detailslist.php?start={start}&language=ru")
        found = [int(x) for x in re.findall(r"view_qonunhoview\.php\?showdetail=&asosi_id=(\d+)", page)]
        new = [i for i in dict.fromkeys(found) if i not in ids]
        if not new:
            return ids
        ids += new
        if limit != "all" and len(ids) >= int(limit):
            return ids[:int(limit)]
        start += 25


CARD_LABELS = (  # label on the reference card ("Маълумотнома" tab) -> record key
    ("номер документа", "number"), ("дата принятия", "date"), ("состояние", "status"),
    ("регистрация в минюсте", "mj"), ("начало действия", "effective_date"), ("окончание действия", "repeal_date"),
    ("утрат", "repeal_date"), ("редакция", "revision_date"), ("органы эмитенты", "adopted_by"),
    ("дата включения", "registered_date"))


def parse_card(page):
    """Reference card of an act: number, dates, status, Ministry of Justice registration, issuer, publication."""
    m = re.search(r'<script id="tpx_view_qonunho_aboutdoc"[^>]*>(.*?)</script>', page, re.S)
    card = m.group(1) if m else ""
    h1 = re.search(r"<h1>(.*?)</h1>", card, re.S)
    out = {"title": text(h1.group(1)) if h1 else ""}
    for label, value in re.findall(r"<tr>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>", card, re.S):
        label, value = text(label).lower().rstrip(":"), text(value)
        key = next((k for lab, k in CARD_LABELS if lab in label), None)
        if key and value and key not in out:
            out[key] = value
    if "mj" in out:
        mj = re.search(r"№\s*(\S+)(?:\s+от\s+(\S+))?", out.pop("mj"))
        if mj:
            out["mj_number"], out["mj_date"] = mj.group(1), mj.group(2)
    pub = re.search(r"<h2>\s*Опубликование документа\s*</h2>(.*?)(?:<h2|$)", card, re.S)
    if pub and text(pub.group(1)):
        out["publication"] = text(pub.group(1))
    issuer = out.get("adopted_by")
    if issuer and issuer.isupper():
        out["adopted_by"] = issuer.title().replace(" Ва ", " ва ").replace(" И ", " и ")
    return out


def parse_act(page):
    m = re.search(r'<script id="tpx_view_qonunho_asosi_id"[^>]*>(.*?)</script>', page, re.S)
    body = m.group(1) if m else ""
    amended = [int(x) for block in re.findall(r"(?i)<div class=\"?doc-info-change\"?>(.*?)</div>", body, re.S)
               for x in re.findall(r"asosi_id=(\d+)", block)]
    return body, parse_card(page), list(dict.fromkeys(amended))


def date_from_title(title):
    m = re.search(r"(\d{1,2})\s+(" + "|".join(RU_MONTHS) + r")\s+(\d{4})", title or "")
    return f"{m.group(3)}-{RU_MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}" if m else None


def fetch_acts(fetcher, ids, done):
    for aid in ids:
        if ("act", aid) in done:
            continue
        tj = fetcher.get(f"/publicadliya/view_qonunhoview.php?showdetail=&asosi_id={aid}&language=tj")
        ru = fetcher.get(f"/publicadliya/view_qonunhoview.php?showdetail=&asosi_id={aid}&language=ru")
        body_tj, card_tj, amended = parse_act(tj)
        body_ru, card_ru, amended_ru = parse_act(ru)
        same = text(body_tj)[:400] == text(body_ru)[:400]  # Russian-only acts show the same text for both
        rec = {"kind": "act", "legacy_id": aid, "body_format": "html",
               "title": {"tj": card_tj.get("title", ""), "ru": card_ru.get("title", "")},
               "body": {"tj": "" if same and card_tj.get("title") == card_ru.get("title") else body_tj,
                        "ru": "" if same and card_tj.get("title") != card_ru.get("title") else body_ru},
               "amended_by": list(dict.fromkeys(amended + amended_ru)),
               "source_url": f"{BASE}/publicadliya/view_qonunhoview.php?showdetail=&asosi_id={aid}"}
        for key in ("number", "date", "status", "mj_number", "mj_date", "effective_date", "repeal_date",
                    "revision_date", "registered_date"):
            if card_tj.get(key) or card_ru.get(key):
                rec[key] = card_tj.get(key) or card_ru.get(key)
        for key in ("adopted_by", "publication"):
            if card_tj.get(key) or card_ru.get(key):
                rec[key] = {"tj": card_tj.get(key, ""), "ru": card_ru.get(key, "")}
        rec["date"] = rec.get("date") or date_from_title(card_ru.get("title"))
        yield rec


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--plans", help="'all' or a number")
    ap.add_argument("--drafts", help="'all' or a number")
    ap.add_argument("--acts", help="'all' or a number (newest first)")
    ap.add_argument("--act-ids", help="comma-separated asosi_id values")
    ap.add_argument("--files", help="folder for attached files of plans and drafts")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between requests")
    a = ap.parse_args()
    global OUT_DIR
    OUT_DIR = os.path.dirname(os.path.abspath(a.out))
    done = set()
    if os.path.exists(a.out):
        with open(a.out, encoding="utf-8") as f:
            done = {(r["kind"], r["legacy_id"]) for r in map(json.loads, filter(str.strip, f))}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    fetcher = Fetcher(a.delay)
    n = 0
    with open(a.out, "a", encoding="utf-8") as out:
        def write(rec):
            nonlocal n
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            n += 1
            print(f"{rec['kind']} {rec['legacy_id']}: {(rec['title'].get('tj') or rec['title'].get('ru'))[:70]}")
        for kind, limit in (("plan", a.plans), ("draft", a.drafts)):
            if limit:
                for rec in fetch_register(fetcher, kind, limit, done, a.files):
                    write(rec)
        ids = [int(x) for x in re.findall(r"\d+", a.act_ids or "")]
        if a.acts:
            ids += act_ids_from_list(fetcher, a.acts)
        for rec in fetch_acts(fetcher, list(dict.fromkeys(ids)), done):
            write(rec)
    print(f"Done: {n} new records in {a.out}")


if __name__ == "__main__":
    main()
