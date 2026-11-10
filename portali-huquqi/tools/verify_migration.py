"""Compare imported acts with the previous portal (http://www.portali-huquqi.tj), record by record.

For a random sample of acts that carry an old-portal number, the old page is fetched again and its
title, number and full text (Tajik and Russian) are compared with what the new database holds.
Run it after `flask --app app import-legacy` and before switching the domain:

    python tools/verify_migration.py --db /data/portal.db --sample 30
    python tools/verify_migration.py --db instance/portal.db --ids 19892,21650

Exit code 0 when every checked act matches, 1 otherwise. Waits between requests like the fetcher.
"""
import argparse
import os
import random
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import legaltext  # noqa: E402
from fetch_old_portal import Fetcher, parse_act, text  # noqa: E402


def words(html_or_text):
    """Text reduced to its words, so differences in markup and spacing do not count."""
    return re.findall(r"\w+", legaltext.to_text(html_or_text or "").lower())


def similarity(a, b):
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sa, sb = set(a), set(b)
    return round(min(len(a), len(b)) / max(len(a), len(b)) * len(sa & sb) / len(sa | sb), 3)


def check(fetcher, row):
    problems = []
    for lang in ("tj", "ru"):
        page = fetcher.get(f"/publicadliya/view_qonunhoview.php?showdetail=&asosi_id={row['legacy_id']}&language={lang}")
        body, card, _ = parse_act(page)
        ours = row[f"body_{lang}"]
        if not ours:
            if lang == "ru" and not row["title_ru"]:
                continue  # act exists in one language only
            if lang == "tj" and row["body_ru"]:
                continue  # Russian-only act
        score = similarity(words(legaltext.sanitize(body)), words(ours))
        if score < 0.98:
            problems.append(f"text {lang}: {score:.0%} alike ({len(words(body))} words there, {len(words(ours))} here)")
        title = row[f"title_{lang}"]
        if title and card.get("title") and text(card["title"]).strip() != title.strip():
            problems.append(f"title {lang} differs")
        if lang == "tj" and card.get("number") and (row["number"] or "") != card["number"]:
            problems.append(f"number: {card['number']} there, {row['number']} here")
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="path to portal.db")
    ap.add_argument("--sample", type=int, default=20, help="number of random acts to check")
    ap.add_argument("--ids", help="comma-separated old numbers (asosi_id) instead of a random sample")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--seed", type=int, help="fixed random seed, to repeat the same sample")
    a = ap.parse_args()
    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row
    cols = "id, legacy_id, number, title_tj, title_ru, body_tj, body_ru"
    if a.ids:
        ids = [int(x) for x in re.findall(r"\d+", a.ids)]
        rows = [r for i in ids for r in conn.execute(
            f"SELECT {cols} FROM documents WHERE kind='act' AND legacy_id=?", (i,))]
        missing = sorted(set(ids) - {r["legacy_id"] for r in rows})
        for i in missing:
            print(f"asosi_id {i}: NOT IMPORTED")
    else:
        missing = []
        all_ids = [r[0] for r in conn.execute("SELECT id FROM documents WHERE kind='act' AND legacy_id IS NOT NULL")]
        random.Random(a.seed).shuffle(all_ids)
        rows = [conn.execute(f"SELECT {cols} FROM documents WHERE id=?", (i,)).fetchone() for i in all_ids[:a.sample]]
    fetcher = Fetcher(a.delay)
    bad = len(missing)
    for row in rows:
        problems = check(fetcher, row)
        bad += bool(problems)
        print(f"asosi_id {row['legacy_id']} -> /doc/{row['id']}: " + ("; ".join(problems) if problems else "ok"))
    total = len(rows) + len(missing)
    print(f"\nChecked {total} acts: {total - bad} match, {bad} need a look.")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
