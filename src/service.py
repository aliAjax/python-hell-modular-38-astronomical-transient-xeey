from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError
from .rules import RuleEngine, current_stamp, preempt_reason


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "observation" and action == "preempt":
            # Preemption only happens as part of a higher-priority plan winning a contest.
            raise InvalidTransition("preempt is performed by the scheduler contest, not directly")
        if kind == "observation" and action == "schedule":
            return self._schedule_with_contest(actor, entity, data or {}, expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _schedule_with_contest(self, actor, entity, data, expected_version):
        """Schedule one plan, bumping lower-priority, not-yet-started plans away."""
        next_status, patch = self.rules.validate_transition(
            actor, entity, "schedule", dict(data), self._lookup
        )
        expected = int(expected_version) if expected_version is not None else entity["version"]
        stamp = current_stamp()
        with self.repository.transaction() as connection:
            fresh_row = self.repository._select_entity(connection, entity["id"])
            fresh = self.repository._entity_from_row(fresh_row)
            if int(fresh["version"]) != expected:
                raise ConflictError(
                    "version conflict: expected %s, found %s" % (expected, fresh["version"])
                )
            if fresh["status"] != "requested":
                raise InvalidTransition("cannot schedule from status %s" % fresh["status"])

            scheduled = self.repository.list_entities(
                kind="observation", status="scheduled", connection=connection
            )
            losers = self.rules.evaluate_telescope_contest(fresh, scheduled)
            self.rules.ensure_team_available(
                fresh, scheduled, bumped_ids=[loser["id"] for loser in losers]
            )

            for loser in losers:
                loser_row = self.repository._select_entity(connection, loser["id"])
                loser_fresh = self.repository._entity_from_row(loser_row)
                if loser_fresh["status"] != "scheduled":
                    # Lost the race with another coordinator decision; re-evaluate.
                    raise ConflictError(
                        "plan %s changed before preemption completed; retry" % loser["id"]
                    )
                loser_data = dict(loser_fresh["data"])
                loser_data.pop("scheduled_by", None)
                loser_data.pop("operator_id", None)
                loser_data.update(
                    {
                        "preempt_reason": preempt_reason(loser_fresh, fresh, stamp),
                        "preempted_by": fresh["id"],
                        "preempted_at": stamp,
                    }
                )
                self.repository._write_entity(
                    connection, loser["id"], loser_fresh["version"], "requested", loser_data
                )
                self.repository._insert_audit(
                    connection,
                    loser["id"],
                    actor.user_id,
                    actor.role,
                    "preempt",
                    "scheduled",
                    "requested",
                    {
                        "reason": loser_data["preempt_reason"],
                        "winner": fresh["id"],
                    },
                )

            merged = dict(fresh["data"])
            merged.update(patch)
            for stale_field in ("preempt_reason", "preempted_by", "preempted_at"):
                merged.pop(stale_field, None)
            self.repository._write_entity(connection, fresh["id"], expected, next_status, merged)
            self.repository._insert_audit(
                connection,
                fresh["id"],
                actor.user_id,
                actor.role,
                "schedule",
                "requested",
                "scheduled",
                {"patch": patch, "preempted": [loser["id"] for loser in losers]},
            )

            winner_row = self.repository._select_entity(connection, fresh["id"])
            result = self.repository._entity_from_row(winner_row)
        result["preempted"] = [loser["id"] for loser in losers]
        return result

    def queue(self, telescope_id=None):
        """Return the observing queue ordered per telescope with queue reasons."""
        candidates = {
            item["id"]: item
            for item in self.repository.list_entities(kind="candidate")
        }
        telescopes = {
            item["id"]: item
            for item in self.repository.list_entities(kind="telescope")
        }
        observations = self.repository.list_entities(kind="observation")
        stamp = current_stamp()

        def describe(entity):
            data = entity["data"]
            telescope = telescopes.get(data.get("telescope_id"))
            candidate = candidates.get(data.get("candidate_id"))
            return {
                "id": entity["id"],
                "seq": entity["seq"],
                "status": entity["status"],
                "version": entity["version"],
                "telescope_id": data.get("telescope_id"),
                "telescope_name": (telescope or {}).get("data", {}).get("name"),
                "candidate_id": data.get("candidate_id"),
                "event_id": (candidate or {}).get("data", {}).get("event_key"),
                "team_id": data.get("team_id"),
                "mode": data.get("mode"),
                "start_at": data.get("start_at"),
                "end_at": data.get("end_at"),
                "priority": data.get("priority", 0.0),
                "started": str(data.get("start_at", "")) <= stamp
                if entity["status"] == "scheduled"
                else False,
                "queue_reason": data.get("preempt_reason"),
                "preempted_by": data.get("preempted_by"),
                "created_at": entity["created_at"],
            }

        items = [
            describe(entity)
            for entity in observations
            if telescope_id is None or entity["data"].get("telescope_id") == telescope_id
        ]
        status_rank = {"scheduled": 0, "requested": 1, "completed": 2, "withdrawn": 3}
        items.sort(
            key=lambda item: (
                str(item["telescope_id"]),
                status_rank.get(item["status"], 9),
                str(item["start_at"]),
                item["seq"],
            )
        )
        return items

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
