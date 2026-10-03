"""无障碍设施缺陷整改与复开领域服务。

在基础登记、幂等、事务与哈希审计能力之上，实现一条不可被新工单掩盖的
缺陷整改事件链：报修冻结责任方与 SLA 时钟、临时替代措施带到期与交接、
维修完成须独立复检通过才能复开；重复报修、复发、证据撤回、跨单位移交
全部延续原事件，并为值班经理与监管审计提供时点还原。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor

FACILITY_TYPES = frozenset({
    "accessible_elevator",
    "platform_lift",
    "ramp",
    "accessible_entrance",
    "guide_path",
    "accessible_toilet",
    "accessible_parking",
})

ROUTE_FACILITY_TYPES = frozenset({
    "accessible_elevator",
    "platform_lift",
    "ramp",
    "accessible_entrance",
    "guide_path",
})

AFFECTED_GROUPS = frozenset({
    "wheelchair",
    "mobility_impaired",
    "visual_impaired",
    "hearing_impaired",
    "elderly",
    "stroller",
    "temporary_injury",
})

MOBILITY_GROUPS = frozenset({"wheelchair", "mobility_impaired"})

SEVERITIES = ("low", "medium", "high", "critical")
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

MEASURE_KINDS = frozenset({
    "staff_assist",
    "alternative_route",
    "shuttle",
    "staffed_crossing",
    "portable_ramp",
    "notice_only",
})

# 临时措施必须是可感知的真实替代，单纯告示不能充当无障碍通道。
NON_ROUTE_KINDS = frozenset({"notice_only"})

# 尚未真正闭环的事件状态集合。
OPEN_STATUSES = frozenset({"open", "in_repair", "reopened"})

DEFAULT_SLA: dict[str, Any] = {
    "response_minutes": {"critical": 15, "high": 30, "medium": 60, "low": 120},
    "measure_minutes": {"critical": 30, "high": 60, "medium": 120, "low": 240},
    "repair_hours": {"critical": 4, "high": 24, "medium": 72, "low": 168},
    "reinspection_hours": 24,
    "guarantee_hours": 24 * 30,
    "measure_max_hours": 72,
}


def _parse_ts(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} 必须是 ISO-8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def minutes_between(start_iso: str, end_iso: str) -> int:
    return int((_parse_ts(end_iso, "now") - _parse_ts(start_iso, "deadline")).total_seconds() // 60)


def _positive(value: Any, field: str) -> None:
    if isinstance(value, dict):
        for sub in value.values():
            _positive(sub, field)
    elif not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValidationError(f"{field} 必须是正数")


def merge_sla(override: dict[str, Any] | None) -> dict[str, Any]:
    """合并合同自定义 SLA，未覆盖部分使用默认值。"""

    sla = {
        "response_minutes": dict(DEFAULT_SLA["response_minutes"]),
        "measure_minutes": dict(DEFAULT_SLA["measure_minutes"]),
        "repair_hours": dict(DEFAULT_SLA["repair_hours"]),
        "reinspection_hours": DEFAULT_SLA["reinspection_hours"],
        "guarantee_hours": DEFAULT_SLA["guarantee_hours"],
        "measure_max_hours": DEFAULT_SLA["measure_max_hours"],
    }
    if override:
        for key, value in override.items():
            if key not in sla:
                raise ValidationError(f"sla 不支持的字段: {key}")
            _positive(value, f"sla.{key}")
            if isinstance(sla[key], dict):
                if not isinstance(value, dict):
                    raise ValidationError(f"sla.{key} 必须是按风险等级的映射")
                for level in value:
                    if level not in SEVERITIES:
                        raise ValidationError(f"sla.{key} 含未知等级 {level}")
                sla[key].update(value)
            else:
                sla[key] = value
    return sla


def derive_risk(severity: str, sole_route: bool, groups: frozenset[str]) -> tuple[str, str]:
    """根据报告等级与设施影响推导有效等级（报修时自动升级）。"""

    effective = severity
    if groups & MOBILITY_GROUPS and SEVERITY_ORDER[effective] < SEVERITY_ORDER["medium"]:
        effective = "medium"
    if sole_route and groups & MOBILITY_GROUPS and SEVERITY_ORDER[effective] < SEVERITY_ORDER["high"]:
        effective = "high"
    return effective, effective


class AccessibilityService:
    """实施无障碍设施缺陷登记、整改、复检与复开规则。"""

    def __init__(self, domain) -> None:
        self.domain = domain
        self.database = domain.database
        self.clock = domain.clock

    # ------------------------------------------------------------------ 基础工具

    def _now_dt(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def _now(self) -> str:
        return _iso(self._now_dt())

    def _actor(self, connection, actor_id: str) -> Actor:
        return self.domain._actor(connection, actor_id)

    def _require(self, actor: Actor, *roles: str) -> None:
        self.domain._require(actor, *roles)

    def _text(self, value: str, field: str, limit: int = 300) -> str:
        return self.domain._text(value, field, limit)

    def _idempotent(self, connection, *, request_id, action, payload, create):
        return self.domain._idempotent(
            connection, request_id=request_id, action=action, payload=payload, create=create
        )

    def _replayed(self, connection, *, request_id: str, action: str,
                  payload: dict[str, Any]) -> dict[str, Any] | None:
        """状态机操作的幂等重放：命中原回执则直接返回，跳过已变迁的状态守卫。"""

        request_id = self.domain._identifier(request_id, "request_id")
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return {**json.loads(row["response_json"]), "replayed": True}

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _facility(self, connection, facility_id: str):
        row = connection.execute(
            "SELECT * FROM facilities WHERE facility_id=?", (facility_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("设施不存在")
        return row

    def _incident(self, connection, incident_id: str):
        row = connection.execute(
            "SELECT * FROM incidents WHERE incident_id=?", (incident_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("缺陷事件不存在")
        return row

    def _org_name(self, connection, organization_id: str) -> str:
        row = connection.execute(
            "SELECT name FROM organizations WHERE organization_id=?", (organization_id,)
        ).fetchone()
        return row["name"] if row else organization_id

    def _site_scope(self, actor: Actor, site) -> None:
        if actor.role != "admin" and actor.organization_id != site["organization_id"]:
            raise PermissionDenied("不能操作其他机构场所的设施")

    def _event(self, connection, *, incident_id: str, event_type: str, actor_id: str,
               detail: dict[str, Any], work_order_id: str | None = None,
               measure_id: str | None = None, reinspection_id: str | None = None,
               now: str | None = None) -> int:
        """追加事件链节点，同时写入全局哈希审计链。"""

        now = now or self._now()
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0) AS seq FROM incident_events WHERE incident_id=?",
            (incident_id,),
        ).fetchone()["seq"] + 1
        event_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO incident_events(event_id,incident_id,seq,event_type,actor_id,"
            "work_order_id,measure_id,reinspection_id,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (event_id, incident_id, seq, event_type, actor_id, work_order_id, measure_id,
             reinspection_id, canonical_json(detail), now),
        )
        append_event(connection, actor_id=actor_id, action=f"accessibility.{event_type}",
                     resource_type="incident", resource_id=incident_id,
                     detail={"seq": seq, **detail}, occurred_at=now)
        return seq

    def _notify(self, connection, *, incident_id: str, event_seq: int | None,
                kind: str, recipient_id: str, label: str, reason: str,
                channel: str = "api") -> None:
        connection.execute(
            "INSERT INTO notifications(notification_id,incident_id,event_seq,recipient_kind,"
            "recipient_id,recipient_label,channel,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, incident_id, event_seq, kind, recipient_id, label,
             channel, reason, self._now()),
        )

    def _manager_target(self, connection, site_org: str) -> tuple[str, str, str]:
        return ("role", f"{site_org}:duty_manager", f"{self._org_name(connection, site_org)}值班经理")

    def _org_target(self, connection, org_id: str) -> tuple[str, str, str]:
        return ("org", org_id, f"{self._org_name(connection, org_id)}维护责任方")

    def _active_contract(self, connection, facility, now: datetime):
        """定位时刻生效的维护合同：设施专属优先于场所通用。"""

        stamp = _iso(now)
        return connection.execute(
            "SELECT * FROM maintenance_contracts WHERE site_id=? "
            "AND valid_from<=? AND valid_to>=? "
            "AND (facility_id=? OR facility_id IS NULL) "
            "ORDER BY (facility_id IS NULL), created_at, contract_id LIMIT 1",
            (facility["site_id"], stamp, stamp, facility["facility_id"]),
        ).fetchone()

    def _latest_work_order(self, connection, incident_id: str):
        return connection.execute(
            "SELECT * FROM work_orders WHERE incident_id=? ORDER BY seq DESC LIMIT 1",
            (incident_id,),
        ).fetchone()

    def _contractor_orgs(self, connection, incident_id: str) -> set[str]:
        return {row["contractor_org_id"] for row in connection.execute(
            "SELECT DISTINCT contractor_org_id FROM work_orders WHERE incident_id=?",
            (incident_id,))}

    def _create_work_order(self, connection, *, incident, contractor_org_id: str,
                           contract_id: str, kind: str, continued_from: str | None,
                           now_dt: datetime, summary: str) -> str:
        """沿事件链开新工单；移交/返工不重置原 SLA 时钟。"""

        seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0) AS seq FROM work_orders WHERE incident_id=?",
            (incident["incident_id"],),
        ).fetchone()["seq"] + 1
        work_order_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO work_orders(work_order_id,incident_id,seq,kind,contractor_org_id,"
            "contract_id,status,due_response_at,due_repair_at,due_reinspect_at,"
            "continued_from_work_order_id,summary,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (work_order_id, incident["incident_id"], seq, kind, contractor_org_id,
             contract_id, "open", incident["due_response_at"], incident["due_repair_at"],
             None, continued_from, summary, _iso(now_dt)),
        )
        return work_order_id

    def _require_contractor(self, actor: Actor, work_order) -> None:
        self._require(actor, "admin", "operator")
        if actor.role != "admin" and actor.organization_id != work_order["contractor_org_id"]:
            raise PermissionDenied("只有冻结/承接的维护机构能操作该工单")

    def _flag_overdue(self, connection, *, incident_id: str, key: str, level: int) -> bool:
        """首次记录某环节逾期；重复扫描不产生重复升级。"""

        try:
            connection.execute(
                "INSERT INTO overdue_flags(incident_id,flag_key,escalation_level,first_flagged_at) "
                "VALUES(?,?,?,?)",
                (incident_id, key, level, self._now()),
            )
            return True
        except Exception:
            return False

    def _resolve_flags_like(self, connection, incident_id: str, fragment: str) -> None:
        connection.execute(
            "UPDATE overdue_flags SET resolved_at=? WHERE incident_id=? AND flag_key LIKE ? "
            "AND resolved_at IS NULL",
            (self._now(), incident_id, f"%{fragment}%"),
        )

    def _clear_resolved_flags(self, connection, incident_id: str) -> None:
        """事件重开后旧逾期环节可以再次被计时（历史升级已留在事件链）。"""

        connection.execute(
            "DELETE FROM overdue_flags WHERE incident_id=? AND resolved_at IS NOT NULL",
            (incident_id,),
        )

    # ------------------------------------------------------------------ 登记

    def register_facility(self, *, request_id: str, actor_id: str, site_id: str,
                          facility_id: str, facility_type: str, location: str,
                          description: str, affected_groups: list[str],
                          sole_accessible_route: bool = False) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "site_id": site_id, "facility_id": facility_id,
                   "facility_type": facility_type, "location": location,
                   "affected_groups": sorted(affected_groups),
                   "sole_accessible_route": sole_accessible_route}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._site_scope(actor, site)
            facility_id = self.domain._identifier(facility_id, "facility_id")
            if facility_type not in FACILITY_TYPES:
                raise ValidationError("facility_type 不在允许范围内")
            location = self._text(location, "location")
            description = self._text(description, "description", 500)
            groups = self._groups(affected_groups)
            if not isinstance(sole_accessible_route, bool):
                raise ValidationError("sole_accessible_route 必须是布尔值")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO facilities(facility_id,site_id,facility_type,location,"
                        "description,affected_groups_json,sole_accessible_route,available,"
                        "available_note,current_incident_id,version,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,1,?,NULL,1,?,?)",
                        (facility_id, site_id, facility_type, location, description,
                         canonical_json(sorted(groups)), int(sole_accessible_route),
                         "登记即开放", actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("设施编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="accessibility.facility.registered",
                             resource_type="facility", resource_id=facility_id,
                             detail={"site_id": site_id, "facility_type": facility_type,
                                     "location": location, "affected_groups": sorted(groups),
                                     "sole_accessible_route": sole_accessible_route},
                             occurred_at=self._now())
                return "facility", facility_id, {"facility_id": facility_id}

            receipt = self._idempotent(connection, request_id=request_id,
                                       action="a11y_register_facility", payload=payload,
                                       create=create)
            return {"facility_id": facility_id, "replayed": receipt.replayed}

    def _groups(self, values: list[str]) -> frozenset[str]:
        if not isinstance(values, list) or not values:
            raise ValidationError("affected_groups 必须是非空数组")
        groups = frozenset(str(v) for v in values)
        unknown = groups - AFFECTED_GROUPS
        if unknown:
            raise ValidationError(f"未知影响人群: {sorted(unknown)}")
        return groups

    def register_maintenance_contract(self, *, request_id: str, actor_id: str, site_id: str,
                                      contract_id: str, contractor_org_id: str,
                                      valid_from: str, valid_to: str,
                                      facility_id: str | None = None,
                                      sla: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "site_id": site_id, "contract_id": contract_id,
                   "contractor_org_id": contractor_org_id, "valid_from": valid_from,
                   "valid_to": valid_to, "facility_id": facility_id, "sla": sla or {}}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._site_scope(actor, site)
            replayed = self._replayed(connection, request_id=request_id,
                                      action="a11y_register_contract", payload=payload)
            if replayed is not None:
                return replayed
            contract_id = self.domain._identifier(contract_id, "contract_id")
            contractor_org_id = self.domain._identifier(contractor_org_id, "contractor_org_id")
            if connection.execute(
                "SELECT 1 FROM organizations WHERE organization_id=?", (contractor_org_id,)
            ).fetchone() is None:
                raise NotFoundError("维护机构不存在")
            if facility_id is not None:
                facility = self._facility(connection, facility_id)
                if facility["site_id"] != site_id:
                    raise ValidationError("设施不属于该场所")
            start = _parse_ts(valid_from, "valid_from")
            end = _parse_ts(valid_to, "valid_to")
            if end <= start:
                raise ValidationError("valid_to 必须晚于 valid_from")
            merged = merge_sla(sla)

            def create():
                try:
                    connection.execute(
                        "INSERT INTO maintenance_contracts(contract_id,site_id,contractor_org_id,"
                        "facility_id,valid_from,valid_to,sla_json,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (contract_id, site_id, contractor_org_id, facility_id,
                         _iso(start), _iso(end), canonical_json(merged), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("合同编号已经存在") from exc
                append_event(connection, actor_id=actor_id,
                             action="accessibility.contract.registered",
                             resource_type="maintenance_contract", resource_id=contract_id,
                             detail={"site_id": site_id, "contractor_org_id": contractor_org_id,
                                     "facility_id": facility_id,
                                     "valid_from": _iso(start), "valid_to": _iso(end),
                                     "sla": merged},
                             occurred_at=self._now())
                return "maintenance_contract", contract_id, {"contract_id": contract_id, "sla": merged}

            receipt = self._idempotent(connection, request_id=request_id,
                                       action="a11y_register_contract", payload=payload,
                                       create=create)
            return {"contract_id": contract_id, "sla": merged, "replayed": receipt.replayed}

    # ------------------------------------------------------------------ 报修与升级

    def report_defect(self, *, request_id: str, actor_id: str, facility_id: str,
                      severity: str, summary: str,
                      impact: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "facility_id": facility_id, "severity": severity,
                   "summary": summary, "impact": impact or {}}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            facility = self._facility(connection, facility_id)
            replayed = self._replayed(connection, request_id=request_id,
                                      action="a11y_report_defect", payload=payload)
            if replayed is not None:
                return replayed
            if severity not in SEVERITIES:
                raise ValidationError("severity 必须是 low/medium/high/critical")
            summary = self._text(summary, "summary", 500)
            if impact is not None and not isinstance(impact, dict):
                raise ValidationError("impact 必须是对象")
            now_dt = self._now_dt()

            def create():
                groups = frozenset(json.loads(facility["affected_groups_json"]))
                effective, risk = derive_risk(severity, bool(facility["sole_accessible_route"]),
                                              groups)
                open_row = connection.execute(
                    "SELECT * FROM incidents WHERE facility_id=? AND status != 'closed' "
                    "ORDER BY opened_at DESC LIMIT 1",
                    (facility_id,),
                ).fetchone()
                last_closed = connection.execute(
                    "SELECT * FROM incidents WHERE facility_id=? AND status='closed' "
                    "ORDER BY opened_at DESC LIMIT 1",
                    (facility_id,),
                ).fetchone()
                guarantee_deadline = (
                    _parse_ts(last_closed["guarantee_until"], "guarantee_until")
                    if last_closed and last_closed["guarantee_until"] else None
                )

                if open_row is not None:
                    return self._repeat_report(connection, open_row=open_row, actor=actor,
                                               severity=severity, effective=effective, risk=risk,
                                               summary=summary, impact=impact or {}, now_dt=now_dt)
                if guarantee_deadline is not None and now_dt <= guarantee_deadline:
                    return self._reopen_recurrence(connection, closed=last_closed, actor=actor,
                                                   severity=severity, effective=effective,
                                                   risk=risk, summary=summary,
                                                   impact=impact or {}, now_dt=now_dt)

                contract = self._active_contract(connection, facility, now_dt)
                if contract is None:
                    raise ValidationError("设施当前无生效维护合同，无法冻结责任方，请先签订合同")
                sla = json.loads(contract["sla_json"])
                due_response = now_dt + timedelta(minutes=sla["response_minutes"][effective])
                due_measure = now_dt + timedelta(minutes=sla["measure_minutes"][effective])
                due_repair = now_dt + timedelta(hours=sla["repair_hours"][effective])
                incident_id = uuid.uuid4().hex
                recurrence_count = last_closed["recurrence_count"] + 1 if last_closed else 0
                continued_from = None
                if last_closed is not None:
                    continued_from = self._latest_work_order(
                        connection, last_closed["incident_id"])["work_order_id"]
                connection.execute(
                    "INSERT INTO incidents(incident_id,facility_id,site_id,status,reported_severity,"
                    "risk_level,impact_json,affected_groups_json,frozen_contractor_org,"
                    "frozen_contract_id,sla_json,opened_at,due_response_at,due_measure_at,"
                    "due_repair_at,escalation_level,recurrence_count,recurrence_of_incident_id,"
                    "reopened_at,reopened_by,basis_reinspection_id,guarantee_until,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (incident_id, facility_id, facility["site_id"], "open", severity, risk,
                     canonical_json(impact or {}), facility["affected_groups_json"],
                     contract["contractor_org_id"], contract["contract_id"],
                     canonical_json(sla), _iso(now_dt), _iso(due_response), _iso(due_measure),
                     _iso(due_repair), 0, recurrence_count,
                     last_closed["incident_id"] if last_closed is not None else None,
                     None, None, None, None, actor_id, self._now()),
                )
                incident = self._incident(connection, incident_id)
                work_order_id = self._create_work_order(
                    connection, incident=incident,
                    contractor_org_id=contract["contractor_org_id"],
                    contract_id=contract["contract_id"], kind="initial",
                    continued_from=continued_from, now_dt=now_dt, summary=summary,
                )
                connection.execute(
                    "UPDATE facilities SET available=0, available_note=?, current_incident_id=?, "
                    "version=version+1 WHERE facility_id=?",
                    (f"缺陷停用：{summary}", incident_id, facility_id),
                )
                detail = {"facility_id": facility_id, "reported_severity": severity,
                          "risk_level": risk, "effective_severity": effective,
                          "frozen_contractor_org": contract["contractor_org_id"],
                          "frozen_contract_id": contract["contract_id"],
                          "due_response_at": _iso(due_response),
                          "due_measure_at": _iso(due_measure),
                          "due_repair_at": _iso(due_repair),
                          "recurrence_count": recurrence_count,
                          "recurrence_of_incident_id": last_closed["incident_id"]
                          if last_closed is not None else None,
                          "summary": summary, "impact": impact or {}}
                seq = self._event(connection, incident_id=incident_id,
                                  event_type="defect.reported", actor_id=actor_id,
                                  detail=detail, work_order_id=work_order_id, now=_iso(now_dt))
                site = self._site(connection, facility["site_id"])
                self._notice_new_incident(connection=connection, site=site, contract=contract,
                                          incident_id=incident_id, event_seq=seq)
                return "incident", incident_id, {
                    "incident_id": incident_id, "work_order_id": work_order_id,
                    "mode": "new", "risk_level": risk,
                    "frozen_contractor_org": contract["contractor_org_id"]}

            receipt = self._idempotent(connection, request_id=request_id,
                                       action="a11y_report_defect", payload=payload,
                                       create=create)
            response = json.loads(connection.execute(
                "SELECT response_json FROM request_receipts WHERE request_id=?",
                (request_id,)).fetchone()["response_json"])
            return {**response, "replayed": receipt.replayed}

    def _repeat_report(self, connection, *, open_row, actor, severity, effective, risk,
                       summary, impact, now_dt):
        """对未关闭事件的重复报修：挂原链、自动升级，不允许新工单掩盖。"""

        incident_id = open_row["incident_id"]
        new_risk = SEVERITIES[max(SEVERITY_ORDER[risk], SEVERITY_ORDER[open_row["risk_level"]])]
        connection.execute(
            "UPDATE incidents SET risk_level=?, escalation_level=escalation_level+1 "
            "WHERE incident_id=?",
            (new_risk, incident_id),
        )
        detail = {"facility_id": open_row["facility_id"], "repeat": True,
                  "reported_severity": severity, "effective_severity": effective,
                  "risk_level": new_risk, "summary": summary, "impact": impact,
                  "frozen_contractor_org": open_row["frozen_contractor_org"]}
        seq = self._event(connection, incident_id=incident_id,
                          event_type="defect.repeat_reported", actor_id=actor.actor_id,
                          detail=detail, now=_iso(now_dt))
        site = self._site(connection, open_row["site_id"])
        kind, rid, label = self._manager_target(connection, site["organization_id"])
        self._notify(connection, incident_id=incident_id, event_seq=seq, kind=kind,
                     recipient_id=rid, label=label, reason="重复报修，自动升级")
        okind, orid, olabel = self._org_target(connection, open_row["frozen_contractor_org"])
        self._notify(connection, incident_id=incident_id, event_seq=seq, kind=okind,
                     recipient_id=orid, label=olabel, reason="重复报修，自动升级")
        return "incident", incident_id, {"incident_id": incident_id, "mode": "repeat",
                                         "risk_level": new_risk,
                                         "frozen_contractor_org": open_row["frozen_contractor_org"]}

    def _reopen_recurrence(self, connection, *, closed, actor, severity, effective, risk,
                           summary, impact, now_dt):
        """质保窗口内复发：重开同一事件链并延续复发责任。"""

        incident_id = closed["incident_id"]
        new_risk = SEVERITIES[max(SEVERITY_ORDER[risk], SEVERITY_ORDER[closed["risk_level"]])]
        connection.execute(
            "UPDATE incidents SET status='reopened', risk_level=?, "
            "recurrence_count=recurrence_count+1, reopened_at=?, reopened_by=?, "
            "escalation_level=escalation_level+1, basis_reinspection_id=NULL, guarantee_until=NULL "
            "WHERE incident_id=?",
            (new_risk, _iso(now_dt), actor.actor_id, incident_id),
        )
        incident = self._incident(connection, incident_id)
        previous = self._latest_work_order(connection, incident_id)
        work_order_id = self._create_work_order(
            connection, incident=incident,
            contractor_org_id=closed["frozen_contractor_org"],
            contract_id=closed["frozen_contract_id"], kind="recurrence",
            continued_from=previous["work_order_id"], now_dt=now_dt,
            summary=f"复发返工：{summary}"[:300],
        )
        connection.execute(
            "UPDATE facilities SET available=0, available_note=?, current_incident_id=?, "
            "version=version+1 WHERE facility_id=?",
            (f"整改后复发停用：{summary}", incident_id, closed["facility_id"]),
        )
        self._clear_resolved_flags(connection, incident_id)
        detail = {"facility_id": closed["facility_id"], "recurrence": True,
                  "reported_severity": severity, "risk_level": new_risk,
                  "recurrence_count": incident["recurrence_count"],
                  "frozen_contractor_org": closed["frozen_contractor_org"],
                  "previous_work_order_id": previous["work_order_id"],
                  "invalidated_basis_reinspection_id": closed["basis_reinspection_id"],
                  "summary": summary, "impact": impact}
        seq = self._event(connection, incident_id=incident_id,
                          event_type="defect.recurred", actor_id=actor.actor_id,
                          detail=detail, work_order_id=work_order_id, now=_iso(now_dt))
        site = self._site(connection, closed["site_id"])
        kind, rid, label = self._manager_target(connection, site["organization_id"])
        self._notify(connection, incident_id=incident_id, event_seq=seq, kind=kind,
                     recipient_id=rid, label=label, reason="整改后复发，原事件链重开")
        okind, orid, olabel = self._org_target(connection, closed["frozen_contractor_org"])
        self._notify(connection, incident_id=incident_id, event_seq=seq, kind=okind,
                     recipient_id=orid, label=olabel, reason="整改后复发，继续承担原链责任")
        return "incident", incident_id, {"incident_id": incident_id, "work_order_id": work_order_id,
                                         "mode": "recurrence", "risk_level": new_risk,
                                         "recurrence_count": incident["recurrence_count"],
                                         "frozen_contractor_org": closed["frozen_contractor_org"]}

    def _notice_new_incident(self, *, connection, site, contract, incident_id, event_seq):
        kind, rid, label = self._org_target(connection, contract["contractor_org_id"])
        self._notify(connection, incident_id=incident_id, event_seq=event_seq, kind=kind,
                     recipient_id=rid, label=label, reason="新报修，责任方与 SLA 已冻结")
        mkind, mrid, mlabel = self._manager_target(connection, site["organization_id"])
        self._notify(connection, incident_id=incident_id, event_seq=event_seq, kind=mkind,
                     recipient_id=mrid, label=mlabel, reason="新报修")

    # ------------------------------------------------------------------ 响应与维修

    def respond_to_work_order(self, *, request_id: str, actor_id: str, incident_id: str,
                              summary: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "incident_id": incident_id, "summary": summary}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            incident = self._incident(connection, incident_id)
            work_order = self._latest_work_order(connection, incident_id)
            self._require_contractor(actor, work_order)
            replayed = self._replayed(connection, request_id=request_id,
                                      action="a11y_respond", payload=payload)
            if replayed is not None:
                return replayed
            if work_order["status"] != "open":
                raise ConflictError("当前工单已响应或已结束")
            now = self._now()

            def create():
                connection.execute(
                    "UPDATE work_orders SET status='in_repair', responded_at=? WHERE work_order_id=?",
                    (now, work_order["work_order_id"]),
                )
                connection.execute(
                    "UPDATE incidents SET status='in_repair' WHERE incident_id=? "
                    "AND status IN ('open','reopened')",
                    (incident_id,),
                )
                self._resolve_flags_like(connection, incident_id,
                                         f"response:{work_order['work_order_id']}")
                detail = {"work_order_id": work_order["work_order_id"],
                          "contractor_org_id": work_order["contractor_org_id"],
                          "on_time": now <= incident["due_response_at"], "summary": summary}
                self._event(connection, incident_id=incident_id,
                            event_type="work_order.responded", actor_id=actor_id,
                            detail=detail, work_order_id=work_order["work_order_id"], now=now)
                return "work_order", work_order["work_order_id"], {
                    "work_order_id": work_order["work_order_id"], "responded_at": now}

            self._idempotent(connection, request_id=request_id, action="a11y_respond",
                             payload=payload, create=create)
            return {"incident_id": incident_id, "replayed": False}

    def activate_temporary_measure(self, *, request_id: str, actor_id: str, incident_id: str,
                                   kind: str, description: str, valid_from: str,
                                   valid_to: str, handover_rule: str,
                                   provider_org_id: str | None = None,
                                   supersedes_measure_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "incident_id": incident_id, "kind": kind,
                   "description": description, "valid_from": valid_from, "valid_to": valid_to,
                   "handover_rule": handover_rule, "provider_org_id": provider_org_id,
                   "supersedes_measure_id": supersedes_measure_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            incident = self._incident(connection, incident_id)
            facility = self._facility(connection, incident["facility_id"])
            site = self._site(connection, incident["site_id"])
            self._site_scope(actor, site)
            replayed = self._replayed(connection, request_id=request_id,
                                      action="a11y_activate_measure", payload=payload)
            if replayed is not None:
                return replayed
            if incident["status"] not in OPEN_STATUSES:
                raise ConflictError("事件已关闭，不能再登记临时措施")
            if kind not in MEASURE_KINDS:
                raise ValidationError("临时措施类型不被支持")
            if facility["sole_accessible_route"] and kind in NON_ROUTE_KINDS:
                raise ValidationError("该设施是唯一无障碍通道，仅张贴告示不能替代通行，必须安排真实替代")
            description = self._text(description, "description", 500)
            handover_rule = self._text(handover_rule, "handover_rule", 500)
            start = _parse_ts(valid_from, "valid_from")
            end = _parse_ts(valid_to, "valid_to")
            now_dt = self._now_dt()
            if end <= start:
                raise ValidationError("valid_to 必须晚于 valid_from")
            sla = json.loads(incident["sla_json"])
            if end - start > timedelta(hours=sla["measure_max_hours"]):
                raise ValidationError(
                    f"临时措施一次最长 {sla['measure_max_hours']} 小时，到期前必须交接或续设")
            provider = provider_org_id or site["organization_id"]
            if actor.role != "admin" and actor.organization_id != provider:
                raise PermissionDenied("只能由替代服务提供机构或枢纽管理方登记")
            active = connection.execute(
                "SELECT measure_id FROM temporary_measures WHERE incident_id=? AND status='active'",
                (incident_id,),
            ).fetchall()
            if active and supersedes_measure_id is None:
                raise ConflictError("已有生效中的替代承诺，续设必须指定被接替的措施")
            if supersedes_measure_id is not None:
                target = connection.execute(
                    "SELECT * FROM temporary_measures WHERE measure_id=? AND incident_id=?",
                    (supersedes_measure_id, incident_id),
                ).fetchone()
                if target is None:
                    raise NotFoundError("被接替的临时措施不存在")
                if target["status"] != "active":
                    raise ConflictError("只能接替仍在生效的措施")
                if start > _parse_ts(target["valid_to"], "valid_to"):
                    raise ValidationError("接替措施必须在原承诺到期前生效，不得出现空窗")

            def create():
                measure_id = uuid.uuid4().hex
                work_order = self._latest_work_order(connection, incident_id)
                connection.execute(
                    "INSERT INTO temporary_measures(measure_id,incident_id,work_order_id,"
                    "provider_org_id,kind,description,valid_from,valid_to,handover_rule,status,"
                    "supersedes_measure_id,handed_over_to,handed_over_at,handover_note,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (measure_id, incident_id, work_order["work_order_id"], provider, kind,
                     description, _iso(start), _iso(end), handover_rule, "active",
                     supersedes_measure_id, None, None, None, actor_id, self._now()),
                )
                if supersedes_measure_id is not None:
                    connection.execute(
                        "UPDATE temporary_measures SET status='superseded' WHERE measure_id=?",
                        (supersedes_measure_id,),
                    )
                self._resolve_flags_like(connection, incident_id, "measure")
                detail = {"measure_id": measure_id, "kind": kind, "description": description,
                          "provider_org_id": provider, "valid_from": _iso(start),
                          "valid_to": _iso(end), "handover_rule": handover_rule,
                          "supersedes_measure_id": supersedes_measure_id}
                seq = self._event(connection, incident_id=incident_id,
                                  event_type="measure.activated", actor_id=actor_id,
                                  detail=detail, measure_id=measure_id, now=_iso(now_dt))
                mkind, mrid, mlabel = self._manager_target(connection, site["organization_id"])
                self._notify(connection, incident_id=incident_id, event_seq=seq, kind=mkind,
                             recipient_id=mrid, label=mlabel, reason="临时替代服务已启用")
                self._notify(connection, incident_id=incident_id, event_seq=seq,
                             kind="group", recipient_id="affected_passengers",
                             label="行动不便旅客", reason="临时替代服务已启用",
                             channel="station_notice")
                if supersedes_measure_id is not None:
                    self._event(connection, incident_id=incident_id,
                                event_type="measure.superseded", actor_id=actor_id,
                                detail={"measure_id": supersedes_measure_id,
                                        "by_measure_id": measure_id},
                                measure_id=supersedes_measure_id, now=_iso(now_dt))
                return "temporary_measure", measure_id, {
                    "measure_id": measure_id, "valid_to": _iso(end)}

            self._idempotent(connection, request_id=request_id,
                             action="a11y_activate_measure", payload=payload, create=create)
            return {"incident_id": incident_id, "replayed": False}

    def handover_measure(self, *, request_id: str, actor_id: str, measure_id: str,
                         to_party: str, note: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "measure_id": measure_id, "to_party": to_party,
                   "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            row = connection.execute(
                "SELECT * FROM temporary_measures WHERE measure_id=?", (measure_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("临时措施不存在")
            incident = self._incident(connection, row["incident_id"])
            site = self._site(connection, incident["site_id"])
            self._site_scope(actor, site)
            replayed = self._replayed(connection, request_id=request_id,
                                      action="a11y_handover_measure", payload=payload)
            if replayed is not None:
                return replayed
            if row["status"] != "active":
                raise ConflictError("措施已结束，不能交接")
            to_party = self._text(to_party, "to_party", 200)
            note = self._text(note, "note", 500)
            now = self._now()

            def create():
                connection.execute(
                    "UPDATE temporary_measures SET handed_over_to=?, handed_over_at=?, "
                    "handover_note=? WHERE measure_id=?",
                    (to_party, now, note, measure_id),
                )
                detail = {"measure_id": measure_id, "handed_over_to": to_party,
                          "handover_rule": row["handover_rule"], "note": note,
                          "valid_to": row["valid_to"]}
                seq = self._event(connection, incident_id=row["incident_id"],
                                  event_type="measure.handed_over", actor_id=actor_id,
                                  detail=detail, measure_id=measure_id, now=now)
                self._notify(connection, incident_id=row["incident_id"], event_seq=seq,
                             kind="external_party", recipient_id=to_party, label=to_party,
                             reason="临时替代服务交接，须按原承诺到期时间续接")
                return "temporary_measure", measure_id, {"measure_id": measure_id,
                                                          "handed_over_at": now}

            self._idempotent(connection, request_id=request_id,
                             action="a11y_handover_measure", payload=payload, create=create)
            return {"measure_id": measure_id, "replayed": False}

    def complete_repair(self, *, request_id: str, actor_id: str, incident_id: str,
                        summary: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "incident_id": incident_id, "summary": summary}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            incident = self._incident(connection, incident_id)
            work_order = self._latest_work_order(connection, incident_id)
            self._require_contractor(actor, work_order)
            replayed = self._replayed(connection, request_id=request_id,
                                      action="a11y_complete_repair", payload=payload)
            if replayed is not None:
                return replayed
            if incident["status"] not in OPEN_STATUSES:
                raise ConflictError("当前事件不在维修环节")
            if work_order["status"] not in ("open", "in_repair"):
                raise ConflictError("当前工单已走完维修环节")
            summary = self._text(summary, "summary", 500)
            sla = json.loads(incident["sla_json"])
            now_dt = self._now_dt()
            due_reinspect = now_dt + timedelta(hours=sla["reinspection_hours"])

            def create():
                if work_order["responded_at"] is None:
                    connection.execute(
                        "UPDATE work_orders SET responded_at=? WHERE work_order_id=?",
                        (_iso(now_dt), work_order["work_order_id"]),
                    )
                connection.execute(
                    "UPDATE work_orders SET status='completed', completed_at=?, completed_by=?, "
                    "due_reinspect_at=?, summary=? WHERE work_order_id=?",
                    (_iso(now_dt), actor_id, _iso(due_reinspect), summary,
                     work_order["work_order_id"]),
                )
                connection.execute(
                    "UPDATE incidents SET status='awaiting_reinspection' WHERE incident_id=?",
                    (incident_id,),
                )
                self._resolve_flags_like(connection, incident_id,
                                         f"repair:{work_order['work_order_id']}")
                self._resolve_flags_like(connection, incident_id,
                                         f"response:{work_order['work_order_id']}")
                detail = {"work_order_id": work_order["work_order_id"],
                          "repair_on_time": _iso(now_dt) <= incident["due_repair_at"],
                          "due_reinspect_at": _iso(due_reinspect), "summary": summary}
                seq = self._event(connection, incident_id=incident_id,
                                  event_type="repair.completed", actor_id=actor_id,
                                  detail=detail, work_order_id=work_order["work_order_id"],
                                  now=_iso(now_dt))
                site = self._site(connection, incident["site_id"])
                kind, rid, label = self._manager_target(connection, site["organization_id"])
                self._notify(connection, incident_id=incident_id, event_seq=seq, kind=kind,
                             recipient_id=rid, label=label, reason="维修完成，等待独立复检")
                return "work_order", work_order["work_order_id"], {
                    "work_order_id": work_order["work_order_id"],
                    "due_reinspect_at": _iso(due_reinspect)}

            self._idempotent(connection, request_id=request_id,
                             action="a11y_complete_repair", payload=payload, create=create)
            return {"incident_id": incident_id, "replayed": False}

    # ------------------------------------------------------------------ 独立复检与复开

    def submit_reinspection(self, *, request_id: str, actor_id: str, incident_id: str,
                            result: str, evidence: list[dict[str, str]],
                            note: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "incident_id": incident_id, "result": result,
                   "evidence": evidence, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            incident = self._incident(connection, incident_id)
            work_order = self._latest_work_order(connection, incident_id)
            replayed = self._replayed(connection, request_id=request_id,
                                      action="a11y_reinspection", payload=payload)
            if replayed is not None:
                return replayed
            if incident["status"] != "awaiting_reinspection" or work_order["status"] != "completed":
                raise ConflictError("只能对已完成维修、待复检的工单提交复检")
            if actor.organization_id in self._contractor_orgs(connection, incident_id):
                raise PermissionDenied("复检人必须独立于所有承接过该事件的维护机构")
            if result not in ("pass", "fail"):
                raise ValidationError("result 必须是 pass 或 fail")
            evidence = self._validate_evidence(evidence)
            note = self._text(note, "note", 500)
            now_dt = self._now_dt()
            sla = json.loads(incident["sla_json"])

            def create():
                reinspection_id = uuid.uuid4().hex
                evidence_hash = digest(evidence)
                connection.execute(
                    "INSERT INTO reinspections(reinspection_id,incident_id,work_order_id,"
                    "inspector_actor_id,inspector_org_id,result,independent,evidence_json,"
                    "evidence_hash,note,withdrawn,created_at) VALUES(?,?,?,?,?,?,1,?,?,?,0,?)",
                    (reinspection_id, incident_id, work_order["work_order_id"], actor_id,
                     actor.organization_id, result, canonical_json(evidence), evidence_hash,
                     note, _iso(now_dt)),
                )
                detail = {"work_order_id": work_order["work_order_id"],
                          "reinspection_id": reinspection_id, "result": result,
                          "inspector_actor_id": actor_id,
                          "inspector_org_id": actor.organization_id,
                          "evidence_hash": evidence_hash, "evidence_count": len(evidence),
                          "note": note}
                if result == "pass":
                    self._apply_pass(connection, incident=incident, work_order=work_order,
                                     reinspection_id=reinspection_id, actor=actor,
                                     detail=detail, now_dt=now_dt, sla=sla)
                else:
                    self._apply_fail(connection, incident=incident, work_order=work_order,
                                     reinspection_id=reinspection_id, actor=actor,
                                     detail=detail, now_dt=now_dt)
                return "reinspection", reinspection_id, {
                    "reinspection_id": reinspection_id, "result": result}

            self._idempotent(connection, request_id=request_id, action="a11y_reinspection",
                             payload=payload, create=create)
            return {"incident_id": incident_id, "replayed": False}

    def _validate_evidence(self, evidence: list[dict[str, str]]) -> list[dict[str, str]]:
        if not isinstance(evidence, list) or not evidence:
            raise ValidationError("复检必须上传至少一份证据")
        cleaned = []
        seen = set()
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                raise ValidationError(f"证据 {index} 必须是对象")
            ref = str(item.get("evidence_id", "")).strip()
            kind = str(item.get("type", "")).strip()
            uri = str(item.get("uri", "")).strip()
            if not ref or not kind or not uri:
                raise ValidationError(f"证据 {index} 缺少 evidence_id/type/uri")
            if ref in seen:
                raise ValidationError(f"证据编号重复: {ref}")
            seen.add(ref)
            cleaned.append({"evidence_id": ref, "type": kind, "uri": uri,
                            "sha256": str(item.get("sha256", "")).strip()})
        return cleaned

    def _apply_pass(self, connection, *, incident, work_order, reinspection_id, actor,
                    detail, now_dt, sla):
        guarantee_until = now_dt + timedelta(hours=sla["guarantee_hours"])
        connection.execute(
            "UPDATE work_orders SET status='passed' WHERE work_order_id=?",
            (work_order["work_order_id"],),
        )
        connection.execute(
            "UPDATE incidents SET status='closed', basis_reinspection_id=?, guarantee_until=? "
            "WHERE incident_id=?",
            (reinspection_id, _iso(guarantee_until), incident["incident_id"]),
        )
        connection.execute(
            "UPDATE facilities SET available=1, available_note=?, current_incident_id=NULL, "
            "version=version+1 WHERE facility_id=?",
            (f"复检通过复开：{reinspection_id}", incident["facility_id"]),
        )
        ended = []
        for measure in connection.execute(
            "SELECT measure_id FROM temporary_measures WHERE incident_id=? AND status='active'",
            (incident["incident_id"],),
        ).fetchall():
            connection.execute(
                "UPDATE temporary_measures SET status='ended_on_reopen' WHERE measure_id=?",
                (measure["measure_id"],),
            )
            ended.append(measure["measure_id"])
        self._resolve_flags_like(connection, incident["incident_id"], "reinspection")
        self._resolve_flags_like(connection, incident["incident_id"], "measure")
        seq = self._event(connection, incident_id=incident["incident_id"],
                          event_type="reinspection.passed", actor_id=actor.actor_id,
                          detail={**detail, "guarantee_until": _iso(guarantee_until),
                                  "ended_measure_ids": ended},
                          work_order_id=work_order["work_order_id"],
                          reinspection_id=reinspection_id, now=_iso(now_dt))
        site = self._site(connection, incident["site_id"])
        kind, rid, label = self._manager_target(connection, site["organization_id"])
        self._notify(connection, incident_id=incident["incident_id"], event_seq=seq,
                     kind=kind, recipient_id=rid, label=label,
                     reason="独立复检通过，设施复开")
        for measure_id in ended:
            self._event(connection, incident_id=incident["incident_id"],
                        event_type="measure.ended_on_reopen", actor_id=actor.actor_id,
                        detail={"measure_id": measure_id}, measure_id=measure_id,
                        now=_iso(now_dt))

    def _apply_fail(self, connection, *, incident, work_order, reinspection_id, actor,
                    detail, now_dt):
        connection.execute(
            "UPDATE work_orders SET status='failed' WHERE work_order_id=?",
            (work_order["work_order_id"],),
        )
        connection.execute(
            "UPDATE incidents SET status='in_repair', escalation_level=escalation_level+1 "
            "WHERE incident_id=?",
            (incident["incident_id"],),
        )
        rework_id = self._create_work_order(
            connection, incident=incident,
            contractor_org_id=work_order["contractor_org_id"],
            contract_id=work_order["contract_id"], kind="rework",
            continued_from=work_order["work_order_id"], now_dt=now_dt,
            summary=f"复检不合格返工：{detail['note']}"[:300],
        )
        seq = self._event(connection, incident_id=incident["incident_id"],
                          event_type="reinspection.failed", actor_id=actor.actor_id,
                          detail={**detail, "rework_work_order_id": rework_id},
                          work_order_id=work_order["work_order_id"],
                          reinspection_id=reinspection_id, now=_iso(now_dt))
        site = self._site(connection, incident["site_id"])
        kind, rid, label = self._manager_target(connection, site["organization_id"])
        self._notify(connection, incident_id=incident["incident_id"], event_seq=seq,
                     kind=kind, recipient_id=rid, label=label,
                     reason="复检不合格，沿原事件链返工并升级")
        okind, orid, olabel = self._org_target(connection, work_order["contractor_org_id"])
        self._notify(connection, incident_id=incident["incident_id"], event_seq=seq,
                     kind=okind, recipient_id=orid, label=olabel,
                     reason="复检不合格，继续返工，原时限不变")

    def withdraw_reinspection(self, *, request_id: str, actor_id: str,
                              reinspection_id: str, reason: str) -> dict[str, Any]:
        """撤回复检证据；若该复检曾是关单依据，立即作废复开并重开原链。"""

        payload = {"actor_id": actor_id, "reinspection_id": reinspection_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            row = connection.execute(
                "SELECT * FROM reinspections WHERE reinspection_id=?", (reinspection_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("复检记录不存在")
            incident = self._incident(connection, row["incident_id"])
            replayed = self._replayed(connection, request_id=request_id,
                                      action="a11y_withdraw_reinspection", payload=payload)
            if replayed is not None:
                return replayed
            if actor.organization_id in self._contractor_orgs(connection, row["incident_id"]):
                raise PermissionDenied("维护机构不能撤回复检结论")
            if row["withdrawn"]:
                raise ConflictError("该复检证据已经撤回")
            reason = self._text(reason, "reason", 500)
            now_dt = self._now_dt()

            def create():
                connection.execute(
                    "UPDATE reinspections SET withdrawn=1, withdrawn_by=?, withdrawn_at=?, "
                    "withdrawn_reason=? WHERE reinspection_id=?",
                    (actor_id, _iso(now_dt), reason, reinspection_id),
                )
                was_basis = incident["basis_reinspection_id"] == reinspection_id
                detail = {"reinspection_id": reinspection_id, "was_basis": was_basis,
                          "reason": reason, "inspector_org_id": actor.organization_id}
                if was_basis:
                    self._reopen_after_withdrawal(connection, incident=incident,
                                                  reinspection=row, actor=actor,
                                                  detail=detail, now_dt=now_dt)
                else:
                    self._event(connection, incident_id=incident["incident_id"],
                                event_type="reinspection.withdrawn", actor_id=actor_id,
                                detail=detail, reinspection_id=reinspection_id,
                                now=_iso(now_dt))
                return "reinspection", reinspection_id, {
                    "reinspection_id": reinspection_id, "reopened": was_basis}

            self._idempotent(connection, request_id=request_id,
                             action="a11y_withdraw_reinspection", payload=payload,
                             create=create)
            response = json.loads(connection.execute(
                "SELECT response_json FROM request_receipts WHERE request_id=?",
                (self.domain._identifier(request_id, "request_id"),)
            ).fetchone()["response_json"])
            return {**response, "replayed": False}

    def _reopen_after_withdrawal(self, connection, *, incident, reinspection, actor,
                                 detail, now_dt):
        prior = connection.execute(
            "SELECT * FROM work_orders WHERE work_order_id=?",
            (reinspection["work_order_id"],),
        ).fetchone()
        connection.execute(
            "UPDATE incidents SET status='reopened', basis_reinspection_id=NULL, "
            "guarantee_until=NULL, escalation_level=escalation_level+1 WHERE incident_id=?",
            (incident["incident_id"],),
        )
        connection.execute(
            "UPDATE facilities SET available=0, available_note=?, current_incident_id=?, "
            "version=version+1 WHERE facility_id=?",
            ("复检依据撤回，复开作废", incident["incident_id"], incident["facility_id"]),
        )
        self._clear_resolved_flags(connection, incident["incident_id"])
        work_order_id = self._create_work_order(
            connection, incident=incident,
            contractor_org_id=prior["contractor_org_id"],
            contract_id=prior["contract_id"], kind="evidence_withdrawn",
            continued_from=reinspection["work_order_id"], now_dt=now_dt,
            summary="验收依据撤回，重新整改复检",
        )
        seq = self._event(connection, incident_id=incident["incident_id"],
                          event_type="reinspection.withdrawn", actor_id=actor.actor_id,
                          detail={**detail, "rework_work_order_id": work_order_id},
                          work_order_id=work_order_id,
                          reinspection_id=reinspection["reinspection_id"], now=_iso(now_dt))
        site = self._site(connection, incident["site_id"])
        kind, rid, label = self._manager_target(connection, site["organization_id"])
        self._notify(connection, incident_id=incident["incident_id"], event_seq=seq,
                     kind=kind, recipient_id=rid, label=label,
                     reason="复检证据撤回，旧验收单不得再作为关单依据")
        okind, orid, olabel = self._org_target(connection, incident["frozen_contractor_org"])
        self._notify(connection, incident_id=incident["incident_id"], event_seq=seq,
                     kind=okind, recipient_id=orid, label=olabel,
                     reason="验收依据撤回，事件重新打开")

    # ------------------------------------------------------------------ 跨单位移交

    def transfer_responsibility(self, *, request_id: str, actor_id: str, incident_id: str,
                                target_contract_id: str, reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "incident_id": incident_id,
                   "target_contract_id": target_contract_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            incident = self._incident(connection, incident_id)
            replayed = self._replayed(connection, request_id=request_id,
                                      action="a11y_transfer", payload=payload)
            if replayed is not None:
                return replayed
            if incident["status"] not in OPEN_STATUSES:
                raise ConflictError("事件已关闭，不能移交")
            facility = self._facility(connection, incident["facility_id"])
            now_dt = self._now_dt()
            target = connection.execute(
                "SELECT * FROM maintenance_contracts WHERE contract_id=? AND site_id=? "
                "AND valid_from<=? AND valid_to>=?",
                (target_contract_id, facility["site_id"], _iso(now_dt), _iso(now_dt)),
            ).fetchone()
            if target is None:
                raise NotFoundError("目标合同不存在或已失效")
            if target["facility_id"] is not None and target["facility_id"] != facility["facility_id"]:
                raise ValidationError("目标合同不覆盖该设施")
            previous = self._latest_work_order(connection, incident_id)
            if previous["contractor_org_id"] == target["contractor_org_id"]:
                raise ConflictError("目标机构与当前承接机构相同，无需移交")
            reason = self._text(reason, "reason", 500)

            def create():
                work_order_id = self._create_work_order(
                    connection, incident=incident,
                    contractor_org_id=target["contractor_org_id"],
                    contract_id=target["contract_id"], kind="transfer",
                    continued_from=previous["work_order_id"], now_dt=now_dt,
                    summary=f"跨单位移交承接：{reason}"[:300],
                )
                detail = {"from_contractor_org_id": previous["contractor_org_id"],
                          "from_contract_id": previous["contract_id"],
                          "to_contractor_org_id": target["contractor_org_id"],
                          "to_contract_id": target["contract_id"],
                          "new_work_order_id": work_order_id,
                          "frozen_contractor_org_unchanged": incident["frozen_contractor_org"],
                          "due_response_at_unchanged": incident["due_response_at"],
                          "due_repair_at_unchanged": incident["due_repair_at"],
                          "reason": reason}
                seq = self._event(connection, incident_id=incident_id,
                                  event_type="responsibility.transferred", actor_id=actor_id,
                                  detail=detail, work_order_id=work_order_id, now=_iso(now_dt))
                for org_id, why in (
                    (previous["contractor_org_id"], "责任移交：此前逾期环节仍由原机构负责"),
                    (target["contractor_org_id"], "责任移交：承接剩余时限，原 SLA 时钟不变"),
                ):
                    kind, rid, label = self._org_target(connection, org_id)
                    self._notify(connection, incident_id=incident_id, event_seq=seq,
                                 kind=kind, recipient_id=rid, label=label, reason=why)
                return "work_order", work_order_id, {"work_order_id": work_order_id,
                                                     "to_contractor_org_id": target["contractor_org_id"]}

            self._idempotent(connection, request_id=request_id, action="a11y_transfer",
                             payload=payload, create=create)
            return {"incident_id": incident_id, "replayed": False}

    # ------------------------------------------------------------------ 定时扫描

    def run_sweep(self) -> dict[str, Any]:
        """过期替代承诺、各 SLA 环节逾期与升级；可重复执行且幂等。"""

        with self.database.transaction(immediate=True) as connection:
            now_dt = self._now_dt()
            stamp = _iso(now_dt)
            expired_measures = []
            for measure in connection.execute(
                "SELECT * FROM temporary_measures WHERE status='active' AND valid_to<=?",
                (stamp,),
            ).fetchall():
                connection.execute(
                    "UPDATE temporary_measures SET status='expired' WHERE measure_id=?",
                    (measure["measure_id"],),
                )
                seq = self._event(connection, incident_id=measure["incident_id"],
                                  event_type="measure.expired", actor_id="system",
                                  detail={"measure_id": measure["measure_id"],
                                          "valid_to": measure["valid_to"],
                                          "handover_rule": measure["handover_rule"],
                                          "handed_over_to": measure["handed_over_to"]},
                                  measure_id=measure["measure_id"], now=stamp)
                incident = self._incident(connection, measure["incident_id"])
                facility = self._facility(connection, incident["facility_id"])
                site = self._site(connection, incident["site_id"])
                kind, rid, label = self._manager_target(connection, site["organization_id"])
                self._notify(connection, incident_id=incident["incident_id"], event_seq=seq,
                             kind=kind, recipient_id=rid, label=label,
                             reason="替代承诺已到期，须立即续设或安排交接")
                if facility["sole_accessible_route"]:
                    self._notify(connection, incident_id=incident["incident_id"], event_seq=seq,
                                 kind="group", recipient_id="affected_passengers",
                                 label="行动不便旅客", reason="唯一无障碍通道的替代服务已到期",
                                 channel="station_notice")
                expired_measures.append(measure["measure_id"])

            escalations = []
            for overdue in self._detect_overdue(connection, now_dt):
                if not self._flag_overdue(connection, incident_id=overdue["incident_id"],
                                          key=overdue["flag_key"], level=1):
                    continue
                connection.execute(
                    "UPDATE incidents SET escalation_level=escalation_level+1 "
                    "WHERE incident_id=?",
                    (overdue["incident_id"],),
                )
                incident = self._incident(connection, overdue["incident_id"])
                seq = self._event(connection, incident_id=overdue["incident_id"],
                                  event_type="sla.overdue", actor_id="system",
                                  detail={**overdue, "escalation_level": incident["escalation_level"]},
                                  work_order_id=overdue.get("work_order_id"), now=stamp)
                site = self._site(connection, incident["site_id"])
                kind, rid, label = self._manager_target(connection, site["organization_id"])
                self._notify(connection, incident_id=overdue["incident_id"], event_seq=seq,
                             kind=kind, recipient_id=rid, label=label,
                             reason=f"环节逾期：{overdue['stage']}")
                if overdue.get("responsible_org"):
                    okind, orid, olabel = self._org_target(connection, overdue["responsible_org"])
                    self._notify(connection, incident_id=overdue["incident_id"], event_seq=seq,
                                 kind=okind, recipient_id=orid, label=olabel,
                                 reason=f"环节逾期：{overdue['stage']}")
                if incident["escalation_level"] >= 3:
                    self._notify(connection, incident_id=overdue["incident_id"], event_seq=seq,
                                 kind="role", recipient_id="regulator", label="监管方",
                                 reason=f"多次逾期升级：{overdue['stage']}")
                escalations.append(overdue)
            return {"expired_measures": expired_measures,
                    "escalation_count": len(escalations),
                    "escalations": escalations, "swept_at": stamp}

    def _detect_overdue(self, connection, now_dt) -> list[dict[str, Any]]:
        stamp = _iso(now_dt)
        found: list[dict[str, Any]] = []
        for inc in connection.execute(
            "SELECT * FROM incidents WHERE status != 'closed'"
        ).fetchall():
            for wo in connection.execute(
                "SELECT * FROM work_orders WHERE incident_id=? AND responded_at IS NULL "
                "AND kind!='rework' "
                "AND due_response_at IS NOT NULL AND due_response_at<?",
                (inc["incident_id"], stamp),
            ).fetchall():
                found.append(self._overdue_row(inc, wo, "response", wo["due_response_at"], stamp))
            if stamp > inc["due_measure_at"]:
                active = connection.execute(
                    "SELECT 1 FROM temporary_measures WHERE incident_id=? AND status='active' "
                    "AND valid_to>? LIMIT 1",
                    (inc["incident_id"], stamp),
                ).fetchone()
                if active is None:
                    found.append({
                        "incident_id": inc["incident_id"], "site_id": inc["site_id"],
                        "facility_id": inc["facility_id"], "stage": "temporary_measure",
                        "flag_key": f"measure:{inc['due_measure_at']}",
                        "work_order_id": None, "deadline": inc["due_measure_at"],
                        "overdue_minutes": minutes_between(inc["due_measure_at"], stamp),
                        "responsible_org": inc["frozen_contractor_org"],
                    })
            for wo in connection.execute(
                "SELECT * FROM work_orders WHERE incident_id=? AND completed_at IS NULL "
                "AND due_repair_at IS NOT NULL AND due_repair_at<?",
                (inc["incident_id"], stamp),
            ).fetchall():
                found.append(self._overdue_row(inc, wo, "repair", wo["due_repair_at"], stamp))
            for wo in connection.execute(
                "SELECT * FROM work_orders WHERE incident_id=? AND status='completed' "
                "AND due_reinspect_at IS NOT NULL AND due_reinspect_at<?",
                (inc["incident_id"], stamp),
            ).fetchall():
                site = self._site(connection, inc["site_id"])
                found.append(self._overdue_row(inc, wo, "reinspection", wo["due_reinspect_at"],
                                               stamp, org=site["organization_id"]))
        return found

    def _overdue_row(self, inc, wo, stage, deadline, stamp, org=None):
        return {
            "incident_id": inc["incident_id"], "site_id": inc["site_id"],
            "facility_id": inc["facility_id"], "stage": stage,
            "flag_key": f"{stage}:{wo['work_order_id']}",
            "work_order_id": wo["work_order_id"], "contract_id": wo["contract_id"],
            "deadline": deadline,
            "overdue_minutes": minutes_between(deadline, stamp),
            "responsible_org": org or wo["contractor_org_id"],
        }

    # ------------------------------------------------------------------ 查询

    def available_routes(self, site_id: str) -> dict[str, Any]:
        """值班经理视角：当前可用路线、替代路线与不可用节点。"""

        now = self._now()
        with self.database.transaction() as connection:
            self._site(connection, site_id)
            routes = []
            for facility in connection.execute(
                "SELECT * FROM facilities WHERE site_id=? ORDER BY location, facility_id",
                (site_id,),
            ).fetchall():
                if facility["facility_type"] not in ROUTE_FACILITY_TYPES:
                    continue
                measures = connection.execute(
                    "SELECT * FROM temporary_measures WHERE incident_id IN "
                    "(SELECT incident_id FROM incidents WHERE facility_id=?) "
                    "AND status='active' AND valid_from<=? AND valid_to>? "
                    "ORDER BY valid_from DESC",
                    (facility["facility_id"], now, now),
                ).fetchall()
                if facility["available"]:
                    status = "available"
                elif measures:
                    status = "alternative"
                else:
                    status = "unavailable"
                routes.append({
                    "facility_id": facility["facility_id"],
                    "facility_type": facility["facility_type"],
                    "location": facility["location"],
                    "sole_accessible_route": bool(facility["sole_accessible_route"]),
                    "status": status,
                    "incident_id": facility["current_incident_id"],
                    "active_measures": [{
                        "measure_id": m["measure_id"], "kind": m["kind"],
                        "description": m["description"], "valid_to": m["valid_to"],
                        "provider_org_id": m["provider_org_id"],
                        "handed_over_to": m["handed_over_to"],
                    } for m in measures],
                })
            return {"site_id": site_id, "checked_at": now, "routes": routes}

    def expiring_commitments(self, site_id: str, within_hours: float = 24) -> dict[str, Any]:
        """即将到期但尚未交接/续设的替代承诺。"""

        now_dt = self._now_dt()
        now = _iso(now_dt)
        horizon = _iso(now_dt + timedelta(hours=within_hours))
        with self.database.transaction() as connection:
            self._site(connection, site_id)
            rows = connection.execute(
                "SELECT m.* FROM temporary_measures m JOIN incidents i ON i.incident_id=m.incident_id "
                "WHERE i.site_id=? AND m.status='active' AND m.valid_to>? AND m.valid_to<=? "
                "ORDER BY m.valid_to",
                (site_id, now, horizon),
            ).fetchall()
            return {"site_id": site_id, "checked_at": now, "within_hours": within_hours,
                    "items": [{
                        "measure_id": r["measure_id"], "incident_id": r["incident_id"],
                        "kind": r["kind"], "description": r["description"],
                        "valid_to": r["valid_to"],
                        "minutes_remaining": max(0, minutes_between(now, r["valid_to"])),
                        "handover_rule": r["handover_rule"],
                        "handed_over_to": r["handed_over_to"],
                        "provider_org_id": r["provider_org_id"],
                    } for r in rows]}

    def overdue_board(self, site_id: str) -> dict[str, Any]:
        """每个逾期环节及其责任机构，含尚未跑 sweep 的新逾期。"""

        now_dt = self._now_dt()
        with self.database.transaction() as connection:
            self._site(connection, site_id)
            rows = [r for r in self._detect_overdue(connection, now_dt)
                    if r["site_id"] == site_id]
            for row in rows:
                stored = connection.execute(
                    "SELECT 1 FROM overdue_flags WHERE incident_id=? AND flag_key=? "
                    "AND resolved_at IS NULL",
                    (row["incident_id"], row["flag_key"]),
                ).fetchone()
                row["already_escalated"] = stored is not None
                row["escalation_level"] = connection.execute(
                    "SELECT escalation_level FROM incidents WHERE incident_id=?",
                    (row["incident_id"],),
                ).fetchone()["escalation_level"]
            rows.sort(key=lambda r: -r["overdue_minutes"])
            return {"site_id": site_id, "checked_at": _iso(now_dt), "items": rows}

    def get_incident(self, incident_id: str) -> dict[str, Any]:
        """导出完整事件链：工单、措施、复检、通知、逾期标记一屏可见。"""

        with self.database.transaction() as connection:
            inc = self._incident(connection, incident_id)
            work_orders = [dict_wo(r) for r in connection.execute(
                "SELECT * FROM work_orders WHERE incident_id=? ORDER BY seq", (incident_id,))]
            measures = [dict_measure(r) for r in connection.execute(
                "SELECT * FROM temporary_measures WHERE incident_id=? ORDER BY valid_from",
                (incident_id,))]
            reinspections = [dict_reinspection(r) for r in connection.execute(
                "SELECT * FROM reinspections WHERE incident_id=? ORDER BY created_at",
                (incident_id,))]
            timeline = [{
                "seq": r["seq"], "event_type": r["event_type"], "actor_id": r["actor_id"],
                "work_order_id": r["work_order_id"], "measure_id": r["measure_id"],
                "reinspection_id": r["reinspection_id"],
                "detail": json.loads(r["detail_json"]), "created_at": r["created_at"],
            } for r in connection.execute(
                "SELECT * FROM incident_events WHERE incident_id=? ORDER BY seq", (incident_id,))]
            notifications = [dict_notification(r) for r in connection.execute(
                "SELECT * FROM notifications WHERE incident_id=? ORDER BY created_at",
                (incident_id,))]
            flags = [dict_flag(r) for r in connection.execute(
                "SELECT * FROM overdue_flags WHERE incident_id=? ORDER BY first_flagged_at",
                (incident_id,))]
            return {
                "incident_id": incident_id,
                "facility_id": inc["facility_id"], "site_id": inc["site_id"],
                "status": inc["status"], "reported_severity": inc["reported_severity"],
                "risk_level": inc["risk_level"],
                "affected_groups": json.loads(inc["affected_groups_json"]),
                "impact": json.loads(inc["impact_json"]),
                "frozen_contractor_org": inc["frozen_contractor_org"],
                "frozen_contract_id": inc["frozen_contract_id"],
                "opened_at": inc["opened_at"],
                "due_response_at": inc["due_response_at"],
                "due_measure_at": inc["due_measure_at"],
                "due_repair_at": inc["due_repair_at"],
                "escalation_level": inc["escalation_level"],
                "recurrence_count": inc["recurrence_count"],
                "recurrence_of_incident_id": inc["recurrence_of_incident_id"],
                "reopened_at": inc["reopened_at"], "reopened_by": inc["reopened_by"],
                "basis_reinspection_id": inc["basis_reinspection_id"],
                "guarantee_until": inc["guarantee_until"],
                "work_orders": work_orders, "temporary_measures": measures,
                "reinspections": reinspections, "timeline": timeline,
                "notifications": notifications, "overdue_flags": flags,
            }

    def snapshot_facility_at(self, facility_id: str, at: str) -> dict[str, Any]:
        """按任一日期还原：设施状态、替代承诺、通知对象、验收依据与复发责任。"""

        point = _parse_ts(at, "at")
        stamp = _iso(point)
        with self.database.transaction() as connection:
            facility = self._facility(connection, facility_id)
            incidents = connection.execute(
                "SELECT * FROM incidents WHERE facility_id=? AND created_at<=? ORDER BY opened_at",
                (facility_id, stamp),
            ).fetchall()
            availability = "available"
            availability_reason = "登记即开放"
            incident_views = []
            for inc in incidents:
                events = connection.execute(
                    "SELECT * FROM incident_events WHERE incident_id=? AND created_at<=? ORDER BY seq",
                    (inc["incident_id"], stamp),
                ).fetchall()
                if not events:
                    continue
                for event in events:
                    etype = event["event_type"]
                    if etype in ("defect.reported", "defect.recurred"):
                        availability = "unavailable"
                        availability_reason = etype
                    elif etype == "reinspection.failed":
                        availability = "unavailable"
                        availability_reason = etype
                    elif etype == "reinspection.passed":
                        detail = json.loads(event["detail_json"])
                        withdrawn = connection.execute(
                            "SELECT 1 FROM reinspections WHERE reinspection_id=? AND withdrawn=1 "
                            "AND withdrawn_at<=?",
                            (detail["reinspection_id"], stamp),
                        ).fetchone()
                        if not withdrawn:
                            availability = "available"
                            availability_reason = f"复检通过：{detail['reinspection_id']}"
                    elif etype == "reinspection.withdrawn":
                        detail = json.loads(event["detail_json"])
                        if detail.get("was_basis"):
                            availability = "unavailable"
                            availability_reason = "复检依据撤回"

                active_measures = []
                for event in events:
                    if event["event_type"] != "measure.activated":
                        continue
                    detail = json.loads(event["detail_json"])
                    mid = detail["measure_id"]
                    if detail["valid_from"] > stamp or detail["valid_to"] <= stamp:
                        continue
                    ended = connection.execute(
                        "SELECT 1 FROM incident_events WHERE incident_id=? AND measure_id=? "
                        "AND event_type IN ('measure.superseded','measure.expired',"
                        "'measure.ended_on_reopen') AND created_at<=? LIMIT 1",
                        (inc["incident_id"], mid, stamp),
                    ).fetchone()
                    if ended:
                        continue
                    handover = connection.execute(
                        "SELECT detail_json FROM incident_events WHERE incident_id=? AND measure_id=? "
                        "AND event_type='measure.handed_over' AND created_at<=? "
                        "ORDER BY seq DESC LIMIT 1",
                        (inc["incident_id"], mid, stamp),
                    ).fetchone()
                    active_measures.append({
                        "measure_id": mid, "kind": detail["kind"],
                        "description": detail["description"],
                        "valid_from": detail["valid_from"], "valid_to": detail["valid_to"],
                        "provider_org_id": detail["provider_org_id"],
                        "handover_rule": detail["handover_rule"],
                        "handed_over_to": json.loads(handover["detail_json"])["handed_over_to"]
                        if handover else None,
                    })
                if active_measures and availability == "unavailable":
                    availability = "alternative"

                notifications = [dict_notification(r) for r in connection.execute(
                    "SELECT * FROM notifications WHERE incident_id=? AND created_at<=? ORDER BY created_at",
                    (inc["incident_id"], stamp),
                )]
                basis = self._basis_at(connection, inc["incident_id"], stamp)
                work_orders = [dict_wo(r) for r in connection.execute(
                    "SELECT * FROM work_orders WHERE incident_id=? AND created_at<=? ORDER BY seq",
                    (inc["incident_id"], stamp),
                )]
                timeline = [{
                    "seq": e["seq"], "event_type": e["event_type"], "actor_id": e["actor_id"],
                    "work_order_id": e["work_order_id"], "measure_id": e["measure_id"],
                    "reinspection_id": e["reinspection_id"],
                    "detail": json.loads(e["detail_json"]), "created_at": e["created_at"],
                } for e in events]
                incident_views.append({
                    "incident_id": inc["incident_id"], "status": status_at(events),
                    "risk_level": inc["risk_level"],
                    "frozen_contractor_org": inc["frozen_contractor_org"],
                    "frozen_contract_id": inc["frozen_contract_id"],
                    "recurrence_count_at": recurrence_at(events),
                    "basis_reinspection": basis,
                    "work_orders": work_orders,
                    "active_measures": active_measures,
                    "notifications": notifications,
                    "timeline": timeline,
                })
            return {
                "facility_id": facility_id,
                "facility_type": facility["facility_type"],
                "location": facility["location"],
                "sole_accessible_route": bool(facility["sole_accessible_route"]),
                "as_of": stamp,
                "availability": availability,
                "availability_reason": availability_reason,
                "incidents": incident_views,
            }

    def _basis_at(self, connection, incident_id: str, stamp: str) -> dict[str, Any] | None:
        """完全由事件链还原时点有效的验收依据。

        复检通过成为依据；证据撤回或质保期内复发都会使其失效，
        旧验收单不能继续充当关单依据。
        """

        basis = None
        for event in connection.execute(
            "SELECT * FROM incident_events WHERE incident_id=? AND created_at<=? ORDER BY seq",
            (incident_id, stamp),
        ).fetchall():
            etype = event["event_type"]
            if etype == "reinspection.passed":
                detail = json.loads(event["detail_json"])
                basis = {"reinspection_id": detail["reinspection_id"],
                         "evidence_hash": detail.get("evidence_hash"),
                         "passed_at": event["created_at"],
                         "inspector_org_id": detail.get("inspector_org_id")}
            elif etype == "defect.recurred" and basis is not None:
                detail = json.loads(event["detail_json"])
                if detail.get("invalidated_basis_reinspection_id") == basis["reinspection_id"]:
                    basis = None
            elif etype == "reinspection.withdrawn":
                detail = json.loads(event["detail_json"])
                if detail.get("was_basis"):
                    withdrawn_row = connection.execute(
                        "SELECT 1 FROM reinspections WHERE reinspection_id=? AND withdrawn=1 "
                        "AND withdrawn_at<=?",
                        (basis["reinspection_id"], stamp),
                    ).fetchone() if basis else None
                    if withdrawn_row:
                        basis = None
        return basis


def status_at(events) -> str:
    status = "new"
    for event in events:
        etype = event["event_type"]
        if etype == "defect.reported":
            status = "open"
        elif etype in ("work_order.responded", "reinspection.failed"):
            status = "in_repair"
        elif etype == "repair.completed":
            status = "awaiting_reinspection"
        elif etype == "reinspection.passed":
            status = "closed"
        elif etype in ("defect.recurred", "reinspection.withdrawn"):
            status = "reopened"
        elif etype == "responsibility.transferred" and status == "awaiting_reinspection":
            status = "in_repair"
    return status


def recurrence_at(events) -> int:
    return sum(1 for e in events if e["event_type"] == "defect.recurred")


def dict_wo(r) -> dict[str, Any]:
    return {
        "work_order_id": r["work_order_id"], "seq": r["seq"], "kind": r["kind"],
        "contractor_org_id": r["contractor_org_id"], "contract_id": r["contract_id"],
        "status": r["status"], "due_response_at": r["due_response_at"],
        "due_repair_at": r["due_repair_at"], "due_reinspect_at": r["due_reinspect_at"],
        "responded_at": r["responded_at"], "completed_at": r["completed_at"],
        "completed_by": r["completed_by"],
        "continued_from_work_order_id": r["continued_from_work_order_id"],
        "summary": r["summary"], "created_at": r["created_at"],
    }


def dict_measure(r) -> dict[str, Any]:
    return {
        "measure_id": r["measure_id"], "incident_id": r["incident_id"],
        "work_order_id": r["work_order_id"], "kind": r["kind"],
        "description": r["description"], "provider_org_id": r["provider_org_id"],
        "valid_from": r["valid_from"], "valid_to": r["valid_to"],
        "handover_rule": r["handover_rule"], "status": r["status"],
        "supersedes_measure_id": r["supersedes_measure_id"],
        "handed_over_to": r["handed_over_to"], "handed_over_at": r["handed_over_at"],
        "handover_note": r["handover_note"], "created_by": r["created_by"],
        "created_at": r["created_at"],
    }


def dict_reinspection(r) -> dict[str, Any]:
    return {
        "reinspection_id": r["reinspection_id"], "incident_id": r["incident_id"],
        "work_order_id": r["work_order_id"], "inspector_actor_id": r["inspector_actor_id"],
        "inspector_org_id": r["inspector_org_id"], "result": r["result"],
        "independent": bool(r["independent"]),
        "evidence": json.loads(r["evidence_json"]),
        "evidence_hash": r["evidence_hash"], "note": r["note"],
        "withdrawn": bool(r["withdrawn"]), "withdrawn_by": r["withdrawn_by"],
        "withdrawn_at": r["withdrawn_at"], "withdrawn_reason": r["withdrawn_reason"],
        "created_at": r["created_at"],
    }


def dict_notification(r) -> dict[str, Any]:
    return {
        "notification_id": r["notification_id"], "event_seq": r["event_seq"],
        "recipient_kind": r["recipient_kind"], "recipient_id": r["recipient_id"],
        "recipient_label": r["recipient_label"], "channel": r["channel"],
        "reason": r["reason"], "created_at": r["created_at"],
    }


def dict_flag(r) -> dict[str, Any]:
    return {
        "flag_key": r["flag_key"], "escalation_level": r["escalation_level"],
        "first_flagged_at": r["first_flagged_at"], "resolved_at": r["resolved_at"],
    }
