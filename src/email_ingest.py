"""Phase 3 — closed-platform discovery via job-alert EMAILS.

Why this exists: LinkedIn, Naukri, Indeed and Instahyre all forbid automated
scraping and automated applications. Enforcement is real and bans are permanent,
and your primary LinkedIn/Naukri account is worth more than any automation gain.

So this module never touches those websites. It reads *emails they voluntarily
send you* over IMAP, extracts the job headline and the original tracking link,
and feeds them into the normal matching flow flagged apply_policy='manual_only'.
You click the link yourself, in your own browser, logged in as yourself — which is
exactly what the alert email is for. Nothing about your account looks automated.
"""

import email
import html
import imaplib
import re
from email.header import decode_header, make_header

from .config import (sb, ALERT_IMAP_HOST, ALERT_EMAIL, ALERT_EMAIL_APP_PASSWORD,
                     MAX_EMAILS_PER_RUN, log_event)

# Which senders we parse, and what a job link from them looks like.
SOURCES = [
    ("email:linkedin",  r"linkedin\.com",   r"linkedin\.com/(?:comm/)?jobs/view/(\d+)"),
    ("email:naukri",    r"naukri\.com",     r"naukri\.com/(?:job-listings|jobs)[^\s\"'<>]*"),
    ("email:instahyre", r"instahyre\.com",  r"instahyre\.com/[^\s\"'<>]*(?:job|opportunit)[^\s\"'<>]*"),
    ("email:iimjobs",   r"iimjobs\.com",    r"iimjobs\.com/j/[^\s\"'<>]*"),
    ("email:indeed",    r"indeed\.com",     r"indeed\.com/(?:viewjob|rc/clk)[^\s\"'<>]*"),
    # Wider India coverage — set up alerts on whichever of these you use, forward
    # their emails to the alert mailbox, and they parse like the ones above.
    ("email:foundit",   r"foundit\.(?:in|com)", r"foundit\.in/[^\s\"'<>]*job[^\s\"'<>]*"),
    ("email:hirist",    r"hirist\.(?:com|tech)", r"hirist\.(?:com|tech)/[^\s\"'<>]*\d{4,}[^\s\"'<>]*"),
    ("email:cutshort",  r"cutshort\.io",    r"cutshort\.io/job[^\s\"'<>]*"),
    ("email:shine",     r"shine\.com",      r"shine\.com/jobs/[^\s\"'<>]*"),
    ("email:timesjobs", r"timesjobs\.com",  r"timesjobs\.com/[^\s\"'<>]*job[^\s\"'<>]*"),
    ("email:wellfound", r"wellfound\.com",  r"wellfound\.com/(?:jobs|l/)[^\s\"'<>]*"),
    ("email:glassdoor", r"glassdoor\.(?:com|co\.in)", r"glassdoor\.(?:com|co\.in)/[Jj]ob[^\s\"'<>]*"),
]

ANCHOR_RE = re.compile(r"<a\b[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
BOILERPLATE = ("view job", "apply now", "see all", "view all", "unsubscribe", "see more",
               "view jobs", "apply", "learn more", "click here", "see job", "view details",
               "manage alerts", "settings", "privacy policy", "open in app", "download")


def _text(fragment: str) -> str:
    return WS_RE.sub(" ", html.unescape(TAG_RE.sub(" ", fragment or ""))).strip()


def _html_part(msg) -> str:
    """Prefer text/html; fall back to text/plain."""
    best = ""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/html", "text/plain"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/html":
            return body
        best = best or body
    return best


def _context_after(body: str, pos: int, span: int = 260) -> str:
    """LinkedIn/Naukri render the title as a link with 'Company · Location · ₹salary'
    immediately after it. Grab that neighbourhood as both metadata and description."""
    return _text(body[pos:pos + span])


def _split_meta(ctx: str) -> tuple[str, str]:
    """Best-effort company / location out of a 'Company · City, State' style tail."""
    parts = [p.strip() for p in re.split(r"·|\||•|—|\u2013", ctx) if p.strip()]
    company = parts[0][:120] if parts else ""
    location = ""
    for p in parts[1:4]:
        if re.search(r"(india|remote|hybrid|bengaluru|bangalore|mumbai|delhi|gurgaon|gurugram|"
                     r"hyderabad|pune|chennai|noida|kolkata|ahmedabad|jaipur|kochi|indore)",
                     p, re.I):
            location = p[:120]
            break
    return company, location or "India"


def _classify(from_addr: str, url: str) -> tuple[str, str] | tuple[None, None]:
    for name, sender_pat, url_pat in SOURCES:
        if re.search(sender_pat, from_addr, re.I) and re.search(url_pat, url, re.I):
            return name, url
    return None, None


def _parse_message(from_addr: str, body: str) -> list[dict]:
    out, seen_urls = [], set()
    for m in ANCHOR_RE.finditer(body):
        url, inner = m.group(1), _text(m.group(2))
        source, _ = _classify(from_addr, url)
        if not source:
            continue
        if not inner or len(inner) < 6 or inner.lower() in BOILERPLATE:
            continue
        if any(b == inner.lower() for b in BOILERPLATE):
            continue
        key = re.sub(r"[?&](utm_|trk|refId|midToken|eBP|lipi)[^&]*", "", url)[:400]
        if key in seen_urls:
            continue
        seen_urls.add(key)

        ctx = _context_after(body, m.end())
        company, location = _split_meta(ctx)
        out.append({
            "source": source,
            "external_id": (re.search(r"(\d{6,})", url).group(1) if re.search(r"(\d{6,})", url) else ""),
            "title": inner[:300],
            "company": company,
            "location": location,
            # No real JD in an alert email. Keep the neighbourhood text: it usually holds
            # salary, seniority and applicant count, which is real signal for the scorer.
            "description": ctx[:1200],
            "url": url[:1500],
            "ats": "external",
            "apply_policy": "manual_only",     # <- never automated, by design
            "salary": "",
            "posted_at": None,
        })
    return out


def _already_seen(message_id: str) -> bool:
    if not message_id:
        return False
    r = sb.table("email_seen").select("message_id").eq("message_id", message_id).execute()
    return bool(r.data)


def _mark_seen(message_id: str) -> None:
    if message_id:
        try:
            sb.table("email_seen").insert({"message_id": message_id[:400]}).execute()
        except Exception:
            pass


def fetch_email_alerts() -> list[dict]:
    """Read unread job-alert emails and return normalised job rows.
    Silently returns [] if the mailbox secrets aren't configured."""
    if not (ALERT_EMAIL and ALERT_EMAIL_APP_PASSWORD):
        return []

    rows = []
    try:
        box = imaplib.IMAP4_SSL(ALERT_IMAP_HOST)
        box.login(ALERT_EMAIL, ALERT_EMAIL_APP_PASSWORD)
        box.select("INBOX")
        typ, data = box.search(None, "UNSEEN")
        ids = (data[0].split() if data and data[0] else [])[-MAX_EMAILS_PER_RUN:]

        for num in ids:
            try:
                # BODY.PEEK leaves \Seen alone; email_seen is our own idempotency record,
                # so a crash never loses an email and a re-run never double-parses one.
                typ, msg_data = box.fetch(num, "(BODY.PEEK[])")
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                msg_id = str(msg.get("Message-ID") or "").strip()
                if _already_seen(msg_id):
                    box.store(num, "+FLAGS", "\\Seen")
                    continue
                from_addr = str(make_header(decode_header(msg.get("From") or "")))
                body = _html_part(msg)
                found = _parse_message(from_addr, body)
                rows.extend(found)
                _mark_seen(msg_id)
                box.store(num, "+FLAGS", "\\Seen")
                if not found:
                    log_event("email_no_jobs_parsed", {
                        "from": from_addr[:200],
                        "subject": str(make_header(decode_header(msg.get("Subject") or "")))[:200],
                        "hint": "sender not in SOURCES, or the HTML layout changed",
                    })
            except Exception as e:
                log_event("email_parse_error", {"error": str(e)[:300]})
        box.logout()
    except Exception as e:
        log_event("email_connect_error", {"error": str(e)[:300]})
        return rows

    log_event("email_ingest_done", {"listings_found": len(rows)})
    return rows
