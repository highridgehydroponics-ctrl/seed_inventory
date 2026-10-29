# HRH Seed Inventory

Live seed-on-hand dashboard for High Ridge Hydroponics microgreen varieties.  
Served on GitHub Pages at the repo's root (`index.html`).  
Data comes from AppSheet ("Growth Controls" and "Growth Recipes" apps) — no data files live in this repo.

---

## Automated daily seed check (`scripts/check_seeds.py`)

A standalone Python 3 script that runs the same seed-inventory calculations as
the dashboard and sends a push notification to your iPhone if any seeds are running low.  
Run it with `~/hrh-venv/bin/python` (the Gmail checks need the Google libraries installed there).

### What it alerts on

A seed triggers an alert if **either** of these is true *and* it has no "Ordered" lot already on its way:

- On hand is **below 25 %** of the most-recent bag's size (LOW / OUT on the dashboard)
- Estimated run-out is **within 14 days** (matches the dashboard's SOON threshold)

### One-time setup on the Pi

**1. Copy the script to the Pi.**  
SSH in and clone (or pull) this repo:

```bash
ssh highridgehydro@hrh-voice.local
cd ~
git clone https://github.com/highridgehydroponics-ctrl/seed_inventory.git
# or, if already cloned:
cd seed_inventory && git pull
```

**2. Credentials — already done if you use `~/.hrh_env`.**  
The script reads credentials from the shared `~/.hrh_env` file that all HRH Pi scripts use.  
It looks for these keys automatically:

| `~/.hrh_env` key | Used for |
|---|---|
| `APPSHEET_GROWTH_CONTROLS_KEY` | Growth Controls AppSheet app |
| `APPSHEET_GROWTH_RECIPES_KEY` | Growth Recipes AppSheet app |
| `BARK_DEVICE_KEY` | Bark iPhone push notifications |

If `~/.hrh_env` is already set up with those keys (it is on the HRH Pi), no extra file is needed.

**Standalone / override:** you can also create `~/.hrh_seed.env` — see
`scripts/hrh_seed.env.example` for the format.  Keys in `~/.hrh_seed.env` take
precedence over `~/.hrh_env`.

**3. Test it first (dry run — no notification sent):**

```bash
~/hrh-venv/bin/python ~/seed_inventory/scripts/check_seeds.py --dry-run
```

This prints the full status table and shows exactly what the notification *would* say.

**4. Send one real test notification:**

```bash
~/hrh-venv/bin/python ~/seed_inventory/scripts/check_seeds.py --test-bark
```

A message labeled "TEST" will arrive on your iPhone so you can confirm the Bark key works.

### When it runs (updated 2026-10-29)

**From 2026-10-29 the seed math runs inside the Pi's unified inventory job**, `hrh inventory refresh all`
(`~/hrh-inventory/inventory.py`, cronwatch job `inventory-refresh`, **6:30 AM Eastern**, hrh-venv Python).
That job imports `scripts/check_seeds.py` and `scripts/seed_deliveries.py` from this repo, so the
dashboard, the cron job and Sprout all share one copy of the math. Nothing in this repo was removed.

Until the 3-day parallel check finishes (about 2026-11-01), the old standalone job still runs too:
cronwatch job `check_seeds` at **7:20 AM** Eastern, with the **hrh-venv** Python (system `python3`
lacks the Google libraries for the Gmail checks):

```
20 7 * * * /usr/bin/python3 /home/highridgehydro/hrh-cronwatch/cw.py check_seeds
```
(`~/hrh-cronwatch/jobs/check_seeds.sh` runs `hrh check-seeds`, which is
`/home/highridgehydro/hrh-venv/bin/python /home/highridgehydro/seed_inventory/scripts/check_seeds.py`.)

**To turn the old job off:** run `~/hrh-inventory/retire_old_inventory_jobs.sh` on the Pi (comments out the
crontab line; nothing is deleted). **To turn the new job off:** comment out the `inventory-refresh` line in
`crontab -e`. The format is `minute hour * * *`, so `30 8` = 8:30 AM, `0 6` = 6:00 AM.

Ask Sprout "how are we on seed" or run `hrh inventory status seed` on the Pi for the same numbers any time.

### Log file

Every run appends one line to `~/hrh_seed_check.log`:

```
2026-10-19 07:00:03  2 low: Basil (LOW), Arugula (SOON)
2026-10-20 07:00:04  0 low: all OK
```

You can check it any time with:

```bash
tail -20 ~/hrh_seed_check.log
```

### No data snapshot

The dashboard reads AppSheet directly from the browser — there is no JSON snapshot file
in this repo. The `check_seeds.py` script also reads AppSheet live each run.
Nothing is committed to the repo on a normal run.

### True Leaf delivery sweep (added 10/27/2026)

Every seed check run first calls `scripts/seed_deliveries.py`. It reads True Leaf
Market "Your order has been delivered!" emails (last 45 days), turns each seed line
(e.g. *Green Mustard 5 lb x 2*) into grams, and adds it to the AppSheet
`seed_counts` table the same way the dashboard's **+ Add seed** button does.
It skips bags already covered by a later hand count, orders already recorded, and
bags typed in by hand within 4 days. Unknown products or bad sizes are flagged in a
Bark, never guessed. Johnny's deliveries are ignored (their emails list no products).
Delivered orders no longer count as "on order", and vendor ads in emails are ignored.

**On order (added 10/27/2026):** the seed check also reads True Leaf order-status emails
(last 30 days) and lists every order not delivered yet: items, status and True Leaf's
estimated delivery date. Seeds in an open order are not alerted as "order now"; the
7:20 AM Bark says "low, already ordered" with the status. Results are saved to
`~/hrh-cache/seed_status.json` (and, since 2026-10-29, `~/hrh-cache/inventory_status.json`), which the 7:30 AM morning report reads (its old
separate low-seed math was removed), so the seed check, Sprout and the morning
report always agree. "Sango Purple" radish counts as Rambo Radish.

- Map a new product: add a line to `ALIASES` in `scripts/seed_deliveries.py`
  (products you don't track go in `IGNORE`, e.g. Mizuna).
- Sprout's seed answer (`~/hrh-ptt/seed-inventory.py`) uses the same `seed_status()`
  function from `check_seeds.py`, so it always matches this daily check.
- Test (no writes): `~/hrh-venv/bin/python scripts/test_seed_deliveries.py`
- Preview on real data (no writes): `~/hrh-venv/bin/python scripts/seed_deliveries.py --dry-run`
- Run cost: no AI and no API credits (plain Gmail + AppSheet calls).
- Turn off: comment out the `seed_deliveries.sweep(...)` block in `scripts/check_seeds.py`
  (and `_delivery_sweep` in `~/hrh-ptt/seed-inventory.py`).
