# Property watcher

Every hour, checks for 1-bedroom apartments for rent in Abu Dhabi and sends a push
notification (via [ntfy](https://ntfy.sh)) for each listing it hasn't seen before.

- **Relaam**: the pre-filtered search page.
- **MPM Properties**: fills in the filter form (City: Abu Dhabi, Unit Type: Apartment,
  Category: Rent, Beds: 1) and reads every results page.

Run one site locally with `python watch.py mpm` or `python watch.py relaam`.

## Setup

1. Install the **ntfy** app on your phone and subscribe to a hard-to-guess topic,
   e.g. `relaam-omar-7k2q9x` (anyone who knows the name can read it).
2. In this repo: **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `NTFY_TOPIC`
   - Value: your topic name
3. **Actions → Watch property listings → Run workflow** to run it once now.
   The first run records current listings and sends a "watcher is running" notification;
   later runs notify only about new ones.

## Notes

- Sites and filters are defined at the top of each section in `watch.py` (`SOURCES` lists them).
- If a run finds no listings at all, you get one warning notification. Each run
  uploads a `page-snapshot` artifact (HTML + screenshot per site) for debugging.
- GitHub may pause scheduled workflows in repos with no activity for 60 days;
  the hourly `seen.json` commits normally keep it active.
- Private repos get 2,000 free Actions minutes/month; hourly runs use roughly
  1,000–1,500. A public repo has no limit.
