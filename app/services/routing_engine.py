"""Routing form rule engine (PRD 10.5) - pure functions, no DB access.

Rule shape (RoutingForm.rules_json item):
    {"id": "r1", "name": "Enterprise", "logic": "and"|"or",
     "conditions": [ {"field": "f1", "op": "equals", "value": "x"},
                     {"logic": "or", "conditions": [...]} ],      # nested group
     "destination": {"type": "event_type|message|url|team|host|salesforce_lookup", "value": ...}}

"salesforce_lookup" (alias "crm_lookup"): look up the submitter's email in the connected CRM (Salesforce
Contact/Lead owner, HubSpot contact owner, or the mock CRM) and send them to the record owner's booking
page when the owner is an active workspace member; value = optional event-type slug on the owner's page.
Otherwise the form's fallback destination is used.

Operators: equals, not_equals, contains, gt, lt, empty, not_empty.
Rules are evaluated in order; the first matching rule wins, otherwise the fallback destination.
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse

OPS = ("equals", "not_equals", "contains", "gt", "lt", "empty", "not_empty")
OP_LABELS = {
    "equals": "equals", "not_equals": "does not equal", "contains": "contains", "gt": "is greater than",
    "lt": "is less than", "empty": "is empty", "not_empty": "is not empty",
}
DEST_TYPES = ("event_type", "message", "url", "team", "host", "salesforce_lookup", "crm_lookup")
CRM_DEST_TYPES = ("salesforce_lookup", "crm_lookup")
FIELD_TYPES = ("text", "email", "phone", "select", "radio", "checkbox", "consent", "number")
FIELD_TYPE_LABELS = {
    "text": "Short text", "email": "Email", "phone": "Phone", "select": "Dropdown", "radio": "Radio buttons",
    "checkbox": "Checkboxes", "consent": "Consent", "number": "Number",
}


def _is_empty(v: Any) -> bool:
    if v is None or v is False:
        return True
    if isinstance(v, str):
        return v.strip() == ""
    if isinstance(v, (list, tuple, set, dict)):
        return len(v) == 0
    return False


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _norm(v: Any) -> str:
    if v is True:
        return "true"
    if v is False:
        return "false"
    return str(v if v is not None else "").strip().lower()


def match_condition(cond: dict, answers: dict) -> bool:
    if "conditions" in cond:  # nested group
        return match_group(cond, answers)
    op = cond.get("op", "equals")
    actual = answers.get(cond.get("field"))
    expected = cond.get("value")
    if op == "empty":
        return _is_empty(actual)
    if op == "not_empty":
        return not _is_empty(actual)
    values = actual if isinstance(actual, (list, tuple)) else [actual]
    if op == "equals":
        if actual is not False and _is_empty(actual):
            return _norm(expected) == ""
        return any(_norm(a) == _norm(expected) for a in values)
    if op == "not_equals":
        return not any(_norm(a) == _norm(expected) for a in values)
    if op == "contains":
        needle = _norm(expected)
        if isinstance(actual, (list, tuple)):
            return any(needle == _norm(a) or (needle and needle in _norm(a)) for a in actual)
        return bool(needle) and needle in _norm(actual)
    if op in ("gt", "lt"):
        a, b = _num(actual), _num(expected)
        if a is None or b is None:
            return False
        return a > b if op == "gt" else a < b
    return False


def match_group(group: dict, answers: dict) -> bool:
    conds = group.get("conditions") or []
    if not conds:
        return False
    results = (match_condition(c, answers) for c in conds)
    return all(results) if (group.get("logic") or "and").lower() == "and" else any(results)


def evaluate(rules: list, fallback: dict, answers: dict) -> tuple[Optional[str], dict]:
    """Return (matched_rule_id or None, destination). Fallback is used when no rule matches."""
    for rule in rules or []:
        if match_group(rule, answers or {}):
            return rule.get("id"), dict(rule.get("destination") or {})
    return None, dict(fallback or {})


def safe_external_url(url: str) -> Optional[str]:
    """Only absolute http(s) URLs are allowed as external destinations (no javascript:, data:, etc.)."""
    url = (url or "").strip()
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if p.scheme in ("http", "https") and p.netloc:
        return url
    return None


def validate_destination(dest: dict) -> Optional[str]:
    """Return an error message, or None if the destination is usable."""
    t = (dest or {}).get("type")
    v = (dest or {}).get("value")
    if t not in DEST_TYPES:
        return "Choose a destination type."
    if t == "url" and not safe_external_url(v or ""):
        return "External URL must start with http:// or https://."
    if t in CRM_DEST_TYPES and v and not str(v).replace("-", "").replace("_", "").isalnum():
        return "Event type slug may only contain letters, numbers and dashes."
    if t in ("event_type", "team", "host") and not v:
        return "Choose where to send the visitor."
    if t == "message" and not (v or "").strip():
        return "Enter the message to show."
    return None


def describe_condition(cond: dict, fields: dict) -> str:
    if "conditions" in cond:
        inner = f" {(cond.get('logic') or 'and').upper()} ".join(describe_condition(c, fields) for c in cond["conditions"])
        return f"({inner})"
    label = fields.get(cond.get("field"), cond.get("field"))
    op = cond.get("op")
    if op in ("empty", "not_empty"):
        return f"{label} {OP_LABELS[op]}"
    return f"{label} {OP_LABELS.get(op, op)} “{cond.get('value', '')}”"
