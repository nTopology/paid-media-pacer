"""
Sales Capacity & Touch Quality — v1.0

Answers one business question: have we hit sales capacity, or is there room to
push more volume? Drives the hiring-vs-paid-budget decision.

Sections:
  1. Headline — the 3+ touch dose-response
  2. Weekly trend (outbound per rep, contacts touched, replies, meetings)
  3. Touches per contact distribution
  4. Effort x intent quadrant
  5. Speed to lead (monthly, Outreach-tracked subset only)
  6. Qualified opportunities per rep (monthly, by motion)
  7. Per-rep drill-down
  8. Quality of touch — placeholder, phase 2

Reads from `bi-ntop.salesforce.*` and `bi-ntop.hubspot.*` (not the
aero_prod_ad_reporting tables the other pages use).

Metric definitions come from the Jai Toor capacity conversations; the touch
source inventory comes from Rick Groves' contact_touch_tracking doc.
See docs/sales-capacity-notes.md.
"""

from __future__ import annotations

import base64
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from google.cloud import bigquery
from google.oauth2 import service_account


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
        "orange":     _RAW["signal_indicators"]["orange"],
        "gray_light": _RAW["neutrals_optional"]["gray_light"],
        "gray_mid":   _RAW["neutrals_optional"]["gray_mid"],
        "gray_dark":  _RAW["neutrals_optional"]["gray_dark"],
    }
except Exception:
    _C = {
        "blue": "#248AFF", "black": "#000000", "white": "#FFFFFF",
        "green": "#1FA34E", "red": "#D43F3F", "orange": "#F39C12",
        "gray_light": "#E5E5E5", "gray_mid": "#999999", "gray_dark": "#333333",
    }

_SVG_FILE = _LOGO_DIR / "nTop-Logo_Light-theme.svg"
_PNG_FILE = _LOGO_DIR / "nTop-Logo_Light-theme_400w.png"

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


# ── Constants ─────────────────────────────────────────────────────────────────
SERVICE_ACCOUNT_FILE = "service-account.json"
GCP_PROJECT          = "bi-ntop"

# Jai Toor's benchmark: 3+ outbound touches to a contact is "high effort".
# Validated empirically — see the dose-response callout in section 1.
HIGH_EFFORT_THRESHOLD = 3

# Who is doing the outreach, split by the motion they're running. Every "per rep"
# figure divides within these teams, and the weekly trend charts them separately —
# blending them hides one team's volume behind another's, which is exactly how a
# single high-volume ABM programme came to look like team-wide capacity.
#
# This has to be an explicit mapping. Salesforce `user.title` is blank for most of
# these people, there is no user_role table in the warehouse, and everyone is on an
# @ntop.com Standard licence — so no field separates a rep from the CEO, the
# General Counsel, or a Solutions Engineer who owns a couple of logged emails.
# Counting all task/event owners instead (the original spec's
# `COUNT(DISTINCT owner_id)`) pulled in 41 people and understated outbound per rep
# by roughly 3x.
#
# Overridable per-session from the sidebar.
TEAMS = {
    "Reps": ["Addison Berenzweig", "Evan Boyer", "James Gibbons"],
    "ABM":  ["Laurel Berger"],
    "CS":   ["Taha Benhaddou", "Cheyenne Cullen", "Albright Tshisekedi",
             "Neil Brayman"],
}
TEAM_ORDER = ["Reps", "ABM", "CS"]

TEAM_COLORS = {
    "Reps": "#0047FF",       # new business — the capacity question
    "ABM":  "#EB6834",       # high-volume programme, kept visually distinct
    "CS":   _C["green"],     # existing customers
}

TEAM_BLURB = {
    "Reps": "New-business outbound. This is the team the capacity question is "
            "actually about.",
    "ABM":  "High-volume account-based programme. Ran hard Feb–Apr 2026 against "
            "largely freshly-imported contacts; read its volume separately from "
            "rep effort.",
    "CS":   "Customer success outreach to existing customers. High reply rates "
            "here are expected and are not a new-business signal.",
}

# Outbound touches in-window that make someone worth a second look if they aren't
# in the selected teams.
ROSTER_REVIEW_THRESHOLD = 250

# People with real outbound volume who are deliberately counted nowhere. Listed so
# the staleness check stays quiet about known cases and only speaks up for someone
# genuinely new; without this it would fire every week and get ignored.
EXCLUDED_OWNERS = {
    "Andrew Hanno":        "VP of Marketing, since departed — activity isn't "
                           "comparable to a carrying rep's",
    "Joel Bejar":          "VP of Sales — relationship support only, not "
                           "carrying a patch",
    "Hemant Bhoosnurmath": "Solutions Engineer — technical support on live deals",
}

# Outbound channels. The values are the touch_type codes the SQL emits; the sidebar
# filter selects which of them count as an outbound touch.
#
# LinkedIn and Call volumes are genuinely small (209 and 86 in 2026 against 13,693
# emails), so treat their trend lines as directional at best.
CHANNELS = {
    "Email":    "email_out",
    "LinkedIn": "linkedin",
    "Call":     "call",
    "Other":    "other_channel",
}

# Outreach activity only starts being reliably logged in 2026.
DATA_START    = date(2026, 1, 1)
DEFAULT_START = date(2026, 1, 1)

# Salesforce opportunity record types. Renewal is deliberately excluded —
# see METHODOLOGY_NOTES.
RT_HV        = ("0124R000001UuQlQAK", "0124R000001UuQgQAK")
RT_STRATEGIC = ("012Qo00000AyuhVIAR", "012Qo00000Avzc5IAB")
RT_EXPANSION = "0124R000001JIhxQAG"
RT_RENEWAL   = "0124R000001UuQqQAK"

MOTION_COLORS = {
    "Strategic": "#0047FF",
    "HV":        _C["blue"],
    "Expansion": _C["green"],
    "Other":     _C["gray_mid"],
}
MOTION_ORDER = ["Strategic", "HV", "Expansion", "Other"]


# ── Metric explainer copy ─────────────────────────────────────────────────────
# Single source of truth for all panel copy. Audience is the CRO, CFO and sales
# leadership — plain English, no field names, no SQL. Edit here, not in the
# render calls below. `why` renders as an always-visible caption under each
# panel title; `what` renders in the help tooltip on the panel's headline
# metric. Neither goes in an expander.
METRIC_COPY = {
    "outbound_per_rep": {
        "what": (
            "Emails, calls, and LinkedIn messages sent by each rep per week, "
            "counted from Outreach activity synced to Salesforce."
        ),
        "why": (
            "The clearest read on how hard the team is working. If this flattens "
            "while lead volume climbs, reps are at capacity and more spend will "
            "pile up leads nobody works."
        ),
    },
    "contacts_touched": {
        "what": "Distinct people who received at least one rep touch in the week.",
        "why": (
            "Separates working more accounts from working the same accounts "
            "harder. Both are real, but they mean different things for a budget "
            "decision."
        ),
    },
    "touches_per_contact": {
        "what": (
            "How many outbound touches each contact receives, bucketed into 1, "
            "2, and 3 or more."
        ),
        "why": (
            "Three touches is the threshold where response rates jump. The share "
            "of contacts getting fewer than three is the clearest sign the team "
            "is spread too thin."
        ),
    },
    "quadrant": {
        "what": (
            "Every touched contact sorted by how much effort they absorbed "
            "against whether they replied or took a meeting."
        ),
        "why": (
            "Shows where effort converts and where it evaporates. The "
            "high-effort, no-response group is the most expensive segment we "
            "have and the best place to change targeting or messaging."
        ),
    },
    "speed_to_lead": {
        "what": (
            "Time from a demo request being submitted to the first rep action we "
            "can see against that contact."
        ),
        "why": (
            "Inbound demo requests go cold fast, so response time directly "
            "affects conversion. Read this alongside the tracking caveat below — "
            "it is currently as much a measure of what gets logged as of how "
            "fast the team moves."
        ),
    },
    "qualified_opps": {
        "what": (
            "Opportunities reaching qualified stage each month, divided by the "
            "reps carrying them, split by sales motion."
        ),
        "why": (
            "The output side of capacity. Rising touches with flat qualified "
            "opps means added effort isn't producing pipeline — a signal to fix "
            "targeting before adding either budget or headcount. Monthly counts "
            "are small, so read the trend, not any single month."
        ),
    },
    "quality_of_touch": {
        "what": (
            "A 1–10 rating of how good each outbound email actually is, "
            "combining a rubric defined by sales leadership with the response "
            "the email earned."
        ),
        "why": (
            "Touch volume alone can't tell you whether the team is doing good "
            "work or just going through the motions. When reps get stretched, "
            "the tell is that outreach stays frequent but gets generic — quality "
            "scoring is how we'd catch that early instead of after conversion "
            "drops."
        ),
    },
}

QUALITY_STATUS_LINE = (
    "The underlying data is in place — we have the full text of every 1:1 email "
    "reps have sent. What's still needed is the scoring rubric from sales "
    "leadership defining what a 9 or 10 looks like. Once that exists, this panel "
    "will track average quality per rep over time alongside the volume metrics "
    "above."
)

SPEED_TO_LEAD_CAVEAT = (
    "Reflects only follow-up logged through Outreach and Salesforce. Roughly "
    "half of inbound follow-up runs through Lemlist and Heyreach, which do not "
    "sync back to Salesforce, and reps complete only ~15% of assigned demo "
    "tasks. Treat as a floor on responsiveness and a measure of logging "
    "coverage — not a team-level SLA."
)

# Quadrant cell labels and their one-line explanations.
QUADRANT_CELLS = [
    ("high_effort", "high_intent", "Working as intended",
     "Sustained outreach, contact engaged back. What good looks like.",
     _C["green"]),
    ("high_effort", "no_intent", "Effort spent, nothing back",
     "Heavy outreach, no response. Either the wrong targets or the wrong "
     "message. This is the quadrant to attack.",
     _C["red"]),
    ("low_effort", "high_intent", "Easy wins",
     "Engaged after minimal outreach. Strong fit signal; worth studying what "
     "these have in common.",
     _C["blue"]),
    ("low_effort", "no_intent", "Under-worked",
     "One or two touches, no response. We don't yet know whether these are bad "
     "targets or just abandoned too early.",
     _C["orange"]),
]

# ── Methodology notes ─────────────────────────────────────────────────────────
# Judgement calls baked into this page. Rendered visibly near the top of the
# page (not in an expander) so nobody has to read the code to know what was
# decided. Add to this list whenever a definition changes.
METHODOLOGY_NOTES = [
    ("Outbound is split into three teams, never blended",
     "Reps run new business (Addison, Evan, James). ABM is the high-volume "
     "account programme (Laurel). CS works existing customers (Taha, Cheyenne, "
     "Albright, Neil). Every per-rep figure divides within a team and the trend "
     "charts them separately — a single blended average is what let one "
     "programme's volume read as team-wide capacity."),
    ("Volume is not capacity unless it converts",
     "In 2026 the ABM programme logged 2,859 outbound touches and produced 3 "
     "meetings — about 1 per thousand, against 36–248 for everyone else. The "
     "Feb–Apr spike in touch volume was largely this programme, so read the "
     "per-team lines before concluding effort has fallen since April."),
    ("Some owners are counted nowhere",
     "The VP of Marketing (since departed) and the VP of Sales are excluded: one "
     "isn't comparable to a carrying rep, the other only supports relationships. "
     "A Solutions Engineer is excluded as technical support on live deals. Their "
     "volume is shown as an excluded-by-design line, not hidden."),
    ("\"Per rep\" means these teams, not everyone in Salesforce",
     "Counting every task and meeting owner instead would pull in 41 people — the "
     "CEO, finance, legal, solutions engineers and anyone who merely sat in on a "
     "meeting — and understate outbound per rep by roughly 3x."),
    ("Inbound calls and LinkedIn profile views are not outbound touches",
     "Salesforce logs both alongside genuine outreach. Counting an inbound call "
     "as a rep touch flatters effort, and looking at someone's LinkedIn profile "
     "isn't contact with them. Both are excluded (61 tasks in 2026)."),
    ("What counts as a touch",
     "Only Outreach-logged activity: outbound emails, calls, and LinkedIn/other "
     "messages. Inbound email replies and non-recurring meetings count as intent "
     "signals, not as touches."),
    ("Bulk email tracking is excluded",
     "Newsletter send/open/click logs are attributed to whoever owns the record, "
     "not to a rep doing work. Two reps carry ~16K each. Counting them would "
     "inflate touch volume roughly 5x and make capacity look fine when it isn't."),
    ("Renewals are excluded from qualified opportunities",
     "Renewal-record-type opportunities are the single largest bucket (68 so far "
     "in 2026, vs 50 Strategic and 36 HV) but they're customer-success driven, "
     "not the output of outbound effort. Including them would flatter the "
     "per-rep numbers on a page about new pipeline capacity."),
    ("Departed reps stay in the trend",
     "Reps who have since left still own their historical touches, so excluding "
     "them would make past weeks look artificially quiet. Rep roster excludes "
     "ops and system accounts only."),
    ("Speed to lead is anchored on the HubSpot form submission",
     "Salesforce auto-creates a mirror task seconds after a form fill. Using "
     "that would show a fake ~6-minute response time. The demo task only counts "
     "as a rep action once it is actually completed."),
    ("Recurring meeting instances are tracked separately",
     "Standing internal meetings would otherwise read as fresh engagement, and "
     "Salesforce holds recurring instances dated out to 2028."),
    ("Effort and intent thresholds",
     f"High effort is {HIGH_EFFORT_THRESHOLD}+ outbound touches to one contact. "
     "High intent is at least one email reply or one non-recurring meeting."),
    ("Grain",
     "Touch metrics are weekly. Speed to lead and qualified opportunities are "
     "monthly — the counts are small enough that weekly is noise."),
    ("Channel volumes are lopsided",
     "Of 2026 outbound, email is 13,693 touches, LinkedIn 209 and calls 86. The "
     "channel filter works, but a LinkedIn or call trend line is directional at "
     "best — don't read a slope into a handful of touches a week."),
]


# ── Brand CSS ─────────────────────────────────────────────────────────────────
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

/* Headline dose-response callout */
.dose-band {{
    display: flex;
    gap: 0;
    border: 2px solid {_C["black"]};
    margin-bottom: 8px;
}}
.dose-cell {{
    flex: 1;
    padding: 18px 22px;
    border-right: 1px solid {_C["gray_light"]};
}}
.dose-cell:last-child {{ border-right: none; }}
.dose-label {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: {_C["gray_dark"]};
    margin-bottom: 6px;
}}
.dose-value {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    font-size: 40px;
    line-height: 1;
    color: {_C["black"]};
}}
.dose-sub {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 12px;
    color: {_C["gray_mid"]};
    margin-top: 6px;
}}

/* Methodology notes card */
.notes-card {{
    border: 1px solid {_C["gray_light"]};
    border-left: 3px solid {_C["blue"]};
    background: #FAFAFA;
    padding: 16px 20px 8px 20px;
    margin-bottom: 8px;
}}
.notes-title {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    font-size: 15px;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    color: {_C["black"]};
    margin: 0 0 10px 0;
}}
.notes-card ul {{ margin: 0; padding-left: 18px; }}
.notes-card li {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 13px;
    color: {_C["gray_dark"]};
    line-height: 1.5;
    margin-bottom: 7px;
}}
.notes-card li b {{ color: {_C["black"]}; }}

/* Effort x intent quadrant */
.quad-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
}}
.quad-cell {{
    border: 1px solid {_C["gray_light"]};
    border-top: 3px solid var(--accent);
    padding: 16px 18px;
    background: {_C["white"]};
}}
.quad-name {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    font-size: 16px;
    color: var(--accent);
    margin: 0 0 2px 0;
}}
.quad-cohort {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    color: {_C["gray_mid"]};
    margin-bottom: 10px;
}}
.quad-value {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    font-size: 30px;
    line-height: 1;
    color: {_C["black"]};
}}
.quad-value span {{
    font-size: 14px;
    color: {_C["gray_dark"]};
    font-family: 'IBM Plex Sans', sans-serif;
    font-weight: 400;
}}
.quad-meta {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 12px;
    color: {_C["gray_mid"]};
    margin: 6px 0 10px 0;
}}
.quad-expl {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 13px;
    color: {_C["gray_dark"]};
    line-height: 1.45;
    margin: 0;
}}

/* Phase-2 placeholder panel — same card styling as the real panels */
.dev-card {{
    border: 1px solid {_C["gray_light"]};
    border-top: 3px solid {_C["gray_mid"]};
    padding: 18px 20px;
    background: {_C["white"]};
}}
.dev-badge {{
    display: inline-block;
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: {_C["white"]};
    background: {_C["gray_mid"]};
    padding: 3px 9px;
    border-radius: 2px;
    vertical-align: middle;
    margin-left: 10px;
}}
.dev-status {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 13px;
    color: {_C["gray_dark"]};
    line-height: 1.55;
    border-left: 2px solid {_C["gray_light"]};
    padding-left: 14px;
    margin-top: 12px;
}}
</style>
"""


# ── Page setup ────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Sales Capacity", layout="wide")
st.markdown(_BRAND_CSS, unsafe_allow_html=True)

if _PNG_FILE.exists():
    st.logo(str(_PNG_FILE))

st.markdown(
    f'<div class="ntop-header">'
    f'{_LOGO_IMG_HTML}'
    f'<div class="ntop-header-text">'
    f'<div class="ntop-page-title">Sales Capacity &amp; Touch Quality</div>'
    f'<div class="ntop-page-subtitle">'
    f'Have we hit sales capacity, or is there room to push more volume? '
    f'Touch effort, where it converts, and how fast inbound gets worked.'
    f'</div></div></div>',
    unsafe_allow_html=True,
)


# ── Auth ──────────────────────────────────────────────────────────────────────
@st.cache_resource
def get_credentials():
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    try:
        if "gcp_service_account" in st.secrets:
            return service_account.Credentials.from_service_account_info(
                st.secrets["gcp_service_account"], scopes=scopes
            )
    except Exception:
        pass
    return service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=scopes
    )


@st.cache_resource
def get_bq_client():
    return bigquery.Client(credentials=get_credentials(), project=GCP_PROJECT)


def _run(query: str, params: list | None = None) -> pd.DataFrame:
    cfg = bigquery.QueryJobConfig(query_parameters=params or [])
    return (
        get_bq_client()
        .query(query, job_config=cfg)
        .to_dataframe(create_bqstorage_client=False)
    )


def _dates(start: date, end: date) -> list:
    return [
        bigquery.ScalarQueryParameter("start_date", "DATE", start),
        bigquery.ScalarQueryParameter("end_date", "DATE", end),
    ]


def _dates_reps(start: date, end: date, reps: tuple[str, ...],
                channels: tuple[str, ...] = ()) -> list:
    """
    Params for the touch queries. Team membership goes in as arrays rather than
    interpolated SQL — rep names originate from warehouse data via the picker, so
    they never get concatenated into query text.
    """
    return _dates(start, end) + [
        bigquery.ArrayQueryParameter("reps", "STRING", list(reps)),
        bigquery.ArrayQueryParameter("channels", "STRING", list(channels)),
        bigquery.ArrayQueryParameter("t_reps", "STRING", TEAMS["Reps"]),
        bigquery.ArrayQueryParameter("t_abm", "STRING", TEAMS["ABM"]),
        bigquery.ArrayQueryParameter("t_cs", "STRING", TEAMS["CS"]),
    ]


# ── SQL building blocks ───────────────────────────────────────────────────────
# Ops and system record owners. Deliberately NOT filtered on is_active —
# departed reps still own historical touches and must stay in trend data.
_OPS_USERS = """
ops_users AS (
  SELECT id FROM `bi-ntop.salesforce.user`
  WHERE _fivetran_deleted = FALSE
    AND (name IN ('Revenue Operations','Hubspot Integration',
                  'Service Account Marketo','CS Team')
         OR name LIKE '%Integration%'
         OR name LIKE '%Service Account%')
)
"""

# The selected people, resolved from display names and tagged with their team.
# This is an allowlist, so it also subsumes the ops/system-account exclusion
# above — a service account can never be on it.
_SEL_USERS = """
sel_users AS (
  SELECT id, name,
    CASE WHEN name IN UNNEST(@t_reps) THEN 'Reps'
         WHEN name IN UNNEST(@t_abm)  THEN 'ABM'
         WHEN name IN UNNEST(@t_cs)   THEN 'CS'
         ELSE 'Unassigned' END AS team
  FROM `bi-ntop.salesforce.user`
  WHERE _fivetran_deleted = FALSE
    AND name IN UNNEST(@reps)
)
"""

# Touch bucketing, matched on task subject.
#
# TRAP 1: only these subject patterns are touches. `Sent %`, `Opened %` and
# `Clicked %` tasks are bulk-newsletter tracking logs attributed to whoever owns
# the record — including them inflates touch counts ~5x. They fall through this
# CASE to NULL and are dropped by `WHERE touch_type IS NOT NULL`.
#
# TRAP 4: `Submitted Form%` tasks are duplicate mirrors of HubSpot form
# submissions and are likewise not matched here.
#
# TRAP 8: `[Outreach] [Call]` carries a direction marker that appears in mixed
# case — [outbound] and [Outbound] — but ALSO as [inbound]. Matching the bucket
# loosely (the original spec's `LIKE '%[outreach] [call]%'`) counted 27 inbound
# calls in 2026 as outbound touches. Direction is now matched explicitly and
# inbound calls get their own non-outbound bucket.
#
# TRAP 9: `LinkedIn: View a Profile` is a passive research action, not outreach
# to the contact. It sat inside the old catch-all `[Outreach] [Other]%` bucket
# and counted as a touch — 34 in 2026. Now bucketed separately and excluded.
# Its branch must stay ABOVE the generic Other branch or it gets swallowed.
_TOUCH_CASE = """
CASE WHEN t.subject LIKE '[Outreach] [Email] [Out]%' THEN 'email_out'
     WHEN t.subject LIKE '[Outreach] [Email] [In]%'  THEN 'email_in'
     WHEN LOWER(t.subject) LIKE '[outreach] [call] [outbound]%' THEN 'call'
     WHEN LOWER(t.subject) LIKE '[outreach] [call] [inbound]%'  THEN 'call_in'
     WHEN t.subject LIKE '[Outreach] [Other] LinkedIn: View a Profile%'
                                                     THEN 'li_profile_view'
     WHEN t.subject LIKE '[Outreach] [Other] LinkedIn:%' THEN 'linkedin'
     WHEN t.subject LIKE '[Outreach] [Other]%'       THEN 'other_channel'
     WHEN t.subject LIKE 'Catalyst Note%'            THEN 'catalyst'
END
"""

# Which touch types count as outbound is driven by the channel filter.
_OUTBOUND_TYPES = "UNNEST(@channels)"

# Outbound-only subject match, for the queries that don't need bucket labels.
# Mirrors _TOUCH_CASE exactly, including traps 8 and 9.
_OUTBOUND_SUBJECT_MATCH = """
(subject LIKE '[Outreach] [Email] [Out]%'
 OR LOWER(subject) LIKE '[outreach] [call] [outbound]%'
 OR (subject LIKE '[Outreach] [Other]%'
     AND subject NOT LIKE '[Outreach] [Other] LinkedIn: View a Profile%'))
"""

# TRAP 2: `salesforce.event` holds future-dated recurring instances out to 2028.
# Every event read is bounded by CURRENT_DATE() as well as the user's end date,
# so the trend can't grow phantom future rows.
_EVENT_END_BOUND = "LEAST(@end_date, CURRENT_DATE())"


# ── Data loaders ──────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def load_weekly_facts(start: date, end: date, reps: tuple[str, ...],
                      channels: tuple[str, ...], by_team: bool) -> pd.DataFrame:
    """
    Weekly fact table — the primary query for this tab.

    `by_team=True` returns one row per week per team; False aggregates across all
    selected people. The per-team form is what the trend charts use.
    """
    dim = "team, " if by_team else ""
    grp = "wk, team" if by_team else "wk"
    query = f"""
    WITH {_SEL_USERS},
    touches AS (
      SELECT DATE_TRUNC(DATE(t.created_date), WEEK(MONDAY)) AS wk,
             s.team, t.owner_id, t.who_id,
             {_TOUCH_CASE} AS touch_type
      FROM `bi-ntop.salesforce.task` t
      JOIN sel_users s ON s.id = t.owner_id
      WHERE t._fivetran_deleted = FALSE          -- TRAP 7
        AND DATE(t.created_date) BETWEEN @start_date AND @end_date
        AND t.who_id IS NOT NULL
      UNION ALL
      SELECT DATE_TRUNC(DATE(e.start_date_time), WEEK(MONDAY)),
             s.team, e.owner_id, e.who_id,
             IF(e.is_child, 'meeting_recurring', 'meeting')
      FROM `bi-ntop.salesforce.event` e
      JOIN sel_users s ON s.id = e.owner_id
      WHERE e._fivetran_deleted = FALSE          -- TRAP 7
        AND DATE(e.start_date_time)
            BETWEEN @start_date AND {_EVENT_END_BOUND}   -- TRAP 2
        AND e.who_id IS NOT NULL
    )
    SELECT wk, {dim}
      COUNT(DISTINCT owner_id) AS active_reps,
      COUNT(DISTINCT who_id)   AS contacts_touched,
      COUNTIF(touch_type IN {_OUTBOUND_TYPES}) AS outbound,
      COUNTIF(touch_type = 'email_in')          AS replies,
      COUNTIF(touch_type = 'meeting')           AS meetings,
      COUNTIF(touch_type = 'meeting_recurring') AS meetings_recurring,
      ROUND(COUNTIF(touch_type IN {_OUTBOUND_TYPES})
            / NULLIF(COUNT(DISTINCT owner_id),0), 1) AS outbound_per_rep
    FROM touches
    WHERE touch_type IS NOT NULL
    GROUP BY {grp}
    ORDER BY {grp}
    """
    return _run(query, _dates_reps(start, end, reps, channels))


@st.cache_data(ttl=3600)
def load_touches_per_contact(start: date, end: date, reps: tuple[str, ...],
                             channels: tuple[str, ...]) -> pd.DataFrame:
    """Distribution of outbound touches per contact, bucketed 1 / 2 / 3+."""
    query = f"""
    WITH {_SEL_USERS},
    per_contact AS (
      SELECT t.who_id, COUNT(*) AS outbound
      FROM `bi-ntop.salesforce.task` t
      WHERE t._fivetran_deleted = FALSE
        AND DATE(t.created_date) BETWEEN @start_date AND @end_date
        AND t.who_id IS NOT NULL
        AND t.owner_id IN (SELECT id FROM sel_users)
        AND {_TOUCH_CASE} IN {_OUTBOUND_TYPES}
      GROUP BY 1
    )
    SELECT
      CASE WHEN outbound = 1 THEN '1 touch'
           WHEN outbound = 2 THEN '2 touches'
           ELSE '{HIGH_EFFORT_THRESHOLD}+ touches' END AS bucket,
      COUNT(*) AS contacts,
      ROUND(AVG(outbound),1) AS avg_outbound
    FROM per_contact
    GROUP BY 1
    ORDER BY 1
    """
    return _run(query, _dates_reps(start, end, reps, channels))


@st.cache_data(ttl=3600)
def load_quadrant(start: date, end: date, reps: tuple[str, ...],
                  channels: tuple[str, ...], by_team: bool = False
                  ) -> pd.DataFrame:
    """
    Effort x intent quadrant, one row per contact cohort.

    Effort respects the channel filter; intent does not. A reply or a meeting is
    an outcome, not a channel — narrowing to LinkedIn should change how much
    effort we count, not whether the contact responded.

    With `by_team=True`, effort and intent are counted per (team, contact): a
    contact worked by two teams appears under both with only that team's touches.
    That's the right scope for comparing motions, and it's why the per-team rows
    don't sum to the combined figure.
    """
    dim  = "team, " if by_team else ""
    pgrp = "team, who_id" if by_team else "who_id"
    ggrp = "1,2,3" if by_team else "1,2"
    query = f"""
    WITH {_SEL_USERS},
    u AS (
      SELECT s.team, t.who_id,
        CASE WHEN {_TOUCH_CASE} IN {_OUTBOUND_TYPES}       THEN 'out'
             WHEN t.subject LIKE '[Outreach] [Email] [In]%' THEN 'reply'
        END AS k
      FROM `bi-ntop.salesforce.task` t
      JOIN sel_users s ON s.id = t.owner_id
      WHERE t._fivetran_deleted = FALSE
        AND DATE(t.created_date) BETWEEN @start_date AND @end_date
        AND t.who_id IS NOT NULL
      UNION ALL
      -- is_child = FALSE: recurring instances are excluded from intent
      SELECT s.team, e.who_id, 'meeting'
      FROM `bi-ntop.salesforce.event` e
      JOIN sel_users s ON s.id = e.owner_id
      WHERE e._fivetran_deleted = FALSE
        AND e.is_child = FALSE
        AND DATE(e.start_date_time)
            BETWEEN @start_date AND {_EVENT_END_BOUND}   -- TRAP 2
        AND e.who_id IS NOT NULL
    ),
    per AS (
      SELECT {pgrp},
        COUNTIF(k = 'out')     AS outbound,
        COUNTIF(k = 'reply')   AS replies,
        COUNTIF(k = 'meeting') AS meetings
      FROM u WHERE k IS NOT NULL GROUP BY {pgrp}
    )
    SELECT {dim}
      IF(outbound >= {HIGH_EFFORT_THRESHOLD}, 'high_effort', 'low_effort') AS effort,
      IF(replies > 0 OR meetings > 0, 'high_intent', 'no_intent') AS intent,
      COUNT(*) AS contacts,
      ROUND(AVG(outbound),1) AS avg_outbound
    FROM per
    WHERE outbound > 0
    GROUP BY {ggrp}
    """
    return _run(query, _dates_reps(start, end, reps, channels))


@st.cache_data(ttl=3600)
def load_speed_to_lead(start: date, end: date) -> pd.DataFrame:
    """
    Monthly speed to lead for demo requests.

    TRAP 3/4: anchored on the HubSpot form submission timestamp, not the
    Salesforce `Submitted Form` mirror task and not the auto-created
    `nTop Demo Request` task's created_date (which would show a fake
    ~6-minute median). The demo task counts only once actually completed.
    """
    query = f"""
    WITH demo_forms AS (
      SELECT hc.property_salesforcecontactid AS cid,
             MIN(cfs.timestamp) AS lead_ts
      FROM `bi-ntop.hubspot.contact_form_submission` cfs
      -- TRAP 6: hubspot.form's primary key is `guid`, not `id`
      JOIN `bi-ntop.hubspot.form` f
        ON f.guid = cfs.form_id AND f.name = 'Request a Demo'
      -- TRAP 7: bridge tables like contact_form_submission have no
      -- _fivetran_deleted column; core entities like contact do
      JOIN `bi-ntop.hubspot.contact` hc
        ON hc.id = cfs.contact_id AND hc._fivetran_deleted = FALSE
      WHERE DATE(cfs.timestamp) BETWEEN @start_date AND @end_date
        AND hc.property_salesforcecontactid IS NOT NULL
      GROUP BY 1
    ),
    -- pre-conversion activity can sit under the LEAD id, not the contact id
    ids AS (
      SELECT cid, lead_ts, cid AS who_id FROM demo_forms
      UNION ALL
      SELECT d.cid, d.lead_ts, l.id
      FROM demo_forms d
      JOIN `bi-ntop.salesforce.lead` l
        ON l.converted_contact_id = d.cid AND l._fivetran_deleted = FALSE
    ),
    rep_actions AS (
      -- first_touch must be >= lead_ts >= @start_date, so bounding the scan
      -- here is equivalent and much cheaper than reading all 3.3M tasks
      SELECT who_id, created_date AS ts
      FROM `bi-ntop.salesforce.task`
      WHERE _fivetran_deleted = FALSE
        AND DATE(created_date) >= @start_date
        AND {_OUTBOUND_SUBJECT_MATCH}
      UNION ALL
      -- TRAP 3: demo task counts only when WORKED, via completion timestamp
      SELECT who_id, completed_date_time
      FROM `bi-ntop.salesforce.task`
      WHERE _fivetran_deleted = FALSE
        AND subject LIKE 'nTop Demo Request%'
        AND status = 'Completed'
        AND completed_date_time IS NOT NULL
        AND DATE(completed_date_time) >= @start_date
      UNION ALL
      SELECT who_id, start_date_time
      FROM `bi-ntop.salesforce.event`
      WHERE _fivetran_deleted = FALSE
        AND is_child = FALSE
        AND DATE(start_date_time)
            BETWEEN @start_date AND {_EVENT_END_BOUND}   -- TRAP 2
    ),
    ft AS (
      SELECT i.cid, MIN(r.ts) AS first_touch
      FROM ids i
      JOIN rep_actions r ON r.who_id = i.who_id AND r.ts >= i.lead_ts
      GROUP BY 1
    )
    SELECT
      DATE_TRUNC(DATE(d.lead_ts), MONTH) AS mo,
      COUNT(*) AS demo_requests,
      ROUND(100*COUNTIF(ft.first_touch IS NOT NULL)/COUNT(*),0) AS pct_touched,
      ROUND(APPROX_QUANTILES(
        TIMESTAMP_DIFF(ft.first_touch, d.lead_ts, MINUTE),100)[OFFSET(50)]/60,1)
        AS median_hrs,
      ROUND(100*COUNTIF(
        TIMESTAMP_DIFF(ft.first_touch, d.lead_ts, HOUR) <= 24)/COUNT(*),0)
        AS pct_within_24h
    FROM demo_forms d
    LEFT JOIN ft ON ft.cid = d.cid
    -- ROLLUP adds a mo IS NULL row holding the true whole-window figures, so the
    -- headline median is a real median rather than an average of monthly medians
    GROUP BY ROLLUP(1)
    ORDER BY 1
    """
    return _run(query, _dates(start, end))


@st.cache_data(ttl=3600)
def load_qualified_opps(start: date, end: date) -> pd.DataFrame:
    """
    Monthly qualified opportunities per rep, split by motion.

    Monthly grain, not weekly — single-digit counts per rep per month already.
    Renewals are excluded; see METHODOLOGY_NOTES.
    """
    query = f"""
    SELECT
      DATE_TRUNC(o.qualified_opportunity_date_c, MONTH) AS mo,
      CASE WHEN o.record_type_id IN {RT_HV}        THEN 'HV'
           WHEN o.record_type_id IN {RT_STRATEGIC} THEN 'Strategic'
           WHEN o.record_type_id = '{RT_EXPANSION}' THEN 'Expansion'
           ELSE 'Other' END AS motion,
      COUNT(*) AS qualified_opps,
      COUNT(DISTINCT o.owner_id) AS reps,
      ROUND(COUNT(*)/NULLIF(COUNT(DISTINCT o.owner_id),0),1) AS per_rep
    FROM `bi-ntop.salesforce.opportunity` o
    WHERE o._fivetran_deleted = FALSE
      AND o.stage_name <> 'Rejected'
      AND o.record_type_id <> '{RT_RENEWAL}'
      AND o.qualified_opportunity_date_c BETWEEN @start_date AND @end_date
    GROUP BY 1,2
    ORDER BY 1,2
    """
    return _run(query, _dates(start, end))


@st.cache_data(ttl=3600)
def load_rep_options(start: date, end: date) -> pd.DataFrame:
    """
    Everyone who owns a touch in the window, with their outbound volume.

    Populates the sidebar rep picker and drives the roster staleness check, so
    this one deliberately does NOT apply the roster filter — it uses only the
    ops/system-account exclusion.
    """
    query = f"""
    WITH {_OPS_USERS},
    touches AS (
      SELECT t.owner_id,
             IF({_OUTBOUND_SUBJECT_MATCH.replace('subject', 't.subject')}, 1, 0)
               AS is_outbound
      FROM `bi-ntop.salesforce.task` t
      WHERE t._fivetran_deleted = FALSE
        AND DATE(t.created_date) BETWEEN @start_date AND @end_date
        AND t.who_id IS NOT NULL
        AND t.owner_id NOT IN (SELECT id FROM ops_users)
        AND {_TOUCH_CASE} IS NOT NULL
      UNION ALL
      SELECT e.owner_id, 0
      FROM `bi-ntop.salesforce.event` e
      WHERE e._fivetran_deleted = FALSE
        AND DATE(e.start_date_time)
            BETWEEN @start_date AND {_EVENT_END_BOUND}   -- TRAP 2
        AND e.who_id IS NOT NULL
        AND e.owner_id NOT IN (SELECT id FROM ops_users)
    )
    SELECT COALESCE(us.name, t.owner_id) AS rep,
           us.title,
           SUM(t.is_outbound) AS outbound,
           COUNT(*) AS all_touches
    FROM touches t
    LEFT JOIN `bi-ntop.salesforce.user` us
      ON us.id = t.owner_id AND us._fivetran_deleted = FALSE
    GROUP BY 1,2
    ORDER BY outbound DESC, all_touches DESC
    """
    return _run(query, _dates(start, end))


@st.cache_data(ttl=3600)
def load_per_rep(start: date, end: date, reps: tuple[str, ...],
                 channels: tuple[str, ...]) -> pd.DataFrame:
    """Per-person drill-down over the selected window."""
    query = f"""
    WITH {_SEL_USERS},
    touches AS (
      SELECT t.owner_id, t.who_id, {_TOUCH_CASE} AS touch_type
      FROM `bi-ntop.salesforce.task` t
      WHERE t._fivetran_deleted = FALSE
        AND DATE(t.created_date) BETWEEN @start_date AND @end_date
        AND t.who_id IS NOT NULL
        AND t.owner_id IN (SELECT id FROM sel_users)
      UNION ALL
      SELECT e.owner_id, e.who_id,
             IF(e.is_child, 'meeting_recurring', 'meeting')
      FROM `bi-ntop.salesforce.event` e
      WHERE e._fivetran_deleted = FALSE
        AND DATE(e.start_date_time)
            BETWEEN @start_date AND {_EVENT_END_BOUND}   -- TRAP 2
        AND e.who_id IS NOT NULL
        AND e.owner_id IN (SELECT id FROM sel_users)
    ),
    clean AS (SELECT * FROM touches WHERE touch_type IS NOT NULL),
    per_rep_contact AS (
      SELECT owner_id, who_id,
             COUNTIF(touch_type IN {_OUTBOUND_TYPES}) AS ob
      FROM clean GROUP BY 1,2
    ),
    agg AS (
      SELECT owner_id,
        COUNT(DISTINCT who_id) AS contacts_touched,
        COUNTIF(touch_type IN {_OUTBOUND_TYPES}) AS outbound,
        COUNTIF(touch_type = 'email_in')          AS replies,
        COUNTIF(touch_type = 'meeting')           AS meetings,
        COUNTIF(touch_type = 'catalyst')          AS catalyst_notes
      FROM clean GROUP BY 1
    ),
    bench AS (
      SELECT owner_id,
        COUNTIF(ob >= {HIGH_EFFORT_THRESHOLD}) AS contacts_at_benchmark,
        COUNTIF(ob > 0) AS contacts_with_outbound
      FROM per_rep_contact GROUP BY 1
    )
    SELECT
      COALESCE(s.name, a.owner_id) AS rep,
      s.team,
      a.outbound, a.contacts_touched, a.replies, a.meetings, a.catalyst_notes,
      b.contacts_at_benchmark,
      ROUND(100*b.contacts_at_benchmark
            / NULLIF(b.contacts_with_outbound,0), 0) AS pct_at_benchmark,
      ROUND(a.outbound / NULLIF(b.contacts_with_outbound,0), 1)
            AS avg_outbound_per_contact,
      -- the yield column: meetings booked per 1,000 outbound touches. This is
      -- what separated the ABM programme from rep work by two orders of magnitude
      ROUND(1000 * a.meetings / NULLIF(a.outbound,0), 1) AS meetings_per_1k
    FROM agg a
    LEFT JOIN bench b ON b.owner_id = a.owner_id
    LEFT JOIN sel_users s ON s.id = a.owner_id
    ORDER BY a.outbound DESC
    """
    return _run(query, _dates_reps(start, end, reps, channels))


# ── Chart helpers ─────────────────────────────────────────────────────────────
def _base_layout(fig: go.Figure, title: str, grain: str, height: int = 420) -> None:
    tick_fmt = "%b %Y" if grain == "Monthly" else "%b %d"
    fig.update_layout(
        title=dict(text=title, font=dict(family="Oswald", size=20, color=_C["black"])),
        height=height,
        margin=dict(l=60, r=40, t=60, b=40),
        hovermode="x unified",
        plot_bgcolor="white",
        paper_bgcolor="white",
        legend=dict(
            orientation="h",
            yanchor="bottom", y=1.02,
            xanchor="left", x=0,
            font=dict(size=12, family="IBM Plex Sans"),
        ),
        xaxis=dict(tickformat=tick_fmt, showgrid=True, gridcolor="#F0F0F0"),
        yaxis=dict(
            showgrid=True, gridcolor="#F0F0F0",
            tickformat=",d", rangemode="tozero",
        ),
    )


def _prep_weeks(df: pd.DataFrame, start: date, end: date, today: date
                ) -> tuple[pd.DataFrame, bool]:
    """
    Drop weeks the date filter only partly covers — those rows are artifacts of
    the filter, not real dips. The current in-progress week is kept but flagged.
    Returns (df, current_week_included).
    """
    if df.empty:
        return df, False

    this_monday = today - timedelta(days=today.weekday())

    def _keep(wk: date) -> bool:
        if wk < start:                 # leading week, cut by the From date
            return False
        if wk == this_monday:          # current week, in progress
            return True
        return wk + timedelta(days=6) <= end

    d = df.copy()
    d["_wk"] = pd.to_datetime(d["wk"]).dt.date
    d = d[d["_wk"].map(_keep)]
    has_current = bool((d["_wk"] == this_monday).any())
    return d.drop(columns="_wk"), has_current


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Filters")
    today = date.today()

    start_date = st.date_input(
        "From", value=DEFAULT_START, min_value=DATA_START, max_value=today
    )
    end_date = st.date_input(
        "To", value=today, min_value=DATA_START, max_value=today
    )

    if start_date > end_date:
        st.error("'From' must be on or before 'To'.")
        st.stop()

    st.divider()

    # Team layer. Outbound splits into three motions that mean different things;
    # averaging them together is how one programme's volume masked the rest.
    selected_teams = st.multiselect(
        "Teams",
        options=TEAM_ORDER,
        default=TEAM_ORDER,
        help=(
            "Reps run new business, ABM runs the high-volume account programme, "
            "CS works existing customers. The weekly trend charts them separately "
            "and every 'per rep' figure divides within a team."
        ),
    )

    # Rep picker, scoped to the chosen teams. Options are everyone who owns a
    # touch in the window, ordered by outbound volume, so anyone unassigned who is
    # doing real work is near the top and easy to spot.
    rep_opts_df = load_rep_options(start_date, end_date)
    seen = rep_opts_df["rep"].tolist()

    team_members = [
        n for t in selected_teams for n in TEAMS[t] if n in seen
    ]
    other_active = [
        n for n in seen
        if n not in {m for ms in TEAMS.values() for m in ms}
        and n not in EXCLUDED_OWNERS
    ]

    selected_reps = st.multiselect(
        "People",
        options=team_members + other_active,
        default=team_members,
        help=(
            "Defaults to everyone on the selected teams. Anyone unassigned who "
            "owns touches in this window is listed after them so they can be "
            "pulled in ad hoc."
        ),
    )

    st.divider()

    selected_channels = st.multiselect(
        "Touch channels",
        options=list(CHANNELS),
        default=list(CHANNELS),
        help=(
            "Which channels count as an outbound touch. Replies and meetings are "
            "outcomes rather than channels, so they are never filtered here — "
            "narrowing to LinkedIn changes how much effort is counted, not "
            "whether the contact responded."
        ),
    )

    st.caption(
        f"{len(selected_reps)} of {len(seen)} touch owners selected across "
        f"{len(selected_teams)} team(s). Outreach activity is only reliably "
        f"logged from January 2026 onward. Data is cached for one hour."
    )

REPS      = tuple(selected_reps)
CHANS     = tuple(CHANNELS[c] for c in selected_channels)
TEAMS_ON  = [t for t in TEAM_ORDER if t in selected_teams]
# Which team each selected person belongs to, for labelling in Python
REP_TEAM  = {n: t for t, ms in TEAMS.items() for n in ms}


# ── Methodology notes ─────────────────────────────────────────────────────────
_notes_li = "".join(
    f"<li><b>{head}</b> — {body}</li>" for head, body in METHODOLOGY_NOTES
)
st.markdown(
    f'<div class="notes-card">'
    f'<p class="notes-title">How these numbers are built</p>'
    f'<ul>{_notes_li}</ul></div>',
    unsafe_allow_html=True,
)


# ── Roster staleness check ────────────────────────────────────────────────────
# An explicit roster goes stale the moment someone is hired. Rather than let that
# happen quietly, flag anyone outside the selection doing real outbound volume.
_outside = rep_opts_df[
    (~rep_opts_df["rep"].isin(selected_reps))
    & (rep_opts_df["outbound"] >= ROSTER_REVIEW_THRESHOLD)
]
_unrecognised = _outside[~_outside["rep"].isin(EXCLUDED_OWNERS)]
if not _unrecognised.empty:
    _who = ", ".join(
        f"{r.rep} ({int(r.outbound):,} outbound)"
        for r in _unrecognised.itertuples()
    )
    st.warning(
        f"**{len(_unrecognised)} "
        f"{'person' if len(_unrecognised) == 1 else 'people'} outside the "
        f"selected teams logged {ROSTER_REVIEW_THRESHOLD}+ outbound touches in "
        f"this window:** {_who}. If they belong to a team, add them to `TEAMS` in "
        f"the page source; if they shouldn't count at all, add them to "
        f"`EXCLUDED_OWNERS`. Until then their volume is excluded from every "
        f"figure below."
    )

# People with real volume who are deliberately counted nowhere. Surfaced quietly
# rather than hidden — the effort is real work, it just isn't team capacity.
_known_excluded = _outside[_outside["rep"].isin(EXCLUDED_OWNERS)]
if not _known_excluded.empty:
    _known_txt = " · ".join(
        f"{r.rep} — {EXCLUDED_OWNERS[r.rep]} ({int(r.outbound):,} outbound)"
        for r in _known_excluded.itertuples()
    )
    st.caption(f":gray[Excluded by design: {_known_txt}.]")

if not REPS:
    st.info(
        "Nobody selected. Pick at least one team or person in the sidebar to see "
        "the touch metrics. Speed to lead and qualified opportunities are "
        "unaffected."
    )
elif not CHANS:
    st.info(
        "No touch channels selected. Pick at least one in the sidebar — with none "
        "selected there is nothing to count as an outbound touch."
    )

ACTIVE = bool(REPS) and bool(CHANS)

# Team composition, so the reader knows who is in each line on the charts below
if ACTIVE:
    _comp = " · ".join(
        f"**{t}** {', '.join(n for n in TEAMS[t] if n in REPS)}"
        for t in TEAMS_ON if any(n in REPS for n in TEAMS[t])
    )
    _extra = [n for n in REPS if n not in REP_TEAM]
    if _extra:
        _comp += f" · **Unassigned** {', '.join(_extra)}"
    st.caption(_comp)
    if len(selected_channels) < len(CHANNELS):
        st.caption(
            f":gray[Outbound counted from **{', '.join(selected_channels)}** only. "
            f"Replies and meetings are unfiltered.]"
        )


# ── Team-series chart helper ──────────────────────────────────────────────────
def _team_lines(df: pd.DataFrame, col: str, title: str, ylab: str,
                fmt: str = ",d", height: int = 380) -> go.Figure:
    """One line per team on a single y-axis. Never a second axis — two scales on
    one plot make the alignment arbitrary and invent a relationship."""
    fig = go.Figure()
    for team in TEAMS_ON:
        t_df = df[df["team"] == team]
        if t_df.empty:
            continue
        hov = "%{y:.1f}" if fmt == ".1f" else "%{y:,d}"
        fig.add_trace(go.Scatter(
            x=t_df["wk"], y=t_df[col], name=team,
            mode="lines+markers",
            line=dict(color=TEAM_COLORS[team], width=2.5),
            marker=dict(size=5),
            hovertemplate=f"%{{x|%b %d}}: {hov}<extra>{team}</extra>",
        ))
    _base_layout(fig, title, "Weekly", height=height)
    fig.update_layout(yaxis=dict(
        title=ylab, showgrid=True, gridcolor="#F0F0F0",
        tickformat=fmt, rangemode="tozero",
    ))
    return fig


# ── 1. Headline — the dose-response ───────────────────────────────────────────
st.divider()
st.subheader("The 3-touch threshold")
st.caption(
    "The single most useful output of this page: what a third touch is worth. "
    "Everything below is the detail behind it."
)

with st.spinner("Loading touch data…"):
    quad_df = (
        load_quadrant(start_date, end_date, REPS, CHANS)
        if ACTIVE else pd.DataFrame()
    )

if quad_df.empty:
    st.info("No touch data in the selected range.")
    q_lookup: dict = {}
    total_contacts = 0
else:
    q_lookup = {
        (r.effort, r.intent): r for r in quad_df.itertuples()
    }
    total_contacts = int(quad_df["contacts"].sum())


def _cell(effort: str, intent: str, field: str, default=0):
    row = q_lookup.get((effort, intent))
    return getattr(row, field) if row is not None else default


_hi = _cell("high_effort", "high_intent", "contacts")
_hn = _cell("high_effort", "no_intent", "contacts")
_li = _cell("low_effort", "high_intent", "contacts")
_ln = _cell("low_effort", "no_intent", "contacts")

_high_rate = 100 * _hi / (_hi + _hn) if (_hi + _hn) else 0
_low_rate  = 100 * _li / (_li + _ln) if (_li + _ln) else 0
_lift      = _high_rate - _low_rate

st.markdown(
    f'<div class="dose-band">'
    f'<div class="dose-cell">'
    f'<div class="dose-label">3+ touches</div>'
    f'<div class="dose-value" style="color:{_C["green"]};">{_high_rate:.1f}%</div>'
    f'<div class="dose-sub">replied or took a meeting '
    f'&middot; {_hi + _hn:,} contacts</div></div>'
    f'<div class="dose-cell">'
    f'<div class="dose-label">1–2 touches</div>'
    f'<div class="dose-value" style="color:{_C["gray_dark"]};">{_low_rate:.1f}%</div>'
    f'<div class="dose-sub">replied or took a meeting '
    f'&middot; {_li + _ln:,} contacts</div></div>'
    f'<div class="dose-cell">'
    f'<div class="dose-label">Difference</div>'
    f'<div class="dose-value" style="color:{_C["blue"]};">+{_lift:.1f} pts</div>'
    f'<div class="dose-sub">the empirical case for the 3-touch benchmark</div>'
    f'</div></div>',
    unsafe_allow_html=True,
)
st.caption(
    f"All selected teams combined: {total_contacts:,} contacts who received at "
    f"least one outbound touch in this window. A contact counts as engaged if "
    f"they replied by email or took a non-recurring meeting."
)

# The combined figure blends motions that convert very differently, which is the
# same mistake as a single blended per-rep average. Break it out by team.
if ACTIVE and len(TEAMS_ON) > 1:
    with st.spinner("Loading per-team dose-response…"):
        qt_df = load_quadrant(start_date, end_date, REPS, CHANS, True)

    if not qt_df.empty:
        rows = []
        for team in TEAMS_ON:
            t = qt_df[qt_df["team"] == team]
            if t.empty:
                continue
            g = {(r.effort, r.intent): int(r.contacts) for r in t.itertuples()}
            hi = g.get(("high_effort", "high_intent"), 0)
            hn = g.get(("high_effort", "no_intent"), 0)
            lo = g.get(("low_effort", "high_intent"), 0)
            ln_ = g.get(("low_effort", "no_intent"), 0)
            if not (hi + hn) or not (lo + ln_):
                continue
            hr, lr = 100 * hi / (hi + hn), 100 * lo / (lo + ln_)
            rows.append({
                "Team": team,
                "Contacts": hi + hn + lo + ln_,
                f"{HIGH_EFFORT_THRESHOLD}+ touches engage": f"{hr:.1f}%",
                "1–2 touches engage": f"{lr:.1f}%",
                # sign-aware: ABM's dose-response is genuinely negative
                "Difference": f"{hr - lr:+.1f} pts",
            })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True,
                         hide_index=True)
            st.caption(
                "Counted per team, so a contact worked by two teams appears "
                "under both with only that team's touches — which is why these "
                "don't sum to the combined figure above. The threshold holds far "
                "harder for new-business reps than the blended number suggests; "
                "high-volume ABM outreach dilutes it."
            )


# ── 2. Weekly trend ───────────────────────────────────────────────────────────
st.divider()
st.subheader("Weekly effort trend")
st.caption(METRIC_COPY["outbound_per_rep"]["why"])

with st.spinner("Loading weekly trend…"):
    wk_df = (
        load_weekly_facts(start_date, end_date, REPS, CHANS, True)
        if ACTIVE else pd.DataFrame()
    )

wk_df, _has_current_week = _prep_weeks(wk_df, start_date, end_date, today)

if wk_df.empty:
    st.info(
        "No complete weeks in the selected range. Weeks the date filter only "
        "partly covers are left out, because a clipped week reads as a dip that "
        "isn't real — widen the range to see the weekly trend."
    )
else:
    # Drop the in-progress week BY WEEK, not by row — there is one row per week
    # per team now, so slicing off a single row would leave the partial week in
    # and the headline tiles would report it as the latest complete week.
    _wkd = pd.to_datetime(wk_df["wk"]).dt.date
    _this_monday = today - timedelta(days=today.weekday())
    _is_complete = (
        _wkd != _this_monday if _has_current_week
        else pd.Series(True, index=wk_df.index)
    )
    _last_wk = (
        wk_df.loc[_is_complete, "wk"].max() if _is_complete.any()
        else wk_df["wk"].max()
    )
    _latest = wk_df[wk_df["wk"] == _last_wk]

    # One tile per team. Deliberately not a blended average — a single
    # "outbound per rep" across teams running different motions is the number
    # that hid the ABM programme inside team-wide capacity.
    cols = st.columns(max(len(TEAMS_ON), 1))
    for col, team in zip(cols, TEAMS_ON):
        row = _latest[_latest["team"] == team]
        if row.empty:
            col.metric(f"{team} — outbound per rep", "—",
                       help=TEAM_BLURB[team])
            continue
        r = row.iloc[0]
        n_team = len([n for n in TEAMS[team] if n in REPS])
        col.metric(
            f"{team} — outbound per rep",
            f"{r['outbound_per_rep']:.1f}",
            help=f"{TEAM_BLURB[team]}  ·  {METRIC_COPY['outbound_per_rep']['what']}",
        )
        col.caption(
            f"{int(r['outbound']):,} touches · {int(r['active_reps'])} of "
            f"{n_team} active · {int(r['contacts_touched']):,} contacts"
        )

    st.caption(
        f"Latest complete week beginning "
        f"{pd.to_datetime(_last_wk).strftime('%b %d, %Y')}. Each team divides by "
        f"its own reps who logged touches that week."
    )

    st.plotly_chart(
        _team_lines(wk_df, "outbound", "Outbound touches per week, by team",
                    "Outbound touches"),
        use_container_width=True,
    )
    st.plotly_chart(
        _team_lines(wk_df, "outbound_per_rep",
                    "Outbound per rep per week, by team", "Touches per rep",
                    fmt=".1f"),
        use_container_width=True,
    )
    st.caption(
        "Volume and intensity are charted separately rather than on two y-axes — "
        "two scales on one plot make their alignment arbitrary and suggest a "
        "relationship the data doesn't contain."
    )

    if _has_current_week:
        st.caption(
            "The final week is still in progress and will read low until it closes."
        )


# ── 2b. Reach vs engagement ───────────────────────────────────────────────────
if not wk_df.empty:
    st.divider()
    st.subheader("Reach vs engagement")
    st.caption(METRIC_COPY["contacts_touched"]["why"])

    st.plotly_chart(
        _team_lines(wk_df, "contacts_touched",
                    "Distinct contacts touched per week, by team", "Contacts"),
        use_container_width=True,
    )

    # Replies and meetings sum cleanly across teams (they're event counts, not
    # distinct-contact counts), so these are shown for all selected teams at once.
    _out = wk_df.groupby("wk", as_index=False)[
        ["replies", "meetings", "meetings_recurring"]
    ].sum()
    fig2 = go.Figure()
    for col, label, color in [
        ("replies", "Email replies", "#0047FF"),
        ("meetings", "Meetings (non-recurring)", _C["orange"]),
        ("meetings_recurring", "Recurring meeting instances", _C["gray_light"]),
    ]:
        fig2.add_trace(go.Scatter(
            x=_out["wk"], y=_out[col], name=label,
            mode="lines+markers", line=dict(color=color, width=2),
            marker=dict(size=4),
            hovertemplate=f"%{{x|%b %d}}: %{{y:,d}}<extra>{label}</extra>",
        ))
    _base_layout(fig2, "What came back, all selected teams", "Weekly")
    st.plotly_chart(fig2, use_container_width=True)
    st.caption(
        "Replies and meetings are shown across all selected teams combined, and "
        "are never filtered by channel — they're outcomes, not channels. Expect "
        "CS to carry a high reply rate: those are existing customers."
    )


# ── 3. Touches per contact ────────────────────────────────────────────────────
st.divider()
st.subheader("Touches per contact")
st.caption(METRIC_COPY["touches_per_contact"]["why"])

with st.spinner("Loading touch distribution…"):
    dist_df = (
        load_touches_per_contact(start_date, end_date, REPS, CHANS)
        if ACTIVE else pd.DataFrame()
    )

if dist_df.empty:
    st.info("No outbound touch data in the selected range.")
else:
    bench_label = f"{HIGH_EFFORT_THRESHOLD}+ touches"
    dist_total = int(dist_df["contacts"].sum())
    at_bench = int(
        dist_df.loc[dist_df["bucket"] == bench_label, "contacts"].sum()
    )
    pct_bench = 100 * at_bench / dist_total if dist_total else 0

    d1, d2 = st.columns([1, 2])
    with d1:
        st.metric(
            f"Contacts at {HIGH_EFFORT_THRESHOLD}+ touches",
            f"{pct_bench:.1f}%",
            help=METRIC_COPY["touches_per_contact"]["what"],
        )
        st.caption(
            f"{at_bench:,} of {dist_total:,} contacts. The remaining "
            f"{100 - pct_bench:.1f}% got fewer than "
            f"{HIGH_EFFORT_THRESHOLD} touches — below the threshold where "
            f"response rates jump."
        )
    with d2:
        order = ["1 touch", "2 touches", bench_label]
        plot_df = dist_df.set_index("bucket").reindex(order).reset_index()
        plot_df["contacts"] = plot_df["contacts"].fillna(0)
        fig3 = go.Figure(go.Bar(
            x=plot_df["bucket"], y=plot_df["contacts"],
            marker_color=[_C["gray_light"], _C["gray_mid"], "#0047FF"],
            text=[f"{int(v):,}" for v in plot_df["contacts"]],
            textposition="outside",
            hovertemplate="%{x}: %{y:,d} contacts<extra></extra>",
        ))
        _base_layout(fig3, "Contacts by outbound touch count", "Weekly", height=320)
        fig3.update_layout(xaxis=dict(showgrid=False), showlegend=False)
        st.plotly_chart(fig3, use_container_width=True)


# ── 4. Effort x intent quadrant ───────────────────────────────────────────────
st.divider()
st.subheader("Effort vs intent")
st.caption(METRIC_COPY["quadrant"]["why"])

if quad_df.empty:
    st.info("No touch data in the selected range.")
else:
    st.metric(
        "Contacts in scope",
        f"{total_contacts:,}",
        help=METRIC_COPY["quadrant"]["what"],
    )

    cells_html = []
    for effort, intent, name, expl, color in QUADRANT_CELLS:
        n = int(_cell(effort, intent, "contacts"))
        avg = _cell(effort, intent, "avg_outbound")
        share = 100 * n / total_contacts if total_contacts else 0
        effort_lbl = (
            f"{HIGH_EFFORT_THRESHOLD}+ touches" if effort == "high_effort"
            else f"1–{HIGH_EFFORT_THRESHOLD - 1} touches"
        )
        intent_lbl = "replied or met" if intent == "high_intent" else "neither"
        cells_html.append(
            f'<div class="quad-cell" style="--accent:{color};">'
            f'<p class="quad-name">{name}</p>'
            f'<div class="quad-cohort">{effort_lbl} &middot; {intent_lbl}</div>'
            f'<div class="quad-value">{n:,} <span>contacts &middot; '
            f'{share:.1f}%</span></div>'
            f'<div class="quad-meta">{avg} outbound touches on average</div>'
            f'<p class="quad-expl">{expl}</p>'
            f'</div>'
        )
    st.markdown(
        f'<div class="quad-grid">{"".join(cells_html)}</div>',
        unsafe_allow_html=True,
    )


# ── 5. Speed to lead ──────────────────────────────────────────────────────────
st.divider()
st.subheader("Speed to lead — Outreach-tracked subset only")
st.caption(METRIC_COPY["speed_to_lead"]["why"])
st.caption(
    ":gray[Not affected by the rep filter — the first response to an inbound "
    "demo request can come from anyone, so narrowing to a few reps would "
    "misread slow follow-up where it was simply someone else who replied.]"
)

with st.spinner("Loading speed to lead…"):
    s2l_df = load_speed_to_lead(start_date, end_date)

st.warning(SPEED_TO_LEAD_CAVEAT)

if s2l_df.empty:
    st.info("No demo requests in the selected range.")
else:
    # Split the ROLLUP total row (mo IS NULL) from the monthly rows
    _is_total = s2l_df["mo"].isna()
    overall   = s2l_df[_is_total].iloc[0]
    s2l_df    = s2l_df[~_is_total].sort_values("mo")

    s1, s2, s3 = st.columns(3)
    s1.metric(
        "Demo requests with a visible touch",
        f"{overall['pct_touched']:.0f}%",
        help=METRIC_COPY["speed_to_lead"]["what"],
    )
    # median is NULL when nothing in the window got a visible touch
    _median = overall["median_hrs"]
    s2.metric(
        "Median time to first touch",
        "—" if pd.isna(_median) else f"{_median:.1f} hrs",
    )
    s3.metric("Touched within 24 hours", f"{overall['pct_within_24h']:.0f}%")
    st.caption(
        f"{int(overall['demo_requests']):,} demo requests in the selected "
        f"window. Figures are for the window as a whole, not an average of the "
        f"monthly rows below."
    )

    fig4 = go.Figure()
    fig4.add_trace(go.Bar(
        x=s2l_df["mo"], y=s2l_df["demo_requests"], name="Demo requests",
        marker_color=_C["gray_light"],
        hovertemplate="%{x|%b %Y}: %{y:,d} requests<extra></extra>",
    ))
    fig4.add_trace(go.Scatter(
        x=s2l_df["mo"], y=s2l_df["pct_touched"], name="% with a visible touch",
        mode="lines+markers", yaxis="y2",
        line=dict(color="#0047FF", width=2.5), marker=dict(size=6),
        hovertemplate="%{x|%b %Y}: %{y:.0f}% touched<extra></extra>",
    ))
    fig4.add_trace(go.Scatter(
        x=s2l_df["mo"], y=s2l_df["pct_within_24h"], name="% within 24 hrs",
        mode="lines+markers", yaxis="y2",
        line=dict(color=_C["orange"], width=2, dash="dash"), marker=dict(size=5),
        hovertemplate="%{x|%b %Y}: %{y:.0f}% within 24h<extra></extra>",
    ))
    _base_layout(fig4, "Demo requests and logged follow-up coverage", "Monthly")
    fig4.update_layout(
        yaxis=dict(title="Demo requests", showgrid=True, gridcolor="#F0F0F0",
                   tickformat=",d", rangemode="tozero"),
        yaxis2=dict(title="% of requests", overlaying="y", side="right",
                    showgrid=False, rangemode="tozero", ticksuffix="%"),
    )
    st.plotly_chart(fig4, use_container_width=True)

    show_df = s2l_df.copy()
    show_df["mo"] = pd.to_datetime(show_df["mo"]).dt.strftime("%b %Y")
    st.dataframe(
        show_df.rename(columns={
            "mo": "Month", "demo_requests": "Demo requests",
            "pct_touched": "% touched", "median_hrs": "Median hrs to touch",
            "pct_within_24h": "% within 24 hrs",
        }),
        use_container_width=True, hide_index=True,
    )


# ── 6. Qualified opportunities per rep ────────────────────────────────────────
st.divider()
st.subheader("Qualified opportunities per rep")
st.caption(METRIC_COPY["qualified_opps"]["why"])
st.caption(
    ":gray[Not affected by the rep filter — opportunities are owned by account "
    "executives, a different group from the outbound team above. \"Per rep\" "
    "here means per opportunity owner.]"
)

with st.spinner("Loading qualified opportunities…"):
    opp_df = load_qualified_opps(start_date, end_date)

if opp_df.empty:
    st.info("No qualified opportunities in the selected range.")
else:
    o1, o2 = st.columns(2)
    o1.metric(
        "Qualified opps in window",
        f"{int(opp_df['qualified_opps'].sum()):,}",
        help=METRIC_COPY["qualified_opps"]["what"],
    )
    _months = opp_df["mo"].nunique()
    o2.metric(
        "Avg per month",
        f"{opp_df['qualified_opps'].sum() / _months:.1f}" if _months else "—",
    )
    st.caption(
        "Renewal-record-type opportunities are excluded — see the methodology "
        "notes at the top of the page. Rejected-stage opportunities are also "
        "excluded."
    )

    fig5 = go.Figure()
    for motion in MOTION_ORDER:
        m_df = opp_df[opp_df["motion"] == motion]
        if m_df.empty:
            continue
        fig5.add_trace(go.Bar(
            x=m_df["mo"], y=m_df["per_rep"], name=motion,
            marker_color=MOTION_COLORS[motion],
            customdata=m_df[["qualified_opps", "reps"]],
            hovertemplate=(
                "%{x|%b %Y}: %{y:.1f} per rep<br>"
                "%{customdata[0]:,d} opps across %{customdata[1]:,d} reps"
                f"<extra>{motion}</extra>"
            ),
        ))
    _base_layout(fig5, "Qualified opportunities per rep by motion", "Monthly")
    fig5.update_layout(
        barmode="group",
        yaxis=dict(title="Qualified opps per rep", showgrid=True,
                   gridcolor="#F0F0F0", rangemode="tozero"),
    )
    st.plotly_chart(fig5, use_container_width=True)

    opp_show = opp_df.copy()
    opp_show["mo"] = pd.to_datetime(opp_show["mo"]).dt.strftime("%b %Y")
    st.dataframe(
        opp_show.rename(columns={
            "mo": "Month", "motion": "Motion",
            "qualified_opps": "Qualified opps", "reps": "Reps",
            "per_rep": "Per rep",
        }),
        use_container_width=True, hide_index=True,
    )


# ── 7. Per-rep drill-down ─────────────────────────────────────────────────────
st.divider()
st.subheader("Per-rep drill-down")
st.caption(
    "Who is carrying the outbound load, and whether their contacts are getting "
    "enough touches to clear the threshold."
)

with st.spinner("Loading per-rep detail…"):
    rep_df = (
        load_per_rep(start_date, end_date, REPS, CHANS)
        if ACTIVE else pd.DataFrame()
    )

if rep_df.empty:
    st.info("No rep activity in the selected range.")
else:
    st.dataframe(
        rep_df.rename(columns={
            "rep": "Rep",
            "team": "Team",
            "outbound": "Outbound",
            "contacts_touched": "Contacts touched",
            "replies": "Replies",
            "meetings": "Meetings",
            "meetings_per_1k": "Meetings / 1k outbound",
            "catalyst_notes": "Catalyst notes",
            "contacts_at_benchmark": f"Contacts at {HIGH_EFFORT_THRESHOLD}+",
            "pct_at_benchmark": f"% at {HIGH_EFFORT_THRESHOLD}+",
            "avg_outbound_per_contact": "Avg touches per contact",
        }),
        use_container_width=True, hide_index=True,
    )
    st.caption(
        f"**Meetings / 1k outbound** is the yield column — it is what separates "
        f"working accounts from running volume, and it spanned two orders of "
        f"magnitude across this team in 2026. Read it next to Outbound, not "
        f"instead of it."
    )
    st.caption(
        f"Counted per person, so the {HIGH_EFFORT_THRESHOLD}+ columns mean "
        f"\"{HIGH_EFFORT_THRESHOLD}+ touches from this person\". A contact worked "
        f"by two people appears under both, which is why these don't sum to the "
        f"page-level totals above. Catalyst notes are qualitative and are not "
        f"counted as touches anywhere on this page. Departed reps are included "
        f"so historical weeks stay accurate."
    )


# ── 8. Quality of touch — phase 2 placeholder ─────────────────────────────────
st.divider()
st.markdown(
    f'### Quality of touch <span class="dev-badge">In development</span>',
    unsafe_allow_html=True,
)
st.caption(METRIC_COPY["quality_of_touch"]["why"])

# Deliberately no number, no chart, no fake data. And no heuristic proxy:
# task.description contains the full quoted thread, so email length grows with
# reply depth rather than with writing effort — the apparent correlation
# between length and intent is an artifact.
st.markdown(
    f'<div class="dev-card">'
    f'<p class="dev-status">{QUALITY_STATUS_LINE}</p>'
    f'</div>',
    unsafe_allow_html=True,
)
