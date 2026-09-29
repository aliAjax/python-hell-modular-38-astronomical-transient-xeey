from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine, SchedulingConflict


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value, connection=None):
        return self.repository.find_entities(
            self.rules.normalize_kind(kind), field, value, connection=connection
        )

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
        expected = int(expected_version) if expected_version is not None else entity["version"]
        payload = dict(data or {})

        if self.rules.normalize_kind(entity["kind"]) == "observation" and action == "schedule":
            return self._schedule(actor, entity, payload, expected)

        next_status, patch = self.rules.validate_transition(
            actor, entity, action, payload, self._lookup
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

    def _schedule(self, actor, entity, payload, expected_version):
        now = datetime.now(timezone.utc)
        with self.repository.locked_connection() as connection:
            current_entity = self.repository.get_entity(entity["id"], connection=connection)
            if not current_entity:
                raise NotFoundError("entity not found: " + entity["id"])
            if expected_version is not None and current_entity["version"] != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_entity["version"])
                )
            try:
                next_status, patch, displaced_updates = self.rules.schedule_evaluation(
                    actor,
                    current_entity,
                    payload,
                    lambda kind, field, value: self._lookup(kind, field, value, connection=connection),
                    now=now,
                )
            except SchedulingConflict as exc:
                reason = "; ".join(item["reason"] for item in exc.reasons)
                self.repository.apply_updates_with_audit(
                    connection,
                    [
                        {
                            "entity_id": current_entity["id"],
                            "expected_version": current_entity["version"],
                            "status": "requested",
                            "data": {
                                "queue_reason": reason,
                                "queue_reason_code": ",".join(
                                    item["reason_code"] for item in exc.reasons
                                ),
                            },
                        }
                    ],
                    [
                        {
                            "entity_id": current_entity["id"],
                            "actor_id": actor.user_id,
                            "actor_role": actor.role,
                            "action": "schedule_blocked",
                            "from_status": "requested",
                            "to_status": "requested",
                            "detail": {"reasons": exc.reasons},
                        }
                    ],
                    now=now.isoformat(timespec="seconds"),
                )
                connection.commit()
                raise ConflictError(reason)
            updates = [
                {
                    "entity_id": current_entity["id"],
                    "expected_version": expected_version,
                    "status": next_status,
                    "data": patch,
                }
            ]
            audit_entries = [
                {
                    "entity_id": current_entity["id"],
                    "actor_id": actor.user_id,
                    "actor_role": actor.role,
                    "action": "schedule",
                    "from_status": current_entity["status"],
                    "to_status": next_status,
                    "detail": {
                        "patch": patch,
                        "displaced_observation_ids": [
                            item["entity"]["id"] for item in displaced_updates
                        ],
                    },
                }
            ]
            for item in displaced_updates:
                old = item["entity"]
                updates.append(
                    {
                        "entity_id": old["id"],
                        "expected_version": old["version"],
                        "status": item["next_status"],
                        "data": item["patch"],
                    }
                )
                audit_entries.append(
                    {
                        "entity_id": old["id"],
                        "actor_id": actor.user_id,
                        "actor_role": actor.role,
                        "action": item["audit_action"],
                        "from_status": old["status"],
                        "to_status": item["next_status"],
                        "detail": item["audit_detail"],
                    }
                )
            self.repository.apply_updates_with_audit(connection, updates, audit_entries)
            scheduled_id = current_entity["id"]
        return self.repository.get_entity(scheduled_id)

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
