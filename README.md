# Global Situation Room — Streamlit Cloud edition

A cloud-hosted version of the Global Situation Room dashboard. It runs on
[Streamlit Community Cloud](https://share.streamlit.io), so you can open it on
**any device (iPad, phone, desktop) from one URL** — no PC, no `start.bat`, no
Wi-Fi/firewall setup.

It does **not** rebuild the UI. `streamlit_app.py` is a thin wrapper that:

1. Runs your existing `data_fetcher.build()` on Streamlit's servers (cached,
   refreshed every 5 minutes).
2. Injects the fresh data into the existing HTML page as `window.GSR_DATA`.
3. Serves the whole dashboard and re-runs on a timer for auto-refresh.

---

## Files in this folder

| File | Purpose |
|------|---------|
| `streamlit_app.py` | The wrapper (entry point). |
| `requirements.txt` | Python deps for Streamlit Cloud. |
| `.streamlit/config.toml` | Dark theme / headless server. |
| `.gitignore` | Keeps tokens and generated data out of git. |
| `data_fetcher.py` | **Copy this in** from your Main Control Room folder. |
| `global-situation-room.html` | **Copy this in** from your Main Control Room folder. |

> ⚠️ Two files must be copied in before deploying: **`data_fetcher.py`** and
> **`global-situation-room.html`**. They are the single source of truth in your
> local folder — drop the current versions here. (When you update the local
> dashboard later, re-copy them and push again.)

---

## Deploy (one-time)

1. Put `data_fetcher.py` and `global-situation-room.html` in this folder.
2. Create a **new GitHub repo** (e.g. `global-situation-room`) and push this
   folder to it:
   ```
   git init
   git add .
   git commit -m "Global Situation Room — Streamlit"
   git branch -M main
   git remote add origin https://github.com/<you>/global-situation-room.git
   git push -u origin main
   ```
3. Go to **share.streamlit.io → Create app**, pick the repo, branch `main`, and
   main file `streamlit_app.py`. Deploy.
4. You'll get a URL like `https://<name>.streamlit.app`. Open it on the iPad and
   **Add to Home Screen** for a one-tap, full-screen launch.

## Optional: FinMind API token (better Taiwan-data rate limits)

Shared cloud IPs hit FinMind's free rate limit faster. If you have a token:
**App → Settings → Secrets**, then add:
```
FINMIND_TOKEN = "your_token_here"
```
The wrapper picks it up automatically.

---

## Notes & limits

- **Sleeping:** Community Cloud apps sleep after inactivity; the first visit
  wakes it in ~20–40s.
- **Data source = cloud, not your PC.** Google News / YouTube live-checks can
  occasionally be rate-limited from shared cloud IPs; the dashboard degrades
  gracefully (shows "unverified" rather than breaking).
- **Refresh cadence:** controlled by `REFRESH_SEC` in `streamlit_app.py`
  (default 300s). The in-page 60s timer is harmless on cloud.
- **Layout height:** if the dashboard looks cut off, raise `COMPONENT_HEIGHT`
  in `streamlit_app.py`.
- The TradingView charts and the Hormuz vessel map are client-side iframes and
  work the same as in the local version.
