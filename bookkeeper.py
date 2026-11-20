#!/usr/bin/env python3
"""Scan Gmail for invoices/receipts, download them and prepare a quarterly submission folder.

    python bookkeeper.py                      # last completed quarter (+/- 14 days grace on the email date)
    python bookkeeper.py --quarter 2026-Q2    # a specific quarter
    python bookkeeper.py --all                # no date filter
    python bookkeeper.py --dry-run            # list matching emails only
    python bookkeeper.py --ai                 # run the AI on every document (default: only where regex found no total)
    python bookkeeper.py --no-ai              # regex only, no API calls
    python bookkeeper.py --html-to-pdf        # only convert already-saved .html receipts to PDF (no Gmail)
    python bookkeeper.py --final --quarter 2026-Q3   # no Gmail: table, PDFs, worksheet, bank matching for the quarter
    python bookkeeper.py --portal                    # local portal: collect, upload, build, review, VAT return form
"""
import argparse
import base64
import csv
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from urllib.parse import unquote, urlparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

import pdfplumber
import pypdfium2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']


def load_config() -> dict:
    """The private settings: config.yml next to the script (gitignored; copy config.example.yml), or the file named by
    BOOKKEEPER_CONFIG. Mailboxes, the AI model, and everything tuned to one owner's mail and suppliers live there."""
    import yaml
    path = Path(os.environ.get('BOOKKEEPER_CONFIG') or HERE / 'config.yml')
    return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}


CONFIG = load_config()
cfg_list = lambda key, default=(): tuple(CONFIG.get(key) or default)
cfg_regex = lambda key: '|'.join(f'(?:{x})' for x in CONFIG.get(key) or []) or r'(?!)'  # no entries: matches nothing
MODEL = os.environ.get('BOOKKEEPER_MODEL') or CONFIG.get('ai_model') or ''
MIN_CONFIDENCE = 0.7
# Mailboxes to scan; the first is the original one (plain ledger keys, token.json). config.yml `accounts`, or BOOKKEEPER_ACCOUNTS=a@x.com,b@y.com
ACCOUNTS = [a.strip() for a in (os.environ.get('BOOKKEEPER_ACCOUNTS') or ','.join(CONFIG.get('accounts') or [])).split(',') if a.strip()]
GRACE_DAYS = 14  # emails arrive a bit before/after the period; search wider, then filter on the invoice date
# Subject / body words that make an email a candidate. config.yml `keywords` replaces the list (other languages).
KEYWORDS = cfg_list('keywords', ('invoice', 'receipt', 'bill', 'billing', 'statement', 'order confirmation', 'payment received',
                                 'payment confirmation', 'payment confirmed', 'payment successful', 'your payment', 'your order',
                                 'your purchase', 'subscription', 'tax invoice', 'credit note', 'creditnota',
                                 'factuur', 'bon', 'bestelbevestiging', 'rekening', 'betaling', 'rechnung', 'quittung'))
# Mail that matches keywords but is never a document: own calendar reminders, own sent mail,
# review requests, newsletters and delivery tracking.
EXCLUDE = ('from:calendar-notification@google.com', 'from:me', 'subject:out-for-delivery',
           'subject:"delivered notification"') + cfg_list('search_exclude')  # config.yml: more Gmail search terms to exclude
SEARCH = ('(' + ' OR '.join(f'"{k}"' if ' ' in k else k for k in KEYWORDS) + ')'
          ' OR (has:attachment {filename:pdf filename:invoice filename:receipt filename:factuur}'
          ' -from:me -from:calendar-notification@google.com)')
SEARCH = f'({SEARCH}) ' + ' '.join(f'-{e}' for e in EXCLUDE)
# Mail the keyword search catches that is never a bookkeeping document: marketplace orders, bank/investment
# statements, threads, shared-file notices, terms updates, order status changes. Subject rules do not apply
# when the mail carries a PDF, so a real invoice sent in a reply is kept.
# config.yml `noise_senders` / `noise_subjects`: regular expressions for the owner's own senders and subjects.
NOISE_SENDERS = re.compile(r'bank|card services|' + cfg_regex('noise_senders'), re.I)
NOISE_ALWAYS = re.compile(r'\bbank\b|(?<!billing )statement', re.I)  # bank/account/investment statements, even with a PDF (but not "Billing Statement", which AWS uses for invoices)
NOISE_SUBJECTS = re.compile(r'^\s*(re|fw|fwd|aw)\s*:|shared .{0,40}with you|(updates?|changes?|review).{0,30}(terms|t&cs)|'
                            r'terms (of service|and conditions|& conditions)|\bt&cs\b|direct debit|aanvraag incompleet|'
                            r'order.{0,40}(cancel+ed|was updated|delivered|shipped|dispatched|on its way|pickup|pick-up|collection|arrive)|'
                            r'delivered:|delivery|shipped|shipment|dispatched|on its way|tracking|track your|pick-?up|ready for collection|'
                            r'\breminder|transaction alert|credit received|bezorg|verzend|versand|lieferung|' + cfg_regex('noise_subjects'), re.I)
SAVE_EXT = {'application/pdf': '.pdf', 'image/png': '.png', 'image/jpeg': '.jpg'}
SAVE_SUFFIXES = {'.pdf', '.png', '.jpg', '.jpeg', '.xlsx', '.xls', '.csv', '.doc', '.docx', '.xml'}
MIN_IMAGE_BYTES = 15_000  # smaller inline images are logos and tracking pixels
# Hosts whose invoice links return the PDF without a login. Links from emails can point anywhere (tracking, unsubscribe),
# so only these are ever fetched. config.yml `hosted_pdf_hosts` adds hosts for other suppliers.
HOSTED_PDF_HOSTS = {'invoice.bolt.eu', 'invoice.taxify.eu', 'pay.stripe.com', 'invoice.stripe.com', 'dashboard.stripe.com',
                    'doclink.live.boltsvc.net'} | set(cfg_list('hosted_pdf_hosts'))
# Hosts that also serve login-only pages: only URLs under these exact prefixes are fetched (signed "view my bill" links).
HOSTED_PDF_PREFIXES = cfg_list('hosted_pdf_prefixes')
MAX_PDF_BYTES = 5_000_000
LINK_RE = re.compile(r'https?://[^\s"\'<>)]+', re.I)
LINK_HINT = re.compile(r'invoice|receipt|bill|factuur|download|pdf|statement|pay\.stripe|invoice\.stripe', re.I)
# The one table. summary.csv, the packet indexes and the worksheet are all written from these rows.
CSV_FIELDS = ['doc_id', 'supplier', 'supplier_vat', 'vies_status', 'doc_type', 'invoice_no', 'invoice_date', 'period', 'currency',
              'net', 'vat', 'gross', 'fx_rate', 'fx_source', 'net_eur', 'vat_eur', 'gross_eur', 'rc_vat_eur', 'rate_pct',
              'treatment', 'reason', 'risk', 'confidence', 'needs_review', 'review_reason', 'duplicate_of', 'bucket',
              'payment', 'category', 'source', 'source_file', 'page', 'gmail_link', 'direction']


# ---------- quarters ----------

def quarter_range(q: str) -> tuple[date, date]:
    m = re.fullmatch(r'(\d{4})-Q([1-4])', q)
    if not m:
        sys.exit(f'Bad quarter "{q}", expected e.g. 2026-Q3')
    year, n = int(m[1]), int(m[2])
    start = date(year, 3 * n - 2, 1)
    end = date(year + (n == 4), 3 * n % 12 + 1, 1) - timedelta(days=1)
    return start, end


def last_quarter(today: date | None = None) -> str:
    """The last completed calendar quarter, e.g. 2026-Q3 when run in October 2026."""
    today = today or date.today()
    n = (today.month - 1) // 3  # quarters completed this year
    return f'{today.year}-Q{n}' if n else f'{today.year - 1}-Q4'


MONTHS = {m: i for i, m in enumerate('jan feb mar apr may jun jul aug sep oct nov dec'.split(), 1)}
DATE_LABEL = r'(?:invoice date|date of issue|issue date|issued|billing date|bill date|factuurdatum|rechnungsdatum|datum|date)'
DATE_VALUE = (r'(\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})')
DATE_RE = re.compile(DATE_LABEL + r'\s*[:\-]?\s*' + DATE_VALUE, re.I)


def parse_date(raw: str) -> date | None:
    """Parse the date formats seen on invoices; numeric dates are day-first (European)."""
    raw = raw.strip()
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
            return date.fromisoformat(raw)
        m = re.fullmatch(r'(\d{1,2})[./-](\d{1,2})[./-](\d{4})', raw)
        if m:
            return date(int(m[3]), int(m[2]), int(m[1]))
        m = re.fullmatch(r'(\d{1,2})\s+([A-Za-z]+)\.?,?\s+(\d{4})', raw)
        if m and m[2][:3].lower() in MONTHS:
            return date(int(m[3]), MONTHS[m[2][:3].lower()], int(m[1]))
        m = re.fullmatch(r'([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})', raw)
        if m and m[1][:3].lower() in MONTHS:
            return date(int(m[3]), MONTHS[m[1][:3].lower()], int(m[2]))
    except ValueError:
        pass
    return None


def find_invoice_date(text: str) -> str | None:
    """First labelled date in the text (e.g. 'Invoice date: 28 Sep 2026'), as YYYY-MM-DD."""
    for m in DATE_RE.finditer(text):
        if d := parse_date(m[1]):
            return d.isoformat()
    return None


# ---------- gmail ----------

def token_path(account: str) -> Path:
    return HERE / ('token.json' if account == ACCOUNTS[0] else f'token_{re.sub(r"[^a-z0-9]+", "_", account.lower())}.json')


def gmail_service(account: str):
    """One OAuth token per mailbox. The first account keeps the original token.json; log in as the matching Google user."""
    token = token_path(account)
    creds = Credentials.from_authorized_user_file(token, SCOPES) if token.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = InstalledAppFlow.from_client_secrets_file(HERE / 'credentials.json', SCOPES).run_local_server(port=0, login_hint=account)
        token.write_text(creds.to_json())
    return build('gmail', 'v1', credentials=creds)


def list_message_ids(svc, query: str) -> list[str]:
    ids, page = [], None
    while True:
        r = svc.users().messages().list(userId='me', q=query, pageToken=page, maxResults=500).execute()
        ids += [m['id'] for m in r.get('messages', [])]
        page = r.get('nextPageToken')
        if not page:
            return ids


def headers(msg) -> dict:
    return {h['name'].lower(): h['value'] for h in msg['payload'].get('headers', [])}


def walk(part):
    yield part
    for p in part.get('parts', []):
        yield from walk(p)


def decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        self.skip += tag in ('script', 'style')

    def handle_endtag(self, tag):
        self.skip -= tag in ('script', 'style')

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.out.append(data.strip())


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    return '\n'.join(p.out)


def message_body(msg) -> tuple[str, str]:
    """Return (plain text, raw html) of the email body."""
    plain, html = '', ''
    for p in walk(msg['payload']):
        data = p.get('body', {}).get('data')
        if not data or p.get('filename'):
            continue
        text = decode(data).decode('utf-8', 'replace')
        if p['mimeType'] == 'text/plain':
            plain += text
        elif p['mimeType'] == 'text/html':
            html += text
    return plain or html_to_text(html), html


def fetch_attachments(svc, msg) -> list[tuple[str, str, bytes]]:
    out = []
    for p in walk(msg['payload']):
        name = p.get('filename')
        body = p.get('body', {})
        if not name:
            continue
        # Providers often send PDFs as application/octet-stream, so trust the suffix too.
        ext = SAVE_EXT.get(p.get('mimeType')) or Path(name).suffix.lower()
        if ext not in SAVE_SUFFIXES:
            continue
        if ext in ('.png', '.jpg', '.jpeg') and body.get('size', 0) < MIN_IMAGE_BYTES:
            continue  # logo / tracking pixel
        if body.get('attachmentId'):
            body = svc.users().messages().attachments().get(userId='me', messageId=msg['id'], id=body['attachmentId']).execute()
        if body.get('data'):
            out.append((name, ext, decode(body['data'])))
    return out


# ---------- cache: a Gmail message, its attachments and hosted PDFs are downloaded once ----------

CACHE_ROOT: Path | None = None  # output/_cache, set in main(); None = caching off (--no-cache)


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.part')
    tmp.write_bytes(data)
    tmp.replace(path)  # an interrupted run never leaves a half-written cache file


def get_message(svc, account: str, raw_id: str) -> dict:
    """The full Gmail message (headers and body parts). Message content never changes, so it is fetched once."""
    path = CACHE_ROOT and CACHE_ROOT / account / f'{raw_id}.json'
    if path and path.exists():
        try:
            return json.loads(path.read_text())
        except ValueError:
            pass  # corrupt cache file: fetch again
    msg = svc.users().messages().get(userId='me', id=raw_id, format='full').execute()
    if path:
        _write_atomic(path, json.dumps(msg).encode())
    return msg


def get_attachments(svc, msg: dict, account: str) -> list[tuple[str, str, bytes]]:
    """Attachments of a message, from the cache when present. Gmail attachment ids are not stable, so the bytes are cached."""
    folder = CACHE_ROOT and CACHE_ROOT / account / msg['id']
    manifest = folder and folder / 'attachments.json'
    if manifest and manifest.exists():
        try:
            return [(e['name'], e['ext'], (folder / e['file']).read_bytes()) for e in json.loads(manifest.read_text())]
        except (ValueError, OSError, KeyError):
            pass
    found = fetch_attachments(svc, msg)
    if folder:
        entries = []
        for i, (name, ext, data) in enumerate(found):
            _write_atomic(folder / f'{i}{ext}', data)
            entries.append({'name': name, 'ext': ext, 'file': f'{i}{ext}'})
        _write_atomic(manifest, json.dumps(entries).encode())  # written last: it marks the entry complete
    return found


def is_noise(meta: dict, attachments) -> bool:
    if NOISE_SENDERS.search(meta['sender']) or NOISE_ALWAYS.search(meta['subject']):
        return True
    return not any(ext == '.pdf' for _, ext, _ in attachments) and bool(NOISE_SUBJECTS.search(meta['subject']))


def invoice_links(html: str, plain: str) -> list[str]:
    """Links in the email that probably lead to a hosted invoice/receipt (Stripe, AWS, GCP, Xero...)."""
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.I) + LINK_RE.findall(plain)
    seen = []
    for u in hrefs:
        if u.startswith('http') and LINK_HINT.search(u) and u not in seen:
            seen.append(u)
    return seen[:5]


def pdf_text(data: bytes, limit: int = 6000) -> str:
    try:
        with pdfplumber.open(BytesIO(data)) as pdf:
            return '\n'.join(pg.extract_text() or '' for pg in pdf.pages[:3])[:limit]
    except Exception as e:
        logger.warning('Could not read PDF: %s', e)
        return ''


def unwrap_tracking(url: str) -> str:
    """Email providers wrap links as https://track.example/L0/<percent-encoded target>/1/<id>; return the target."""
    m = re.search(r'/C?L0/([^/]+)', url)
    return unquote(m[1]).rstrip(']') if m else url


def fetch_hosted_pdfs(links: list[str]) -> list[tuple[str, str, bytes]]:
    """Download invoice PDFs that are only linked from the email (Bolt, Stripe receipts...)."""
    out = []
    for link in links:
        url = unwrap_tracking(link)
        u = urlparse(url)
        if u.scheme != 'https' or not (u.hostname in HOSTED_PDF_HOSTS or url.startswith(HOSTED_PDF_PREFIXES)):
            continue
        cached = CACHE_ROOT and CACHE_ROOT / 'hosted' / (hashlib.sha256(url.encode()).hexdigest() + '.pdf')
        if cached and cached.exists():
            data = cached.read_bytes()
        else:
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'bookkeeper'}), timeout=30) as r:
                    data = r.read(MAX_PDF_BYTES + 1)
            except Exception as e:
                logger.warning('Could not fetch %s: %s', u.hostname, e)
                continue
            if cached and data[:4] == b'%PDF' and len(data) <= MAX_PDF_BYTES:
                _write_atomic(cached, data)  # failures are never cached, so they are retried
        if data[:4] == b'%PDF' and len(data) <= MAX_PDF_BYTES and data not in [d for _, _, d in out]:
            out.append((u.hostname, '.pdf', data))
    return out


def upgrade_record(rec: dict, out: Path) -> bool:
    """Ledger entry saved only as an email-body .html: fetch the hosted PDF invoice, if any, and add it."""
    if rec.get('hosted_tried') or not rec.get('files') or not all(f.endswith('.html') for f in rec['files']):
        return False
    rec['hosted_tried'] = True
    folder = out / rec.get('folder', 'all')
    pdfs = fetch_hosted_pdfs(rec.get('links', []))
    for (_, ext, data), html_name in zip(pdfs, rec['files']):
        dest = unique(folder / (Path(html_name).stem + ext))
        dest.write_bytes(data)
        rec['files'].insert(0, str(dest.relative_to(folder)))
        rec['document_type'] = 'invoice'
    return bool(pdfs)


# ---------- html -> pdf ----------

CHROME_CANDIDATES = ('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                     '/Applications/Chromium.app/Contents/MacOS/Chromium',
                     '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
                     '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser')
PDF_WORKERS = 6
PDF_TIMEOUT = 90  # seconds per file; receipts load remote images


def find_chrome() -> str | None:
    """A Chromium-based browser for headless printing: $BOOKKEEPER_CHROME, then the usual macOS apps, then PATH."""
    for c in (os.environ.get('BOOKKEEPER_CHROME'), *CHROME_CANDIDATES,
              *(shutil.which(n) for n in ('google-chrome', 'chromium', 'chrome'))):
        if c and Path(c).exists():
            return c
    return None


def html_to_pdf(chrome: str, src: Path, dest: Path) -> bool:
    """Print one saved email-body .html to PDF with headless Chrome. The HTML is untrusted mail, so a temporary copy
    carries a CSP that blocks its scripts (disabling scripting in Chrome itself breaks printing)."""
    with tempfile.TemporaryDirectory() as profile:  # separate profile so several instances can run at once
        safe = Path(profile) / 'in.html'
        safe.write_bytes(b'<meta http-equiv="Content-Security-Policy" content="script-src \'none\'">' + src.read_bytes())
        try:
            subprocess.run([chrome, '--headless=new', '--disable-gpu', '--no-pdf-header-footer',
                            f'--user-data-dir={profile}', f'--print-to-pdf={dest}', safe.as_uri()],
                           capture_output=True, timeout=PDF_TIMEOUT, check=True)
        except (subprocess.SubprocessError, OSError) as e:
            logger.error('PDF conversion failed for %s: %s', src.name, e)
            dest.unlink(missing_ok=True)
            return False
    return dest.exists() and dest.stat().st_size > 0


def convert_html_records(records: list[dict], out: Path) -> int:
    """Turn every .html in the records' files into a PDF (in parallel), delete the .html and update the record."""
    jobs = [(rec, i, out / rec.get('folder', 'all') / f) for rec in records
            for i, f in enumerate(rec.get('files') or []) if f.endswith('.html') and (out / rec.get('folder', 'all') / f).exists()]
    if not jobs:
        return 0
    chrome = find_chrome()
    if not chrome:
        logger.error('HTML left unconverted: no Chrome/Chromium found. Set BOOKKEEPER_CHROME to its executable.')
        return 0
    dests = [unique(src.with_suffix('.pdf')) for _, _, src in jobs]  # decided up front so parallel jobs cannot collide
    logger.info('Converting %d HTML receipts to PDF with %s workers', len(jobs), PDF_WORKERS)
    with ThreadPoolExecutor(PDF_WORKERS) as ex:
        results = list(ex.map(lambda j: html_to_pdf(chrome, j[0][2], j[1]), zip(jobs, dests)))
    done = 0
    for (rec, i, src), dest, ok in zip(jobs, dests, results):
        if ok:
            rec['files'][i] = str(dest.relative_to(out / rec.get('folder', 'all')))
            src.unlink()
            done += 1
    logger.info('Converted %d/%d HTML receipts to PDF', done, len(jobs))
    return done


# ---------- extraction ----------

PROMPT = """You read one document (an email plus any attachment text or file) for a Maltese sole trader's VAT bookkeeping.
Report only what the document shows. Do not judge whether VAT is deductible. Reply with ONLY a JSON object:
{{"is_financial_document": bool,
 "doc_type": "tax_invoice|simplified_invoice|fiscal_receipt|order_confirmation|quotation|payment_receipt|statement|fee_schedule|other",
 "vendor": str, "date": "YYYY-MM-DD or null", "invoice_number": str|null, "currency": "ISO code or null",
 "total_amount": number|null, "net_amount": number|null, "vat_amount": number|null, "vat_rate": number|null,
 "vat_amount_eur": number|null, "vat_country": "ISO-2 or null", "supplier_country": "ISO-2 or null",
 "supplier_vat": str|null, "buyer_vat": str|null, "reverse_charge": bool, "supply": "services|goods|mixed|null",
 "category": "software|hosting|telecom|hardware|professional_services|transport|food_drink|groceries|entertainment|accommodation|rent|utilities|education|household|other",
 "confidence": 0..1}}
- is_financial_document: false for newsletters, marketing, shipping notices and reminders without an amount.
- doc_type: tax_invoice = a document titled invoice, with an invoice number and a VAT breakdown (or an explicit
  reverse-charge / 0% note), also when a shop's POS printed it; fiscal_receipt = till or card receipt without an invoice number; payment_receipt = confirms payment of an earlier invoice or bill;
  order_confirmation = an order or booking, not yet invoiced; quotation = offer, estimate or pro-forma;
  fee_schedule = price list; statement = bank, card or account statement.
- total_amount = GROSS amount payable INCLUDING VAT. net_amount = total excluding VAT. vat_amount = VAT charged, in the
  document currency (0 if the document says none, null if it does not say). net_amount + vat_amount must equal total_amount.
- vat_rate is the percentage only (18 for "18.000%"). Never put a rate into an amount field, or a net price into vat_amount.
- vat_amount_eur: only when the currency is not EUR and the document also states the VAT in EUR.
- vat_country: the country whose VAT RATE is charged, which is often not the supplier's country (e.g. "VAT - Malta 18%" = MT
  even from a US, Irish or Luxembourg supplier; Dutch BTW 21% = NL). null if no VAT.
- supplier_country: where the supplier is established (its address). supplier_vat: the supplier's VAT number as printed.
- buyer_vat: the CUSTOMER's VAT number if printed on the document (bill-to block, "your VAT number"), else null.
- reverse_charge: true only if the document says reverse charge or similar wording.
- date: the invoice date (date of issue), not the due or payment date.

From: {sender}
Subject: {subject}
Email date: {sent}

--- Email body ---
{body}

--- Attachment text ---
{attachments}
"""
FACTS_V = 2  # version of the fact set above; older ledger entries are read again
DOC_TYPES = ('tax_invoice', 'simplified_invoice', 'fiscal_receipt', 'order_confirmation', 'quotation', 'payment_receipt', 'statement', 'fee_schedule', 'other')
TAX_DOCS = ('tax_invoice', 'simplified_invoice')  # the only documents that can support input VAT


# List prices per million tokens (config: ai_price_per_mtok), only for the cost estimate in the log
PRICE_IN, PRICE_OUT = (list(CONFIG.get('ai_price_per_mtok') or []) + [0.0, 0.0])[:2]  # USD per million input / output tokens, for the cost line in the log
USAGE = {'calls': 0, 'binary': 0, 'in': 0, 'out': 0}


def ai_ready() -> bool:
    return bool(os.environ.get('ANTHROPIC_API_KEY') and MODEL)


def ai_client(required_by: str = ''):
    """The AI client, or None when the API key or the model is not configured (then only the regex pass and the rules run)."""
    if not ai_ready():
        if required_by:
            sys.exit(f'ERROR: {required_by} needs the ANTHROPIC_API_KEY environment variable and ai_model in config.yml (or --no-ai)')
        return None
    import anthropic
    return anthropic.Anthropic()

MIN_TEXT_CHARS = 100  # less text than this from a PDF means a scan: send the file itself
MAX_SEND_BYTES = 5_000_000
MEDIA = {'.pdf': ('document', 'application/pdf'), '.png': ('image', 'image/png'), '.jpg': ('image', 'image/jpeg'), '.jpeg': ('image', 'image/jpeg')}


def log_usage():
    if USAGE['calls']:
        logger.info('AI: %d calls (%d with the file attached), %d input / %d output tokens, about $%.2f (ai_price_per_mtok in config.yml)',
                    USAGE['calls'], USAGE['binary'], USAGE['in'], USAGE['out'], USAGE['in'] / 1e6 * PRICE_IN + USAGE['out'] / 1e6 * PRICE_OUT)


def unreadable_file(attachments, att_text: str):
    """Text comes first. Only when no text could be pulled out of the attachments, return the first PDF/image to send as is."""
    if len(att_text.strip()) >= MIN_TEXT_CHARS:
        return None
    for name, ext, data in attachments:
        if ext in MEDIA and len(data) <= MAX_SEND_BYTES and (ext != '.pdf' or len(pdf_text(data).strip()) < MIN_TEXT_CHARS):
            return ext, data
    return None


def extract_ai(client, meta: dict, body: str, att_text: str, binary=None) -> dict:
    content = [{'type': 'text', 'text': PROMPT.format(body=body[:4000], attachments=att_text[:8000] or ('(attached file)' if binary else '(none)'), **meta)}]
    if binary:
        kind, mime = MEDIA[binary[0]]
        content.insert(0, {'type': kind, 'source': {'type': 'base64', 'media_type': mime, 'data': base64.b64encode(binary[1]).decode()}})
    r = client.messages.create(model=MODEL, max_tokens=700, messages=[{'role': 'user', 'content': content}])
    USAGE['calls'] += 1
    USAGE['binary'] += bool(binary)
    USAGE['in'] += r.usage.input_tokens
    USAGE['out'] += r.usage.output_tokens
    text = r.content[0].text
    res, _ = json.JSONDecoder().raw_decode(text[text.index('{'):])  # the first JSON object; anything after it is ignored
    if res.get('doc_type') not in DOC_TYPES:
        res['doc_type'] = 'other'
    res['document_type'] = 'invoice' if res['doc_type'] in TAX_DOCS else 'other' if res['doc_type'] in ('statement', 'fee_schedule', 'other') else 'receipt'
    res['facts_v'] = FACTS_V
    return res


TOTAL_RE = re.compile(r'(?:total\s+incl\w*\.?\s+(?:vat|btw|tax)|totaal\s+incl\w*\.?\s+btw|gesamtbetrag|amount due|te betalen|total|totaal|gesamt)'
                      r'\D{0,25}?(€|\$|£|EUR|USD|GBP)?\s*(\d[\d.,]*\d)', re.I)
VAT_RE = re.compile(r'(?:vat|btw|mwst|tax)[^\d\n]{0,15}\d{1,2}(?:[.,]\d+)?\s*%[^\d\n]{0,10}(\d[\d.,]*\d)', re.I)
INVNO_RE = re.compile(r'(?:invoice|factuur|rechnung)\s*(?:no\.?|number|nr\.?|#)\s*[:.]?\s*([A-Z0-9][A-Z0-9/_-]{3,})', re.I)


def to_number(raw: str) -> float | None:
    raw = raw.replace('.', '').replace(',', '.') if raw.rfind(',') > raw.rfind('.') else raw.replace(',', '')
    try:
        return float(raw)
    except ValueError:
        return None


def parse_fields(text: str) -> dict:
    """Best-effort total, currency, VAT amount and invoice number from a document's text."""
    text = re.sub(r'-\s*\n\s*', '-', text)  # invoice numbers wrapped at a hyphen
    out = {}
    # Prefer an explicit "total incl. VAT" over a bare "total" (which is often the net amount)
    for m in sorted(TOTAL_RE.finditer(text), key=lambda m: 'incl' not in m[0].lower()):
        if (n := to_number(m[2])) is not None and n < 1_000_000 and re.search(r'[.,]\d{2}$', m[2]):
            out['total_amount'] = n
            out['currency'] = {'€': 'EUR', '$': 'USD', '£': 'GBP'}.get(m[1], m[1] and m[1].upper())
            break
    if 'total_amount' in out and not out['currency']:
        c = re.search(r'\((EUR|USD|GBP)\)|\b(EUR|USD|GBP)\b|(€)', text)
        out['currency'] = c and {'€': 'EUR'}.get(c[3], c[1] or c[2])
    if (m := VAT_RE.search(text)) and (v := to_number(m[1])) is not None:
        out['vat_amount'] = v
    if (m := INVNO_RE.search(text)) and re.search(r'\d', m[1]):  # a real number has a digit; "Account" / "INVOICE" are headings
        out['invoice_number'] = m[1].strip('.-')
    return out


def enrich_from_pdf(rec: dict, out: Path) -> bool:
    """Fill missing total/currency/VAT/invoice number on a ledger entry from its first PDF."""
    pdf = next((f for f in rec.get('files', []) if f.endswith('.pdf')), None)
    if rec.get('pdf_parsed') or rec.get('ai') or not pdf:
        return False
    rec['pdf_parsed'] = True
    path = out / rec.get('folder', 'all') / pdf
    if not path.exists():
        return False
    found = parse_fields(pdf_text(path.read_bytes()))
    changed = False
    for k, v in found.items():
        if v is not None and rec.get(k) is None:
            rec[k], changed = v, True
    return changed


def extract_heuristic(meta: dict, body: str, att_text: str, has_file: bool, has_links: bool = False) -> dict:
    blob = f'{meta["subject"]}\n{body}\n{att_text}'
    found = parse_fields(blob)
    total, cur = found.get('total_amount'), found.get('currency')
    name = re.sub(r'<.*?>|"', '', meta['sender']).strip()
    domain = re.search(r'@([\w.-]+)', meta['sender'])
    parts = domain[1].split('.') if domain else []
    if len(parts) > 2 and parts[-2] in ('com', 'co', 'org', 'net', 'gov'):  # acme.com.mt -> acme, not com
        parts.pop(-2)
    vendor = name if name and '@' not in name else (parts[-2] if len(parts) > 1 else meta['sender'])
    # Collect everything the search matched; sorting out noise comes later.
    return {'is_financial_document': True, 'document_type': 'invoice' if has_file else 'receipt',
            'vendor': vendor, 'date': find_invoice_date(blob), 'invoice_number': found.get('invoice_number'), 'total_amount': total, 'currency': cur,
            'vat_amount': found.get('vat_amount'), 'category': None, 'confidence': 0.4}


# ---------- output ----------

def slug(s, n=40) -> str:
    return re.sub(r'[^A-Za-z0-9]+', '-', str(s or 'unknown')).strip('-')[:n] or 'unknown'


def unique(path: Path) -> Path:
    i, p = 2, path
    while p.exists():
        p = path.with_name(f'{path.stem}_{i}{path.suffix}')
        i += 1
    return p


# Folder-name merges for vendors that email under several names (lower-case slug -> folder).
# config.yml `vendor_aliases`, e.g. {noreply-billing-acme-online: Acme}.
VENDOR_ALIASES = {str(k).lower(): str(v) for k, v in (CONFIG.get('vendor_aliases') or {}).items()}


def vendor_folder(vendor, base: Path) -> str:
    """One folder per company: legal suffixes dropped, aliases merged, and an existing folder reused whatever its case."""
    s = slug(vendor)
    s = re.sub(r'(-(pbc|inc|llc|ltd|limited|gmbh|bv|plc))+$', '', s, flags=re.I) or s
    s = VENDOR_ALIASES.get(s.lower(), s)
    if base.exists():
        s = next((d.name for d in base.iterdir() if d.is_dir() and d.name.lower() == s.lower()), s)
    return s


def save_files(qdir: Path, info: dict, msg_id: str, attachments, html: str, review: bool, plain: str = '') -> list[str]:
    total = info.get('total_amount')
    has_total = isinstance(total, (int, float))
    base = qdir / 'needs_review' if review else qdir if has_total else qdir / 'no_amount'  # no_amount: sort these by hand
    folder = base / vendor_folder(info.get('vendor'), base)
    folder.mkdir(parents=True, exist_ok=True)
    stem = '_'.join([info['date'], slug(info.get('vendor')), slug(info.get('invoice_number') or msg_id[:8]),
                     f'{total:.2f}{info.get("currency") or ""}' if has_total else 'noamount'])
    files = [(f'{stem}{ext}', data) for _, ext, data in attachments]
    if not files and html:  # receipt that only lives in the email body
        files = [(f'{stem}.html', html.encode())]
    elif not files and plain:  # plain-text-only email
        files = [(f'{stem}.txt', plain.encode())]
    saved = []
    for name, data in files:
        dest = unique(folder / name)
        dest.write_bytes(data)
        saved.append(str(dest.relative_to(qdir)))
    return saved


def archive_previous(out: Path, label: str, ledger: dict, period) -> None:
    """Start this scope from scratch so every file in its folder is from this run: move the old folder to
    output/_archive/<label>_<timestamp> and forget the ledger entries for it (or dated in its period)."""
    old = out / label
    if old.exists() and any(old.iterdir()):
        dest = out / '_archive' / f'{label}_{datetime.now():%Y%m%d-%H%M%S}'
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(old, dest)
        logger.info('Moved previous %s output to %s', label, dest)
    lo, hi = (period[0].isoformat(), period[1].isoformat()) if period else ('', '9999-12-31')
    for k in [k for k, v in ledger.items() if v.get('source') not in ('manual', 'sales') and (v.get('folder') == label or lo <= v.get('date', '') <= hi)]:
        del ledger[k]


def valid_date(s) -> str | None:
    try:
        return date.fromisoformat(str(s)).isoformat()
    except ValueError:
        return None


def adopt_files(rec: dict, out: Path, qdir: Path, period) -> None:
    """A ledger hit from an earlier run (e.g. --all) that falls in this period: copy its files into this folder."""
    folder = rec.get('folder', 'all')
    if not period or folder == qdir.name or not rec['files']:
        return
    if not period[0].isoformat() <= rec['date'] <= period[1].isoformat():
        return
    new = []
    for f in rec['files']:
        src = out / folder / f
        if not src.exists():
            new.append(f)
            continue
        dest = unique(qdir / f)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        new.append(str(dest.relative_to(qdir)))
    rec['files'], rec['folder'] = new, qdir.name


# ---------- Bolt Business monthly report (CSV with one row per ride) ----------

BOLT_COLUMNS = {'user_invoice_link', 'driver_vat_code', 'ride_start', 'total_price', 'vat', 'ride_price'}


def read_bolt_report(path: Path) -> list[dict]:
    """Rows of a Bolt Business report CSV, or [] if the file is anything else."""
    try:
        with open(path, newline='', encoding='utf-8-sig') as f:
            rd = csv.DictReader(f)
            return list(rd) if BOLT_COLUMNS <= set(rd.fieldnames or []) else []
    except (OSError, UnicodeDecodeError, csv.Error):
        return []


def bolt_ride_record(row: dict, base: dict) -> dict | None:
    """One ledger record per ride, with the exact VAT Bolt reports. No AI involved."""
    num = lambda x: float(x) if str(x).strip() else 0.0
    try:
        total, net, vat = num(row['total_price']), num(row['ride_price']), num(row['vat'])
        start = datetime.fromisoformat(row['ride_start'].replace('Z', '+00:00')).astimezone(ZoneInfo('Europe/Malta'))
    except (ValueError, KeyError):
        return None
    link = row['user_invoice_link']
    inv = re.search(r'[?&]i=([^&]+)', link)
    return {**{k: base.get(k) for k in ('folder', 'account', 'gmail_link')},
            'is_financial_document': True, 'document_type': 'invoice', 'vendor': 'Bolt Malta',
            'date': start.date().isoformat(), 'invoice_number': inv and unquote(inv[1]),
            'total_amount': total, 'currency': (row.get('currency') or 'EUR').upper(), 'vat_amount': vat,
            'vat_rate': round(vat / net * 100) if vat and net else None, 'vat_deductible': None,
            'net_amount': round(total - vat, 2), 'doc_type': 'tax_invoice', 'supplier_country': 'MT', 'vat_country': 'MT' if vat else None,
            'supply': 'services', 'facts_v': FACTS_V,
            'category': 'transport', 'confidence': 1.0, 'needs_review': False, 'ai': False, 'vat_checked': True,
            'supplier': row.get('driver_official_name'), 'supplier_vat': row.get('driver_vat_code') if str(row.get('driver_vat_code', '')).startswith('MT') else None,
            'subject': f'Bolt ride {start:%Y-%m-%d} to {row.get("to", "")[:40]}', 'links': [link], 'source': 'bolt_csv', 'files': []}


def expand_bolt_reports(ledger: dict, out: Path, period=None) -> int:
    """Turn saved Bolt report CSVs into one record per ride and download each ride's PDF invoice.
    The report itself stays in the ledger, filed under noVAT (it is a list, the rides are the documents)."""
    added = 0
    for key, rec in list(ledger.items()):
        if rec.get('csv_checked') or not rec.get('files'):
            continue
        rec['csv_checked'] = True
        base = out / rec.get('folder', 'all')
        for f in [f for f in rec['files'] if f.endswith('.csv')]:
            rows = read_bolt_report(base / f)
            if not rows:
                continue
            logger.info('Bolt report %s: %d rides', f, len(rows))
            rec.update(doc_type='statement', facts_v=FACTS_V, vat_checked=True, vat_reason='Bolt report; its rides are filed individually')
            for i, row in enumerate(rows):
                k = f'{key}#{i}'
                ride = bolt_ride_record(row, rec)
                if k in ledger or not ride or (period and not period[0].isoformat() <= ride['date'] <= period[1].isoformat()):
                    continue
                pdfs = fetch_hosted_pdfs(ride['links'])
                if pdfs:
                    ride['files'] = save_files(base, ride, k.replace('#', '_')[-12:], pdfs, '', False)
                else:
                    ride['needs_review'], ride['vat_reason'] = True, 'ride invoice PDF could not be downloaded'
                    logger.warning('No PDF for Bolt ride %s (%s)', ride['invoice_number'], ride['date'])
                ledger[k] = ride
                added += 1
    return added


# ---------- VAT triage: eligibility, VAT amount, duplicates, folders ----------

BUCKET_DIRS = {'main': '', 'noVAT': 'noVAT', 'no_amount': 'no_amount', 'needs_review': 'needs_review', 'duplicates': 'duplicates'}
VENDOR_PROMPT = """For each supplier name below (with sample email subjects) decide whether a Maltese VAT-registered
freelancer could claim input VAT on a typical purchase from it. deductible=false: groceries, meat, supermarkets,
restaurants, food delivery, entertainment, hotels, personal goods (tobacco, clothing, gifts), banks and card
statements, newsletters and marketing. deductible=true: software, hosting, telecom, equipment, professional services,
business fees. deductible=null: you cannot tell from the name (generic names such as "com", a private person).
Reply with ONLY a JSON object: {{"<supplier name exactly as given>": {{"deductible": true|false|null, "reason": "max 6 words"}}}}

{items}
"""


def ask_json(client, prompt: str, max_tokens: int, content=None) -> dict:
    r = client.messages.create(model=MODEL, max_tokens=max_tokens, messages=[{'role': 'user', 'content': content or prompt}])
    USAGE['calls'] += 1
    USAGE['in'] += r.usage.input_tokens
    USAGE['out'] += r.usage.output_tokens
    text = r.content[0].text
    return json.loads(text[text.index('{'):text.rindex('}') + 1])


def classify_vendors(client, docs: list[dict], out: Path) -> dict:
    """Vendor-level VAT eligibility, cached in output/vendors.json. Edit that file to override the AI: an entry is never
    overwritten once it exists. Only new vendor names cost an API call (60 per call)."""
    path = out / 'vendors.json'
    vendors = json.loads(path.read_text()) if path.exists() else {}
    new = {}
    for r in docs:
        if r.get('vendor') and r['vendor'] not in vendors:
            new.setdefault(r['vendor'], []).append(r.get('subject') or '')
    if client and new:
        names = list(new)
        logger.info('Classifying %d new vendors', len(names))
        for i in range(0, len(names), 60):
            chunk = names[i:i + 60]
            items = '\n'.join(f'- {n} | ' + ' / '.join(x[:60] for x in new[n][:2]) for n in chunk)
            try:
                got = ask_json(client, VENDOR_PROMPT.format(items=items), 4000)
            except Exception as e:
                logger.warning('Vendor classification failed for a batch of %d: %s', len(chunk), e)
                continue
            for n in chunk:
                if isinstance(got.get(n), dict):
                    vendors[n] = {'deductible': got[n].get('deductible'), 'reason': str(got[n].get('reason', ''))[:60]}
        path.write_text(json.dumps(vendors, indent=2, ensure_ascii=False))
    return vendors


def local_inputs(rec: dict, out: Path):
    """Rebuild what extract_ai needs from the files already saved for a ledger entry."""
    base = out / rec.get('folder', 'all')
    body, att_text, attachments = '', '', []
    for f in rec.get('files') or []:
        path = base / f
        if not path.exists():
            continue
        ext = path.suffix.lower()
        if ext in ('.html', '.txt') and not body:
            raw = path.read_text(errors='replace')
            body = html_to_text(raw) if ext == '.html' else raw
        elif ext == '.pdf':
            data = path.read_bytes()
            att_text += pdf_text(data) + '\n'
            attachments.append((path.name, ext, data))
        elif ext in MEDIA:
            attachments.append((path.name, ext, path.read_bytes()))
    return body, att_text, unreadable_file(attachments, att_text)


FACT_KEYS = ('doc_type', 'document_type', 'net_amount', 'vat_amount', 'vat_rate', 'vat_amount_eur', 'vat_country', 'supplier_country',
             'supplier_vat', 'buyer_vat', 'reverse_charge', 'supply', 'category', 'confidence', 'facts_v')


def read_facts_with_ai(client, docs: list[dict], out: Path, since: str, save=None, workers: int = 8) -> None:
    """One AI call per document whose facts (FACTS_V) have not been read yet. Documents dated before `since`
    (the VAT registration) are outside every return and are not read."""
    todo = [r for r in docs if r.get('facts_v') != FACTS_V and r.get('date', '') >= since]
    if not client or not todo:
        return
    logger.info('Reading document facts with the AI for %d documents', len(todo))

    def one(rec):
        body, att_text, binary = local_inputs(rec, out)
        meta = {'sender': rec.get('vendor') or '', 'subject': rec.get('subject') or '', 'sent': rec.get('date') or ''}
        return extract_ai(client, meta, body, att_text, binary)

    def apply(rec, res):
        if not res.get('is_financial_document', True):
            res['doc_type'], res['document_type'] = 'other', 'other'
        for k in ('total_amount', 'currency'):
            if res.get(k) is not None:
                rec[k] = res[k]
        if res.get('invoice_number') and norm_no(res['invoice_number']) != norm_no(rec.get('invoice_number')):  # keep "A-1" over "A 1"
            rec['invoice_number'] = res['invoice_number']
        for k in FACT_KEYS:  # facts replace whatever the regex pass guessed, including a null
            rec[k] = res.get(k)
        if valid_date(res.get('date')):
            rec['date'] = valid_date(res['date'])
        for k in ('vat_deductible', 'vat_reason', 'vat_source'):  # judgements of the old prompt
            rec.pop(k, None)
        rec['vat_checked'], rec['ai'] = True, True

    with ThreadPoolExecutor(workers) as ex:
        for n, (rec, fut) in enumerate([(r, ex.submit(one, r)) for r in todo], 1):
            try:
                apply(rec, fut.result())
            except Exception as e:  # stays unread, retried next run
                logger.warning('Fact read failed for %s: %s', rec.get('subject', '')[:50], e)
            if n % 100 == 0:
                logger.info('Facts read %d/%d', n, len(todo))
                if save:
                    save()  # an interrupted run keeps what it already paid for


NOT_TAX_DOCS = ('order_confirmation', 'quotation', 'payment_receipt', 'fiscal_receipt')


def norm_no(s) -> str:
    """Invoice number for comparing: "93FE9B6A-0073", "93FE9B6A 0073" and "INv-1039" / "INV-1039" are the same number."""
    return re.sub(r'[^a-z0-9]', '', str(s or '').lower())


def mark_duplicates(docs: list[dict], ledger_keys: dict) -> int:
    """Flag repeats of the same document (same mail in two mailboxes, an image and a PDF of one invoice, an order
    confirmation and its invoice). The best copy is kept: Bolt CSV / manual, then tax invoices, then PDFs.
    - same supplier + invoice number (an email listing several numbers is a repeat when each is already seen)
    - the same invoice number (6+ characters) within 3 days under another sender name
    - same supplier + gross + currency on the same day, or within 3 days when one side is not a tax invoice, unless both
      carry different invoice numbers of the same kind or different VAT amounts (two rides at the same price)
    The same gross within 3 days at another supplier is only noted (possible_duplicate_of), never set aside."""
    def rank(r):
        exts = {f.rsplit('.', 1)[-1] for f in r.get('files', [])}
        return (r.get('source') in ('bolt_csv', 'manual'), r.get('doc_type') in TAX_DOCS, r.get('document_type') == 'invoice',
                r.get('total_amount') is not None, 'pdf' in exts, bool(exts & {'png', 'jpg'}), bool(r.get('vat_amount')), r.get('confidence') or 0)

    def gap(a, b):
        try:
            return abs((date.fromisoformat(a['date']) - date.fromisoformat(b['date'])).days)
        except (ValueError, TypeError, KeyError):
            return 999

    seen_inv, seen_no, seen_amt, seen_gross, n = {}, {}, defaultdict(list), defaultdict(list), 0
    for r in docs:
        r['duplicate_of'] = None
        r.pop('possible_duplicate_of', None)
    for r in sorted(docs, key=rank, reverse=True):
        # Vendor key = first word of the company folder: "acme" / "Acme Communications Limited" are one company.
        v = vendor_folder(r.get('vendor'), Path('/nonexistent')).lower().split('-')[0]
        raw = str(r['invoice_number']) if r.get('invoice_number') and re.search(r'\d', str(r['invoice_number'])) else ''
        inv = norm_no(raw) or None
        cur, tot = r.get('currency'), r.get('total_amount')
        parts = [norm_no(x) for x in re.split(r'[,;]', raw)] if re.search(r'[,;]', raw) else []  # "671096, 671101": one email covering several invoices
        first = inv and seen_inv.get((v, inv, cur))
        if not first and len(parts) > 1 and all((v, x, cur) in seen_inv for x in parts):
            first = seen_inv[(v, parts[0], cur)]
        if not first and inv and len(inv) >= 6 and (hit := seen_no.get(inv)) and gap(hit, r) <= 3:
            first = hit
        if not first and tot is not None:
            for hit, hinv in seen_amt[(v, tot, cur)]:
                loose = r.get('doc_type') in NOT_TAX_DOCS or hit.get('doc_type') in NOT_TAX_DOCS
                trusted = {'bolt_csv', 'manual'} & {r.get('source'), hit.get('source')}
                va, vb = r.get('vat_amount'), hit.get('vat_amount')
                if gap(hit, r) > (3 if loose else 0):
                    continue
                if inv and hinv and inv != hinv and not loose and not trusted:
                    continue
                if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and abs(va - vb) > 0.02 and not (inv and inv == hinv):
                    continue
                first = hit
                break
        if first and first is not r:
            r['duplicate_of'] = ledger_keys.get(id(first), '?')
            n += 1
            continue
        if inv:
            seen_inv[(v, inv, cur)] = r
            seen_no.setdefault(inv, r)
        if tot is not None:
            seen_amt[(v, tot, cur)].append((r, inv))
            if tot >= 20 and (other := next((h for h, hv in seen_gross[(tot, cur)] if hv != v and gap(h, r) <= 3), None)):
                r['possible_duplicate_of'] = ledger_keys.get(id(other), '?')
            seen_gross[(tot, cur)].append((r, v))
    return n


def organize(docs: list[dict], out: Path) -> None:
    """Move each document's files to <folder>/[bucket/]<Vendor>/ (the bucket comes from the table row's treatment)."""
    touched = set()
    for rec in docs:
        b = rec.get('bucket') or 'noVAT'
        base = out / rec.get('folder', 'all')
        new_files = []
        for f in rec.get('files') or []:
            parts = Path(f).parts
            rest = Path(*parts[1:]) if parts[0] in BUCKET_DIRS.values() and parts[0] else Path(f)
            if len(rest.parts) == 1:  # older flat layout: file straight in the folder, give it its company folder
                rest = Path(vendor_folder(rec.get('vendor'), base / BUCKET_DIRS[b])) / rest
            target = Path(BUCKET_DIRS[b]) / rest
            if target != Path(f) and (base / f).exists():
                dest = unique(base / target)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(base / f, dest)
                touched.add(base / f)
                target = dest.relative_to(base)
            else:
                target = Path(f)
            new_files.append(str(target))
        rec['files'] = new_files
    for old in touched:  # remove vendor folders emptied by the moves
        d = old.parent
        while d != out and d.exists() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent


def triage(docs: list[dict], out: Path, client, ledger_path: Path | None = None, ledger: dict | None = None) -> list[dict]:
    """Ledger records -> table rows: supplier classes, duplicates, document facts, treatment by rule, folders, PDFs."""
    docs = [r for r in docs if r.get('is_financial_document')]
    keys = {id(v): k for k, v in (ledger or {}).items()}
    save = (lambda: ledger_path.write_text(json.dumps(ledger, indent=2))) if ledger_path and ledger is not None else None
    rules = load_rules(out)
    vendors = classify_vendors(client, docs, out)
    read_facts_with_ai(client, docs, out, rules['registration_date'], save)
    dups = mark_duplicates(docs, keys)  # after the read: the kept copy is chosen by document type
    rows = build_table(docs, keys, rules, vendors, out)
    organize(docs, out)
    convert_html_records(docs, out)
    for row in rows:  # the moves and the HTML-to-PDF step changed the paths
        row['source_file'] = ' | '.join(row['_rec'].get('files') or [])
    count = defaultdict(int)
    for row in rows:
        count[row['treatment']] += 1
    logger.info('Triage: %s; %d duplicates set aside', ', '.join(f'{t} {n}' for t, n in sorted(count.items())), dups)
    return rows


# ---------- the VAT table: one normalised row per document, every output is written from it ----------

EU = set('AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE'.split())
CLAIM = ('domestic_input', 'oss_risk')  # domestic input VAT (Boxes 31/37); oss_risk is kept apart so it can be left out
RC = ('reverse_charge_eu', 'reverse_charge_non_eu')
BLOCKED_CATEGORIES = ('food_drink', 'groceries', 'entertainment', 'accommodation')
TREATMENT_BUCKET = {'domestic_input': 'main', 'oss_risk': 'main', 'reverse_charge_eu': 'main', 'reverse_charge_non_eu': 'main',
                    'duplicate': 'duplicates', 'needs_review': 'needs_review'}
ECB_URL = 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml'
VIES_URL = 'https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{cc}/vat/{n}'


def r2(x):
    return None if x is None else float(Decimal(str(x)).quantize(Decimal('0.01'), ROUND_HALF_UP))


def num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def load_rules(out: Path) -> dict:
    """rules.yml (in the repo: general settings and supplier classes) plus output/decisions.yml (the owner's own decisions:
    suppliers used for the business, and per-document treatments). Decisions win. Rules are data, not code."""
    import yaml
    rules = yaml.safe_load((HERE / 'rules.yml').read_text()) or {}
    path = out / 'decisions.yml'
    dec = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    rules['suppliers'] = (dec.get('suppliers') or []) + (rules.get('suppliers') or [])
    apath = out / 'review_answers.yml'  # written by the review page (--review); decisions.yml wins over it
    ans = (yaml.safe_load(apath.read_text()) or {}) if apath.exists() else {}
    rules['documents'] = {norm_no(k): v for k, v in {**(ans.get('documents') or {}), **(dec.get('documents') or {})}.items()}
    rules['ignored_payments'] = set(ans.get('ignored_payments') or [])
    rules['payment_links'] = ans.get('payment_links') or {}  # payment id -> doc_id, set by the owner in the portal
    rules['registration_date'] = str(dec.get('registration_date') or rules.get('registration_date') or '0000-00-00')
    rules['credit_brought_forward'] = dec.get('credit_brought_forward') or {}
    rules['ignore_payments'] = dec.get('ignore_payments') or []
    rules['vat_number'] = norm_no(dec.get('vat_number') or os.environ.get('BOOKKEEPER_VAT_NUMBER'))
    return rules


def supplier_rule(rules: dict, vendor) -> dict:
    return next((s for s in rules.get('suppliers', []) if re.search(s['match'], str(vendor or ''), re.I)), {})


class FxRates:
    """ECB euro reference rates (foreign currency per EUR), cached in output/_cache/ecb_rates.json for a day.
    rate(cur, day) is the rate of that day, or the last one published before it (weekends, holidays)."""

    def __init__(self, out: Path):
        self.path, self.rates = out / '_cache' / 'ecb_rates.json', None

    def load(self):
        if self.path.exists() and datetime.now().timestamp() - self.path.stat().st_mtime < 86400:
            self.rates = json.loads(self.path.read_text())
            return
        try:
            import xml.etree.ElementTree as ET
            with urllib.request.urlopen(ECB_URL, timeout=60) as r:
                root = ET.fromstring(r.read())
            self.rates = {d.get('time'): {c.get('currency'): float(c.get('rate')) for c in d} for d in root.iter() if d.get('time') and d.get('time') >= '2025-01-01'}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.rates))
        except Exception as e:
            logger.warning('ECB rates not available (%s)', e)
            self.rates = json.loads(self.path.read_text()) if self.path.exists() else {}

    def rate(self, cur: str, day: str):
        if self.rates is None:
            self.load()
        for d in sorted((d for d in self.rates if d <= day), reverse=True)[:10]:
            if cur in self.rates[d]:
                return self.rates[d][cur], d
        return None, None


def vies_check(vat_no, country, cache: dict) -> str:
    """Supplier VAT number against VIES. '' when there is no checkable number. Results are cached with the date checked."""
    raw = re.sub(r'[^A-Za-z0-9]', '', str(vat_no or '')).upper()
    if raw.isdigit() and country:
        raw = {'GR': 'EL'}.get(country, country) + raw
    m = re.fullmatch(r'([A-Z]{2})([A-Z0-9]{2,12})', raw)
    if not m:
        return ''
    cc, n = m.groups()
    if cc == 'EU':
        return 'not in VIES (non-Union OSS number)'
    if {'EL': 'GR'}.get(cc, cc) not in EU:
        return 'not an EU number'
    hit = cache.get(cc + n)
    if not hit:
        if os.environ.get('BOOKKEEPER_NO_VIES'):
            return 'not checked'
        try:
            with urllib.request.urlopen(VIES_URL.format(cc=cc, n=n), timeout=20) as r:
                res = json.loads(r.read())
            if res.get('userError') not in ('VALID', 'INVALID'):
                return f'could not verify ({res.get("userError")})'
            hit = cache[cc + n] = {'valid': bool(res.get('isValid')), 'checked': date.today().isoformat()}
        except Exception as e:
            return f'could not verify ({type(e).__name__})'
    return f'{"valid" if hit["valid"] else "INVALID"} ({hit["checked"]})'


def build_row(key: str, rec: dict, rules: dict, vendors: dict, fx) -> dict:
    """One table row from a ledger record: checked amounts, EUR values, treatment (first matching rule) and review reasons.
    The record itself is not changed. Nothing here is a tax decision: uncertain cases go to needs_review."""
    why, risk = [], []
    sup = supplier_rule(rules, rec.get('vendor'))
    home, std = rules.get('home_country', 'MT'), rules.get('standard_rate', 18)
    gross, net, vat, rate = (num(rec.get(k)) for k in ('total_amount', 'net_amount', 'vat_amount', 'vat_rate'))
    cur = str(rec.get('currency') or '').upper() or None
    day = rec.get('date') or ''
    dt = rec.get('doc_type')

    # amounts: gross, net and VAT are separate fields and must agree
    if gross is not None:
        if net is not None and vat is not None and abs(net + vat - gross) > 0.02:
            if abs(net - gross) <= 0.02:
                gross = r2(net + vat)
                why.append('recorded total was the net amount; gross recomputed as net + VAT')
            else:
                why.append(f'net {net:.2f} + VAT {vat:.2f} != gross {gross:.2f}')
                net = None
        if vat is not None and vat > gross:
            why.append(f'recorded VAT {vat:.2f} is larger than the total; dropped')
            vat = None
        if rate and 0 < rate < 100:
            calc = r2(gross * rate / (100 + rate))
            if vat is None:
                vat = calc
                why.append(f'VAT calculated from the rate: {gross:.2f} x {rate:g} / {100 + rate:g} = {calc:.2f}')
            elif vat > calc + 0.02:
                why.append(f'recorded VAT {vat:.2f} is impossible at {rate:g}% of {gross:.2f}; recomputed {calc:.2f}')
                vat, net = calc, None
            elif vat < calc - 0.02:
                why.append(f'recorded VAT {vat:.2f} is below {rate:g}% of the total ({calc:.2f}): mixed rates or items without VAT?')
        if net is None and vat is not None:
            net = r2(gross - vat)
    has_vat = bool(vat and vat > 0)

    # EUR values: the invoice's own EUR VAT if it states one, else the ECB reference rate of the invoice date
    fx_rate = fx_source = net_eur = vat_eur = gross_eur = None
    if gross is not None and cur == 'EUR':
        fx_rate, fx_source, net_eur, vat_eur, gross_eur = 1.0, '', net, vat, gross
    elif gross is not None and cur:
        stated = num(rec.get('vat_amount_eur'))
        if stated and has_vat:
            fx_rate, fx_source, vat_eur = round(vat / stated, 4), 'EUR VAT stated on the invoice', r2(stated)
            net_eur = r2(stated * 100 / rate) if rate else r2(net * stated / vat)
            gross_eur = r2(net_eur + vat_eur)
        else:
            ecb, ecb_day = fx.rate(cur, day)
            if ecb:
                fx_rate, fx_source = ecb, f'ECB {ecb_day}'
                net_eur, vat_eur, gross_eur = (r2(x / ecb) if x is not None else None for x in (net, vat, gross))
            else:
                fx_source = f'no ECB rate for {cur}'
    elif gross is not None:
        why.append('currency unknown')

    # treatment: first match wins
    sc = str(sup.get('country') or rec.get('supplier_country') or '').upper()[:2] or None
    vc = str(rec.get('vat_country') or '').upper()[:2] or None
    known = rules.get('vat_rates') or {}
    if has_vat and rate and vc != home and rate in (known.get(home) or [std]) and rate not in (known.get(vc) or []) and (vc is None or vc in known):
        vc = home  # e.g. 18% from a Luxembourg or Irish supplier is Maltese VAT (OSS), whatever country was read
    vend = vendors.get(rec.get('vendor')) or {}
    # The buyer VAT number counts only if it is the owner's own (decisions.yml vat_number), never the supplier's printed twice
    buyer = norm_no(rec.get('buyer_vat'))
    buyer_ok = bool(buyer) and buyer != norm_no(rec.get('supplier_vat')) and (not rules.get('vat_number') or buyer == rules['vat_number'])
    business = buyer_ok or bool(sup.get('business')) or vend.get('deductible') is True
    by_id = rules['documents'].get(norm_no(key))
    by_no = rules['documents'].get(norm_no(rec.get('invoice_number')))
    by_id, by_no = (None if d and d.get('treatment') in SALE_TREATMENTS else d for d in (by_id, by_no))
    if by_id and by_id.get('treatment'):
        t, reason = by_id['treatment'], f'owner decision: {by_id.get("note", "")}'
    elif rec.get('duplicate_of'):
        t, reason = 'duplicate', f'same document as {rec["duplicate_of"]}'
    elif by_no and by_no.get('treatment'):
        t, reason = by_no['treatment'], f'owner decision: {by_no.get("note", "")}'
    elif not dt:
        t, reason = ('pre_registration', 'dated before the VAT registration') if day < rules['registration_date'] else \
                    ('needs_review', 'document facts not read yet (run with the AI configured)')
    elif dt in ('statement', 'fee_schedule', 'other'):
        t, reason = 'not_a_purchase', f'{dt}, not an invoice'
    elif gross is None:
        t, reason = 'needs_review', 'no amount could be read'
    elif gross == 0:
        t, reason = 'not_a_purchase', 'zero amount'
    elif day < rules['registration_date']:
        t, reason = 'pre_registration', 'dated before the VAT registration'
    elif dt in ('quotation', 'order_confirmation', 'payment_receipt'):
        t, reason = 'no_tax_invoice', f'{dt.replace("_", " ")}: only a tax invoice supports input VAT'
    elif sup.get('treatment'):
        t, reason = sup['treatment'], sup.get('reason') or 'supplier rule'
        if sup.get('review'):
            why.append(sup['review'])
    elif vend.get('deductible') is False and not sup.get('business'):
        t, reason = 'personal', f'supplier judged not a business cost: {vend.get("reason", "")}'
    elif has_vat and vc and vc != home:
        t, reason = 'foreign_vat', f'{vc} VAT, not deductible in Malta'
    elif has_vat and sc and sc != home and (vc == home or rate == std):
        t, reason = ('oss_risk', f'Maltese VAT charged by a {sc} supplier (OSS, consumer-style invoice)') if business else \
                    ('needs_review', f'Maltese VAT charged by a {sc} supplier (OSS); business use not established')
        risk.append('OSS: deductibility doubtful')
    elif has_vat and sc and sc != home:
        t, reason = 'foreign_vat', f'VAT at {rate or "?"}% charged by a {sc} supplier, not Maltese VAT'
    elif rec.get('category') in BLOCKED_CATEGORIES:
        t, reason = 'blocked', f'{rec["category"]}: blocked category (confirm with the accountant)'
    elif not has_vat and sc and sc != home:
        zone = 'reverse_charge_eu' if sc in EU else 'reverse_charge_non_eu'
        if rec.get('supply') == 'goods':
            t, reason = 'needs_review', f'goods from {sc} without VAT: acquisition or import, not a service reverse charge'
        elif not business:
            t, reason = 'needs_review', f'{zone} candidate ({sc} supplier, no VAT charged): no buyer VAT number on it, business use?'
        else:
            t, reason = zone, f'service from a {sc} supplier without VAT: reverse charge at {std}%'
            if dt not in TAX_DOCS:
                why.append(f'reverse charge on a {dt.replace("_", " ")}, not a tax invoice')
    elif not has_vat:
        t, reason = ('exempt_or_zero', 'no VAT charged') if sc == home or vat == 0 else ('needs_review', 'no VAT shown and supplier country unknown')
    elif not sc:
        t, reason = 'needs_review', 'VAT charged but the supplier country is unknown'
    elif dt == 'fiscal_receipt':
        t, reason = 'no_tax_invoice', 'fiscal receipt: does not support an input VAT claim'
    elif not business:
        t, reason = 'needs_review', 'Maltese VAT invoice, business use not established (add the supplier to decisions.yml)'
    else:
        t, reason = 'domestic_input', f'Maltese {rate or std:g}% tax invoice'
        if not buyer_ok:
            risk.append('buyer VAT number not on the invoice')
        if dt == 'simplified_invoice' and (gross_eur or 0) > 100:
            risk.append('simplified invoice above EUR 100')

    claimed = t in CLAIM or t in RC
    if claimed:
        if not (buyer_ok or sup.get('business')):
            risk.append('business use assumed from the supplier type (AI guess)')
        if sup.get('mixed_use'):
            risk.append('mixed use (home + office): claimed at 100%')
        if net_eur is None:
            why.append('no EUR value')
        if not rec.get('files'):
            why.append('no file saved for this document')
        elif all(f.lower().endswith(('.jpg', '.jpeg', '.png')) for f in rec['files']):
            risk.append('read from a photo: check the amounts against the image')
    if t == 'needs_review':
        why.insert(0, reason)
    if (num(rec.get('confidence')) or 0) < rules.get('min_confidence', MIN_CONFIDENCE) and t not in ('duplicate', 'pre_registration'):
        why.append(f'confidence {rec.get("confidence")}')
    if rec.get('possible_duplicate_of') and t != 'duplicate':
        why.append(f'same amount within 3 days as {rec["possible_duplicate_of"]}: same purchase?')
    if rec.get('needs_review') and rec.get('vat_reason') and not rec.get('facts_v'):
        why.append(rec['vat_reason'])
    if claimed:
        why += risk
    accepted = next((d for d in (by_id, by_no) if d and d.get('accepted')), None) if t != 'duplicate' else None
    if accepted:  # the owner has looked at this row: it leaves the review queue
        why, reason = [], f'{reason} (checked by the owner{": " + accepted["note"] if accepted.get("note") else ""})'
    y, m = (day[:4], int(day[5:7])) if valid_date(day) else ('', 1)
    return {'doc_id': key, 'direction': 'purchase', 'supplier': rec.get('vendor') or '', 'supplier_vat': rec.get('supplier_vat') or '', 'vies_status': '',
            'doc_type': dt or '', 'invoice_no': rec.get('invoice_number') or '', 'invoice_date': day, 'period': f'{y}-Q{(m - 1) // 3 + 1}' if y else '',
            'currency': cur or '', 'net': net, 'vat': vat, 'gross': gross, 'fx_rate': fx_rate, 'fx_source': fx_source,
            'net_eur': net_eur, 'vat_eur': vat_eur, 'gross_eur': gross_eur,
            'rc_vat_eur': r2(net_eur * std / 100) if t in RC and net_eur is not None else None, 'rate_pct': rate,
            'treatment': t, 'reason': reason, 'risk': '; '.join(risk), 'confidence': rec.get('confidence'),
            'needs_review': bool(why), 'review_reason': '; '.join(dict.fromkeys(why)), 'duplicate_of': rec.get('duplicate_of') or '',
            'bucket': TREATMENT_BUCKET.get(t, 'no_amount' if gross is None else 'noVAT'), 'category': rec.get('category') or '',
            'source': rec.get('source') or 'email', 'source_file': ' | '.join(rec.get('files') or []), 'page': '',
            'gmail_link': rec.get('gmail_link') or '', 'payment': '', '_rec': rec, '_country': sc, '_buyer_ok': buyer_ok, '_accepted': bool(accepted)}


def build_table(docs: list[dict], keys: dict, rules: dict, vendors: dict, out: Path) -> list[dict]:
    """The table for a set of ledger records, plus VIES status for the suppliers behind claimed rows.
    Writes treatment, bucket, needs_review and review_reason back to the records (the ledger mirrors the table)."""
    fx = FxRates(out)
    rows = sorted((build_row(keys.get(id(r), '?'), r, rules, vendors, fx) for r in docs), key=lambda r: (r['invoice_date'], r['supplier'], r['doc_id']))
    vpath = out / '_cache' / 'vies.json'
    cache = json.loads(vpath.read_text()) if vpath.exists() else {}
    for row in rows:
        if row['treatment'] in CLAIM or row['treatment'] == 'reverse_charge_eu':
            row['vies_status'] = vies_check(row['supplier_vat'], row['_country'], cache)
            if not row['vies_status'].startswith('valid') and not row['_accepted']:
                row['review_reason'] = '; '.join(filter(None, [row['review_reason'], f'supplier VAT number: {row["vies_status"] or "none on the document"}']))
                row['needs_review'] = True
        for k in ('treatment', 'bucket', 'needs_review', 'review_reason'):
            row['_rec'][k] = row[k]
    if cache:
        vpath.parent.mkdir(parents=True, exist_ok=True)
        vpath.write_text(json.dumps(cache, indent=1))
    return rows


def return_boxes(rows: list[dict]) -> dict:
    """Malta VAT return figures in EUR. Box numbers follow the owner's Q3 2026 fill sheet: verify them against the portal form.
    VAT is summed per invoice, never recomputed on the total."""
    tot = lambda field, *ts: r2(sum(r[field] or 0 for r in rows if r['treatment'] in ts))
    b = {'3': tot('net_eur', 'reverse_charge_eu'), '6': tot('rc_vat_eur', 'reverse_charge_eu'),
         '4': tot('net_eur', 'reverse_charge_non_eu'), '7': tot('rc_vat_eur', 'reverse_charge_non_eu')}
    b.update({'9a': b['3'], '13a': b['6'], '11': b['4'], '15': b['7']})
    b['5'] = b['12'] = r2(b['3'] + b['4'])
    b['8'] = b['16'] = r2(b['6'] + b['7'])
    b['17'] = r2(b['8'] - b['16'])
    b['31'], b['37'] = tot('net_eur', *CLAIM), tot('vat_eur', *CLAIM)
    b['31_without_oss'], b['37_without_oss'] = tot('net_eur', 'domestic_input'), tot('vat_eur', 'domestic_input')
    for t, box in SALE_BOX.items():  # sales without output VAT: Boxes 1, 2, 20, 21
        b[box] = tot('net_eur', t)
    for rate, (net_box, vat_box) in SALE_RATE_BOX.items():  # taxable sales per rate
        mine = [r for r in rows if r['treatment'] == 'sale_domestic' and r['rate_pct'] == rate]
        b[net_box], b[vat_box] = r2(sum(r['net_eur'] or 0 for r in mine)), r2(sum(r['vat_eur'] or 0 for r in mine))
    b['22'] = r2(sum(b[k] for k in ('18', '18a', '18b', '19', '20', '21')))
    b['25'] = r2(sum(b[k] for k in ('23', '23a', '23b', '24')))
    b['26'] = r2(b['17'] + b['25'])
    net = r2(b['37'] - b['26'])  # input VAT less output VAT, before adjustments and credit brought forward
    b['42'], b['43'] = max(net, 0.0), max(-net, 0.0)
    return b


def table_checks(rows: list[dict], label: str = '') -> list[tuple[str, bool, str]]:
    """Hard checks that must pass before a worksheet is handed to the owner: (check, passed, detail)."""
    used = [r for r in rows if r['treatment'] in CLAIM or r['treatment'] in RC]
    b = return_boxes(rows)
    bad_sum = [r['doc_id'] for r in rows if None not in (r['net'], r['vat'], r['gross']) and abs(r['net'] + r['vat'] - r['gross']) > 0.011]
    no_eur = [r['doc_id'] for r in used if r['net_eur'] is None or (r['treatment'] in CLAIM and r['vat_eur'] is None)]
    ids = [(r['supplier'].lower().split()[0] if r['supplier'] else '', norm_no(r['invoice_no'])) for r in used if r['invoice_no']]
    twice = sorted({f'{s} {n}' for s, n in ids if ids.count((s, n)) > 1})
    outside = [r['doc_id'] for r in used if re.fullmatch(r'\d{4}-Q[1-4]', label) and r['period'] != label]
    nofile = [r['doc_id'] for r in used if not r['source_file']]
    sales = [r for r in rows if is_sale(r) and (r['treatment'] in SALE_BOX or r['treatment'] == 'sale_domestic')]
    sale_no_eur = [r['doc_id'] for r in sales if r['net_eur'] is None or (r['treatment'] == 'sale_domestic' and r['rate_pct'] not in SALE_RATE_BOX)]
    sale_nos = [norm_no(r['invoice_no']) for r in sales]
    sale_twice = sorted({n for n in sale_nos if n and sale_nos.count(n) > 1})
    return [('net + VAT = gross on every row', not bad_sum, ', '.join(bad_sum)),
            ('every claimed row has EUR values', not no_eur, ', '.join(no_eur)),
            ('no document counted twice', not twice, ', '.join(twice)),
            ('every claimed row is dated inside the period', not outside, ', '.join(outside)),
            ('every claimed row has its file', not nofile, ', '.join(nofile)),
            ('Box 4 = Box 11 and Box 7 = Box 15', b['4'] == b['11'] and b['7'] == b['15'], ''),
            ('Box 17 = 0.00 (reverse charge nets out)', b['17'] == 0, f'{b["17"]:.2f}'),
            ('every sale in the return has EUR values and a box', not sale_no_eur, ', '.join(sale_no_eur)),
            ('no sales invoice number is used twice', not sale_twice, ', '.join(sale_twice))]


def fmt(x) -> str:
    return '' if x is None else f'{x:,.2f}'


def write_worksheet(qdir: Path, rows: list[dict], unpaid: list[dict] | None = None, unbilled: list[dict] | None = None) -> None:
    """<label>_VAT_worksheet.md: box values, hard checks, the claimed rows per section, reconciliations and the review queue.
    A working paper for the owner to check. The tool never files or submits anything."""
    label = qdir.name
    b, checks = return_boxes(rows), table_checks(rows, label)
    sec = lambda *ts: [r for r in rows if r['treatment'] in ts]
    brought, bf_note = brought_forward(qdir.parent, label)
    f = return_form(rows, brought)
    line = lambda r: (f'| {r["invoice_date"]} | {r["supplier"][:32]} | {r["invoice_no"]} | {r["currency"]} {fmt(r["gross"])} | {fmt(r["net_eur"])} | '
                      f'{fmt(r["rc_vat_eur"] if r["treatment"] in RC else r["vat_eur"])} | {r["fx_source"]} {r["fx_rate"] if r["fx_source"] else ""} | {r["vies_status"]} | {r["risk"]} |')
    head = '| Date | Supplier | Invoice | Gross | Net EUR | VAT EUR | FX | VIES | Risk |\n|---|---|---|---|---|---|---|---|---|'
    out = [f'# {label} VAT worksheet', '', f'Generated {datetime.now():%Y-%m-%d %H:%M}. Working paper only: the owner checks every figure, decides every open item '
           'and files the return himself. Box numbers follow the Q3 2026 fill sheet; verify them against the portal form.', '',
           '## Boxes (EUR)', '', '| Box | Meaning | Value |', '|---|---|---|',
           f'| 3 and 9a | EU reverse-charge services, net | {fmt(b["3"])} |', f'| 6 and 13a | VAT on those | {fmt(b["6"])} |',
           f'| 4 and 11 | Non-EU reverse-charge services, net | {fmt(b["4"])} |', f'| 7 and 15 | VAT on those | {fmt(b["7"])} |',
           f'| 5 / 8 and 12 / 16 | Reverse-charge totals | {fmt(b["5"])} / {fmt(b["8"])} |',
           f'| 31 / 37 | Domestic purchases net / input VAT, with the OSS items | {fmt(b["31"])} / {fmt(b["37"])} |',
           f'| 31 / 37 | The same without the OSS items | {fmt(b["31_without_oss"])} / {fmt(b["37_without_oss"])} |',
           f'| 17 | Box 8 minus Box 16 | {fmt(b["17"])} |',
           f'| 1 | Sales to VAT-registered EU customers, the customer accounts for the VAT | {fmt(b["1"])} |', f'| 2 | Sales with the place of supply outside Malta | {fmt(b["2"])} |',
           f'| 18 / 23 | Taxable sales at 18%, net / VAT | {fmt(b["18"])} / {fmt(b["23"])} |',
           f'| 18a / 23a, 18b / 23b, 19 / 24 | Taxable sales at 7%, 12%, 5%, net / VAT | {fmt(b["18a"])} / {fmt(b["23a"])}, {fmt(b["18b"])} / {fmt(b["23b"])}, {fmt(b["19"])} / {fmt(b["24"])} |',
           f'| 20 / 21 | Exports and exempt with credit / exempt without credit | {fmt(b["20"])} / {fmt(b["21"])} |',
           f'| 22 / 25 | Sales sub-total / output VAT | {fmt(b["22"])} / {fmt(b["25"])} |', f'| 26 | Box 17 + Box 25 | {fmt(b["26"])} |',
           f'| 42 / 43 | Excess credit / tax due (with the OSS items, before credit brought forward) | {fmt(f["42"])} / {fmt(f["43"])} |',
           f'| 44 / 45 | Excess credit brought forward / tax payable | {fmt(f["44"])} / {fmt(f["45"])} |', ''] + ([bf_note, ''] if bf_note else []) + [
           f'Rounding: {rules_std_note(b)}', '', '## Hard checks', '']
    out += [f'- {"PASS" if ok else "**FAIL**"}: {name}{f" ({detail})" if detail and not ok else ""}' for name, ok, detail in checks]
    for title, ts in (('A. Domestic input VAT', ('domestic_input',)), ('B. Reverse-charge services', RC), ('C. Risk items: Maltese VAT charged through OSS', ('oss_risk',))):
        part = sec(*ts)
        vat_field = 'rc_vat_eur' if ts == RC else 'vat_eur'
        out += ['', f'## {title}', '', head] + [line(r) for r in part] + \
               [f'| | **Total ({len(part)} documents)** | | | **{fmt(r2(sum(r["net_eur"] or 0 for r in part)))}** | **{fmt(r2(sum(r[vat_field] or 0 for r in part)))}** | | | |']
    sales = [r for r in rows if is_sale(r)]
    if sales:
        box = lambda r: 'Box ' + (SALE_BOX.get(r['treatment']) or ' / '.join(SALE_RATE_BOX.get(r['rate_pct'], ('?',)))) if r['treatment'] in SALE_BOX or r['treatment'] == 'sale_domestic' else r['treatment'].replace('_', ' ')
        out += ['', f'## D. Sales ({len(sales)} invoices issued)', '', '| Date | Customer | Invoice | Gross | Net EUR | VAT EUR | Reported in | VIES | Payment |', '|---|---|---|---|---|---|---|---|---|']
        out += [f'| {r["invoice_date"]} | {r["supplier"][:32]} | {r["invoice_no"]} | {r["currency"]} {fmt(r["gross"])} | {fmt(r["net_eur"])} | {fmt(r["vat_eur"])} | {box(r)} | {r["vies_status"]} | {r["payment"]} |' for r in sales]
        recap, gaps = recap_statement(rows), sales_gaps(rows)
        if recap:
            out += ['', '### Recapitulative statement (EU customers behind Box 1)', '', '| Country | VAT number | Customer | Kind | Net EUR |', '|---|---|---|---|---|'] + [f'| {" | ".join(x)} |' for x in recap]
        if gaps:
            out += ['', f'**Invoice numbers missing from the series: {", ".join(gaps)}.** Add them to output/sales/, or they belong to another quarter.']
    if unbilled:
        out += ['', f'## Money in without a sales invoice ({len(unbilled)})', '', 'Payments received that matched no issued invoice. Add the invoice to output/sales/, or mark it as not a sale in the portal '
                '(refund, own transfer).', '', '| Date | Account | Amount | Description |', '|---|---|---|---|']
        out += [f'| {t["date"]} | {t["account"]} | {t["currency"]} {fmt(t["amount"])} | {t["text"][:70]} |' for t in unbilled]
    bolt = [r for r in rows if re.search(r'\bbolt\b', r['supplier'], re.I) and r['treatment'] != 'duplicate' and not is_sale(r)]
    if bolt:
        with_no = sum(r['_buyer_ok'] for r in bolt)
        out += ['', '## Bolt rides (reconciliation only, none is claimed until the owner marks the business rides)', '',
                f'{len(bolt)} rides, net {fmt(r2(sum(r["net_eur"] or 0 for r in bolt)))}, VAT {fmt(r2(sum(r["vat_eur"] or 0 for r in bolt)))}, '
                f'gross {fmt(r2(sum(r["gross_eur"] or 0 for r in bolt)))}; {sum((r["vat_eur"] or 0) > 0 for r in bolt)} with VAT, {with_no} show the buyer VAT number. '
                'Compare the count and sums with the Bolt Work Profile export. To claim a ride, add its invoice number to output/decisions.yml.']
    dups = [r for r in rows if r['treatment'] == 'duplicate']
    if any(r['payment'] for r in rows):
        used = sec(*CLAIM, *RC)
        out += ['', '## Bank reconciliation', '', f'{sum(r["payment"][:4].isdigit() for r in used)} of {len(used)} claimed documents have a payment in the statements.', '',
                '| Date | Supplier | Invoice | Gross | Payment |', '|---|---|---|---|---|']
        out += [f'| {r["invoice_date"]} | {r["supplier"][:32]} | {r["invoice_no"]} | {r["currency"]} {fmt(r["gross"])} | {r["payment"]} |' for r in used]
        out += ['', f'### Payments without a document ({len(unpaid or [])})', '', 'Money out that matched no document. Find the invoice, or add a pattern to `ignore_payments` in output/decisions.yml '
                '(transfers, rent, cash).', '', '| Date | Account | Amount | Description |', '|---|---|---|---|']
        out += [f'| {t["date"]} | {t["account"]} | {t["currency"]} {fmt(-t["amount"])} | {t["text"][:70]} |' for t in unpaid or []]
    out += ['', f'## Duplicates set aside ({len(dups)})', ''] + [f'- {r["invoice_date"]} {r["supplier"][:32]} {r["invoice_no"]} {r["currency"]} {fmt(r["gross"])} = {r["duplicate_of"]}' for r in dups]
    counts = defaultdict(lambda: [0, 0.0])
    for r in rows:
        counts[r['treatment']][0] += 1
        counts[r['treatment']][1] += r['gross_eur'] or 0
    out += ['', '## All documents by treatment', '', '| Treatment | Documents | Gross EUR |', '|---|---|---|'] + [f'| {t} | {n} | {fmt(g)} |' for t, (n, g) in sorted(counts.items())]
    queue = [r for r in rows if r['needs_review'] and r['treatment'] not in ('duplicate', 'pre_registration', 'not_a_purchase', 'not_a_sale')]
    out += ['', f'## Review queue ({len(queue)}): the owner decides, the tool does not', '', '| Date | Supplier | Invoice | Gross | Treatment | Why |', '|---|---|---|---|---|---|']
    out += [f'| {r["invoice_date"]} | {r["supplier"][:32]} | {r["invoice_no"]} | {r["currency"]} {fmt(r["gross"])} | {r["treatment"]} | {r["review_reason"]} |'
            for r in sorted(queue, key=lambda r: (r['treatment'] not in CLAIM + RC, r['treatment'], r['invoice_date']))]
    out += ['', 'Not covered by this tool: goods for re-sale, capital goods, purchases at reduced rates, adjustments, business purpose per taxi ride, mixed-use apportionment. '
            'Tax-impact questions go to the accountant first.', '']
    (qdir / f'{label}_VAT_worksheet.md').write_text('\n'.join(out))


def rules_std_note(b: dict) -> str:
    return (f'VAT is summed per invoice. 18% of Box 31 would be {fmt(r2(b["31"] * 0.18))}, the invoice sum is {fmt(b["37"])}; '
            'a gap of a cent or two is expected, a larger one means a row is not at 18% or is misread.')


def write_summary(qdir: Path, rows: list[dict], unpaid: list[dict] | None = None, statements: list[dict] | None = None, unbilled: list[dict] | None = None) -> None:
    """summary.csv (the whole table), summary_by_vendor.csv (EUR, per supplier and treatment) and the worksheet."""
    with open(qdir / 'summary.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, CSV_FIELDS, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    totals = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    for r in rows:
        t = totals[(r['supplier'], r['treatment'])]
        t[0] += 1
        for i, k in enumerate(('net_eur', 'rc_vat_eur' if r['treatment'] in RC else 'vat_eur', 'gross_eur'), 1):
            t[i] += r[k] or 0
    with open(qdir / 'summary_by_vendor.csv', 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['supplier', 'treatment', 'documents', 'net_eur', 'vat_eur', 'gross_eur'])
        for (supplier, treatment), (n, net, vat, gross) in sorted(totals.items()):
            w.writerow([supplier, treatment, n, f'{net:.2f}', f'{vat:.2f}', f'{gross:.2f}'])
    write_worksheet(qdir, rows, unpaid, unbilled)
    recap, rpath = recap_statement(rows), qdir / f'{qdir.name}_recapitulative_statement.csv'
    if recap:
        with open(rpath, 'w', newline='') as f:
            csv.writer(f).writerows([['country', 'customer_vat', 'customer', 'kind', 'net_eur']] + recap)
    else:
        rpath.unlink(missing_ok=True)
    (qdir / '.review.json').write_text(json.dumps({'rows': [{k: v for k, v in r.items() if not k.startswith('_')} for r in rows],
                                                   'unpaid': [{**t, 'id': payment_id(t)} for t in unpaid or []], 'unbilled': [{**t, 'id': payment_id(t)} for t in unbilled or []],
                                                   'statements': statements}))
    b = return_boxes(rows)
    logger.info('%d documents (%d need review) -> %s', len(rows), sum(r['needs_review'] for r in rows), qdir)
    logger.info('Domestic input: net %.2f, VAT %.2f (without OSS items: %.2f / %.2f). Reverse charge: net %.2f, VAT %.2f',
                b['31'], b['37'], b['31_without_oss'], b['37_without_oss'], b['5'], b['8'])
    if any(is_sale(r) for r in rows):
        logger.info('Sales: Box 1 %.2f, Box 2 %.2f, taxable net %.2f, output VAT %.2f. Excess credit %.2f, tax due %.2f', b['1'], b['2'], b['22'], b['25'], b['42'], b['43'])
    for name, ok, detail in table_checks(rows, qdir.name):
        if not ok:
            logger.warning('CHECK FAILED: %s %s', name, detail)


# ---------- bank statements: every document against a payment, every payment against a document ----------

BANK_DATE = ('completed date', 'booking date', 'transaction date', 'posting date', 'value date', 'started date', 'date')
BANK_TEXT = ('description', 'details', 'narrative', 'payee', 'counterparty', 'merchant', 'reference', 'transaction details', 'particulars')
BANK_OUT, BANK_IN = ('debit', 'paid out', 'money out', 'withdrawal', 'withdrawals'), ('credit', 'paid in', 'money in', 'deposit', 'deposits')
PAY_BEFORE, PAY_AFTER = 10, 45  # a payment may come this many days before (order, prepayment) or after (direct debit) the invoice date
FX_TOLERANCE = 0.04  # card payment in another currency: bank rate and fees against the ECB rate


def bank_amount(raw) -> float | None:
    raw = re.sub(r'[^\d.,()+-]', '', str(raw or ''))
    if not re.search(r'\d', raw):
        return None
    n = to_number(raw.strip('()+-'))
    return None if n is None else -n if raw.startswith(('-', '(')) or raw.endswith('-') else n


def read_statement(path: Path) -> list[dict]:
    """Transactions of one CSV export, whatever the bank: the columns are found by their header names (Revolut, and the
    usual date / description / amount or debit + credit layouts). Money out is negative. [] if the file is not a statement."""
    try:
        text = path.read_text(encoding='utf-8-sig', errors='replace')
        lines = text.splitlines()
        start = next(i for i, l in enumerate(lines[:40]) if 'date' in l.lower() and re.search(r'amount|debit|credit|paid|money', l, re.I))
        dialect = csv.Sniffer().sniff(lines[start], delimiters=',;\t')
        rd = csv.DictReader(lines[start:], dialect=dialect)
        cols = {str(c).strip().lower(): c for c in rd.fieldnames or [] if c}
    except (OSError, StopIteration, csv.Error):
        return []
    pick = lambda names: next((cols[n] for n in names if n in cols), None)
    c_date, c_amt, c_out, c_in, c_cur, c_state = pick(BANK_DATE), pick(('amount',)), pick(BANK_OUT), pick(BANK_IN), pick(('currency',)), pick(('state', 'status'))
    c_text = [cols[n] for n in BANK_TEXT if n in cols]
    if not c_date or not (c_amt or c_out):
        return []
    txs = []
    for row in rd:
        day = parse_date(str(row.get(c_date) or '').strip()[:10]) or parse_date(str(row.get(c_date) or '').strip())
        amount = bank_amount(row.get(c_amt)) if c_amt else None
        if amount is None and c_out:
            out_, in_ = bank_amount(row.get(c_out)), bank_amount(row.get(c_in)) if c_in else None
            amount = -abs(out_) if out_ else abs(in_) if in_ else None
        if not day or not amount or (c_state and str(row.get(c_state) or '').strip().upper() in ('REVERTED', 'DECLINED', 'FAILED', 'PENDING')):
            continue
        txs.append({'date': day.isoformat(), 'amount': amount, 'currency': str(row.get(c_cur) or 'EUR').strip().upper() if c_cur else 'EUR',
                    'text': ' '.join(str(row.get(c) or '').strip() for c in c_text), 'account': path.stem, 'used': False})
    return txs


def read_statements(folder: Path) -> list[dict]:
    """Every CSV under output/statements/ (one export per account, any number of files; overlapping exports are de-duplicated)."""
    if not folder.is_dir():
        return []
    seen, txs = set(), []
    for path in sorted(folder.rglob('*.csv')):
        got = read_statement(path)
        logger.info('Statement %s: %d transactions', path.name, len(got)) if got else logger.warning('Not a readable statement export: %s', path)
        for t in got:
            k = (t['date'], t['amount'], t['currency'], t['text'])
            if k not in seen:
                seen.add(k)
                txs.append(t)
    other = [f.name for f in folder.rglob('*') if f.is_file() and f.suffix.lower() in ('.pdf', '.xlsx', '.xls')]
    if other:
        logger.warning('Statements are read from CSV exports only; not read: %s', ', '.join(other[:5]))
    return txs


def match_payments(rows: list[dict], txs: list[dict], rules: dict | None = None) -> list[dict]:
    """Give every document its payment and return the outgoing payments left without a document.
    A payment matches when it is money out, within PAY_BEFORE / PAY_AFTER days of the invoice date, and either the same
    amount in the document's currency or (another currency) within FX_TOLERANCE of the EUR value. Among candidates the one
    naming the supplier wins, then the exact amount, then the nearest date; a payment is used once.
    'no payment found' is only said when the statements reach into the invoice's payment window; a claimed row without one goes to review."""
    if not txs:
        for r in rows:
            r['payment'] = ''
        return []
    lo, hi = min(t['date'] for t in txs), max(t['date'] for t in txs)
    days = lambda a, b: (date.fromisoformat(a) - date.fromisoformat(b)).days
    order = sorted((r for r in rows if r['treatment'] != 'duplicate' and (r['gross'] or 0) > 0 and valid_date(r['invoice_date'])),
                   key=lambda r: (r['treatment'] not in CLAIM + RC, r['invoice_date']))
    for r in rows:
        r['payment'] = ''
    links, by_id = (rules or {}).get('payment_links') or {}, {r['doc_id']: r for r in rows}
    for t in txs:  # the owner said which document a payment belongs to
        r = by_id.get(links.get(payment_id(t)))
        if r is not None and not t['used'] and not r['payment']:
            t['used'] = True
            r['payment'] = f'{t["date"]} {t["account"]} {t["currency"]} {abs(t["amount"]):.2f} (linked by you)'
    for r in order:
        if r['payment']:
            continue
        words = [w for w in re.findall(r'[a-z]{4,}', r['supplier'].lower()) if w not in ('limited', 'malta', 'noreply', 'billing', 'online', 'services', 'receipts')][:3]
        best = None
        for t in txs:
            if t['used'] or (t['amount'] > 0) != is_sale(r):  # a purchase is paid with money out, a sale with money in
                continue
            gap = days(t['date'], r['invoice_date'])
            if not -PAY_BEFORE <= gap <= (SALE_PAY_AFTER if is_sale(r) else PAY_AFTER):
                continue
            paid = abs(t['amount'])
            exact = t['currency'] == r['currency'] and abs(paid - r['gross']) <= 0.011
            near = t['currency'] != r['currency'] and t['currency'] == 'EUR' and r['gross_eur'] and abs(paid - r['gross_eur']) <= FX_TOLERANCE * r['gross_eur']
            if not (exact or near):
                continue
            named = any(w in t['text'].lower() for w in words)
            score = (named, exact, -abs(gap))
            if best is None or score > best[0]:
                best = (score, t, exact, named)
        if best:
            _, t, exact, named = best
            t['used'] = True
            r['payment'] = f'{t["date"]} {t["account"]} {t["currency"]} {abs(t["amount"]):.2f}' + ('' if exact else ' (other currency)') + ('' if named else ' (amount only)')
        elif hi >= r['invoice_date'] and days(lo, r['invoice_date']) <= PAY_AFTER:  # the statements reach into its payment window
            r['payment'] = 'no payment found' + (' (not paid yet?)' if is_sale(r) else '')
            if r['treatment'] in CLAIM + RC and not r.get('_accepted'):
                r['review_reason'] = '; '.join(filter(None, [r['review_reason'], 'no payment found in the statements: cash or another card?']))
                r['needs_review'] = True
        else:
            r['payment'] = 'no statement for this date'
    ignore = [re.compile(x, re.I) for x in (rules or {}).get('ignore_payments') or []]
    done = (rules or {}).get('ignored_payments') or set()
    return sorted((t for t in txs if not t['used'] and t['amount'] < 0 and not any(x.search(t['text']) for x in ignore) and payment_id(t) not in done), key=lambda t: t['amount'])


def payment_id(t: dict) -> str:
    return f'{t["date"]}|{t["amount"]:.2f}|{t["currency"]}|{t["text"][:60]}'


def statement_stats(folder: Path, txs: list[dict], unpaid: list[dict]) -> list[dict]:
    """Per uploaded export, after matching: what was read from it and how much of it found a document."""
    stats = []
    for path in sorted(folder.rglob('*.csv')) if folder.is_dir() else []:
        mine = [t for t in txs if t['account'] == path.stem]
        stats.append({'file': path.name, 'transactions': len(mine), 'from': min((t['date'] for t in mine), default=''), 'to': max((t['date'] for t in mine), default=''),
                      'matched': sum(t['used'] for t in mine), 'unmatched': sum(t['account'] == path.stem for t in unpaid)})
    return stats


# ---------- sales: the invoices the owner issued (output/sales/: a CSV list, PDFs or photos) ----------

# Malta VAT FORM 003/2011: where a sale is reported. Net box, VAT box (None = no output VAT).
SALE_BOX = {'sale_eu_reverse_charge': '1', 'sale_outside_malta': '2', 'sale_export': '20', 'sale_exempt': '21'}
SALE_RATE_BOX = {18: ('18', '23'), 7: ('18a', '23a'), 12: ('18b', '23b'), 5: ('19', '24')}
SALE_TREATMENTS = ('sale_domestic', 'sale_eu_reverse_charge', 'sale_outside_malta', 'sale_export', 'sale_exempt', 'not_a_sale')
SALE_PAY_AFTER = 90  # a customer may pay this many days after the invoice date
SALE_COLS = {'invoice_number': ('invoice number', 'invoice no', 'invoice no.', 'invoice #', 'invoice', 'number', 'no', 'invoice_no', 'invoice_number'),
             'date': ('invoice date', 'date', 'issue date', 'issued', 'invoice_date'),
             'vendor': ('customer', 'client', 'customer name', 'client name', 'name', 'bill to'),
             'customer_country': ('country', 'customer country', 'client country', 'customer_country'),
             'customer_vat': ('customer vat', 'customer vat number', 'vat number', 'vat no', 'vat no.', 'vat id', 'customer_vat'),
             'net_amount': ('net', 'net amount', 'subtotal', 'amount excl vat', 'amount excl. vat', 'amount'),
             'vat_amount': ('vat', 'vat amount', 'tax', 'tax amount'), 'total_amount': ('gross', 'total', 'total amount', 'amount incl vat', 'amount incl. vat'),
             'vat_rate': ('vat rate', 'rate', 'vat %', 'vat_rate'), 'currency': ('currency',), 'supply': ('supply', 'type', 'kind'),
             'treatment': ('treatment',)}
SALES_PROMPT = """You read one sales invoice or credit note that a sole trader (the seller) issued to a customer.
Report only what the document shows. Reply with ONLY a JSON object:
{"is_invoice": bool, "customer": str, "customer_country": "ISO-2 or null", "customer_vat": str|null,
 "date": "YYYY-MM-DD or null", "invoice_number": str|null, "currency": "ISO code or null",
 "total_amount": number|null, "net_amount": number|null, "vat_amount": number|null, "vat_rate": number|null,
 "reverse_charge": bool, "supply": "services|goods|mixed|null", "confidence": 0..1}
- customer: the party billed (bill-to block), never the seller. customer_vat: the customer's VAT number as printed.
- total_amount = gross including VAT, net_amount = excluding VAT, vat_amount = VAT charged (0 if none). A credit note has negative amounts.
- is_invoice: false for quotations, pro-forma invoices, statements and anything that is not an issued invoice or credit note.

--- Document text ---
"""
is_sale = lambda r: r.get('direction') == 'sale'


def read_sales_csv(path: Path) -> list[dict]:
    """The owner's own list of issued invoices (an export of the invoicing tool, or typed by hand): one record per row.
    Columns are found by header name (SALE_COLS). [] if the file is not such a list."""
    try:
        lines = path.read_text(encoding='utf-8-sig', errors='replace').splitlines()
        start = next(i for i, l in enumerate(lines[:20]) if 'date' in l.lower() and re.search(r'invoice|number|customer|client', l, re.I))
        rd = csv.DictReader(lines[start:], dialect=csv.Sniffer().sniff(lines[start], delimiters=',;\t'))
        cols = {str(c).strip().lower(): c for c in rd.fieldnames or [] if c}
    except (OSError, StopIteration, csv.Error):
        return []
    pick = {k: next((cols[n] for n in names if n in cols), None) for k, names in SALE_COLS.items()}
    if not pick['date'] or not pick['invoice_number'] or not (pick['net_amount'] or pick['total_amount']):
        return []
    recs = []
    for row in rd:
        get = lambda k: str(row.get(pick[k]) or '').strip() if pick[k] else ''
        day = parse_date(get('date'))
        amount = lambda k: bank_amount(get(k).rstrip('%'))
        if not day or not get('invoice_number') or (amount('net_amount') is None and amount('total_amount') is None):
            continue
        recs.append({'vendor': get('vendor'), 'date': day.isoformat(), 'invoice_number': get('invoice_number'), 'currency': get('currency').upper() or 'EUR',
                     'total_amount': amount('total_amount'), 'net_amount': amount('net_amount'), 'vat_amount': amount('vat_amount'), 'vat_rate': amount('vat_rate'),
                     'customer_country': get('customer_country').upper()[:2], 'customer_vat': get('customer_vat'), 'supply': get('supply').lower() or 'services',
                     'treatment_hint': get('treatment').lower(), 'confidence': 1.0, 'files': [], 'source': 'sales_csv'})
    return recs


def read_sale_file(path: Path, client) -> dict | None:
    """One issued invoice as a PDF or photo: regex pass, then the AI when it is configured (the customer is only read by the AI)."""
    ext, data = manual_bytes(path)
    text = pdf_text(data) if ext == '.pdf' else ''
    found = parse_fields(text)
    rec = {'vendor': '', 'date': find_invoice_date(text), 'invoice_number': found.get('invoice_number'), 'currency': found.get('currency'),
           'total_amount': found.get('total_amount'), 'vat_amount': found.get('vat_amount'), 'confidence': 0.4}
    if client:
        try:
            binary = unreadable_file([(path.name, ext, data)], text)
            content = [{'type': 'text', 'text': SALES_PROMPT + (text[:8000] or '(attached file)')}]
            if binary:
                content.insert(0, {'type': MEDIA[ext][0], 'source': {'type': 'base64', 'media_type': MEDIA[ext][1], 'data': base64.b64encode(data).decode()}})
            got = ask_json(client, '', 600, content)
            if not got.get('is_invoice', True):
                return None
            rec = {**got, 'vendor': got.get('customer') or '', 'ai': True}
        except Exception as e:
            logger.warning('The AI could not read sales file %s, using regex: %s', path, e)
    rec.update(source='sales', files=[], date=valid_date(rec.get('date')) or datetime.fromtimestamp(path.stat().st_mtime).date().isoformat())
    return rec


def sale_row(key: str, rec: dict, rules: dict, fx) -> dict:
    """One table row for an issued invoice: checked amounts, EUR values and the box it is reported in (first matching rule).
    As with purchases, nothing here is a tax decision: anything unclear goes to needs_review."""
    why = []
    home, std = rules.get('home_country', 'MT'), rules.get('standard_rate', 18)
    gross, net, vat, rate = (num(rec.get(k)) for k in ('total_amount', 'net_amount', 'vat_amount', 'vat_rate'))
    cur, day = str(rec.get('currency') or '').upper() or None, rec.get('date') or ''
    if gross is None and net is not None:
        vat = vat if vat is not None else r2(net * rate / 100) if rate else 0.0
        gross = r2(net + vat)
    if gross is not None:
        if vat is None:
            vat = r2(gross - net) if net is not None else r2(gross * rate / (100 + rate)) if rate else None
        if net is None and vat is not None:
            net = r2(gross - vat)
        if None not in (net, vat) and abs(net + vat - gross) > 0.02:
            why.append(f'net {net:.2f} + VAT {vat:.2f} != gross {gross:.2f}')
    if rate is None and net and vat:
        rate = next((k for k in (rules.get('vat_rates') or {}).get(home) or [std] if abs(vat / net * 100 - k) < 0.3), None)
    has_vat = bool(vat)
    if has_vat and rate and net and abs(net * rate / 100 - vat) > 0.03:
        why.append(f'VAT {vat:.2f} is not {rate:g}% of the net {net:.2f}')
    fx_rate = fx_source = net_eur = vat_eur = gross_eur = None
    if gross is not None and cur == 'EUR':
        fx_rate, fx_source, net_eur, vat_eur, gross_eur = 1.0, '', net, vat, gross
    elif gross is not None and cur:
        ecb, ecb_day = fx.rate(cur, day)
        if ecb:
            fx_rate, fx_source = ecb, f'ECB {ecb_day}'
            net_eur, vat_eur, gross_eur = (r2(x / ecb) if x is not None else None for x in (net, vat, gross))
        else:
            fx_source = f'no ECB rate for {cur}'

    cc = str(rec.get('customer_country') or '').upper()[:2] or None
    cvat = re.sub(r'[^A-Za-z0-9]', '', str(rec.get('customer_vat') or '')).upper()
    if not cc and re.match(r'[A-Z]{2}\d', cvat):
        cc = {'EL': 'GR'}.get(cvat[:2], cvat[:2])
    goods = rec.get('supply') == 'goods'
    by = next((d for d in (rules['documents'].get(norm_no(key)), rules['documents'].get(norm_no(rec.get('invoice_number')))) if d), None)
    hint = rec.get('treatment_hint') if rec.get('treatment_hint') in SALE_TREATMENTS else None
    if by and by.get('treatment') in SALE_TREATMENTS:
        t, reason = by['treatment'], f'owner decision: {by.get("note", "")}'
    elif gross is None:
        t, reason = 'needs_review', 'no amount could be read'
    elif gross == 0:
        t, reason = 'not_a_sale', 'zero amount'
    elif day < rules['registration_date']:
        t, reason = 'pre_registration', 'dated before the VAT registration'
    elif hint:
        t, reason = hint, 'treatment given in the sales list'
    elif has_vat:
        t, reason = ('sale_domestic', f'{rate:g}% VAT charged') if rate in SALE_RATE_BOX else ('needs_review', f'VAT charged at {rate or "an unknown"}%, not a rate of the return form')
    elif not cc:
        t, reason = 'needs_review', 'no VAT charged and the customer country is unknown'
    elif cc == home:
        t, reason = 'needs_review', 'no VAT charged to a Maltese customer: exempt supply (Box 21), or is the VAT missing?'
    elif cc in EU and cvat:
        t, reason = 'sale_eu_reverse_charge', f'{"goods" if goods else "services"} to a VAT-registered {cc} customer without VAT: the customer accounts for it'
    elif cc in EU:
        t, reason = 'needs_review', f'no VAT charged to a customer in {cc} without a VAT number: Maltese VAT (or OSS) normally applies to consumers'
    else:
        t, reason = ('sale_export', f'goods to {cc}, outside the EU') if goods else ('sale_outside_malta', f'services to a {cc} customer, outside the EU')
    if t == 'needs_review':
        why.insert(0, reason)
    if t in SALE_BOX or t == 'sale_domestic':
        if t == 'sale_domestic' and rate not in SALE_RATE_BOX:
            why.append('taxable sale without a VAT rate of the form: it is in no box until the VAT is on the invoice')
        if net_eur is None:
            why.append('no EUR value')
        if not rec.get('vendor'):
            why.append('customer name not read')
        if t == 'sale_eu_reverse_charge' and not cvat:
            why.append('customer VAT number missing: needed for the recapitulative statement')
    if (num(rec.get('confidence')) or 0) < rules.get('min_confidence', MIN_CONFIDENCE) and t != 'pre_registration':
        why.append(f'confidence {rec.get("confidence")}')
    accepted = bool(by and by.get('accepted'))
    if accepted:
        why, reason = [], f'{reason} (checked by the owner{": " + by["note"] if by.get("note") else ""})'
    y, m = (day[:4], int(day[5:7])) if valid_date(day) else ('', 1)
    return {'doc_id': key, 'direction': 'sale', 'supplier': rec.get('vendor') or '', 'supplier_vat': cvat, 'vies_status': '', 'doc_type': 'sales_invoice',
            'invoice_no': rec.get('invoice_number') or '', 'invoice_date': day, 'period': f'{y}-Q{(m - 1) // 3 + 1}' if y else '', 'currency': cur or '',
            'net': net, 'vat': vat, 'gross': gross, 'fx_rate': fx_rate, 'fx_source': fx_source, 'net_eur': net_eur, 'vat_eur': vat_eur, 'gross_eur': gross_eur,
            'rc_vat_eur': None, 'rate_pct': rate, 'treatment': t, 'reason': reason, 'risk': '', 'confidence': rec.get('confidence'),
            'needs_review': bool(why), 'review_reason': '; '.join(dict.fromkeys(why)), 'duplicate_of': '', 'bucket': 'sales', 'category': '',
            'source': rec.get('source') or 'sales', 'source_file': ' | '.join(rec.get('files') or []), 'page': '', 'gmail_link': '', 'payment': '',
            '_rec': rec, '_country': cc, '_buyer_ok': False, '_accepted': accepted, '_goods': goods}


def build_sales(ledger: dict, out: Path, qdir: Path, period, client, rules: dict) -> list[dict]:
    """Rows for every issued invoice under output/sales/ dated in the period. A CSV list gives exact figures; a PDF or photo
    is read once (ledger key sale:<path>) and copied to <quarter>/sales/. The same invoice number in both: the list's
    figures, the file attached. Customer VAT numbers behind Box 1 are checked in VIES."""
    folder = out / 'sales'
    if not folder.is_dir():
        return []
    recs, by_no = {}, {}
    for path in sorted(folder.rglob('*.csv')):
        got = read_sales_csv(path)
        logger.info('Sales list %s: %d invoices', path.name, len(got)) if got else logger.warning('Not a readable sales list: %s', path)
        for rec in got:
            key = f'sale:{path.relative_to(folder)}#{rec["invoice_number"]}'
            if norm_no(rec['invoice_number']) in by_no:
                logger.warning('Sales invoice %s is listed twice, second one ignored (%s)', rec['invoice_number'], path.name)
                continue
            recs[key] = by_no[norm_no(rec['invoice_number'])] = rec
    for path in sorted(folder.rglob('*')):
        if not path.is_file() or path.suffix.lower() not in MANUAL_EXT or path.name.startswith('.'):
            continue
        key = f'sale:{path.relative_to(folder)}'
        rec = ledger.get(key)
        if rec is None:
            try:
                rec = read_sale_file(path, client)
            except Exception as e:
                logger.warning('Sales file could not be read, skipped: %s (%s)', path, e)
                continue
            if rec is None:
                logger.info('Sales file is not an issued invoice, skipped: %s', path)
                continue
            ledger[key] = rec
        listed = by_no.get(norm_no(rec.get('invoice_number'))) if rec.get('invoice_number') else None
        target = listed or rec
        if period and not period[0].isoformat() <= target['date'] <= period[1].isoformat():
            continue
        ext, data = manual_bytes(path)
        dest = qdir / 'sales' / (slug(path.stem, 80) + ext)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        target['files'] = sorted(set((target.get('files') or []) + [str(dest.relative_to(qdir))]))
        if not listed:
            recs[key] = rec
    fx, vpath = FxRates(out), out / '_cache' / 'vies.json'
    cache = json.loads(vpath.read_text()) if vpath.exists() else {}
    rows = [sale_row(k, r, rules, fx) for k, r in recs.items() if not period or period[0].isoformat() <= r['date'] <= period[1].isoformat()]
    for row in rows:
        if row['treatment'] == 'sale_eu_reverse_charge' and row['supplier_vat']:
            row['vies_status'] = vies_check(row['supplier_vat'], row['_country'], cache)
            if not row['vies_status'].startswith('valid') and not row['_accepted']:
                row['review_reason'] = '; '.join(filter(None, [row['review_reason'], f'customer VAT number: {row["vies_status"]}']))
                row['needs_review'] = True
    if cache:
        vpath.parent.mkdir(parents=True, exist_ok=True)
        vpath.write_text(json.dumps(cache, indent=1))
    if rows:
        logger.info('Sales: %d invoices, net EUR %.2f, output VAT %.2f', len(rows), sum(r['net_eur'] or 0 for r in rows), sum(r['vat_eur'] or 0 for r in rows if r['treatment'] == 'sale_domestic'))
    return sorted(rows, key=lambda r: (r['invoice_date'], r['invoice_no']))


def sales_gaps(rows: list[dict]) -> list[str]:
    """Invoice numbers missing from the quarter's sales when they run in one numbered series (INV-0007, INV-0009: 0008 is missing)."""
    parts = [re.fullmatch(r'(.*?)(\d+)', str(r['invoice_no'])) for r in rows if is_sale(r)]
    if len(parts) < 2 or not all(parts) or len({(m[1], len(m[2])) for m in parts}) != 1:
        return []
    nums = sorted(int(m[2]) for m in parts)
    width, prefix = len(parts[0][2]), parts[0][1]
    return [f'{prefix}{n:0{width}d}' for n in range(nums[0], nums[-1]) if n not in nums][:20]


def recap_statement(rows: list[dict]) -> list[list]:
    """Recapitulative statement lines: one per VAT-registered EU customer behind Box 1 (net EUR, goods or services)."""
    tot = defaultdict(float)
    for r in rows:
        if r['treatment'] == 'sale_eu_reverse_charge':
            tot[(r['_country'] if '_country' in r else r['supplier_vat'][:2], r['supplier_vat'], r['supplier'], 'goods' if r.get('_goods') else 'services')] += r['net_eur'] or 0
    return [[c, v, name, kind, f'{r2(x):.2f}'] for (c, v, name, kind), x in sorted(tot.items(), key=lambda kv: [str(x) for x in kv[0]])]


def unmatched_income(txs: list[dict], rules: dict | None = None) -> list[dict]:
    """Money in that matched no issued invoice (call after match_payments). Refunds and own transfers are answered in the portal."""
    ignore = [re.compile(x, re.I) for x in (rules or {}).get('ignore_payments') or []]
    done = (rules or {}).get('ignored_payments') or set()
    return sorted((t for t in txs if not t['used'] and t['amount'] > 0 and not any(x.search(t['text']) for x in ignore) and payment_id(t) not in done), key=lambda t: -t['amount'])


# ---------- manual documents (output/expenses/: invoices that never arrive by email) ----------

MANUAL_EXT = {'.pdf', '.png', '.jpg', '.jpeg', '.heic', '.heif'}
PHOTO_MAX_BYTES, PHOTO_MAX_PX = 1_500_000, 2200
NO_HINT_FOLDERS = ('receipts', 'photos', 'scans', 'misc', 'other')  # folder names that are not a supplier


def manual_bytes(path: Path) -> tuple[str, bytes]:
    """(extension, bytes) of a hand-collected file as it is read and filed. PDFs as they are. Phone photos are made
    readable: HEIC converted to JPEG with macOS `sips`, large images turned upright and scaled to PHOTO_MAX_PX.
    The original is never changed."""
    ext = path.suffix.lower()
    if ext in ('.heic', '.heif'):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / 'photo.jpg'
            subprocess.run(['sips', '-s', 'format', 'jpeg', str(path), '--out', str(tmp)], capture_output=True, timeout=60, check=True)
            data, ext = tmp.read_bytes(), '.jpg'
    else:
        data = path.read_bytes()
    if ext != '.pdf' and len(data) > PHOTO_MAX_BYTES:
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(BytesIO(data))).convert('RGB')
        im.thumbnail((PHOTO_MAX_PX, PHOTO_MAX_PX))
        buf = BytesIO()
        im.save(buf, 'JPEG', quality=85)
        data, ext = buf.getvalue(), '.jpg'
    return ext, data


def extract_manual(path: Path, expenses: Path, client) -> dict | None:
    """Read one hand-collected file. The folder name is the vendor hint; the AI reads it (a few dozen files, cents)."""
    ext, data = manual_bytes(path)
    att_text = pdf_text(data) if ext == '.pdf' else ''
    hint = path.parent.name if path.parent != expenses and path.parent.name.lower() not in NO_HINT_FOLDERS else ''
    sent = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
    meta = {'sender': hint, 'subject': path.stem, 'sent': sent}
    info = extract_heuristic(meta, '', att_text, True)
    if client:
        try:
            info = extract_ai(client, meta, '', att_text, unreadable_file([(path.name, ext, data)], att_text))
            info['ai'], info['vat_checked'] = True, True
        except Exception as e:
            logger.warning('The AI could not read manual file %s, using regex: %s', path, e)
    if not info.get('is_financial_document', True):
        return None
    info.update(is_financial_document=True, source='manual', account='manual', subject=path.stem, links=[],
                orig=str(path.relative_to(expenses)), date=valid_date(info.get('date')) or sent)
    info['needs_review'] = bool(info.get('ai')) and (info.get('confidence', 0) < MIN_CONFIDENCE or info.get('total_amount') is None)
    return info


def import_manual(ledger: dict, out: Path, qdir: Path, label: str, period, client, expenses: Path) -> list[dict]:
    """Add every file under expenses/ that is dated inside the period to the scope: copied (originals are never touched),
    read once (ledger key manual:<relative path>), then triaged and de-duplicated with the email documents."""
    if not expenses.is_dir():
        return []
    recs = []
    for path in sorted(expenses.rglob('*')):
        if not path.is_file() or path.suffix.lower() not in MANUAL_EXT or path.name.startswith('.'):
            continue
        key = f'manual:{path.relative_to(expenses)}'
        rec = ledger.get(key)
        if rec is None:
            try:
                rec = extract_manual(path, expenses, client)
            except Exception as e:  # one unreadable photo must not stop the run
                logger.warning('Manual file could not be read, skipped: %s (%s)', path, e)
                continue
            if rec is None:
                logger.info('Manual file is not an invoice, skipped: %s', path)
                continue
            ledger[key] = rec
            logger.info('Manual: %s | %s | %s', rec.get('vendor'), rec['date'], path.relative_to(expenses))
        if period and not period[0].isoformat() <= rec['date'] <= period[1].isoformat():
            continue
        if not (rec.get('folder') == label and rec.get('files') and all((out / label / f).exists() for f in rec['files'])):
            total = rec.get('total_amount')
            name = '_'.join([rec['date'], slug(rec.get('vendor')), slug(rec.get('invoice_number') or path.stem),
                             f'{total:.2f}{rec.get("currency") or ""}' if isinstance(total, (int, float)) else 'noamount'])
            ext, data = manual_bytes(path)
            dest = unique(qdir / vendor_folder(rec.get('vendor'), qdir) / (name + ext))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            rec['files'], rec['folder'] = [str(dest.relative_to(qdir))], label
        recs.append(rec)
    logger.info('Manual documents in scope: %d (from %s)', len(recs), expenses)
    return recs


# ---------- final packets: one PDF for the claimable documents, one for noVAT ----------

def as_pdf(path: Path) -> bytes | None:
    """PDF bytes for a saved file: PDFs as they are, images wrapped in a PDF page; anything else cannot be merged."""
    ext = path.suffix.lower()
    try:
        if ext == '.pdf':
            return path.read_bytes()
        if ext in ('.png', '.jpg', '.jpeg'):
            from PIL import Image
            buf = BytesIO()
            Image.open(path).convert('RGB').save(buf, 'PDF')
            return buf.getvalue()
    except Exception as e:
        logger.warning('Cannot read %s for the packet: %s', path, e)
    return None


def render_index(chrome: str, title: str, sections: list[tuple[str, list[dict]]], offset: int, dest: Path) -> int:
    """Index page(s) for a packet: one table per section with EUR totals, printed by Chrome. Returns the page count."""
    from html import escape
    html = (f'<!doctype html><meta charset=utf-8><style>body{{font:11px Helvetica,Arial,sans-serif;margin:24px}}'
            f'table{{border-collapse:collapse;width:100%;margin-bottom:14px}}td,th{{border-bottom:1px solid #ccc;padding:3px 5px;text-align:left}}'
            f'td{{white-space:nowrap}}.n{{text-align:right}}.w{{white-space:normal;color:#555}}h3{{margin:10px 0 4px}}</style><h2>{escape(title)}</h2>'
            f'<p>Working paper. Net and VAT in EUR (foreign currency at the rate shown in the worksheet). The owner decides every item.</p>')
    i = 0
    for heading, rows in sections:
        body = ''
        for r in rows:
            i += 1
            vat = r['rc_vat_eur'] if r['treatment'] in RC else r['vat_eur']
            page = r['page'] + offset if r['page'] else 'FILE MISSING'
            body += (f'<tr><td>{i}</td><td>{escape(r["invoice_date"])}</td><td>{escape(r["supplier"][:30])}</td><td>{escape(str(r["invoice_no"]))[:22]}</td>'
                     f'<td>{escape(r["doc_type"].replace("_", " "))}</td><td class=n>{r["currency"]} {fmt(r["gross"])}</td><td class=n>{fmt(r["net_eur"])}</td>'
                     f'<td class=n>{fmt(vat)}</td><td class=w>{escape(r["treatment"].replace("_", " "))}{" *" if r["needs_review"] else ""}</td><td class=n>{page}</td></tr>')
        net = r2(sum(r['net_eur'] or 0 for r in rows))
        vat = r2(sum((r['rc_vat_eur'] if r['treatment'] in RC else r['vat_eur']) or 0 for r in rows))
        html += (f'<h3>{escape(heading)}</h3><table><tr><th>#<th>Date<th>Supplier<th>Invoice<th>Type<th class=n>Gross<th class=n>Net EUR<th class=n>VAT EUR<th>Treatment<th class=n>Page</tr>'
                 f'{body}<tr><th colspan=6>Total ({len(rows)} documents)<th class=n>{fmt(net)}<th class=n>{fmt(vat)}<th colspan=2></tr></table>')
    html += '<p>* in the review queue of the worksheet.</p>'
    src = dest.with_suffix('.html')
    src.write_text(html)
    ok = html_to_pdf(chrome, src, dest)
    src.unlink(missing_ok=True)
    if not ok:
        return 0
    return len(pypdfium2.PdfDocument(str(dest)))


def build_packet(qdir: Path, name: str, title: str, sections: list[tuple[str, list[dict]]]) -> None:
    """Merge every document of the table rows (section order, then date) into qdir/<name>.pdf behind an index page and
    write <name>_index.csv. Both list every row given: a row whose file is missing stays in, marked, and is never dropped."""
    rows = [r for _, part in sections for r in part]
    merged = pypdfium2.PdfDocument.new()
    skipped = []
    for r in rows:
        start = len(merged)
        for f in filter(None, r['source_file'].split(' | ')):
            data = as_pdf(qdir / f)
            if data is None:
                skipped.append(f)
                continue
            try:
                merged.import_pages(pypdfium2.PdfDocument(data))
            except Exception as e:
                logger.warning('Cannot merge %s: %s', f, e)
                skipped.append(f)
        r['page'], r['_pages'] = (start + 1, len(merged) - start) if len(merged) > start else ('', 0)
        if not r['page']:
            logger.warning('%s: no file could be merged for %s %s %s (kept in the index)', name, r['invoice_date'], r['supplier'], r['invoice_no'])
    dest = qdir / f'{name}.pdf'
    if not rows:
        logger.info('%s: nothing to list', name)
        dest.unlink(missing_ok=True)
        return
    chrome = find_chrome()
    if chrome:  # index in front; page numbers are shifted by the index length (rendered twice to learn it)
        tmp = qdir / f'.{name}_index.pdf'
        n = render_index(chrome, title, sections, 0, tmp)
        n = render_index(chrome, title, sections, n, tmp) if n else 0
        if n:
            final = pypdfium2.PdfDocument.new()
            final.import_pages(pypdfium2.PdfDocument(str(tmp)))
            if len(merged):
                final.import_pages(merged)
            merged = final
            for r in rows:
                r['page'] = r['page'] + n if r['page'] else ''
        tmp.unlink(missing_ok=True)
    else:
        logger.warning('Chrome not found: %s has no index page (see %s_index.csv)', name, name)
    if len(merged):
        merged.save(str(dest))
    with open(qdir / f'{name}_index.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, CSV_FIELDS, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    logger.info('%s: %d documents, %d pages -> %s%s', name, len(rows), len(merged), dest,
                f' ({len(skipped)} files not mergeable: {", ".join(skipped[:3])})' if skipped else '')


def build_packets(qdir: Path, rows: list[dict]) -> None:
    """<Q>_claimable.pdf: A domestic input VAT, B reverse-charge services, C risk items (OSS). <Q>_noVAT.pdf: everything
    else except duplicates. Both come from the same table rows as summary.csv, never from a separate filter."""
    sec = lambda *ts: [r for r in rows if r['treatment'] in ts and not is_sale(r)]
    build_packet(qdir, f'{qdir.name}_claimable', f'{qdir.name}: documents behind the VAT return',
                 [('A. Domestic input VAT (Boxes 31 / 37)', sec('domestic_input')), ('B. Reverse-charge services (Boxes 3 to 16)', sec(*RC)),
                  ('C. Risk items: Maltese VAT charged through OSS (inside Boxes 31 / 37 only if the owner keeps them)', sec('oss_risk'))])
    build_packet(qdir, f'{qdir.name}_noVAT', f'{qdir.name}: not claimed (by treatment)',
                 [(t.replace('_', ' '), sec(t)) for t in sorted({r['treatment'] for r in rows if not is_sale(r)} - set(CLAIM) - set(RC) - {'duplicate'})])
    build_packet(qdir, f'{qdir.name}_sales', f'{qdir.name}: sales invoices issued', [('Sales invoices with a file', [r for r in rows if is_sale(r) and r['source_file']])])


def restore_manual(rows: list[dict], qdir: Path, expenses: Path) -> None:
    """A claimed row must have its file. If a manual document's copy has gone missing, copy it again from expenses/."""
    for row in rows:
        rec = row['_rec']
        if row['treatment'] == 'duplicate' or rec.get('source') != 'manual' or all((qdir / f).exists() for f in rec.get('files') or [None]):
            continue
        src = expenses / rec.get('orig', '')
        if rec.get('orig') and src.is_file():
            dest = qdir / (rec['files'][0] if rec.get('files') else Path(BUCKET_DIRS.get(rec.get('bucket'), '')) / vendor_folder(rec.get('vendor'), qdir) / src.name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            ext, data = manual_bytes(src)
            dest = dest.with_suffix(ext)
            dest.write_bytes(data)
            rec['files'] = [str(dest.relative_to(qdir))]
            row['source_file'] = rec['files'][0]
            logger.warning('Restored missing file for %s %s from %s', row['supplier'], row['invoice_no'], src)


def finalize(records: list[dict], ledger: dict, ledger_path: Path, out: Path, qdir: Path, label: str, period, client, expenses: Path) -> None:
    """Manual documents + triage + the two merged PDFs + summaries and worksheet, for one scope. Gmail is not involved."""
    records = records + import_manual(ledger, out, qdir, label, period, client, expenses)
    rows = triage(records, out, client, ledger_path, ledger)
    if period:  # the facts read may put an invoice date outside the email's quarter
        rows = [r for r in rows if period[0].isoformat() <= r['invoice_date'] <= period[1].isoformat()]
    restore_manual(rows, qdir, expenses)
    rules = load_rules(out)
    rows += build_sales(ledger, out, qdir, period, client, rules)
    ledger_path.write_text(json.dumps(ledger, indent=2))
    all_txs = txs = read_statements(out / 'statements')
    if period:  # payments of the quarter, plus the window in which its invoices can be paid
        lo, hi = (period[0] - timedelta(days=PAY_BEFORE)).isoformat(), (period[1] + timedelta(days=max(PAY_AFTER, SALE_PAY_AFTER))).isoformat()
        txs = [t for t in txs if lo <= t['date'] <= hi]
    unpaid = match_payments(rows, txs, rules)
    unbilled = unmatched_income(txs, rules)
    if period:
        unpaid, unbilled = ([t for t in ts if period[0].isoformat() <= t['date'] <= period[1].isoformat()] for ts in (unpaid, unbilled))
    build_packets(qdir, rows)  # sets the page of each row
    write_summary(qdir, rows, unpaid, statement_stats(out / 'statements', all_txs, unpaid + unbilled), unbilled)


def run_final(out: Path, label: str, client, expenses: Path) -> None:
    """The --final step: the quarter from the ledger + manual documents + statements -> table, PDFs, worksheet. No Gmail."""
    ledger_path = out / 'ledger.json'
    ledger = json.loads(ledger_path.read_text())
    period = quarter_range(label)
    qdir = out / label
    qdir.mkdir(parents=True, exist_ok=True)
    expand_bolt_reports(ledger, out, period)
    lo, hi = period[0].isoformat(), period[1].isoformat()
    records = [r for r in ledger.values() if r.get('source') != 'manual' and r.get('is_financial_document') and lo <= r.get('date', '') <= hi]
    for r in records:
        adopt_files(r, out, qdir, period)
    finalize(records, ledger, ledger_path, out, qdir, label, period, client, expenses)


# ---------- portal: the whole quarter on one local page (collect, upload, review, return) ----------

REVIEW_CHOICES = ('domestic_input', 'reverse_charge_eu', 'reverse_charge_non_eu', 'oss_risk', 'personal', 'blocked', 'foreign_vat',
                  'exempt_or_zero', 'no_tax_invoice', 'not_a_purchase')
# VAT FORM 003/2011 as printed on the MTCA acknowledgement: (section, [(line, [boxes])])
FORM = [('Intra-Community and non-EU trade', [
            ('Exempt IC supplies of goods and services where the customer is liable for the tax', ['1']),
            ('Supplies where the place of supply is outside Malta', ['2']),
            ('IC acquisitions of goods and services received from other EU member states', ['3', '6']),
            ('Goods and services received where the place of supply is Malta, other than Box 3', ['4', '7']),
            ('Sub-total', ['5', '8'])]),
        ('Deductions of self-charged VAT', [
            ('IC acquisitions of goods excluding capital goods', ['9', '13']),
            ('Services received from EU member states where the purchaser is liable', ['9a', '13a']),
            ('IC acquisitions of capital goods', ['10', '14']),
            ('Goods and services received where the place of supply is Malta', ['11', '15']),
            ('Sub-total, and Box 17 = 8 - 16', ['12', '16', '17'])]),
        ('Domestic supplies and exports', [
            ('Taxable goods / services @ 18%', ['18', '23']), ('Taxable services @ 7%', ['18a', '23a']), ('Taxable services @ 12%', ['18b', '23b']),
            ('Taxable goods / services @ 5%, and Box 25', ['19', '24', '25']), ('Exempt with credit / exports', ['20']),
            ('Exempt without credit', ['21']), ('Sub-total, and Box 26 = 17 + 25', ['22', '26'])]),
        ('Domestic purchases and imports', [
            ('Taxable purchases for re-sale @ 18%', ['27', '34']), ('Taxable purchases for re-sale @ 5%', ['28', '35']),
            ('Exempt purchases for re-sale', ['29']), ('Capital goods', ['30', '36']), ('Services and overheads @ 18%', ['31', '37']),
            ('Services and overheads @ 7%', ['31a', '37a']), ('Services and overheads @ 12%', ['31b', '37b']),
            ('Services and overheads @ 5%', ['32', '38']), ('Sub-total, and Box 39 = input tax', ['33', '39']),
            ('Adjustments: in favour of the department / in your favour', ['40', '41'])]),
        ('Result', [('Excess credit', ['42']), ('Tax due before credit brought forward', ['43']), ('Excess credit brought forward', ['44']), ('Tax payable', ['45'])])]
FORM_NOT_HANDLED = ('9', '10', '13', '14', '27', '28', '29', '30', '31a', '31b', '32', '34', '35', '36', '37a', '37b', '38', '40', '41')


def return_form(rows: list[dict], brought: float = 0.0) -> dict:
    """Every box of VAT FORM 003/2011. Handled: sales (Boxes 1, 2, 18 to 26), reverse-charge services, services / overheads
    at 18% and the credit brought forward. Goods for re-sale, capital goods, purchases at other rates and adjustments stay
    0.00 (FORM_NOT_HANDLED)."""
    b = return_boxes(rows)
    f = {box: 0.0 for _, lines in FORM for _, boxes in lines for box in boxes}
    f.update({k: v for k, v in b.items() if k in f})
    f['33'], f['39'] = f['31'], f['37']
    net = r2((f['39'] - f['26']) + (f['41'] - f['40']))
    f['42'], f['43'], f['44'] = max(net, 0.0), max(-net, 0.0), r2(brought)
    f['45'] = max(r2(f['43'] - f['44']), 0.0)
    return f


def brought_forward(out: Path, label: str) -> tuple[float, str]:
    """Box 44 for a quarter and where it comes from: credit_brought_forward in output/decisions.yml, else Box 42 of the
    return filed for the quarter before (output/filed.json)."""
    import yaml
    dpath, fpath = out / 'decisions.yml', out / 'filed.json'
    own = ((yaml.safe_load(dpath.read_text()) or {}).get('credit_brought_forward') or {}) if dpath.exists() else {}
    if label in own:
        return float(own[label] or 0), 'Box 44 is credit_brought_forward in output/decisions.yml.'
    if not re.fullmatch(r'\d{4}-Q[1-4]', label):
        return 0.0, ''
    m = int(label[:4]) * 4 + int(label[-1]) - 2
    prev = f'{m // 4}-Q{m % 4 + 1}'
    try:
        credit = float((json.loads(fpath.read_text()).get(prev) or {}).get('42') or 0)
    except (OSError, ValueError):
        credit = 0.0
    if not credit:
        return 0.0, ''
    return credit, (f'Box 44 is Box 42 of the return filed for {prev} ({credit:.2f}). If that credit was refunded or differs, set '
                    f'credit_brought_forward: {{{label}: <amount>}} in output/decisions.yml. Check how the portal form carries a remaining credit.')


PORTAL_PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Bookkeeper</title>
<style>
:root{--bg:#f5f6f8;--panel:#fff;--card:#f8f9fb;--fg:#111827;--mut:#6b7280;--line:#e6e8ec;--acc:#4f46e5;--btn:#4f46e5;--acc2:#eef2ff;--warn:#b45309;--warn2:#fff4e5;--ok:#15803d;--ok2:#e8f7ee;--bad:#b91c1c;--bad2:#fdecec;
  --side:#111827;--shadow:0 1px 2px rgba(16,24,40,.05),0 2px 6px rgba(16,24,40,.06)}
@media(prefers-color-scheme:dark){:root{--bg:#0b0f17;--panel:#131a24;--card:#18212d;--fg:#e6e9ee;--mut:#9aa4b2;--line:#263243;--acc:#a5b4fc;--btn:#5b5ff0;--acc2:#1f2547;--warn:#fbbf24;--warn2:#2b2212;--ok:#4ade80;--ok2:#11281b;--bad:#f87171;--bad2:#2c1616;
  --side:#0d131c;--shadow:0 1px 2px rgba(0,0,0,.4)}}
*{box-sizing:border-box}html{color-scheme:light dark}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
#app{display:grid;grid-template-columns:236px minmax(0,1fr);min-height:100vh}
aside{background:var(--side);color:#cbd5e1;padding:20px 14px;display:flex;flex-direction:column;gap:18px;position:sticky;top:0;height:100vh}
.brand{font-weight:700;font-size:16px;display:flex;align-items:center;gap:10px;color:#fff;padding:0 6px}
.logo{width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,#6366f1,#22d3ee);display:grid;place-items:center;font-weight:800;color:#fff}
.ql{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:#8b95a5;display:grid;gap:6px;padding:0 6px}
.ql select{background:#1f2937;color:#fff;border-color:#374151;width:100%;font-size:14px;text-transform:none;letter-spacing:0}
nav{display:grid;gap:2px}nav button{display:flex;align-items:center;gap:10px;background:none;color:#cbd5e1;border:0;border-radius:9px;padding:9px 10px;text-align:left;font-weight:500;width:100%;box-shadow:none}
nav button:hover{background:rgba(255,255,255,.07);filter:none}nav button.on{background:rgba(99,102,241,.28);color:#fff}nav button span{flex:1}nav button svg{width:18px;height:18px;flex:none;opacity:.9}
nav em{font-style:normal;font-size:11px;background:rgba(255,255,255,.12);border-radius:10px;padding:1px 8px}nav em.hot{background:#f59e0b;color:#111;font-weight:700}
#job{font-size:12px;color:#8b95a5;margin-top:auto;padding:0 6px;overflow-wrap:anywhere}#job.bad{color:#fca5a5}.foot{font-size:11px;color:#6b7280;padding:0 6px}
main{padding:26px 30px 0;min-width:0;max-width:1480px}
@media(max-width:900px){#app{grid-template-columns:1fr}aside{position:static;height:auto;flex-direction:row;flex-wrap:wrap;align-items:center;gap:10px;padding:12px}nav{display:flex;flex-wrap:wrap}nav button{width:auto}#job{margin:0}.foot{display:none}main{padding:14px 14px 0}}
h2{font-size:13px;margin:26px 0 10px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);font-weight:600}
h2.t{font-size:24px;margin:0 0 16px;text-transform:none;letter-spacing:-.01em;color:var(--fg);font-weight:700}h2.t span{display:block;font-size:13px;font-weight:400;margin-top:2px}
h3{font-size:15px;margin:0 0 10px;display:flex;justify-content:space-between;align-items:baseline}h3 span{font-size:12px;font-weight:400;color:var(--mut)}
p{margin:8px 0}.mut{color:var(--mut)}.ok{color:var(--ok)}.bad{color:var(--bad)}.why{color:var(--warn)}.small{font-size:12px}a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
button{font:inherit;font-weight:600;padding:8px 14px;border-radius:9px;border:1px solid transparent;background:var(--btn);color:#fff;cursor:pointer;box-shadow:var(--shadow);transition:filter .12s}
button:hover{filter:brightness(1.08)}button.sec{background:var(--panel);color:var(--fg);border-color:var(--line)}button.mini{padding:4px 10px;font-size:12px;border-radius:7px}button:disabled{opacity:.5;cursor:default;filter:none}
select,input[type=text]{font:inherit;padding:7px 10px;background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:8px;max-width:100%;outline:0}
select:focus,input[type=text]:focus{border-color:var(--btn);box-shadow:0 0 0 3px rgba(99,102,241,.2)}input[type=checkbox]{accent-color:var(--btn);width:15px;height:15px;vertical-align:-2px}
input[type=file]{font-size:12px;max-width:100%;color:var(--mut)}input[type=file]::file-selector-button{font:inherit;font-size:12px;margin-right:8px;padding:5px 10px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px}.two{display:grid;grid-template-columns:3fr 2fr;gap:14px}@media(max-width:1100px){.two{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px;min-width:0;box-shadow:var(--shadow)}
.card>span.mut:first-child{font-size:11px;text-transform:uppercase;letter-spacing:.06em;font-weight:600}.card b.big{display:block;font-size:22px;letter-spacing:-.01em;margin:4px 0 2px}.card button{margin-top:8px}
.prog{height:6px;border-radius:6px;background:var(--line);overflow:hidden;margin-bottom:6px}.prog i{display:block;height:100%;background:linear-gradient(90deg,#6366f1,#22d3ee);border-radius:6px}
.scroll{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);padding:9px 10px;text-align:left;vertical-align:top}tr:last-child td{border-bottom:0}
td.n,th.n{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}td.d{white-space:nowrap;font-variant-numeric:tabular-nums}
th{background:var(--card);white-space:nowrap;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);font-weight:600}th.s{cursor:pointer;user-select:none}th.s:hover{color:var(--acc)}
tr:hover td{background:var(--card)}tr[data-id],tr[data-pid],tr.g{cursor:pointer}tr.g td{background:var(--card)}tr.sub td:nth-child(3){padding-left:30px}
tr.sel td,tr.sel:hover td{background:var(--acc2)}tr.sel td:first-child{box-shadow:inset 3px 0 0 var(--btn)}
.pm{display:inline-grid;place-items:center;width:18px;height:18px;border-radius:5px;background:var(--btn);color:#fff;margin-right:8px;font-weight:700;font-size:13px;line-height:1}
tr.f td{padding:5px 6px;background:var(--panel)}tr.f:hover td{background:var(--panel)}td select,td input[type=text]{width:100%;min-width:150px;margin-bottom:4px;padding:5px 8px}td input.cf{min-width:56px;margin:0;padding:4px 7px;font-size:12px;background:var(--card)}
table.form td{vertical-align:middle}.box{display:inline-block;min-width:160px;margin:2px 10px 2px 0;font-variant-numeric:tabular-nums}.box i{font-style:normal;color:var(--mut);display:inline-block;min-width:34px;font-size:12px}
pre{background:var(--panel);border:1px solid var(--line);padding:12px;border-radius:12px;max-height:260px;overflow:auto;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap}
.tag{display:inline-block;font-size:11px;font-weight:500;padding:1px 8px;margin:1px 3px 1px 0;border-radius:10px;background:var(--card);border:1px solid var(--line);color:var(--mut);cursor:pointer;white-space:nowrap}
.tag.b{background:var(--acc2);border-color:transparent;color:var(--acc)}.tag.g{background:var(--ok2);border-color:transparent;color:var(--ok)}.tag.q,.tag.a{background:var(--warn2);border-color:transparent;color:var(--warn)}
.tag.on{outline:2px solid var(--btn);outline-offset:1px}.tag:hover{filter:brightness(.96)}
.tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:10px 0}.tools input[type=text]{min-width:260px}
.batch{background:var(--acc2);border-radius:12px;padding:8px 12px}
ul.files,ul.todo,ul.act{list-style:none;padding:0;margin:8px 0 0}ul.files{max-height:180px;overflow:auto;font-size:12px;border-top:1px solid var(--line);margin-top:12px;padding-top:8px}ul.files li{padding:3px 0;overflow-wrap:anywhere}
ul.todo li{padding:8px 0;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:center}ul.todo li:last-child{border:0}ul.todo li.done{color:var(--mut)}ul.todo li>span:nth-child(2){flex:1}
.dot{flex:none;width:20px;height:20px;border-radius:50%;display:grid;place-items:center;font-size:12px;font-weight:700;background:var(--warn2);color:var(--warn)}li.done .dot{background:var(--ok2);color:var(--ok)}
ul.act{font-size:13px;max-height:260px;overflow:auto}ul.act li{padding:5px 0;border-bottom:1px solid var(--line)}ul.act li:last-child{border:0}ul.act .mut{display:block;font-size:11px}
.note{border-left:3px solid var(--btn)}
#bar{position:sticky;bottom:0;background:var(--panel);border:1px solid var(--line);border-bottom:0;border-radius:14px 14px 0 0;box-shadow:0 -4px 16px rgba(16,24,40,.08);padding:12px 16px;margin-top:18px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
</style>
<div id=app><aside><div class=brand><span class=logo>B</span>Bookkeeper</div><label class=ql>Quarter<select id=q></select></label><nav id=nav></nav><div id=job></div>
<div class=foot>Runs on this computer only. It never files or submits anything.</div></aside><main id=main></main></div><input type=file id=up_p hidden accept=".pdf,.png,.jpg,.jpeg,.heic,.heif">
<script>
const TOKEN = "__TOKEN__", CH = __CHOICES__, CHS = __SCHOICES__, FORM = __FORM__, NOT = __NOT__, NL = String.fromCharCode(10);
const D0 = () => ({sort: 'invoice_date', dir: 1, q: '', tag: '', tr: '', id: '', f_invoice_date: '', f_supplier: '', f_doc_type: '', f_gross_eur: '', f_net_eur: '', f_vat: '', f_treatment: '', f_tags: '', f_payment: ''});
const R0 = () => ({sort: 'gross_eur', dir: -1, q: '', flag: '', grp: 'open', by: 'supplier', f_invoice_date: '', f_supplier: '', f_gross_eur: '', f_vat: '', f_treatment: '', f_flags: ''});
const V = {doc: D0(), rev: R0(), pay: {sort: 'amount', dir: -1}};
let S = null, tab = 'overview', E = {}, P = {}, NOTE = [], sel = new Set(), psel = new Set(), shown = [], pshown = [], payFor = '', payKind = 'receipts', PIDS = {out: [], in: []}, G = {}, vis = [], pvis = [], last = '', plast = '';
const openG = new Set();
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = x => x === null || x === '' || x === undefined ? '' : Number(x).toLocaleString('en', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const nice = s => String(s || '').replace(/_/g, ' ');
const claimed = t => ['domestic_input','oss_risk','reverse_charge_eu','reverse_charge_non_eu'].includes(t);
const live = r => !['duplicate','pre_registration','not_a_purchase','not_a_sale'].includes(r.treatment), isSale = r => r.direction === 'sale', isOpen = r => r.needs_review && live(r);
const paid = r => /^[0-9]{4}/.test(r.payment || ''), vatOf = r => claimed(r.treatment) ? (r.rc_vat_eur ?? r.vat_eur) : r.vat_eur;
const q = () => document.getElementById('q').value;
const qname = l => { const [y, n] = l.split('-Q'); return `Q${n} ${y}`; };
const day = s => new Date(s + 'T12:00').toLocaleDateString('en-GB', {day: 'numeric', month: 'long', year: 'numeric'});
const title = what => `<h2 class=t>${what} for ${qname(S.label)}<span class=mut>${day(S.period[0])} to ${day(S.period[1])}${S.built_at ? ' · built ' + esc(S.built_at) : ' · not built yet'}</span></h2>`;
const post = async (url, body, headers = {}) => { const res = await fetch(url, {method: 'POST', headers: {'X-Token': TOKEN, ...headers}, body});
  if (res.status === 403) alert('The portal was restarted since this page was opened. Reload the page, then try again.'); return res; };
const links = r => String(r.source_file||'').split(' | ').filter(Boolean).map((f,i)=>`<a href="/file?q=${q()}&p=${encodeURIComponent(f)}" target=_blank>file${i?i+1:''}</a>`).join(' · ')
  + (r.gmail_link ? ` · <a href="${esc(r.gmail_link)}" target=_blank>email</a>` : '');
const tcls = t => t.startsWith('Box') ? 'b' : t === 'paid' ? 'g' : t === 'no payment' ? 'a' : '';
const chip = (view, t, on, cls = '') => `<span class="tag ${cls || tcls(t)} ${on === t ? 'on' : ''}" data-view="${view}" data-tag="${esc(t)}" title="${on === t ? 'Click to remove this filter' : 'Show only: ' + esc(t)}">${esc(t)}${on === t ? ' ×' : ''}</span>`;
const hit = (text, query) => query.toLowerCase().split(' ').filter(Boolean).every(w => text.includes(w));
const words = (r, extra = '') => [r.supplier, r.invoice_no, r.invoice_date, nice(r.doc_type), nice(r.treatment), r.reason, r.review_reason, r.payment, r.currency, r.gross, money(r.gross),
  r.tags.join(' '), r.flags.join(' '), extra].join(' ').toLowerCase();
const rowKey = k => r => k === 'vat' ? vatOf(r) : k === 'tags' ? r.tags.join(' ') : k === 'flags' ? r.flags.join(' ') : r[k];
// one filter box per column: words must all be in the cell; on amount columns ">100" or "<20" compares the EUR value
const COLF = {invoice_date: r => r.invoice_date, supplier: r => [r.supplier, r.invoice_no, nice(r.doc_type)].join(' '), doc_type: r => nice(r.doc_type),
  gross_eur: r => [r.currency, money(r.gross), r.gross, money(r.gross_eur)].join(' '), net_eur: r => money(r.net_eur) + ' ' + r.net_eur, vat: r => money(vatOf(r)) + ' ' + vatOf(r),
  treatment: r => nice(r.treatment) + ' ' + r.reason, flags: r => r.flags.join(' ') + ' ' + r.review_reason, tags: r => r.tags.concat(r.flags).join(' '), payment: r => r.payment};
const NUMF = {gross_eur: r => r.gross_eur, net_eur: r => r.net_eur, vat: vatOf};
const colHit = (r, v) => Object.keys(COLF).every(k => { const f = String(v['f_' + k] || '').trim(); if (!f) return true;
  if (NUMF[k] && /^[<>] *[0-9.]+$/.test(f)) { const x = NUMF[k](r) || 0, y = Number(f.slice(1)); return f[0] === '>' ? x > y : x < y; }
  return hit(String(COLF[k](r) ?? '').toLowerCase(), f); });
const fin = (view, key, hint = 'filter') => `<td><input type=text class=cf id="cf_${view}_${key}" placeholder="${hint}" value="${esc(V[view]['f_' + key])}" oninput="V.${view}['f_${key}']=this.value;go(false)">`;
function sortRows(list, v, get){ return [...list].sort((x, y) => { const a = get(x), b = get(y), na = a == null || a === '', nb = b == null || b === '';
  return na || nb ? na - nb : v.dir * (typeof a === 'number' && typeof b === 'number' ? a - b : String(a).localeCompare(String(b))); }); }
const th = (view, key, label, cls = '') => `<th class="s ${cls}" onclick="sortBy('${view}','${key}')" title="Sort">${label}${V[view].sort === key ? (V[view].dir > 0 ? ' ▲' : ' ▼') : ''}`;
function sortBy(view, key){ const v = V[view]; v.dir = v.sort === key ? -v.dir : 1; v.sort = key; go(); }
const counts = (list, get) => { const c = {}; list.forEach(r => get(r).forEach(t => c[t] = (c[t] || 0) + 1)); return Object.entries(c).sort((a, b) => b[1] - a[1]); };
const ansOf = id => E[id] || (a => a ? {v: a.treatment || (a.accepted ? 'accept' : ''), note: a.note || ''} : {v: '', note: ''})(S.answers[id]);
const docLabel = r => `${r.invoice_date} · ${r.supplier} · ${r.currency} ${money(r.gross)}${r.invoice_no ? ' · ' + r.invoice_no : ''}`;
async function load(label, keep){ S = await (await fetch('/api/state' + (label ? '?q=' + label : ''))).json();
  if (!keep) { E = {}; P = {}; sel.clear(); psel.clear(); }
  document.getElementById('q').innerHTML = S.quarters.map(x => `<option value="${x}" ${x===S.label?'selected':''}>${qname(x)}</option>`).join(''); draw(); job(); }
document.getElementById('q').onchange = e => { NOTE = []; Object.assign(V.doc, D0()); Object.assign(V.rev, R0()); load(e.target.value).then(() => route(true)); };
function draw(){
  const a = document.activeElement, keep = a && a.id && a.tagName === 'INPUT' && a.type === 'text' ? [a.id, a.selectionStart] : null;
  const ic = d => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
  const tabs = [['overview', 'Overview', '', ic('<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/>')],
    ['review', 'Review', S.rows.filter(isOpen).length + S.unpaid.length + S.unbilled.length, ic('<path d="M9 11l3 3 8-8"/><path d="M20 12v7a1 1 0 01-1 1H5a1 1 0 01-1-1V5a1 1 0 011-1h10"/>')],
    ['documents', 'Documents', S.rows.length, ic('<path d="M14 3H7a1 1 0 00-1 1v16a1 1 0 001 1h10a1 1 0 001-1V7z"/><path d="M14 3v4h4M9 13h6M9 17h6"/>')],
    ['return', 'VAT return', '', ic('<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 7h8M8 12h2M14 12h2M8 16h2M14 16h2"/>')]];
  document.getElementById('nav').innerHTML = tabs.map(([k, t, c, i]) => `<button class="${k===tab?'on':''}" onclick="tab='${k}';go()">${i}<span>${t}</span>${c !== '' ? `<em class="${k === 'review' && c ? 'hot' : ''}">${c}</em>` : ''}</button>`).join('');
  document.getElementById('main').innerHTML = ({overview, review, documents, return: ret})[tab in {overview:1,review:1,documents:1,return:1} ? tab : 'overview']();
  if (keep) { const el = document.getElementById(keep[0]); if (el) { el.focus(); el.setSelectionRange(keep[1], keep[1]); } }
  lock();
}
function lock(){ document.querySelectorAll('main button, main input[type=file]').forEach(b => b.disabled = !!(S && S.job.running)); }
function overview(){
  const f = S.form, n = t => S.rows.filter(r => r.treatment === t).length, cl = S.rows.filter(r => claimed(r.treatment)), fails = S.checks.filter(c => !c[1]);
  const mark = {ok: ['ok', '✓'], review: ['why', '?'], new: ['why', '…'], bad: ['bad', '✗']};
  const flist = items => items.length ? `<ul class=files>${items.map(u => `<li><span class="${mark[u.state][0]}">${mark[u.state][1]}</span> <b>${esc(u.file)}</b> <span class="${u.state === 'bad' ? 'bad' : 'mut'}">${esc(u.status)}</span>${u.doc_id ? ` <a href="#documents" data-doc="${esc(u.doc_id)}">show</a>` : ''}</li>`).join('')}</ul>` : '';
  return title('Overview') + (NOTE.length ? `<div class="card note" style="margin-bottom:14px"><b>Last upload</b>${NOTE.map(x => `<div class="${x[0] ? 'ok' : 'bad'}">${x[0] ? '✓' : '✗'} ${esc(x[1])}</div>`).join('')}</div>` : '') + `
  <div class=two><div class=card><h3>To do <span>${S.todo.filter(t => t.done).length} of ${S.todo.length} done</span></h3>
  <div class=prog><i style="width:${Math.round(100 * S.todo.filter(t => t.done).length / (S.todo.length || 1))}%"></i></div>
  <ul class=todo>${S.todo.map(t => `<li class="${t.done ? 'done' : ''}"><span class=dot>${t.done ? '✓' : '!'}</span><span>${esc(t.text)}</span>
    ${t.done ? '' : t.run ? `<button class=mini onclick="run('final')">Build now</button>` : t.tab ? `<a href="#${t.tab}">Open</a>` : ''}</li>`).join('')}</ul></div>
  <div class=card><h3>Activity</h3>${S.activity.length ? `<ul class=act>${S.activity.map(a => `<li><span class=mut>${esc(a.time)}${a.quarter ? ' · ' + qname(a.quarter) : ''}</span>${esc(a.text)}</li>`).join('')}</ul>` : '<p class=mut>Nothing yet.</p>'}</div></div>
  <h2>Collect</h2><div class=grid>
  <div class=card><span class=mut>1. Email</span><b class=big>${S.rows.filter(r=>r.source==='email').length} documents from Gmail</b>
    <p class=mut>Searches both mailboxes for the quarter and downloads again. Takes several minutes.</p><button class=sec onclick="run('gmail')">Scan Gmail for ${qname(S.label)}</button></div>
  <div class=card><span class=mut>2. Invoices and receipt photos</span><b class=big>${S.uploads.length} file${S.uploads.length===1?'':'s'} for ${qname(S.label)}</b>
    <p class=mut>PDF invoices that never arrive by email, and photos of receipts (HEIC, JPG, PNG).${S.other_quarters ? ` ${S.other_quarters} more files in the folder belong to other quarters.` : ''}</p>
    <input type=file id=up_r multiple accept=".pdf,.png,.jpg,.jpeg,.heic,.heif"> <button class=sec onclick="upload('receipts',document.getElementById('up_r'))">Add and build</button>${flist(S.uploads)}</div>
  <div class=card><span class=mut>3. Sales invoices</span><b class=big>${S.rows.filter(isSale).length} invoice${S.rows.filter(isSale).length===1?'':'s'} issued</b>
    <p class=mut>The invoices you issued: a CSV list from your invoicing tool (invoice number, date, customer, country, VAT number, net, VAT), and / or the PDFs.</p>
    <input type=file id=up_v multiple accept=".csv,.pdf,.png,.jpg,.jpeg"> <button class=sec onclick="upload('sales',document.getElementById('up_v'))">Add and build</button>${flist(S.sales_files)}</div>
  <div class=card><span class=mut>4. Bank statements</span><b class=big>${S.statements.length} CSV export${S.statements.length===1?'':'s'}</b>
    <p class=mut>${S.statements.length ? `${cl.filter(paid).length} of ${cl.length} claimed documents have a payment. ${S.unpaid.length ? `<a href="#review">${S.unpaid.length} payment(s) have no document</a>.` : 'No payment is left without a document.'}` : 'Export CSV (not PDF) from each bank and card for the quarter.'}</p>
    <input type=file id=up_s multiple accept=".csv"> <button class=sec onclick="upload('statements',document.getElementById('up_s'))">Add and build</button>${flist(S.statements)}</div>
  <div class=card><span class=mut>5. Build</span><b class=big>${S.built ? `${S.rows.length} documents, ${S.rows.filter(isOpen).length} open` : 'Not built yet'}</b>
    <p class=mut>Reads new documents with the AI${S.has_key?'':' (not configured: only rules, no reading)'}, applies the rules, matches payments, writes the PDFs.</p><button onclick="run('final')">Build ${qname(S.label)}</button></div>
  </div>
  <h2>Result</h2><div class=grid>
  <div class=card><span class=mut>Domestic input VAT (Box 31 / 37)</span><b class=big>${money(f['31'])} / ${money(f['37'])}</b><span class=mut>${n('domestic_input')} documents + ${n('oss_risk')} OSS risk items</span></div>
  <div class=card><span class=mut>Reverse charge (Box 5 / 8)</span><b class=big>${money(f['5'])} / ${money(f['8'])}</b><span class=mut>${n('reverse_charge_eu')} EU, ${n('reverse_charge_non_eu')} non-EU</span></div>
  <div class=card><span class=mut>Sales / output VAT (Box 25)</span><b class=big>${money(f['1'] + f['2'] + f['22'])} / ${money(f['25'])}</b><span class=mut>${S.rows.filter(isSale).length} invoices issued${f['1'] || f['2'] ? `, ${money(f['1'] + f['2'])} without Maltese VAT (Box 1 / 2)` : ''}</span></div>
  <div class=card><span class=mut>${f['43']>0?'Tax payable (Box 45)':'Excess credit (Box 42)'}</span><b class=big>${money(f['43']>0?f['45']:f['42'])}</b>
    <span class="${fails.length?'bad':'ok'}">${fails.length ? fails.length + ' check(s) failed' : 'All checks pass'}</span></div>
  <div class=card><span class=mut>Compared with the filed return</span><b class="big ${S.filed?(S.diff.length?'bad':'ok'):''}">${S.filed ? (S.diff.length ? S.diff.length + ' box(es) differ' : 'Identical') : 'No filed figures'}</b>
    <span class=mut>${S.filed ? 'output/filed.json' : 'Add the quarter to output/filed.json after filing'}</span></div></div>
  <h2>Downloads</h2><p>${S.downloads.map(d => `<a href="/download?q=${S.label}&f=${encodeURIComponent(d)}" target=_blank>${esc(d)}</a>`).join(' · ') || '<span class=mut>Nothing built yet.</span>'}</p>
  <h2>Log of the last run</h2><pre id=log>${esc((S.job.log||[]).slice(-60).join(NL)) || 'No run since the portal was started.'}</pre>
  <p class="mut small">You sign in to the MTCA portal and submit yourself.</p>`;
}
function review(){
  const v = V.rev, groups = {open: ['All open questions', isOpen], claimed: ['Claimed in the return, with a question', r => claimed(r.treatment) && isOpen(r)],
    unclassified: ['Not classified: needs your decision', r => r.treatment === 'needs_review'], other: ['Not claimed, flagged', r => !claimed(r.treatment) && r.treatment !== 'needs_review' && isOpen(r)],
    answered: ['Already answered', r => !r.needs_review && S.answers[r.doc_id]]};
  const base = S.rows.filter(groups[v.grp][1]), list = sortRows(base.filter(r => (!v.flag || r.flags.includes(v.flag)) && hit(words(r, ansOf(r.doc_id).note), v.q) && colHit(r, v)), v, rowKey(v.sort));
  shown = list.map(r => r.doc_id);
  const opts = (cur, sale) => `<option value="">Leave open</option><option value="accept" ${cur==='accept'?'selected':''}>Checked: keep as it is</option>${(sale ? CHS : CH).map(c=>`<option value="${c}" ${cur===c?'selected':''}>Set: ${nice(c)}</option>`).join('')}`;
  const rowH = (r, sub) => { const a = ansOf(r.doc_id), on = sel.has(r.doc_id); return `<tr data-id="${esc(r.doc_id)}" class="${on ? 'sel' : ''} ${sub ? 'sub' : ''}"><td><input type=checkbox class=cb ${on ? 'checked' : ''}><td class=d>${esc(r.invoice_date)}
    <td><b>${esc(r.supplier)}</b><div class="mut small">${esc(r.invoice_no)} · ${esc(nice(r.doc_type))}</div>
    <td class=n>${esc(r.currency)} ${money(r.gross)}${r.currency !== 'EUR' && r.gross_eur ? `<div class="mut small">EUR ${money(r.gross_eur)}</div>` : ''}<td class=n>${money(vatOf(r))}
    <td>${esc(nice(r.treatment))}<div class="mut small">${esc(r.reason)}</div><td>${r.flags.map(t => chip('rev', t, v.flag, 'q')).join('')}<div class="why small">${esc(r.review_reason)}</div>
    <td>${links(r)}<td><select class=tr>${opts(a.v, isSale(r))}</select><input type=text class=note placeholder="Note (e.g. business purpose)" value="${esc(a.note)}"></tr>`; };
  const gs = new Map(); let body = ''; G = {}; vis = [];
  list.forEach(r => { const k = v.by ? r.supplier || '(no name)' : r.doc_id; gs.has(k) || gs.set(k, []); gs.get(k).push(r); });
  for (const [k, rs] of gs){
    if (rs.length === 1) { vis.push(rs[0].doc_id); body += rowH(rs[0]); continue; }
    G[k] = rs.map(r => r.doc_id);
    const op = openG.has(k), n = rs.filter(r => sel.has(r.doc_id)).length, all = n === rs.length, ds = rs.map(r => r.invoice_date).sort(), sum = f => rs.reduce((s, r) => s + (f(r) || 0), 0);
    const done = rs.filter(r => ansOf(r.doc_id).v).length;
    body += `<tr class="g ${all ? 'sel' : ''}" data-g="${esc(k)}" title="Click to ${op ? 'close' : 'open'}"><td><input type=checkbox class=gcb ${all ? 'checked' : ''} title="Select all ${rs.length}"><td class=d>${esc(ds[0])}<div class="mut small">to ${esc(ds[ds.length - 1])}</div>
      <td><b><span class=pm>${op ? '−' : '+'}</span>${esc(k)}</b><div class="mut small">${rs.length} documents${n && !all ? ` · ${n} selected` : ''}</div>
      <td class=n>EUR ${money(sum(r => r.gross_eur))}<td class=n>${money(sum(vatOf))}<td>${[...new Set(rs.map(r => nice(r.treatment)))].map(esc).join(', ')}
      <td>${counts(rs, r => r.flags).map(([t, c]) => chip('rev', t, v.flag, 'q').replace('</span>', ` ${c}</span>`)).join('')}<td><td class="mut small">${done ? `${done} of ${rs.length} answered` : ''}</tr>`;
    if (op) rs.forEach(r => { vis.push(r.doc_id); body += rowH(r, true); });
  }
  let h = title('Review') + `<p class=mut>Answer once. Click a row to select it (Shift-click selects everything in between), then "Apply to selected" and Save. Several documents of one supplier are folded into one line: + opens it. Answers are stored on this computer (output/review_answers.yml). Tax-impact doubts go to the accountant first.</p>
  <div class=tools><input type=text id=rq placeholder="Search supplier, number, amount, reason..." value="${esc(v.q)}" oninput="V.rev.q=this.value;go(false)">
    <select onchange="V.rev.grp=this.value;V.rev.flag='';go()">${Object.entries(groups).map(([k, g]) => `<option value="${k}" ${k===v.grp?'selected':''}>${g[0]} (${S.rows.filter(g[1]).length})</option>`).join('')}</select>
    <span>${counts(base, r => r.flags).map(([t, c]) => chip('rev', t, v.flag, 'q').replace('</span>', ` ${c}</span>`)).join('')}</span>
    ${dirty(v, R0()) ? `<button class=mini onclick="Object.assign(V.rev,R0());go()">Reset view</button>` : ''}</div>
  <div class="tools batch"><label><input type=checkbox onchange="selAll(this.checked)" ${list.length && list.every(r => sel.has(r.doc_id)) ? 'checked' : ''}> Select all ${list.length} shown</label>
    <b id=nsel>${sel.size} selected</b><select id=b_act>${opts('')}${CHS.map(c => `<option value="${c}">Set (sales): ${nice(c)}</option>`).join('')}</select><input type=text id=b_note placeholder="Note for all selected (optional)">
    <button class=sec onclick="applyBatch()">Apply to selected</button>${sel.size ? ' <button class="sec mini" onclick="sel.clear();draw()">Clear selection</button>' : ''}
    <span style="flex:1"></span><label><input type=checkbox ${v.by ? 'checked' : ''} onchange="V.rev.by=this.checked?'supplier':'';go()"> Group by supplier</label>
    ${v.by ? `<button class="sec mini" onclick="Object.keys(G).forEach(k => openG.add(k));draw()">Open all</button><button class="sec mini" onclick="openG.clear();draw()">Close all</button>` : ''}</div>
  <div class=scroll><table><tr><th>${th('rev','invoice_date','Date')}${th('rev','supplier','Supplier / customer')}${th('rev','gross_eur','Amount','n')}${th('rev','vat','VAT EUR','n')}${th('rev','treatment','Treated as')}${th('rev','flags','Question')}<th>Files<th>Your answer</tr>
  <tr class=f><td>${fin('rev','invoice_date','2026-08')}${fin('rev','supplier')}${fin('rev','gross_eur','>100')}${fin('rev','vat')}${fin('rev','treatment')}${fin('rev','flags')}<td><td></tr>${body}</table></div>
  ${list.length ? '' : '<p class=mut>Nothing matches. Use "Reset view" to see everything again.</p>'}`;
  pshown = pvis = [];
  const payT = (items, inc) => { const k = inc ? 'in' : 'out', pv = V.pay, free = S.rows.filter(r => live(r) && !paid(r) && (r.gross || 0) > 0 && isSale(r) === inc);
    const pays = sortRows(items.filter(t => hit([t.text, t.date, t.account, money(Math.abs(t.amount))].join(' ').toLowerCase(), v.q)), pv, t => pv.sort === 'amount' ? Math.abs(t.amount) : t[pv.sort]);
    PIDS[k] = pays.map(t => t.id); pshown = pvis = pvis.concat(PIDS[k]);
    const guess = t => free.filter(r => String(r.supplier).toLowerCase().split(/[^a-z]+/).some(w => w.length > 3 && t.text.toLowerCase().includes(w))
      || (r.gross_eur && Math.abs(Math.abs(t.amount) - r.gross_eur) <= 0.04 * r.gross_eur)).slice(0, 6);
    return `<h2>${inc ? 'Money in without a sales invoice' : 'Payments without a document'} (${items.length})</h2><p class=mut>${inc ? 'Payments received that match no invoice you issued. Add the invoice, link it to one that is already here, or mark it as not a sale (refund, own transfer, private).'
      : 'Money that left the account and matches no invoice. Upload the invoice for it, link it to a document that is already here, or mark it as needing none.'}</p>
    <datalist id=docs_${k}>${free.map(r => `<option value="${esc(docLabel(r))}">`).join('')}</datalist>
    <div class="tools batch"><label><input type=checkbox onchange="PIDS.${k}.forEach(i => this.checked ? psel.add(i) : psel.delete(i));draw()" ${pays.length && pays.every(t => psel.has(t.id)) ? 'checked' : ''}> Select all ${pays.length} shown</label>
      <b>${PIDS[k].filter(i => psel.has(i)).length} selected</b><button class=sec onclick="PIDS.${k}.filter(i => psel.has(i)).forEach(i => { P[i] = {ignore: true}; psel.delete(i); });draw()">${inc ? 'Not a sale' : 'No document needed'} for selected</button></div>
    <div class=scroll><table><tr><th>${th('pay','date','Date')}${th('pay','amount','Amount','n')}${th('pay','text','Description')}${th('pay','account','Statement')}<th>What is it?</tr>
    ${pays.map(t => { const p = P[t.id] || {}, g = guess(t), d = p.doc && S.rows.find(r => r.doc_id === p.doc);
      return `<tr data-pid="${esc(t.id)}" class="${psel.has(t.id) ? 'sel' : ''}"><td><input type=checkbox class=pcb ${psel.has(t.id)?'checked':''}><td class=d>${esc(t.date)}<td class=n>${esc(t.currency)} ${money(Math.abs(t.amount))}<td>${esc(t.text)}<td>${esc(t.account)}
      <td><select class=pa><option value="">Leave open</option><option value="ignore" ${p.ignore?'selected':''}>${inc ? 'Not a sale (refund, own transfer, private)' : 'No document needed (private, transfer, cash)'}</option>
        ${g.map(r => `<option value="${esc(r.doc_id)}" ${p.doc===r.doc_id?'selected':''}>It is: ${esc(docLabel(r))}</option>`).join('')}
        ${d && !g.includes(d) ? `<option value="${esc(d.doc_id)}" selected>It is: ${esc(docLabel(d))}</option>` : ''}</select>
        <input type=text class=pl list=docs_${k} placeholder="or type to find another ${inc ? 'invoice' : 'document'}"> <button class="sec mini" data-up="${esc(t.id)}" data-kind="${inc ? 'sales' : 'receipts'}">Upload its invoice</button></tr>`; }).join('')}</table></div>`; };
  if (S.unpaid.length) h += payT(S.unpaid, false);
  if (S.unbilled.length) h += payT(S.unbilled, true);
  const n = Object.keys(E).length + Object.keys(P).length;
  return h + `<div id=bar><button onclick="save()">Save and rebuild</button><span id=msg class="${n ? 'why' : 'mut'}">${n ? n + ' unsaved change(s)' : 'No changes'}</span>
    ${n ? `<button class="sec mini" onclick="E={};P={};draw()">Discard</button>` : ''}</div>`;
}
function selAll(on){ shown.forEach(i => on ? sel.add(i) : sel.delete(i)); draw(); }
function applyBatch(){
  const act = document.getElementById('b_act').value, note = document.getElementById('b_note').value.trim();
  if (!sel.size) return alert('Tick the rows first.');
  sel.forEach(id => E[id] = {v: act, note: note || ansOf(id).note}); sel.clear(); draw();
}
function tick(){ const n = Object.keys(E).length + Object.keys(P).length, m = document.getElementById('msg'), s = document.getElementById('nsel');
  if (m) { m.textContent = n ? n + ' unsaved change(s)' : 'No changes'; m.className = n ? 'why' : 'mut'; } if (s) s.textContent = sel.size + ' selected'; }
function documents(){
  const v = V.doc, ts = [...new Set(S.rows.map(r => r.treatment))].sort();
  const list = sortRows(S.rows.filter(r => v.id ? r.doc_id === v.id : (!v.tr || r.treatment === v.tr) && (!v.tag || r.tags.includes(v.tag)) && hit(words(r), v.q) && colHit(r, v)), v, rowKey(v.sort));
  const sum = k => list.reduce((s, r) => s + (live(r) ? (k === 'vat' ? vatOf(r) : r[k]) || 0 : 0), 0);
  return title('Documents') + `<div class=tools><input type=text id=dq placeholder="Search supplier, number, amount, tag..." value="${esc(v.q)}" oninput="V.doc.q=this.value;V.doc.id='';go(false)">
    <select onchange="V.doc.tr=this.value;V.doc.id='';go()"><option value="">All treatments</option>${ts.map(t=>`<option value="${t}" ${t===v.tr?'selected':''}>${nice(t)} (${S.rows.filter(r=>r.treatment===t).length})</option>`).join('')}</select>
    <select onchange="V.doc.tag=this.value;V.doc.id='';go()"><option value="">All tags</option>${counts(S.rows, r => r.tags).map(([t, c]) => `<option value="${esc(t)}" ${t===v.tag?'selected':''}>${esc(t)} (${c})</option>`).join('')}</select>
    ${dirty(v, D0()) ? `<button class=mini onclick="Object.assign(V.doc,D0());go()">Reset view</button>` : ''}${v.id ? ' <b class=why>Showing one document.</b>' : ''}
    <span class=mut>${list.length} of ${S.rows.length} · net EUR ${money(sum('net_eur'))} · VAT EUR ${money(sum('vat'))} (duplicates not counted)</span></div>
  <div class=scroll><table><tr>${th('doc','invoice_date','Date')}${th('doc','supplier','Supplier / customer')}${th('doc','doc_type','Type')}${th('doc','gross_eur','Gross','n')}${th('doc','net_eur','Net EUR','n')}${th('doc','vat','VAT EUR','n')}${th('doc','treatment','Treatment')}${th('doc','tags','Tags')}${th('doc','payment','Payment')}<th>Files</tr>
  <tr class=f>${fin('doc','invoice_date','2026-08')}${fin('doc','supplier')}${fin('doc','doc_type')}${fin('doc','gross_eur','>100')}${fin('doc','net_eur')}${fin('doc','vat')}${fin('doc','treatment')}${fin('doc','tags')}${fin('doc','payment')}<td></tr>
  ${list.map(r => `<tr><td class=d>${esc(r.invoice_date)}<td><b>${esc(r.supplier)}</b><div class="mut small">${esc(r.invoice_no)}</div><td>${esc(nice(r.doc_type))}<td class=n>${esc(r.currency)} ${money(r.gross)}<td class=n>${money(r.net_eur)}
    <td class=n>${money(vatOf(r))}<td>${esc(nice(r.treatment))}${r.needs_review?' <span class=why title="In the review queue">*</span>':''}<div class="mut small">${esc(r.reason)}</div>
    <td>${r.tags.map(t => chip('doc', t, v.tag)).join('')}${r.flags.map(t => `<span class="tag q" title="${esc(r.review_reason)}">${esc(t)}</span>`).join('')}<td class=small>${esc(r.payment)}<td>${links(r)}</tr>`).join('')}</table></div>
  <p class=mut>Click a column to sort, a tag to filter, or type in the box under a column (amounts also take &gt;100 or &lt;20). Box tags say where a document is counted in the return. Orange tags and * are open questions (Review tab).</p>`;
}
function ret(){
  const f = S.form, filed = S.filed, cell = b => { const d = filed && Math.abs((filed[b] ?? 0) - f[b]) > 0.004;
    return `<span class=box><i>${b}</i> <b>${money(f[b])}</b>${filed ? (d ? ` <span class=bad>filed ${money(filed[b])}</span>` : ' <span class=ok>=</span>') : ''}${NOT.includes(b)?' <span class=mut>·</span>':''}</span>`; };
  let h = title('VAT return') + `<p class=mut>VAT FORM 003/2011, in EUR. Working paper: check every figure on the MTCA portal form before you submit there yourself. Boxes marked · are not calculated by this tool (goods for re-sale, capital goods, purchases at other rates, adjustments) and stay 0.00.</p>` + S.form_notes.map(x => `<p class=why>${esc(x)}</p>`).join('');
  if (!S.built) h += '<p class=why><b>This quarter is not built yet: every box is 0.00. Use Build on the Overview tab.</b></p>';
  if (filed) h += `<p class="${S.diff.length?'bad':'ok'}"><b>${S.diff.length ? 'Differs from the filed return in box ' + S.diff.join(', ') : 'Every box equals the filed return.'}</b></p>`;
  for (const [sec, lines] of FORM) h += `<h2>${sec}</h2><div class=scroll><table class=form>` + lines.map(([l, bs]) => `<tr><td style="width:46%">${esc(l)}<td>${bs.map(cell).join('')}</tr>`).join('') + '</table></div>';
  h += '<h2>Checks</h2><div class=card><ul class=todo>' + S.checks.map(c => `<li class="${c[1] ? 'done' : ''}"><span class=dot>${c[1] ? '✓' : '!'}</span><span class="${c[1] ? '' : 'bad'}">${esc(c[0])}${c[1] || !c[2] ? '' : ' (' + esc(c[2]) + ')'}</span></li>`).join('') + '</ul></div>';
  return h;
}
async function save(){
  const documents = {}, links = {}, ignored = [];
  for (const [id, a] of Object.entries(E)) documents[id] = a.v ? (a.v === 'accept' ? {accepted: true, note: a.note} : {treatment: a.v, accepted: true, note: a.note}) : null;
  for (const [id, p] of Object.entries(P)) p.ignore ? ignored.push(id) : links[id] = p.doc;
  const res = await post('/save?q=' + q(), JSON.stringify({documents, ignored, links}), {'Content-Type': 'application/json'});
  if (!res.ok) document.getElementById('msg').textContent = 'Failed: ' + await res.text(); else { E = {}; P = {}; sel.clear(); psel.clear(); job(); }
}
async function upload(kind, input, pid){
  const files = [...input.files]; if (!files.length) return;
  NOTE = [];
  for (const f of files){ const res = await post(`/upload?kind=${kind}&q=${q()}&name=${encodeURIComponent(f.name)}`, f, pid ? {'X-Payment': encodeURIComponent(pid)} : {}); NOTE.push([res.ok, await res.text()]); }
  input.value = '';
  if (NOTE.some(x => x[0])) { const res = await post(`/run?step=final&q=${q()}`, ''); if (!res.ok) NOTE.push([false, 'Saved, but the build did not start: ' + await res.text()]); }
  if (pid) { delete P[pid]; alert(NOTE.map(x => (x[0] ? 'OK: ' : 'FAILED: ') + x[1]).join(NL)); }
  await load(q(), true);
}
document.getElementById('up_p').onchange = e => upload(payKind, e.target, payFor);
async function run(step){
  if (step === 'gmail' && !confirm(`Scan Gmail for ${qname(S.label)}? The current ${S.label} folder is archived and rebuilt from the mailboxes.`)) return;
  const res = await post(`/run?step=${step}&q=${q()}`, ''); if (!res.ok) alert(await res.text()); job();
}
let timer = null;
async function job(){
  const j = await (await fetch('/api/job')).json(), el = document.getElementById('job'), log = document.getElementById('log'), was = S && S.job.running;
  el.textContent = j.running ? `Running: ${j.name}...` : (j.ok === false ? `Last run failed: ${j.name} (see the log on the Overview tab)` : S && S.activity.length ? 'Last action: ' + S.activity[0].time.slice(11) + ' ' + S.activity[0].text.slice(0, 90) : '');
  el.className = j.ok === false && !j.running ? 'bad' : 'mut';
  if (log && j.log.length) { log.textContent = j.log.slice(-60).join(NL); log.scrollTop = log.scrollHeight; }
  if (S) S.job = j;
  lock(); clearTimeout(timer);
  if (j.running) timer = setTimeout(job, 1500); else if (was) load(q(), true);
}
document.addEventListener('click', e => {
  const t = e.target.closest('[data-tag]'), d = e.target.closest('[data-doc]'), u = e.target.closest('[data-up]');
  if (t) { const v = t.dataset.view === 'doc' ? V.doc : V.rev, k = t.dataset.view === 'doc' ? 'tag' : 'flag'; v[k] = v[k] === t.dataset.tag ? '' : t.dataset.tag; v.id = ''; go(); }
  else if (d) { e.preventDefault(); Object.assign(V.doc, D0(), {id: d.dataset.doc}); tab = 'documents'; go(); }
  else if (u) { payFor = u.dataset.up; payKind = u.dataset.kind || 'receipts'; document.getElementById('up_p').click(); }
  else if (e.target.closest('a,select,button,option,input:not([type=checkbox])')) return;
  else if (e.target.closest('tr[data-g]')) { const k = e.target.closest('tr[data-g]').dataset.g, ids = G[k] || [];
    if (e.target.classList.contains('gcb')) { const on = !ids.every(i => sel.has(i)); ids.forEach(i => on ? sel.add(i) : sel.delete(i)); }
    else openG.has(k) ? openG.delete(k) : openG.add(k);
    draw(); }
  else if (e.target.closest('tr[data-id]')) { last = pick(e, e.target.closest('tr[data-id]').dataset.id, sel, vis, last); draw(); }
  else if (e.target.closest('tr[data-pid]')) { plast = pick(e, e.target.closest('tr[data-pid]').dataset.pid, psel, pvis, plast); draw(); }
});
// a click selects or unselects the row; with Shift, every row between the last clicked one and this one gets the same state
function pick(e, id, set, order, from){
  const on = !set.has(id), a = order.indexOf(from), b = order.indexOf(id);
  if (e.shiftKey && a >= 0 && b >= 0) { order.slice(Math.min(a, b), Math.max(a, b) + 1).forEach(i => on ? set.add(i) : set.delete(i)); window.getSelection().removeAllRanges(); }
  else on ? set.add(id) : set.delete(id);
  return id;
}
document.addEventListener('change', e => {
  const el = e.target, row = el.closest('tr[data-id]'), pay = el.closest('tr[data-pid]');
  if (row) { const id = row.dataset.id;
    if (el.classList.contains('cb')) return;
    else E[id] = {v: row.querySelector('.tr').value, note: row.querySelector('.note').value.trim()};
    tick(); }
  else if (pay) { const id = pay.dataset.pid;
    if (el.classList.contains('pcb')) return;
    if (el.classList.contains('pa')) { if (!el.value) delete P[id]; else P[id] = el.value === 'ignore' ? {ignore: true} : {doc: el.value}; }
    if (el.classList.contains('pl')) { const r = S.rows.find(r => docLabel(r) === el.value); if (!r) return; P[id] = {doc: r.doc_id}; }
    draw(); }
});
// the tab, quarter, filter and sort live in the address, so Back and Forward work and a view can be bookmarked
const dirty = (v, d) => Object.keys(d).some(k => v[k] !== d[k]);
function route(push){
  const v = tab === 'documents' ? V.doc : tab === 'review' ? V.rev : null, d = tab === 'documents' ? D0() : R0(), p = new URLSearchParams({quarter: S.label});
  if (v) for (const k in d) if (v[k] !== d[k]) p.set(k, v[k]);
  const h = '#' + tab + '?' + p;
  if (h !== location.hash) history[push ? 'pushState' : 'replaceState'](null, '', h);
}
function go(push = true){ route(push); draw(); }
function readHash(){
  const [t, qs] = location.hash.slice(1).split('?'), p = new URLSearchParams(qs || '');
  tab = t || 'overview'; Object.assign(V.doc, D0()); Object.assign(V.rev, R0());
  const v = tab === 'documents' ? V.doc : tab === 'review' ? V.rev : null;
  if (v) for (const [k, x] of p) if (k in v) v[k] = k === 'dir' ? Number(x) : x;
  return p.get('quarter');
}
window.onpopstate = window.onhashchange = () => { const want = readHash(); if (!S) return; want && want !== S.label ? load(want) : draw(); };
load(readHash()).then(() => route(false));
</script>"""

JOB = {'name': '', 'running': False, 'ok': None, 'log': []}


def start_job(name: str, fn, after=None) -> bool:
    """Run one long step in the background (one at a time); its log lines are shown on the page. after(ok) runs at the end."""
    if JOB['running']:
        return False
    JOB.update(name=name, running=True, ok=None, log=[])

    class Tail(logging.Handler):
        def emit(self, record):
            JOB['log'].append(f'{record.levelname[:4]} {record.getMessage()}'[:300])
            del JOB['log'][:-400]

    def work():
        handler = Tail()
        logging.getLogger().addHandler(handler)
        try:
            fn()
            JOB['ok'] = True
        except BaseException as e:  # SystemExit from a missing credential included
            logger.error('%s failed: %s', name, e)
            JOB['ok'] = False
        finally:
            logging.getLogger().removeHandler(handler)
            try:
                after and after(JOB['ok'])
            finally:
                JOB['running'] = False

    import threading
    threading.Thread(target=work, daemon=True).start()
    return True


def log_activity(out: Path, text: str, label: str = '') -> None:
    """One line in output/activity.json: what was last done in the portal (uploads, answers, runs). The last 300 are kept."""
    path = out / 'activity.json'
    try:
        items = json.loads(path.read_text())
    except (OSError, ValueError):
        items = []
    items.append({'time': datetime.now().strftime('%Y-%m-%d %H:%M'), 'quarter': label, 'text': str(text)[:300]})
    _write_atomic(path, json.dumps(items[-300:], indent=1).encode())


def save_answers(out: Path, documents: dict, ignored: list, links: dict | None = None) -> None:
    """Merge the page's answers into output/review_answers.yml (owned by the tool; decisions.yml is never written)."""
    import yaml
    path = out / 'review_answers.yml'
    ans = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    docs = ans.get('documents') or {}
    for key, a in documents.items():
        if a is None:
            docs.pop(key, None)
        elif a.get('treatment') in REVIEW_CHOICES + SALE_TREATMENTS + (None,):
            docs[key] = {k: v for k, v in (('treatment', a.get('treatment')), ('accepted', True), ('note', str(a.get('note') or '')[:300])) if v}
    ans['documents'] = docs
    ans['ignored_payments'] = sorted(set(ans.get('ignored_payments') or []) | {str(x) for x in ignored})
    ans['payment_links'] = {**(ans.get('payment_links') or {}), **{str(k): str(v) for k, v in (links or {}).items() if v}}
    path.write_text('# Written by the portal (bookkeeper.py --portal). Keyed by doc_id.\n' + yaml.safe_dump(ans, allow_unicode=True, sort_keys=True))


BOX_TAG = {'domestic_input': 'Box 31/37', 'oss_risk': 'Box 31/37', 'reverse_charge_eu': 'Box 3/6', 'reverse_charge_non_eu': 'Box 4/7'}
FLAGS = (('taxi', r'taxi ride'), ('OSS', r'\bOSS\b'), ('no payment', r'no payment found'), ('buyer VAT no.', r'buyer VAT number not'), ('low confidence', r'^confidence'),
         ('mixed use', r'mixed use'), ('VIES', r'VIES'), ('same amount', r'same amount'), ('business use?', r'candidate|business use|business purpose'),
         ('VAT amount', r'\bVAT\b|net \+|total'), ('file', r'\bfile\b|photo|image'))


def row_tags(r: dict) -> tuple[list[str], list[str]]:
    """What a document belongs to, for the portal's filters. tags: where it is counted in the return, expense category,
    where it came from, whether a payment was found. flags: one short word per open question (from review_reason)."""
    pay, files = r.get('payment') or '', str(r.get('source_file') or '').lower()
    source = {'bolt_csv': 'Bolt report', 'manual': 'receipt photo' if files.endswith(('.jpg', '.jpeg', '.png')) else 'added by hand'}.get(r.get('source'), 'email')
    box = BOX_TAG.get(r['treatment'], 'not in the return')
    if is_sale(r):
        t = r['treatment']
        box = 'Box ' + (SALE_BOX[t] if t in SALE_BOX else '/'.join(SALE_RATE_BOX.get(r.get('rate_pct'), ('?',)))) if t in SALE_BOX or t == 'sale_domestic' else 'not in the return'
        source = 'sales list' if r.get('source') == 'sales_csv' else 'sales invoice file'
    tags = [box, 'sale' if is_sale(r) else '', str(r.get('category') or '').replace('_', ' '), source,
            'paid' if re.match(r'\d{4}', pay) else 'no payment' if pay.startswith('no payment') else '']
    flags = []
    for part in filter(None, str(r.get('review_reason') or '').split('; ')) if r.get('needs_review') else ():
        flag = next((name for name, rx in FLAGS if re.search(rx, part)), 'other')
        if flag not in flags:
            flags.append(flag)
    return [t for t in tags if t], flags


def quarter_of(day: str) -> str:
    return f'{day[:4]}-Q{(int(day[5:7]) - 1) // 3 + 1}'


_MANUAL = {'mtime': 0.0, 'dates': {}}


def manual_dates(out: Path) -> dict:
    """ledger key -> date of every hand-added file that was read (cached until the ledger changes)."""
    path = out / 'ledger.json'
    mtime = path.stat().st_mtime if path.exists() else 0.0
    if mtime != _MANUAL['mtime']:
        _MANUAL.update(mtime=mtime, dates={k: str(v.get('date') or '') for k, v in json.loads(path.read_text()).items() if k.startswith('manual:')} if mtime else {})
    return _MANUAL['dates']


def upload_status(out: Path, label: str, expenses: Path, data: dict, built_at: float) -> tuple[list[dict], int, list[dict]]:
    """What became of every file the owner added: (files under expenses/ for this quarter, how many belong to other
    quarters, statement exports). state: ok, review (read, has an open question), new (not built yet), bad (not readable)."""
    by_id, dates, files, other = {r['doc_id']: r for r in data['rows']}, manual_dates(out), [], 0
    for path in sorted(expenses.rglob('*')) if expenses.is_dir() else []:
        if not path.is_file() or path.suffix.lower() not in MANUAL_EXT or path.name.startswith('.'):
            continue
        key = f'manual:{path.relative_to(expenses)}'
        r, name = by_id.get(key), str(path.relative_to(expenses))
        if r:
            dup = f', same as {r["duplicate_of"]}' if r.get('duplicate_of') else ''
            files.append({'file': name, 'doc_id': key, 'state': 'review' if r['needs_review'] and r['treatment'] != 'duplicate' else 'ok',
                          'status': f'{r["supplier"]} · {r["invoice_date"]} · {r["currency"]} {fmt(r["gross"])} · {r["treatment"].replace("_", " ")}{dup}'
                                    + (f' · question: {r["review_reason"]}' if r['needs_review'] and r['treatment'] != 'duplicate' else '')})
        elif key in dates:
            if valid_date(dates[key]) and quarter_of(dates[key]) == label:
                files.append({'file': name, 'state': 'new', 'status': f'read (dated {dates[key]}), not in the table yet: press Build'})
            else:
                other += 1
        elif path.stat().st_mtime > built_at:
            files.append({'file': name, 'state': 'new', 'status': 'not read yet: press Build'})
        else:
            files.append({'file': name, 'state': 'bad', 'status': 'not read as an invoice or receipt at the last build (see the log); check the file, or enter it by hand'})
    stats, folder, statements = {s['file']: s for s in data.get('statements') or []}, out / 'statements', []
    for path in sorted(folder.rglob('*')) if folder.is_dir() else []:
        if not path.is_file() or path.name.startswith('.'):
            continue
        s = stats.get(path.name)
        if path.suffix.lower() != '.csv':
            statements.append({'file': path.name, 'state': 'bad', 'status': 'not read: only CSV exports are read, not PDF or Excel'})
        elif path.stat().st_mtime > built_at or s is None:
            statements.append({'file': path.name, 'state': 'new', 'status': 'not matched yet: press Build'})
        elif not s['transactions']:
            statements.append({'file': path.name, 'state': 'bad', 'status': 'no transactions read: not a statement export, or all of it is already in another file'})
        else:
            statements.append({'file': path.name, 'state': 'review' if s['unmatched'] else 'ok', 'from': s['from'], 'to': s['to'],
                               'status': f'{s["transactions"]} transactions, {s["from"]} to {s["to"]} · {s["matched"]} matched to a document'
                                         + (f' · {s["unmatched"]} outgoing without a document (Review tab)' if s['unmatched'] else '')})
    return files, other, statements


def sales_status(out: Path, data: dict, built_at: float) -> list[dict]:
    """What became of every file under output/sales/ (lists and invoice files), for the Overview tab."""
    folder, by_id, files = out / 'sales', {r['doc_id']: r for r in data['rows']}, []
    for path in sorted(folder.rglob('*')) if folder.is_dir() else []:
        if not path.is_file() or path.name.startswith('.') or path.suffix.lower() not in MANUAL_EXT | {'.csv'}:
            continue
        name = str(path.relative_to(folder))
        if path.stat().st_mtime > built_at:
            files.append({'file': name, 'state': 'new', 'status': 'not processed yet: press Build'})
        elif path.suffix.lower() == '.csv':
            mine = [r for r in data['rows'] if r['doc_id'].startswith(f'sale:{name}#')]
            files.append({'file': name, 'state': 'review' if any(r['needs_review'] for r in mine) else 'ok',
                          'status': f'{len(mine)} invoice(s) in this quarter' + (f', {sum(r["needs_review"] for r in mine)} with a question' if any(r['needs_review'] for r in mine) else '')})
        else:
            r = by_id.get(f'sale:{name}') or next((x for x in data['rows'] if x.get('direction') == 'sale' and f'sales/{slug(path.stem, 80)}' in str(x.get('source_file'))), None)
            files.append({'file': name, 'doc_id': r['doc_id'], 'state': 'review' if r['needs_review'] else 'ok',
                          'status': f'{r["supplier"]} · {r["invoice_date"]} · {r["currency"]} {fmt(r["gross"])} · {r["treatment"].replace("_", " ")}'} if r else
                         {'file': name, 'state': 'ok', 'status': 'another quarter, or not an issued invoice'})
    return files


def todo_list(s: dict, period) -> list[dict]:
    """What is still needed for the quarter, derived from the state: {done, text, tab to open | run a build}."""
    rows, lo, hi = s['rows'], period[0].isoformat(), period[1].isoformat()
    waiting = [u for u in s['uploads'] + s['statements'] + s['sales_files'] if u['state'] == 'new']
    bad = [u for u in s['uploads'] + s['statements'] if u['state'] == 'bad']
    open_ = sum(r['needs_review'] and r['treatment'] not in ('duplicate', 'pre_registration', 'not_a_purchase', 'not_a_sale') for r in rows)
    todo = []
    if not s['built']:
        todo.append({'done': False, 'run': True, 'text': 'Build the quarter (scan Gmail first if the mail was never downloaded for it)'})
    elif waiting:
        todo.append({'done': False, 'run': True, 'text': f'{len(waiting)} added file(s) are not processed yet: ' + ', '.join(u['file'] for u in waiting[:4])})
    else:
        todo.append({'done': True, 'text': f'Built {s["built_at"]}: {len(rows)} documents'})
    if bad:
        todo.append({'done': False, 'text': f'{len(bad)} added file(s) could not be read: ' + ', '.join(u['file'] for u in bad[:4])})
    spans = [(u['from'], u['to']) for u in s['statements'] if u.get('from')]
    if not s['statements']:
        todo.append({'done': False, 'text': 'Add the bank and card statements for the quarter (CSV export), so every document is checked against a payment'})
    elif spans:
        first, last = min(a for a, _ in spans), max(b for _, b in spans)
        short = first > (period[0] + timedelta(days=5)).isoformat() or last < (period[1] - timedelta(days=5)).isoformat()
        todo.append({'done': not short, 'text': f'Statements cover {first} to {last}' + (f'; the quarter runs {lo} to {hi}: add the missing export' if short else ' (the whole quarter)')})
    if s['built']:
        todo.append({'done': not open_, 'tab': 'review', 'text': f'Answer {open_} open question(s) on documents' if open_ else 'No open questions on documents'})
        if s['statements']:
            n = len(s['unpaid'])
            todo.append({'done': not n, 'tab': 'review', 'text': f'{n} payment(s) have no document: upload the invoice, link it, or mark it as needing none' if n else 'Every outgoing payment has a document or an answer'})
            n = len(s['unbilled'])
            if n or any(is_sale(r) for r in rows):
                todo.append({'done': not n, 'tab': 'review', 'text': f'{n} incoming payment(s) have no sales invoice: add the invoice, link it, or mark it as not a sale' if n else 'Every incoming payment has a sales invoice or an answer'})
        gaps = sales_gaps(rows)
        if gaps:
            todo.append({'done': False, 'tab': 'documents', 'text': 'Sales invoice numbers missing from the series: ' + ', '.join(gaps)})
        fails = [c[0] for c in s['checks'] if not c[1]]
        todo.append({'done': not fails, 'tab': 'return', 'text': 'Check failed: ' + '; '.join(fails) if fails else 'All checks on the figures pass'})
        if s['filed']:
            todo.append({'done': not s['diff'], 'tab': 'return', 'text': 'The figures differ from the filed return in box ' + ', '.join(s['diff']) if s['diff'] else 'Filed: every box equals the filed return'})
        elif date.today() > period[1]:
            todo.append({'done': False, 'tab': 'return', 'text': 'File the return yourself on the MTCA portal, then record the filed figures in output/filed.json'})
    return todo


def portal_state(out: Path, label: str, expenses: Path) -> dict:
    """Everything the page shows for one quarter, from the files the last build wrote."""
    import yaml
    qdir = out / label
    built = (qdir / '.review.json').exists()
    data = json.loads((qdir / '.review.json').read_text()) if built else {'rows': [], 'unpaid': []}
    built_at = (qdir / '.review.json').stat().st_mtime if built else 0.0
    for r in data['rows']:
        r['tags'], r['flags'] = row_tags(r)
    apath, fpath, period = out / 'review_answers.yml', out / 'filed.json', quarter_range(label)
    answers = ((yaml.safe_load(apath.read_text()) or {}).get('documents') or {}) if apath.exists() else {}
    filed = (json.loads(fpath.read_text()).get(label) if fpath.exists() else None) or None
    brought, bf_note = brought_forward(out, label)
    form = return_form(data['rows'], brought)
    today, recent = date.today(), set()
    for i in range(5):  # this quarter and the four before it can always be picked
        m = today.year * 12 + today.month - 1 - 3 * i
        recent.add(f'{m // 12}-Q{m % 12 // 3 + 1}')
    quarters = sorted({d.name for d in out.iterdir() if d.is_dir() and re.fullmatch(r'\d{4}-Q[1-4]', d.name)} | recent | {label}, reverse=True)
    names = (f'{label}_VAT_worksheet.md', f'{label}_claimable.pdf', f'{label}_noVAT.pdf', f'{label}_sales.pdf', f'{label}_recapitulative_statement.csv', 'summary.csv', 'summary_by_vendor.csv')
    uploads, other, statements = upload_status(out, label, expenses, data, built_at)
    try:
        activity = json.loads((out / 'activity.json').read_text())[::-1][:15]
    except (OSError, ValueError):
        activity = []
    s = {'label': label, 'period': [period[0].isoformat(), period[1].isoformat()], 'quarters': quarters, 'built': built,
         'built_at': datetime.fromtimestamp(built_at).strftime('%Y-%m-%d %H:%M') if built else '', 'rows': data['rows'], 'unpaid': data['unpaid'],
         'unbilled': data.get('unbilled') or [], 'sales_files': sales_status(out, data, built_at), 'form_notes': [bf_note] if bf_note else [],
         'answers': answers, 'form': form, 'filed': filed, 'diff': [b for b in form if filed and abs((filed.get(b) or 0) - form[b]) > 0.004],
         'checks': table_checks(data['rows'], label) if built else [], 'has_key': ai_ready(),
         'uploads': uploads, 'other_quarters': other, 'statements': statements, 'activity': activity,
         'downloads': [n for n in names if (qdir / n).exists()], 'job': JOB}
    s['todo'] = todo_list(s, period)
    return s


def serve_portal(out: Path, label: str, expenses: Path, port: int) -> None:
    """The local portal, on 127.0.0.1 only. It reads what the builds wrote, takes uploads (statement CSVs, invoices and
    receipt photos), starts the Gmail scan or the build as a background job, and stores the review answers.
    Every POST needs the per-run token; files are served only from inside a quarter folder. It never submits anything."""
    import secrets
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs
    token = secrets.token_urlsafe(16)
    js = lambda x: json.dumps(x).replace('</', '<\\/')
    page = (PORTAL_PAGE.replace('__TOKEN__', token).replace('__CHOICES__', js(REVIEW_CHOICES)).replace('__SCHOICES__', js(SALE_TREATMENTS)).replace('__FORM__', js(FORM)).replace('__NOT__', js(FORM_NOT_HANDLED))).encode()
    types = {'.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.txt': 'text/plain; charset=utf-8',
             '.csv': 'text/csv; charset=utf-8', '.md': 'text/plain; charset=utf-8'}

    def gmail(q):
        cmd = [sys.executable, str(Path(__file__).resolve()), '--quarter', q, '--out', str(out), '--expenses', str(expenses)]
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as proc:
            for line in proc.stdout:
                JOB['log'].append(re.sub(r'^[\d: -]+ - (\w+) - \[[^\]]+\] - ', r'\1 ', line.rstrip())[:300])
                del JOB['log'][:-400]
        if proc.returncode:
            raise RuntimeError(f'exit code {proc.returncode}')

    def run(name, q, fn) -> bool:
        def after(ok):
            try:
                d = json.loads((out / q / '.review.json').read_text())
                done = f'{len(d["rows"])} documents, {sum(r["needs_review"] for r in d["rows"])} flagged, {len(d["unpaid"])} payment(s) without a document'
            except (OSError, ValueError):
                done = 'nothing built'
            log_activity(out, f'{name} finished: {done}' if ok else f'{name} FAILED: {(JOB["log"] or ["no log"])[-1]}', q)
        return start_job(name, fn, after)

    class Handler(BaseHTTPRequestHandler):
        def send(self, code, body, ctype='text/plain; charset=utf-8'):
            body = body if isinstance(body, bytes) else str(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def args(self):
            url = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            q = qs.get('q') or label
            return url.path, qs, q if re.fullmatch(r'\d{4}-Q[1-4]', q) else label

        def local(self) -> bool:  # a page on another site must not reach this server through the browser
            return self.headers.get('Host', '').split(':')[0] in ('127.0.0.1', 'localhost')

        def do_GET(self):
            path, qs, q = self.args()
            if not self.local():
                return self.send(403, 'forbidden')
            if path == '/':
                return self.send(200, page, 'text/html; charset=utf-8')
            if path == '/api/state':
                return self.send(200, json.dumps(portal_state(out, q, expenses)), 'application/json')
            if path == '/api/job':
                return self.send(200, json.dumps(JOB), 'application/json')
            qdir = (out / q).resolve()
            target = (qdir / qs.get('p', '')).resolve() if path == '/file' else (qdir / Path(qs.get('f', '')).name) if path == '/download' else None
            if target and qdir in target.parents and target.is_file() and target.suffix.lower() in types:  # only documents inside the quarter folder
                return self.send(200, target.read_bytes(), types[target.suffix.lower()])
            self.send(404, 'not found')

        def do_POST(self):
            path, qs, q = self.args()
            if not self.local() or self.headers.get('X-Token') != token:
                return self.send(403, 'forbidden')
            try:
                size = int(self.headers.get('Content-Length') or 0)
                if size > 25_000_000:
                    return self.send(413, 'file too large (25 MB at most)')
                body = self.rfile.read(size)
                if path == '/save':
                    data = json.loads(body)
                    docs, ignored, links = data.get('documents') or {}, data.get('ignored') or [], data.get('links') or {}
                    save_answers(out, docs, ignored, links)
                    log_activity(out, f'Saved {len(docs)} answer(s) on documents, {len(ignored)} payment(s) marked as needing no document, {len(links)} payment(s) linked', q)
                    ok = run(f'rebuild {q}', q, lambda: run_final(out, q, None, expenses))
                    return self.send(200 if ok else 409, 'ok' if ok else 'answers saved, but another run is in progress: press Build afterwards')
                if path == '/upload':
                    name = Path(qs.get('name', '')).name
                    ext = Path(name).suffix.lower()
                    kind = qs.get('kind')
                    folder, allowed = (out / 'statements', {'.csv'}) if kind == 'statements' else (out / 'sales', MANUAL_EXT | {'.csv'}) if kind == 'sales' else (expenses / 'receipts', MANUAL_EXT)
                    if ext not in allowed or not body:
                        return self.send(400, f'not accepted here: {name}')
                    folder.mkdir(parents=True, exist_ok=True)
                    dest = unique(folder / (slug(Path(name).stem, 80) + ext))
                    dest.write_bytes(body)
                    logger.info('Uploaded %s', dest)
                    if qs.get('kind') == 'statements':
                        got = read_statement(dest)
                        if not got:
                            dest.unlink()
                            log_activity(out, f'Statement refused (not readable): {name}', q)
                            return self.send(400, f'{name}: not read as a statement, nothing saved. It needs a header row with a date column and an amount (or debit / credit) column.')
                        msg = f'{name}: statement with {len(got)} transactions, {min(t["date"] for t in got)} to {max(t["date"] for t in got)}'
                    elif kind == 'sales' and ext == '.csv':
                        got = read_sales_csv(dest)
                        if not got:
                            dest.unlink()
                            log_activity(out, f'Sales list refused (not readable): {name}', q)
                            return self.send(400, f'{name}: not read as a sales list, nothing saved. It needs a header row with invoice number, date and net or total columns.')
                        msg = f'{name}: sales list with {len(got)} invoices, {min(r["date"] for r in got)} to {max(r["date"] for r in got)}'
                    else:
                        base, prefix = (out / 'sales', 'sale') if kind == 'sales' else (expenses, 'manual')
                        msg, pid = f'{name}: saved as {dest.relative_to(base)}', unquote(self.headers.get('X-Payment') or '')
                        if pid:  # uploaded as the invoice of one payment: link the two, whatever amount is read from it
                            save_answers(out, {}, [], {pid: f'{prefix}:{dest.relative_to(base)}'})
                            msg += f', linked to the payment of {pid.split("|")[0]}'
                    log_activity(out, 'Added ' + msg, q)
                    return self.send(200, msg)
                if path == '/run':
                    step = qs.get('step')
                    fn = (lambda: gmail(q)) if step == 'gmail' else (lambda: run_final(out, q, ai_client(), expenses)) if step == 'final' else None
                    if not fn:
                        return self.send(400, 'unknown step')
                    return self.send(200, 'started') if run(f'{"Gmail scan" if step == "gmail" else "build"} {q}', q, fn) else self.send(409, 'another run is in progress')
                self.send(404, 'not found')
            except Exception as e:
                logger.exception('Portal request failed')
                self.send(500, str(e))

        def log_message(self, *a):
            pass

    try:
        server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    except OSError as e:
        logger.error('Port %d is in use (%s): another portal is still running. Stop it with Ctrl-C in its terminal, or start this one with --port %d', port, e, port + 1)
        return
    logger.info('Portal: http://127.0.0.1:%d  (quarter %s, Ctrl-C to stop)', port, label)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--quarter', help='e.g. 2026-Q3 (default: the last completed quarter)')
    ap.add_argument('--all', action='store_true', help='no date filter, all time')
    ap.add_argument('--out', default=str(HERE / 'output'))
    ap.add_argument('--dry-run', action='store_true', help='list matching emails, download nothing')
    ap.add_argument('--ai', action='store_true', help='run the AI on every document (default: only on those the regex pass found no total for)')
    ap.add_argument('--no-ai', action='store_true', help='never call the AI API, regex extraction only')
    ap.add_argument('--account', action='append', choices=ACCOUNTS, help='scan only this mailbox (repeatable; default: all)')
    ap.add_argument('--no-cache', action='store_true', help='ignore output/_cache and download every email again')
    ap.add_argument('--max', type=int, help='process at most N emails')
    ap.add_argument('--triage', action='store_true', help='re-run VAT triage (the AI reads the facts, duplicates, noVAT folders, PDFs) on everything already in the ledger; no Gmail')
    ap.add_argument('--expenses', default=str(HERE / 'output' / 'expenses'), help='folder of hand-collected invoices (Amazon, Google, ...) added to the scope; default output/expenses')
    ap.add_argument('--final', action='store_true', help='no Gmail: take the quarter from the ledger, add the expenses and sales folders, triage, summaries and build the merged PDFs')
    ap.add_argument('--portal', action='store_true', help='open the local portal on http://127.0.0.1:<port>: scan Gmail, add statements and receipts, build, review, and see the VAT return form next to the filed figures')
    ap.add_argument('--review', action='store_true', help='same as --portal')
    ap.add_argument('--port', type=int, default=8765, help='port of the review page')
    ap.add_argument('--html-to-pdf', action='store_true', help='only convert already-saved .html receipts to PDF, then exit (no Gmail)')
    args = ap.parse_args()
    global CACHE_ROOT
    CACHE_ROOT = None if args.no_cache else Path(args.out) / '_cache'

    if args.html_to_pdf:
        out = Path(args.out)
        ledger_path = out / 'ledger.json'
        ledger = json.loads(ledger_path.read_text())
        convert_html_records(list(ledger.values()), out)
        ledger_path.write_text(json.dumps(ledger, indent=2))
        return

    if args.final or args.review or args.portal:
        client = ai_client('--final') if args.final and not args.no_ai else None
        label = args.quarter or last_quarter()
        if args.final:
            run_final(Path(args.out), label, client, Path(args.expenses))
            log_usage()
        if args.review or args.portal:
            serve_portal(Path(args.out), label, Path(args.expenses), args.port)
        return

    if args.triage:
        out = Path(args.out)
        ledger_path = out / 'ledger.json'
        ledger = json.loads(ledger_path.read_text())
        client = None if args.no_ai else ai_client('--triage')
        expand_bolt_reports(ledger, out)
        ledger_path.write_text(json.dumps(ledger, indent=2))
        docs = [r for r in ledger.values() if r.get('is_financial_document')]
        if args.max:
            docs = docs[:args.max]
        rows = triage(docs, out, client, ledger_path, ledger)
        ledger_path.write_text(json.dumps(ledger, indent=2))
        for lab in sorted({r['_rec'].get('folder', 'all') for r in rows}):
            if (out / lab).exists():
                write_summary(out / lab, [r for r in rows if r['_rec'].get('folder', 'all') == lab])
        log_usage()
        return

    label, query, period = 'all', SEARCH, None
    if not args.all:
        label = args.quarter or last_quarter()
        period = quarter_range(label)
        start, end = period
        grace = timedelta(days=GRACE_DAYS)
        query = f'{SEARCH} after:{start - grace:%Y/%m/%d} before:{end + grace + timedelta(days=1):%Y/%m/%d}'
    logger.info('Scope %s, query: %s', label, query)

    src = {}  # ledger key -> (gmail service, raw message id, account). The first account keeps bare ids as keys.
    if not ACCOUNTS:
        sys.exit('ERROR: no mailbox configured. Copy config.example.yml to config.yml and list your Gmail addresses under accounts')
    for acct in args.account or ACCOUNTS:
        svc = gmail_service(acct)
        found = list_message_ids(svc, query)
        logger.info('%s: %d candidate emails', acct, len(found))
        for raw in found:
            src[raw if acct == ACCOUNTS[0] else f'{acct}:{raw}'] = (svc, raw, acct)
    ids = list(src)[:args.max]
    logger.info('%d candidate emails in total', len(ids))

    if args.dry_run:
        for i in ids:
            svc, raw, acct = src[i]
            m = svc.users().messages().get(userId='me', id=raw, format='metadata', metadataHeaders=['From', 'Subject', 'Date']).execute()
            h = headers(m)
            print(f'{acct[:12]:12}  {h.get("date", "")[:16]:16}  {h.get("from", "")[:40]:40}  {h.get("subject", "")[:70]}')
        return

    client = None if args.no_ai else ai_client('--ai' if args.ai else '')
    if not client and not args.no_ai:
        logger.warning('AI not configured (API key or ai_model): regex extraction only, documents without a total will not be read')

    out = Path(args.out)
    qdir = out / label
    ledger_path = out / 'ledger.json'
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    archive_previous(out, label, ledger, period)
    qdir.mkdir(parents=True, exist_ok=True)

    ids_set = set(ids)
    for n, msg_id in enumerate(ids, 1):
        svc, raw_id, acct = src[msg_id]
        if msg_id in ledger:
            adopt_files(ledger[msg_id], out, qdir, period)
            if upgrade_record(ledger[msg_id], out):
                logger.info('Fetched hosted PDF invoice for %s | %s', msg_id, ledger[msg_id].get('vendor'))
            enrich_from_pdf(ledger[msg_id], out)
            continue
        try:
            msg = get_message(svc, acct, raw_id)
            h = headers(msg)
            sent = datetime.fromtimestamp(int(msg['internalDate']) / 1000).date().isoformat()
            meta = {'sender': h.get('from', ''), 'subject': h.get('subject', ''), 'sent': sent}
            body, html = message_body(msg)
            attachments = get_attachments(svc, msg, acct)
            links = invoice_links(html, body)
            if is_noise(meta, attachments):
                info = {'is_financial_document': False, 'document_type': 'other', 'vendor': meta['sender'], 'confidence': 1.0}
            else:
                if not any(ext == '.pdf' for _, ext, _ in attachments):
                    attachments += fetch_hosted_pdfs(links)
                att_text = '\n'.join(pdf_text(d) for _, ext, d in attachments if ext == '.pdf')
                info = extract_heuristic(meta, body, att_text, bool(attachments), bool(links))
                if client and (args.ai or info.get('total_amount') is None):  # regex first; the AI only where it found no total
                    try:
                        info = extract_ai(client, meta, body, att_text, unreadable_file(attachments, att_text))
                        info['ai'] = True
                    except Exception as e:
                        logger.warning('AI extraction failed for %s, keeping regex result: %s', msg_id, e)
        except Exception as e:  # one bad email must not stop the run; it is retried next time
            logger.error('Failed on message %s: %s', msg_id, e)
            continue

        info['date'] = valid_date(info.get('date')) or sent
        if period and not period[0].isoformat() <= info['date'] <= period[1].isoformat():
            logger.info('[%d/%d] out of period (%s), skipped | %s', n, len(ids), info['date'], meta['subject'][:60])
            continue  # inside the grace window but dated outside the quarter; not saved, not in the ledger
        info['needs_review'] = info['is_financial_document'] and bool(info.get('ai')) and (info.get('confidence', 0) < MIN_CONFIDENCE or info.get('total_amount') is None)
        info['files'] = save_files(qdir, info, raw_id, attachments, html, info['needs_review'], body) if info['is_financial_document'] else []
        info['folder'] = label
        info['ai'] = bool(info.get('ai'))
        info['account'] = acct
        info['gmail_link'] = f'https://mail.google.com/mail/u/{"0" if acct == ACCOUNTS[0] else acct}/#all/{raw_id}'
        info['subject'] = meta['subject']
        info['links'] = links
        ledger[msg_id] = info
        expand_bolt_reports(ledger, out, period)
        ledger_path.write_text(json.dumps(ledger, indent=2))
        logger.info('[%d/%d] %s | %s | %s', n, len(ids), 'DOC ' if info['is_financial_document'] else 'skip', info.get('vendor'), meta['subject'][:60])

    records = [r for k, r in ledger.items() if k.split('#')[0] in ids_set and r.get('folder') == label]
    finalize(records, ledger, ledger_path, out, qdir, label, period, client, Path(args.expenses))
    log_usage()


if __name__ == '__main__':
    main()
