from __future__ import annotations

import os
import time
import warnings
from pathlib import Path
from uuid import UUID

# pandas emits a UserWarning on every read_sql_query call when the
# connection is a raw psycopg/asyncpg object rather than a SQLAlchemy
# engine. Functionally the queries work — we just don't get the
# SQLAlchemy type-mapping niceties we don't need. Migrating the whole
# dashboard to SQLAlchemy for that warning is overkill, so silence it
# at the source instead.
warnings.filterwarnings(
    "ignore",
    message="pandas only supports SQLAlchemy connectable",
    category=UserWarning,
)

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


# ─────────────────────────────────────────────────────────────────────────
# Data fetchers (campaign-scoped queries — push the filter to Postgres
# instead of pulling all runs and filtering in pandas).
# ─────────────────────────────────────────────────────────────────────────


@st.cache_data(ttl=10)
def fetch_campaigns() -> pd.DataFrame:
    """List of campaigns for the sidebar selector. Tiny + fast."""
    with psycopg.connect(DATABASE_URL) as conn:
        return pd.read_sql_query(
            """
            SELECT c.id::text AS campaign_id, c.name AS campaign_name,
                   c.created_at,
                   COALESCE(c.total_cost_usd, 0)   AS total_cost_usd,
                   COALESCE(c.total_tokens_in, 0)  AS total_tokens_in,
                   COALESCE(c.total_tokens_out, 0) AS total_tokens_out,
                   (SELECT count(*) FROM attack_runs ar
                      WHERE ar.campaign_id = c.id) AS run_count
              FROM campaigns c
             ORDER BY c.created_at DESC
            """,
            conn,
        )


@st.cache_data(ttl=10)
def fetch_runs(campaign_id: str | None) -> pd.DataFrame:
    query = """
        SELECT
          ar.id::text AS id, ar.created_at, ar.case_id,
          ar.category, ar.subcategory, ar.source,
          ar.red_team_subagent_id, ar.red_team_model,
          ar.judge_verdict, ar.judge_reasoning, ar.judge_rubric_version,
          ar.target_version, ar.latency_ms,
          ar.attack_prompt, ar.observed_output,
          c.name AS campaign_name, c.id::text AS campaign_id
        FROM attack_runs ar
        JOIN campaigns c ON c.id = ar.campaign_id
    """
    params: tuple = ()
    if campaign_id and campaign_id != "__ALL__":
        query += " WHERE ar.campaign_id = %s"
        params = (campaign_id,)
    query += " ORDER BY ar.created_at DESC"
    with psycopg.connect(DATABASE_URL) as conn:
        return pd.read_sql_query(query, conn, params=params)


@st.cache_data(ttl=5)
def fetch_targets() -> list[dict]:
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, name, target_type, target_url, config_json, notes "
            "FROM targets ORDER BY created_at ASC"
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


@st.cache_data(ttl=10)
def fetch_vulns(campaign_id: str | None) -> pd.DataFrame:
    query = """
        SELECT
          v.id::text AS vuln_id,
          v.attack_run_id::text AS attack_run_id,
          v.campaign_id::text AS campaign_id,
          v.category, v.subcategory,
          v.severity::text AS severity, v.state::text AS state,
          v.parent_vuln_id::text AS parent_vuln_id,
          v.target_version, v.created_at,
          vr.severity_rationale, vr.observed_vs_expected,
          vr.repro_steps, vr.suggested_fix, vr.defense_reference,
          ar.attack_prompt, ar.observed_output, ar.case_id
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


def _insert_target(name, target_type, target_url, config, notes) -> None:
    import json as _json
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO targets (name, target_type, target_url, config_json, notes) "
            "VALUES (%s, %s, %s, %s::jsonb, %s)",
            (name, target_type, target_url, _json.dumps(config or {}), notes),
        )
        conn.commit()


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


# ─────────────────────────────────────────────────────────────────────────
# Page chrome
# ─────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="AI Security Platform",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _format_cost(usd: float) -> str:
    """Pick a sensible precision for the cost tile.

    `$0.0000` looks broken even though it's technically correct; show
    something readable at every magnitude instead:
      - $0       when the buffer hasn't received any usage yet
      - 0.XX¢    for sub-cent costs (1¢ = $0.01)
      - $0.XX    for cents-to-dollars
      - $X.XX    for ≥ $1
    """
    if usd <= 0:
        return "$0"
    if usd < 0.01:
        return f"{usd * 100:.2f}¢"
    if usd < 1:
        return f"${usd:.3f}"
    return f"${usd:.2f}"


# ─────────────────────────────────────────────────────────────────────────
# Sidebar — persistent campaign scope + live KPIs + active-campaign panel
# ─────────────────────────────────────────────────────────────────────────

campaigns_df = fetch_campaigns()

with st.sidebar:
    st.markdown("### 🛡️ AI Security Platform")
    st.caption(f"AgentForge Adversarial · DB: **{DEPLOY_ENV}**")
    st.divider()

    # --- Campaign scope ---
    st.markdown("#### Campaign")
    if campaigns_df.empty:
        st.info("No campaigns yet.")
        st.session_state["scope_campaign_id"] = "__ALL__"
    else:
        labels = {"__ALL__": f"All ({len(campaigns_df)} total)"}
        for _, c in campaigns_df.iterrows():
            ts = pd.to_datetime(c["created_at"]).strftime("%m-%d %H:%M")
            labels[c["campaign_id"]] = (
                f"{ts} · {int(c['run_count'])} runs · {c['campaign_id'][:8]}…"
            )
        # Default to latest campaign so the dashboard lands on real data.
        default_key = (
            st.session_state.get("scope_campaign_id")
            or campaigns_df["campaign_id"].iloc[0]
        )
        if default_key not in labels:
            default_key = campaigns_df["campaign_id"].iloc[0]
        chosen = st.selectbox(
            "Scope",
            options=list(labels.keys()),
            format_func=lambda k: labels[k],
            index=list(labels.keys()).index(default_key),
            key="sidebar_campaign_picker",
            label_visibility="collapsed",
        )
        st.session_state["scope_campaign_id"] = chosen

    scope_campaign_id = st.session_state.get("scope_campaign_id", "__ALL__")

    # --- Live KPIs (campaign-scoped, refreshes with the page) ---
    st.markdown("#### Live KPIs")
    _scoped_df = fetch_runs(scope_campaign_id)
    if _scoped_df.empty:
        st.caption("_no runs in scope yet_")
    else:
        total = len(_scoped_df)
        fails = int((_scoped_df["judge_verdict"] == "fail").sum())
        partials = int((_scoped_df["judge_verdict"] == "partial").sum())
        passes = int((_scoped_df["judge_verdict"] == "pass").sum())
        st.metric("Total runs", total)
        kc1, kc2, kc3 = st.columns(3)
        kc1.metric("FAIL", fails, delta=None, delta_color="inverse")
        kc2.metric("PARTIAL", partials)
        kc3.metric("PASS", passes)

        # Cost rollup (P2). When scoped to a single campaign, show its row;
        # when scoped to __ALL__, sum across campaigns.
        if scope_campaign_id == "__ALL__":
            cost_total = float(campaigns_df["total_cost_usd"].sum())
            tokens_in = int(campaigns_df["total_tokens_in"].sum())
            tokens_out = int(campaigns_df["total_tokens_out"].sum())
        else:
            crow = campaigns_df[campaigns_df["campaign_id"] == scope_campaign_id]
            cost_total = float(crow["total_cost_usd"].iloc[0]) if not crow.empty else 0.0
            tokens_in = int(crow["total_tokens_in"].iloc[0]) if not crow.empty else 0
            tokens_out = int(crow["total_tokens_out"].iloc[0]) if not crow.empty else 0
        st.metric(
            "Red-team LLM cost",
            _format_cost(cost_total),
            help=(
                "Cost of OpenAI calls for Judge + mutator + class-probe + "
                "partial-reentry. Target's own LLM bill is not visible to "
                "us (black box). Written by `cost.flush_to_db()` at "
                "campaign end."
            ),
        )
        st.caption(
            f"tokens · in **{tokens_in:,}** · out **{tokens_out:,}**"
        )

    st.divider()

    # --- Active campaign progress fragment ---
    # A Streamlit fragment re-runs ONLY this block every `run_every` seconds,
    # leaving the rest of the page (tabs, charts) untouched.
    @st.fragment(run_every=5)
    def _render_progress_fragment() -> None:
        active = st.session_state.get("active_campaign")
        if not active:
            return
        st.markdown("#### ▶ Active campaign")
        prog = campaign_progress(DATABASE_URL, UUID(active["campaign_id"]))
        count = int(prog["count"])
        initial_expected = int(active["expected"])
        alive = pid_alive(int(active["pid"]))
        done = not alive
        pct = min(count / initial_expected, 1.0) if initial_expected else 0.0
        st.progress(
            pct,
            text=f"{count} / ≥ {initial_expected} attacks landed",
        )
        log_path = active.get("log_path")
        if log_path:
            phase = latest_phase_line(log_path)
            if phase:
                phase_short = phase if len(phase) <= 90 else phase[:87] + "…"
                st.caption(f"_{phase_short}_")
        st.caption(
            f"target: `{active.get('target_name', '?')}`\n"
            f"pid: `{active['pid']}`"
        )
        if alive and st.button("⏹ Cancel", type="secondary", key="cancel_btn",
                               width="stretch"):
            if cancel_campaign(int(active["pid"])):
                st.warning(f"Cancel signal sent. {count} rows preserved.")
            else:
                st.info("Subprocess was already gone.")
            st.session_state.pop("active_campaign", None)
            st.cache_data.clear()
            st.rerun()
        if done:
            st.success(f"✅ Done · last verdict: {prog['last_verdict']}")
            st.session_state.pop("active_campaign", None)
            st.cache_data.clear()
            st.rerun()

    _render_progress_fragment()

    if st.session_state.get("active_campaign"):
        st.divider()

    # --- Quick links ---
    with st.expander("📦 Targets", expanded=False):
        _tgts = fetch_targets()
        if not _tgts:
            st.caption("None configured.")
        else:
            for t in _tgts:
                st.markdown(f"• `{t['name']}` _({t['target_type']})_")
        st.caption("Add new ones from the 🚀 Launch tab.")


# ─────────────────────────────────────────────────────────────────────────
# Main area — tab content swap. Coverage-first (reviewer workflow).
# ─────────────────────────────────────────────────────────────────────────

tab_coverage, tab_vulns, tab_runs, tab_launch = st.tabs([
    "📊 Coverage",
    "🛡️ Vulnerabilities",
    "🎯 Attack runs",
    "🚀 Launch",
])

# Shared data for the three data tabs.
df = fetch_runs(scope_campaign_id)


# ─────────────────────────────────────────────────────────────────────────
# Tab 1 — Coverage heat-map
# ─────────────────────────────────────────────────────────────────────────

with tab_coverage:
    st.subheader("Coverage heat-map · category × verdict")
    st.caption(
        "Each cell is the count of attack runs at that category × verdict cross. "
        "Darker = more runs. FAIL columns are the actionable signal."
    )
    if df.empty:
        st.info(
            "No attack runs in scope. Start one from the **🚀 Launch** tab, "
            "or run `python -m agentforge_adversarial run --cases evals/cases` "
            "from the CLI."
        )
    else:
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
                    scale=alt.Scale(
                        domain=[0, max(1, int(heat_long["count"].max()))],
                        range=[0.15, 1.0],
                    ),
                    legend=None,
                ),
                tooltip=["category", "verdict", "count"],
            )
            .properties(height=max(180, 44 * heat.shape[0]))
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
        st.altair_chart(heat_chart + heat_labels, width="stretch")

        # Lineage breakdown by source — surfaces the 4 source channels visually.
        st.markdown("##### Sources of attack runs")
        st.caption(
            "`direct` = seed YAMLs · `random` = mutator variants · "
            "`class_probe` = FAIL fan-out · `partial_reentry` = PARTIAL fresh phrasings"
        )
        source_counts = (
            df.groupby(["source", "judge_verdict"]).size().reset_index(name="count")
        )
        source_chart = (
            alt.Chart(source_counts)
            .mark_bar()
            .encode(
                x=alt.X("source:N", axis=alt.Axis(labelAngle=0, title=None)),
                y=alt.Y("count:Q", title="runs"),
                color=alt.Color(
                    "judge_verdict:N",
                    sort=verdict_order,
                    scale=alt.Scale(
                        domain=verdict_order,
                        range=[verdict_colors[v] for v in verdict_order],
                    ),
                    legend=alt.Legend(title="verdict"),
                ),
                tooltip=["source", "judge_verdict", "count"],
            )
            .properties(height=180)
        )
        st.altair_chart(source_chart, width="stretch")


# ─────────────────────────────────────────────────────────────────────────
# Tab 2 — Vulnerability Board
# ─────────────────────────────────────────────────────────────────────────

with tab_vulns:
    st.subheader("🛡️ Vulnerability Board")
    st.caption(
        "Each FAIL'd attack becomes one `vulnerabilities` row + one "
        "`vuln_reports` row written by the Documentation Agent. Severity is "
        "weighted by clinical-safety impact. State-transition buttons drive "
        "the triage workflow per `docs/components/dashboard.md §4.2`."
    )
    vulns_df = fetch_vulns(scope_campaign_id)
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
        vk4.metric("Discovered (pending)", int(state_counts.get("discovered", 0)))
        vk5.metric("Triaged",  int(state_counts.get("triaged", 0)))

        fcol_a, fcol_b = st.columns(2)
        with fcol_a:
            sev_filter = st.multiselect(
                "Severity",
                ["critical", "high", "medium", "low"],
                default=["critical", "high", "medium", "low"],
                key="vuln_sev_filter",
            )
        with fcol_b:
            state_filter = st.multiselect(
                "State",
                ["discovered", "triaged", "fix_proposed", "fix_validated",
                 "reopened", "closed"],
                default=["discovered", "triaged", "fix_proposed", "reopened"],
                key="vuln_state_filter",
            )
        vuln_view = vulns_df[
            vulns_df["severity"].isin(sev_filter)
            & vulns_df["state"].isin(state_filter)
        ]

        st.dataframe(
            vuln_view[
                ["case_id", "category", "subcategory", "severity", "state",
                 "target_version", "created_at"]
            ],
            width="stretch",
            hide_index=True,
        )

        if not vuln_view.empty:
            with st.expander("🔍 Inspect a vulnerability", expanded=True):
                vuln_ids = vuln_view["vuln_id"].tolist()

                def _vuln_label(vid: str) -> str:
                    row = vuln_view.loc[vuln_view["vuln_id"] == vid].iloc[0]
                    return (
                        f"[{row['severity'].upper()}] {row['case_id']}  ·  "
                        f"{row['category']}  ·  state={row['state']}"
                    )

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
                    st.markdown(
                        f"**Category:** `{v['category']}` / `{v['subcategory']}`"
                    )
                with meta_r:
                    st.markdown(f"**vuln_id:** `{v['vuln_id']}`")
                    st.markdown(f"**attack_run_id:** `{v['attack_run_id']}`")
                    if v.get("parent_vuln_id"):
                        st.markdown(
                            f"**Parent vuln:** `{v['parent_vuln_id'][:8]}…` "
                            "(class-probe lineage)"
                        )

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
                    if cur_state in ("discovered", "triaged", "fix_proposed") \
                       and st.button("→ Close", key=f"close_{chosen_vuln}"):
                        _transition_vuln_state(chosen_vuln, "closed")
                        st.cache_data.clear()
                        st.rerun()
                if cur_state == "fix_validated":
                    st.caption(
                        "✅ Fix-validated by the regression harness. "
                        "(Not operator-clickable.)"
                    )
                if cur_state == "closed":
                    st.caption(
                        "This vuln is closed. Re-opening requires a new "
                        "FAIL from the regression harness."
                    )

                # --- Regression replay (P2) ---
                # Dispatches the *original* attack_prompt against the current
                # target. PASS → fix_validated. FAIL → reopened. PARTIAL →
                # no state change. See `agentforge_adversarial/regression.py`.
                st.divider()
                st.markdown("**Regression replay**")
                replay_help = (
                    "Replay this vulnerability's original attack against the "
                    "current target version. Use this after deploying a "
                    "candidate fix. The Judge's verdict drives state "
                    "transitions (PASS → fix_validated, FAIL → reopened, "
                    "PARTIAL → no change)."
                )
                if cur_state == "discovered":
                    st.caption(
                        "🔁 Replay disabled while state=discovered. "
                        "Triage first."
                    )
                else:
                    if st.button(
                        "🔁 Replay against current target",
                        key=f"replay_{chosen_vuln}",
                        help=replay_help,
                    ):
                        import asyncio as _asyncio

                        from agentforge_adversarial.config import Config
                        from agentforge_adversarial.db import connection
                        from agentforge_adversarial.regression import (
                            replay_vulnerability,
                        )
                        from agentforge_adversarial.target import make_client
                        from agentforge_adversarial.targets import (
                            get_default_target,
                            get_target_by_name,
                        )

                        async def _replay_one() -> dict:
                            cfg = Config.from_env()
                            from openai import AsyncOpenAI
                            oc = (
                                AsyncOpenAI(api_key=cfg.openai_api_key)
                                if cfg.openai_api_key else None
                            )
                            async with connection(cfg) as _conn:
                                # Reload vuln + attack_run from DB to get
                                # canonical attack_prompt (the dashboard df
                                # may be stale).
                                row = await _conn.fetchrow(
                                    """
                                    SELECT v.id AS vuln_id,
                                           v.attack_run_id, v.state::text AS state,
                                           v.category, v.subcategory,
                                           v.target_version AS original_target_version,
                                           ar.case_id, ar.attack_prompt,
                                           ar.expected_failure_mode
                                      FROM vulnerabilities v
                                      JOIN attack_runs ar ON ar.id = v.attack_run_id
                                     WHERE v.id = $1
                                    """,
                                    UUID(chosen_vuln),
                                )
                                if row is None:
                                    return {"error": "vuln not found"}
                                # Default target = whatever's in the targets table.
                                target_row = await get_default_target(_conn)
                            if target_row is None:
                                return {"error": "no default target"}
                            chat_client = make_client(target_row)
                            new_target_version = (
                                f"{target_row['target_type']}:"
                                f"{target_row['target_url']}"
                            )
                            async with connection(cfg) as _conn:
                                res = await replay_vulnerability(
                                    _conn, dict(row),
                                    chat_client=chat_client,
                                    openai_client=oc,
                                    new_target_version=new_target_version,
                                )
                            return {
                                "verdict": res.new_verdict,
                                "state": res.new_state,
                                "observed": res.observed_output_truncated,
                                "new_target_version": new_target_version,
                            }

                        with st.spinner("Dispatching replay against current target…"):
                            outcome = _asyncio.run(_replay_one())
                        if "error" in outcome:
                            st.error(f"Replay failed: {outcome['error']}")
                        else:
                            verdict_color = {
                                "pass": "🟢", "fail": "🔴", "partial": "🟡",
                            }.get(outcome["verdict"], "⚪")
                            st.success(
                                f"Replay verdict: {verdict_color} "
                                f"**{outcome['verdict'].upper()}** → "
                                f"new state: `{outcome['state']}`"
                            )
                            st.caption(
                                f"target: `{outcome['new_target_version']}`"
                            )
                            with st.expander("Observed output (truncated)"):
                                st.code(outcome["observed"], language="text")
                            st.cache_data.clear()
                            # Don't auto-rerun — let the operator read the
                            # verdict first, then refresh to see the new state.


# ─────────────────────────────────────────────────────────────────────────
# Tab 3 — Attack runs (filters + table + drill-down)
# ─────────────────────────────────────────────────────────────────────────

with tab_runs:
    st.subheader("Attack runs")
    if df.empty:
        st.info("No attack runs in scope.")
    else:
        fcol1, fcol2, fcol3 = st.columns(3)
        with fcol1:
            cats = st.multiselect(
                "Category",
                sorted(df["category"].unique()),
                default=list(sorted(df["category"].unique())),
                key="runs_cat_filter",
            )
        with fcol2:
            verdicts = st.multiselect(
                "Verdict",
                ["pass", "partial", "fail"],
                default=["pass", "partial", "fail"],
                key="runs_verdict_filter",
            )
        with fcol3:
            sources = st.multiselect(
                "Source",
                sorted(df["source"].unique()),
                default=list(sorted(df["source"].unique())),
                key="runs_source_filter",
            )

        mask = (
            df["category"].isin(cats)
            & df["judge_verdict"].isin(verdicts)
            & df["source"].isin(sources)
        )
        filtered = df[mask]

        st.caption(f"{len(filtered)} of {len(df)} runs match filters")
        st.dataframe(
            filtered[
                ["case_id", "category", "subcategory", "source",
                 "red_team_subagent_id", "judge_verdict",
                 "judge_rubric_version", "latency_ms"]
            ],
            width="stretch",
            hide_index=True,
        )

        with st.expander("🔍 Inspect a single attack run", expanded=True):
            st.caption(
                "Pick a row to see the full attack prompt, the target's "
                "observed output, and the Judge's reasoning."
            )
            if filtered.empty:
                st.info("No runs match the active filters.")
            else:
                ids = filtered["id"].tolist()

                def _label(run_id: str) -> str:
                    row = filtered.loc[filtered["id"] == run_id].iloc[0]
                    return (
                        f"{row['case_id']}  —  {row['category']}  —  "
                        f"{row['judge_verdict']}"
                    )

                case_choice = st.selectbox(
                    "Attack run",
                    options=ids,
                    format_func=_label,
                    label_visibility="collapsed",
                    key="runs_inspector",
                )
                row = filtered.loc[filtered["id"] == case_choice].iloc[0]

                meta_l, meta_r = st.columns(2)
                with meta_l:
                    st.markdown(f"**Verdict:** `{row['judge_verdict']}`")
                    st.markdown(f"**Rubric:** `{row['judge_rubric_version']}`")
                    st.markdown(f"**Source:** `{row['source']}`")
                with meta_r:
                    st.markdown(
                        f"**Red Team subagent:** `{row['red_team_subagent_id']}`"
                    )
                    st.markdown(f"**Red Team model:** `{row['red_team_model']}`")
                    st.markdown(f"**Latency:** {int(row['latency_ms'])} ms")

                st.markdown("**Judge reasoning:**")
                st.info(row["judge_reasoning"])

                st.markdown("**Attack prompt:**")
                st.code(row["attack_prompt"], language="text")

                st.markdown("**Observed output:**")
                st.code(row["observed_output"], language="text")


# ─────────────────────────────────────────────────────────────────────────
# Tab 4 — Launch panel (operator console)
# ─────────────────────────────────────────────────────────────────────────

with tab_launch:
    st.subheader("🚀 Launch a campaign")
    st.caption(
        "Operator console · Campaign-approval flow per "
        "`docs/components/dashboard.md §4.1`. Watch progress in the sidebar."
    )

    targets = fetch_targets()
    active = st.session_state.get("active_campaign")

    if not targets:
        st.error(
            "No targets configured. Run `make init-db` to seed the default "
            "Co-Pilot target, then refresh."
        )
    else:
        names = [t["name"] for t in targets]
        if "target_picker" not in st.session_state:
            st.session_state["target_picker"] = names[0]
        if st.session_state["target_picker"] not in names:
            st.session_state["target_picker"] = names[0]
        sel = st.selectbox(
            "Target system",
            options=names,
            key="target_picker",
            disabled=active is not None,
            help="The AI system to attack. Add more from the form below.",
        )
        target = targets[names.index(sel)]
        meta_bits = [
            f"type: `{target['target_type']}`",
            f"url: `{target['target_url']}`",
        ]
        if target["target_type"] == "copilot":
            pid_val = (target.get("config_json") or {}).get("patient_id") or "—"
            meta_bits.append(
                f"patient_id: `{pid_val[:12]}…`" if pid_val != "—"
                else "patient_id: `—`"
            )
        st.caption(" · ".join(meta_bits))

        cases_dir = st.text_input(
            "Cases dir", "evals/cases",
            disabled=active is not None, key="cases_dir_input",
        )

        st.markdown("**Swarm config**")
        st.caption(
            "`direct` = seed YAMLs · `random` = mutator variants · "
            "`class_probe` = FAIL fan-out · `partial_reentry` = PARTIAL re-runs."
        )
        sc1, sc2, sc3 = st.columns(3)
        with sc1:
            sw_variants = st.slider(
                "Mutations per seed", 0, 10, 3,
                disabled=active is not None, key="sw_variants",
                help=(
                    "Random LLM-mutated variants per seed in round 0. "
                    "0 = seeds only. Default 3."
                ),
            )
        with sc2:
            sw_max_rounds = st.slider(
                "Class-probe rounds", 0, 3, 2,
                disabled=active is not None, key="sw_max_rounds",
                help=(
                    "Max LangGraph rounds after initial dispatch. "
                    "0 disables FAIL fan-out."
                ),
            )
        with sc3:
            sw_mutator_model = st.selectbox(
                "Mutator + class-probe model",
                options=["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
                index=0,
                disabled=active is not None, key="sw_mutator_model",
            )

        ready = active is None
        if target["target_type"] == "copilot":
            pid_val = (target.get("config_json") or {}).get("patient_id") or ""
            if not pid_val:
                st.warning(
                    "⚠️ Co-Pilot target needs `patient_id` configured. "
                    "Recreate it via '+ Add a new target' below with the "
                    "patient_id field filled."
                )
                ready = False

        sw_review = st.checkbox(
            "Review before dispatch (Approve / Modify / Override gate)",
            value=False, disabled=active is not None, key="sw_review",
            help=(
                "When on: clicking Run shows the proposed swarm spec and "
                "asks for explicit Accept / Modify / Override before "
                "spawning the subprocess."
            ),
        )

        launch_in_flight = st.session_state.get("launch_in_flight", False)
        pending_review = st.session_state.get("pending_review")

        def _dispatch(variants: int, rounds: int, model: str) -> None:
            st.session_state["launch_in_flight"] = True
            try:
                with st.spinner(
                    "Starting campaign (subprocess + LangGraph init "
                    "takes ~20 s before progress appears in the sidebar)…"
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
                # Auto-scope sidebar to the new campaign so KPIs follow it.
                st.session_state["scope_campaign_id"] = str(cid)
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
            ac, mc, oc = st.columns(3)
            with ac:
                if st.button("✅ Accept", type="primary", key="approve_accept"):
                    _dispatch(
                        pending_review["mutations_per_seed"],
                        pending_review["max_rounds"],
                        pending_review["mutator_model"],
                    )
            with mc:
                if st.button("✏️ Modify (back to picker)", key="approve_modify"):
                    st.session_state.pop("pending_review", None)
                    st.rerun()
            with oc:
                override_json = st.text_area(
                    "Override (paste swarm-spec JSON)",
                    placeholder='{"mutations_per_seed": 5, "max_rounds": 1, "mutator_model": "gpt-4o"}',
                    height=80, key="approve_override_json",
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
            st.info(
                "▶ A campaign is currently running. Watch progress in the "
                "sidebar; the form here is locked until it finishes."
            )

    st.divider()

    with st.expander("➕ Add a new target", expanded=False):
        st.caption(
            "Targets are AI systems the platform attacks. Once added, they "
            "appear in the Target dropdown above. `target_type` determines "
            "how the chat client is built — see "
            "`agentforge_adversarial/target.py:make_client`."
        )
        with st.form("add_target_form", clear_on_submit=True):
            new_name = st.text_input("Name", placeholder="e.g. Staging Co-Pilot")
            new_type = st.selectbox(
                "Type", ["copilot", "generic_chat", "openai_compat"],
                help=(
                    "copilot = OpenEMR Clinical Co-Pilot (needs patient_id). "
                    "generic_chat = any HTTP/JSON endpoint with a prompt "
                    "template. openai_compat = OpenAI Chat Completions "
                    "wire format."
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
                new_config = {
                    "patient_id": new_patient,
                    "physician_user_id": new_physician,
                }
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
                        _insert_target(
                            new_name, new_type, new_url, new_config, new_notes
                        )
                        st.cache_data.clear()
                        st.success(
                            f"Added target `{new_name}`. Reload to see it "
                            "in the dropdown."
                        )
                    except Exception as e:
                        st.error(f"Failed to add target: {e}")
