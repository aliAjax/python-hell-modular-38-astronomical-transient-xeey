from datetime import datetime, timezone

from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError


class SchedulingConflict(ConflictError):
    def __init__(self, reasons):
        self.reasons = reasons
        super().__init__("; ".join(item["reason"] for item in reasons))


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def calculate_priority(magnitude, transient_type):
    """Return a deterministic observing priority from 0 to 100."""
    try:
        mag = float(magnitude)
    except (TypeError, ValueError):
        raise ValidationError("magnitude must be numeric")
    if mag < -30 or mag > 40:
        raise ValidationError("magnitude is outside the supported range")
    type_weights = {
        "grb": 30,
        "supernova": 22,
        "tde": 20,
        "variable": 8,
        "unknown": 12,
    }
    score = max(0.0, 100.0 - mag * 4.0) + type_weights.get(transient_type, 5)
    return round(min(100.0, score), 2)


def measurements_overlap(first_start, first_end, second_start, second_end):
    return str(first_start) < str(second_end) and str(second_start) < str(first_end)


def parse_utc(value):
    """Parse the normalized UTC ISO-8601 values used by observation windows."""
    text = str(value or "")
    if not text:
        raise ValidationError("observation time is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("observation times must use ISO-8601 UTC format")
    if parsed.tzinfo is None:
        raise ValidationError("observation times must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _priority(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _observation_priority(entity, candidate=None):
    value = _priority(entity["data"].get("priority_score"))
    if value is None and candidate:
        value = _priority(candidate["data"].get("priority_score"))
    return value if value is not None else float("-inf")


def _submission_key(entity):
    if entity.get("seq") is not None:
        return (1, entity["seq"])
    return (0, entity.get("created_at") or "", entity.get("id") or "")


def _validate_source(actor, data, lookup):
    if len(str(data.get("name", "")).strip()) < 2:
        raise ValidationError("source name is required")
    return {}


def _validate_candidate(actor, data, lookup):
    source_id = data.get("source_id")
    if not _find_one(lookup, "source", "id", source_id):
        raise ValidationError("source does not exist")
    try:
        ra = float(data.get("ra"))
        dec = float(data.get("dec"))
    except (TypeError, ValueError):
        raise ValidationError("ra and dec must be numeric")
    if ra < 0 or ra >= 360 or dec < -90 or dec > 90:
        raise ValidationError("coordinates are outside valid ranges")
    event_key = "%s:%s" % (source_id, data.get("event_id"))
    duplicate = _find_one(lookup, "candidate", "event_key", event_key)
    if duplicate:
        raise ConflictError("candidate already exists for event key " + event_key)
    return {
        "event_key": event_key,
        "measurements": [
            {
                "observed_at": data.get("observed_at"),
                "ra": ra,
                "dec": dec,
                "magnitude": float(data["magnitude"]),
            }
        ],
        "priority_score": calculate_priority(data.get("magnitude"), data.get("transient_type")),
    }


def _validate_telescope(actor, data, lookup):
    if not (0 < float(data.get("aperture_m", 0))):
        raise ValidationError("aperture_m must be positive")
    return {}


def _validate_observation(actor, data, lookup):
    candidate = _find_one(lookup, "candidate", "id", data.get("candidate_id"))
    if not candidate:
        raise ValidationError("candidate does not exist")
    if not _find_one(lookup, "telescope", "id", data.get("telescope_id")):
        raise ValidationError("telescope does not exist")
    start_at = parse_utc(data.get("start_at"))
    end_at = parse_utc(data.get("end_at"))
    if start_at >= end_at:
        raise ValidationError("observation end must be after start")
    priority = _priority(candidate["data"].get("priority_score"))
    if priority is None:
        raise ValidationError("observation priority is unavailable")
    return {
        "scheduled_team": data.get("team_id"),
        "priority_score": priority,
    }


def _validate_merge_measurement(actor, entity, data, lookup):
    measurement = data.get("measurement")
    if not isinstance(measurement, dict):
        raise ValidationError("measurement must be an object")
    try:
        ra = float(measurement.get("ra"))
        dec = float(measurement.get("dec"))
        magnitude = float(measurement.get("magnitude"))
    except (TypeError, ValueError):
        raise ValidationError("measurement coordinates and magnitude must be numeric")
    if ra < 0 or ra >= 360 or dec < -90 or dec > 90:
        raise ValidationError("measurement coordinates are outside valid ranges")
    observed_at = str(measurement.get("observed_at", "")).strip()
    if not observed_at:
        raise ValidationError("measurement observed_at is required")
    existing = list(entity["data"].get("measurements") or [])
    if any(item.get("observed_at") == observed_at for item in existing):
        raise ConflictError("measurement timestamp already merged")
    merged = existing + [{"observed_at": observed_at, "ra": ra, "dec": dec, "magnitude": magnitude}]
    latest = sorted(merged, key=lambda item: item["observed_at"])[-1]
    return {
        "measurements": merged,
        "merged_measurement_count": len(merged),
        "latest_magnitude": latest["magnitude"],
        "priority_score": calculate_priority(latest["magnitude"], entity["data"].get("transient_type")),
    }


def _validate_reclassify(actor, entity, data, lookup):
    if data.get("new_type") not in {"grb", "supernova", "tde", "variable", "unknown"}:
        raise ValidationError("unsupported transient type")
    return {"transient_type": data["new_type"], "previous_type": entity["data"].get("transient_type")}


def _validate_correct(actor, entity, data, lookup):
    if not str(data.get("reason", "")).strip():
        raise ValidationError("correction reason is required")
    return {"corrected_by": actor.user_id}


def _scheduling_evaluation(actor, entity, data, lookup, now=None):
    now = now or datetime.now(timezone.utc)
    start_at = parse_utc(entity["data"].get("start_at"))
    end_at = parse_utc(entity["data"].get("end_at"))
    candidate = _find_one(lookup, "candidate", "id", entity["data"].get("candidate_id"))
    incoming_priority = _observation_priority(entity, candidate)

    team_id = entity["data"].get("team_id") or entity["data"].get("scheduled_team")
    telescope_observations = (
        lookup("observation", "telescope_id", entity["data"].get("telescope_id")) if lookup else []
    )
    team_observations = (
        lookup("observation", "scheduled_team", team_id) if lookup else []
    )
    team_observations += lookup("observation", "team_id", team_id) if lookup else []
    others = {item["id"]: item for item in telescope_observations + team_observations}
    blocked = []
    displaced = []
    incoming_submission = _submission_key(entity)

    for other in others.values():
        if other["id"] == entity["id"] or other["status"] != "scheduled":
            continue
        if not measurements_overlap(
            start_at,
            end_at,
            parse_utc(other["data"].get("start_at")),
            parse_utc(other["data"].get("end_at")),
        ):
            continue

        same_team = (
            other["data"].get("scheduled_team") == team_id or other["data"].get("team_id") == team_id
        )
        other_candidate = _find_one(lookup, "candidate", "id", other["data"].get("candidate_id"))
        other_priority = _observation_priority(other, other_candidate)
        other_started = parse_utc(other["data"].get("start_at")) <= now

        if same_team:
            blocked.append(
                {
                    "observation_id": other["id"],
                    "reason": "同一队伍在该时段已有观测计划",
                    "reason_code": "team_committed",
                    "priority_score": other_priority,
                }
            )
            continue
        if other_started:
            blocked.append(
                {
                    "observation_id": other["id"],
                    "reason": "该计划已经开始，不能顶掉",
                    "reason_code": "already_started",
                    "priority_score": other_priority,
                }
            )
        elif other_priority < incoming_priority or (
            other_priority == incoming_priority
            and incoming_submission < _submission_key(other)
        ):
            reason = (
                "被更高优先级观测申请顶掉"
                if other_priority < incoming_priority
                else "同优先级竞争中，后提交的计划被先提交的申请顶掉"
            )
            displaced.append(
                {
                    "entity": other,
                    "reason": reason,
                    "reason_code": "preempted" if other_priority < incoming_priority else "same_priority_later",
                    "priority_score": other_priority,
                }
            )
        else:
            blocked.append(
                {
                    "observation_id": other["id"],
                    "reason": "同望远镜时段已被同优先级或更高优先级、且先提交或已锁定的计划占用",
                    "reason_code": "higher_or_equal_priority",
                    "priority_score": other_priority,
                }
            )

    if blocked:
        raise SchedulingConflict(blocked)

    scheduled_at = now.isoformat(timespec="seconds")
    incoming_patch = {
        "scheduled_by": actor.user_id,
        "scheduled_at": scheduled_at,
        "priority_score": incoming_priority,
        "queue_reason": None,
        "queue_reason_code": None,
        "bumped_by_observation_id": None,
        "bumped_by_user_id": None,
        "bumped_at": None,
        "bump_priority_score": None,
        "displaced_observation_ids": [item["entity"]["id"] for item in displaced],
    }
    displaced_updates = []
    for item in displaced:
        other = item["entity"]
        reason = item["reason"]
        displaced_patch = {
            "scheduled_by": None,
            "scheduled_at": None,
            "queue_reason": reason,
            "queue_reason_code": item["reason_code"],
            "bumped_by_observation_id": entity["id"],
            "bumped_by_user_id": actor.user_id,
            "bumped_at": scheduled_at,
            "bump_priority_score": incoming_priority,
        }
        displaced_updates.append(
            {
                "entity": other,
                "next_status": "requested",
                "patch": displaced_patch,
                "audit_action": "preempt",
                "audit_detail": {
                    "reason": reason,
                    "replacement_observation_id": entity["id"],
                    "replacement_priority_score": incoming_priority,
                    "preempted_priority_score": item["priority_score"],
                },
            }
        )
    return incoming_patch, displaced_updates


def _validate_schedule(actor, entity, data, lookup):
    incoming_patch, displaced_updates = _scheduling_evaluation(actor, entity, data, lookup)
    return incoming_patch


class RuleEngine:
    ALIASES = {
        "sources": "source",
        "candidates": "candidate",
        "telescopes": "telescope",
        "observations": "observation",
    }
    INITIAL_STATUS = {
        "source": "registered",
        "candidate": "detected",
        "telescope": "available",
        "observation": "requested",
    }
    TRANSITIONS = {
        "source": {
            "activate": (("registered",), "active"),
            "retire": (("active",), "retired"),
        },
        "candidate": {
            "merge_measurement": (("detected", "triaged"), "triaged"),
            "triage": (("detected",), "triaged"),
            "reclassify": (("triaged",), "triaged"),
            "correct": (("detected", "triaged", "classified"), "triaged"),
            "withdraw": (("detected", "triaged"), "withdrawn"),
            "classify": (("triaged",), "classified"),
        },
        "telescope": {
            "restrict": (("available",), "restricted"),
            "restore": (("restricted",), "available"),
        },
        "observation": {
            "schedule": (("requested",), "scheduled"),
            "complete": (("scheduled",), "completed"),
            "withdraw": (("requested", "scheduled"), "withdrawn"),
            "correct": (("requested", "scheduled"), "requested"),
        },
    }
    CREATE_REQUIRED = {
        "source": ("name", "survey_name"),
        "candidate": ("source_id", "event_id", "ra", "dec", "magnitude", "transient_type", "observed_at"),
        "telescope": ("name", "aperture_m", "site_name"),
        "observation": ("candidate_id", "telescope_id", "team_id", "start_at", "end_at", "mode"),
    }
    ACTION_REQUIRED = {
        ("source", "retire"): ("reason",),
        ("candidate", "merge_measurement"): ("measurement",),
        ("candidate", "triage"): ("reason",),
        ("candidate", "reclassify"): ("new_type", "reason"),
        ("candidate", "correct"): ("reason",),
        ("candidate", "withdraw"): ("reason",),
        ("candidate", "classify"): ("classification",),
        ("telescope", "restrict"): ("reason",),
        ("observation", "schedule"): ("operator_id",),
        ("observation", "withdraw"): ("reason",),
        ("observation", "correct"): ("reason",),
    }
    CREATE_ROLES = {
        "source": ("analyst", "admin"),
        "candidate": ("analyst", "operator", "admin"),
        "telescope": ("coordinator", "admin"),
        "observation": ("analyst", "coordinator", "admin"),
    }
    ROLE_ACTIONS = {
        "activate": ("coordinator", "admin"),
        "retire": ("coordinator", "admin"),
        "merge_measurement": ("analyst", "operator", "admin"),
        "triage": ("analyst", "operator", "admin"),
        "reclassify": ("analyst", "supervisor", "admin"),
        "correct": ("analyst", "supervisor", "admin"),
        ("observation", "withdraw"): ("analyst", "coordinator", "supervisor", "admin"),
        ("observation", "correct"): ("analyst", "supervisor", "admin"),
        "withdraw": ("analyst", "supervisor", "admin"),
        "classify": ("analyst", "supervisor", "admin"),
        "restrict": ("coordinator", "admin"),
        "restore": ("coordinator", "admin"),
        "schedule": ("coordinator", "admin"),
        "complete": ("operator", "coordinator", "admin"),
    }
    CUSTOM_CREATE = {
        "source": _validate_source,
        "candidate": _validate_candidate,
        "telescope": _validate_telescope,
        "observation": _validate_observation,
    }
    CUSTOM_TRANSITIONS = {
        ("candidate", "merge_measurement"): _validate_merge_measurement,
        ("candidate", "reclassify"): _validate_reclassify,
        ("candidate", "correct"): _validate_correct,
        ("observation", "schedule"): _validate_schedule,
        ("observation", "correct"): _validate_correct,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        return custom(actor, data, lookup) if custom else {}

    def schedule_evaluation(self, actor, entity, data, lookup=None, now=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get("schedule")
        if not transition:
            raise InvalidTransition("unknown action schedule for %s" % kind)
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot schedule from status %s" % entity["status"])
        self._ensure_role(actor, self.ROLE_ACTIONS.get("schedule", ("admin",)))
        self._require(data, self.ACTION_REQUIRED.get((kind, "schedule"), ()))
        incoming_patch, displaced_updates = _scheduling_evaluation(
            actor, entity, data, lookup, now=now
        )
        return next_status, incoming_patch, displaced_updates

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed_roles = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
