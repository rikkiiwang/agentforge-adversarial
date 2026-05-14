"""Unit tests for the Documentation Agent.

The full DB write path is exercised by `test_runner.py` (integration). This
module covers the pure-logic severity map + lineage resolution.
"""
from __future__ import annotations

from agentforge_adversarial.documentation_agent import (
    _SEVERITY_BY_CATEGORY,
    severity_for,
)


def test_severity_critical_for_data_exfil_and_verification_bypass():
    """PHI leak and uncited prescription = critical (clinical-safety anchor)."""
    assert severity_for("data_exfiltration") == "critical"
    assert severity_for("verification_bypass") == "critical"


def test_severity_high_for_pi_tm_sc_ir():
    """Persona, tool, state, identity attacks = high (defense-in-depth chain)."""
    assert severity_for("prompt_injection") == "high"
    assert severity_for("tool_misuse") == "high"
    assert severity_for("state_corruption") == "high"
    assert severity_for("identity_role") == "high"


def test_severity_medium_for_dos_and_observability_leak():
    """Cost + system-prompt leakage = medium (operational, not clinical-safety)."""
    assert severity_for("dos_cost") == "medium"
    assert severity_for("observability_leak") == "medium"


def test_severity_default_medium_for_unknown_category():
    """Unknown category falls back to medium so we don't drop on the floor."""
    assert severity_for("nonexistent_category") == "medium"


def test_severity_map_covers_all_shipped_categories():
    expected = {
        "prompt_injection", "data_exfiltration", "tool_misuse",
        "state_corruption", "identity_role", "dos_cost",
        "observability_leak", "verification_bypass",
    }
    assert expected <= set(_SEVERITY_BY_CATEGORY.keys()), (
        "severity_for must answer for every shipped category"
    )
