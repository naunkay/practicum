# Group 7 - Engagement Insights Agent: Raw Data v3.4 (team package)

> **ALL DATA IN THIS PACKAGE ARE SYNTHETIC - FOR DEMO PURPOSES ONLY.**
> No record describes a real employer, employee, customer, or Gallup client.

This is the **raw layer**: what the survey platform, the HR system, the operations system and the manager app export, **before any cleaning**.
It is the input for the team's cleaning step (SQL / Python). Derived values (team means, score changes, hierarchy scores) and the agent's data are **not** part of the raw layer; they belong to the cleaned layer.

## Layers
| layer | what | status |
|---|---|---|
| **raw** (this package) | source-system exports, loaded as-is; known issues included | done |
| **cleaned** | after SQL / Python: ISO dates, canonical names, duplicate columns removed, analysis tables (team means, hierarchy scores, ...) | being designed by the team |
| **agent extract** | what the agent may read: team level only, n >= 5 | built from the cleaned layer |

## At a glance
| | |
|---|---|
| Organization | one generic organization (`organizations`: ORG001), no industry. The schema already separates organizations |
| Departments / teams | 12 / 38 (types: operations, support, administrative) |
| Employees | 2,580 roster rows: 1,538 active, 27 on leave, 1,015 departed (roster keeps everyone employed on/after 2023-09-28; extract date 2026-10-09) |
| Current age mix | 18-29 21%, 30-39 27%, 40-49 22%, 50-59 16%, 60+ 14% |
| Surveys | annual Q12 census only: 2024-Annual, 2025-Annual, 2026-Annual (fielded in September, same dates for every team; invitees need >= 30 days tenure) |
| Survey responses | 3,691 rows: 3,435 complete, 256 partial; response rate 79.5% |
| Teams below n = 5 | 8 team-years with fewer than 5 respondents (n = team respondents): Payroll 2024, HR Operations 2024, Market Insights 2025, HR Operations 2025, User Research & Design 2026, Market Insights 2026, Payroll 2026, Learning & Development 2026. Their results must never be shown |
| Actions / follow-ups | 100 / 138 |
| Business outcomes | 114 (team x survey year; each row covers `measured_from` to `measured_to`, the 12 months ending on that survey's last fielding day) |
| Decision history / action library | 125 / 50 |

## Packages (split by source system)
| folder | source | tables |
|---|---|---|
| `data/survey/` | survey platform export (Q12 census) | `survey_periods` (organization-wide window and release date), `survey_responses`, `survey_participation` (per team: fielding window, invited, responded), `q12_items` (codebook incl. Gallup hierarchy level) |
| `data/org_hr/` | company HRIS | `organizations`, `departments`, `teams`, `employees`, `manager_assignments` (manager history) |
| `data/business/` | operations / HR metrics | `business_outcomes` |
| `data/decisions/` | manager app (actions and decisions after the survey) | `management_actions`, `action_followups`, `decision_history`, `action_library` |
| `data/engagement_raw.sqlite` | the same raw tables in one SQLite database, created with `schema/raw_schema.sql` (keys and clean-value checks; no views) | |

`team_id` links every package; `survey_period` also links survey and business data. `survey_responses.response_id` is the only survey key and links to nothing else (anonymous by design).

## Known raw-data issues (to fix in the cleaning step)
Only these two kinds of issue are present; every other value is already valid.

**1. Mixed date formats.** Most values are `YYYY-MM-DD`; some rows use `YYYY/MM/DD` or `MM/DD/YYYY` (no day-first dates, so every value has exactly one reading).
| column | rows with a non-ISO format |
|---|---|
| `employees.termination_date` | 152 |
| `management_actions.due_date` | 15 |
| `management_actions.created_at` (date + time) | 15 |
| `action_followups.followup_date` | 21 |
| `decision_history.decision_date` | 19 |

**2. Letter case differs.** The same name appears as e.g. `Customer Support`, `customer support`, `CUSTOMER SUPPORT`. The canonical spelling is the one in `departments.department_name` (for department) and the five owner roles listed in the data dictionary.
| column | rows not in canonical case |
|---|---|
| `employees.department` | 206 |
| `survey_responses.department` | 295 |
| `management_actions.owner` | 8 |

Because of these issues, `schema/raw_schema.sql` does not check date formats, date order or name matching; those rules belong to the cleaned layer.

## Rules that apply to every later layer
- **Privacy.** The agent never reads raw data. Results are shown only for team-years with n >= 5 (n = team respondents; whether Gallup counts n per item is an open question). Employee data are background: usable only aggregated to team level, never for individual-level analysis or output. 15%-18% of responses per year are the only response in their team x age band x tenure band group, so row-level survey data must never leave the team.
- **Team level only (for now).** No department-level results: Gallup works mainly at team level, and a department total minus its visible teams would reveal a hidden team's score (that would happen in 8 department-years in this data).
- **Time.** `teams` and `employees` describe the organization on the extract date; history is in `manager_assignments`, `survey_responses.department` (department at survey time) and `decision_history`. `business_outcomes` periods end on the survey's last fielding day, so pairing a survey with the same `survey_period` is concurrent; for "engagement then results" use survey year t with outcomes of year t+1.
- **Several organizations.** Every id is unique across the database; `survey_periods` and `_dataset_metadata` are per organization; department names are unique within an organization.
- **Gallup content.** Q12 item text in `q12_items` is Gallup proprietary: course use only, not for public sites or repositories.

## Files in this package
| path | what |
|---|---|
| `data/` | raw CSVs by source system, and `engagement_raw.sqlite` (the same tables in SQLite) |
| `schema/raw_schema.sql` | raw landing schema (DDL): tables, keys, allowed values |
| `schema/dataset_v3.md` | raw schema spec: every table and column explained |
| `schema/DATA_DICTIONARY.csv` | every raw column with type, meaning, allowed values and raw issues |
| `schema/DATABASE_SCHEMA.png` | ERD: how the raw tables connect |

SQLite enforces foreign keys only after `PRAGMA foreign_keys = ON` on each connection.

## Demo product
A runnable prototype is included in this folder as a Streamlit demo app. It follows the product narrative in the State of the Team Agent document and lets the manager move through the workflow from business goal selection to conversation guidance, commitments, and review tracking.

For online hosting, the app uses `data/engagement_demo.sqlite`, a public-safe database containing team-level aggregates only. It excludes respondent-level survey rows, employee records, and proprietary Q12 item wording. Do not upload `data/engagement_raw.sqlite`, the source CSV files, or `app_backup_before_fix.py` to a public repository.

Run it with:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

The demo includes:
- Screen 1: business goal and ranked Q12 results
- Screen 2: conversation kit and structured prompts
- Screen 3: commitments and confirmed action plan
- Screen 4: review questions and next-step tracking

`app.py` is the final demo prototype.

### Put the demo online with Streamlit Community Cloud

1. Sign in to GitHub and create a new repository. Choose **Public** if anyone should be able to open the finished demo.
2. Upload only these files from this folder: `app.py`, `requirements.txt`, `README.md`, `.gitignore`, and `data/engagement_demo.sqlite`.
3. Do not upload `data/engagement_raw.sqlite`, any source CSVs, or `app_backup_before_fix.py`. The `.gitignore` helps prevent them being added with Git.
4. Sign in to Streamlit Community Cloud with the same GitHub account and select **Create app**.
5. Choose the new repository, branch `main`, and main file path `app.py`, then select **Deploy**.
6. When deployment finishes, Streamlit provides a web link that can be shared.

GitHub and Streamlit will ask you to sign in and approve access. I cannot complete those account-authorization steps for you, but the files above are ready for deployment.
