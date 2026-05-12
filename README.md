# Paid Media Pacer

A Streamlit dashboard that tracks nTop's monthly paid media spend against budget. Replaces a manually maintained Google Sheet with a live view that pulls spend from BigQuery and budgets from a simple Google Sheet.

## What it does

- **Pacing tile.** Month-to-date actual spend vs. expected linear pace, variance in dollars and percent, percent of budget consumed.
- **Action panel.** Plain-English recommendation: how much daily spend to lift or cut, broken out by channel at the current mix.
- **Daily spend chart.** Daily bars with cumulative actual and expected pace lines overlaid.
- **Channel breakdown.** Tabs for Total, LinkedIn, Google, and Other (Microsoft + Reddit), each with its own pacing metrics and daily chart.
- **Strategic vs High Velocity.** Bucketing of campaign spend by category, with current mix and daily trend.

## How it works

- **Spend** comes from `bi-ntop.aero_prod_ad_reporting.ad_reporting__account_report` (account-level) and `bi-ntop.aero_prod_ad_reporting.ad_reporting__campaign_report` (campaign-level) in BigQuery.
- **Budgets** come from a Google Sheet (Paid Media Pacer - Budgets) with columns: `month`, `total_budget`, `notes`. Edit the sheet to update budgets.
- **Strategic vs HV rules** are defined in `app.py` at the top of the file:
    - LinkedIn campaigns with "tier" in the name → Strategic
    - LinkedIn campaigns with "high-velo" in the name → High Velocity
    - LinkedIn campaigns matching neither → Unmatched (surfaced in the UI for review)
    - Google rules are a named-overrides dictionary plus a default of 100% HV
- Data refreshes once per app load (caches are 1 hour). dbt refreshes the underlying tables nightly.

## Running locally

1. Clone the repo and `cd` into it
2. Create and activate a Python virtual environment: `python3 -m venv venv && source venv/bin/activate`
3. Install dependencies: `pip install -r requirements.txt`
4. Place a service account JSON key file at `service-account.json` in the project root (see Authentication below)
5. Make sure the budget Google Sheet is shared with the service account email as a Viewer
6. Run: `streamlit run app.py`

The app will open in your browser at http://localhost:8501.

## Authentication

The app uses a Google Cloud service account to read from both BigQuery and Google Sheets. The service account email is `paid-media-pacer-local@bi-ntop.iam.gserviceaccount.com`.

Required IAM roles on the `bi-ntop` GCP project:
- `BigQuery Data Viewer`
- `BigQuery Job User`

Required APIs enabled on the `bi-ntop` project:
- BigQuery API
- Google Sheets API
- Google Drive API

The service account JSON key file is NOT committed to this repo (it's in `.gitignore` as `*.json`). To get a copy, ask the project owner or generate a new key in the GCP console.

## Deployment

For production deployment to nTop infrastructure, deploy to Cloud Run with IAP (Identity-Aware Proxy) gating access to nTop Google accounts. Talk to BizOps (Rick Groves) for deployment.

## Owners

- App owner: Michael Fynn (Marketing)
- BizOps / infrastructure: Rick Groves