# HRH Seed Inventory

Live seed-on-hand dashboard for High Ridge Hydroponics microgreen varieties.  
Served on GitHub Pages at the repo's root (`index.html`).  
Data comes from AppSheet ("Growth Controls" and "Growth Recipes" apps) — no data files live in this repo.

---

## Automated daily seed check (`scripts/check_seeds.py`)

A standalone Python 3 script that runs the same seed-inventory calculations as
the dashboard and sends a push notification to your iPhone if any seeds are running low.  
No extra packages needed — it uses only Python's built-in standard library.

### What it alerts on

A seed triggers an alert if **either** of these is true *and* it has no "Ordered" lot already on its way:

- On hand is **below 25 %** of the most-recent bag's size (LOW / OUT on the dashboard)
- Estimated run-out is **within 14 days** (matches the dashboard's SOON threshold)

### One-time setup on the Pi

**1. Copy the script to the Pi.**  
SSH in and clone (or pull) this repo:

```bash
ssh highridgehydro@192.168.1.174
cd ~
git clone https://github.com/highridgehydroponics-ctrl/seed_inventory.git
# or, if already cloned:
cd seed_inventory && git pull
```

**2. Create the credentials file.**  
Copy the example, then fill in your real keys:

```bash
cp ~/seed_inventory/scripts/hrh_seed.env.example ~/.hrh_seed.env
chmod 600 ~/.hrh_seed.env
nano ~/.hrh_seed.env
```

You need three values:
- `GC_API_KEY` — AppSheet key for the Growth Controls app
- `GR_API_KEY` — AppSheet key for the Growth Recipes app
- `BARK_KEY` — device key from the Bark app on your iPhone

**3. Test it first (dry run — no notification sent):**

```bash
python3 ~/seed_inventory/scripts/check_seeds.py --dry-run
```

This prints the full status table and shows exactly what the notification *would* say.

**4. Send one real test notification:**

```bash
python3 ~/seed_inventory/scripts/check_seeds.py --test-bark
```

A message labeled "TEST" will arrive on your iPhone so you can confirm the Bark key works.

### Crontab — runs every day at 7:00 AM Eastern time

The crontab entry below sets the timezone for just that line, so it works correctly
even if the Pi's system clock is set to UTC:

```
CRON_TZ=America/New_York
0 7 * * * cd /home/highridgehydro/seed_inventory && python3 scripts/check_seeds.py >> /home/highridgehydro/hrh_seed_check.log 2>&1
```

To add it without overwriting your existing cron jobs:

```bash
(crontab -l 2>/dev/null; echo 'CRON_TZ=America/New_York'; echo '0 7 * * * cd /home/highridgehydro/seed_inventory && python3 scripts/check_seeds.py >> /home/highridgehydro/hrh_seed_check.log 2>&1') | crontab -
```

**To turn it off:** run `crontab -e`, find the two lines above, and delete them (or put a `#` in front of the `0 7` line).  
**To change the time:** edit the `0 7` part. The format is `minute hour * * *`, so `30 8` = 8:30 AM, `0 6` = 6:00 AM.

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
