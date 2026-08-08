# Explainability Contract (domain-neutral)

Chinese Tech Wire V0.5.5 implements a local, deterministic explainability pattern intended for reuse in other collector projects (OEM Radar, Smartphone Clank, subscription trackers, etc.).

This is a **design contract**, not a published Python package.

## Core types

### DecisionExplanation
Top-level explanation of why a scored record has its current score, status, and delivery decision.

Fields (conceptual):
- `record_id`
- `decision_type` (e.g. STORY_LEAD, ALERT, CLUSTER)
- `score`
- `status`
- `contributions: list[Contribution]`
- `threshold_checks: list[ThresholdCheck]`
- `timeline: list[TimelineEvent]`
- `material_changes: list[MaterialChange]`
- `delivery: DeliveryDecision | null`
- `explanation_version`
- `generated_at`

### Contribution
- `component` — name of score component
- `raw` — raw component value
- `weight` — configured weight
- `contribution` — raw × weight (or documented transform)
- `facts` — list of statements grounded in stored data or named rules

### ThresholdCheck
```json
{
  "code": "SUPPRESSED_STALE",
  "message": "Record is older than the configured freshness window.",
  "actual": 73.4,
  "threshold": 48,
  "unit": "hours",
  "passed": false,
  "source_rule": "newsroom.alerts.max_age_hours"
}
```

### TimelineEvent
- `timestamp` (ISO-8601 UTC preferred)
- `event_type`
- `layer` (domain-specific: NEWS / COMMUNITY / DOCUMENTARY / DISCORD / …)
- `source`
- `title`, `summary`, `url`
- `is_first_of_type`
- `inferred` — true when chronology is reconstructed without a direct timestamp

### MaterialChange
- `field` or `event_type`
- `before` / `after` when available
- `message`
- Only record changes that cross configured materiality thresholds.

### DeliveryDecision
- `eligible`, `would_send`
- `reason_code` (single primary reason)
- `checks: list[ThresholdCheck]`
- Must call the **same** production evaluator used for live delivery.

## Rules

1. Never invent facts not present in stored records or configuration.
2. One primary suppression reason per delivery decision.
3. Weighted contributions should reconcile approximately to the stored final score (document known caps).
4. CLI and GUI must share one structured explanation function.
5. Observational UIs must not re-scrape, re-score, or re-notify on GET.
6. Secrets never appear in explanations.

## CTW mapping

| Contract | CTW module |
|----------|------------|
| DecisionExplanation | `pipeline.explain.LeadExplanation` |
| DeliveryDecision | `evaluate_alert_eligibility` → `alert_decision` dict |
| TimelineEvent | `pipeline.explain.TimelineEvent` |
| Human view | `--explain-lead` / lead detail GUI |
| Machine view | `--explain-lead-json` |

## Versioning

Include `explanation_version` (CTW: `v1`) so downstream tools can detect schema changes.
