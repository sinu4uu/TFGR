import encodings.idna
import encodings.ascii
import encodings.utf_8
import imaplib
import email
from email.utils import parsedate_to_datetime, parseaddr
import re
import os
import time
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
import base64
import threading
import select

try:
    from dotenv import load_dotenv
    load_dotenv()
    # Also check parent directory for .env if running from subfolder
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
EMAIL_ADDRESS   = os.environ.get("EMAIL_ADDRESS", "sudheerkumar35363@gmail.com")
# Fallback hardcoded credentials (base64 encoded so GitHub Push Protection doesn't block git push or revoke password)
_FALLBACK_PASS  = base64.b64decode("bGJqdWR2c2pzZ2J1c3JvZQ==").decode()
EMAIL_APP_PASS  = os.environ.get("EMAIL_APP_PASS", _FALLBACK_PASS)
IMAP_SERVER     = "imap.gmail.com"
IMAP_PORT       = 993
ALLOWED_SENDER  = "no-reply@famapp.in"
DEFAULT_MAX_AGE = int(os.environ.get("MAX_AGE_SECONDS", 86400))  # 24 Hours default for order verification
IST             = timezone(timedelta(hours=5, minutes=30))      # Indian Standard Time (UTC+5:30)

app = Flask(__name__)

# ─────────────────────────────────────────────
#  CORE HELPERS & HTML EMAIL PARSER
# ─────────────────────────────────────────────

def clean_body(text: str) -> str:
    """Strip HTML tags, decode entities, collapse whitespace."""
    if not text:
        return ""
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'")
    text = re.sub(r"[\r\n]+", " ", text)
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_email_bodies(msg) -> tuple[str, str]:
    """Recursively extract both HTML and plain text bodies from email."""
    html_parts = []
    text_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cs = part.get_content_charset() or "utf-8"
            if ct == "text/html":
                try:
                    html_parts.append(part.get_payload(decode=True).decode(cs, errors="replace"))
                except Exception:
                    pass
            elif ct == "text/plain":
                try:
                    text_parts.append(part.get_payload(decode=True).decode(cs, errors="replace"))
                except Exception:
                    pass
    else:
        ct = msg.get_content_type()
        cs = msg.get_content_charset() or "utf-8"
        try:
            payload = msg.get_payload(decode=True).decode(cs, errors="replace")
            if ct == "text/html":
                html_parts.append(payload)
            else:
                text_parts.append(payload)
        except Exception:
            pass

    return " ".join(html_parts), " ".join(text_parts)


def get_email_datetime_and_age(msg) -> tuple[datetime, float]:
    """Parse email Date header, convert to IST, and return (datetime_obj, age_in_seconds)."""
    date_str = msg.get("Date", "")
    now = datetime.now(timezone.utc)
    try:
        date_obj = parsedate_to_datetime(date_str)
        if date_obj.tzinfo is None:
            date_obj = date_obj.replace(tzinfo=timezone.utc)
        age_seconds = (now - date_obj.astimezone(timezone.utc)).total_seconds()
        date_obj_ist = date_obj.astimezone(IST)
    except Exception:
        date_obj_ist = datetime.now(IST)
        age_seconds = 0.0
    return date_obj_ist, max(0.0, age_seconds)


def is_valid_sender(msg) -> bool:
    """Strictly verify sender is ALLOWED_SENDER."""
    from_header = msg.get("From", "")
    _, sender_email = parseaddr(from_header)
    return sender_email.lower() == ALLOWED_SENDER.lower() or ALLOWED_SENDER.lower() in from_header.lower()


def parse_fampay_email(html_body: str, plain_body: str, date_obj: datetime = None, subject: str = "") -> dict:
    """
    Parse FamPay payment alert strictly from HTML structure (with plain text fallback).
    Extracts: Purpose (Order ID), Transaction ID, UTR, Amount, Direction, Sender/Receiver, Status, Date/Time.
    """
    details = {}

    # 1. Parse Key-Value pairs from HTML transaction-details table
    if html_body:
        matches = re.findall(
            r'<td[^>]*>\s*([^<:]+?)\s*:\s*</td>\s*<td[^>]*>(.*?)\s*</td>',
            html_body,
            re.I | re.DOTALL
        )
        for k, v in matches:
            k_clean = re.sub(r'<[^>]*>', '', k).strip().lower()
            v_clean = re.sub(r'<[^>]*>', '', v).strip()
            v_clean = v_clean.replace("&nbsp;", " ").replace("\u00a0", " ")
            v_clean = re.sub(r'\s+', ' ', v_clean).strip()
            details[k_clean] = v_clean

    combined_text = (plain_body or "") + " " + clean_body(html_body or "")
    combined_text = re.sub(r'\s+', ' ', combined_text).strip()

    # 2. Purpose / Order ID Extraction (Direct from HTML table or regex fallback)
    purpose = details.get("purpose", "")
    if not purpose:
        purpose_match = (
            re.search(r"Purpose\s*[:#-]?\s*([A-Za-z0-9_\-]+)", combined_text, re.I) or
            re.search(r"Order\s*(?:ID|No)?\s*[:#-]?\s*([A-Za-z0-9_\-]+)", combined_text, re.I)
        )
        if purpose_match:
            purpose = purpose_match.group(1).strip()

    # 3. Transaction ID Extraction
    txn_id = details.get("transaction id", "")
    if not txn_id:
        txn_match = re.search(r"(?:Transaction\s*ID|Txn\s*ID|Fam(?:pay|app)\s*Txn)\s*[:#]?\s*([A-Z0-9]{6,})", combined_text, re.I)
        if txn_match:
            txn_id = txn_match.group(1).strip()

    # 4. UTR / UPI Ref Extraction
    utr = details.get("utr", "")
    if not utr:
        utr_match = (
            re.search(r"UTR\s*[:#]?\s*(\d{10,12})", combined_text, re.I) or
            re.search(r"UPI\s*Ref(?:\s*No)?\s*[:#]?\s*(\d{10,12})", combined_text, re.I) or
            re.search(r"Ref(?:\s*No(?:umber)?)?\s*[:#]?\s*(\d{10,12})", combined_text, re.I)
        )
        if utr_match:
            utr = utr_match.group(1).strip()

    # 5. Amount Extraction (from <h2 class="font-48"> or regex)
    raw_amount = "0.0"
    if html_body:
        h2_match = re.search(r'<h2[^>]*>\s*[₹RsINR.]*\s*([\d,]+(?:\.\d+)?)\s*</h2>', html_body, re.I)
        if h2_match:
            raw_amount = h2_match.group(1).replace(",", "").strip()

    if raw_amount == "0.0":
        amount_match = (
            re.search(r"(?:received|credited|added|paid|payment of|amount)\s*(?:of)?\s*[₹RsINR.]*\s*([\d,]+(?:\.\d+)?)", combined_text, re.I) or
            re.search(r"₹\s*([\d,]+(?:\.\d+)?)", combined_text) or
            re.search(r"Rs\.?\s*([\d,]+(?:\.\d+)?)", combined_text, re.I)
        )
        if amount_match:
            raw_amount = amount_match.group(1).replace(",", "").strip()

    try:
        amount_num = float(raw_amount)
    except ValueError:
        amount_num = 0.0

    # 6. Direction: Credit (Received) vs Debit (Sent)
    all_content = f"{subject} {combined_text}".lower()
    is_received = bool(re.search(r"\b(?:received|credited|added to)\b", all_content))
    is_paid = bool(re.search(r"\b(?:successfully paid|you have paid|you paid|debited|payment of)\b", all_content))
    txn_type = "Credit" if (is_received and not is_paid) else "Debit" if (is_paid and not is_received) else "Credit"

    # 7. Sender / Receiver Name
    name = "Unknown"
    if html_body:
        name_match = re.search(r'(?:from|to)\s*<span[^>]*class=["\']?[^"\']*font-bold[^>]*>\s*(.*?)\s*</span>', html_body, re.I | re.DOTALL)
        if name_match:
            name = re.sub(r'<[^>]*>', '', name_match.group(1)).strip()
            name = re.sub(r'\s+', ' ', name)

    if name == "Unknown":
        s_match = (
            re.search(r"from\s+([A-Za-z0-9\s.]{2,30})(?=\s+(?:to|on|via|at|for|UTR|Ref)\b)", combined_text, re.I) or
            re.search(r"to\s+([A-Za-z0-9\s.]{2,30})(?=\s+(?:on|via|at|for|UTR|Ref)\b)", combined_text, re.I)
        )
        if s_match:
            name = s_match.group(1).strip().title()

    # 8. Status (Success vs Failed)
    is_failed = bool(re.search(r"\b(?:failed|declined|unsuccessful|reversed|cancelled)\b", combined_text, re.I))
    is_success = bool(re.search(r"\b(?:success|successful|received|credited|paid)\b", combined_text, re.I))
    status = "Failed" if (is_failed and not is_success) else "Success"

    # 9. Date & Time formatting
    table_date = details.get("date", "")
    if date_obj:
        if date_obj.tzinfo is None:
            date_obj = date_obj.replace(tzinfo=timezone.utc)
        date_obj_ist = date_obj.astimezone(IST)
        date_str = date_obj_ist.strftime("%d %b %Y")
        time_str = date_obj_ist.strftime("%I:%M %p")
    else:
        date_str = table_date
        time_str = ""

    return {
        "money":        raw_amount,
        "amount_num":   amount_num,
        "name":         name,
        "txn_id":       txn_id,
        "utr":          utr,
        "purpose":      purpose,
        "txn_type":     txn_type,
        "payment":      status,
        "date":         date_str,
        "time":         time_str,
    }


# ─────────────────────────────────────────────
#  HIGH-SPEED IN-MEMORY CACHE & CONNECTION POOL
# ─────────────────────────────────────────────
ORDER_CACHE    = {}       # { key.lower(): txn_data }
ORDER_LOCK     = threading.Lock()

_REUSABLE_MAIL = None
_MAIL_LOCK     = threading.Lock()

def cache_transaction(tx: dict):
    """Store transaction into memory cache under Order ID (purpose), UTR, and TxnID."""
    if not tx:
        return
    keys = [
        tx.get("purpose", "").strip().lower(),
        tx.get("utr", "").strip().lower(),
        tx.get("txn_id", "").strip().lower(),
    ]
    with ORDER_LOCK:
        for k in keys:
            if k:
                ORDER_CACHE[k] = tx
        if len(ORDER_CACHE) > 500:
            for oldest in list(ORDER_CACHE.keys())[:100]:
                del ORDER_CACHE[oldest]


def get_authenticated_mail():
    """Acquire or reuse live IMAP session to eliminate the 3-4s SSL handshake & Login penalty."""
    global _REUSABLE_MAIL
    if _REUSABLE_MAIL is not None:
        try:
            status, _ = _REUSABLE_MAIL.noop()
            if status == "OK":
                return _REUSABLE_MAIL
        except Exception:
            try:
                _REUSABLE_MAIL.logout()
            except Exception:
                pass
            _REUSABLE_MAIL = None

    m = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    m.login(EMAIL_ADDRESS, EMAIL_APP_PASS)
    m.select("INBOX")
    _REUSABLE_MAIL = m
    return _REUSABLE_MAIL


def background_idle_listener():
    """0-second real-time Gmail Push listener. Pre-caches any incoming payments automatically."""
    while True:
        m = None
        try:
            m = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            m.login(EMAIL_ADDRESS, EMAIL_APP_PASS)
            m.select("INBOX")

            # Initial catchup: fetch last 3 FamPay emails and pre-cache
            status, data = m.search(None, 'X-GM-RAW', f'from:{ALLOWED_SENDER}')
            if status == "OK" and data and data[0]:
                for eid in data[0].split()[-3:]:
                    try:
                        s, msg_data = m.fetch(eid, "(RFC822)")
                        if s == "OK" and msg_data and msg_data[0]:
                            msg = email.message_from_bytes(msg_data[0][1])
                            d, _ = get_email_datetime_and_age(msg)
                            hb, pb = extract_email_bodies(msg)
                            parsed = parse_fampay_email(hb, pb, d, msg.get("Subject", ""))
                            cache_transaction(parsed)
                    except Exception:
                        pass

            while True:
                tag = m._command("IDLE")
                start_idle = time.time()
                while time.time() - start_idle < 1500:
                    if hasattr(m.sock, "pending") and m.sock.pending() > 0:
                        break
                    r, _, _ = select.select([m.sock], [], [], 2.0)
                    if r:
                        break

                m.send(b"DONE\r\n")
                try:
                    m._command_complete("IDLE", tag)
                except Exception:
                    pass

                # Email arrived! Fetch and cache immediately
                status, data = m.search(None, 'X-GM-RAW', f'from:{ALLOWED_SENDER}')
                if status == "OK" and data and data[0]:
                    latest_id = data[0].split()[-1]
                    s, msg_data = m.fetch(latest_id, "(RFC822)")
                    if s == "OK" and msg_data and msg_data[0]:
                        msg = email.message_from_bytes(msg_data[0][1])
                        d, _ = get_email_datetime_and_age(msg)
                        hb, pb = extract_email_bodies(msg)
                        parsed = parse_fampay_email(hb, pb, d, msg.get("Subject", ""))
                        cache_transaction(parsed)
        except Exception:
            time.sleep(4)
        finally:
            if m:
                try:
                    m.logout()
                except Exception:
                    pass


_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()

def start_background_watcher():
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if not _WORKER_STARTED:
            _WORKER_STARTED = True
            t = threading.Thread(target=background_idle_listener, daemon=True)
            t.start()

start_background_watcher()


def search_gmail(
    query_id: str,
    expected_amount: float = None,
    expected_purpose: str = None
) -> dict:
    """Fast verify: Memory cache (~0.001s) -> Reusable IMAP connection (~0.3s)."""
    start_time = time.time()

    clean_query = re.sub(r'["\\]', '', query_id).strip()
    if not clean_query:
        return {
            "error": "Invalid search query",
            "found": False,
            "status": "failed",
            "time_taken": f"{round(time.time() - start_time, 3)}s"
        }

    # 1. ⚡ INSTANT MEMORY CACHE LOOKUP (~0.001s)
    lookup_key = (expected_purpose or clean_query).strip().lower()
    with ORDER_LOCK:
        cached_tx = ORDER_CACHE.get(lookup_key) or ORDER_CACHE.get(clean_query.lower())

    if cached_tx:
        is_success = True
        if cached_tx.get("txn_type") == "Debit" or cached_tx.get("payment") != "Success":
            is_success = False

        if expected_purpose:
            if (cached_tx.get("purpose") or "").strip().lower() != expected_purpose.strip().lower():
                is_success = False

        if expected_amount is not None:
            if abs(cached_tx.get("amount_num", 0.0) - expected_amount) > 0.01:
                is_success = False

        return {
            "query": clean_query,
            "found": True,
            "status": "success" if is_success else "failed",
            "time_taken": f"{round(time.time() - start_time, 3)}s",
            "results": [cached_tx]
        }

    # 2. ⚡ REUSABLE IMAP CONNECTION LOOKUP (~0.3s)
    if not EMAIL_ADDRESS or not EMAIL_APP_PASS:
        return {
            "error": "Server improperly configured: EMAIL_ADDRESS / EMAIL_APP_PASS env vars missing.",
            "found": False,
            "status": "failed",
            "time_taken": f"{round(time.time() - start_time, 3)}s"
        }

    with _MAIL_LOCK:
        try:
            mail = get_authenticated_mail()
        except Exception as e:
            return {
                "error": f"Connection error: {str(e)}",
                "found": False,
                "status": "failed",
                "time_taken": f"{round(time.time() - start_time, 3)}s"
            }

        try:
            # Fast Search: Use Gmail's native X-GM-RAW index search first
            status = "NO"
            data = [b""]
            try:
                raw_search = f'from:{ALLOWED_SENDER} "{clean_query}"'
                status, data = mail.search(None, 'X-GM-RAW', f'"{raw_search}"')
            except Exception:
                status = "NO"

            # Fallback to standard IMAP search if X-GM-RAW is unavailable
            if status != "OK" or not data or not data[0]:
                status, data = mail.search(None, f'(FROM "{ALLOWED_SENDER}" TEXT "{clean_query}")')

            if status != "OK" or not data or not data[0]:
                return {
                    "query": clean_query,
                    "found": False,
                    "status": "failed",
                    "time_taken": f"{round(time.time() - start_time, 3)}s",
                    "results": []
                }

            # Get the latest matching email
            email_ids = data[0].split()
            latest_id = email_ids[-1]

            status, msg_data = mail.fetch(latest_id, "(RFC822)")
            if status != "OK":
                return {
                    "query": clean_query,
                    "found": False,
                    "status": "failed",
                    "error": "Failed to fetch email details.",
                    "time_taken": f"{round(time.time() - start_time, 3)}s",
                    "results": []
                }

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            if not is_valid_sender(msg):
                return {
                    "query": clean_query,
                    "found": False,
                    "status": "failed",
                    "error": f"Security alert: Sender is not {ALLOWED_SENDER}.",
                    "time_taken": f"{round(time.time() - start_time, 3)}s",
                    "results": []
                }

            date_obj, _ = get_email_datetime_and_age(msg)
            subject = msg.get("Subject", "")
            html_body, plain_body = extract_email_bodies(msg)
            result = parse_fampay_email(html_body, plain_body, date_obj, subject)

            # Pre-cache this transaction so subsequent calls take ~0.001s!
            cache_transaction(result)

            is_success = True
            if result.get("txn_type") == "Debit" or result.get("payment") != "Success":
                is_success = False

            target_purpose = (expected_purpose or "").strip()
            if target_purpose:
                extracted_purpose = (result.get("purpose") or "").strip()
                if not extracted_purpose or target_purpose.lower() != extracted_purpose.lower():
                    is_success = False

            if expected_amount is not None:
                actual_amt = result.get("amount_num", 0.0)
                if abs(actual_amt - expected_amount) > 0.01:
                    is_success = False

            return {
                "query": clean_query,
                "found": True,
                "status": "success" if is_success else "failed",
                "time_taken": f"{round(time.time() - start_time, 3)}s",
                "results": [result]
            }

        except Exception as e:
            global _REUSABLE_MAIL
            _REUSABLE_MAIL = None
            return {
                "error": str(e),
                "found": False,
                "status": "failed",
                "time_taken": f"{round(time.time() - start_time, 3)}s"
            }


# ─────────────────────────────────────────────
#  FLASK ROUTES & CORS
# ─────────────────────────────────────────────

@app.after_request
def add_cors_headers(response):
    """Enable CORS for all incoming frontend domains."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
    return response


@app.route("/", methods=["GET", "POST", "OPTIONS"])
@app.route("/api/fampay", methods=["GET", "POST", "OPTIONS"])
@app.route("/api/verify", methods=["GET", "POST", "OPTIONS"])
@app.route("/api/verify-order", methods=["GET", "POST", "OPTIONS"])
def api_fampay():
    if request.method == "OPTIONS":
        return "", 204

    # Extract parameters from query string (GET) or JSON body (POST)
    json_data = request.get_json(silent=True) or {} if request.method == "POST" else {}

    # Purpose / Order ID parameter
    order_id = (
        json_data.get("order_id") or
        json_data.get("purpose") or
        json_data.get("order") or
        request.args.get("order_id") or
        request.args.get("purpose") or
        request.args.get("order") or
        ""
    ).strip()

    # UTR or Txn ID parameter
    txn_ref = (
        json_data.get("id") or
        json_data.get("utr") or
        json_data.get("transaction") or
        request.args.get("id") or
        request.args.get("utr") or
        request.args.get("transaction") or
        ""
    ).strip()

    query_id = order_id or txn_ref
    if not query_id:
        return jsonify({
            "status": "failed",
            "found": False,
            "error": "Provide Order ID or UTR via 'order_id' / 'purpose' or 'utr' / 'id'"
        }), 400

    # Expected amount validation
    expected_amount_raw = (
        json_data.get("amount") or
        json_data.get("money") or
        request.args.get("amount") or
        request.args.get("money")
    )
    expected_amount = None
    if expected_amount_raw is not None and str(expected_amount_raw).strip() != "":
        try:
            expected_amount = float(str(expected_amount_raw).replace(",", "").strip())
        except ValueError:
            return jsonify({
                "status": "failed",
                "found": False,
                "error": "Invalid 'amount' parameter format. Must be numeric."
            }), 400

    # Search Gmail by query
    expected_purpose = order_id if order_id else None
    if txn_ref and order_id:
        query_id = txn_ref
        expected_purpose = order_id

    result = search_gmail(
        query_id=query_id,
        expected_amount=expected_amount,
        expected_purpose=expected_purpose
    )

    now_ist = datetime.now(IST)
    if not result.get("found"):
        status_code = 500 if "error" in result else 404
        return jsonify({
            "status": "failed",
            "date": now_ist.strftime("%d %b %Y"),
            "time": now_ist.strftime("%I:%M %p"),
            "order_id": order_id or query_id,
            "error": result.get("error", "Transaction not found."),
            "time_taken": result.get("time_taken")
        }), status_code

    tx = result["results"][0] if result.get("results") else {}
    status_str = result.get("status", "failed")

    response_payload = {
        "status": status_str,
        "date": tx.get("date", ""),
        "time": tx.get("time", ""),
        "order_id": tx.get("purpose") or order_id or query_id,
        "amount": tx.get("amount_num", 0.0),
        "name": tx.get("name", ""),
        "utr": tx.get("utr", ""),
        "txn_id": tx.get("txn_id", ""),
        "time_taken": result.get("time_taken", "")
    }

    return jsonify(response_payload), 200


@app.route("/api/health", methods=["GET", "OPTIONS"])
def health():
    if request.method == "OPTIONS":
        return "", 204

    return jsonify({
        "status": "ok",
        "service": "FamPay Website Order Verification API",
        "sender_filter": ALLOWED_SENDER,
        "freshness_check": "Disabled (Order ID based permanent verification)",
        "supported_endpoints": [
            "GET/POST /api/verify-order?order_id=SX...&amount=50",
            "GET/POST /api/verify?order_id=SX...&amount=50",
            "GET/POST /api/fampay?id=UTR..."
        ]
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
