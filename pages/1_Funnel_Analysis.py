"""
Funnel Analysis page.
Spend, engagement, and lifecycle funnel by channel + Strategic/HV/Expansion opp outcomes.
"""

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from google.cloud import bigquery
from google.oauth2 import service_account


# Configuration
SERVICE_ACCOUNT_FILE = "service-account.json"
GCP_PROJECT = "bi-ntop"
AD_TABLE = "bi-ntop.aero_prod_ad_reporting.ad_reporting__account_report"
FUNNEL_TABLE = "bi-ntop.aero_prod.marketing_lifecycle_funnel"
OPP_TABLE = "bi-ntop.salesforce.opportunity"

# Record type IDs from project doc, verified against real data
RECORD_TYPE_LABELS = {
    "0124R000001UuQlQAK": "HV",
    "0124R000001UuQgQAK": "HV",
    "012Qo00000AyuhVIAR": "Strategic",
    "012Qo00000Avzc5IAB": "Strategic",
    "0124R000001JIhxQAG": "Expansion",
}

# Channels we care about in the funnel view (rest get lumped as "Other")
PAID_CHANNEL_MAP = {
    ("Paid Social", "linkedin_ads"): "LinkedIn",
    ("Warm Outbound", "linkedin_ads"): "LinkedIn",
    ("Paid Search", "google_ads"): "Google",
    ("Paid Search", "microsoft_ads"): "Microsoft",
    ("Paid Social", "facebook_ads"): "Other Paid",
    ("Paid Social", "twitter_ads"): "Other Paid",
    ("Paid Display", None): "Other Paid",
    ("Other Advertising", None): "Other Paid",
}


# Page setup
st.set_page_config(page_title="Funnel Analysis", layout="wide")
st.title("Funnel Analysis")


# Authentication (reuses same service account as the budget pacer)
@st.cache_resource
def get_credentials():
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
    return bigquery.Client(credentials=get_credentials(), project=GCP_PROJECT)


# Data loaders
@st.cache_data(ttl=3600)
def load_spend_by_channel_month(months_back: int = 12) -> pd.DataFrame:
    """Monthly paid media spend by platform for the last N months."""
    query = f"""
    SELECT
        DATE_TRUNC(date_day, MONTH) AS month_start,
        platform,
        ROUND(SUM(spend), 2) AS spend
    FROM `{AD_TABLE}`
    WHERE date_day >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL @months_back MONTH), MONTH)
      AND spend > 0
    GROUP BY month_start, platform
    ORDER BY month_start, platform
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("months_back", "INT64", months_back),
        ]
    )
    client = get_bq_client()
    df = client.query(query, job_config=job_config).to_dataframe(create_bqstorage_client=False)
    df["month_start"] = pd.to_datetime(df["month_start"]).dt.date
    return df


@st.cache_data(ttl=3600)
def load_lifecycle_funnel_by_channel_month(months_back: int = 12) -> pd.DataFrame:
    """
    Monthly lifecycle funnel counts by channel/platform from Deepline.
    Aggregates weekly report_week rows into months.
    Excludes the opp/customer stages — those come from Salesforce direct.
    """
    query = f"""
    SELECT
        DATE_TRUNC(report_week, MONTH) AS month_start,
        channel,
        platform,
        SUM(accounts_aware) AS accounts_aware,
        SUM(accounts_engaged) AS accounts_engaged,
        SUM(accounts_mqa) AS accounts_mqa,
        SUM(accounts_sqa) AS accounts_sqa
    FROM `{FUNNEL_TABLE}`
    WHERE report_week >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL @months_back MONTH), MONTH)
    GROUP BY month_start, channel, platform
    ORDER BY month_start, channel, platform
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("months_back", "INT64", months_back),
        ]
    )
    client = get_bq_client()
    df = client.query(query, job_config=job_config).to_dataframe(create_bqstorage_client=False)
    df["month_start"] = pd.to_datetime(df["month_start"]).dt.date

    # Group raw channel/platform into the buckets we display
    def bucket(row):
        return PAID_CHANNEL_MAP.get(
            (row["channel"], row["platform"]),
            "Non-Paid" if row["channel"] not in ("Paid Social", "Paid Search", "Paid Display", "Other Advertising")
            else "Other Paid"
        )
    df["channel_group"] = df.apply(bucket, axis=1)
    return df


@st.cache_data(ttl=3600)
def load_opp_outcomes_by_month(months_back: int = 12) -> pd.DataFrame:
    """
    Monthly opp counts and ARR from Salesforce, split by Strategic / HV / Expansion.
    Filters: NB+Expansion+Renewal/Expansion only, exclude Rejected.
    """
    record_type_list = "', '".join(RECORD_TYPE_LABELS.keys())
    query = f"""
    SELECT
        DATE_TRUNC(DATE(created_date), MONTH) AS month_start,
        opp.record_type_id,
        opp.type,
        COUNT(*) AS opp_count,
        ROUND(SUM(COALESCE(opf.arr, 0)), 2) AS total_arr,
ROUND(SUM(COALESCE(opf.new_expansion_arr, 0)), 2) AS new_expansion_arr,
COUNTIF(COALESCE(opf.arr, 0) > 0) AS opps_with_arr
    FROM `{OPP_TABLE}` opp
LEFT JOIN `bi-ntop.google_sheets.opportunity_fields` opf
  ON opp.id = opf.opportunity_id
    WHERE opp._fivetran_deleted = FALSE
      AND opp.created_date >= TIMESTAMP(DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL @months_back MONTH), MONTH))
      AND opp.stage_name != 'Rejected'
      AND opp.type IN ('New Business', 'Expansion', 'Renewal/Expansion')
      AND opp.record_type_id IN ('{record_type_list}')
    GROUP BY month_start, opp.record_type_id, opp.type
    ORDER BY month_start, record_type_id, type
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("months_back", "INT64", months_back),
        ]
    )
    client = get_bq_client()
    df = client.query(query, job_config=job_config).to_dataframe(create_bqstorage_client=False)
    df["month_start"] = pd.to_datetime(df["month_start"]).dt.date

    # Bucket into Strategic / HV / Expansion. Expansion type wins over record_type label
    # (e.g. an HV-record-type-id but Expansion type is an Expansion).
    def bucket(row):
        if row["type"] in ("Expansion", "Renewal/Expansion"):
            return "Expansion"
        return RECORD_TYPE_LABELS.get(row["record_type_id"], "Other")
    df["segment"] = df.apply(bucket, axis=1)
    return df


# Helpers
def previous_full_month(today: date) -> date:
    """Return the first day of the previous complete month."""
    first_of_this_month = today.replace(day=1)
    if first_of_this_month.month == 1:
        return date(first_of_this_month.year - 1, 12, 1)
    return date(first_of_this_month.year, first_of_this_month.month - 1, 1)


def fmt_money(value) -> str:
    if value is None or pd.isna(value):
        return "$0"
    return f"${value:,.0f}"


def fmt_count(value) -> str:
    if value is None or pd.isna(value):
        return "0"
    return f"{int(value):,}"


# Load all data once
try:
    spend_df = load_spend_by_channel_month(months_back=12)
    funnel_df = load_lifecycle_funnel_by_channel_month(months_back=12)
    opp_df = load_opp_outcomes_by_month(months_back=12)
except Exception as e:
    st.error(f"Failed to load data: {type(e).__name__}: {e}")
    st.stop()


# Month picker
today = date.today()
default_month = previous_full_month(today)
available_months = sorted(
    set(spend_df["month_start"]) | set(funnel_df["month_start"]) | set(opp_df["month_start"]),
    reverse=True,
)
if default_month not in available_months:
    default_month = available_months[0]

selected_month = st.selectbox(
    "Month",
    options=available_months,
    index=available_months.index(default_month),
    format_func=lambda d: d.strftime("%B %Y"),
)

st.caption(
    "Funnel data: aware, engaged, MQA, SQA from Deepline's marketing_lifecycle_funnel model. "
    "Opp data: New Business + Expansion opps created in the selected month from Salesforce, "
    "bucketed Strategic / HV / Expansion via record_type_id. "
    "ARR is sourced from google_sheets.opportunity_fields and is only populated on ~19% of opps "
    "(typically those past early qualification stages)."
)


def render_snapshot(month: date, label_prefix: str = "") -> None:
    """Render the funnel + outcomes for one month. label_prefix is shown in headers when comparing."""
    spend_month = spend_df[spend_df["month_start"] == month]
    funnel_month = funnel_df[funnel_df["month_start"] == month]
    opp_month = opp_df[opp_df["month_start"] == month]

    # Funnel section
    header = f"Funnel, {month.strftime('%B %Y')}"
    if label_prefix:
        header = f"{label_prefix} — {header}"
    st.subheader(header)

    paid_funnel = funnel_month[
        funnel_month["channel_group"].isin(["LinkedIn", "Google", "Microsoft", "Other Paid"])
    ]
    total_spend = spend_month["spend"].sum()
    total_aware = paid_funnel["accounts_aware"].sum()
    total_engaged = paid_funnel["accounts_engaged"].sum()
    total_mqa = paid_funnel["accounts_mqa"].sum()
    total_sqa = paid_funnel["accounts_sqa"].sum()

    fc1, fc2, fc3, fc4, fc5 = st.columns(5)
    fc1.metric("Paid Spend", fmt_money(total_spend))
    fc2.metric("Aware accounts", fmt_count(total_aware))
    fc3.metric("Engaged", fmt_count(total_engaged))
    fc4.metric("MQA", fmt_count(total_mqa))
    fc5.metric("SQA", fmt_count(total_sqa))

    st.markdown("**By paid channel**")
    channel_view = paid_funnel.groupby("channel_group", as_index=False).agg({
        "accounts_aware": "sum",
        "accounts_engaged": "sum",
        "accounts_mqa": "sum",
        "accounts_sqa": "sum",
    })
    spend_by_channel = spend_month.groupby("platform", as_index=False).agg({"spend": "sum"})
    platform_to_channel = {
        "linkedin_ads": "LinkedIn",
        "google_ads": "Google",
        "microsoft_ads": "Microsoft",
        "reddit_ads": "Other Paid",
        "facebook_ads": "Other Paid",
    }
    spend_by_channel["channel_group"] = (
        spend_by_channel["platform"].map(platform_to_channel).fillna("Other Paid")
    )
    spend_by_channel = spend_by_channel.groupby("channel_group", as_index=False).agg({"spend": "sum"})

    channel_view = channel_view.merge(spend_by_channel, on="channel_group", how="left")
    channel_view["spend"] = channel_view["spend"].fillna(0)
    channel_view = channel_view[[
        "channel_group", "spend", "accounts_aware", "accounts_engaged", "accounts_mqa", "accounts_sqa"
    ]]
    channel_view = channel_view.rename(columns={
        "channel_group": "Channel",
        "spend": "Spend ($)",
        "accounts_aware": "Aware",
        "accounts_engaged": "Engaged",
        "accounts_mqa": "MQA",
        "accounts_sqa": "SQA",
    })

    st.dataframe(
        channel_view,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Spend ($)": st.column_config.NumberColumn(format="$%.0f"),
            "Aware": st.column_config.NumberColumn(format="%d"),
            "Engaged": st.column_config.NumberColumn(format="%d"),
            "MQA": st.column_config.NumberColumn(format="%d"),
            "SQA": st.column_config.NumberColumn(format="%d"),
        },
    )

    # Outcomes section
    outcomes_header = f"Outcomes, {month.strftime('%B %Y')}"
    if label_prefix:
        outcomes_header = f"{label_prefix} — {outcomes_header}"
    st.markdown(f"**{outcomes_header}**")

    outcomes = opp_month.groupby("segment", as_index=False).agg({
        "opp_count": "sum",
        "total_arr": "sum",
        "new_expansion_arr": "sum",
        "opps_with_arr": "sum",
    })
    for segment in ["Strategic", "HV", "Expansion"]:
        if segment not in outcomes["segment"].values:
            outcomes = pd.concat([
                outcomes,
                pd.DataFrame([{
                    "segment": segment, "opp_count": 0, "total_arr": 0,
                    "new_expansion_arr": 0, "opps_with_arr": 0,
                }]),
            ], ignore_index=True)
    segment_order = {"Strategic": 0, "HV": 1, "Expansion": 2}
    outcomes = outcomes[outcomes["segment"].isin(segment_order.keys())]
    outcomes = outcomes.sort_values("segment", key=lambda s: s.map(segment_order))

    oc1, oc2, oc3 = st.columns(3)
    for col, (_, row) in zip([oc1, oc2, oc3], outcomes.iterrows()):
        seg = row["segment"]
        col.metric(
            f"{seg} opps",
            fmt_count(row["opp_count"]),
            f"{fmt_money(row['total_arr'])} ARR ({int(row['opps_with_arr'])} of {int(row['opp_count'])} with ARR)",
            delta_color="off",
        )


# View selector
view_mode = st.radio(
    "View",
    options=["Single month", "Month-over-month", "Trend over time"],
    horizontal=True,
    label_visibility="collapsed",
)

st.divider()

if view_mode == "Single month":
    render_snapshot(selected_month)
elif view_mode == "Month-over-month":
    available_for_comparison = [m for m in available_months if m != selected_month]
    if not available_for_comparison:
        st.warning("Need at least two months of data to compare.")
    else:
        comparison_default = available_for_comparison[0]
        comparison_month = st.selectbox(
            "Compare to",
            options=available_for_comparison,
            index=0,
            format_func=lambda d: d.strftime("%B %Y"),
            key="comparison_month",
        )
        st.divider()
        col_left, col_right = st.columns(2)
        with col_left:
            render_snapshot(selected_month, label_prefix="A")
        with col_right:
            render_snapshot(comparison_month, label_prefix="B")

else:  # Trend over time
    # Use the last 12 months ascending so the chart reads left to right
    trend_months = sorted(set(funnel_df["month_start"]))[-12:]

    # Build a wide table: rows = month, cols = stages, values = totals across paid channels
    paid_only = funnel_df[
        funnel_df["channel_group"].isin(["LinkedIn", "Google", "Microsoft", "Other Paid"])
    ]
    funnel_trend = paid_only.groupby("month_start", as_index=False).agg({
        "accounts_aware": "sum",
        "accounts_engaged": "sum",
        "accounts_mqa": "sum",
        "accounts_sqa": "sum",
    })
    spend_trend = spend_df.groupby("month_start", as_index=False).agg({"spend": "sum"})

    funnel_trend = funnel_trend[funnel_trend["month_start"].isin(trend_months)]
    spend_trend = spend_trend[spend_trend["month_start"].isin(trend_months)]

    # The most recent 3 months are "still maturing" for cohort purposes
    maturity_cutoff = trend_months[-3] if len(trend_months) >= 3 else trend_months[0]

    st.subheader("Funnel trend, last 12 months")
    st.caption(
        "Most recent 3 months shown faded — opps and SQA from these months are still maturing. "
        "Treat as directional only."
    )

    trend_fig = go.Figure()

    def add_line(df, x_col, y_col, name, color):
        # Split into mature and maturing halves so we can color them differently
        mature = df[df[x_col] < maturity_cutoff]
        maturing = df[df[x_col] >= maturity_cutoff]
        if not mature.empty:
            trend_fig.add_trace(go.Scatter(
                x=mature[x_col], y=mature[y_col], name=name,
                mode="lines+markers", line=dict(color=color, width=3),
                legendgroup=name, showlegend=True,
            ))
        if not maturing.empty:
            trend_fig.add_trace(go.Scatter(
                x=maturing[x_col], y=maturing[y_col], name=name,
                mode="lines+markers",
                line=dict(color=color, width=3, dash="dot"),
                marker=dict(symbol="circle-open"),
                opacity=0.5,
                legendgroup=name, showlegend=False,
            ))

    add_line(funnel_trend, "month_start", "accounts_aware", "Aware", "#0047FF")
    add_line(funnel_trend, "month_start", "accounts_engaged", "Engaged", "#34A853")
    add_line(funnel_trend, "month_start", "accounts_mqa", "MQA", "#F5A623")
    add_line(funnel_trend, "month_start", "accounts_sqa", "SQA", "#1a1a1a")

    trend_fig.update_layout(
        height=400,
        margin=dict(l=40, r=40, t=20, b=40),
        yaxis=dict(title="Accounts"),
        xaxis=dict(title=None),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )
    st.plotly_chart(trend_fig, use_container_width=True)

    # Spend trend below
    st.markdown("**Paid spend by month**")
    spend_fig = go.Figure()
    spend_fig.add_trace(go.Bar(
        x=spend_trend["month_start"],
        y=spend_trend["spend"],
        marker_color=[
            "#0047FF" if m < maturity_cutoff else "rgba(0,71,255,0.4)"
            for m in spend_trend["month_start"]
        ],
    ))
    spend_fig.update_layout(
        height=300,
        margin=dict(l=40, r=40, t=20, b=40),
        yaxis=dict(title="Spend ($)"),
        xaxis=dict(title=None),
        bargap=0.2,
        showlegend=False,
    )
    st.plotly_chart(spend_fig, use_container_width=True)

    # Outcomes trend: opp counts by segment, stacked
    st.markdown("**Opp outcomes by month**")
    st.caption(
        "Stacked bar by segment. Recent months faded because opp pipeline from these months "
        "is still being created and may grow."
    )
    outcomes_trend = opp_df[opp_df["month_start"].isin(trend_months)].copy()
    outcomes_pivot = outcomes_trend.pivot_table(
        index="month_start", columns="segment", values="opp_count", aggfunc="sum"
    ).fillna(0).reset_index()
    for segment in ["Strategic", "HV", "Expansion"]:
        if segment not in outcomes_pivot.columns:
            outcomes_pivot[segment] = 0

    outcomes_fig = go.Figure()
    seg_colors = {"Strategic": "#0047FF", "HV": "#F5A623", "Expansion": "#34A853"}
    for segment in ["Strategic", "HV", "Expansion"]:
        mature_mask = outcomes_pivot["month_start"] < maturity_cutoff
        outcomes_fig.add_trace(go.Bar(
            x=outcomes_pivot["month_start"],
            y=outcomes_pivot[segment],
            name=segment,
            marker_color=[
                seg_colors[segment] if mature else f"rgba(0,0,0,0)"
                for mature in mature_mask
            ],
            # Use a fainter version for maturing months by adjusting opacity per bar
            marker=dict(
                color=[
                    seg_colors[segment] if mature else seg_colors[segment]
                    for mature in mature_mask
                ],
                opacity=[1.0 if mature else 0.4 for mature in mature_mask],
            ),
        ))
    outcomes_fig.update_layout(
        barmode="stack",
        height=350,
        margin=dict(l=40, r=40, t=20, b=40),
        yaxis=dict(title="Opps created"),
        xaxis=dict(title=None),
        bargap=0.2,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(outcomes_fig, use_container_width=True)