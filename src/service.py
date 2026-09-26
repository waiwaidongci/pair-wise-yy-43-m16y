from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, ESTIMATE_EDIT_STATES,
                    INVALID_REASONS, RECORD_ROLES, VIEW_ROLES,
                    completion_blockers, criterion_text, escalation_blocker,
                    escalation_criterion, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def correct_estimate(self, item_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        quantity = require_number(payload.get("quantity"), "quantity")
        expected_version = payload.get("expected_version")
        if expected_version is not None:
            if not isinstance(expected_version, int) or expected_version < 1:
                raise ValueError("expected_version必须是正整数")
        item = self.repository.get_item(item_id)
        if item["status"] not in ESTIMATE_EDIT_STATES:
            raise ConflictError(f"事件已进入{item['status']}阶段，不能再更正估算油量", {
                "step": "estimate_correction",
                "reason": "stage_locked",
                "current_status": item["status"],
            })
        if abs(float(item["quantity"]) - float(quantity)) <= 1e-9:
            return self.enrich(item)
        result = self.repository.correct_quantity(
            item_id, quantity, expected_version, actor)
        updated, invalidated = result["item"], result["invalidated_count"]
        self.repository.append_audit("estimate_corrected", ENTITY, item_id, actor, {
            "previous_quantity": item["quantity"],
            "quantity": quantity,
            "previous_version": item["version"],
            "version": updated["version"],
            "confirmations_invalidated": invalidated,
        })
        return self.enrich(updated)

    def confirm_escalation(self, item_id: int, payload: Dict[str, Any],
                           actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, role_for_transition("containing"))
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] not in ESTIMATE_EDIT_STATES:
            raise ConflictError(f"事件已进入{item['status']}阶段，无需升级确认", {
                "step": "escalation_confirmation",
                "reason": "stage_passed",
                "current_status": item["status"],
            })
        criterion = escalation_criterion(
            item["severity"], item["quantity"], item["threshold"])
        if criterion is None:
            raise ConflictError("估算油量未越过升级线，无需确认升级", {
                "step": "escalation_confirmation",
                "reason": "not_required",
            })
        expected_version = payload.get("expected_version")
        if expected_version is not None:
            if not isinstance(expected_version, int) or expected_version < 1:
                raise ValueError("expected_version必须是正整数")
            if expected_version != item["version"]:
                raise ConflictError("版本冲突，请刷新后重试", {
                    "step": "escalation_confirmation",
                    "reason": "version_conflict",
                    "current_version": item["version"],
                })
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note", 1000)
        # 已有与当前版本、估算、判据一致的有效确认：幂等返回
        latest = self.repository.get_latest_confirmation(item_id)
        if (latest is not None and latest["status"] == "valid"
                and latest["item_version"] == item["version"]
                and latest["criterion"] == criterion
                and abs(float(latest["quantity"]) - float(item["quantity"])) <= 1e-9):
            return self.enrich(item)
        confirmation = self.repository.add_escalation_confirmation(
            item_id, item["quantity"], criterion, item["version"], note, actor)
        self.repository.append_audit("escalation_confirmed", ENTITY, item_id, actor, {
            "confirmation_id": confirmation["id"],
            "quantity": item["quantity"],
            "threshold": item["threshold"],
            "criterion": criterion,
            "criterion_text": criterion_text(criterion),
            "item_version": item["version"],
            "note": note,
        })
        return self.enrich(self.repository.get_item(item_id))

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        # 评估进入围控：必须存在与当前估算、判据、版本一致的有效升级确认
        if target == "containing":
            confirmation = self.repository.get_latest_confirmation(item_id)
            block = escalation_blocker(item, confirmation)
            if block is not None:
                step, reason = block
                if reason is None:
                    message = "缺少指挥官的升级确认，不能从评估进入围控"
                else:
                    message = ("升级确认已失效（{}），需要重新确认，"
                               "不能从评估进入围控").format(INVALID_REASONS.get(reason, reason))
                raise ConflictError(message, {
                    "step": step,
                    "reason": reason or "missing_confirmation",
                    "required_confirmation": {
                        "quantity": item["quantity"],
                        "criterion": escalation_criterion(
                            item["severity"], item["quantity"], item["threshold"]),
                        "item_version": item["version"],
                    },
                })
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        audit_detail: Dict[str, Any] = {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        }
        if target == "containing":
            confirmation = self.repository.get_latest_confirmation(item_id)
            audit_detail["confirmation_id"] = confirmation["id"] if confirmation else None
        self.repository.append_audit("transition", ENTITY, item_id, actor, audit_detail)
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        self._view(role)
        confirmation_map = self.repository.latest_confirmation_map()
        return [self.enrich(item, confirmation_map.get(item["id"]))
                for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def enrich(self, item: Dict[str, Any],
               confirmation: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        criterion = escalation_criterion(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = criterion is not None
        result["escalation_criterion"] = criterion
        if criterion is not None:
            result["escalation_criterion_text"] = criterion_text(criterion)
        if confirmation is None:
            confirmation = self.repository.get_latest_confirmation(item["id"])
        result["escalation_confirmation"] = self._confirmation_view(confirmation)
        return result

    @staticmethod
    def _confirmation_view(confirmation: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if confirmation is None:
            return None
        view = {
            "id": confirmation["id"],
            "status": confirmation["status"],
            "quantity": confirmation["quantity"],
            "criterion": confirmation["criterion"],
            "criterion_text": criterion_text(confirmation["criterion"]),
            "item_version": confirmation["item_version"],
            "note": confirmation.get("note"),
            "created_by": confirmation["created_by"],
            "created_at": confirmation["created_at"],
        }
        if confirmation["status"] == "invalidated":
            reason = confirmation.get("invalid_reason")
            view["invalid_reason"] = reason
            view["invalid_reason_text"] = INVALID_REASONS.get(reason, reason)
            view["invalidated_at"] = confirmation.get("invalidated_at")
        return view
