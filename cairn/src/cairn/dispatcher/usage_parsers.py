from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re

from cairn.server.usage_models import TOKEN_FIELDS, UsageReading


@dataclass
class ParsedUsage:
    readings: list[UsageReading]
    notes: list[str] = field(default_factory=list)
    session_key: str | None = None
    snapshot: dict | None = None


def json_events(stdout: str) -> list[dict]:
    try:
        value = json.loads(stdout)
    except ValueError:
        value = None
    if isinstance(value, dict):
        return [value]
    events = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _name(value):
    return value.strip() if isinstance(value, str) and value.strip() and len(value) <= 200 else None


def configured_model(worker):
    return _name(getattr(worker, "model", None)) or _name(worker.env.get({"codex": "CODEX_MODEL", "claudecode": "ANTHROPIC_MODEL", "pi": "PI_MODEL"}.get(worker.type, "")))


def _identity(worker, reported=None):
    model = _name(reported)
    if model:
        return {"model": model, "model_source": "reported"}
    model = configured_model(worker)
    return {"model": model, "model_source": "configured" if model else "unknown"}


def _number(value, notes):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 9_000_000_000_000:
        notes.append("invalid_usage_fields")
        return None
    return value


def _sum_known(*values):
    return sum(values) if all(value is not None for value in values) else None


def _inclusive(worker, usage, notes, model=None):
    total = _number(usage.get("input_tokens"), notes)
    cached = _number(usage.get("cached_input_tokens"), notes)
    writes = _number(usage.get("cache_write_input_tokens"), notes)
    output = _number(usage.get("output_tokens"), notes)
    if total is not None and cached is not None and cached > total:
        cached = None
        notes.append("invalid_usage_fields")
    uncached = total - cached if total is not None and cached is not None else None
    if writes is not None and uncached is not None and writes > uncached:
        writes = None
        notes.append("invalid_usage_fields")
    return UsageReading(**_identity(worker, model), input_tokens=total, cached_input_tokens=cached,
                        uncached_input_tokens=uncached, cache_write_input_tokens=writes, output_tokens=output)


def _exclusive(worker, usage, notes, model=None, *, camel=False, pi=False):
    keys = ("input", "cacheRead", "cacheWrite", "output") if pi else (
        ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "outputTokens") if camel else
        ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))
    fresh, cached, writes, output = (_number(usage.get(key), notes) for key in keys)
    return UsageReading(**_identity(worker, model), input_tokens=_sum_known(fresh, cached, writes),
                        cached_input_tokens=cached, uncached_input_tokens=_sum_known(fresh, writes),
                        cache_write_input_tokens=writes, output_tokens=output)


def _merge(readings):
    groups = {}
    for reading in readings:
        groups.setdefault((reading.model, reading.model_source), []).append(reading)
    return [UsageReading(model=key[0], model_source=key[1], **{
        field: _sum_known(*(getattr(row, field) for row in rows)) for field in TOKEN_FIELDS
    }) for key, rows in groups.items()]


def _codex(worker, events):
    notes, readings = [], []
    turn = 0
    seen = set()
    model = None
    for event in events:
        if event.get("type") == "turn.started":
            turn += 1
            model = event.get("model")
        if event.get("type") not in ("turn.completed", "turn.failed") or not isinstance(event.get("usage"), dict):
            continue
        key = event.get("turn_id") or turn
        if key in seen:
            continue
        seen.add(key)
        readings.append(_inclusive(worker, event["usage"], notes, event.get("model") or model))
    return ParsedUsage(_merge(readings), notes)


def _pi(worker, events):
    notes, readings = [], []
    messages = [event.get("message") for event in events if event.get("type") == "message_end"]
    if not messages:
        messages = [event.get("message") for event in events if event.get("type") == "turn_end"]
    if not messages:
        messages = [message for event in events if event.get("type") == "agent_end" for message in event.get("messages", [])]
    seen = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant" or not isinstance(message.get("usage"), dict):
            continue
        identity = message.get("responseId") or message.get("id") or hashlib.sha256(json.dumps(message, sort_keys=True).encode()).hexdigest()
        if identity in seen:
            continue
        seen.add(identity)
        usage = message["usage"]
        if message.get("stopReason") in ("error", "aborted") and not any(usage.get(key) for key in ("input", "cacheRead", "cacheWrite", "output")):
            notes.append("pi_error_zero_usage")
            continue
        readings.append(_exclusive(worker, usage, notes, message.get("responseModel") or message.get("model"), pi=True))
    return ParsedUsage(_merge(readings), notes)


def claude_session_key(events):
    session = next((event.get("session_id") for event in reversed(events) if event.get("session_id")), None)
    return hashlib.sha256(str(session).encode()).hexdigest() if session else None


def _claude(worker, events, resumed, baseline):
    notes = []
    session_key = claude_session_key(events)
    version = next((event.get("claude_code_version") for event in events if event.get("type") == "system" and event.get("subtype") == "init"), None)
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(version))
    version_tuple = tuple(map(int, match.groups())) if match else None
    cumulative = version_tuple is not None and version_tuple >= (2, 1, 277)
    init_model = next((event.get("model") for event in events if event.get("type") == "system" and event.get("subtype") == "init"), None)
    results = [event for event in events if event.get("type") == "result"]
    # A result is a cumulative snapshot, never another additive API response.
    selected = next((event for event in reversed(results) if isinstance(event.get("modelUsage"), dict) and event["modelUsage"]
                     and (not event.get("is_error") or any(value.get(key) for value in event["modelUsage"].values()
                         if isinstance(value, dict) for key in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")))), None)
    if len({event.get("session_id") for event in results if event.get("session_id")}) > 1:
        notes.append("claude_session_resets_unaccounted")
    if selected is not None and (not resumed or version_tuple is not None):
        raw = [_exclusive(worker, usage, notes, model, camel=True) for model, usage in selected["modelUsage"].items() if isinstance(usage, dict)]
        snapshot = {row.model: row.model_dump() for row in raw}
        selected_session = selected.get("session_id")
        if selected_session and hashlib.sha256(str(selected_session).encode()).hexdigest() != session_key:
            # A reset followed by an error may leave the latest usable result in
            # the old session. Never seed the new session with old counters.
            snapshot = None
        if cumulative and resumed:
            if baseline is None:
                notes.append("claude_resume_baseline_missing")
            else:
                delta = []
                for row in raw:
                    previous = baseline.get(row.model)
                    values = {}
                    for field in TOKEN_FIELDS:
                        current = getattr(row, field)
                        old = previous.get(field) if previous else 0
                        if current is None or old is None:
                            values[field] = None
                        elif current < old:
                            values[field] = None
                            notes.append("claude_counter_reset")
                        else:
                            values[field] = current - old
                    delta.append(UsageReading(model=row.model, model_source=row.model_source, **values))
                return ParsedUsage(delta, notes + ["session_delta_applied"], session_key, snapshot)
        else:
            return ParsedUsage(_merge(raw), notes, session_key if cumulative else None, snapshot if cumulative else None)
    else:
        snapshot = None
        if resumed:
            notes.append("claude_resume_scope_unknown")
    # The result usage is per turn, but excludes subagents. It is safer than
    # attributing an unknown cumulative session total to this resumed invocation.
    readings = []
    for event in results:
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
            if event.get("is_error") and not any(usage.get(key) for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")):
                continue
            readings.append(_exclusive(worker, usage, notes, init_model))
    if readings:
        notes.append("claude_main_loop_only")
    else:
        # Assistant output counts are placeholders in Claude's stream. Recover
        # input/cache only, counting shared API message IDs once.
        seen = set()
        for event in events:
            message = event.get("message")
            if event.get("type") != "assistant" or not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                continue
            key = message.get("id")
            if not key or key in seen:
                continue
            seen.add(key)
            reading = _exclusive(worker, message["usage"], notes, message.get("model"))
            reading.output_tokens = None
            readings.append(reading)
        if readings:
            notes.extend(["partial_cli_stream", "claude_subagents_unavailable"])
    return ParsedUsage(_merge(readings), notes, session_key if cumulative else None, snapshot)


def parse_usage(worker, stdout: str, *, resumed=False, baseline=None) -> ParsedUsage:
    events = json_events(stdout)
    if worker.type == "codex":
        parsed = _codex(worker, events)
    elif worker.type == "pi":
        parsed = _pi(worker, events)
    elif worker.type == "claudecode":
        parsed = _claude(worker, events, resumed, baseline)
    else:
        parsed = ParsedUsage([], ["mock_usage_unreported"])
    if not parsed.readings:
        parsed.readings = [UsageReading(**_identity(worker))]
        parsed.notes.append("usage_unreported")
    if len(parsed.readings) > 128:
        parsed.readings = parsed.readings[:128]
        parsed.notes.append("model_limit")
    return parsed
