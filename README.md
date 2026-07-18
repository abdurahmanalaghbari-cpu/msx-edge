# MSX EDGE — automated daily pipeline

Every trading day (Sun–Thu), GitHub Actions fetches end-of-day data for every
symbol in `symbols.txt`, runs each stock's saved champion strategy, and
publishes **Tomorrow's Tape** to GitHub Pages — a phone-friendly page showing
BUY / EXIT / HOLD / NEAR setups for the next session, plus the full MSX EDGE
tool with data preloaded at `/tool.html`.

## One-time setup (~10 minutes)

1. **Create the repo.** Sign in to GitHub → New repository → name it
   `msx-edge`, set it **Public** (free GitHub Pages requires a public repo —
   note this publishes your price data and strategy parameters), and upload
   every file in this folder (keep the `.github/workflows/` structure).
2. **Set your universe.** Edit `symbols.txt` — one ticker per line, exactly as
   it appears in the stockanalysis.com URL:
   `stockanalysis.com/quote/msm/OQGN/` → `OQGN`. The full MSX list is at
   stockanalysis.com/list/muscat-securities-market/.
3. **Enable Pages.** Repo → Settings → Pages → Source: **GitHub Actions**.
4. **First run.** Actions tab → "Daily MSX screen" → Run workflow → tick
   **full** → Run. This pulls maximum history and publishes the site — at this
   point the tape page shows a setup notice, and `/tool.html` already has all
   your data loaded.
5. **Add your champions.** Open `/tool.html` (any device), run the Optimizer,
   save champions, then Strategies tab → **Export champions JSON**. Rename the
   file to `champions.json`, upload it to the repo root, and run the workflow
   once more (no need to tick full). The tape comes alive.
6. **On your phone.** Open `https://<your-username>.github.io/msx-edge/`,
   then Share → **Add to Home Screen**. That icon is now your app.

From then on it runs itself at 8 PM Muscat time on trading days (Sun–Thu).

## Ongoing

- **Re-optimize anytime, from any device:** open `/tool.html` (data is already
  loaded), re-run the optimizer, save champions, then use **Cloud sync** in the
  Strategies tab — one click publishes `champions.json` back to this repo and
  rebuilds the site within ~2 minutes. One-time setup: create a fine-grained
  token (GitHub → Settings → Developer settings → Fine-grained tokens) scoped
  to only this repo with `Contents: Read and write` and `Actions: Read and
  write`, and paste it into the Cloud sync panel. It stays in that browser
  only. (Manual alternative: export the JSON and upload it to the repo.)
- **Add a stock:** add the ticker to `symbols.txt`, run the workflow once with
  **full** ticked, then save a champion for it.
- **Debug a symbol:** locally, `python msx_fetch.py --check TICKER`.

## Files

| File | Purpose |
|---|---|
| `msx_fetch.py` | Downloads/merges EOD CSVs into `data/` |
| `symbols.txt` | Your universe (one ticker per line) |
| `champions.json` | Saved best strategy per stock (exported from the tool) |
| `engine.js` | Strategy/backtest engine — extracted verbatim from the tool |
| `screen.js` | Runs the screen, builds `site/` |
| `msx_edge.html` | The full tool (published to `/tool.html`) |
| `.github/workflows/daily.yml` | The schedule |

## Honest notes

- Data comes from stockanalysis.com (S&P Global-sourced, split-adjusted,
  updated after each close). It's an unofficial integration — if their site
  changes, `msx_fetch.py` may need a small patch. The tape page shows a red
  banner whenever the latest bar looks stale, so lag is never silent.
- GitHub cron is best-effort; runs can start up to ~15 min late. The 19:00
  catch-up run covers slow source updates.
- Signals assume fills at the **next session's open**, long-only, with the
  costs saved alongside each champion. A simulation, not a promise.
