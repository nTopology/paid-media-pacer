"""
Event & Webinar Attribution — v1.0
Net-new contacts attributed to HubSpot campaigns prefixed EV- (events) or WN- (webinars).
Source buckets show where contacts came from before registering.
"""

from __future__ import annotations

import base64
import json
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st


# ── Brand assets ───────────────────────────────────────────────────────────────
_BRAND_DIR = Path(__file__).parent.parent / "static" / "brand"
_LOGO_DIR  = Path(__file__).parent.parent / "static" / "logos"

try:
    _RAW = json.loads((_BRAND_DIR / "colors.json").read_text())
    _C = {
        "blue":       _RAW["accent"]["ntop_blue"]["hex"],
        "black":      _RAW["primary"]["black"]["hex"],
        "white":      _RAW["primary"]["white"]["hex"],
        "green":      _RAW["signal_indicators"]["green"],
        "red":        _RAW["signal_indicators"]["red"],
        "gray_light": _RAW["neutrals_optional"]["gray_light"],
        "gray_mid":   _RAW["neutrals_optional"]["gray_mid"],
        "gray_dark":  _RAW["neutrals_optional"]["gray_dark"],
    }
except Exception:
    _C = {
        "blue": "#248AFF", "black": "#000000", "white": "#FFFFFF",
        "green": "#1FA34E", "red": "#D43F3F",
        "gray_light": "#E5E5E5", "gray_mid": "#999999", "gray_dark": "#333333",
    }

_SVG_FILE = _LOGO_DIR / "nTop-Logo_Light-theme.svg"
_PNG_FILE  = _LOGO_DIR / "nTop-Logo_Light-theme_400w.png"

if _SVG_FILE.exists():
    _logo_b64      = base64.b64encode(_SVG_FILE.read_bytes()).decode()
    _LOGO_IMG_HTML = (
        f'<img src="data:image/svg+xml;base64,{_logo_b64}" '
        f'height="54" style="display:block;flex-shrink:0;">'
    )
elif _PNG_FILE.exists():
    _logo_b64      = base64.b64encode(_PNG_FILE.read_bytes()).decode()
    _LOGO_IMG_HTML = (
        f'<img src="data:image/png;base64,{_logo_b64}" '
        f'height="54" style="display:block;flex-shrink:0;">'
    )
else:
    _LOGO_IMG_HTML = (
        f'<span style="font-family:Oswald,sans-serif;font-weight:700;'
        f'font-size:22px;color:{_C["black"]};">nTop</span>'
    )


# ── Brand CSS ──────────────────────────────────────────────────────────────────
_BRAND_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Oswald:wght@700&family=IBM+Plex+Sans:wght@400;700&family=IBM+Plex+Mono&display=swap');

html, body, .stApp, .stMarkdown, .stCaption, p {{
    font-family: 'IBM Plex Sans', sans-serif;
}}
h1, h2, h3 {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    color: {_C["black"]};
}}
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] div {{
    font-family: 'IBM Plex Sans', sans-serif;
}}
.stAlert p, .stAlert div {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 14px;
}}
.ntop-header {{
    display: flex;
    align-items: center;
    gap: 20px;
    padding-bottom: 20px;
    margin-bottom: 24px;
    border-bottom: 2px solid {_C["black"]};
}}
.ntop-header-text {{
    border-left: 1px solid {_C["gray_light"]};
    padding-left: 20px;
}}
.ntop-page-title {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    font-size: 26px;
    color: {_C["black"]};
    line-height: 1.15;
    margin: 0 0 5px 0;
}}
.ntop-page-subtitle {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 13px;
    color: {_C["gray_dark"]};
    margin: 0;
    line-height: 1.4;
}}
.kpi-band {{
    display: flex;
    gap: 16px;
    margin: 0 0 24px 0;
}}
.kpi-tile {{
    flex: 1;
    background: #f5f5f4;
    border-radius: 8px;
    padding: 1rem 1.25rem;
    min-width: 0;
}}
.kpi-label {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.10em;
    color: {_C["gray_mid"]};
    margin-bottom: 6px;
}}
.kpi-value {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    font-size: 28px;
    color: {_C["black"]};
    line-height: 1.1;
}}
</style>
"""


# ── Constants ──────────────────────────────────────────────────────────────────
HS_BASE       = "https://api.hubapi.com"
METRICS_START = "2024-06-01"

# Channel color palette (spec-defined)
CHANNEL_COLORS: dict[str, str] = {
    "Email marketing":   "#1D9E75",
    "Organic search":    "#378ADD",
    "Social media":      "#D4537E",
    "Other campaigns":   "#7F77DD",
    "Direct":            "#D85A30",
    "Referrals / other": "#888780",
}
CHANNEL_ORDER = list(CHANNEL_COLORS.keys())


# ── HubSpot API helpers ────────────────────────────────────────────────────────

def _hs_token() -> str:
    if "hubspot_api_token" in st.secrets:
        return str(st.secrets["hubspot_api_token"])
    import os
    return os.environ.get("HUBSPOT_API_TOKEN", "")


def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {_hs_token()}",
        "Content-Type":  "application/json",
    }


def _hs_get(path: str, params: dict | None = None) -> dict:
    resp = requests.get(
        f"{HS_BASE}{path}",
        headers=_hs_headers(),
        params=params or {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _hs_post(path: str, payload: dict) -> dict:
    resp = requests.post(
        f"{HS_BASE}{path}",
        headers=_hs_headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _bucket_source(src: str) -> str:
    s = (src or "").upper().strip()
    if s == "EMAIL_MARKETING":
        return "Email marketing"
    if s == "ORGANIC_SEARCH":
        return "Organic search"
    if s in ("SOCIAL_MEDIA", "PAID_SOCIAL"):
        return "Social media"
    if s in ("OTHER_CAMPAIGNS", "PAID_SEARCH"):
        return "Other campaigns"
    if s == "DIRECT_TRAFFIC":
        return "Direct"
    return "Referrals / other"


def _debug_contact_probe(campaign_guid: str) -> dict:
    """
    Uncached diagnostic: makes the actual HTTP calls and returns full response info.
    Used only in the debug expander — never cached.
    """
    today_str = date.today().isoformat()
    out: dict = {"campaign_guid": campaign_guid}

    # Step 1: contacts endpoint
    url1 = (
        f"{HS_BASE}/marketing/v3/campaigns/{campaign_guid}"
        "/reports/contacts/NEW_CONTACTS_FIRST_TOUCH"
    )
    params1 = {"startDate": METRICS_START, "endDate": today_str, "limit": 10}
    try:
        r1 = requests.get(url1, headers=_hs_headers(), params=params1, timeout=30)
        out["contacts_url"]    = r1.url
        out["contacts_status"] = r1.status_code
        out["contacts_body"]   = r1.text[:2000]
        data1 = r1.json() if r1.ok else {}
        ids = [
            str(item.get("id") or item.get("contactId", ""))
            for item in data1.get("results", [])
            if item.get("id") or item.get("contactId")
        ]
        out["contact_ids_found"]  = len(ids)
        out["contact_ids_sample"] = ids[:5]
    except Exception as exc:
        out["contacts_exception"] = str(exc)
        ids = []

    if not ids:
        out["batch_read"] = "skipped — contacts endpoint returned 0 IDs"
        return out

    # Step 2: batch read
    try:
        r2 = requests.post(
            f"{HS_BASE}/crm/v3/objects/contacts/batch/read",
            headers=_hs_headers(),
            json={
                "inputs":     [{"id": cid} for cid in ids],
                "properties": ["hs_analytics_source", "hs_latest_source"],
            },
            timeout=30,
        )
        out["batch_status"]        = r2.status_code
        out["batch_body"]          = r2.text[:2000]
        data2                      = r2.json() if r2.ok else {}
        out["batch_results_count"] = len(data2.get("results", []))
        out["sources_non_null"]    = sum(
            1 for c in data2.get("results", [])
            if (c.get("properties") or {}).get("hs_analytics_source")
        )
    except Exception as exc:
        out["batch_exception"] = str(exc)

    return out


# ── Data fetchers ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600)
def fetch_ev_wn_campaigns() -> list[dict]:
    """
    Paginate /marketing/v3/campaigns with properties=hs_name so names are populated.
    The `id` field on each result is the campaign GUID used in all subsequent calls.
    Returns list of {"id": guid, "name": str} filtered to EV- and WN- prefixes.
    """
    campaigns: list[dict] = []
    after: str | None = None
    while True:
        params: dict = {"limit": 100, "properties": "hs_name"}
        if after:
            params["after"] = after
        data = _hs_get("/marketing/v3/campaigns", params)
        for c in data.get("results", []):
            name = (c.get("properties") or {}).get("hs_name", "")
            if name.startswith("EV-") or name.startswith("WN-"):
                campaigns.append({"id": c["id"], "name": name})
        after = ((data.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            break
    return campaigns


@st.cache_data(ttl=3600)
def fetch_campaign_metrics(campaign_guid: str, start_date: str, end_date: str) -> dict:
    """
    GET /marketing/v3/campaigns/{campaignGuid}/reports/metrics
    Uses GUID (the `id` from the list endpoint), not the numeric CRM object ID.
    start_date/end_date are cache-key args to avoid replaying stale zeros.
    """
    return _hs_get(
        f"/marketing/v3/campaigns/{campaign_guid}/reports/metrics",
        {"startDate": start_date, "endDate": end_date},
    )


@st.cache_data(ttl=3600)
def fetch_contact_ids(campaign_guid: str, attr_type: str) -> list[str]:
    """
    GET /marketing/v3/campaigns/{guid}/reports/contacts/{attr_type}
    attr_type must be NEW_CONTACTS_FIRST_TOUCH or NEW_CONTACTS_LAST_TOUCH.
    Returns list of contact ID strings. Cached 1 h.
    """
    today_str = date.today().isoformat()
    ids: list[str] = []
    after: str | None = None
    while True:
        params: dict = {
            "startDate": METRICS_START,
            "endDate":   today_str,
            "limit":     100,
        }
        if after:
            params["after"] = after
        # Let HTTP errors propagate — caller catches and records them in id_errors.
        data = _hs_get(
            f"/marketing/v3/campaigns/{campaign_guid}/reports/contacts/{attr_type}",
            params,
        )
        results = data.get("results", [])
        for item in results:
            cid = item.get("id") or item.get("contactId")
            if cid:
                ids.append(str(cid))
        after = ((data.get("paging") or {}).get("next") or {}).get("after")
        if not after or not results:
            break
    return ids


@st.cache_data(ttl=86400)
def fetch_contact_sources_bulk(contact_ids: tuple[str, ...]) -> dict[str, str]:
    """
    POST /crm/v3/objects/contacts/batch/read for a deduplicated set of IDs.
    Returns {contact_id: channel_bucket}. Accepts tuple so it's hashable. Cached 24 h.
    """
    if not contact_ids:
        return {}
    result: dict[str, str] = {}
    ids = list(contact_ids)
    for i in range(0, len(ids), 100):
        chunk = ids[i : i + 100]
        # Let HTTP errors propagate — caller catches and surfaces them.
        resp = _hs_post(
            "/crm/v3/objects/contacts/batch/read",
            {
                "inputs":     [{"id": cid} for cid in chunk],
                "properties": ["hs_analytics_source", "hs_latest_source"],
            },
        )
        for contact in resp.get("results", []):
            props = contact.get("properties") or {}
            src   = props.get("hs_analytics_source") or ""
            result[str(contact["id"])] = _bucket_source(src)
    return result


# ── Page setup ─────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Event & Webinar Attribution", layout="wide")
st.markdown(_BRAND_CSS, unsafe_allow_html=True)

if _PNG_FILE.exists():
    st.logo(str(_PNG_FILE))

st.markdown(
    f'<div class="ntop-header">'
    f'{_LOGO_IMG_HTML}'
    f'<div class="ntop-header-text">'
    f'<div class="ntop-page-title">Event &amp; Webinar Attribution</div>'
    f'<div class="ntop-page-subtitle">'
    f'Net-new contacts attributed to EV- and WN- HubSpot campaigns. '
    f'Source channels from <code>hs_analytics_source</code>. '
    f'Date range: 2024-06-01 to today.'
    f'</div></div></div>',
    unsafe_allow_html=True,
)


# ── Controls ───────────────────────────────────────────────────────────────────
ctrl1, ctrl2 = st.columns(2)
with ctrl1:
    campaign_type = st.radio(
        "Campaign type",
        ["All", "Events", "Webinars"],
        horizontal=True,
        index=0,
    )
with ctrl2:
    attr_model = st.radio(
        "Attribution model",
        ["First touch", "Last touch"],
        horizontal=True,
        index=0,
    )

metric_key  = "newContactsFirstTouch" if attr_model == "First touch" else "newContactsLastTouch"


# ── Load campaigns ─────────────────────────────────────────────────────────────
if not _hs_token():
    st.error(
        "HubSpot API token is not set. "
        "Go to Streamlit Cloud → your app → ⋮ → Settings → Secrets and add:\n\n"
        "```\nhubspot_api_token = \"pat-na1-...\"\n```"
    )
    st.stop()

with st.spinner("Fetching campaign list from HubSpot…"):
    try:
        all_campaigns = fetch_ev_wn_campaigns()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        body   = exc.response.text[:400] if exc.response is not None else ""
        st.error(
            f"HubSpot API returned {status}.\n\n"
            f"**Details:** {body}\n\n"
            "If this says 'MISSING_SCOPES', the Service Key is missing `marketing.campaigns.read`. "
            "If it says 'INVALID_AUTHENTICATION', the token in Streamlit secrets is wrong."
        )
        st.stop()
    except Exception as exc:
        st.error(f"Could not fetch campaigns: {exc}")
        st.stop()

if campaign_type == "Events":
    campaigns = [c for c in all_campaigns if c.get("name", "").startswith("EV-")]
elif campaign_type == "Webinars":
    campaigns = [c for c in all_campaigns if c.get("name", "").startswith("WN-")]
else:
    campaigns = list(all_campaigns)

if not campaigns:
    st.info("No campaigns match the current filter.")
    st.stop()


# ── Fetch campaign metrics (fast, 1 h cache) ───────────────────────────────────
today_str = date.today().isoformat()
campaign_rows: list[dict] = []
metrics_bar = st.progress(0, text="Loading campaign metrics…")

for i, c in enumerate(campaigns):
    metrics_bar.progress(
        (i + 1) / len(campaigns),
        text=f"Metrics {i + 1}/{len(campaigns)}: {c.get('name', c['id'])}",
    )
    try:
        raw = fetch_campaign_metrics(c["id"], METRICS_START, today_str)
        # Response is either {"metrics": {...}} (nested) or the flat dict itself
        m = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else raw
        if isinstance(m, list):
            m = {item.get("metric") or item.get("name"): item.get("value", 0) for item in m}
        ft = int(m.get("newContactsFirstTouch", 0) or 0)
        lt = int(m.get("newContactsLastTouch",  0) or 0)
        metrics_error = None
    except Exception as _exc:
        ft, lt, raw = 0, 0, {}
        metrics_error = str(_exc)

    name      = c.get("name", "")
    try:
        # Parse YYYY-MM-DD from name (e.g. EV-2025-06-24-...)
        date_part = "-".join(name.split("-")[1:4])
        sort_date = date.fromisoformat(date_part)
    except Exception:
        sort_date = date.min

    campaign_rows.append({
        "id":         c["id"],
        "name":       name,
        "type":       "Event" if name.startswith("EV-") else "Webinar",
        "sort_date":  sort_date,
        "ft_count":   ft,
        "lt_count":   lt,
        "_raw":       raw,
        "_error":     metrics_error,
    })

metrics_bar.empty()

# Apply current attribution model and filter out zero-registration campaigns
for row in campaign_rows:
    row["count"] = row["ft_count"] if attr_model == "First touch" else row["lt_count"]

campaign_rows_all = campaign_rows  # keep unfiltered copy for diagnostics
campaign_rows = [r for r in campaign_rows if r["count"] > 0]

if not campaign_rows:
    first = campaign_rows_all[0] if campaign_rows_all else {}
    if first.get("_error"):
        st.error(f"Metrics API error: {first['_error']}")
    else:
        st.warning("All campaigns returned 0 registrations. Raw API response for first campaign:")
        st.json(first.get("_raw", {}))
    st.stop()

# Sort descending by date for the table (most recent first)
campaign_rows.sort(key=lambda r: r["sort_date"], reverse=True)


# ── Fetch contact IDs for every campaign (both models, 1 h cache) ──────────────
# Fetch FT + LT for all campaigns upfront so switching the model toggle is instant.
ft_ids:    dict[str, list[str]] = {}
lt_ids:    dict[str, list[str]] = {}
id_errors: dict[str, str]       = {}   # campaign_id → error string, if any

id_bar = st.progress(0, text="Fetching contact IDs…")
for i, row in enumerate(campaign_rows):
    id_bar.progress(
        (i + 1) / len(campaign_rows),
        text=f"Contact IDs {i + 1}/{len(campaign_rows)}: {row['name']}",
    )
    try:
        ft_ids[row["id"]] = fetch_contact_ids(row["id"], "NEW_CONTACTS_FIRST_TOUCH")
    except Exception as exc:
        ft_ids[row["id"]] = []
        id_errors[row["id"]] = f"FT fetch failed: {exc}"
    try:
        lt_ids[row["id"]] = fetch_contact_ids(row["id"], "NEW_CONTACTS_LAST_TOUCH")
    except Exception as exc:
        lt_ids[row["id"]] = []
        prev = id_errors.get(row["id"], "")
        id_errors[row["id"]] = (prev + " | " if prev else "") + f"LT fetch failed: {exc}"
id_bar.empty()

# Deduplicate across both models and bulk-read sources in one shot (24 h cache).
all_ids: tuple[str, ...] = tuple(sorted({
    cid
    for mapping in (ft_ids, lt_ids)
    for ids in mapping.values()
    for cid in ids
}))
sources_lookup: dict[str, str] = {}
bulk_error: str | None = None
with st.spinner(f"Loading sources for {len(all_ids):,} unique contacts (cached 24 h)…"):
    try:
        sources_lookup = fetch_contact_sources_bulk(all_ids)
    except Exception as exc:
        bulk_error = str(exc)

# Aggregate per campaign using whichever ID list the selected model needs.
for row in campaign_rows:
    id_list = ft_ids.get(row["id"], []) if attr_model == "First touch" else lt_ids.get(row["id"], [])
    src: dict[str, int] = {}
    for cid in id_list:
        bucket = sources_lookup.get(cid, "Referrals / other")
        src[bucket] = src.get(bucket, 0) + 1
    row["sources"]    = src
    row["n_contacts"] = len(id_list)

# Determine whether source data actually loaded before rendering cards or chart.
sources_available = any(sum(r["sources"].values()) > 0 for r in campaign_rows)


# ── Debug expander (always visible so failures are surfaced immediately) ────────
ft_total = sum(len(v) for v in ft_ids.values())
lt_total = sum(len(v) for v in lt_ids.values())
with st.expander(
    "Debug: contact source diagnostics"
    + (" ✓" if sources_available else " ✗ — source data missing"),
    expanded=not sources_available,
):
    st.write(f"**Campaigns in filter:** {len(campaign_rows)}")
    st.write(f"**FT contact IDs returned by API:** {ft_total}")
    st.write(f"**LT contact IDs returned by API:** {lt_total}")
    st.write(f"**Unique IDs sent to batch read:** {len(all_ids)}")
    st.write(f"**Source values resolved:** {len(sources_lookup)}")
    if id_errors:
        st.write("**Per-campaign fetch errors:**")
        for cid, err in id_errors.items():
            st.write(f"- `{cid}`: {err}")
    if bulk_error:
        st.write(f"**Batch read error:** `{bulk_error}`")
    if campaign_rows:
        first = campaign_rows[0]
        st.write(
            f"**Live probe for:** `{first['name']}` — GUID `{first['id']}`"
        )
        with st.spinner("Running probe…"):
            probe = _debug_contact_probe(first["id"])
        st.json(probe)


# ── Metric cards ───────────────────────────────────────────────────────────────
total_registrations = sum(r["count"] for r in campaign_rows)
n_campaigns         = len(campaign_rows)

if sources_available:
    email_total = sum(r["sources"].get("Email marketing", 0) for r in campaign_rows)
    email_pct   = email_total / total_registrations if total_registrations else 0
    st.markdown(
        f'<div class="kpi-band">'
        f'<div class="kpi-tile">'
        f'<div class="kpi-label">Net-new contacts</div>'
        f'<div class="kpi-value">{total_registrations:,}</div>'
        f'</div>'
        f'<div class="kpi-tile">'
        f'<div class="kpi-label">Email-sourced</div>'
        f'<div class="kpi-value">{email_total:,}</div>'
        f'</div>'
        f'<div class="kpi-tile">'
        f'<div class="kpi-label">Email share</div>'
        f'<div class="kpi-value">{email_pct:.1%}</div>'
        f'</div>'
        f'<div class="kpi-tile">'
        f'<div class="kpi-label">Campaigns tracked</div>'
        f'<div class="kpi-value">{n_campaigns}</div>'
        f'</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
else:
    st.markdown(
        f'<div class="kpi-band">'
        f'<div class="kpi-tile">'
        f'<div class="kpi-label">Net-new contacts</div>'
        f'<div class="kpi-value">{total_registrations:,}</div>'
        f'</div>'
        f'<div class="kpi-tile">'
        f'<div class="kpi-label">Campaigns tracked</div>'
        f'<div class="kpi-value">{n_campaigns}</div>'
        f'</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

st.divider()

# ── Horizontal stacked bar chart ───────────────────────────────────────────────
# Sort ascending by date so Plotly's bottom→top axis puts the newest campaign at the top.
chart_rows = sorted(campaign_rows, key=lambda r: r["sort_date"])

fig = go.Figure()
if sources_available:
    for channel in CHANNEL_ORDER:
        vals = [r["sources"].get(channel, 0) for r in chart_rows]
        if sum(vals) == 0:
            continue
        fig.add_trace(go.Bar(
            name=channel,
            x=vals,
            y=[r["name"] for r in chart_rows],
            orientation="h",
            marker_color=CHANNEL_COLORS[channel],
            hovertemplate="%{y}<br>" + channel + ": %{x:,d}<extra></extra>",
        ))
else:
    # Source data failed — show error and grey registration totals as placeholder.
    err_detail = bulk_error or (list(id_errors.values())[0] if id_errors else "0 contact IDs returned from the contacts endpoint")
    st.error(
        f"Contact source breakdown unavailable — check the Debug expander above for the full API response. "
        f"Error: {err_detail}"
    )
    fig.add_trace(go.Bar(
        name="Registrations (source breakdown unavailable)",
        x=[r["count"] for r in chart_rows],
        y=[r["name"] for r in chart_rows],
        orientation="h",
        marker_color=_C["gray_mid"],
        hovertemplate="%{y}<br>Registrations: %{x:,d}<extra></extra>",
    ))

fig.update_layout(
    barmode="stack",
    height=max(350, 52 * len(chart_rows) + 100),
    margin=dict(l=20, r=40, t=10, b=40),
    xaxis=dict(title="Net-new contacts", showgrid=True, gridcolor="#F0F0F0"),
    yaxis=dict(title=None),
    legend=dict(
        orientation="h",
        yanchor="bottom", y=1.02,
        xanchor="left",   x=0,
        font=dict(size=12, family="IBM Plex Sans"),
    ),
    plot_bgcolor="white",
    paper_bgcolor="white",
)

st.plotly_chart(fig, use_container_width=True)


# ── Per-campaign table ─────────────────────────────────────────────────────────
st.divider()
with st.expander("Per-campaign breakdown", expanded=True):
    table_rows = []
    for r in campaign_rows:  # already sorted most-recent first
        src        = r["sources"]
        src_total  = sum(src.values())
        email_cnt  = src.get("Email marketing", 0)
        table_rows.append({
            "Campaign":        r["name"],
            "Type":            r["type"],
            "Date":            r["sort_date"].isoformat() if r["sort_date"] != date.min else "—",
            "Registrations":   r["count"],
            "Contacts fetched": r.get("n_contacts", 0),
            "Email-sourced":   email_cnt,
            "Email %":         f"{email_cnt / src_total:.0%}" if src_total else "—",
            **{ch: src.get(ch, 0) for ch in CHANNEL_ORDER},
        })
    st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)


# ── Footer ─────────────────────────────────────────────────────────────────────
st.caption(
    f"Attribution window: {METRICS_START} to {date.today().isoformat()}. "
    "Source buckets from `hs_analytics_source` (HubSpot original source). "
    "Campaign metrics cached 1 h; contact source lookup cached 24 h."
)
with st.expander("Methodology"):
    st.markdown("""
**Attribution model** controls which set of contacts is counted per campaign:
- **First touch** — contacts for whom this campaign was the first known marketing touchpoint.
- **Last touch** — contacts for whom this campaign was the most recent touchpoint before conversion.

**Source bucketing** maps `hs_analytics_source` values as follows:

| HubSpot value | Chart channel |
|---|---|
| `EMAIL_MARKETING` | Email marketing |
| `ORGANIC_SEARCH` | Organic search |
| `SOCIAL_MEDIA`, `PAID_SOCIAL` | Social media |
| `OTHER_CAMPAIGNS`, `PAID_SEARCH` | Other campaigns |
| `DIRECT_TRAFFIC` | Direct |
| Everything else | Referrals / other |

Campaigns with zero registrations in the selected attribution model are hidden.
Invite-driven roadshows (Bristol, DC, FORMNEXT, SciTech, El Segundo) appear
with low counts — this is expected.
""")
