from __future__ import annotations

import os
import time
from pathlib import Path
from uuid import UUID

import altair as alt
import pandas as pd
import psycopg
import streamlit as st
from dotenv import load_dotenv

from launcher import (  # streamlit puts dashboard/ on sys.path
    campaign_progress,
    pid_alive,
    start_campaign,
)

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]
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
st.caption(
    f"DB: **{DEPLOY_ENV}** · attack runs source: Postgres `attack_runs` table "
    "(see `docs/components/database-schema.md`)"
)

# --- Launch panel (operator console — Campaign-approval, see docs/components/dashboard.md §4.1) ---


@st.cache_data(ttl=5)
def fetch_targets() -> list[dict]:
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, name, target_type, target_url, config_json, notes "
            "FROM targets ORDER BY created_at ASC"
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _insert_target(name, target_type, target_url, config, notes) -> None:
    import json as _json
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO targets (name, target_type, target_url, config_json, notes) "
            "VALUES (%s, %s, %s, %s::jsonb, %s)",
            (name, target_type, target_url, _json.dumps(config or {}), notes),
        )
        conn.commit()


targets = fetch_targets()
active = st.session_state.get("active_campaign")

with st.expander("🚀 Launch a campaign", expanded=active is None):
    if not targets:
        st.error(
            "No targets configured. Run `make init-db` to seed the default "
            "Co-Pilot target, then refresh."
        )
    else:
        names = [t["name"] for t in targets]
        idx = st.session_state.get("selected_target_idx", 0)
        if idx >= len(names):
            idx = 0
        sel = st.selectbox(
            "Target system",
            options=names,
            index=idx,
            disabled=active is not None,
            help="The AI system to attack. Add more via '+ Add target' below.",
        )
        st.session_state["selected_target_idx"] = names.index(sel)
        target = targets[names.index(sel)]
        meta_bits = [f"type: `{target['target_type']}`",
                     f"url: `{target['target_url']}`"]
        if target["target_type"] == "copilot":
            pid_val = (target.get("config_json") or {}).get("patient_id") or "—"
            meta_bits.append(f"patient_id: `{pid_val[:12]}…`" if pid_val != "—" else "patient_id: `—`")
        st.caption(" · ".join(meta_bits))

        cases_dir = st.text_input(
            "Cases dir", "evals/cases", disabled=active is not None
        )

        ready = active is None
        if target["target_type"] == "copilot":
            pid_val = (target.get("config_json") or {}).get("patient_id") or ""
            if not pid_val:
                st.warning(
                    "⚠️ Co-Pilot target needs `patient_id` configured. "
                    "Edit the row's `config_json` (psql) or recreate it via "
                    "'+ Add target' below with the patient_id field filled."
                )
                ready = False

        if active is None:
            if st.button("▶ Run campaign", disabled=not ready, type="primary"):
                try:
                    pid, cid, expected = start_campaign(
                        Path(cases_dir), target_name=target["name"]
                    )
                    st.session_state["active_campaign"] = {
                        "pid": pid,
                        "campaign_id": str(cid),
                        "expected": expected,
                        "started_at": time.time(),
                        "target_name": target["name"],
                    }
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to launch: {e}")
        else:
            prog = campaign_progress(DATABASE_URL, UUID(active["campaign_id"]))
            count = int(prog["count"])
            expected = int(active["expected"])
            done = count >= expected or not pid_alive(int(active["pid"]))
            pct = min(count / expected, 1.0) if expected else 0.0
            st.progress(
                pct,
                text=(
                    f"Running on `{active.get('target_name', '?')}`… "
                    f"{count}/{expected} attacks "
                    f"(last verdict: {prog['last_verdict'] or '—'})"
                ),
            )
            if done:
                st.success(
                    f"✅ Campaign complete: `{active['campaign_id']}` "
                    f"({count} rows; last verdict: {prog['last_verdict']})"
                )
                st.session_state.pop("active_campaign", None)
                st.cache_data.clear()
            else:
                st.markdown(
                    '<meta http-equiv="refresh" content="5">', unsafe_allow_html=True
                )

with st.expander("➕ Add a new target", expanded=False):
    st.caption(
        "Targets are AI systems the platform attacks. Once added, they "
        "appear in the Target dropdown above. `target_type` determines how "
        "the chat client is built — see `agentforge_adversarial/target.py:make_client`."
    )
    with st.form("add_target_form", clear_on_submit=True):
        new_name = st.text_input("Name", placeholder="e.g. Staging Co-Pilot")
        new_type = st.selectbox(
            "Type", ["copilot", "generic_chat", "openai_compat"],
            help=(
                "copilot = OpenEMR Clinical Co-Pilot (needs patient_id). "
                "generic_chat = any HTTP/JSON endpoint with a prompt template. "
                "openai_compat = OpenAI Chat Completions wire format."
            ),
        )
        new_url = st.text_input(
            "URL",
            placeholder="https://api.example.com/v1/chat/completions",
        )
        new_notes = st.text_input("Notes (optional)", placeholder="")

        if new_type == "copilot":
            new_patient = st.text_input(
                "patient_id (Synthea UUID)",
                placeholder="0fe1a5d2-1234-5678-9abc-def012345678",
            )
            new_physician = st.text_input("physician_user_id", value="admin")
            new_config = {"patient_id": new_patient, "physician_user_id": new_physician}
        elif new_type == "openai_compat":
            new_api_key = st.text_input("API key (Bearer)", type="password")
            new_model = st.text_input("Model", value="gpt-4o-mini")
            new_config = {"api_key": new_api_key, "model": new_model}
        else:  # generic_chat
            new_template = st.text_area(
                "Request template (JSON, with {PROMPT} placeholder)",
                value='{"prompt": "{PROMPT}"}',
            )
            new_response_path = st.text_input(
                "Response path (dot-separated)", value="response"
            )
            try:
                import json as _j
                new_template_dict = _j.loads(new_template)
            except Exception:
                new_template_dict = {"prompt": "{PROMPT}"}
            new_config = {
                "request_template": new_template_dict,
                "response_path": new_response_path,
            }

        submitted = st.form_submit_button("Add target", type="primary")
        if submitted:
            if not new_name or not new_url:
                st.error("Name and URL are required.")
            else:
                try:
                    _insert_target(new_name, new_type, new_url, new_config, new_notes)
                    st.cache_data.clear()
                    st.success(f"Added target `{new_name}`. Reload to see it in the dropdown.")
                except Exception as e:
                    st.error(f"Failed to add target: {e}")

df = fetch_runs()
if df.empty:
    st.info(
        "No attack runs yet. Click **▶ Run campaign** above, or start one "
        "from the CLI: `python -m agentforge_adversarial run --cases evals/cases --mutate`"
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
