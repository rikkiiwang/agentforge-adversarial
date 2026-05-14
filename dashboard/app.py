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
    cancel_campaign,
    latest_phase_line,
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


# A Streamlit fragment is the modern replacement for a full-page meta-refresh:
# it re-runs ONLY the decorated function every `run_every` seconds, leaving
# the rest of the page untouched. This is why the rest of the dashboard no
# longer "blinks" / "resets" every 5 seconds.
@st.fragment(run_every=5)
def _render_progress_fragment() -> None:
    active = st.session_state.get("active_campaign")
    if not active:
        return
    prog = campaign_progress(DATABASE_URL, UUID(active["campaign_id"]))
    count = int(prog["count"])
    initial_expected = int(active["expected"])
    alive = pid_alive(int(active["pid"]))
    done = not alive
    pct = min(count / initial_expected, 1.0) if initial_expected else 0.0
    st.progress(
        pct,
        text=(
            f"Running on `{active.get('target_name', '?')}`… "
            f"{count} attacks landed "
            f"(≥ {initial_expected} expected from seeds + mutator; "
            f"more = class-probe fan-out)."
        ),
    )
    # Subprocess phase / latest verdict — explains the count to the operator.
    # During the 5–20 s startup or 5–10 s parallel mutator window, the row
    # count is stuck at 0 but the phase line shows the runner is alive
    # ("[graph:mutate] generating mutations for 8 seeds in parallel…").
    log_path = active.get("log_path")
    if log_path:
        phase = latest_phase_line(log_path)
        if phase:
            phase_display = phase if len(phase) <= 180 else phase[:177] + "…"
            st.code(phase_display, language="text")
    cancel_col, info_col = st.columns([1, 4])
    with cancel_col:
        if alive and st.button("⏹ Cancel", type="secondary", key="cancel_btn"):
            if cancel_campaign(int(active["pid"])):
                st.warning(
                    f"Cancel signal sent to PID {active['pid']}. "
                    f"{count} rows already in DB remain (cancellation "
                    "doesn't roll them back)."
                )
            else:
                st.info("Subprocess was already gone.")
            st.session_state.pop("active_campaign", None)
            st.cache_data.clear()
            st.rerun()
    with info_col:
        st.caption(
            f"campaign_id: `{active['campaign_id']}`  ·  "
            f"pid: `{active['pid']}`  ·  "
            f"target: `{active.get('target_name', '?')}`"
        )
    if done:
        st.success(
            f"✅ Campaign complete: `{active['campaign_id']}` "
            f"({count} total rows; last verdict: {prog['last_verdict']})"
        )
        st.session_state.pop("active_campaign", None)
        st.cache_data.clear()
        st.rerun()


targets = fetch_targets()
active = st.session_state.get("active_campaign")

with st.expander("🚀 Launch a campaign", expanded=True):
    if not targets:
        st.error(
            "No targets configured. Run `make init-db` to seed the default "
            "Co-Pilot target, then refresh."
        )
    else:
        names = [t["name"] for t in targets]
        # Streamlit preserves widget state across reruns when keyed.
        # Without a key, parameter changes (e.g. flipping `disabled`) cause
        # the widget's auto-generated key to change and its value to reset.
        if "target_picker" not in st.session_state:
            st.session_state["target_picker"] = names[0]
        if st.session_state["target_picker"] not in names:
            st.session_state["target_picker"] = names[0]
        sel = st.selectbox(
            "Target system",
            options=names,
            key="target_picker",
            disabled=active is not None,
            help="The AI system to attack. Add more via '+ Add target' below.",
        )
        target = targets[names.index(sel)]
        meta_bits = [f"type: `{target['target_type']}`",
                     f"url: `{target['target_url']}`"]
        if target["target_type"] == "copilot":
            pid_val = (target.get("config_json") or {}).get("patient_id") or "—"
            meta_bits.append(f"patient_id: `{pid_val[:12]}…`" if pid_val != "—" else "patient_id: `—`")
        st.caption(" · ".join(meta_bits))

        cases_dir = st.text_input(
            "Cases dir", "evals/cases", disabled=active is not None, key="cases_dir_input"
        )

        # --- Swarm config (operator-tunable knobs per campaign) ---
        st.markdown("**Swarm config**")
        st.caption(
            "Sources: `direct` = seed YAMLs, `random` = LLM-mutated variants, "
            "`class_probe` = boundary variants generated on FAIL."
        )
        swarm_col1, swarm_col2, swarm_col3 = st.columns(3)
        with swarm_col1:
            sw_variants = st.slider(
                "Mutations per seed",
                min_value=0,
                max_value=10,
                value=3,
                disabled=active is not None,
                key="sw_variants",
                help=(
                    "How many `random` variants the LLM mutator produces per "
                    "seed in round 0 (also used by PARTIAL re-entry). "
                    "0 = seeds only (no mutator). Default 3."
                ),
            )
        with swarm_col2:
            sw_max_rounds = st.slider(
                "Class-probe rounds",
                min_value=0,
                max_value=3,
                value=2,
                disabled=active is not None,
                key="sw_max_rounds",
                help=(
                    "Maximum LangGraph rounds after the initial dispatch. "
                    "Round 0 = seeds + mutator. Round 1+ = PARTIAL re-entry "
                    "and FAIL → class-probe fan-out. 0 disables fan-out."
                ),
            )
        with swarm_col3:
            sw_mutator_model = st.selectbox(
                "Mutator + class-probe model",
                options=["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
                index=0,
                disabled=active is not None,
                key="sw_mutator_model",
                help="OpenAI model used by the Red Team mutator + class-probe subagents.",
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

        # --- Approval gate (per docs/components/dashboard.md §4.1) ---
        # When "Review before dispatch" is on, clicking Run does NOT immediately
        # spawn the subprocess. Instead the dashboard shows the proposed swarm
        # spec + three buttons: Accept (dispatch as configured), Modify (loop
        # back to the picker), Override (paste a JSON spec to dispatch). The
        # spec written to the campaign's `notes` column for audit.
        sw_review = st.checkbox(
            "Review before dispatch (Approve / Modify / Override gate)",
            value=False,
            disabled=active is not None,
            key="sw_review",
            help=(
                "When on: clicking Run shows the proposed swarm spec and "
                "asks for explicit Accept / Modify / Override before "
                "spawning the subprocess. Off = auto-dispatch."
            ),
        )

        # Double-spawn protection: set a "launch in flight" flag on click,
        # before the 20s subprocess parse blocks. Streamlit reruns the script
        # on every interaction; if a stray rerun fires during that window,
        # the second click sees this flag and skips spawning.
        launch_in_flight = st.session_state.get("launch_in_flight", False)
        pending_review = st.session_state.get("pending_review")

        def _dispatch(variants: int, rounds: int, model: str) -> None:
            st.session_state["launch_in_flight"] = True
            try:
                with st.spinner(
                    "Starting campaign (subprocess + LangGraph init "
                    "takes ~20 s before progress appears)…"
                ):
                    pid, cid, expected, log_path = start_campaign(
                        Path(cases_dir),
                        target_name=target["name"],
                        mutations_per_seed=variants,
                        max_rounds=rounds,
                        mutator_model=model,
                    )
                st.session_state["active_campaign"] = {
                    "pid": pid,
                    "campaign_id": str(cid),
                    "expected": expected,
                    "log_path": log_path,
                    "started_at": time.time(),
                    "target_name": target["name"],
                }
                st.session_state["launch_in_flight"] = False
                st.session_state.pop("pending_review", None)
                st.rerun()
            except Exception as e:
                st.session_state["launch_in_flight"] = False
                st.session_state.pop("pending_review", None)
                st.error(f"Failed to launch: {e}")

        if active is None and pending_review is None:
            if st.button(
                "▶ Run campaign",
                disabled=not ready or launch_in_flight,
                type="primary",
            ):
                if sw_review:
                    st.session_state["pending_review"] = {
                        "target": target["name"],
                        "cases_dir": cases_dir,
                        "mutations_per_seed": sw_variants,
                        "max_rounds": sw_max_rounds,
                        "mutator_model": sw_mutator_model,
                    }
                    st.rerun()
                else:
                    _dispatch(sw_variants, sw_max_rounds, sw_mutator_model)
        elif pending_review is not None:
            st.info("**Review the proposed swarm spec before dispatch:**")
            st.code(
                f"target:              {pending_review['target']}\n"
                f"cases_dir:           {pending_review['cases_dir']}\n"
                f"mutations_per_seed:  {pending_review['mutations_per_seed']}\n"
                f"max_rounds:          {pending_review['max_rounds']}\n"
                f"mutator_model:       {pending_review['mutator_model']}",
                language="yaml",
            )
            accept_col, modify_col, override_col = st.columns(3)
            with accept_col:
                if st.button("✅ Accept", type="primary", key="approve_accept"):
                    _dispatch(
                        pending_review["mutations_per_seed"],
                        pending_review["max_rounds"],
                        pending_review["mutator_model"],
                    )
            with modify_col:
                if st.button("✏️ Modify (back to picker)", key="approve_modify"):
                    st.session_state.pop("pending_review", None)
                    st.rerun()
            with override_col:
                override_json = st.text_area(
                    "Override (paste swarm-spec JSON)",
                    placeholder='{"mutations_per_seed": 5, "max_rounds": 1, "mutator_model": "gpt-4o"}',
                    height=80,
                    key="approve_override_json",
                )
                if st.button("🚀 Override + dispatch", key="approve_override"):
                    import json as _j
                    try:
                        spec = _j.loads(override_json)
                        _dispatch(
                            int(spec.get("mutations_per_seed", 3)),
                            int(spec.get("max_rounds", 2)),
                            str(spec.get("mutator_model", "gpt-4o-mini")),
                        )
                    except Exception as e:
                        st.error(f"Invalid override JSON: {e}")
        else:
            _render_progress_fragment()

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
        "from the CLI: `python -m agentforge_adversarial run --cases evals/cases`"
    )
    st.stop()

# --- Campaign selector ---
# Every Run-click writes one row to `campaigns` and N rows to `attack_runs`
# linked via `campaign_id`. The selector scopes every view below.
campaign_options = (
    df[["campaign_id", "campaign_name", "created_at"]]
    .drop_duplicates(subset=["campaign_id"])
    .sort_values("created_at", ascending=False)
)
campaign_labels = {
    "__ALL__": f"All campaigns ({len(campaign_options)} total)",
}
for _, c in campaign_options.iterrows():
    ts = pd.to_datetime(c["created_at"]).strftime("%Y-%m-%d %H:%M")
    rows_in_c = int((df["campaign_id"] == c["campaign_id"]).sum())
    campaign_labels[c["campaign_id"]] = (
        f"{ts}  ·  {c['campaign_name']}  ·  {rows_in_c} runs  ·  {c['campaign_id'][:8]}…"
    )

st.subheader("Campaign")
st.caption(
    "Each Run-click creates a `campaigns` row + N `attack_runs` rows. "
    "Switch campaigns here — every view below (KPIs, heat-map, table, "
    "drill-down) is scoped to your selection."
)
chosen_campaign = st.selectbox(
    "Campaign",
    options=list(campaign_labels.keys()),
    format_func=lambda k: campaign_labels[k],
    label_visibility="collapsed",
)
if chosen_campaign != "__ALL__":
    df = df[df["campaign_id"] == chosen_campaign].reset_index(drop=True)

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


# --- Vulnerability Board (P1) ---
# Per docs/components/dashboard.md §4.2. The Documentation Agent writes one
# vulnerabilities row + one vuln_reports row on every FAIL. Here we surface
# those rows with state-transition controls (discovered → triaged).
@st.cache_data(ttl=10)
def fetch_vulns(campaign_id: str | None) -> pd.DataFrame:
    query = """
        SELECT
          v.id::text AS vuln_id,
          v.attack_run_id::text AS attack_run_id,
          v.campaign_id::text AS campaign_id,
          v.category, v.subcategory,
          v.severity::text AS severity,
          v.state::text AS state,
          v.parent_vuln_id::text AS parent_vuln_id,
          v.target_version,
          v.created_at,
          vr.severity_rationale,
          vr.observed_vs_expected,
          vr.repro_steps,
          vr.suggested_fix,
          vr.defense_reference,
          ar.attack_prompt,
          ar.observed_output,
          ar.case_id
        FROM vulnerabilities v
        JOIN vuln_reports vr ON vr.vuln_id = v.id
        JOIN attack_runs ar ON ar.id = v.attack_run_id
    """
    params: tuple = ()
    if campaign_id and campaign_id != "__ALL__":
        query += " WHERE v.campaign_id = %s"
        params = (campaign_id,)
    query += " ORDER BY v.severity DESC, v.created_at DESC"
    with psycopg.connect(DATABASE_URL) as conn:
        return pd.read_sql_query(query, conn, params=params)


def _transition_vuln_state(vuln_id: str, new_state: str, actor: str = "operator") -> None:
    extra = ""
    if new_state == "triaged":
        extra = ", triaged_at = now(), triaged_by = %s"
    elif new_state == "closed":
        extra = ", closed_at = now(), closed_by = %s"
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        if extra:
            cur.execute(
                f"UPDATE vulnerabilities SET state = %s::vuln_state, updated_at = now(){extra} WHERE id = %s",
                (new_state, actor, vuln_id),
            )
        else:
            cur.execute(
                "UPDATE vulnerabilities SET state = %s::vuln_state, updated_at = now() WHERE id = %s",
                (new_state, vuln_id),
            )
        conn.commit()


st.subheader("🛡️ Vulnerability Board")
st.caption(
    "Each FAIL'd attack becomes one `vulnerabilities` row + one "
    "`vuln_reports` row written by the Documentation Agent. Severity is "
    "weighted by clinical-safety impact. State-transition buttons drive "
    "the triage workflow per `docs/components/dashboard.md §4.2`."
)
vulns_df = fetch_vulns(chosen_campaign)
if vulns_df.empty:
    st.info(
        "No vulnerabilities recorded yet. They appear here automatically "
        "whenever the Judge marks an attack FAIL."
    )
else:
    sev_counts = vulns_df["severity"].value_counts()
    state_counts = vulns_df["state"].value_counts()
    vk1, vk2, vk3, vk4, vk5 = st.columns(5)
    vk1.metric("Total vulns", int(len(vulns_df)))
    vk2.metric("Critical", int(sev_counts.get("critical", 0)))
    vk3.metric("High",     int(sev_counts.get("high", 0)))
    vk4.metric("Discovered (pending review)", int(state_counts.get("discovered", 0)))
    vk5.metric("Triaged",  int(state_counts.get("triaged", 0)))

    sev_filter = st.multiselect(
        "Severity",
        ["critical", "high", "medium", "low"],
        default=["critical", "high", "medium", "low"],
        key="vuln_sev_filter",
    )
    state_filter = st.multiselect(
        "State",
        ["discovered", "triaged", "fix_proposed", "fix_validated", "reopened", "closed"],
        default=["discovered", "triaged", "fix_proposed", "reopened"],
        key="vuln_state_filter",
    )
    vuln_view = vulns_df[
        vulns_df["severity"].isin(sev_filter) & vulns_df["state"].isin(state_filter)
    ]

    st.dataframe(
        vuln_view[
            ["case_id", "category", "subcategory", "severity", "state",
             "target_version", "created_at"]
        ],
        use_container_width=True,
        hide_index=True,
    )

    if not vuln_view.empty:
        st.markdown("**Inspect a vulnerability**")
        vuln_ids = vuln_view["vuln_id"].tolist()

        def _vuln_label(vid: str) -> str:
            row = vuln_view.loc[vuln_view["vuln_id"] == vid].iloc[0]
            return f"[{row['severity'].upper()}] {row['case_id']}  ·  {row['category']}  ·  state={row['state']}"

        chosen_vuln = st.selectbox(
            "Vulnerability",
            options=vuln_ids,
            format_func=_vuln_label,
            label_visibility="collapsed",
            key="vuln_inspector",
        )
        v = vuln_view.loc[vuln_view["vuln_id"] == chosen_vuln].iloc[0]

        meta_l, meta_r = st.columns(2)
        with meta_l:
            st.markdown(f"**Severity:** `{v['severity']}`")
            st.markdown(f"**State:** `{v['state']}`")
            st.markdown(f"**Category:** `{v['category']}` / `{v['subcategory']}`")
        with meta_r:
            st.markdown(f"**vuln_id:** `{v['vuln_id']}`")
            st.markdown(f"**attack_run_id:** `{v['attack_run_id']}`")
            if v.get("parent_vuln_id"):
                st.markdown(f"**Parent vuln:** `{v['parent_vuln_id'][:8]}…` (class-probe lineage)")

        st.markdown("**Severity rationale:**")
        st.info(v["severity_rationale"])

        st.markdown("**Observed vs expected:**")
        st.code(v["observed_vs_expected"], language="text")

        st.markdown("**Repro steps:**")
        st.code(v["repro_steps"], language="text")

        st.markdown("**Suggested fix:**")
        st.success(v["suggested_fix"])
        if v.get("defense_reference"):
            st.caption(f"Defense reference: `{v['defense_reference']}`")

        # --- State transitions (§4.2) ---
        st.markdown("**State transitions**")
        cur_state = v["state"]
        tcol1, tcol2, tcol3 = st.columns(3)
        with tcol1:
            if cur_state == "discovered" and st.button(
                "→ Triage", key=f"triage_{chosen_vuln}", type="primary"
            ):
                _transition_vuln_state(chosen_vuln, "triaged")
                st.cache_data.clear()
                st.rerun()
        with tcol2:
            if cur_state == "triaged" and st.button(
                "→ Fix proposed", key=f"fixprop_{chosen_vuln}"
            ):
                _transition_vuln_state(chosen_vuln, "fix_proposed")
                st.cache_data.clear()
                st.rerun()
        with tcol3:
            if cur_state in ("discovered", "triaged", "fix_proposed") and st.button(
                "→ Close", key=f"close_{chosen_vuln}"
            ):
                _transition_vuln_state(chosen_vuln, "closed")
                st.cache_data.clear()
                st.rerun()
        if cur_state == "fix_validated":
            st.caption(
                "✅ This vuln has been fix-validated by the regression harness. "
                "(Regression harness writes this state; not operator-clickable.)"
            )
        if cur_state == "closed":
            st.caption("This vuln is closed. Re-opening requires a new FAIL from the regression harness.")

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
