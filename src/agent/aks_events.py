"""Bounded, correlated Kubernetes Event evidence; no diagnosis or tool calls."""

from datetime import datetime, timezone
import json

MAX_EVENTS = 20
MAX_EVENT_MESSAGE_CHARS = 500


def event_evidence(step, objects, redact):
    report = {"state": "not_collected", "events": [], "truncated": False,
              "discarded_invalid": 0, "discarded_unrelated": 0, "untrusted_data": True}
    if step is None:
        return report
    if step.outcome in {"error", "unknown", "missing"}:
        report.update(state=step.outcome, reason=step.parser_metadata.get("reason", "event_resource_unavailable"))
        return report
    payload = step.payload
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        report.update(state="unknown", reason="invalid_event_list")
        return report

    def timestamp(value):
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
        except (ValueError, OverflowError):
            return None

    selected = {}
    for event in payload["items"]:
        if not isinstance(event, dict):
            report["discarded_invalid"] += 1
            continue
        meta, ref = event.get("metadata"), event.get("involvedObject", event.get("regarding"))
        if (not isinstance(meta, dict) or meta.get("namespace") != "default"
                or not isinstance(ref, dict) or ref.get("namespace") != "default"
                or not isinstance(ref.get("kind"), str) or not isinstance(ref.get("name"), str)):
            report["discarded_invalid"] += 1
            continue
        key = (ref["kind"], ref["name"])
        if key not in objects or (objects[key] and ref.get("uid") != objects[key]):
            report["discarded_unrelated"] += 1
            continue
        message = event.get("message", event.get("note", ""))
        if any(not isinstance(value, str) for value in (message, event.get("type", ""), event.get("reason", ""))):
            report["discarded_invalid"] += 1
            continue
        series = event.get("series", {})
        if not isinstance(series, dict):
            report["discarded_invalid"] += 1
            continue
        count = series.get("count", event.get("count"))
        if count is not None and (type(count) is not int or count < 0):
            report["discarded_invalid"] += 1
            continue
        first = timestamp(event.get("firstTimestamp") or event.get("eventTime"))
        last = timestamp(series.get("lastObservedTime") or event.get("lastTimestamp") or event.get("eventTime"))
        sanitized = redact(message, MAX_EVENT_MESSAGE_CHARS + 1)
        item = {"type": redact(event.get("type", ""), 128), "reason": redact(event.get("reason", ""), 128),
                "message": sanitized[:MAX_EVENT_MESSAGE_CHARS],
                "message_truncated": len(message) > MAX_EVENT_MESSAGE_CHARS or len(sanitized) > MAX_EVENT_MESSAGE_CHARS,
                "object": {"kind": ref["kind"], "name": redact(ref["name"], 253)},
                "namespace": "default", "first_timestamp": first, "last_timestamp": last, "count": count,
                "correlation": "uid" if objects[key] else "exact_object_reference"}
        # Preserve reported aggregate counts. Never sum repeated observations.
        identity = meta.get("uid") or meta.get("name")
        if not isinstance(identity, str):
            identity = json.dumps(item, sort_keys=True)
        rank = (last or first or "", count if count is not None else -1, json.dumps(item, sort_keys=True))
        if identity not in selected or rank > selected[identity][0]:
            selected[identity] = (rank, item)
    ordered = sorted(selected.values(), key=lambda pair: (
        pair[0][0], pair[1]["type"] == "Warning", pair[0][2]), reverse=True)
    report.update(state="collected" if ordered else "no_relevant_events", relevant_count=len(ordered),
                  events=[item for _, item in ordered[:MAX_EVENTS]], truncated=len(ordered) > MAX_EVENTS)
    if report["discarded_invalid"]:
        report["state"] = "partial" if ordered else "unknown"
        report["reason"] = "invalid_or_namespace_mismatched_events"
    return report
