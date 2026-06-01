"""
Global Situation Room — Streamlit Cloud wrapper.

Keeps the existing HTML dashboard exactly as-is and feeds it fresh data
server-side, so it runs on any device (iPad / phone / desktop) from a single
URL — no local backend or start.bat needed.

It reuses your existing data_fetcher.build(): every few minutes the app
re-fetches all sources (FinMind/TWSE, Stooq/Yahoo, USGS, Google News, YouTube
live-status) and injects the result into the page as window.GSR_DATA, which
the dashboard already knows how to render.

Deploy:
  1. Put data_fetcher.py and global-situation-room.html in THIS folder.
  2. Push the folder to a GitHub repo (e.g. global-situation-room).
  3. On share.streamlit.io → Create app → point at streamlit_app.py.
See README.md for details.
"""
import os
import json
import streamlit as st
from streamlit.components.v1 import html as st_html

try:
    from streamlit_autorefresh import st_autorefresh
    _HAS_AUTOREFRESH = True
except Exception:
    _HAS_AUTOREFRESH = False

import data_fetcher

HERE = os.path.dirname(os.path.abspath(__file__))
REFRESH_SEC = 300          # rebuild data every 5 min (matches the original backend)
COMPONENT_HEIGHT = 2600    # iframe height in px; raise if your layout is taller

st.set_page_config(page_title="Global Situation Room", page_icon="🛰",
                   layout="wide", initial_sidebar_state="collapsed")

# Strip Streamlit's chrome so the dashboard fills the viewport.
st.markdown(
    "<style>#MainMenu,header[data-testid='stHeader'],footer{display:none!important}"
    ".block-container{padding:0!important;margin:0!important;max-width:100%!important}"
    "[data-testid='stAppViewBlockContainer']{padding:0!important}"
    "iframe{border:0!important}</style>",
    unsafe_allow_html=True,
)

# Optional FinMind token (Streamlit → Settings → Secrets → FINMIND_TOKEN="...").
try:
    _tok = st.secrets.get("FINMIND_TOKEN", "")
    if _tok:
        data_fetcher.FINMIND_TOKEN = _tok
except Exception:
    pass

# Auto-refresh the whole app on a timer; the server re-fetches and re-injects.
if _HAS_AUTOREFRESH:
    st_autorefresh(interval=REFRESH_SEC * 1000, key="gsr_autorefresh")


@st.cache_data(ttl=REFRESH_SEC, show_spinner="Fetching live intel…")
def get_data():
    return data_fetcher.build()


try:
    data = get_data()
except Exception as e:
    st.error(f"Data fetch failed: {e}")
    st.stop()

tpl_path = os.path.join(HERE, "global-situation-room.html")
if not os.path.exists(tpl_path):
    st.error("global-situation-room.html not found in this folder. "
             "Copy it in next to streamlit_app.py (see README).")
    st.stop()

with open(tpl_path, encoding="utf-8") as f:
    page = f.read()

# Safe-embed the JSON inside the HTML (prevent a stray </script> from breaking it).
data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
inject = f"<script>window.GSR_DATA={data_json};</script>"

# 1) Feed fresh data into the page (dashboard reads window.GSR_DATA).
if '<script src="data.js"></script>' in page:
    page = page.replace('<script src="data.js"></script>', inject)
else:
    page = page.replace("</head>", inject + "</head>")

# 2) Hide the "backend not detected / snapshot" banner — data here is live.
page = page.replace("</head>", "<style>#backendBanner{display:none!important}</style></head>")

st_html(page, height=COMPONENT_HEIGHT, scrolling=True)
