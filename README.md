# Monthly medical-device reporting analysis

An exploratory analysis of FDA medical device reports for product code LOX (PTCA balloon catheters), focusing on reporting trends, event timing, and an unusual increase in May 2025.

The dataset contains 1,922 unique report keys received by the FDA between January and December 2025, across manufacturers. Data was retrieved using the openFDA API and processed in Python. The analysis uses Excel for monthly reporting and visualization, with additional Python scripts for data validation and narrative screening.

## Main findings

### 1. An unusual increase in May

May recorded 343 reports, compared with 128 in April, an increase of 215 (168.0%). This was the largest positive month-to-month change in the dataset.

The increase was concentrated in two areas:

- **Event timing:** Reports with recorded event dates more than 90 days before FDA receipt increased from 5 to 232 (+227). Reports within 30 days decreased from 95 to 82.
- **Manufacturer:** Reports listing Boston Scientific increased from 73 to 284 (+211), accounting for 211 of the net 215-report increase.

The median recorded-date lag increased from 21 days in April to 506 days in May. This prompted further investigation into the underlying reports and how their event dates were recorded.

### 2. Narrative screening revealed an important data-quality issue

I used Python to screen the narratives of all 343 May reports for references to registry reporting, event-date precision, January 1 date substitution, and possible duplicate reporting.

| Narrative flag | Reports | Share of May |
|---|---:|---:|
| Registry / CathPCI reference | 229 | 66.8% |
| Explicit year-only event date | 229 | 66.8% |
| January 1 substitution language | 220 | 64.1% |
| Possible duplicate language | 212 | 61.8% |

Counts represent distinct report keys, and flags may overlap. Twelve selected reports were also manually checked to assess the screening rules and review exceptions.

The registry-related narratives describe a reporting process in which exact event dates were unavailable, with January 1 sometimes substituted for year-only dates. This helps explain why so many May records appear to concern substantially older events.

However, 53 registry-flagged reports contained structured event dates other than January 1 despite narratives describing year-only precision. This raises questions about the provenance and interpretation of the date fields.

All 229 registry-flagged reports remained in the over-90-day category when tested using the latest possible date in their stated 2024 event year. Their exact event-to-receipt intervals, however, cannot be established from year-only dates.

### 3. Interpretation

The May increase was strongly associated with registry-derived reporting and older recorded event dates. What initially appeared to be an unusually large reporting lag was partly influenced by how dates were represented in the underlying data.

This does not establish that the increase was caused entirely by registry submissions, that the reports represent unique clinical incidents, or that device performance changed.

A useful next investigation would examine registry submission practices, differences between structured and narrative event dates, and possible links to previously submitted reports.

## Methods and validation

The analysis follows four steps:

1. **Extraction:** Retrieve LOX medical device reports using the openFDA API.
2. **Preparation:** Clean records, calculate event-to-receipt lags, assign timing groups, and audit report-key uniqueness and missing dates.
3. **Monthly analysis:** Compare reporting volume and timing composition across 2025, identifying the largest month-to-month increase.
4. **Narrative review:** Screen all May narratives using predefined phrase patterns, preserve supporting text, and manually review selected flagged and unflagged cases.

The 1,922 report keys were checked for uniqueness. Monthly counts, timing groups, and calculations were reconciled against the cleaned dataset and audit results. The narrative screening was independently checked against its saved output.

The narrative analysis uses locally downloaded API records. The saved extracts were not independently revalidated through a new live API retrieval.

## Project files

**Python scripts**

- `fetch_data.py` — Retrieves device event records from openFDA.
- `prepare_data.py` — Cleans and validates the downloaded reports.
- `analyze_monthly.py` — Produces monthly summaries and the selected-month comparison.
- `screen_may_narratives.py` — Screens May narratives and saves report-level flags, supporting evidence, and a manual-review selection.

**Analysis outputs**

- `outputs/monthly_analysis/` — Monthly counts, timing summaries, selected-month records, and validation results.
- `outputs/may_screening/` — Narrative-screening results, source evidence, and manual-review materials.
- Excel workbook — Monthly reporting dashboard, data-quality checks, and May investigation findings.

## Run locally

The scripts use Python and the locally downloaded openFDA data.

From the project directory:

```bash
python fetch_data.py
python prepare_data.py
python analyze_monthly.py
python screen_may_narratives.py
```

The monthly analysis defaults to `data/processed/reports_clean.csv` and `data/processed/audit_summary.json`, with results written to `outputs/monthly_analysis/`. The narrative-screening script uses the downloaded raw API records and writes to `outputs/may_screening/`.

## Limitations

- FDA reports cannot independently establish device failure rates, causality, or the frequency of clinical incidents.
- Unique `mdr_report_key` values do not necessarily correspond to unique incidents.
- The analysis covers reports received in 2025, not all events occurring in 2025.
- Recorded-date lag may differ from actual event-to-receipt lag when event dates are incomplete or substituted.
- Narrative screening identifies text patterns, not independently verified reporting circumstances. Missing a phrase does not prove that the circumstance is absent.
- Possible-duplicate flags do not identify or confirm duplicate pairs.
- Manual review was targeted and illustrative, not a representative validation sample.

The investigation is intended to identify reporting patterns and questions for further review, not to make conclusions about the comparative safety of devices or manufacturers.

## Data source

FDA openFDA — Medical Device Event API

https://open.fda.gov/apis/device/event/