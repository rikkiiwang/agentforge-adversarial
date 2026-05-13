from __future__ import annotations

import os

import altair as alt
import pandas as pd
import psycopg
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]
TARGET_URL = os.environ.get("TARGET_URL", "")
DEPLOY_ENV = "Railway" if "railway" in DATABASE_URL.lower() else "local"


@st.cache_data(ttl=10)
def fetch_runs() -> pd.DataFrame:
    with psycopg.connect(DATABASE_URL) as conn:
        return pd.read_sql_query(
            """
            SELECT
              ar.id::text AS id,
              ar.created_at,
              ar.case_id,
              ar.category,
              ar.subcategory,
              ar.source,
              ar.red_team_subagent_id,
              ar.red_team_model,
              ar.judge_verdict,
              ar.judge_reasoning,
              ar.judge_rubric_version,
              ar.target_version,
              ar.latency_ms,
              ar.attack_prompt,
              ar.observed_output,
              c.name AS campaign_name,
              c.id::text AS campaign_id
            FROM attack_runs ar
            JOIN campaigns c ON c.id = ar.campaign_id
            ORDER BY ar.created_at DESC
            """,
            conn,
        )


st.set_page_config(page_title="AI Security Platform Dashboard", layout="wide")
st.title("AI Security Platform Dashboard")
header_bits = [
    f"DB: **{DEPLOY_ENV}**",
]
if TARGET_URL:
    header_bits.append(f"target: `{TARGET_URL}`")
st.caption(
    " · ".join(header_bits)
    + " · attack runs source: Postgres `attack_runs` table"
    + " (see `docs/components/database-schema.md`)"
)

df = fetch_runs()
if df.empty:
    st.info(
        "No attack runs yet. Start one with:\n\n"
        "`python -m agentforge_adversarial run --cases evals/cases --mutate`"
    )
    st.stop()

# --- Top: KPIs ---
total = len(df)
fails = (df["judge_verdict"] == "fail").sum()
partials = (df["judge_verdict"] == "partial").sum()
passes = (df["judge_verdict"] == "pass").sum()
campaigns_n = df["campaign_id"].nunique()

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total runs", total)
c2.metric("FAIL", int(fails))
c3.metric("PARTIAL", int(partials))
c4.metric("PASS", int(passes))
c5.metric("Campaigns", int(campaigns_n))

# --- Verdict heat-map by category ---
st.subheader("Coverage heat-map  ·  category × verdict")
st.caption(
    "Each cell is the count of attack runs at that category × verdict cross. "
    "Darker = more runs. FAIL columns are the actionable signal."
)
heat = (
    df.groupby(["category", "judge_verdict"]).size().unstack(fill_value=0)
)
for col in ("pass", "partial", "fail"):
    if col not in heat.columns:
        heat[col] = 0
heat = heat[["pass", "partial", "fail"]]

heat_long = heat.reset_index().melt(
    id_vars="category", var_name="verdict", value_name="count"
)
verdict_order = ["pass", "partial", "fail"]
verdict_colors = {"pass": "#2ca02c", "partial": "#f0ad4e", "fail": "#d9534f"}
heat_chart = (
    alt.Chart(heat_long)
    .mark_rect(stroke="white", strokeWidth=1)
    .encode(
        x=alt.X(
            "verdict:N",
            sort=verdict_order,
            axis=alt.Axis(title=None, labelAngle=0, labelFontSize=13),
        ),
        y=alt.Y("category:N", axis=alt.Axis(title=None, labelFontSize=13)),
        color=alt.Color(
            "verdict:N",
            sort=verdict_order,
            scale=alt.Scale(
                domain=verdict_order,
                range=[verdict_colors[v] for v in verdict_order],
            ),
            legend=None,
        ),
        opacity=alt.Opacity(
            "count:Q",
            scale=alt.Scale(domain=[0, max(1, int(heat_long["count"].max()))], range=[0.15, 1.0]),
            legend=None,
        ),
        tooltip=["category", "verdict", "count"],
    )
    .properties(height=max(160, 40 * heat.shape[0]))
)
heat_labels = (
    alt.Chart(heat_long)
    .mark_text(fontSize=14, fontWeight="bold", color="white")
    .encode(
        x=alt.X("verdict:N", sort=verdict_order),
        y=alt.Y("category:N"),
        text=alt.Text("count:Q"),
    )
)
st.altair_chart(heat_chart + heat_labels, use_container_width=True)

# --- Filters ---
st.subheader("Attack runs")
fcol1, fcol2, fcol3 = st.columns(3)
with fcol1:
    cats = st.multiselect(
        "Category",
        sorted(df["category"].unique()),
        default=list(sorted(df["category"].unique())),
    )
with fcol2:
    verdicts = st.multiselect(
        "Verdict",
        ["pass", "partial", "fail"],
        default=["pass", "partial", "fail"],
    )
with fcol3:
    sources = st.multiselect(
        "Source",
        sorted(df["source"].unique()),
        default=list(sorted(df["source"].unique())),
    )

mask = (
    df["category"].isin(cats)
    & df["judge_verdict"].isin(verdicts)
    & df["source"].isin(sources)
)
filtered = df[mask]

st.caption(f"{len(filtered)} of {total} runs match filters")
st.dataframe(
    filtered[
        [
            "case_id",
            "category",
            "subcategory",
            "source",
            "red_team_subagent_id",
            "judge_verdict",
            "judge_rubric_version",
            "latency_ms",
        ]
    ],
    use_container_width=True,
    hide_index=True,
)

# --- Inspect a run ---
st.subheader("Inspect a single attack run")
st.caption(
    "Pick a row above to see the full attack prompt, the target's observed output, "
    "and the Judge's reasoning."
)
if filtered.empty:
    st.info("No runs match the active filters.")
else:
    ids = filtered["id"].tolist()

    def _label(run_id: str) -> str:
        row = filtered.loc[filtered["id"] == run_id].iloc[0]
        return f"{row['case_id']}  —  {row['category']}  —  {row['judge_verdict']}"

    case_choice = st.selectbox(
        "Attack run", options=ids, format_func=_label, label_visibility="collapsed"
    )
    row = filtered.loc[filtered["id"] == case_choice].iloc[0]

    meta_l, meta_r = st.columns(2)
    with meta_l:
        st.markdown(f"**Verdict:** `{row['judge_verdict']}`")
        st.markdown(f"**Rubric:** `{row['judge_rubric_version']}`")
        st.markdown(f"**Source:** `{row['source']}`")
    with meta_r:
        st.markdown(f"**Red Team subagent:** `{row['red_team_subagent_id']}`")
        st.markdown(f"**Red Team model:** `{row['red_team_model']}`")
        st.markdown(f"**Latency:** {int(row['latency_ms'])} ms")

    st.markdown("**Judge reasoning:**")
    st.info(row["judge_reasoning"])

    st.markdown("**Attack prompt:**")
    st.code(row["attack_prompt"], language="text")

    st.markdown("**Observed output:**")
    st.code(row["observed_output"], language="text")
