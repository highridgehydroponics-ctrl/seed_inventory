#!/home/highridgehydro/hrh-venv/bin/python
"""Offline tests for seed_deliveries.py (fake Gmail + fake AppSheet, no writes).
Run: ~/hrh-venv/bin/python scripts/test_seed_deliveries.py"""
import datetime, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seed_deliveries as sd

sd.STATE_FILE = Path(tempfile.mkdtemp()) / 'state.json'
sd.LOCK_FILE = Path(tempfile.mkdtemp()) / 'lock'
LB = sd.G_PER_LB
fails = []

def check(cond, what):
    print(('PASS ' if cond else 'FAIL ') + what)
    if not cond:
        fails.append(what)

def email(order, items, extra=''):
    return (f'Hey there, delivered! Order details: Order number: {order} Order date: Oct 19, 2026'
            f'------------------------------ Items in this shipment: | {items} | ------------------------------ '
            f'Summary | Total paid | $1.00 | YOU MIGHT ALSO LIKE: Broccoli - Waltham 29 - Microgreens Seeds SHOP NOW {extra}')

# 1. parsing
o, it, er = sd.parse_delivery_text(email('90000003', 'Green Mustard Microgreens Seeds 5 lb Quantity: 2 $45.84'))
check(o == '90000003' and len(it) == 1 and it[0]['qty'] == 2 and abs(it[0]['grams'] - 10 * LB) < 0.01 and not er,
      'Green Mustard 5 lb x 2 = 4,536 g')
o, it, er = sd.parse_delivery_text(email('1', 'Cress - Upland - Microgreens Seeds 5 lb Quantity: 1 $65.00 '
                                         'Arugula Seeds - Astro - Microgreens Seeds 25 Lb Quantity: 1 $384.47 '
                                         'Barnwood Style Planter Aged Quantity: 1 $9.97 '
                                         'Wheat - Hard Red Spring (Organic) - Wheatgrass Seeds 25 lb Bag Quantity: 1 $46.34'))
check(len(it) == 3 and not er, 'multi-item email: 3 seed lines, planter ignored, "25 lb Bag" read')
check(not any('Broccoli' in i['title'] for i in it), 'ads ("you might also like") are not read as items')
o, it, er = sd.parse_delivery_text(email('2', 'Kale - Red Russian - Microgreens Seeds Quantity: 1 $10.00'))
check(not it and er and 'bag size' in er[0], 'missing bag size → flagged, not added')
o, it, er = sd.parse_delivery_text(email('3', 'Kale Microgreens Seeds 5 lb Quantity: 0 $0.00'))
check(not it and er, 'quantity 0 → rejected')
o, it, er = sd.parse_delivery_text(email('4', 'Kale Microgreens Seeds 50 lb Quantity: 3 $1.00'))
check(not it and er and 'too large' in er[0], '150 lb on one line → rejected as unsafe')
o, it, er = sd.parse_delivery_text(email('5', 'Basil Microgreens Seeds 8 oz Quantity: 2 $9.00'))
check(len(it) == 1 and abs(it[0]['grams'] - LB) < 0.01, '8 oz x 2 = 1 lb')

# 2. name lookup
tracked = {'Green Mustard', 'Wasabi', 'Pea Shoots', 'Tendril Pea', 'Broccoli', 'Wheatgrass', 'Arugula', 'Leeks', 'Triton Radish'}
check(sd.match_variety('Mustard - Wasabi - Microgreens Seeds', tracked) == 'Wasabi', 'Mustard - Wasabi → Wasabi')
check(sd.match_variety('Pea - Afila Tendril - Microgreens Seeds', tracked) == 'Tendril Pea', 'Afila Tendril → Tendril Pea')
check(sd.match_variety('Pea - Speckled - Microgreens Seeds', tracked) == 'Pea Shoots', 'Pea - Speckled → Pea Shoots')
check(sd.match_variety('Radish - Triton Purple - Microgreens Seeds', tracked) == 'Triton Radish', 'Triton Purple → Triton Radish')
t2 = tracked | {'Rambo Radish', 'Watercress'}
check(sd.match_variety('Radish - Red Stem - Microgreens Seeds', t2) == 'Triton Radish', 'Radish - Red Stem → Triton Radish (Joe)')
check(sd.match_variety('Radish - Purple - Microgreens Seeds', t2) == 'Rambo Radish', 'Radish - Purple → Rambo Radish (Joe)')
check(sd.match_variety('Radish - Triton Purple - Microgreens Seeds', t2) == 'Triton Radish', 'Triton Purple is Triton, not Rambo')
check(sd.match_variety('Cress - Upland - Microgreens Seeds', t2) == 'Watercress', 'Cress - Upland → Watercress (Joe)')
check(sd.match_variety('Sorrel - Red Veined - Microgreens Seeds', t2) is None, 'unknown product (Sorrel) → not guessed')
check(sd.match_variety('Mustard Seeds - Greenwave - Microgreens', tracked) is None, 'Greenwave mustard ≠ Green Mustard')

# 3. full sweep with a fake Gmail, emails deliberately out of order
D = datetime.datetime
class FakeGmail:
    def __init__(self, msgs): self.msgs = msgs
    def users(self): return self
    def messages(self): return self
    def list(self, **k): self._r = {'messages': [{'id': m} for m in self.msgs]}; return self
    def get(self, id, **k): self._r = self.msgs[id]; return self
    def execute(self): return self._r

def gm(when, body):
    return {'internalDate': str(int(when.timestamp() * 1000)), 'body': body,
            'payload': {'headers': [{'name': 'From', 'value': 'True Leaf <support@trueleafmarket.com>'}]}}

msgs = {   # newest first in the dict on purpose (shuffled vs. delivery order)
    'm3': gm(D(2026, 10, 26, 16, 42), email('90000003', 'Green Mustard Microgreens Seeds 5 lb Quantity: 2 $45.84')),
    'm2': gm(D(2026, 10, 22, 15, 3), email('90000002', 'Broccoli - Waltham 29 - Microgreens Seeds 25 lb Quantity: 1 $166.64')),
    'm4': gm(D(2026, 10, 18, 9, 0), email('90000004', 'Leek Seeds - Carentan - Microgreens 5 Lb Quantity: 1 $1.00')),
    'm1': gm(D(2026, 10, 21, 21, 0), email('90000001', 'Broccoli - Waltham 29 - Microgreens Seeds 25 lb Quantity: 1 $166.64')),
    'm5': gm(D(2026, 10, 20, 9, 0), email('90000005', 'Sorrel - Red Veined - Microgreens Seeds 1 lb Quantity: 1 $1.00 '
                                                   'Mustard - Mizuna Lime Streaks - Microgreen Seeds 1 Lb Quantity: 1 $42.46')),
}
varieties = [{'id': n[:3].lower(), 'name': n, 'sd': '10'} for n in tracked]
vid = {v['name']: v['id'] for v in varieties}
batches = [{'variety': vid['Broccoli'], 'actual_planting_date': '10/22/2026 08:00:00', 'number_of_trays': '20', '_sd': '20'}]
counts = [
    {'variety_id': vid['Broccoli'], 'counted_at': '10/20/2026 15:57:00', 'counted_grams': '4.54', 'entry_type': 'count'},
    {'variety_id': vid['Green Mustard'], 'counted_at': '10/14/2026 09:21:00', 'counted_grams': '680.39', 'entry_type': 'count'},
    {'variety_id': vid['Leeks'], 'counted_at': '10/25/2026 11:10:00', 'counted_grams': '907.18', 'entry_type': 'count'},
]
written, barks = [], []
fake = FakeGmail(msgs)
rep = sd.sweep(fake, lambda m: m['body'], varieties, batches, counts, tracked,
               appsheet_add=written.extend, bark=lambda t, b: barks.append((t, b)), log=lambda m: None)
added = [(a['variety'], a['order']) for a in rep['added']]
check(added == [('Broccoli', '90000001'), ('Broccoli', '90000002'), ('Green Mustard', '90000003')],
      'adds in delivery order regardless of email order: Broccoli x2, then Green Mustard')
b1, b2 = [r for r in written if r['variety_name'] == 'Broccoli']
check(abs(b1['counted_grams'] - (4.54 + 25 * LB)) < 0.02, 'Broccoli bag 1: 4.54 g on hand + 11,340 g')
check(abs(b2['counted_grams'] - (b1['counted_grams'] - 400 + 25 * LB)) < 0.02,
      'Broccoli bag 2 builds on bag 1 minus the 400 g planted in between')
gmr = [r for r in written if r['variety_name'] == 'Green Mustard'][0]
check(abs(gmr['counted_grams'] - (680.39 + 10 * LB)) < 0.02 and gmr['entry_type'] == 'restock'
      and gmr['counted_at'] == '10/26/2026 16:42:00' and 'True Leaf order #90000003' in gmr['note'],
      'Green Mustard row: 680 g + 4,536 g, restock, dated at delivery, order # in note')
check(any(s['variety'] == 'Leeks' and 'count' in s['why'] for s in rep['skipped']),
      'Leeks delivered before its newest count → skipped (count already includes it)')
check(any('Sorrel' in f['what'] for f in rep['flagged']), 'unknown Sorrel flagged for a human')
check(not any('Mizuna' in f['what'] for f in rep['flagged']) and any('ignored' in s['why'] for s in rep['skipped']),
      'Mizuna ignored quietly (not flagged)')
check(len(barks) == 1 and 'Broccoli' in barks[0][1] and 'Sorrel' in barks[0][1] and 'Mizuna' not in barks[0][1],
      'one Bark with adds + Sorrel flag, no Mizuna')
check(rep['delivered_orders'] >= {'90000003', '90000002', '90000001'}, 'delivered order numbers returned')

# 4. second run: nothing added twice, flag not re-sent
written2, barks2 = [], []
rep2 = sd.sweep(fake, lambda m: m['body'], varieties, batches, counts, tracked,
                appsheet_add=written2.extend, bark=lambda t, b: barks2.append((t, b)), log=lambda m: None)
check(not written2 and not rep2['added'], 'second run adds nothing (already recorded)')
check(not barks2, 'second run sends no repeat Bark')

# 5. a bag typed in by hand on the dashboard is not double-counted
hand = [{'variety_id': vid['Green Mustard'], 'counted_at': '10/14/2026 09:21:00', 'counted_grams': '680.39', 'entry_type': 'count'},
        {'variety_id': vid['Green Mustard'], 'counted_at': '10/27/2026 08:00:00', 'counted_grams': '5000',
         'added_grams': str(round(10 * LB, 2)), 'entry_type': 'restock', 'note': 'by hand'}]
act, row, why = sd.plan_restock(vid['Green Mustard'], 'Green Mustard', D(2026, 10, 26, 16, 42), 10 * LB,
                                '90000003', {'size': 5, 'unit': 'lb', 'qty': 2}, hand, {})
check(act == 'skip', f'hand-entered restock within 4 days → skipped ({why})')

# 6. variety with no count yet → flagged, not written
act, row, why = sd.plan_restock('zzz', 'Wasabi', D(2026, 10, 26), 1000, '9', {'size': 1, 'unit': 'lb', 'qty': 1}, [], {})
check(act == 'flag' and row is None, 'no starting count → flagged, not written')

# 7. open orders (ordered, not delivered) — emails deliberately shuffled
def oe(when, subject, order, items, extra=''):
    marker = 'Items purchased:' if 'received' in subject else 'Items in this shipment:'
    return dict(when=when, subject=subject, text=(
        f'Order details: Order number: {order} Order date: Oct 23, 2026 {extra} {marker} | {items} | '
        f'Summary Subtotal $1.00 Shipping - Priority Mail $26.00 | While you wait, check these out... '
        f'Broccoli - Waltham 29 - Microgreens Seeds SHOP NOW'))
LEEK = 'Leek - Large American Flag - Microgreens Seeds 1 lb Quantity: 1 $47.81 ' \
       'Radish Seeds - Microgreen - Sango Purple (Organic) 5 lb Quantity: 1 $147.37'
KALE = 'Kale - Dwarf Vates - Microgreens Seeds 25 lb Quantity: 1 $224.74'
GM = 'Green Mustard Microgreens Seeds 5 lb Quantity: 2 $45.84'
emails = [
    oe(D(2026, 10, 26, 23, 50), 'Your order has been picked up!', '90000011', KALE,
       '● Estimated delivery: Monday, November 2 ●'),
    oe(D(2026, 10, 23, 16, 33), 'We have received your order!', '90000010', LEEK),
    oe(D(2026, 10, 26, 16, 42), 'Your order has been delivered!', '90000012', GM),
    oe(D(2026, 10, 26, 22, 8), 'Your order has been picked up!', '90000010', LEEK, 'Estimated delivery: Awaiting ETA'),
    oe(D(2026, 10, 19, 15, 52), 'We have received your order!', '90000012', GM),
    oe(D(2026, 10, 23, 17, 38), 'Your order is ready to ship!', '90000010', LEEK),
    oe(D(2026, 10, 26, 11, 8), 'We have received your order!', '90000011', KALE),
]
t3 = {'Leeks', 'Rambo Radish', 'Kale', 'Green Mustard', 'Broccoli'}
oo = sd.summarize_orders(emails, t3)
check([o['order'] for o in oo] == ['90000010', '90000011'], 'open orders: delivered one dropped, oldest first')
lk = oo[0]
check(lk['status'] == 'shipped' and lk['eta'] is None and lk['shipping'] == 'Priority Mail',
      'Leek order: shipped, no ETA yet, Priority Mail (newest email wins)')
check([i['variety'] for i in lk['items']] == ['Leeks', 'Rambo Radish'], 'Leek → Leeks, Sango Purple → Rambo Radish (Joe)')
check(oo[1]['eta'] == datetime.date(2026, 11, 2), 'Kale order ETA = Nov 2, 2026')
check(not any(i['variety'] == 'Broccoli' for o in oo for i in o['items']), 'ads in order emails are not items')
check(sd.describe_order_status(lk) == 'shipped Oct 26, no ETA yet (Priority Mail)', 'status text: shipped Oct 26, no ETA yet')
check(sd.describe_order_status(oo[1]).startswith('shipped Oct 26, arriving Mon Nov 2'), 'status text: arriving Mon Nov 2')
by = sd.on_order_by_variety(oo)
check(set(by) == {'Leeks', 'Rambo Radish', 'Kale'} and by['Leeks'][0]['lbs'] == 1.0, 'on order by variety: Leeks 1 lb, Rambo, Kale')
check(sd.parse_eta('Estimated delivery: Saturday, January 2', D(2026, 12, 29)) == datetime.date(2027, 1, 2),
      'ETA across New Year → next year')

print(f"\n{'ALL TESTS PASSED' if not fails else f'{len(fails)} TEST(S) FAILED'}")
sys.exit(1 if fails else 0)
