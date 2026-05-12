"""
Paid Media Pacer
A Streamlit app that shows month-to-date paid media spend versus budget,
pulling spend from BigQuery and budget from a Google Sheet.
"""

from datetime import date, datetime
from calendar import monthrange

import pandas as pd
import streamlit as st
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

# Plain-English summary
st.divider()

if abs(variance) < monthly_budget * 0.02:
    st.info(
        f"You are on pace. MTD spend of ${mtd_spend:,.0f} is within 2% of the "
        f"expected ${expected_spend_today:,.0f} for day {day_of_month}."
    )
elif variance < 0:
    days_left = days_in_month - day_of_month
    needed_daily = (monthly_budget - mtd_spend) / days_left if days_left else 0
    st.warning(
        f"You are ${abs(variance):,.0f} under pace. To land on budget by month end, "
        f"daily spend needs to average ${needed_daily:,.0f} for the remaining "
        f"{days_left} days."
    )
else:
    days_left = days_in_month - day_of_month
    needed_daily = (monthly_budget - mtd_spend) / days_left if days_left else 0
    st.error(
        f"You are ${variance:,.0f} over pace. To land on budget by month end, "
        f"daily spend needs to drop to ${needed_daily:,.0f} for the remaining "
        f"{days_left} days."
    )