"""Outgoing e-mail. Messages are queued in the `outbox` table and delivered by a
background thread (or `flask --app app send-mail` from cron). Without SMTP settings
messages are written to the log and marked 'logged', so nothing is lost.

Environment: SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD,
SMTP_FROM (defaults to SMTP_USER), SMTP_SSL=1 for port 465.
"""
import logging
import os
import smtplib
import sqlite3
import threading
from email.message import EmailMessage
from email.utils import formataddr

log = logging.getLogger("portal.mail")
_lock = threading.Lock()


def smtp_configured():
    return bool(os.environ.get("SMTP_HOST"))


def _send(msg):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465" if os.environ.get("SMTP_SSL") == "1" else "587"))
    user, password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    if os.environ.get("SMTP_SSL") == "1":
        server = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
        server.starttls()
    try:
        if user:
            server.login(user, password or "")
        server.send_message(msg)
    finally:
        server.quit()


def deliver_pending(db_path, site_name="Portali huquqi", limit=200):
    """Send queued messages. Safe to call often; only one run at a time."""
    if not _lock.acquire(blocking=False):
        return 0
    sent = 0
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM outbox WHERE status='queued' ORDER BY id LIMIT ?", (limit,)).fetchall()
        sender = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or "noreply@localhost"
        for r in rows:
            if not smtp_configured():
                log.info("MAIL (not sent, SMTP not configured) to=%s subject=%s\n%s", r["to_email"], r["subject"], r["body"])
                conn.execute("UPDATE outbox SET status='logged', sent_at=datetime('now') WHERE id=?", (r["id"],))
                continue
            msg = EmailMessage()
            msg["From"] = formataddr((site_name, sender))
            msg["To"] = r["to_email"]
            msg["Subject"] = r["subject"]
            msg.set_content(r["body"])
            try:
                _send(msg)
                conn.execute("UPDATE outbox SET status='sent', sent_at=datetime('now'), attempts=attempts+1 WHERE id=?",
                             (r["id"],))
                sent += 1
            except Exception as e:  # noqa: BLE001 - keep the queue moving
                attempts = r["attempts"] + 1
                conn.execute("UPDATE outbox SET status=?, attempts=?, error=? WHERE id=?",
                             ("failed" if attempts >= 5 else "queued", attempts, str(e)[:500], r["id"]))
                log.warning("mail to %s failed: %s", r["to_email"], e)
            conn.commit()
        conn.commit()
        conn.close()
    finally:
        _lock.release()
    return sent


def deliver_async(db_path, site_name):
    threading.Thread(target=deliver_pending, args=(db_path, site_name), daemon=True).start()
