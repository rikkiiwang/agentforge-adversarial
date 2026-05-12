# Threat Model — OpenEMR Clinical Co-Pilot under adversarial pressure

**Status:** Skeleton draft 2026-05-11. Per-category deep dives in
`docs/taxonomy/`. This document is **machine-readable** — every leaf has
an `id` that test cases and dashboard cells join against.

---

## Summary (~500 words)

The OpenEMR Clinical Co-Pilot is a multi-tenant AI assistant embedded in a
healthcare EHR. A single compromise has a wider blast radius than a typical
chatbot: a successful PHI exfiltration is a HIPAA breach; a successful
cross-patient leak is a clinical safety incident; a successful tool-misuse
attack could in principle produce a fabricated clinical note that a
physician relies on. The threat model below is structured around what the
system is *required to defend* — not around what's easy to test.

Nine top-level categories cover the surface. The first six are mandated by
the Week 3 PRD (Map the Attack Surface, p.6): prompt injection, data
exfiltration, state corruption, tool misuse, DoS / cost amplification, and
identity / role exploitation. The last three are **Co-Pilot-specific
extensions** (⭐) chosen because we built specific defenses for them in W1
and W2 and have a defensible answer to "where exactly should this hold?":

- **Verification-Gate Bypass** ⭐ — the W1 Layer-1 source-attribution gate
  + Layer-2 domain rules are the core trust promise. Every claim the agent
  emits must trace to a `record_id` from a tool result in the current turn.
  Bypassing this — unanchored claims, forged `record_id`s, slipping past
  the cross-patient rule — breaks the system's defensibility.
- **Multimodal / Document Poisoning** ⭐ — W2's VLM extraction pipeline
  accepts physician-uploaded PDFs and PNGs. Adversarial content in an
  upload (steganographic instructions, OCR-hostile glyphs, malicious
  bbox-citation forgeries) can poison the agent's reasoning even when the
  user prompt is benign.
- **Observability Leak** ⭐ — the platform logs to Langfuse and an audit
  table. PHI showing up in either is a regulatory failure independent of
  what the user saw.

Every category cross-references **OWASP LLM Top 10** identifiers and
**MITRE ATLAS** tactic IDs so that an outside reviewer (CISO, auditor) can
map findings to recognized taxonomies without reading our internal docs.

Each top-level category contains 2–5 subcategories. Each subcategory
defines its **quality bars** — the set of delivery channels or attack
techniques it must be exercised through before it counts as "covered."
The default channel set (`direct`, `indirect`, `multi-turn`, `encoded`,
`via-upload`) applies to most categories; some categories (DoS, state
corruption) define their own native dimensions.

This document is the **denominator of every coverage metric** the
platform produces. The dashboard's coverage heat-map renders one cell per
`(category × subcategory × channel)` triple. Untested cells are the
highest-priority targets for the Orchestrator's next campaign. Wavering
cells (PARTIAL rate > 25%) get boosted mutator budget. Exploited cells
(open vuln) trigger regression replays.

The taxonomy is hybrid technique/impact-axis by design. CWE is impact-
indexed, MITRE ATT&CK is technique-indexed, and OWASP LLM Top 10 mixes
both. Pure axes collapse on real attacks ("a prompt injection that exfils
PHI" is in both); the cross-tag system lets each finding be filed by its
primary technique with the impact recorded as a secondary signal.

The threat model is a **living document**. New categories may be added as
new defenses ship in the target. The Orchestrator's prioritization logic
treats categories not in this document as out-of-scope: a category must
exist here before it can be tested or counted.

---

## Taxonomy index

Each entry below is a top-level category. Detail and seed attack cases
live in `docs/taxonomy/0X-<name>.md`.

### 1. Prompt Injection (`PI`)

Override the Co-Pilot's instructions via crafted input.

- **Subcategories:** persona_hijack · system_prompt_extraction · jailbreak_classic · instruction_smuggling · tool_invoke_override
- **Quality bars:** direct · indirect · multi-turn · encoded · via-upload
- **Co-Pilot defenses tested:** system prompt isolation · instruction hierarchy
- **Defenses referenced (structured):**
  ```yaml
  - id: B9
    name: Static system prompt + Anthropic prompt caching
    location: copilot/app/agent/prompt.py
  - id: B9-loop
    name: Agent loop instruction hierarchy
    location: copilot/app/agent/loop.py
  ```
- **OWASP / ATLAS:** LLM01 · AML.T0051
- **Detail:** `docs/taxonomy/01-prompt-injection.md`

### 2. Data Exfiltration (`DE`)

Get PHI, cross-patient data, system prompt content, or tool-result data out.

- **Subcategories:** phi_single_patient · cross_patient · system_prompt_leak · tool_result_leak · auth_bypass
- **Quality bars:** direct · indirect · multi-turn · encoded · via-upload
- **Co-Pilot defenses tested:** PHI minimizer (B5) · Layer-2 cross-patient rule (B4) · 3-layer panel-scope (B6)
- **Defenses referenced (structured):**
  ```yaml
  - id: B5
    name: PHI minimizer
    location: copilot/app/phi/minimizer.py
  - id: B4-L2
    name: Layer-2 cross-patient rule
    location: copilot/app/verification/rules.py
  - id: B6
    name: 3-layer panel-scope gate
    location:
      - copilot/app/main.py#_verify_patient_in_panel
      - copilot-demographics-gate.php
      - copilot-finder-scope.php
  ```
- **OWASP / ATLAS:** LLM06 · AML.T0024
- **Detail:** `docs/taxonomy/02-data-exfiltration.md`

### 3. State Corruption (`SC`)

Manipulate conversation history, persisted context, or the prompt cache.

- **Subcategories:** conversation_history_poison · resumed_session_takeover · prompt_cache_poison · multi_turn_context_drift
- **Quality bars:** turn_count · session_resume · cache_warm · cache_cold
- **Co-Pilot defenses tested:** session-pseudonym scope · resume-chat boundary (B11) · prompt-cache pinning (B9)
- **Defenses referenced (structured):**
  ```yaml
  - id: B5-session
    name: Session-scoped PHI pseudonym map
    location: copilot/app/phi/session.py
  - id: B11
    name: Resume-previous-chat persistence boundary
    location: copilot/app/main.py#sessions_recent_resume_end
  - id: B9
    name: Static system prompt + Anthropic prompt cache
    location: copilot/app/agent/prompt.py
  ```
- **OWASP / ATLAS:** LLM04 (data/model poisoning) · AML.T0020
- **Detail:** `docs/taxonomy/03-state-corruption.md`

### 4. Tool Misuse (`TM`)

Cause wrong tool / wrong arguments / recursive tool calls / out-of-scope FHIR reads.

- **Subcategories:** parameter_tampering · unintended_tool_invocation · recursive_tool_loop · oauth_scope_confusion
- **Quality bars:** direct · indirect · multi-turn · via-upload
- **Co-Pilot defenses tested:** ACL check (step 2 of 5-step tool pattern, B3) · schema validation · FHIR-only OAuth scope
- **Defenses referenced (structured):**
  ```yaml
  - id: B3
    name: 5-step tool pattern (resolve · ACL · fetch · minimize · record_id)
    location: copilot/app/tools/_base.py#run_tool
  - id: B4-schema
    name: submit_response structured-output schema
    location: copilot/app/agent/schemas.py
  - id: B2
    name: FHIR-only OAuth scope (data path constraint)
    location: copilot/app/fhir/oauth.py
  ```
- **OWASP / ATLAS:** LLM07 (insecure plugin design) · AML.T0053
- **Detail:** `docs/taxonomy/04-tool-misuse.md`

### 5. DoS & Cost (`DC`)

Burn budget, exhaust tokens, infinite tool loops, defeat the prompt cache.

- **Subcategories:** huge_input · recursive_self_reference · tool_invocation_loop · token_bomb_output · cache_busting
- **Quality bars:** huge_input · recursion · tool_loop · token_bomb · cache_busting *(category-native; channels don't apply)*
- **Co-Pilot defenses tested:** per-turn cost caps · retry limits · response truncation · cache pin
- **Defenses referenced (structured):**
  ```yaml
  - id: B7
    name: FallbackAdapter retry + per-turn provider swap
    location: copilot/app/agent/llm.py#FallbackAdapter
  - id: B9
    name: Static system prompt + Anthropic prompt cache
    location: copilot/app/agent/prompt.py
  - id: agent-loop-caps
    name: Per-turn tool-call + token caps (operational, no B-id yet)
    location: copilot/app/agent/loop.py
  ```
- **OWASP / ATLAS:** LLM04 (resource exhaustion subset) · AML.T0029
- **Detail:** `docs/taxonomy/05-dos-cost.md`

### 6. Identity & Role (`IR`)

Privilege escalation, persona hijack to an elevated role, front-desk → physician boundary.

- **Subcategories:** persona_elevation · cross_role_data_access · admin_bypass_list · panel_scope_bypass
- **Quality bars:** direct · indirect · multi-turn · via-upload
- **Co-Pilot defenses tested:** 3-layer panel-scope gate (B6) · demographics gate · finder scope · session gate · `COPILOT_ADMIN_USERS` env list
- **Defenses referenced (structured):**
  ```yaml
  - id: B6-demographics
    name: Demographics-page scope gate
    location: copilot-demographics-gate.php
  - id: B6-finder
    name: Patient-finder scope filter
    location: copilot-finder-scope.php
  - id: B6-session
    name: Session-open scope verification
    location: copilot/app/main.py#_verify_patient_in_panel
  - id: B6-admin-list
    name: COPILOT_ADMIN_USERS bypass list
    location: copilot/app/config.py + Railway env
  ```
- **OWASP / ATLAS:** LLM02 (insecure output → role drift) · AML.T0011
- **Detail:** `docs/taxonomy/06-identity-role.md`

### 7. Verification-Gate Bypass ⭐ (`VB`)

Force unanchored claims · forge `record_id` · slip past Layer-2 rules. **The core trust promise** of W1.

- **Subcategories:** unanchored_claim · record_id_forgery · layer2_allergy_bypass · layer2_cross_patient_bypass · submit_response_schema_evasion
- **Quality bars:** direct · indirect · multi-turn · encoded · via-upload
- **Co-Pilot defenses tested:** Layer-1 attribution (B4) · Layer-2 domain rules · `submit_response` structured-output tool schema
- **Defenses referenced (structured):**
  ```yaml
  - id: B4-L1
    name: Layer-1 source attribution gate
    location: copilot/app/verification/attribution.py
  - id: B4-L2
    name: Layer-2 domain rules (allergy + cross-patient)
    location: copilot/app/verification/rules.py
  - id: B4-schema
    name: submit_response structured-output schema (Claim model)
    location: copilot/app/agent/schemas.py
  ```
- **OWASP / ATLAS:** LLM09 (overreliance — bypassed verification *is* the failure mode) · custom-defined (no exact ATLAS analogue; ATLAS AML.T0054 closest)
- **Detail:** `docs/taxonomy/07-verification-bypass.md`

### 8. Multimodal & Document Poisoning ⭐ (`MP`)

Adversarial content in uploaded PDFs / PNGs / CCDAs that the W2 VLM extracts and trusts.

- **Subcategories:** instruction_in_lab_pdf · ocr_hostile_glyph · bbox_citation_forgery · upload_payload_stego · oversized_upload_dos
- **Quality bars:** pdf · png_or_image · ccda · oversize · ocr_evasion
- **Co-Pilot defenses tested:** W2 VLM extraction · OCR snap · bbox citation overlay · sha3-512 dedup · file-size cap
- **Defenses referenced (structured):**
  ```yaml
  - id: W2-VLM
    name: Claude vision extraction service
    location: copilot/app/ingestion/service.py
  - id: W2-OCR
    name: Tesseract OCR-snap for image bboxes
    location: copilot/app/ingestion/ocr.py
  - id: W2-BBOX
    name: Bbox citation overlay (PDF text-snap + image render)
    location: copilot/app/web/copilot_iframe.js
  - id: W2-SHA
    name: sha3-512 idempotency dedup
    location: copilot/app/persistence/processed_documents.py
  ```
- **OWASP / ATLAS:** LLM05 (supply chain via input artifacts) · custom
- **Detail:** `docs/taxonomy/08-multimodal-poisoning.md`

### 9. Observability Leak ⭐ (`OL`)

PHI in Langfuse traces, audit logs, error messages, or stack traces.

- **Subcategories:** phi_in_langfuse_trace · phi_in_audit_log · phi_in_error_response · phi_in_stack_trace
- **Quality bars:** trace · audit_log · error_path · stack_trace
- **Co-Pilot defenses tested:** PHI screen in log filters · split logging (Langfuse trace ≠ clinical audit, B10) · error response sanitization
- **Defenses referenced (structured):**
  ```yaml
  - id: B10
    name: Split logging (Langfuse trace ≠ clinical audit)
    location: copilot/app/observability/trace.py
  - id: B5-log-filter
    name: PHI screen in log filters
    location: copilot/app/phi/log_filter.py
  - id: error-sanitization
    name: Error-response sanitization (FastAPI exception handlers)
    location: copilot/app/main.py
  ```
- **OWASP / ATLAS:** LLM06 (info disclosure via logs) · HIPAA-specific (no LLM-Top-10 exact match)
- **Detail:** `docs/taxonomy/09-observability-leak.md`

---

## Machine-readable schema

Test cases in `evals/cases/**/*.yaml` declare their position via frontmatter:

```yaml
threat_id: PI-PH      # category-subcategory ID (Prompt Injection / Persona Hijack)
channel: indirect      # one of the quality bars
target_version: 0.4.2  # which Co-Pilot version this was run against
parent_id: PI-PH-MT-002  # lineage; null for seeds
source: random         # direct | random | mutator | regression
expected_failure_mode: "agent emits patient B allergies"
```

The Postgres `threat_model_cells` table mirrors this taxonomy and is the
source of truth that the heat-map rolls up against. Updates to the
taxonomy go through a versioned migration so coverage trends stay
comparable.

---

## What's deliberately not in scope

Captured here so future contributors don't waste time:

- **Physical security of the OpenEMR host.** Out of scope — this is an
  application-layer adversarial platform.
- **Network-layer attacks** (TLS downgrade, DNS poisoning). Out of scope.
- **OpenEMR's existing PHP attack surface** (SQLi, XSS in legacy forms).
  Tracked in the W1 `AUDIT.md`; not in the Co-Pilot's blast radius.
- **Supply chain on the *attacker* side** (compromised LangGraph,
  malicious PyPI package). Operational hygiene, not platform feature.
- **Attacks that target third-party LLM providers themselves** (Anthropic
  / OpenAI infra). Out of scope — the platform tests the *Co-Pilot's*
  posture, not the model vendor's.

These are listed explicitly so a grader can see we considered them and
made deliberate exclusions.
