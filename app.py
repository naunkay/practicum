import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path(__file__).parent / "data" / "engagement_demo.sqlite"
Q12_ITEMS = [f"Q{i:02d}" for i in range(1, 13)]
LEVELS = ["Basic Needs", "Individual", "Teamwork", "Growth"]
LEVEL_DISPLAY = {
    "Basic Needs": "Basic Needs (Q01–Q02)",
    "Individual": "Individual Contribution (Q03–Q06)",
    "Teamwork": "Teamwork (Q07–Q10)",
    "Growth": "Growth (Q11–Q12)",
}
MIN_N = 5  # team-years with fewer respondents are never shown (README privacy rule)
DECLINE_THRESHOLD = -0.15  # Decline Indicator: a flag only, never changes the ranking
MANAGER_DRIVEN = {"Q04", "Q05", "Q06", "Q11"}  # kit starts with manager self-reflection
DEMO_TEAM = "T009"  # Fulfillment - Day, used in the slide walkthrough
VIEWS = ["Cross-period trends", "Four need levels", "Response distribution", "Goal KPI trend"]
MAX_PINS = 2

# Business goals = the metrics that exist in business_outcomes.
# (column, display format, which direction is better)
GOALS = {
    "Turnover rate": ("turnover_rate", "pct", "lower"),
    "Absenteeism rate": ("absenteeism_rate", "pct", "lower"),
    "Quality score": ("quality_score", "score", "higher"),
    "Customer experience score": ("customer_experience_score", "score", "higher"),
}

CANONICAL_OWNERS = {
    o.lower(): o
    for o in ["Team manager", "Team lead", "Department head", "HR business partner", "L&D partner"]
}

# Project-defined percentile bands (not a Gallup standard): background, text color.
BAND_STYLE = {"Green": ("#DDF2E3", "#1B5E33"), "Yellow": ("#FFF3C4", "#6B5200"), "Red": ("#FBE0E0", "#8A1F1F")}

DEFAULT_COMMITMENTS = [
    {"Action": "Start a 10-minute weekly reset", "Owner": "Manager", "Date": "2026-12-01", "Status": "Not started"},
    {"Action": "Clarify handoff ownership for each deliverable", "Owner": "Team leads", "Date": "2026-12-08", "Status": "Not started"},
    {"Action": "Review what is working in the next team check-in", "Owner": "Whole team", "Date": "2026-12-15", "Status": "Not started"},
]
DEFAULT_NOTES = (
    "The team said expectations are clearer when priorities are reviewed in a weekly huddle. "
    "Several people also said communication breaks down at handoff points. "
    "The team agreed to try a short reset and to clarify ownership for the next deliverable."
)

st.set_page_config(page_title="State of the Team Agent", page_icon="🤝", layout="wide")
st.session_state.setdefault("plans", {})          # confirmed records per team (never overwritten by AI output)
st.session_state.setdefault("view_defaults", {})  # saved default view per team
st.session_state.setdefault("assistant_output", {})
st.session_state.setdefault("conversation_chat", {})


# ---------------------------------------------------------------- data access (code, not the LLM)

@st.cache_data
def load_data():
    conn = sqlite3.connect(DB_PATH)

    team_lookup = pd.read_sql_query(
        """
        SELECT t.team_id, t.team_name, d.department_name AS department, t.region
        FROM teams t
        JOIN departments d ON d.department_id = t.department_id
        ORDER BY t.team_id
        """,
        conn,
    )
    q12 = pd.read_sql_query(
        "SELECT item_id, item_label, hierarchy_level FROM q12_items ORDER BY item_id", conn
    ).set_index("item_id")
    means = pd.read_sql_query("SELECT * FROM team_survey_means", conn)
    distributions = pd.read_sql_query("SELECT * FROM team_response_distributions", conn)
    participation = pd.read_sql_query("SELECT * FROM survey_participation", conn)
    outcomes = pd.read_sql_query("SELECT * FROM business_outcomes", conn)
    periods_meta = pd.read_sql_query("SELECT * FROM survey_periods", conn)
    actions = pd.read_sql_query("SELECT * FROM management_actions", conn)
    followups = pd.read_sql_query("SELECT * FROM action_followups", conn)
    decisions = pd.read_sql_query("SELECT * FROM decision_history", conn)
    conn.close()

    # Raw-layer cleaning: mixed date formats and letter case (see README "Known raw-data issues").
    actions["due_date"] = pd.to_datetime(actions["due_date"], format="mixed").dt.date
    actions["owner"] = actions["owner"].str.lower().map(CANONICAL_OWNERS).fillna(actions["owner"])
    followups["followup_date"] = pd.to_datetime(followups["followup_date"], format="mixed").dt.date
    decisions["decision_date"] = pd.to_datetime(decisions["decision_date"], format="mixed").dt.date

    return team_lookup, q12, means, distributions, participation, outcomes, periods_meta, actions, followups, decisions


@st.cache_data
def team_means(aggregates: pd.DataFrame) -> pd.DataFrame:
    """Use pre-aggregated team-level results; no respondent-level data is loaded by the app."""
    means = aggregates.copy()
    means["q12_mean"] = means[Q12_ITEMS].mean(axis=1)
    return means


def get_team_row(team_id, period, means):
    row = means[(means["team_id"] == team_id) & (means["survey_period"] == period)]
    return None if row.empty else row.iloc[0]


def previous_period(period, periods):
    idx = periods.index(period)
    return periods[idx - 1] if idx > 0 else None


def percentile_among(values: pd.Series, value: float) -> float:
    return float(((values < value).mean() + 0.5 * (values == value).mean()) * 100)


def calc_result_ranking(team_id, period, means, q12) -> pd.DataFrame:
    """Q12 Ranking Algorithm: rank items by team mean and by percentile, weight the two ranks equally.

    Percentile (Synthetic Benchmark) = the team's position among all teams with n >= MIN_N
    in the same survey year, computed separately for each item.
    """
    peers = means[(means["survey_period"] == period) & (means["n"] >= MIN_N)]
    team = peers[peers["team_id"] == team_id]
    if team.empty:
        return pd.DataFrame()
    team = team.iloc[0]

    rows = []
    for item in Q12_ITEMS:
        rows.append(
            {
                "item_id": item,
                "item_label": q12.loc[item, "item_label"],
                "hierarchy_level": q12.loc[item, "hierarchy_level"],
                "team_mean": float(team[item]),
                "percentile": percentile_among(peers[item], team[item]),
            }
        )
    df = pd.DataFrame(rows)
    df["mean_rank"] = df["team_mean"].rank(ascending=False, method="average")
    df["pct_rank"] = df["percentile"].rank(ascending=False, method="average")
    df["combined_rank"] = (df["mean_rank"] + df["pct_rank"]) / 2
    df["item_num"] = df["item_id"].str[1:].astype(int)
    df["tie_group"] = (df["item_num"] > 6).astype(int)  # ties favor Q01-Q06, then lower item number
    df.attrs["peer_count"] = len(peers)
    df.attrs["overall_percentile"] = percentile_among(peers["q12_mean"], team["q12_mean"])
    return df


def pick_strength_and_opportunities(ranking):
    strength = ranking.sort_values(["combined_rank", "tie_group", "item_num"]).iloc[0]
    opportunities = ranking.sort_values(
        ["combined_rank", "tie_group", "item_num"], ascending=[False, True, True]
    ).head(2)
    return strength, opportunities


def get_decline_flags(team_row, prev_row):
    if team_row is None or prev_row is None:
        return {}
    flags = {}
    for item in Q12_ITEMS:
        delta = float(team_row[item]) - float(prev_row[item])
        if delta <= DECLINE_THRESHOLD:
            flags[item] = {"previous": float(prev_row[item]), "current": float(team_row[item]), "delta": delta}
    return flags


def ordinal(value: float) -> str:
    n = int(round(value))
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def band_name(percentile: float) -> str:
    return "Green" if percentile >= 75 else "Yellow" if percentile >= 50 else "Red"


def band_label(percentile: float) -> str:
    return {"Green": "Green (75+)", "Yellow": "Yellow (50–74)", "Red": "Red (<50)"}[band_name(percentile)]


def band_chip(percentile: float) -> str:
    bg, fg = BAND_STYLE[band_name(percentile)]
    return (
        f"<span style='background:{bg};color:{fg};padding:2px 10px;border-radius:12px;font-weight:600'>"
        f"{ordinal(percentile)} percentile · {band_label(percentile)}</span>"
    )


def style_band(value: str) -> str:
    for key, (bg, fg) in BAND_STYLE.items():
        if str(value).startswith(key):
            return f"background-color:{bg};color:{fg};font-weight:600"
    return ""


def format_kpi(value, kind):
    if value is None or pd.isna(value):
        return "Not measured"
    return f"{value * 100:.1f}%" if kind == "pct" else f"{value:.1f}"


def parse_view_request(text: str):
    """Interface Agent stand-in: turn a chat request into a supported screen action, or None."""
    t = text.lower()
    m = re.search(r"q\s?0?(\d{1,2})", t)
    item = f"Q{int(m.group(1)):02d}" if m and 1 <= int(m.group(1)) <= 12 else None
    if "trend" in t and "kpi" not in t and "goal" not in t:
        return "Cross-period trends", item
    if "level" in t or "need" in t:
        return "Four need levels", item
    if "distribution" in t or "how many" in t:
        return "Response distribution", item
    if "kpi" in t or "goal" in t:
        return "Goal KPI trend", item
    return None, item


def get_conversation_kit(team_name):
    return {
        "agenda": [
            "1. Start with a team strength.",
            "2. Explore focus items with examples.",
            "3. Agree on actions, owners, and a review date.",
        ],
        "reflection_questions": [
            "What can I do to better support the team?",
            "Where can I make work clearer?",
        ],
        "questions_to_explore": [
            "What is working well today?",
            "What gets in the way of doing your best work?",
            "What small change would help next month?",
        ],
        "gallup_guidance": [
            ("Look for one strength, two opportunities, and a few questions that help the team explain the signal.",
             "Gallup, Interpreting Your Engagement Results (paraphrased)"),
            ("The team, not the AI, owns the reasons behind a score change.",
             "Gallup, Interpreting Your Engagement Results (paraphrased)"),
            ("Review progress after a short interval: did we do it, and did it make a difference?",
             "Gallup, State of the Team Conversation Outline (paraphrased)"),
        ],
        "team_context": f"Team focus: {team_name}. Keep the discussion anchored to shared work, team habits, and the business goal.",
    }


def organize_notes(notes: str):
    """Plan & Follow-up stand-in: only sorts sentences that are in the notes; never adds reasons."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", notes) if s.strip()]
    reasons = [s for s in sentences if re.search(r"\b(said|told|mentioned|felt|noted)\b", s, re.I)]
    decisions = [s for s in sentences if re.search(r"\b(agreed|decided|will|chose)\b", s, re.I)]
    return reasons, decisions


def action_assistant(team_id: str, screen_key: str, commitments, review_date):
    """Action Assistant side panel: exactly three requests; it never adds a commitment."""
    st.markdown("#### Action Assistant")
    st.caption("Three requests only. It drafts options to take back to the team; it never adds a commitment.")
    store = st.session_state["assistant_output"]
    key = f"{team_id}:{screen_key}"
    if st.button("Move a stuck commitment forward", key=f"aa1_{key}", use_container_width=True):
        stuck = [c for c in commitments if c["Status"] in ("Not started", "Partly done")]
        target = stuck[0] if stuck else commitments[0]
        store[key] = (
            f"**Draft — options for:** {target['Action']} (owner: {target['Owner']})\n\n"
            "- Ask the owner what is blocking the first step, and agree one small step for this week.\n"
            "- Offer to pair the owner with a teammate for the first session.\n"
            "- If it no longer fits, raise it at the review instead of dropping it quietly."
        )
    if st.button("Prepare the review meeting", key=f"aa2_{key}", use_container_width=True):
        store[key] = (
            f"**Draft — 10-minute review on {review_date}**\n\n"
            "1. Did we do it? Go through each commitment's status.\n"
            "2. Did it make a difference? Ask for one example each.\n"
            "3. Decide: keep going, adjust the plan, or pick a new focus."
        )
    if st.button("Write a progress update for the team", key=f"aa3_{key}", use_container_width=True):
        done = sum(c["Status"] == "Done" for c in commitments)
        store[key] = (
            f"**Draft update:** We agreed {len(commitments)} commitments after our survey conversation. "
            f"{done} are done so far. We will check in together on {review_date} to ask what has changed "
            "and what to do next."
        )
    if key in store:
        st.info(store[key])


def conversation_assistant(team_id: str, focus_items, q12):
    """Rule-based demo coach: offers questions, never diagnoses survey-score causes."""
    key = str(team_id)
    chats = st.session_state["conversation_chat"]
    if key not in chats:
        chats[key] = [
            {
                "role": "assistant",
                "content": (
                    "Hi! I can help shape the conversation and suggest questions. "
                    "I won't guess why a score changed—the team supplies that context."
                ),
            }
        ]

    st.markdown("### 💬 AI conversation coach")
    st.caption("Demo assistant · supports the conversation, not diagnosis")
    for turn in chats[key]:
        with st.chat_message(turn["role"]):
            st.write(turn["content"])

    with st.form(f"coach_form_{key}", clear_on_submit=True):
        prompt = st.text_input(
            "Message the coach",
            placeholder="Ask for an agenda or a question to explore…",
            label_visibility="collapsed",
        )
        submitted = st.form_submit_button("Send", use_container_width=True, type="primary")

    if submitted and prompt.strip():
        user_text = prompt.strip()
        item_names = ", ".join(
            f"{item} {q12.loc[item, 'item_label']}" for item in focus_items
        )
        lowered = user_text.lower()
        if any(word in lowered for word in ("why", "cause", "reason")):
            response = (
                "I can't determine the cause from survey scores. Ask the team what they have "
                "experienced around the focus items, then capture their words in the meeting notes. "
                f"Current focus: {item_names}."
            )
        elif "agenda" in lowered or "meeting" in lowered:
            response = (
                "Try this 20-minute flow: 5 minutes on a team strength, 8 minutes exploring "
                f"{item_names}, 5 minutes agreeing on 2–3 actions, and 2 minutes naming owners "
                "and a review date."
            )
        else:
            response = (
                f"For {item_names}, invite concrete examples: “When have we seen this at its "
                "best?” and “What small change would help next month?” Let the team identify "
                "the reasons and choose the actions."
            )
        chats[key].extend(
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": response},
            ]
        )
        st.rerun()


# ---------------------------------------------------------------- app

(team_lookup, q12, aggregate_results, response_distributions, participation, outcomes, periods_meta,
 management_actions, action_followups, decision_history) = load_data()
means = team_means(aggregate_results)
periods = sorted(means["survey_period"].unique().tolist())

st.markdown(
    """
    <style>
    .stApp {
        background: linear-gradient(180deg, #f5f8f3 0%, #edf5eb 100%);
    }
    .stApp > div {
        background: transparent;
    }
    [data-testid="stAppViewContainer"] {
        color: #17231a;
    }
    [data-testid="stMarkdownContainer"] p,
    [data-testid="stCaptionContainer"] {
        color: #34463a;
    }
    [data-testid="stWidgetLabel"] p,
    label {
        color: #24382b !important;
        font-weight: 650 !important;
    }
    input, textarea, [data-baseweb="select"] > div {
        background-color: #ffffff !important;
        color: #142018 !important;
        border-color: #738678 !important;
    }
    [data-testid="stDataFrame"] {
        border: 1px solid #a7b3a8;
        border-radius: 10px;
        overflow: hidden;
    }
    .nav-shell {
        background: linear-gradient(180deg, #0c120d 0%, #111b15 100%);
        border-radius: 18px;
        padding: 18px 12px 14px 12px;
        min-height: calc(100vh - 40px);
        margin-right: 18px;
        box-shadow: 0 10px 24px rgba(8, 20, 13, 0.18);
        border: 1px solid rgba(255,255,255,0.05);
    }
    .nav-header {
        display: flex;
        align-items: center;
        gap: 10px;
        color: #f6f7f2;
        font-weight: 800;
        font-size: 1.15rem;
        padding: 4px 8px 18px 8px;
    }
    .nav-header .icon {
        width: 28px; height: 28px; border-radius: 9px; background: linear-gradient(180deg, #f4d35e 0%, #d7b12b 100%); color: #111b15; display:flex; align-items:center; justify-content:center; font-size: 1rem; box-shadow: inset 0 -2px 0 rgba(0,0,0,0.18);
    }
    .stButton > button {
        width: 100%;
        border: 1px solid #174b2c;
        background: #174b2c;
        color: #ffffff;
        text-align: left;
        padding: 10px 12px;
        border-radius: 12px;
        margin: 6px 0;
        display: flex;
        align-items: center;
        gap: 10px;
        font-size: 0.96rem;
        font-weight: 700;
        cursor: pointer;
        box-shadow: none;
    }
    .stButton > button:hover {
        background: #d7b12b;
        border-color: #8a6d00;
        color: #111b15;
    }
    .stButton > button:focus,
    .stButton > button:active,
    .stButton > button:focus-visible {
        background: #d7b12b;
        border: 1px solid #8a6d00;
        box-shadow: inset 0 -2px 0 rgba(0,0,0,0.1);
        color: #111b15;
    }
    .step-pill {
        width: 28px; height: 28px; border-radius: 8px; background: linear-gradient(180deg, #dff7c8 0%, #b7e27d 100%); color: #0d180f; display:inline-flex; align-items:center; justify-content:center; font-size: 0.8rem; font-weight: 800; box-shadow: inset 0 -2px 0 rgba(0,0,0,0.12);
    }
    .screen-header {
        display: flex; justify-content: space-between; align-items: center; margin: 6px 0 18px 0;
    }
    .screen-header h1 {
        font-size: 2.05rem; font-weight: 900; color: #0c1a12; margin: 0; padding: 0;
    }
    .screen-pill {
        background: linear-gradient(180deg, #f4d35e 0%, #d7b12b 100%);
        color: #101a13; font-size: 0.78rem; font-weight: 800; border-radius: 999px; padding: 7px 12px; letter-spacing: 0.02em; box-shadow: inset 0 -2px 0 rgba(0,0,0,0.10);
    }
    .content-panel {
        background: rgba(255,255,255,0.65); border: 1px solid rgba(20,50,90,0.08); border-radius: 16px; padding: 18px; box-shadow: 0 2px 6px rgba(14,25,50,0.04);
    }
    .mini-card {
        background: #ffffff; border: 1px solid rgba(80,110,160,0.12); border-radius: 12px; padding: 12px 14px; min-height: 86px; box-shadow: inset 0 1px 0 rgba(255,255,255,0.6);
    }
    .mini-card h4 { margin: 0 0 6px 0; color: #304b37; font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.08em; }
    .mini-card .value { font-size: 1.12rem; font-weight: 800; color: #101914; }
    .mini-card .delta { font-size: 0.74rem; color: #1d6b39; }
    .strong-card, .opp-card {
        border-radius: 14px; padding: 14px; min-height: 180px; display: flex; flex-direction: column; gap: 8px; border: 1px solid rgba(20,40,25,0.08);
    }
    .strong-card { background: linear-gradient(180deg, #e7f6e9 0%, #dff2dd 100%); }
    .opp-card { background: linear-gradient(180deg, #fff9e8 0%, #fdf1d4 100%); }
    .status-label { font-size: 0.72rem; font-weight: 800; text-transform: uppercase; letter-spacing: 0.08em; color: #1c3d2c; }
    .theta-card { background: #f9faf7; border-radius: 14px; border: 1px solid rgba(80,110,160,0.1); padding: 16px; }
    .subtle { color: #35483a; }
    .chat-bubble { background: #f4efe6; border:1px solid rgba(117,108,72,0.14); border-radius: 18px; padding: 12px 14px; margin-bottom: 8px; }
    .chat-bubble.alt { background: #edf6ef; }
    .commitment-row { background: #ffffff; border:1px solid rgba(20,40,25,0.08); border-radius: 10px; padding: 8px 12px; margin-bottom: 8px; }
    .screen-subtitle { color: #35483a; font-size: 0.92rem; margin: 0 0 14px 0; }
    .select-label { font-size: 0.8rem; font-weight: 800; color: #24382b; }
    </style>
    """,
    unsafe_allow_html=True,
)

team_ids = team_lookup["team_id"].tolist()
team_names = dict(zip(team_lookup["team_id"], team_lookup["team_name"]))
SCREENS = ["Screen 1 — Goal & results", "Screen 2 — Conversation kit", "Screen 3 — Commitments", "Screen 4 — Review"]
NAV_LABELS = ["Goal & Results", "Conversation Kit", "Commitments", "Review"]

if "screen_index" not in st.session_state:
    st.session_state.screen_index = 0

nav_col, main_col = st.columns([0.22, 0.78])
with nav_col:
    st.markdown(
        """
        <div class='nav-shell'>
            <div class='nav-header'><span class='icon'>🤝</span>State of the Team Agent</div>
        """,
        unsafe_allow_html=True,
    )
    for idx, label in enumerate(NAV_LABELS):
        if st.button(f"{idx + 1} {label}", key=f"nav_{idx}", use_container_width=True):
            st.session_state.screen_index = idx
    st.markdown("</div>", unsafe_allow_html=True)

with main_col:
    selected_team_id = st.selectbox(
        "Team",
        team_ids,
        index=team_ids.index(DEMO_TEAM) if DEMO_TEAM in team_ids else 0,
        format_func=lambda t: f"{team_names[t]} ({t})",
        key="team_select",
    )
    selected_team = team_lookup[team_lookup["team_id"] == selected_team_id].iloc[0]
    goal_cols = st.columns([1.2, 1.2, 1.5, 1])
    with goal_cols[0]:
        selected_goal = st.selectbox("Business goal", list(GOALS.keys()), key="goal_select")
    with goal_cols[1]:
        selected_period = st.selectbox("Survey year", periods, index=len(periods) - 1, key="period_select")
    with goal_cols[2]:
        selected_focus_level = st.selectbox(
            "Q12 focus level",
            ["All Q12 levels"] + [LEVEL_DISPLAY[level] for level in LEVELS],
            key="q12_focus_level",
        )
    with goal_cols[3]:
        st.write("")
        st.write("")
        st.write(f"Team: {selected_team['team_name']} ({selected_team['department']})")
    screen = SCREENS[st.session_state.screen_index]
    plan = st.session_state["plans"].get(selected_team_id)
    st.markdown(
        f"<div class='screen-header'><h1>{screen.split(' — ')[1]}</h1><div class='screen-pill'>Screen {st.session_state.screen_index + 1} of 4</div></div>",
        unsafe_allow_html=True,
    )

# Shared calculations for the selected team and year (all in code).
team_row = get_team_row(selected_team_id, selected_period, means)
prev_period = previous_period(selected_period, periods)
prev_row = get_team_row(selected_team_id, prev_period, means) if prev_period else None
if prev_row is not None and prev_row["n"] < MIN_N:
    prev_row = None  # a hidden prior result cannot be used for comparison either
team_n = int(team_row["n"]) if team_row is not None else 0
visible = team_row is not None and team_n >= MIN_N

ranking = calc_result_ranking(selected_team_id, selected_period, means, q12) if visible else pd.DataFrame()
if not ranking.empty:
    strength, opportunities = pick_strength_and_opportunities(ranking)
decline_flags = get_decline_flags(team_row, prev_row) if visible else {}

goal_col, goal_kind, goal_direction = GOALS[selected_goal]
team_outcomes = outcomes[outcomes["team_id"] == selected_team_id].set_index("survey_period")
kpi_now = team_outcomes[goal_col].get(selected_period) if selected_period in team_outcomes.index else None
kpi_prev = team_outcomes[goal_col].get(prev_period) if prev_period in team_outcomes.index else None
released = periods_meta.set_index("survey_period")["results_released"].get(selected_period)


def suppressed_notice():
    if team_row is None:
        st.warning("No survey results for this team and year.")
    else:
        st.warning(
            f"Only {team_n} people on this team responded in {selected_period}. Results are hidden "
            f"when fewer than {MIN_N} people respond, so no individual can be identified."
        )


def item_change(item):
    return float(team_row[item]) - float(prev_row[item]) if prev_row is not None else None


def current_commitments():
    return plan["commitments"] if plan else DEFAULT_COMMITMENTS


def default_review_date():
    last = max(date.fromisoformat(c["Date"]) for c in current_commitments())
    return last + timedelta(days=14)


if screen == SCREENS[0]:
    # Chat requests are read first so they can update the configurable area below (Interface Agent).
    pins_key, item_key = f"pins_{selected_team_id}", f"trend_item_{selected_team_id}"
    if pins_key not in st.session_state:
        st.session_state[pins_key] = st.session_state["view_defaults"].get(selected_team_id, {}).get("pins", [])
    chat = st.chat_input("Ask for a view, e.g. 'Show me the trend for Q02'")
    chat_reply = None
    if chat:
        view, item = parse_view_request(chat)
        if view is None:
            chat_reply = ("That isn't a supported view. I can show: " + ", ".join(VIEWS) +
                          ". I don't explain why a score moved; that comes from the team.")
        else:
            pins = [p for p in st.session_state[pins_key] if p != view] + [view]
            st.session_state[pins_key] = pins[-MAX_PINS:]
            if item:
                st.session_state[item_key] = item
            action = f'show_view(view="{view}"' + (f', item="{item}"' if item else "") + ")"
            chat_reply = f"Done: `{action}`"

    st.subheader("Step 0: Set business goal")
    st.info("Business goal adds context; it does not change the Q12 ranking.")

    part = participation[
        (participation["team_id"] == selected_team_id) & (participation["survey_period"] == selected_period)
    ]
    eligible = int(part["eligible_count"].iat[0]) if not part.empty else None

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Business goal", selected_goal)
    kpi_delta = None
    if kpi_now is not None and kpi_prev is not None and not pd.isna(kpi_now) and not pd.isna(kpi_prev):
        diff = kpi_now - kpi_prev
        kpi_delta = f"{diff * 100:+.1f} pp vs {prev_period}" if goal_kind == "pct" else f"{diff:+.1f} vs {prev_period}"
    col2.metric(f"Starting KPI ({selected_period})", format_kpi(kpi_now, goal_kind), delta=kpi_delta,
                delta_color="inverse" if goal_direction == "lower" else "normal")
    col3.metric("Respondents", f"{team_n} of {eligible}" if eligible else str(team_n),
                delta=f"n ≥ {MIN_N}: shown" if visible else f"n < {MIN_N}: hidden", delta_color="off")
    col4.metric("Review cadence", "30 days")
    st.caption("KPI is context only—not evidence of cause.")

    if not visible:
        suppressed_notice()
        st.stop()

    st.subheader("Step 1: Understand results")
    o1, o2, o3 = st.columns(3)
    o1.metric("Overall Q12 mean", f"{team_row['q12_mean']:.2f}",
              delta=f"{team_row['q12_mean'] - prev_row['q12_mean']:+.2f} vs {prev_period}" if prev_row is not None else None)
    o2.metric("Overall percentile", ordinal(ranking.attrs["overall_percentile"]))
    o3.metric("Decline flags", str(len(decline_flags)))

    def card(col, title, row, note):
        with col:
            with st.container(border=True):
                st.caption(title)
                st.markdown(f"#### {row['item_id']} {row['item_label']}")
                change = item_change(row["item_id"])
                change_txt = f" · change {change:+.2f}" if change is not None else ""
                st.markdown(f"Mean **{row['team_mean']:.2f}**{change_txt}")
                st.markdown(band_chip(row["percentile"]), unsafe_allow_html=True)
                if row["item_id"] in decline_flags:
                    st.warning("Declined by 0.15+")
                st.write(note)

    c1, c2, c3 = st.columns(3)
    card(c1, "STRENGTH", strength, "Highest combined mean and percentile rank.")
    for col, (_, opp) in zip([c2, c3], opportunities.iterrows()):
        note = "Among the lowest combined ranks."
        if opp["item_id"] in MANAGER_DRIVEN:
            note += " Starts with manager reflection."
        card(col, "OPPORTUNITY", opp, note)
    st.caption("Ranked by team mean and synthetic percentile. Declines are flags, not explanations.")

    focus = [strength["item_id"]] + opportunities["item_id"].tolist()
    past = decision_history[(decision_history["team_id"] == selected_team_id) &
                            (decision_history["issue_focus"].isin(focus))].sort_values("decision_date", ascending=False)
    with st.expander(f"From Decision History: earlier decisions on these items ({len(past)})", expanded=False):
        if past.empty:
            st.write("No earlier decisions on these items.")
        else:
            st.dataframe(past[["decision_date", "issue_focus", "intervention", "outcome_assessment", "notes"]],
                         use_container_width=True, hide_index=True)

    st.markdown("**Full results**")
    table = ranking.copy()
    if selected_focus_level != "All Q12 levels":
        selected_level = next(
            level for level, display in LEVEL_DISPLAY.items() if display == selected_focus_level
        )
        table = table[table["hierarchy_level"] == selected_level]
    st.caption(
        "Focus level filters this table; cards use all 12 items."
    )
    table = pd.DataFrame(
        {
            "Item": table["item_id"] + " " + table["item_label"],
            "Need level": table["hierarchy_level"],
            "Mean": table["team_mean"].round(2),
            "Change": [round(item_change(i), 2) if prev_row is not None else None for i in table["item_id"]],
            "Decline flag": ["Yes" if i in decline_flags else "" for i in table["item_id"]],
            "Percentile": table["percentile"].round(0).astype(int),
            "Band": table["percentile"].apply(band_label),
        }
    )
    styler = table.style.format({"Mean": "{:.2f}", "Change": "{:+.2f}"}, na_rep="–")
    styler = (styler.map if hasattr(styler, "map") else styler.applymap)(style_band, subset=["Band"])
    st.dataframe(styler, use_container_width=True, hide_index=True)
    st.caption(f"Percentiles compare {ranking.attrs['peer_count']} teams (n ≥ {MIN_N}); synthetic benchmark, not Gallup.")

    # ---------------- Configurable area (Customizable Views)
    st.markdown("### Your views")
    st.caption(f"Pin up to {MAX_PINS} views. Ask for a view in chat.")
    if item_key not in st.session_state:
        st.session_state[item_key] = opportunities.iloc[0]["item_id"]
    v1, v2, v3 = st.columns([3, 1, 1])
    with v1:
        st.multiselect("Pinned views", VIEWS, key=pins_key, max_selections=MAX_PINS)
    with v2:
        if st.button("Save as team default", use_container_width=True):
            st.session_state["view_defaults"][selected_team_id] = {"pins": list(st.session_state[pins_key])}
            st.toast("Saved as this team's default view")
    with v3:
        if st.button("Reset to system default", use_container_width=True):
            st.session_state["view_defaults"].pop(selected_team_id, None)
            st.session_state.pop(pins_key, None)
            st.rerun()
    if chat_reply:
        with st.chat_message("assistant"):
            st.write(chat_reply)

    hist = means[(means["team_id"] == selected_team_id) & (means["n"] >= MIN_N)].set_index("survey_period")
    pins = st.session_state[pins_key]
    if not pins:
        st.write("No views pinned.")
    cols = st.columns(len(pins)) if pins else []
    for col, view in zip(cols, pins):
        with col:
            with st.container(border=True):
                st.markdown(f"**{view}**")
                if view == "Cross-period trends":
                    item = st.selectbox("Item", Q12_ITEMS, key=item_key,
                                        format_func=lambda i: f"{i} {q12.loc[i, 'item_label']}")
                    st.line_chart(hist[[item]].rename(columns={item: f"{item} team mean"}))
                elif view == "Four need levels":
                    rows = []
                    for lv in LEVELS:
                        items = [i for i in Q12_ITEMS if q12.loc[i, "hierarchy_level"] == lv]
                        lowest = ranking[ranking["item_id"].isin(items)].sort_values("percentile").iloc[0]
                        now = float(team_row[items].mean())
                        rows.append({"Level": lv, "Mean": round(now, 2),
                                     "Change": round(now - float(prev_row[items].mean()), 2) if prev_row is not None else None,
                                     "Lowest-percentile item": f"{lowest['item_id']} ({ordinal(lowest['percentile'])})"})
                    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
                    st.caption("Level means are project-defined.")
                elif view == "Response distribution":
                    item = st.session_state.get(item_key, opportunities.iloc[0]["item_id"])
                    sub = response_distributions[
                        (response_distributions["team_id"] == selected_team_id)
                        & (response_distributions["survey_period"] == selected_period)
                        & (response_distributions["item_id"] == item)
                    ].set_index("score")
                    counts = sub["response_count"].reindex([1, 2, 3, 4, 5], fill_value=0)
                    counts.index = [str(i) for i in counts.index]
                    st.bar_chart(counts.rename(f"{item} responses"))
                    st.caption(f"{item}: response counts by score (1–5).")
                elif view == "Goal KPI trend":
                    kpi = team_outcomes[[goal_col]].rename(columns={goal_col: selected_goal})
                    st.line_chart(kpi)
                    st.caption("Context only—not evidence of cause.")
    st.progress(25)

elif screen == SCREENS[1]:
    st.subheader("Step 2: Prepare conversation")
    if not visible:
        suppressed_notice()
        st.stop()

    focus_items = opportunities["item_id"].tolist()
    kit = get_conversation_kit(selected_team["team_name"])
    if any(i in MANAGER_DRIVEN for i in focus_items):
        st.info("Manager-driven focus: start with self-reflection.")

    conversation_col, assistant_col = st.columns([2.2, 1], gap="large")
    with conversation_col:
        st.markdown(
            "**Focus items:** "
            + " · ".join(f"{i} {q12.loc[i, 'item_label']}" for i in focus_items)
        )
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("### Suggested agenda")
            for item in kit["agenda"]:
                st.markdown(f"- {item}")
            st.markdown("### Questions to explore")
            for item in kit["questions_to_explore"]:
                st.markdown(f"- {item}")
        with col2:
            st.markdown("### Manager self-reflection")
            for item in kit["reflection_questions"]:
                st.markdown(f"- {item}")
            st.markdown("### Gallup guidance")
            for text, source in kit["gallup_guidance"]:
                st.write(f"{text}  \n*Source: {source}*")
        st.caption("Conversation prompts are project-authored.")

        st.markdown("### From Decision History")
        past = decision_history[(decision_history["team_id"] == selected_team_id) &
                                (decision_history["issue_focus"].isin(focus_items))].sort_values("decision_date", ascending=False)
        if past.empty:
            st.write("No earlier decisions on these items for this team.")
        else:
            st.dataframe(past[["decision_date", "issue_focus", "intervention", "outcome_assessment", "notes"]],
                         use_container_width=True, hide_index=True)
        st.write(kit["team_context"])
    with assistant_col:
        with st.container(border=True):
            conversation_assistant(selected_team_id, focus_items, q12)
    st.progress(50)

elif screen == SCREENS[2]:
    st.subheader("Step 3: Capture decisions and commitments")
    main, side = st.columns([3, 1])
    with main:
        if plan:
            st.success(f"Confirmed record saved to Decision History. Review date: {plan['review_date']}. "
                       "Regenerated drafts can't overwrite it.")
        st.write("Add meeting notes, then confirm the team plan.")
        notes = st.text_area("Team discussion notes", value=plan["notes"] if plan else DEFAULT_NOTES, height=120)
        reasons, decisions_found = organize_notes(notes)

        r1, r2 = st.columns(2)
        with r1:
            st.markdown("**Reasons the team gave** (organized from the notes)")
            for r in reasons or ["No reasons found in the notes."]:
                st.markdown(f"- {r}")
        with r2:
            st.markdown("**What the team decided**")
            for d in decisions_found or ["No decisions found in the notes."]:
                st.markdown(f"- {d}")
        st.caption("Reasons and decisions are taken from your notes.")

        st.markdown("**Commitments**")
        st.dataframe(pd.DataFrame(current_commitments()), use_container_width=True, hide_index=True)

        shared = sum(1 for r in current_commitments() if r["Owner"] != "Manager")
        total = len(current_commitments())
        if shared * 2 >= total:
            st.success(f"Plan check passed: {shared} of {total} commitments are owned by someone other than the manager.")
        else:
            st.warning(f"Plan check: only {shared} of {total} commitments are owned by someone other than the manager. "
                       "Consider sharing ownership with the team.")

        review_date = st.date_input("Review date", value=plan["review_date"] if plan else default_review_date())
        if st.button("Confirm commitment plan", type="primary", disabled=bool(plan)):
            st.session_state["plans"][selected_team_id] = {
                "notes": notes, "reasons": reasons, "commitments": [dict(c) for c in DEFAULT_COMMITMENTS],
                "review_date": review_date,
            }
            st.rerun()
    with side:
        action_assistant(selected_team_id, "s3", current_commitments(),
                         plan["review_date"] if plan else default_review_date())
    st.progress(75)

elif screen == SCREENS[3]:
    st.subheader("Step 4: Review progress")
    main, side = st.columns([3, 1])
    with main:
        if not plan:
            st.warning("No confirmed plan yet. A review needs confirmed commitments: go to Screen 3 and confirm the plan first.")
        st.write("Review actions and team feedback.")

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("### Review questions")
            st.radio("Did we do it?", ["Mostly yes", "Partially", "No"], index=0)
            st.radio("Did it make a difference?", ["Yes", "Somewhat", "Not yet"], index=1)
            st.text_input("What the team said, in its own words", value="Handoffs feel clearer; the weekly reset is still new.")
        with col2:
            st.markdown("### Goal KPI")
            st.metric(selected_goal, "Next quarterly update")
            st.caption(f"Starting value: {format_kpi(kpi_now, goal_kind)}. Next quarterly update pending; context only.")

        st.markdown("### This cycle's commitments")
        edited = st.data_editor(
            pd.DataFrame(current_commitments()),
            column_config={"Status": st.column_config.SelectboxColumn(
                "Status", options=["Not started", "Partly done", "Done"], required=True)},
            disabled=["Action", "Owner", "Date"], hide_index=True, use_container_width=True,
            key=f"status_{selected_team_id}",
        )
        if plan:
            plan["commitments"] = edited.to_dict("records")

        st.markdown("### Decision & Action Tracking")
        st.caption("Execution, team feedback, and measured change. No causal claims.")
        team_dec = decision_history[(decision_history["team_id"] == selected_team_id) &
                                    decision_history["action_id"].notna() & (decision_history["action_id"] != "")]
        rows = []
        for _, d in team_dec.sort_values("decision_date", ascending=False).iterrows():
            act = management_actions[management_actions["action_id"] == d["action_id"]]
            fu = action_followups[action_followups["action_id"] == d["action_id"]].sort_values("followup_date")
            last = fu.iloc[-1] if not fu.empty else None
            rows.append({
                "Decided": d["decision_date"],
                "Item": d["issue_focus"],
                "Action": d["intervention"],
                "1 · Execution": act["status"].iat[0].replace("_", " ") if not act.empty else "–",
                "2 · Team feedback": last["qualitative_feedback"] if last is not None else "–",
                "3 · Measured change": last["outcome_status"] if last is not None else "–",
            })
        if rows:
            st.dataframe(
                pd.DataFrame(rows), use_container_width=True, hide_index=True,
                column_config={
                    "Decided": st.column_config.Column(width="small"),
                    "Item": st.column_config.Column(width="small"),
                    "Action": st.column_config.Column(width="medium"),
                    "1 · Execution": st.column_config.Column(width="small"),
                    "2 · Team feedback": st.column_config.Column(width="medium"),
                    "3 · Measured change": st.column_config.Column(width="small"),
                },
            )
        else:
            st.write("No earlier tracked decisions for this team.")

        st.markdown("### Step 5: Recalibrate")
        choice = st.radio("Team next step", ["Keep going", "Adjust the plan", "New focus"], index=0, horizontal=True)
        destination = {"Keep going": "Step 4 (next review scheduled)", "Adjust the plan": "Step 2", "New focus": "Step 1"}[choice]
        st.write(f"Selected next step: {choice} → returns to {destination}.")
    with side:
        action_assistant(selected_team_id, "s4", current_commitments(),
                         plan["review_date"] if plan else default_review_date())
    st.progress(100)

st.markdown("---")
with st.expander("Agent architecture and governance"):
    st.markdown(
        """
        - One Orchestrator routes the request to the correct flow.
        - Results Analysis Agent: the ranking is calculated in code, not by a language model.
        - Conversation Coach provides questions and guidance; it never invents causes.
        - Plan & Follow-up Agent records confirmed commitments and tracks review status.
        - Interface Agent turns chat requests into supported view actions.
        - Output checks enforce: every number from a tool, no causal claims, no individual-level analysis.
        - In this prototype the agents are rule-based stand-ins; numbers, ranking and thresholds run in code.
        """
    )

with st.expander("Synthetic dataset notes"):
    st.caption(
        f"This demonstration uses synthetic data only. Results are shown only for team-years with at least {MIN_N} "
        "respondents, and only as team-level aggregates."
    )
