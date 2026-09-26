from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, ESTIMATE_ROLES,
                    ESCALATION_CONFIRMATION_ROLES, RECORD_ROLES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_criterion,
                    escalation_required, priority_score, response_deadline_hours,
                    role_for_transition, validate_transition)


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

    def confirm_escalation(self, item_id: int, actor: str,
                           role: str) -> Dict[str, Any]:
        ensure_role(role, ESCALATION_CONFIRMATION_ROLES)
        actor = require_text(actor, "actor", 100)
        with self.repository.locked():
            item = self.repository.get_item(item_id)
            if item["status"] != "assessing":
                raise ConflictError(
                    "只有评估中的事件可以提交升级确认",
                    {"blocked_step": "escalation_confirmation",
                     "current_status": item["status"], "reason": "wrong_status"},
                )
            criterion = escalation_criterion(
                item["severity"], item["quantity"], item["threshold"])
            if criterion is None:
                from .domain import ValidationError
                raise ValidationError("当前估算未达到升级判据，无需提交升级确认")
            try:
                confirmation = self.repository.create_escalation_confirmation(
                    item_id, item["quantity"], item["threshold"], item["severity"],
                    criterion, item["version"], actor)
            except ConflictError as exc:
                if exc.details is None:
                    exc.details = {"blocked_step": "escalation_confirmation",
                                   "current_status": item["status"],
                                   "reason": "active_confirmation_exists"}
                raise
            self.repository.append_audit(
                "escalation_confirmation", ENTITY, item_id, actor, {
                    "confirmation_id": confirmation["id"],
                    "estimated_quantity": confirmation["estimated_quantity"],
                    "threshold": confirmation["threshold"],
                    "severity": confirmation["severity"],
                    "criterion": confirmation["criterion"],
                    "item_version": confirmation["item_version"],
                    "status": "active",
                })
            return self.enrich(self.repository.get_item(item_id), confirmation)

    def correct_estimate(self, item_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, ESTIMATE_ROLES)
        actor = require_text(actor, "actor", 100)
        quantity = require_number(payload.get("quantity"), "quantity")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            from .domain import ValidationError
            raise ValidationError("expected_version必须是正整数")
        with self.repository.locked():
            item = self.repository.get_item(item_id)
            if item["status"] not in ("reported", "assessing"):
                raise ConflictError(
                    "事件已进入围控，不能再更正估算油量",
                    {"blocked_step": "estimate_correction",
                     "current_status": item["status"], "reason": "already_containing"},
                )
            if quantity == item["quantity"]:
                from .domain import ValidationError
                raise ValidationError("估算油量未发生变化")
            reason = (
                f"估算油量已由{item['quantity']:g}更正为{quantity:g}，"
                f"确认所依据的事件版本{item['version']}已失效"
            )
            try:
                result = self.repository.correct_estimate(
                    item_id, quantity, expected_version, reason, actor)
            except ConflictError as exc:
                if exc.details is None:
                    exc.details = {"blocked_step": "estimate_correction",
                                   "current_status": item["status"],
                                   "reason": "version_conflict"}
                raise
            updated = result["item"]
            invalidated = result["invalidated_confirmations"]
            self.repository.append_audit("estimate_correction", ENTITY, item_id, actor, {
                "old_quantity": item["quantity"], "new_quantity": quantity,
                "threshold": item["threshold"], "severity": item["severity"],
                "from_version": item["version"], "to_version": updated["version"],
                "expected_version": expected_version,
                "invalidated_confirmation_ids": [row["id"] for row in invalidated],
                "invalidated_reason": reason if invalidated else None,
            })
            for confirmation in invalidated:
                self.repository.append_audit(
                    "escalation_confirmation_invalidated", ENTITY, item_id, actor, {
                        "confirmation_id": confirmation["id"],
                        "estimated_quantity": confirmation["estimated_quantity"],
                        "threshold": confirmation["threshold"],
                        "severity": confirmation["severity"],
                        "criterion": confirmation["criterion"],
                        "item_version": confirmation["item_version"],
                        "status": "invalidated",
                        "reason": reason,
                    })
            return self.enrich(updated, self.repository.get_latest_confirmation(item_id))

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        if not isinstance(expected_version, int) or expected_version < 1:
            from .domain import ValidationError
            raise ValidationError("expected_version必须是正整数")
        with self.repository.locked():
            item = self.repository.get_item(item_id)
            validate_transition(item["status"], target)
            ensure_role(role, role_for_transition(target))
            escalation_blocker = self._escalation_blocker(item, target)
            if escalation_blocker is not None:
                raise escalation_blocker
            blockers = completion_blockers(target, self.repository.open_record_count(item_id))
            if blockers:
                raise ConflictError(
                    "；".join(blockers),
                    {"blocked_step": target, "reason": "open_records",
                     "current_status": item["status"]},
                )
            updated = self.repository.transition_item(item_id, target, expected_version, actor)
            confirmation = self.repository.get_latest_confirmation(item_id)
            self.repository.append_audit("transition", ENTITY, item_id, actor, {
                "from": item["status"], "to": target,
                "escalation_required": escalation_required(
                    item["severity"], item["quantity"], item["threshold"]),
                "escalation_confirmation_id": confirmation["id"]
                if confirmation and confirmation["status"] == "active" else None,
            })
            return self.enrich(updated, confirmation)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        item = self.repository.get_item(item_id)
        return self.enrich(item, self.repository.get_latest_confirmation(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        confirmations = self.repository.latest_confirmation_map()
        return [self.enrich(item, confirmations.get(item["id"]))
                for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def _escalation_blocker(self, item: Dict[str, Any], target: str):
        if target != "containing" or item["status"] != "assessing":
            return None
        if not escalation_required(item["severity"], item["quantity"], item["threshold"]):
            return None
        confirmation = self.repository.get_latest_confirmation(item["id"])
        valid = (
            confirmation is not None and
            confirmation["status"] == "active" and
            confirmation["item_version"] == item["version"] and
            confirmation["estimated_quantity"] == item["quantity"] and
            confirmation["threshold"] == item["threshold"] and
            confirmation["severity"] == item["severity"]
        )
        if valid:
            return None
        if confirmation is None:
            reason, message = "missing_confirmation", "升级确认缺失：越过升级线后，需由response_commander先提交升级确认才能进入围控"
        elif confirmation["status"] == "invalidated":
            reason = "confirmation_invalidated"
            detail = confirmation.get("invalidated_reason") or "确认依据已失效"
            message = f"升级确认已失效：{detail}；请重新提交升级确认"
        else:
            reason = "confirmation_stale"
            message = (f"升级确认不是当前版本：确认基于事件版本{confirmation['item_version']}，"
                       f"当前版本为{item['version']}；请重新提交升级确认")
        return ConflictError(message, {
            "blocked_step": "escalation_confirmation",
            "required_before_transition": "containing",
            "current_status": item["status"],
            "reason": reason,
            "latest_confirmation_id": confirmation["id"] if confirmation else None,
            "confirmation_status": confirmation["status"] if confirmation else None,
            "invalidated_reason": confirmation.get("invalidated_reason")
            if confirmation else None,
            "current_version": item["version"],
            "confirmed_version": confirmation["item_version"] if confirmation else None,
        })

    @staticmethod
    def enrich(item: Dict[str, Any],
               confirmation: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_confirmation"] = confirmation
        result["escalation_confirmation_status"] = (
            confirmation["status"] if confirmation else None)
        result["escalation_confirmation_invalidated_reason"] = (
            confirmation.get("invalidated_reason") if confirmation else None)
        return result
