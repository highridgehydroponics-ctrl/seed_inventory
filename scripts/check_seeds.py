#!/home/highridgehydro/hrh-venv/bin/python
"""
scripts/check_seeds.py — HRH Seed Inventory daily checker

Reads AppSheet data, computes per-variety seed status (same logic as the
dashboard in index.html), and sends a Bark push notification when any
seeds are running low.

Reads credentials from ~/.hrh_seed.env (chmod 600 that file).
Required keys: GC_API_KEY, GR_API_KEY, BARK_KEY
Optional keys: GC_APP_ID, GR_APP_ID (override built-in defaults)

Usage (run with the hrh-venv Python; it has the Google libraries for the
vendor Gmail check, system python3 does not):
  ~/hrh-venv/bin/python scripts/check_seeds.py            # normal run
  ~/hrh-venv/bin/python scripts/check_seeds.py --dry-run  # print results, no push, no log write

Alert thresholds match the dashboard: LOW = below 25% of bag, SOON = run-out
within 14 days. Varieties already showing an Ordered lot are suppressed.
"""

import argparse
import datetime
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seed_deliveries

# ── AppSheet app IDs (same defaults as dashboard; override via env file) ──────
DEFAULT_GC_APP_ID = '0ddb09d1-5e1c-434e-9e6e-ce46a54cd628'   # Growth Controls
DEFAULT_GR_APP_ID = 'fe7f55b7-a021-4f49-a9cb-c5528e8de84e'   # Growth Recipes

G_PER_LB          = 453.592
LOW_PCT            = 25     # % of bag → LOW  (matches dashboard)
SOON_DAYS          = 14     # days to run-out → SOON status chip (matches dashboard)
ALERT_DAYS         = 14     # days to run-out → Bark alert (matches dashboard SOON threshold)
ACTIVE_STATUSES    = frozenset({'In Use', 'On Shelf'})
APPSHEET_BASE      = 'https://api.appsheet.com/api/v2/apps'

HRH_ENV   = Path.home() / '.hrh_env'        # primary — shared HRH credentials file
SEED_ENV  = Path.home() / '.hrh_seed.env'  # fallback — standalone / override file
LOG_FILE  = Path.home() / 'hrh_seed_check.log'


# ── helpers ───────────────────────────────────────────────────────────────────


# ── Seed-vendor Gmail check ───────────────────────────────────────────────────
_SEED_VENDORS = {
    "trueleafmarket.com": "TrueLeaf Market",
    "johnnyseeds.com":    "Johnny's Seeds",
}

def _gmail_service():
    # Build Gmail API service reusing hrh-sweep token. Returns None on failure.
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        # Must run under ~/hrh-venv/bin/python (system python3 lacks Google libs).
        print("WARNING: Google API libraries missing; vendor Gmail check skipped. "
              "Run with /home/highridgehydro/hrh-venv/bin/python", file=sys.stderr)
        return None
    token_path = os.path.expanduser("~/hrh-sweep/.google_token.json")
    if not os.path.exists(token_path):
        return None
    try:
        # No scope override: keep the sweep token's full scope list on refresh.
        creds = Credentials.from_authorized_user_file(token_path)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(token_path, "w") as _f:
                    _f.write(creds.to_json())
            else:
                return None
        return build("gmail", "v1", credentials=creds)
    except Exception:
        return None


def _msg_plain(msg):
    # Extract plain text from a Gmail API message.
    import base64 as _b64
    def _strip(html): return re.sub(r'<[^>]+>', ' ', html)
    def _walk(p):
        mime = p.get("mimeType", "")
        if mime == "text/plain":
            d = p.get("body", {}).get("data", "")
            if d: return _b64.urlsafe_b64decode(d).decode("utf-8", errors="replace")
        elif mime == "text/html":
            d = p.get("body", {}).get("data", "")
            if d: return _strip(_b64.urlsafe_b64decode(d).decode("utf-8", errors="replace"))
        elif mime.startswith("multipart/"):
            for part in p.get("parts", []):
                if part.get("mimeType") == "text/plain":
                    r = _walk(part)
                    if r: return r
            for part in p.get("parts", []):
                r = _walk(part)
                if r: return r
        return ""
    return _walk(msg.get("payload", {}))


_NOT_PENDING_RE = re.compile(r"delivered|picked up|out for delivery", re.I)
_ADS_RE = re.compile(r"YOU MIGHT ALSO LIKE|Growers Also Purchased|while you wait, check these out", re.I)


def tracked_varieties(varieties):
    # Active microgreens the seed check tracks (no mixes, no testing).
    return [v for v in varieties
            if v.get('reserve_1') == 'Microgreen'
            and v.get('mix?') != 'Y'
            and not re.match(r'^testing', v.get('name', ''), re.I)
            and v.get('production_status') == 'ACTIVE']


def gmail_ordered_names(svc, variety_names, delivered_orders=None, details=None):
    # Search Gmail for TrueLeaf / Johnny's order emails (last 90 days).
    # Returns set of variety names that appear in those emails, skipping
    # orders already delivered (delivered_orders = order numbers from the
    # delivery sweep) and delivery-status emails.
    # If a dict is passed as `details`, it is filled with
    # {variety: {vendor, date, subject}} for the matching email (used by Sprout).
    if svc is None:
        return set()
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=90)).strftime("%Y/%m/%d")
    query  = f"(from:trueleafmarket.com OR from:johnnyseeds.com) (subject:order OR subject:ship OR subject:confirm) after:{cutoff}"
    try:
        resp     = svc.users().messages().list(userId="me", q=query, maxResults=30).execute()
        messages = resp.get("messages", [])
    except Exception:
        return set()
    name_lower = [n.lower() for n in variety_names]
    found: set = set()
    for meta in messages:
        try:
            msg = svc.users().messages().get(userId="me", id=meta["id"], format="full").execute()
        except Exception:
            continue
        hdrs   = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        if not any(d in hdrs.get("From", "").lower() for d in _SEED_VENDORS):
            continue
        # A delivered / picked-up / out-for-delivery email is not a pending order.
        if _NOT_PENDING_RE.search(hdrs.get("Subject", "")):
            continue
        body = _msg_plain(msg)
        # Order already delivered (seen by the delivery sweep) → no longer on order.
        if seed_deliveries.order_number(body) in (delivered_orders or set()):
            continue
        # Drop the vendor's "you might also like" ads so they don't count as ordered.
        body = _ADS_RE.split(body)[0]
        search_text = (hdrs.get("Subject", "") + " " + body).lower()
        for i, nl in enumerate(name_lower):
            orig = variety_names[i]
            if orig in found: continue
            words = [w for w in nl.split() if len(w) >= 5]
            if nl in search_text or (words and all(w in search_text for w in words)):
                found.add(orig)
                if details is not None:
                    details[orig] = _email_info(hdrs)
    return found


def _email_info(hdrs):
    from email.utils import parsedate_to_datetime
    frm = hdrs.get("From", "").lower()
    try:
        d = parsedate_to_datetime(hdrs.get("Date", ""))
        when = f"{d.month}/{d.day}/{d.year}"
    except Exception:
        when = "unknown date"
    return {"vendor": next((v for k, v in _SEED_VENDORS.items() if k in frm), ""),
            "date": when, "subject": hdrs.get("Subject", "")}


def _parse_env_file(path):
    result = {}
    with open(path) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def load_env():
    """
    Load credentials, trying ~/.hrh_env first (shared HRH file), then
    ~/.hrh_seed.env (standalone override).  Maps the Pi's key names to the
    internal names GC_API_KEY / GR_API_KEY / BARK_KEY used throughout this script.
    """
    # ~/.hrh_env — primary source (APPSHEET_GROWTH_CONTROLS_KEY, etc.)
    if HRH_ENV.exists():
        creds = _parse_env_file(HRH_ENV)
        os.environ.setdefault('GC_API_KEY',  creds.get('APPSHEET_GROWTH_CONTROLS_KEY', ''))
        os.environ.setdefault('GR_API_KEY',  creds.get('APPSHEET_GROWTH_RECIPES_KEY', ''))
        os.environ.setdefault('BARK_KEY',    creds.get('BARK_DEVICE_KEY', ''))
        os.environ.setdefault('GC_APP_ID',   creds.get('GC_APP_ID', DEFAULT_GC_APP_ID))
        os.environ.setdefault('GR_APP_ID',   creds.get('GR_APP_ID', DEFAULT_GR_APP_ID))

    # ~/.hrh_seed.env — optional override / standalone use
    if SEED_ENV.exists():
        for k, v in _parse_env_file(SEED_ENV).items():
            os.environ.setdefault(k, v)

    if not HRH_ENV.exists() and not SEED_ENV.exists():
        sys.exit(f'Missing {HRH_ENV} (or {SEED_ENV}). One must exist with AppSheet + Bark keys.')


def appsheet(app_id, api_key, table, action='Find', rows=None):
    url = f"{APPSHEET_BASE}/{app_id}/tables/{urllib.parse.quote(table, safe='')}/Action"
    body = json.dumps({
        'Action': action,
        'Properties': {'Locale': 'en-US'},
        'Rows': rows or [],
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        'ApplicationAccessKey': api_key,
        'Content-Type': 'application/json',
    })
    last_err = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                text = resp.read().decode()
            if not text.strip():
                return []
            data = json.loads(text)
            return data if isinstance(data, list) else (data.get('Rows') or [])
        except Exception as e:
            last_err = e
            if attempt < 3:
                import time as _time
                _time.sleep(5 * attempt)
    raise RuntimeError(f"AppSheet {table} failed after 3 attempts: {last_err}")


def parse_dt(s):
    """Parse AppSheet date strings: MM/DD/YYYY or MM/DD/YYYY HH:MM:SS."""
    if not s:
        return None
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?', str(s))
    if not m:
        return None
    return datetime.datetime(int(m[3]), int(m[1]), int(m[2]),
                             int(m[4] or 0), int(m[5] or 0), int(m[6] or 0))


def num(v):
    try:
        return float(str(v or '').replace(',', ''))
    except (ValueError, TypeError):
        return 0.0


def fmt_g(g):
    return f'{round(g):,} g'


def fmt_lb(g):
    return f'{g / G_PER_LB:.2f} lb'


def fmt_date(d):
    """'Oct 5, 2026' — cross-platform, no leading zero."""
    if d is None:
        return '—'
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def fmt_date_short(d):
    """'Oct 5' — for notification body."""
    if d is None:
        return '—'
    return f"{d.strftime('%b')} {d.day}"


# ── core calculation (mirrors dashboard compute()) ────────────────────────────

def compute_variety(v, planted, future, lots, counts, now, today):
    """
    Replicate the dashboard's compute() for one variety.
    planted : list of {'at': datetime, 'g': float}
    future  : list of {'date': datetime, 'g': float}  (already filtered >= today)
    Returns a dict with all display and alert fields.
    """
    sd = num(v.get('sd', 0))

    # Seed used in last 7 and 28 days (mirrors dashboard: now - p.at <= N * DAY)
    used7  = sum(p['g'] for p in planted if (now - p['at']).total_seconds() <= 7 * 86400)
    used28 = sum(p['g'] for p in planted if (now - p['at']).total_seconds() <= 28 * 86400)

    # Future plantings sorted by date (ascending)
    fut       = sorted(future, key=lambda f: f['date'])
    fut_total = sum(f['g'] for f in fut)

    # Active lots (In Use / On Shelf)
    act = [l for l in lots if l.get('status', '') in ACTIVE_STATUSES]

    def lot_date(l):
        return parse_dt(l.get('date_recieved') or l.get('lot # Start')) or datetime.datetime(1970, 1, 1)

    # Most recent active lot with total_lbs > 0  →  reference bag size
    ref_lots = sorted([l for l in act if num(l.get('total_lbs', 0)) > 0], key=lot_date, reverse=True)
    ref_lot  = ref_lots[0] if ref_lots else None

    # seed_counts entries with counted_at + counted_grams set, newest first
    valid_counts = sorted(
        [c for c in counts if parse_dt(c.get('counted_at')) and num(c.get('counted_grams', 0)) > 0],
        key=lambda c: parse_dt(c['counted_at']),
        reverse=True,
    )
    count_row = valid_counts[0] if valid_counts else None
    max_count = max((num(c.get('counted_grams', 0)) for c in valid_counts), default=0.0)

    # Newest restock entry (entry_type='restock', added_grams > 0)
    restocks = sorted(
        [c for c in counts if c.get('entry_type') == 'restock' and num(c.get('added_grams', 0)) > 0],
        key=lambda c: parse_dt(c['counted_at']),
        reverse=True,
    )
    last_restock = restocks[0] if restocks else None

    # Bag size (mirrors dashboard bagG logic exactly)
    bag_g = (num(ref_lot['total_lbs']) * G_PER_LB) if ref_lot else max_count
    if last_restock:
        lr_date = parse_dt(last_restock['counted_at'])
        if not ref_lot or (lr_date and lr_date >= lot_date(ref_lot)):
            bag_g = num(last_restock['added_grams'])

    result = dict(
        id=v.get('id', ''), name=v.get('name', ''), sd=sd,
        used7=used7, used28=used28,
        on_hand=None, lot_g=bag_g, pct=None, trays=None,
        run_out=None, run_note='', status='SETUP', base_at=None,
    )

    if not count_row:
        return result   # no baseline count yet

    base_at = parse_dt(count_row['counted_at'])
    base    = num(count_row['counted_grams'])
    used_since = sum(p['g'] for p in planted if base_at <= p['at'] <= now)
    on_hand    = base - used_since

    result['base_at'] = base_at
    result['on_hand'] = on_hand
    result['pct']     = (on_hand / bag_g * 100) if bag_g > 0 else 0.0
    result['trays']   = (max(0.0, on_hand) / sd) if sd > 0 else None

    if on_hand <= 0:
        result['run_out']  = today          # datetime.date
        result['run_note'] = 'out now'
    else:
        # Walk scheduled future plantings to find exhaustion point
        cum = 0.0
        for f in fut:
            cum += f['g']
            if cum > on_hand:
                d = f['date']
                result['run_out']  = d.date() if isinstance(d, datetime.datetime) else d
                result['run_note'] = 'scheduled plantings'
                break

        if result['run_out'] is None:
            rate = used28 / 28.0 if used28 > 0 else 0.0
            if rate > 0:
                last = fut[-1]['date'] if fut else datetime.datetime(today.year, today.month, today.day)
                last_d = last.date() if isinstance(last, datetime.datetime) else last
                extra  = max(0.0, on_hand - fut_total) / rate
                result['run_out']  = last_d + datetime.timedelta(days=extra)
                result['run_note'] = ('after schedule ends, at recent pace' if fut
                                      else 'at recent pace')
            else:
                result['run_note'] = 'no recent use'

    # Status chip — matches dashboard (SOON_DAYS = 14)
    days = (result['run_out'] - today).days if result['run_out'] else None
    if on_hand <= 0:
        result['status'] = 'OUT'
    elif result['pct'] < LOW_PCT:
        result['status'] = 'LOW'
    elif days is not None and days <= SOON_DAYS:
        result['status'] = 'SOON'
    else:
        result['status'] = 'OK'

    return result


def seed_status(vars_, batches, inv, counts, email_ordered_names, now, today):
    """
    Status for every tracked variety — the ONE place the seed math lives.
    Used by this script (7:20 AM check) and by ~/hrh-ptt/seed-inventory.py (Sprout),
    so both always give the same numbers. Each result also gets:
      days_left, alert (would Bark), ordered (Ordered lot or open order email),
      reasons (why it alerts), trays_on_order_lbs (lbs in 'Ordered' lots).
    """
    by_var = {v['id']: {'v': v, 'planted': [], 'future': []} for v in vars_}
    for b in batches:
        o = by_var.get(b.get('variety'))
        if not o:
            continue
        trays = num(b.get('number_of_trays', 0))
        sd    = num(b.get('_sd', 0)) or num(o['v'].get('sd', 0))
        g     = trays * sd
        if b.get('actual_planting_date'):
            at = parse_dt(b['actual_planting_date'])
            if at:
                o['planted'].append({'at': at, 'g': g})
        else:
            d = parse_dt(b.get('optimal_planting_date'))
            if d and d.date() >= today:
                o['future'].append({'date': d, 'g': g})

    lots_by = {}
    for l in inv:
        lots_by.setdefault(l.get('variety_id', ''), []).append(l)
    counts_by = {}
    for c in counts:
        counts_by.setdefault(c.get('variety_id', ''), []).append(c)

    # variety IDs that already have an 'Ordered' lot → suppress alert
    ordered_ids = {l.get('variety_id') for l in inv if l.get('status') == 'Ordered'}

    results = []
    for v in vars_:
        vid = v['id']
        o   = by_var[vid]
        r   = compute_variety(v, o['planted'], o['future'],
                              lots_by.get(vid, []), counts_by.get(vid, []), now, today)
        days_left = (r['run_out'] - today).days if r['run_out'] else None
        reasons = []
        if r['on_hand'] is not None:
            if r['status'] in ('OUT', 'LOW'):
                reasons.append(f"{r['pct']:.0f}% of bag")
            if days_left is not None and days_left <= ALERT_DAYS:
                reasons.append(f"runs out in {days_left}d")
        r.update(
            days_left=days_left, reasons=reasons, alert=bool(reasons),
            ordered=(vid in ordered_ids or r.get('name', '') in email_ordered_names),
            trays_on_order_lbs=sum(num(l.get('total_lbs')) for l in lots_by.get(vid, [])
                                   if l.get('status') == 'Ordered'),
            used28_trays=(r['used28'] / r['sd']) if r['sd'] else 0.0,
        )
        results.append(r)

    rank = {'OUT': 0, 'LOW': 1, 'SOON': 2, 'OK': 3, 'SETUP': 4}
    results.sort(key=lambda r: (rank[r['status']], r['name'].lower()))
    return results


# ── Bark notification ─────────────────────────────────────────────────────────

def send_bark(bark_key, title, body, dry_run=False):
    if dry_run:
        print('\n[DRY RUN] Would send Bark push notification:')
        print(f'  Title : {title}')
        print('  Body  :')
        for line in body.splitlines():
            print(f'    {line}')
        return
    payload = json.dumps({'title': title, 'body': body, 'device_key': bark_key, 'sound': 'default', 'url': 'https://highridgehydroponics-ctrl.github.io/seed_inventory/', 'url_label': 'Open Dashboard'}).encode()
    req = urllib.request.Request(
        'https://api.day.app/push',
        data=payload,
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode()


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='HRH seed inventory daily checker')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print what would happen; do not send push or write log')
    parser.add_argument('--test-bark', action='store_true',
                        help='Send a clearly labeled TEST notification and exit')
    args = parser.parse_args()

    load_env()

    gc_key   = os.environ.get('GC_API_KEY', '')
    gr_key   = os.environ.get('GR_API_KEY', '')
    bark_key = os.environ.get('BARK_KEY', '')
    gc_id    = os.environ.get('GC_APP_ID', DEFAULT_GC_APP_ID)

    # On unhandled exception, send a Bark alert so cron failures are visible.
    import sys as _sys_hook
    _orig_hook = _sys_hook.excepthook
    def _failure_hook(exc_type, exc_val, tb):
        import traceback as _tb
        if bark_key and not args.dry_run:
            snippet = ''.join(_tb.format_exception(exc_type, exc_val, tb))[-300:]
            try:
                send_bark(bark_key, '❌ Seed check FAILED', f'{exc_val}\n{snippet[-200:]}')
            except Exception:
                pass
        _orig_hook(exc_type, exc_val, tb)
    _sys_hook.excepthook = _failure_hook
    gr_id    = os.environ.get('GR_APP_ID', DEFAULT_GR_APP_ID)

    if not gc_key or not gr_key:
        sys.exit('ERROR: APPSHEET_GROWTH_CONTROLS_KEY / APPSHEET_GROWTH_RECIPES_KEY not found in ~/.hrh_env')
    if not bark_key and not args.dry_run:
        sys.exit('ERROR: BARK_DEVICE_KEY not found in ~/.hrh_env')

    # ── test-bark mode ──────────────────────────────────────────────────────
    if args.test_bark:
        print('Sending TEST Bark notification…')
        result = send_bark(bark_key,
                           'TEST — HRH seed check',
                           'If you see this, the seed checker script is wired up correctly.\n'
                           'No seeds are actually low right now (this is just a connectivity test).')
        print(f'Bark response: {result}')
        return

    now   = datetime.datetime.now()
    today = datetime.date.today()

    # ── fetch data ──────────────────────────────────────────────────────────
    print('Fetching AppSheet data…', flush=True)
    varieties = appsheet(gc_id, gc_key, 'variety')
    batches   = appsheet(gc_id, gc_key, 'batch')
    inv       = appsheet(gr_id, gr_key, 'seed_inventory')
    counts    = appsheet(gr_id, gr_key, 'seed_counts')
    print(f'  varieties={len(varieties)}  batches={len(batches)}'
          f'  inv lots={len(inv)}  count entries={len(counts)}')

    # ── filter to active microgreens (no mixes, no testing) ─────────────────
    vars_ = tracked_varieties(varieties)

    # ── True Leaf delivery sweep: add delivered bags to seed_counts first ────
    gmail_svc = _gmail_service()
    deliv = seed_deliveries.sweep(
        gmail_svc, _msg_plain, varieties, batches, counts,
        {v.get('name', '') for v in vars_},
        appsheet_add=lambda rows: appsheet(gr_id, gr_key, 'seed_counts', 'Add', rows),
        bark=lambda t, b: send_bark(bark_key, t, b),
        dry_run=args.dry_run,
    )
    if deliv['wrote']:
        counts = appsheet(gr_id, gr_key, 'seed_counts')   # re-read so the math uses AppSheet's copy

    # Varieties confirmed ordered via Gmail (not yet delivered)
    _all_names = [v.get('name', '') for v in vars_ if v.get('name')]
    email_ordered_names = gmail_ordered_names(gmail_svc, _all_names, deliv['delivered_orders'])
    if email_ordered_names:
        print(f"Gmail: order email found for: {', '.join(sorted(email_ordered_names))}")

    # ── compute status for every variety (shared with Sprout's seed-inventory.py) ──
    results = seed_status(vars_, batches, inv, counts, email_ordered_names, now, today)

    # ── print results table ──────────────────────────────────────────────────
    W = 100
    print()
    print(f"{'Variety':<22} {'On Hand':>18} {'%Bag':>5} {'Trays':>6}  {'Run-out':<14} {'Status':<8}  Alert / Note")
    print('─' * W)
    for r in results:
        if r['on_hand'] is None:
            oh, pct, tr, ro = '— (no count yet)', '', '—', '—'
            days_left = None
        else:
            oh        = f"{fmt_g(r['on_hand'])} / {fmt_lb(r['on_hand'])}"
            pct       = f"{r['pct']:.0f}%"
            tr        = str(math.floor(r['trays'])) if r['trays'] is not None else '—'
            ro        = fmt_date(r['run_out'])
            days_left = (r['run_out'] - today).days if r['run_out'] else None

        # alert condition: LOW/OUT, or run-out within ALERT_DAYS (see seed_status)
        if r['alert'] and r['ordered']:
            note = '(ordered — suppressed)'
        elif r['alert']:
            note = 'ALERT — ' + ', '.join(r['reasons'])
        else:
            note = ''

        print(f"{r['name']:<22} {oh:>18} {pct:>5} {tr:>6}  {ro:<14} {r['status']:<8}  {note}")

    print('─' * W)

    # ── collect seeds that need a Bark alert ─────────────────────────────────
    alert_seeds = [r for r in results if r['alert'] and not r['ordered']]

    print(f"\n{len(alert_seeds)} seed(s) need attention out of {len(results)} tracked.")

    if alert_seeds:
        lines = []
        for r in alert_seeds:
            why = ', '.join(r['reasons']) if r['reasons'] else r['status']
            oh  = f"{fmt_g(r['on_hand'])} / {fmt_lb(r['on_hand'])}"
            ro  = fmt_date_short(r['run_out'])
            lines.append(f"• {r['name']}: {oh}, run-out {ro} [{why}]")

        title = f"\U0001f331 {len(alert_seeds)} seed{'s' if len(alert_seeds) != 1 else ''} to order"
        body  = '\n'.join(lines)

        send_bark(bark_key, title, body, dry_run=args.dry_run)
        if not args.dry_run:
            print(f"Bark sent: {title}")
    else:
        if args.dry_run:
            print('[DRY RUN] Nothing is low — no notification would be sent.')
        else:
            print('All seeds OK. No notification sent.')

    # ── log one line per run ──────────────────────────────────────────────────
    summary  = ', '.join(f"{r['name']} ({r['status']})" for r in alert_seeds) or 'all OK'
    log_line = f"{now.strftime('%Y-%m-%d %H:%M:%S')}  {len(alert_seeds)} low: {summary}\n"
    if args.dry_run:
        print(f'\n[DRY RUN] Would append to {LOG_FILE}:')
        print(f'  {log_line.rstrip()}')
    else:
        with open(LOG_FILE, 'a') as fh:
            fh.write(log_line)
        print(f'Logged → {LOG_FILE}')


if __name__ == '__main__':
    main()
