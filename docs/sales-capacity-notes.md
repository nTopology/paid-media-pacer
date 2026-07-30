# Sales Capacity & Touch Quality — Notes

**Owner:** Michael Fynn, nTop Marketing
**Last updated:** July 2026
**App:** `pages/6_Sales_Capacity.py` in the `paid-media-pacer` repo

---

## Purpose

Answer one business question: **have we hit sales capacity, or is there room to push more volume?** The answer drives a hiring-vs-paid-budget decision.

Metric definitions come from the Jai Toor (Deepline) capacity conversations. The touch source inventory comes from Rick Groves' `contact_touch_tracking` doc, verified against live schema 2026-07-29.

Of the five metrics Jai named, four are built here. Quality of touch is phase 2 — see [Not built](#not-built-quality-of-touch).

---

## Data sources

Unlike every other page in this app, this one reads from `bi-ntop.salesforce.*` and `bi-ntop.hubspot.*` rather than `bi-ntop.aero_prod_ad_reporting.*`. The same service account (`paid-media-pacer-local@bi-ntop.iam.gserviceaccount.com`) reaches both — its `BigQuery Data Viewer` / `BigQuery Job User` grants are at the project level. Confirmed reading all seven required tables on 2026-07-30.

| Table | Used for |
|---|---|
| `salesforce.task` | Outbound touches, email replies, demo-task completion. ~3.3M rows. |
| `salesforce.event` | Meetings; `is_child` splits recurring instances from real meetings. |
| `salesforce.user` | Rep display names, ops/system account exclusion. |
| `salesforce.lead` | Pre-conversion activity lookup for speed to lead. |
| `salesforce.opportunity` | Qualified opportunities by motion. |
| `hubspot.contact_form_submission` + `hubspot.form` | Demo request timestamps (the speed-to-lead anchor). |
| `hubspot.contact` | Maps HubSpot contacts to Salesforce contact IDs. |

Each touch query scans roughly 0.5 GB. All loaders carry the app-standard `@st.cache_data(ttl=3600)`, so widget interaction does not re-scan.

---

## What counts as a touch

Matched on `task.subject`, plus `salesforce.event`:

| Bucket | Match rule | Counts as |
|---|---|---|
| `email_out` | `subject LIKE '[Outreach] [Email] [Out]%'` | Outbound touch |
| `call` | `LOWER(subject) LIKE '%[outreach] [call]%'` | Outbound touch |
| `other_channel` | `subject LIKE '[Outreach] [Other]%'` (LinkedIn etc.) | Outbound touch |
| `email_in` | `subject LIKE '[Outreach] [Email] [In]%'` | Intent signal |
| `catalyst` | `subject LIKE 'Catalyst Note%'` | Qualitative — never counted as a touch |
| `meeting` | `event` where `is_child = FALSE` | Intent signal |
| `meeting_recurring` | `event` where `is_child = TRUE` | Tracked separately, excluded from intent |

Case-insensitive matching on the call bucket is required — direction markers appear as both `[outbound]` and `[Outbound]` in the wild.

**Thresholds.** High effort = 3+ outbound touches to a contact (Jai's benchmark). High intent = at least one `email_in` reply or one non-recurring meeting.

**Rep roster.** Excludes ops and system owners by name: `Revenue Operations`, `Hubspot Integration`, `Service Account Marketo`, `CS Team`, plus anything matching `%Integration%` or `%Service Account%`. Deliberately **not** filtered on `is_active` — departed reps still own historical touches, and excluding them would make past weeks look artificially quiet.

---

## Decisions made during the build

These are surfaced in the UI as well, in the "How these numbers are built" card at the top of the page. That card is the user-facing version of this section; keep the two in step.

1. **Renewals are excluded from qualified opportunities.** Renewal record type (`0124R000001UuQqQAK`) is the single largest bucket — 68 qualified in 2026 YTD against 50 Strategic and 36 HV. They're customer-success driven, not the output of outbound effort, so including them (as `Other`, which is where the original spec's `CASE` swept them) would flatter per-rep output on a page about new-pipeline capacity. With them excluded, the `Other` bucket is empty and only Strategic / HV / Expansion appear.

2. **Speed to lead is monthly, not weekly.** The handoff spec's SQL grouped by week while its acceptance criteria were stated monthly. Monthly won: ~46 demo requests/week is thin enough that the median swings hard on low weeks, and this metric is already caveated as coverage-limited.

3. **Partial weeks are dropped from the weekly trend.** A week the date filter only partly covers reads as a dip that isn't real. The current in-progress week is kept but labelled, matching how the Paid Media Pacer and Paid to Pipeline pages treat in-progress periods. Headline metrics use the latest *complete* week.

4. **The speed-to-lead headline uses `GROUP BY ROLLUP`.** This gives a true median across the whole window rather than an average of monthly medians.

5. **Per-rep benchmark columns are per rep, not per contact.** A contact worked by two reps counts under both, so the per-rep table doesn't sum to page-level totals. Called out in the table caption.

---

## Traps

Every one of these was hit during the original analysis. Each is handled in code with a comment naming the trap.

1. **`Sent %` / `Opened %` / `Clicked %` tasks are not rep touches.** Bulk-newsletter tracking logs, attributed to whoever owns the record — two reps carry ~16K each. Counting them inflates touch volume roughly 5x and makes capacity look fine when it isn't. They fall through the subject `CASE` to `NULL` and are dropped.
2. **`salesforce.event` holds future-dated recurring instances out to 2028.** Every event read is bounded by `CURRENT_DATE()` as well as the user's end date.
3. **The `nTop Demo Request` task auto-creates seconds after the form fill.** Using its `created_date` produces a fake ~6-minute median speed to lead. Only `completed_date_time` with `status = 'Completed'` counts.
4. **`Submitted Form%` tasks are duplicate mirrors of HubSpot form submissions** (~14.8K tasks against ~15.6K submissions, created 10–30s later). Dropped entirely; the anchor is `hubspot.contact_form_submission.timestamp`.
5. **Do not use email body length as a quality proxy.** `task.description` contains the full quoted thread, so length grows with reply depth, not writing effort. The apparent length/intent correlation is an artifact.
6. **`hubspot.form`'s primary key is `guid`, not `id`.** Join `contact_form_submission.form_id = form.guid`.
7. **`_fivetran_deleted = FALSE` on every `salesforce.*` and HubSpot core-entity table.** HubSpot bridge/event tables (`contact_form_submission`, `email_event`) do not have this column.

---

## Acceptance numbers

Verified 2026-07-30 with `From = 2026-01-01`.

**Quadrant** — 2,429 contacts with ≥1 outbound touch (spec said 2,428):

| Effort | Intent | Contacts | Spec | Share | Avg outbound |
|---|---|---|---|---|---|
| 3+ touches | Replied or met | 820 | 820 | 33.8% | 11.6 |
| 3+ touches | Neither | 632 | 632 | 26.0% | 4.8 |
| 1–2 touches | Replied or met | 310 | 313 | 12.8% | 1.5 |
| 1–2 touches | Neither | 667 | 663 | 27.5% | 1.3 |

**Headline dose-response:** 3+ touches → **56.5%** reply-or-meeting rate; 1–2 touches → **31.7%**. A +24.7 point gap. This is the empirical validation of Jai's ≥3 benchmark and the most useful single output of the page.

**Touches per contact:** 606 at one touch, 371 at two, 1,452 at 3+ — 59.8% clear the benchmark.

**Weekly fact table:** the spec's sanity range (17–24 active reps, 245–395 outbound, 12–22 outbound per rep, 42–70 meetings) describes the **trailing ~9 weeks**, and reproduces exactly on that window. It does not describe 2026 as a whole: February–March ran far hotter (up to 822 outbound, 37.9 per rep) before settling. Worth knowing before reading the trend as flat.

**Speed to lead:** 19% of demo requests get a visible rep touch, 25.4h median, 10% within 24h — all inside the spec's ranges.

Small drift against the spec's figures is expected and not a bug: the queries are bounded by `CURRENT_DATE()`, and meetings get rescheduled or deleted, which moves a handful of contacts between cells day to day.

---

## Mandatory UI caveat

The speed-to-lead panel renders this on the panel itself, not just here:

> Reflects only follow-up logged through Outreach and Salesforce. Roughly half of inbound follow-up runs through Lemlist and Heyreach, which do not sync back to Salesforce, and reps complete only ~15% of assigned demo tasks. Treat as a floor on responsiveness and a measure of logging coverage — not a team-level SLA.

This matters because the number will be read by the CRO. Presented without the caveat it looks like a performance indictment when it is substantially a tracking gap.

---

## Not built: quality of touch

Deliberately out of scope. The raw material exists — `task.description` holds full email bodies on 128,079 Outreach tasks, 100% populated — but scoring it requires an LLM batch job against Jai's 1–10 rubric, which doesn't exist yet. That's a separate project with its own cost model.

The page renders a placeholder panel in the position the metric will eventually occupy: bottom of the page, same card styling as the real panels, `In development` badge, explainer and status line. No number, no chart, no fake data, and **no heuristic proxy** (see trap 5).

What's needed to unblock it: the scoring rubric from sales leadership defining what a 9 or 10 looks like.

---

## Implementation notes

- All panel copy lives in a single `METRIC_COPY` dict at the top of the page module, mirroring how the other pages keep their rules at the top. Each entry has a `what` and a `why`: `why` renders as an always-visible `st.caption` under the panel title, `what` renders in the `help=` tooltip on the panel's headline metric. Neither goes in an expander — collapsed copy doesn't get read.
- Methodology bullets live in `METHODOLOGY_NOTES`, rendered as a visible card near the top of the page. Add to it whenever a definition changes.
- Date range reuses the sidebar `From`/`To` `st.date_input` pattern from `2_Paid_to_Pipeline.py`. Queries are parameterised on `@start_date` / `@end_date`; `To` is capped at today.
- `requirements.txt` is unchanged — the page uses only streamlit, pandas, plotly and google-cloud-bigquery, all already pinned.

## Refresh cadence

Fivetran syncs Salesforce and HubSpot continuously. App caches are 1 hour.
