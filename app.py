"""
Paid Media Pacer
A Streamlit app that shows month-to-date paid media spend versus budget,
pulling spend from BigQuery and budget from a Google Sheet.
"""

from datetime import date, datetime
from calendar import monthrange

import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from google.cloud import bigquery
from google.oauth2 import service_account


# Configuration
SERVICE_ACCOUNT_FILE = "service-account.json"
GCP_PROJECT = "bi-ntop"
BIGQUERY_TABLE = "bi-ntop.aero_prod_ad_reporting.ad_reporting__account_report"
BUDGET_SHEET_ID = "1D2yoATTH8fY9XrooHUs_pj4meVJFGQqqTCgkj5mqHL4"


# Page setup
st.set_page_config(page_title="Paid Media Pacer", layout="wide")
st.title("Paid Media Pacer")


# Authentication
@st.cache_resource
def get_credentials():
    """Load the service account credentials from the local JSON key file."""
    return service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=[
            "https://www.googleapis.com/auth/cloud-platform",
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    )


@st.cache_resource
def get_bq_client():
    """Return a BigQuery client authenticated with the service account."""
    return bigquery.Client(credentials=get_credentials(), project=GCP_PROJECT)


# Data loaders
@st.cache_data(ttl=3600)
def load_budgets() -> pd.DataFrame:
    """Read monthly budgets from the Google Sheet."""
    import gspread

    gc = gspread.authorize(get_credentials())
    sheet = gc.open_by_key(BUDGET_SHEET_ID).sheet1
    records = sheet.get_all_records()
    df = pd.DataFrame(records)
    df["month"] = pd.to_datetime(df["month"]).dt.date
    df["total_budget"] = pd.to_numeric(df["total_budget"])
    return df


@st.cache_data(ttl=3600)
def load_mtd_spend(month_start: date) -> float:
    """Sum spend in BigQuery from the start of the given month to today."""
    query = f"""
    SELECT ROUND(SUM(spend), 2) AS mtd_spend
    FROM `{BIGQUERY_TABLE}`
    WHERE date_day >= @month_start
      AND date_day <= CURRENT_DATE()
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("month_start", "DATE", month_start),
        ]
    )
    client = get_bq_client()
    result = client.query(query, job_config=job_config).to_dataframe(create_bqstorage_client=False)
    value = result["mtd_spend"].iloc[0]
    return float(value) if value is not None else 0.0
@st.cache_data(ttl=3600)
def load_daily_spend(month_start: date) -> pd.DataFrame:
    """Get daily total spend for the given month, one row per day."""
    query = f"""
    SELECT
        date_day,
        ROUND(SUM(spend), 2) AS daily_spend
    FROM `{BIGQUERY_TABLE}`
    WHERE date_day >= @month_start
      AND date_day <= CURRENT_DATE()
    GROUP BY date_day
    ORDER BY date_day
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("month_start", "DATE", month_start),
        ]
    )
    client = get_bq_client()
    df = client.query(query, job_config=job_config).to_dataframe(create_bqstorage_client=False)
    df["date_day"] = pd.to_datetime(df["date_day"]).dt.date
    return df

@st.cache_data(ttl=3600)
def load_daily_spend_by_channel(month_start: date) -> pd.DataFrame:
    """Get daily spend for the given month, broken out by platform."""
    query = f"""
    SELECT
        date_day,
        platform,
        ROUND(SUM(spend), 2) AS daily_spend
    FROM `{BIGQUERY_TABLE}`
    WHERE date_day >= @month_start
      AND date_day <= CURRENT_DATE()
    GROUP BY date_day, platform
    ORDER BY date_day, platform
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("month_start", "DATE", month_start),
        ]
    )
    client = get_bq_client()
    df = client.query(query, job_config=job_config).to_dataframe(create_bqstorage_client=False)
    df["date_day"] = pd.to_datetime(df["date_day"]).dt.date
    return df

# Main app
today = date.today()
month_start = today.replace(day=1)
days_in_month = monthrange(today.year, today.month)[1]
day_of_month = today.day

st.caption(
    f"Today is {today.strftime('%B %d, %Y')}. "
    f"Day {day_of_month} of {days_in_month}."
)

# Load data
try:
    budgets_df = load_budgets()
except Exception as e:
    st.error(f"Could not load budgets from Google Sheet: {type(e).__name__}: {e}")
    import traceback
    st.code(traceback.format_exc())
    st.stop()

try:
    mtd_spend = load_mtd_spend(month_start)
except Exception as e:
    st.error(f"Could not load spend from BigQuery: {e}")
    st.stop()

# Find this month's budget
this_month_row = budgets_df[budgets_df["month"] == month_start]
if this_month_row.empty:
    st.error(
        f"No budget found for {month_start.strftime('%B %Y')} in the budget sheet. "
        f"Add a row to the sheet and refresh."
    )
    st.stop()

monthly_budget = float(this_month_row["total_budget"].iloc[0])

# Calculate pacing math
expected_spend_today = monthly_budget * (day_of_month / days_in_month)
variance = mtd_spend - expected_spend_today
percent_consumed = mtd_spend / monthly_budget if monthly_budget else 0

# The pacing tile, four metric cards in a row
col1, col2, col3, col4 = st.columns(4)

col1.metric(
    "Monthly Budget",
    f"${monthly_budget:,.0f}",
)

col2.metric(
    "Spend MTD",
    f"${mtd_spend:,.0f}",
    f"{percent_consumed:.0%} of budget",
)

col3.metric(
    "Expected by Today",
    f"${expected_spend_today:,.0f}",
    f"Linear pace through day {day_of_month}",
)

col4.metric(
    "Variance",
    f"${variance:,.0f}",
    "Over pace" if variance > 0 else "Under pace",
    delta_color="inverse",
)

# Action panel
st.divider()
st.subheader("What to do about it")

# We need channel data to recommend a split, so load it up front. The
# real chart section below will reuse the cached result.
try:
    _channel_data_for_panel = load_daily_spend_by_channel(month_start)
except Exception as e:
    st.error(f"Could not load channel data: {type(e).__name__}: {e}")
    st.stop()

_panel_groups = {
    "linkedin_ads": "LinkedIn",
    "google_ads": "Google",
    "microsoft_ads": "Other",
    "reddit_ads": "Other",
}
_channel_data_for_panel["channel_group"] = (
    _channel_data_for_panel["platform"].map(_panel_groups).fillna("Other")
)
mtd_by_channel = (
    _channel_data_for_panel.groupby("channel_group")["daily_spend"].sum().to_dict()
)
li_share = mtd_by_channel.get("LinkedIn", 0) / mtd_spend if mtd_spend else 0
gg_share = mtd_by_channel.get("Google", 0) / mtd_spend if mtd_spend else 0

days_left = days_in_month - day_of_month
needed_daily_total = (monthly_budget - mtd_spend) / days_left if days_left else 0

current_avg_last_7d = (
    _channel_data_for_panel[
        _channel_data_for_panel["date_day"] >= (today - pd.Timedelta(days=7))
    ]["daily_spend"].sum() / 7
)
daily_delta = needed_daily_total - current_avg_last_7d

if abs(variance) < monthly_budget * 0.02:
    st.success(
        f"**On pace.** MTD spend of ${mtd_spend:,.0f} is within 2% of the "
        f"expected ${expected_spend_today:,.0f} for day {day_of_month}. "
        f"Maintain current daily spend of ~${current_avg_last_7d:,.0f}."
    )
elif variance < 0:
    li_target = needed_daily_total * li_share
    gg_target = needed_daily_total * gg_share
    st.warning(
        f"**Under pace by ${abs(variance):,.0f}.** Total daily spend needs to "
        f"increase to **${needed_daily_total:,.0f}/day** for the remaining "
        f"{days_left} days (currently averaging ${current_avg_last_7d:,.0f}/day, "
        f"a +${daily_delta:,.0f} lift). Preserving the current channel mix, that's:\n\n"
        f"- LinkedIn: ~${li_target:,.0f}/day ({li_share:.0%} of total)\n"
        f"- Google: ~${gg_target:,.0f}/day ({gg_share:.0%} of total)\n\n"
        f"The easiest lever is increasing daily caps on top-performing LinkedIn "
        f"campaigns, since LinkedIn is carrying the majority of spend."
    )
else:
    li_target = needed_daily_total * li_share
    gg_target = needed_daily_total * gg_share
    st.error(
        f"**Over pace by ${variance:,.0f}.** Total daily spend needs to "
        f"drop to **${needed_daily_total:,.0f}/day** for the remaining "
        f"{days_left} days (currently averaging ${current_avg_last_7d:,.0f}/day, "
        f"a ${daily_delta:,.0f} cut). Preserving the current channel mix, that's:\n\n"
        f"- LinkedIn: ~${li_target:,.0f}/day ({li_share:.0%} of total)\n"
        f"- Google: ~${gg_target:,.0f}/day ({gg_share:.0%} of total)\n\n"
        f"Pause or reduce daily caps on LinkedIn campaigns first since "
        f"that channel has the most spend volume to cut."
    )
    # Daily spend chart
st.divider()
st.subheader(f"Daily spend, {today.strftime('%B %Y')}")

try:
    daily_df = load_daily_spend(month_start)
except Exception as e:
    st.error(f"Could not load daily spend from BigQuery: {type(e).__name__}: {e}")
    st.stop()

# Build the expected linear pace line: a straight line from $0 on day 1
# to the full monthly budget on the last day of the month.
daily_target = monthly_budget / days_in_month
all_days = pd.date_range(month_start, periods=days_in_month, freq="D").date
pace_df = pd.DataFrame({
    "date_day": all_days,
    "cumulative_pace": [(i + 1) * daily_target for i in range(days_in_month)],
})

# Cumulative actual spend
daily_df_sorted = daily_df.sort_values("date_day").copy()
daily_df_sorted["cumulative_spend"] = daily_df_sorted["daily_spend"].cumsum()

fig = go.Figure()

# Daily spend as bars
fig.add_trace(go.Bar(
    x=daily_df_sorted["date_day"],
    y=daily_df_sorted["daily_spend"],
    name="Daily spend",
    marker_color="#0047FF",
    yaxis="y",
))

# Cumulative actual on a second y-axis (line)
fig.add_trace(go.Scatter(
    x=daily_df_sorted["date_day"],
    y=daily_df_sorted["cumulative_spend"],
    name="Cumulative actual",
    mode="lines+markers",
    line=dict(color="#1a1a1a", width=3),
    yaxis="y2",
))

# Expected pace line (also on second y-axis)
fig.add_trace(go.Scatter(
    x=pace_df["date_day"],
    y=pace_df["cumulative_pace"],
    name="Expected pace",
    mode="lines",
    line=dict(color="#888888", width=2, dash="dash"),
    yaxis="y2",
))

fig.update_layout(
    height=450,
    margin=dict(l=40, r=40, t=20, b=40),
    hovermode="x unified",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    yaxis=dict(title="Daily spend ($)", side="left"),
    yaxis2=dict(title="Cumulative ($)", side="right", overlaying="y", showgrid=False),
    xaxis=dict(title=None),
    bargap=0.2,
)

st.plotly_chart(fig, use_container_width=True)

# Channel breakdown
st.divider()
st.subheader(f"Channel breakdown, {today.strftime('%B %Y')}")

# Reuse the channel data already loaded for the action panel
_channel_data_for_panel["channel_group"] = (
    _channel_data_for_panel["platform"].map(_panel_groups).fillna("Other")
)
grouped = _channel_data_for_panel.groupby(
    ["date_day", "channel_group"], as_index=False
)["daily_spend"].sum()


def render_channel_view(channel_name: str, channel_data: pd.DataFrame, color: str) -> None:
    """Render a small pacing tile and a daily chart for one channel group."""
    if channel_data.empty:
        st.info(f"No spend recorded for {channel_name} this month yet.")
        return

    ch_mtd = channel_data["daily_spend"].sum()
    share_of_total = ch_mtd / mtd_spend if mtd_spend else 0
    last_7_days_cutoff = today - pd.Timedelta(days=7)
    recent = channel_data[channel_data["date_day"] >= last_7_days_cutoff]
    avg_daily_7d = recent["daily_spend"].sum() / 7 if len(recent) else 0

    c1, c2, c3 = st.columns(3)
    c1.metric(f"{channel_name} MTD", f"${ch_mtd:,.0f}")
    c2.metric("Share of total spend", f"{share_of_total:.0%}")
    c3.metric("Avg daily (last 7d)", f"${avg_daily_7d:,.0f}")

    ch_fig = go.Figure()
    ch_fig.add_trace(go.Bar(
        x=channel_data["date_day"],
        y=channel_data["daily_spend"],
        name=channel_name,
        marker_color=color,
    ))
    ch_fig.update_layout(
        height=300,
        margin=dict(l=40, r=40, t=20, b=40),
        yaxis=dict(title="Daily spend ($)"),
        xaxis=dict(title=None),
        bargap=0.2,
        showlegend=False,
    )
    st.plotly_chart(ch_fig, use_container_width=True)


tab_total, tab_linkedin, tab_google, tab_other = st.tabs(
    ["Total", "LinkedIn", "Google", "Other"]
)

with tab_total:
    total_daily = grouped.groupby("date_day", as_index=False)["daily_spend"].sum()
    render_channel_view("Total", total_daily, "#0047FF")

with tab_linkedin:
    li_daily = grouped[grouped["channel_group"] == "LinkedIn"]
    render_channel_view("LinkedIn", li_daily, "#0A66C2")

with tab_google:
    gg_daily = grouped[grouped["channel_group"] == "Google"]
    render_channel_view("Google", gg_daily, "#34A853")

with tab_other:
    other_daily = grouped[grouped["channel_group"] == "Other"]
    render_channel_view("Other (Microsoft + Reddit)", other_daily, "#888888")