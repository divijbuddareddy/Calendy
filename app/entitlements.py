"""Plan / feature entitlement framework (ACC-005) and feature flags (PRD 19.3).

Features and limits are configured by plan here, never hard-coded in UI. Use
`has_feature(workspace, "routing_forms")` server-side and `feature` in templates.
"""
PLANS: dict[str, dict] = {
    "free": {
        "label": "Free",
        "price_minor": 0,
        "max_active_event_types": 1,
        "max_calendars": 1,
        "features": {"one_on_one", "ics", "email_notifications", "embeds", "contacts", "polls"},
    },
    "standard": {
        "label": "Standard",
        "price_minor": 1000,
        "max_active_event_types": None,
        "max_calendars": 6,
        "features": {
            "one_on_one", "ics", "email_notifications", "embeds", "contacts", "polls",
            "group", "multiple_durations", "one_off", "single_use", "workflows", "webhooks",
            "analytics", "payments", "custom_branding", "email_verification", "api", "sms",
        },
    },
    "teams": {
        "label": "Teams",
        "price_minor": 1600,
        "max_active_event_types": None,
        "max_calendars": 6,
        "features": {
            "one_on_one", "ics", "email_notifications", "embeds", "contacts", "polls",
            "group", "multiple_durations", "one_off", "single_use", "workflows", "webhooks",
            "analytics", "payments", "custom_branding", "email_verification", "api", "sms",
            "collective", "round_robin", "routing_forms", "teams", "team_analytics",
            "managed_events", "managed_workflows", "assistant", "notetaker",
        },
    },
    "enterprise": {
        "label": "Enterprise",
        "price_minor": None,
        "max_active_event_types": None,
        "max_calendars": None,
        "features": {
            "one_on_one", "ics", "email_notifications", "embeds", "contacts", "polls",
            "group", "multiple_durations", "one_off", "single_use", "workflows", "webhooks",
            "analytics", "payments", "custom_branding", "email_verification", "api", "sms",
            "collective", "round_robin", "routing_forms", "teams", "team_analytics",
            "managed_events", "managed_workflows", "assistant", "notetaker",
            "sso", "scim", "audit_export", "domain_control", "data_deletion_api",
        },
    },
}

# Global feature flags protecting incomplete modules; flip off to hide.
FEATURE_FLAGS: dict[str, bool] = {
    "assistant": True,
    "notetaker": True,
    "polls": True,
    "sso": True,
}


def plan_of(workspace) -> dict:
    return PLANS.get(getattr(workspace, "plan_id", None) or "free", PLANS["free"])


def has_feature(workspace, feature: str) -> bool:
    if FEATURE_FLAGS.get(feature) is False:
        return False
    return feature in plan_of(workspace)["features"]


def limit(workspace, name: str):
    return plan_of(workspace).get(name)
