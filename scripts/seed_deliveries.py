#!/home/highridgehydro/hrh-venv/bin/python
"""
scripts/seed_deliveries.py — True Leaf Market delivery sweep (no AI, no API credits)

Finds True Leaf Market "Your order has been delivered!" emails in Gmail, reads
each seed product line (name, bag size, quantity), and records the new seed in
the AppSheet seed_counts table exactly like the dashboard's "+ Add seed" button:
  counted_grams = on hand at delivery time + added_grams, entry_type = 'restock'

Johnny's Seeds is ignored on purpose (its delivered emails list no products).

Safety rules (nothing is guessed):
  - Product names map to varieties only through ALIASES below. Unknown seed
    products are flagged (Bark), never written.
  - Bad size / unit / quantity, or an absurd weight, is rejected and flagged.
  - Skipped when the delivery is older than the variety's newest hand count
    (that count already includes the bag), when the same order was already
    recorded (note contains 'True Leaf order #N'), or when a matching restock
    was already typed in by hand within 4 days.
  - Varieties with no starting count yet are flagged, not written.

Used by:
  scripts/check_seeds.py           (7:20 AM cron seed check)
  ~/hrh-ptt/seed-inventory.py      (Sprout's on-demand seed answer)

Standalone test:
  ~/hrh-venv/bin/python scripts/seed_deliveries.py --dry-run   # show what it would do
  ~/hrh-venv/bin/python scripts/seed_deliveries.py             # do it (writes + Bark)
"""

import datetime
import fcntl
import json
import os
import re
import secrets
import sys
from pathlib import Path

G_PER_LB = 453.592
UNIT_G = {'lb': G_PER_LB, 'lbs': G_PER_LB, 'oz': G_PER_LB / 16, 'kg': 1000.0, 'g': 1.0}
MAX_LINE_G = 60 * G_PER_LB          # anything over 60 lb on one line is treated as a parse error
LOOKBACK_DAYS = 45
HAND_ENTRY_WINDOW_DAYS = 4
STATE_FILE = Path.home() / 'seed_inventory' / 'state' / 'deliveries_notified.json'
LOCK_FILE = Path('/tmp/hrh_seed_deliveries.lock')
GMAIL_QUERY = 'from:trueleafmarket.com subject:"has been delivered" newer_than:{d}d'

# True Leaf product-name keywords → AppSheet variety name. First match wins, so
# more specific keywords come first. Add a line here when a new product is flagged.
ALIASES = [
    ('green mustard',        'Green Mustard'),
    ('mustard - wasabi',     'Wasabi'),
    ('wasabi',               'Wasabi'),
    ('afila tendril',        'Tendril Pea'),
    ('tendril',              'Tendril Pea'),
    ('pea - speckled',       'Pea Shoots'),
    ('triton',               'Triton Radish'),
    ('red stem',             'Triton Radish'),     # Joe 10/27/2026: Radish - Red Stem = Triton
    ('rambo',                'Rambo Radish'),
    ('radish - purple',      'Rambo Radish'),      # Joe 10/27/2026
    ('cress - upland',       'Watercress'),        # Joe 10/27/2026
    ('upland cress',         'Watercress'),
    ('broccoli',             'Broccoli'),
    ('wheatgrass',           'Wheatgrass'),
    ('basil',                'Basil'),
    ('beet',                 'Beets'),
    ('leek',                 'Leeks'),
    ('arugula',              'Arugula'),
    ('watercress',           'Watercress'),
    ('kale',                 'Kale'),
    ('cabbage - red',        'Red Cabbage'),
    ('red cabbage',          'Red Cabbage'),
    ('cilantro',             'Cilantro'),
    ('coriander',            'Cilantro'),
    ('borage',               'Borage'),
    ('cantaloupe',           'Cantaloupe'),
]

# Seed products Joe buys but does not track — skipped quietly (no flag, no Bark).
IGNORE = [
    'mizuna',                                      # Joe 10/27/2026
]

ITEM_RE = re.compile(
    r'(?P<title>.+?)\s+(?P<size>\d+(?:\.\d+)?)\s*(?P<unit>lbs?|oz|kg|g)\b(?:\s+bag)?\s*$', re.I)
CHUNK_RE = re.compile(r'(?P<body>.*?)\s*Quantity:\s*(?P<qty>-?\d+)\s+\$[\d,]+\.\d{2}', re.S)


# ── parsing (pure functions, unit-testable) ───────────────────────────────────

def parse_delivery_text(text):
    """Return (order_no, items, errors). items: [{title, size, unit, qty, grams}]"""
    t = re.sub(r'\s+', ' ', text or '')
    m = re.search(r'Order number:\s*#?(\d{5,})', t)
    order_no = m.group(1) if m else None
    a = t.find('Items in this shipment:')
    if a < 0:
        return order_no, [], ['no "Items in this shipment" section']
    b = t.find('Summary', a)
    section = t[a + len('Items in this shipment:'): b if b > 0 else None]
    items, errors = [], []
    for c in CHUNK_RE.finditer(section):
        raw = c.group('body').strip(' |-')
        qty = int(c.group('qty'))
        im = ITEM_RE.match(raw)
        is_seed = re.search(r'seed|microgreen', raw, re.I)
        if not im:
            if is_seed:
                errors.append(f'could not read bag size: "{raw}"')
            continue                                   # non-seed item (planter, trays…)
        unit = im.group('unit').lower()
        size = float(im.group('size'))
        if not is_seed:
            continue
        if qty <= 0 or size <= 0:
            errors.append(f'bad quantity/size: "{raw}" qty {qty}')
            continue
        grams = round(size * UNIT_G[unit] * qty, 2)
        if grams > MAX_LINE_G:
            errors.append(f'weight too large to trust ({grams:,.0f} g): "{raw}" qty {qty}')
            continue
        items.append(dict(title=im.group('title').strip(), size=size, unit=unit, qty=qty, grams=grams))
    if not items and not errors:
        errors.append('no seed products found in shipment')
    return order_no, items, errors


def match_variety(title, tracked_names):
    tl = title.lower()
    for key, name in ALIASES:
        if key in tl:
            return name if name in tracked_names else None
    return None


def parse_dt(s):
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?', str(s or ''))
    if not m:
        return None
    return datetime.datetime(int(m[3]), int(m[1]), int(m[2]), int(m[4] or 0), int(m[5] or 0), int(m[6] or 0))


def _num(v):
    try:
        return float(str(v or '').replace(',', ''))
    except ValueError:
        return 0.0


def fmt_dt(d):
    return d.strftime('%m/%d/%Y %H:%M:%S')


def planted_by_variety(batches, varieties):
    """{variety_id: [(datetime, grams)]} — same math as check_seeds.py / dashboard."""
    sd = {v['id']: _num(v.get('sd')) for v in varieties}
    out = {}
    for b in batches:
        vid = b.get('variety')
        at = parse_dt(b.get('actual_planting_date'))
        if vid not in sd or not at:
            continue
        g = _num(b.get('number_of_trays')) * (_num(b.get('_sd')) or sd[vid])
        out.setdefault(vid, []).append((at, g))
    return out


def plan_restock(vid, name, when, grams, order_no, item, counts, planted):
    """Decide one delivery line. Returns (action, row_or_None, reason)."""
    mine = [c for c in counts if c.get('variety_id') == vid]
    tag = f'True Leaf order #{order_no}'
    if any(tag in (c.get('note') or '') for c in mine):
        return 'skip', None, 'already recorded'
    for c in mine:
        ca = parse_dt(c.get('counted_at'))
        if (c.get('entry_type') == 'restock' and ca
                and 'True Leaf order #' not in (c.get('note') or '')     # our own rows aren't hand entries
                and abs((ca - when).total_seconds()) <= HAND_ENTRY_WINDOW_DAYS * 86400
                and abs(_num(c.get('added_grams')) - grams) <= 0.02 * grams):
            return 'skip', None, f'already added by hand on {ca:%b %-d}'
    valid = sorted([c for c in mine if parse_dt(c.get('counted_at')) and str(c.get('counted_grams', '')).strip() != ''],
                   key=lambda c: parse_dt(c['counted_at']), reverse=True)
    if not valid:
        return 'flag', None, 'no starting count yet — enter a count on the dashboard'
    base_at = parse_dt(valid[0]['counted_at'])
    if when < base_at:
        return 'skip', None, f'included in the {base_at:%b %-d} count'
    used = sum(g for at, g in planted.get(vid, []) if base_at <= at <= when)
    on_hand = _num(valid[0]['counted_grams']) - used
    row = dict(
        id=secrets.token_hex(4), variety_id=vid, variety_name=name, counted_at=fmt_dt(when),
        counted_grams=round(max(0.0, on_hand) + grams, 2), added_grams=round(grams, 2),
        entry_type='restock',
        note=f'Auto: {tag} delivered ({item["size"]:g} {item["unit"]} x {item["qty"]})',
    )
    return 'add', row, f'on hand {max(0.0, on_hand):,.0f} g + {grams:,.0f} g'


# ── Gmail + AppSheet + Bark ───────────────────────────────────────────────────

def fetch_deliveries(svc, msg_text):
    """[{msg_id, when, order_no, items, errors}] oldest first."""
    if svc is None:
        return []
    resp = svc.users().messages().list(userId='me', q=GMAIL_QUERY.format(d=LOOKBACK_DAYS), maxResults=50).execute()
    out = []
    for meta in resp.get('messages', []):
        msg = svc.users().messages().get(userId='me', id=meta['id'], format='full').execute()
        frm = next((h['value'] for h in msg['payload'].get('headers', []) if h['name'] == 'From'), '')
        if 'trueleafmarket.com' not in frm.lower():
            continue
        when = datetime.datetime.fromtimestamp(int(msg['internalDate']) / 1000)
        order_no, items, errors = parse_delivery_text(msg_text(msg))
        out.append(dict(msg_id=meta['id'], when=when, order_no=order_no, items=items, errors=errors))
    return sorted(out, key=lambda d: d['when'])


def _load_state():
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except Exception:
        return set()


def _save_state(seen):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(seen)))


def sweep(svc, msg_text, varieties, batches, counts, tracked_names,
          appsheet_add, bark, dry_run=False, log=print):
    """
    Run the delivery sweep. appsheet_add(rows) writes to seed_counts;
    bark(title, body) sends a push. Returns a report dict:
      {added: [...], skipped: [...], flagged: [...], delivered_orders: set(), wrote: bool}
    `counts` is updated in place with any rows added.
    """
    report = dict(added=[], skipped=[], flagged=[], delivered_orders=set(), wrote=False)
    LOCK_FILE.touch(exist_ok=True)
    with open(LOCK_FILE) as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            deliveries = fetch_deliveries(svc, msg_text)
        except Exception as e:
            report['flagged'].append(dict(what='Gmail', why=f'delivery check failed: {e}'))
            log(f'Delivery sweep: Gmail error: {e}')
            return report
        name_to_id = {v['name']: v['id'] for v in varieties if v.get('name') in tracked_names}
        planted = planted_by_variety(batches, varieties)
        seen = _load_state()
        new_flags = []
        for d in deliveries:
            if d['order_no']:
                report['delivered_orders'].add(d['order_no'])
            label = f"order #{d['order_no'] or '?'} ({d['when']:%b %-d})"
            for err in d['errors']:
                f = dict(what=label, why=err)
                report['flagged'].append(f)
                if d['msg_id'] not in seen:
                    new_flags.append(f)
            if not d['order_no'] and d['items']:
                f = dict(what=label, why='no order number in email — not recorded')
                report['flagged'].append(f)
                if d['msg_id'] not in seen:
                    new_flags.append(f)
                continue
            for it in d['items']:
                name = match_variety(it['title'], tracked_names)
                desc = f"{it['title']} {it['size']:g} {it['unit']} x {it['qty']}"
                if any(k in it['title'].lower() for k in IGNORE):
                    report['skipped'].append(dict(variety=it['title'], grams=it['grams'], order=d['order_no'],
                                                  when=d['when'], why='not tracked (ignored)', item=desc))
                    continue
                if not name:
                    f = dict(what=f'{desc} — {label}', why='product not in the seed lookup list')
                    report['flagged'].append(f)
                    if d['msg_id'] not in seen:
                        new_flags.append(f)
                    continue
                action, row, why = plan_restock(name_to_id[name], name, d['when'], it['grams'],
                                                d['order_no'], it, counts, planted)
                entry = dict(variety=name, grams=it['grams'], order=d['order_no'], when=d['when'], why=why, item=desc)
                if action == 'add':
                    if not dry_run:
                        appsheet_add([row])
                        report['wrote'] = True
                    counts.append(row)          # so a second bag of the same seed builds on this one
                    report['added'].append(entry)
                elif action == 'skip':
                    report['skipped'].append(entry)
                else:
                    f = dict(what=f'{name} — {label}', why=why)
                    report['flagged'].append(f)
                    if d['msg_id'] not in seen:
                        new_flags.append(f)
            seen.add(d['msg_id'])

        for a in report['added']:
            log(f"Delivery added: {a['variety']} +{a['grams']:,.0f} g ({a['grams']/G_PER_LB:.1f} lb), "
                f"True Leaf #{a['order']} delivered {a['when']:%b %-d} [{a['why']}]")
        for s in report['skipped']:
            log(f"Delivery skipped: {s['variety']} #{s['order']} — {s['why']}")
        for f in report['flagged']:
            log(f"Delivery FLAG: {f['what']}: {f['why']}")

        lines = [f"• {a['variety']}: +{a['grams']/G_PER_LB:.1f} lb (True Leaf #{a['order']}, {a['when']:%b %-d})"
                 for a in report['added']]
        lines += [f"⚠ {f['what']}: {f['why']}" for f in new_flags]
        if lines:
            n = len(report['added'])
            title = (f"🌱 Seed delivery added ({n})" if n else "⚠️ Seed delivery needs a look")
            body = '\n'.join(lines)
            if dry_run:
                log(f'[DRY RUN] Would send Bark: {title}\n{body}')
            else:
                try:
                    bark(title, body)
                except Exception as e:
                    log(f'Delivery Bark failed: {e}')
        if not dry_run:
            _save_state(seen)
    return report


def order_number(text):
    """Order number from a True Leaf / Johnny's email body, or None."""
    m = re.search(r'Order (?:number|#)\s*:?\s*#?\s*(\d{6,})', re.sub(r'\s+', ' ', text or ''), re.I)
    return m.group(1) if m else None


# ── standalone run ────────────────────────────────────────────────────────────

def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import check_seeds as cs
    dry = '--dry-run' in sys.argv
    cs.load_env()
    gc, gr, bark_key = os.environ['GC_API_KEY'], os.environ['GR_API_KEY'], os.environ.get('BARK_KEY', '')
    gc_id = os.environ.get('GC_APP_ID', cs.DEFAULT_GC_APP_ID)
    gr_id = os.environ.get('GR_APP_ID', cs.DEFAULT_GR_APP_ID)
    varieties = cs.appsheet(gc_id, gc, 'variety')
    batches = cs.appsheet(gc_id, gc, 'batch')
    counts = cs.appsheet(gr_id, gr, 'seed_counts')
    tracked = {v['name'] for v in cs.tracked_varieties(varieties)}
    rep = sweep(cs._gmail_service(), cs._msg_plain, varieties, batches, counts, tracked,
                appsheet_add=lambda rows: cs.appsheet(gr_id, gr, 'seed_counts', 'Add', rows),
                bark=lambda t, b: cs.send_bark(bark_key, t, b),
                dry_run=dry)
    print(f"{'[DRY RUN] ' if dry else ''}added {len(rep['added'])}, skipped {len(rep['skipped'])}, "
          f"flagged {len(rep['flagged'])}")


if __name__ == '__main__':
    main()
