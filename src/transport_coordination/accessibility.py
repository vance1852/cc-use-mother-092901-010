"""无障碍设施缺陷整改与复开服务。

核心规则：
- 报修按影响自动升级风险，并把维护单位与 SLA 冻结在事件上；
- 初报、复发、证据撤回、跨单位移交全部延续同一条事件链，每个环节独立工单，
  旧工单的逾期记录不随移交或复检消失；
- 临时措施必须带到期时间，移交时必须逐顶交接；
- 维修完成只能由组织独立于维护方的复检人确认，证据被撤回会重新打开事件链；
- 所有状态变化写入哈希审计链，可按任意日期还原现场。
"""

from __future__ import annotations

import datetime as _dt
import json
import uuid
from typing import Any

from .accessibility_domain import (
    AFFECTED_GROUPS,
    EVIDENCE_FAILED,
    EVIDENCE_PASSED,
    EVIDENCE_VALID,
    EVIDENCE_WITHDRAWN,
    EVENT_AWAITING,
    EVENT_CLOSED,
    EVENT_EVIDENCE_WITHDRAWN,
    EVENT_INITIAL,
    EVENT_IN_PROGRESS,
    EVENT_OPEN,
    EVENT_RECURRENCE,
    FACILITY_STATUS_IN_SERVICE,
    FACILITY_STATUS_OUT_OF_SERVICE,
    FACILITY_TYPES,
    INTERIM_KINDS,
    MEASURE_ACTIVE,
    MEASURE_EXPIRED,
    MEASURE_HANDED_OVER,
    RISK_RANK,
    WO_COMPLETED,
    WO_IN_PROGRESS,
    WO_OPEN,
    WO_REJECTED,
    WO_TRANSFERRED,
    effective_risk,
    normalize_sla,
    parse_iso,
)
from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .service import DomainService

PROVIDER_ROLES = ("admin", "operator")


class AccessibilityService(DomainService):
    """在基础服务之上实现无障碍缺陷闭环。"""

    # ---- 登记 -----------------------------------------------------------

    def register_facility(self, *, request_id: str, actor_id: str, site_id: str,
                          facility_id: str, facility_type: str, location_desc: str,
                          affected_groups: list[str]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "facility_id": facility_id,
                   "facility_type": facility_type, "location_desc": location_desc,
                   "affected_groups": affected_groups}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            if actor.organization_id != site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能在其他组织的场所登记设施")
            facility_id = self._identifier(facility_id, "facility_id")
            if facility_type not in FACILITY_TYPES:
                raise ValidationError("facility_type 不合法")
            location_desc = self._text(location_desc, "location_desc")
            groups = self._groups(affected_groups)
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO accessibility_facilities(facility_id,site_id,facility_type,location_desc,"
                        "affected_groups_json,status,current_event_id,version,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (facility_id, site_id, facility_type, location_desc, canonical_json(sorted(groups)),
                         FACILITY_STATUS_IN_SERVICE, None, 1, actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("设施编号已经存在") from exc
                connection.execute(
                    "INSERT INTO facility_status_history(facility_id,status,reason,event_id,changed_by,changed_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (facility_id, FACILITY_STATUS_IN_SERVICE, "registered", None, actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="facility.registered",
                             resource_type="facility", resource_id=facility_id,
                             detail={"site_id": site_id, "facility_type": facility_type,
                                     "location_desc": location_desc, "affected_groups": sorted(groups)},
                             occurred_at=now)
                return "facility", facility_id, {"facility_id": facility_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_facility", payload=payload, create=create)

    def register_contract(self, *, request_id: str, actor_id: str, contract_id: str, site_id: str,
                          provider_organization_id: str, title: str, contact: str,
                          facility_type: str | None = None, facility_id: str | None = None,
                          sla: dict[str, Any] | None = None, valid_from: str, valid_to: str,
                          active: bool = True) -> WriteReceipt:
        payload = {"actor_id": actor_id, "contract_id": contract_id, "site_id": site_id,
                   "provider_organization_id": provider_organization_id, "title": title,
                   "facility_type": facility_type, "facility_id": facility_id, "sla": sla,
                   "valid_from": valid_from, "valid_to": valid_to, "active": active, "contact": contact}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            if actor.organization_id != site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能在其他组织的场所登记合同")
            contract_id = self._identifier(contract_id, "contract_id")
            title = self._text(title, "title")
            contact = self._text(contact, "contact", 200)
            if facility_type is not None and facility_type not in FACILITY_TYPES:
                raise ValidationError("facility_type 不合法")
            if facility_id is not None:
                bound = self._facility(connection, facility_id)
                if bound["site_id"] != site_id:
                    raise ValidationError("合同绑定的设施不属于该场所")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (provider_organization_id,)).fetchone() is None:
                raise NotFoundError("维护组织不存在")
            start = parse_iso(valid_from, "valid_from")
            end = parse_iso(valid_to, "valid_to")
            if end <= start:
                raise ValidationError("valid_to 必须晚于 valid_from")
            try:
                limits = normalize_sla(sla or {})
            except (TypeError, ValueError) as exc:
                raise ValidationError(str(exc)) from exc
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO maintenance_contracts(contract_id,site_id,provider_organization_id,title,"
                        "facility_type,facility_id,contact,sla_json,valid_from,valid_to,active,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (contract_id, site_id, provider_organization_id, title, facility_type, facility_id,
                         contact, canonical_json(limits), self._iso(start), self._iso(end),
                         1 if active else 0, actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("合同编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="contract.registered",
                             resource_type="maintenance_contract", resource_id=contract_id,
                             detail={"site_id": site_id, "provider_organization_id": provider_organization_id,
                                     "facility_type": facility_type, "facility_id": facility_id,
                                     "valid_from": self._iso(start), "valid_to": self._iso(end)},
                             occurred_at=now)
                return "maintenance_contract", contract_id, {"contract_id": contract_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_contract", payload=payload, create=create)

    # ---- 报修与事件链 ----------------------------------------------------

    def report_defect(self, *, request_id: str, actor_id: str, facility_id: str,
                      description: str, risk_level: str = "medium",
                      impact_groups: list[str] | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "facility_id": facility_id, "description": description,
                   "risk_level": risk_level, "impact_groups": impact_groups}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            facility = self._facility(connection, facility_id)
            description = self._text(description, "description", 1000)
            if risk_level not in RISK_RANK:
                raise ValidationError("risk_level 不合法")
            facility_groups = set(json.loads(facility["affected_groups_json"]))
            groups = self._groups(impact_groups) if impact_groups is not None else facility_groups
            if not groups:
                groups = facility_groups
            level, escalated = effective_risk(risk_level, groups)
            now = self._now()

            open_row = connection.execute(
                "SELECT * FROM defect_events WHERE facility_id=? AND status!=? ORDER BY opened_at DESC, chain_seq DESC LIMIT 1",
                (facility_id, EVENT_CLOSED),
            ).fetchone()

            if open_row is not None:
                # 设施仍在停用整改：重复报修只能累加在当前事件上，不许另开工单掩盖。
                def create() -> tuple[str, str, dict[str, Any]]:
                    new_count = open_row["repeat_count"] + 1
                    new_level = level
                    new_escalated = 1 if (escalated or open_row["escalated"]) else 0
                    tightened: list[str] = []
                    if RISK_RANK[level] > RISK_RANK[open_row["risk_level"]]:
                        connection.execute(
                            "UPDATE defect_events SET repeat_count=?, risk_level=?, escalated=? WHERE event_id=?",
                            (new_count, new_level, new_escalated, open_row["event_id"]),
                        )
                        # 升级必须同步收紧未结束工单的时限；基准时间仍是原工单创建时刻，
                        # 只取更早的到期值，绝不向后顺延掩盖逾期。
                        for active_wo in connection.execute(
                            "SELECT * FROM work_orders WHERE event_id=? AND status IN (?, ?)",
                            (open_row["event_id"], WO_OPEN, WO_IN_PROGRESS),
                        ):
                            sla = self._sla_for(connection, active_wo["contract_id"],
                                                json.loads(open_row["frozen_sla_json"]))
                            base = parse_iso(active_wo["created_at"], "created_at")
                            new_response = self._iso(
                                base + _dt.timedelta(hours=sla[level]["response"]))
                            new_repair = self._iso(
                                base + _dt.timedelta(hours=sla[level]["repair"]))
                            connection.execute(
                                "UPDATE work_orders SET "
                                "due_response_at=MIN(due_response_at,?), "
                                "due_repair_at=MIN(due_repair_at,?) WHERE work_order_id=?",
                                (new_response, new_repair, active_wo["work_order_id"]),
                            )
                            tightened.append(active_wo["work_order_id"])
                    else:
                        connection.execute(
                            "UPDATE defect_events SET repeat_count=?, escalated=? WHERE event_id=?",
                            (new_count, new_escalated, open_row["event_id"]),
                        )
                    append_event(connection, actor_id=actor_id, action="defect.repeat_reported",
                                 resource_type="defect_event", resource_id=open_row["event_id"],
                                 detail={"chain_id": open_row["chain_id"], "facility_id": facility_id,
                                         "repeat_count": new_count, "risk_level": new_level,
                                         "escalated": bool(new_escalated), "description": description,
                                         "tightened_work_orders": tightened},
                                 occurred_at=now)
                    self._notify(connection, chain_id=open_row["chain_id"], event_id=open_row["event_id"],
                                 facility_id=facility_id, organization_id=facility_org(connection, facility),
                                 role="operator",
                                 reason="risk_escalation" if tightened else "repeat_report", now=now)
                    return "defect_event", open_row["event_id"], {"event_id": open_row["event_id"],
                                                                  "chain_id": open_row["chain_id"],
                                                                  "attached": "open_event",
                                                                  "tightened_work_orders": tightened}

                return self._idempotent(connection, request_id=request_id,
                                        action="report_defect", payload=payload, create=create)

            latest = connection.execute(
                "SELECT * FROM defect_events WHERE facility_id=? ORDER BY opened_at DESC, chain_seq DESC LIMIT 1",
                (facility_id,),
            ).fetchone()
            chain_id = latest["chain_id"] if latest else uuid.uuid4().hex
            chain_seq = (latest["chain_seq"] + 1) if latest else 1
            report_type = EVENT_INITIAL if latest is None else EVENT_RECURRENCE
            parent_event_id = latest["event_id"] if latest else None
            repeat_count = 0 if latest is None else latest["repeat_count"] + 1
            contract = self._active_contract(connection, facility, self.clock.now())
            if contract is None and report_type == EVENT_INITIAL:
                raise ConflictError("该设施当前没有生效的维护合同，无法冻结责任方")
            event_id = uuid.uuid4().hex
            if contract is not None:
                frozen_contract_id = contract["contract_id"]
                frozen_provider = contract["provider_organization_id"]
                frozen_sla = json.loads(contract["sla_json"])
            else:
                # 复发时新合同尚未生效：沿用上一环节当前责任方（最新工单），绝不因合同断档脱责。
                prev_wo = connection.execute(
                    "SELECT * FROM work_orders WHERE event_id=? ORDER BY seq DESC LIMIT 1",
                    (latest["event_id"],),
                ).fetchone()
                frozen_contract_id = prev_wo["contract_id"]
                frozen_provider = prev_wo["provider_organization_id"]
                frozen_sla = self._sla_for(connection, frozen_contract_id,
                                           json.loads(latest["frozen_sla_json"]))

            def create() -> tuple[str, str, dict[str, Any]]:
                opened_dt = self.clock.now()
                connection.execute(
                    "INSERT INTO defect_events(event_id,chain_id,chain_seq,facility_id,report_type,parent_event_id,"
                    "description,impact_groups_json,risk_level,reported_risk_level,escalated,status,"
                    "frozen_contract_id,frozen_provider_org_id,frozen_sla_json,repeat_count,reported_by,"
                    "opened_at,closed_at,closed_evidence_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (event_id, chain_id, chain_seq, facility_id, report_type, parent_event_id, description,
                     canonical_json(sorted(groups)), level, risk_level, 1 if escalated else 0, EVENT_OPEN,
                     frozen_contract_id, frozen_provider, canonical_json(frozen_sla), repeat_count,
                     actor_id, now, None, None),
                )
                work_order_id = self._open_work_order(
                    connection, event=event_id, chain_id=chain_id, seq=1, provider=frozen_provider,
                    contract_id=frozen_contract_id, sla=frozen_sla, level=level,
                    opened_at=opened_dt, created_by=actor_id,
                )
                connection.execute(
                    "UPDATE accessibility_facilities SET status=?, current_event_id=?, version=version+1 WHERE facility_id=?",
                    (FACILITY_STATUS_OUT_OF_SERVICE, event_id, facility_id),
                )
                connection.execute(
                    "INSERT INTO facility_status_history(facility_id,status,reason,event_id,changed_by,changed_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (facility_id, FACILITY_STATUS_OUT_OF_SERVICE, report_type, event_id, actor_id, now),
                )
                append_event(connection, actor_id=actor_id,
                             action="defect.reported" if report_type == EVENT_INITIAL else "defect.recurred",
                             resource_type="defect_event", resource_id=event_id,
                             detail={"chain_id": chain_id, "chain_seq": chain_seq,
                                     "parent_event_id": parent_event_id, "facility_id": facility_id,
                                     "report_type": report_type, "risk_level": level,
                                     "reported_risk_level": risk_level, "escalated": escalated,
                                     "repeat_count": repeat_count, "impact_groups": sorted(groups),
                                     "frozen_provider_org_id": frozen_provider,
                                     "frozen_contract_id": frozen_contract_id, "work_order_id": work_order_id},
                             occurred_at=now)
                self._notify(connection, chain_id=chain_id, event_id=event_id, facility_id=facility_id,
                             organization_id=frozen_provider, reason="new_defect", now=now)
                manager_org = facility_org(connection, facility)
                self._notify(connection, chain_id=chain_id, event_id=event_id, facility_id=facility_id,
                             organization_id=manager_org, role="operator",
                             reason="risk_escalation" if escalated else "defect_opened", now=now)
                return "defect_event", event_id, {"event_id": event_id, "chain_id": chain_id,
                                                   "chain_seq": chain_seq, "work_order_id": work_order_id,
                                                   "risk_level": level, "escalated": escalated,
                                                   "frozen_provider_org_id": frozen_provider}

            return self._idempotent(connection, request_id=request_id,
                                    action="report_defect", payload=payload, create=create)

    # ---- 维修侧动作 ------------------------------------------------------

    def respond_work_order(self, *, request_id: str, actor_id: str, work_order_id: str,
                           note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "work_order_id": work_order_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            wo = self._work_order(connection, work_order_id)
            event = self._event(connection, wo["event_id"])
            self._require_provider_actor(actor, wo["provider_organization_id"])
            if wo["status"] != WO_OPEN:
                raise ConflictError("工单已不是待响应状态")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE work_orders SET status=?, responded_at=? WHERE work_order_id=?",
                    (WO_IN_PROGRESS, now, work_order_id),
                )
                if event["status"] == EVENT_OPEN:
                    connection.execute(
                        "UPDATE defect_events SET status=? WHERE event_id=?", (EVENT_IN_PROGRESS, event["event_id"])
                    )
                append_event(connection, actor_id=actor_id, action="work_order.responded",
                             resource_type="work_order", resource_id=work_order_id,
                             detail={"chain_id": event["chain_id"], "event_id": event["event_id"],
                                     "response_breached": wo["due_response_at"] < now, "note": note},
                             occurred_at=now)
                return "work_order", work_order_id, {"work_order_id": work_order_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="respond_work_order", payload=payload, create=create)

    def commit_interim(self, *, request_id: str, actor_id: str, event_id: str, kind: str,
                       description: str, expires_at: str, linked_from_measure_id: str | None = None
                       ) -> WriteReceipt:
        payload = {"actor_id": actor_id, "event_id": event_id, "kind": kind,
                   "description": description, "expires_at": expires_at,
                   "linked_from_measure_id": linked_from_measure_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            event = self._event(connection, event_id)
            self._require_open(event)
            current_wo = connection.execute(
                "SELECT * FROM work_orders WHERE event_id=? ORDER BY seq DESC LIMIT 1", (event_id,)
            ).fetchone()
            if current_wo["status"] not in (WO_OPEN, WO_IN_PROGRESS):
                raise ConflictError("当前工单已结束，不能再登记临时措施")
            self._require_provider_actor(actor, current_wo["provider_organization_id"])
            if kind not in INTERIM_KINDS:
                raise ValidationError("kind 不合法")
            description = self._text(description, "description", 500)
            expiry = parse_iso(expires_at, "expires_at")
            if expiry <= self.clock.now():
                raise ValidationError("临时措施到期时间必须晚于当前时间")
            sla = self._sla_for(connection, current_wo["contract_id"],
                                json.loads(event["frozen_sla_json"]))
            base = parse_iso(current_wo["created_at"], "work_order.created_at")
            max_expiry = base + _dt.timedelta(hours=sla[event["risk_level"]]["interim"])
            if expiry > max_expiry:
                raise ConflictError(
                    f"临时措施承诺超过该等级 {sla[event['risk_level']]['interim']} 小时上限，必须复检或续期交接"
                )
            if linked_from_measure_id is not None:
                prior = self._measure(connection, linked_from_measure_id)
                if prior["chain_id"] != event["chain_id"]:
                    raise ValidationError("续接的临时措施不属于同一事件链")
            measure_id = uuid.uuid4().hex
            now = self._now()
            provider_org = current_wo["provider_organization_id"]

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO interim_measures(measure_id,chain_id,event_id,kind,description,"
                    "provider_organization_id,committed_by,linked_from_measure_id,effective_at,expires_at,"
                    "status,handed_to_org_id,handed_over_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (measure_id, event["chain_id"], event_id, kind, description,
                     provider_org, actor_id, linked_from_measure_id, now, self._iso(expiry),
                     MEASURE_ACTIVE, None, None, now),
                )
                append_event(connection, actor_id=actor_id, action="interim.committed",
                             resource_type="interim_measure", resource_id=measure_id,
                             detail={"chain_id": event["chain_id"], "event_id": event_id, "kind": kind,
                                     "expires_at": self._iso(expiry),
                                     "linked_from_measure_id": linked_from_measure_id},
                             occurred_at=now)
                return "interim_measure", measure_id, {"measure_id": measure_id, "expires_at": self._iso(expiry)}

            return self._idempotent(connection, request_id=request_id,
                                    action="commit_interim", payload=payload, create=create)

    def complete_work_order(self, *, request_id: str, actor_id: str, work_order_id: str,
                            repair_summary: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "work_order_id": work_order_id, "repair_summary": repair_summary}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            wo = self._work_order(connection, work_order_id)
            event = self._event(connection, wo["event_id"])
            self._require_open(event)
            self._require_provider_actor(actor, wo["provider_organization_id"])
            if wo["status"] not in (WO_OPEN, WO_IN_PROGRESS):
                raise ConflictError("工单已结束，不能重复申报完成")
            latest = connection.execute(
                "SELECT seq FROM work_orders WHERE event_id=? ORDER BY seq DESC LIMIT 1", (event["event_id"],)
            ).fetchone()
            if latest["seq"] != wo["seq"]:
                raise ConflictError("只能申报最新工单完工，旧工单仅作留痕")
            repair_summary = self._text(repair_summary, "repair_summary", 1000)
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE work_orders SET status=?, completed_at=? WHERE work_order_id=?",
                    (WO_COMPLETED, now, work_order_id),
                )
                connection.execute(
                    "UPDATE defect_events SET status=? WHERE event_id=?", (EVENT_AWAITING, event["event_id"])
                )
                append_event(connection, actor_id=actor_id, action="work_order.completed",
                             resource_type="work_order", resource_id=work_order_id,
                             detail={"chain_id": event["chain_id"], "event_id": event["event_id"],
                                     "repair_breached": wo["due_repair_at"] < now,
                                     "repair_summary": repair_summary},
                             occurred_at=now)
                self._notify(connection, chain_id=event["chain_id"], event_id=event["event_id"],
                             facility_id=event["facility_id"], organization_id=facility_org_id(connection, event),
                             role="reviewer", reason="awaiting_reinspection", now=now)
                return "work_order", work_order_id, {"work_order_id": work_order_id,
                                                      "status": WO_COMPLETED}

            return self._idempotent(connection, request_id=request_id,
                                    action="complete_work_order", payload=payload, create=create)

    # ---- 独立复检 --------------------------------------------------------

    def submit_reinspection(self, *, request_id: str, actor_id: str, event_id: str,
                            result: str, summary: str, evidence_ref: str,
                            evidence_data: dict[str, Any] | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "event_id": event_id, "result": result, "summary": summary,
                   "evidence_ref": evidence_ref, "evidence_data": evidence_data}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            event = self._event(connection, event_id)
            self._require_open(event)
            wo = connection.execute(
                "SELECT * FROM work_orders WHERE event_id=? ORDER BY seq DESC LIMIT 1", (event_id,)
            ).fetchone()
            if wo is not None and actor.organization_id == wo["provider_organization_id"]:
                raise PermissionDenied("复检人必须独立于当前维护责任单位")
            if result not in (EVIDENCE_PASSED, EVIDENCE_FAILED):
                raise ValidationError("result 必须是 passed 或 failed")
            summary = self._text(summary, "summary", 1000)
            evidence_ref = self._text(evidence_ref, "evidence_ref", 300)
            if not isinstance(evidence_data, dict):
                raise ValidationError("evidence_data 必须是对象")
            if wo is None or wo["status"] != WO_COMPLETED:
                raise ConflictError("必须在维修方申报完工后才能复检")
            now_dt = self.clock.now()
            now = self._iso(now_dt)
            completed_at = parse_iso(wo["completed_at"], "completed_at")
            if now_dt < completed_at:
                raise ValidationError("复检时间不能早于完工申报时间")
            current_sla = self._sla_for(connection, wo["contract_id"], json.loads(event["frozen_sla_json"]))
            evidence_id = uuid.uuid4().hex
            evidence_hash = digest({"ref": evidence_ref, "summary": summary, "result": result,
                                    "data": evidence_data, "work_order_id": wo["work_order_id"],
                                    "inspected_at": now})

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO reinspection_evidence(evidence_id,chain_id,event_id,work_order_id,"
                    "inspector_actor_id,inspector_organization_id,result,summary,evidence_ref,payload_json,"
                    "evidence_hash,status,withdrawn_by,withdrawn_at,withdraw_reason,inspected_at,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (evidence_id, event["chain_id"], event_id, wo["work_order_id"], actor_id,
                     actor.organization_id, result, summary, evidence_ref,
                     canonical_json(evidence_data), evidence_hash, EVIDENCE_VALID, None, None, None, now, now),
                )
                if result == EVIDENCE_PASSED:
                    connection.execute(
                        "UPDATE defect_events SET status=?, closed_at=?, closed_evidence_id=? WHERE event_id=?",
                        (EVENT_CLOSED, now, evidence_id, event_id),
                    )
                    connection.execute(
                        "UPDATE accessibility_facilities SET status=?, current_event_id=NULL, version=version+1 "
                        "WHERE facility_id=?",
                        (FACILITY_STATUS_IN_SERVICE, event["facility_id"]),
                    )
                    connection.execute(
                        "INSERT INTO facility_status_history(facility_id,status,reason,event_id,changed_by,changed_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (event["facility_id"], FACILITY_STATUS_IN_SERVICE, "reinspection_passed",
                         event_id, actor_id, now),
                    )
                    action = "reinspection.passed"
                else:
                    # 复检驳回：旧工单留 rejected 痕迹，同一事件链上开新工单继续整改。
                    connection.execute(
                        "UPDATE work_orders SET status=? WHERE work_order_id=?",
                        (WO_REJECTED, wo["work_order_id"]),
                    )
                    new_wo = self._open_work_order(
                        connection, event=event_id, chain_id=event["chain_id"], seq=wo["seq"] + 1,
                        provider=wo["provider_organization_id"], contract_id=wo["contract_id"],
                        sla=current_sla, level=event["risk_level"],
                        opened_at=now_dt, created_by=actor_id,
                    )
                    connection.execute(
                        "UPDATE defect_events SET status=? WHERE event_id=?", (EVENT_OPEN, event_id)
                    )
                    action = "reinspection.failed"
                append_event(connection, actor_id=actor_id, action=action,
                             resource_type="reinspection_evidence", resource_id=evidence_id,
                             detail={"chain_id": event["chain_id"], "event_id": event_id,
                                     "work_order_id": wo["work_order_id"], "result": result,
                                     "evidence_ref": evidence_ref, "evidence_hash": evidence_hash,
                                     "inspector_organization_id": actor.organization_id},
                             occurred_at=now)
                self._notify(connection, chain_id=event["chain_id"], event_id=event_id,
                             facility_id=event["facility_id"],
                             organization_id=wo["provider_organization_id"],
                             reason="reopened_after_failure" if result == EVIDENCE_FAILED else "facility_reopened",
                             now=now)
                response = {"evidence_id": evidence_id, "result": result}
                if result == EVIDENCE_FAILED:
                    response["new_work_order_id"] = new_wo
                return "reinspection_evidence", evidence_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_reinspection", payload=payload, create=create)

    def withdraw_evidence(self, *, request_id: str, actor_id: str, evidence_id: str,
                          reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "evidence_id": evidence_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            row = connection.execute(
                "SELECT * FROM reinspection_evidence WHERE evidence_id=?", (evidence_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("复检证据不存在")
            if actor.role != "admin" and actor.actor_id != row["inspector_actor_id"]:
                raise PermissionDenied("只有原复检人或管理员可以撤回证据")
            if row["status"] == EVIDENCE_WITHDRAWN:
                raise ConflictError("证据已经撤回")
            reason = self._text(reason, "reason", 500)
            event = self._event(connection, row["event_id"])
            facility = self._facility(connection, event["facility_id"])
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE reinspection_evidence SET status=?, withdrawn_by=?, withdrawn_at=?, withdraw_reason=? "
                    "WHERE evidence_id=?",
                    (EVIDENCE_WITHDRAWN, actor_id, now, reason, evidence_id),
                )
                new_event_id = None
                new_wo = None
                if event["closed_evidence_id"] == evidence_id:
                    # 复开依据失效：沿原链新开 evidence_withdrawn 环节，设施重新停用。
                    new_event_id, new_wo = self._spawn_withdrawn_event(
                        connection, parent=event, facility=facility, withdrawn_evidence=row,
                        reason=reason, actor_id=actor_id, now=now,
                    )
                append_event(connection, actor_id=actor_id, action="evidence.withdrawn",
                             resource_type="reinspection_evidence", resource_id=evidence_id,
                             detail={"chain_id": row["chain_id"], "event_id": row["event_id"],
                                     "was_closing_basis": event["closed_evidence_id"] == evidence_id,
                                     "new_event_id": new_event_id, "new_work_order_id": new_wo,
                                     "reason": reason}, occurred_at=now)
                self._notify(connection, chain_id=row["chain_id"], event_id=row["event_id"],
                             facility_id=facility["facility_id"],
                             organization_id=facility_org_id(connection, event), role="operator",
                             reason="evidence_withdrawn", now=now)
                return "reinspection_evidence", evidence_id, {"evidence_id": evidence_id,
                                                               "new_event_id": new_event_id,
                                                               "new_work_order_id": new_wo}

            return self._idempotent(connection, request_id=request_id,
                                    action="withdraw_evidence", payload=payload, create=create)

    # ---- 跨单位移交 ------------------------------------------------------

    def transfer_responsibility(self, *, request_id: str, actor_id: str, event_id: str,
                                to_organization_id: str, to_contract_id: str | None,
                                measure_handovers: list[dict[str, str]] | None = None,
                                reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "event_id": event_id,
                   "to_organization_id": to_organization_id, "to_contract_id": to_contract_id,
                   "measure_handovers": measure_handovers, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            event = self._event(connection, event_id)
            self._require_open(event)
            facility = self._facility(connection, event["facility_id"])
            if actor.role != "admin" and actor.organization_id != facility_org(connection, facility):
                raise PermissionDenied("只有枢纽管理方可以移交责任")
            wo = connection.execute(
                "SELECT * FROM work_orders WHERE event_id=? ORDER BY seq DESC LIMIT 1", (event_id,)
            ).fetchone()
            from_provider = wo["provider_organization_id"]
            if actor.organization_id == from_provider and actor.role != "admin":
                raise PermissionDenied("当前责任方不能自行移交")
            if to_organization_id == from_provider:
                raise ConflictError("新责任方与当前责任方相同")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (to_organization_id,)).fetchone() is None:
                raise NotFoundError("接管组织不存在")
            new_contract = None
            new_sla = self._sla_for(connection, wo["contract_id"], json.loads(event["frozen_sla_json"]))
            if to_contract_id is not None:
                new_contract = connection.execute(
                    "SELECT * FROM maintenance_contracts WHERE contract_id=?", (to_contract_id,)
                ).fetchone()
                if new_contract is None:
                    raise NotFoundError("合同不存在")
                if new_contract["provider_organization_id"] != to_organization_id:
                    raise ValidationError("合同不属于接管组织")
                new_sla = json.loads(new_contract["sla_json"])
            reason = self._text(reason, "reason", 500)
            handovers = measure_handovers or []
            active_measures = connection.execute(
                "SELECT * FROM interim_measures WHERE event_id=? AND status=?",
                (event_id, MEASURE_ACTIVE),
            ).fetchall()
            handed_map = {item.get("measure_id"): item for item in handovers if isinstance(item, dict)}
            if any(measure["measure_id"] not in handed_map for measure in active_measures):
                raise ConflictError("存在未交接的生效临时措施，必须逐项交接后才能移交")
            wo = connection.execute(
                "SELECT * FROM work_orders WHERE event_id=? ORDER BY seq DESC LIMIT 1", (event_id,)
            ).fetchone()
            now_dt = self.clock.now()
            now = self._iso(now_dt)
            transfer_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                for measure in active_measures:
                    item = handed_map[measure["measure_id"]]
                    desc = self._text(item.get("description") or measure["description"], "handovers.description", 500)
                    if "expires_at" in item and item["expires_at"]:
                        expiry = parse_iso(item["expires_at"], "handovers.expires_at")
                        if expiry <= now_dt:
                            raise ValidationError("交接后的临时措施到期时间必须晚于当前时间")
                    else:
                        expiry = parse_iso(measure["expires_at"], "expires_at")
                    new_measure = uuid.uuid4().hex
                    connection.execute(
                        "UPDATE interim_measures SET status=?, handed_to_org_id=?, handed_over_at=? WHERE measure_id=?",
                        (MEASURE_HANDED_OVER, to_organization_id, now, measure["measure_id"]),
                    )
                    connection.execute(
                        "INSERT INTO interim_measures(measure_id,chain_id,event_id,kind,description,"
                        "provider_organization_id,committed_by,linked_from_measure_id,effective_at,expires_at,"
                        "status,handed_to_org_id,handed_over_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (new_measure, event["chain_id"], event_id, measure["kind"], desc,
                         to_organization_id, actor_id, measure["measure_id"], now, self._iso(expiry),
                         MEASURE_ACTIVE, None, None, now),
                    )
                connection.execute(
                    "UPDATE work_orders SET status=? WHERE work_order_id=?",
                    (WO_TRANSFERRED, wo["work_order_id"]),
                )
                new_wo = self._open_work_order(
                    connection, event=event_id, chain_id=event["chain_id"], seq=wo["seq"] + 1,
                    provider=to_organization_id,
                    contract_id=new_contract["contract_id"] if new_contract else None,
                    sla=new_sla, level=event["risk_level"], opened_at=now_dt, created_by=actor_id,
                )
                connection.execute(
                    "UPDATE defect_events SET status=? WHERE event_id=?", (EVENT_OPEN, event_id)
                )
                append_event(connection, actor_id=actor_id, action="responsibility.transferred",
                             resource_type="defect_event", resource_id=event_id,
                             detail={"chain_id": event["chain_id"],
                                     "from_provider_org_id": from_provider,
                                     "to_provider_org_id": to_organization_id,
                                     "previous_work_order_id": wo["work_order_id"],
                                     "new_work_order_id": new_wo,
                                     "handed_over_measures": len(active_measures), "reason": reason},
                             occurred_at=now)
                self._notify(connection, chain_id=event["chain_id"], event_id=event_id,
                             facility_id=facility["facility_id"], organization_id=to_organization_id,
                             reason="responsibility_transferred", now=now)
                return "defect_event", event_id, {"event_id": event_id, "new_work_order_id": new_wo,
                                                   "frozen_provider_org_id": to_organization_id,
                                                   "transfer_id": transfer_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="transfer_responsibility", payload=payload, create=create)

    # ---- 定时巡检：到期与逾期 --------------------------------------------

    def sweep(self) -> dict[str, int]:
        """把已到期的临时措施和已逾期的工单环节落戳并通知，返回处理数量。"""

        expired = overdue = 0
        with self.database.transaction(immediate=True) as connection:
            now = self._now()
            measure_rows = connection.execute(
                "SELECT * FROM interim_measures WHERE status=? AND expires_at<=?", (MEASURE_ACTIVE, now)
            ).fetchall()
            for measure in measure_rows:
                connection.execute(
                    "UPDATE interim_measures SET status=? WHERE measure_id=?",
                    (MEASURE_EXPIRED, measure["measure_id"]),
                )
                event = self._event(connection, measure["event_id"])
                append_event(connection, actor_id="scheduler", action="interim.expired",
                             resource_type="interim_measure", resource_id=measure["measure_id"],
                             detail={"chain_id": measure["chain_id"], "event_id": measure["event_id"],
                                     "expires_at": measure["expires_at"],
                                     "event_still_open": event["status"] != EVENT_CLOSED},
                             occurred_at=now)
                self._notify(connection, chain_id=measure["chain_id"], event_id=measure["event_id"],
                             facility_id=event["facility_id"],
                             organization_id=measure["provider_organization_id"],
                             reason="interim_expired", now=now)
                self._notify(connection, chain_id=measure["chain_id"], event_id=measure["event_id"],
                             facility_id=event["facility_id"],
                             organization_id=facility_org_id(connection, event), role="operator",
                             reason="interim_expired", now=now)
                expired += 1
            wo_rows = connection.execute(
                "SELECT * FROM work_orders WHERE status IN (?, ?, ?)",
                (WO_OPEN, WO_IN_PROGRESS, WO_TRANSFERRED)
            ).fetchall()
            for wo in wo_rows:
                flags: dict[str, str] = {}
                if wo["responded_at"] is None and wo["response_breached_at"] is None and \
                        wo["due_response_at"] < now:
                    flags["response_breached_at"] = now
                if wo["repair_breached_at"] is None and wo["due_repair_at"] < now:
                    flags["repair_breached_at"] = now
                if flags:
                    sets = ", ".join(f"{key}=?" for key in flags)
                    connection.execute(
                        f"UPDATE work_orders SET {sets} WHERE work_order_id=?",
                        (*flags.values(), wo["work_order_id"]),
                    )
                    event = self._event(connection, wo["event_id"])
                    append_event(connection, actor_id="scheduler", action="work_order.overdue",
                                 resource_type="work_order", resource_id=wo["work_order_id"],
                                 detail={"chain_id": wo["chain_id"], "event_id": wo["event_id"],
                                         "breached": sorted(flags)}, occurred_at=now)
                    self._notify(connection, chain_id=wo["chain_id"], event_id=wo["event_id"],
                                 facility_id=event["facility_id"], organization_id=wo["provider_organization_id"],
                                 reason="sla_breach", now=now)
                    self._notify(connection, chain_id=wo["chain_id"], event_id=wo["event_id"],
                                 facility_id=event["facility_id"],
                                 organization_id=facility_org_id(connection, event), role="operator",
                                 reason="sla_breach", now=now)
                    overdue += 1
        return {"expired_measures": expired, "overdue_work_orders": overdue}

    # ---- 值班经理视图 ----------------------------------------------------

    def accessible_routes(self, site_id: str) -> dict[str, Any]:
        """返回场所内当前可用设施，以及停用设施仍生效的替代承诺。"""

        now = self._now()
        connection = self.database.connection
        self._site(connection, site_id)
        usable: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for facility in connection.execute(
            "SELECT * FROM accessibility_facilities WHERE site_id=? ORDER BY facility_id", (site_id,)
        ):
            item = self._facility_view(facility)
            if facility["status"] == FACILITY_STATUS_IN_SERVICE:
                usable.append(item)
                continue
            active = connection.execute(
                "SELECT * FROM interim_measures WHERE event_id=? AND status=? ORDER BY expires_at",
                (facility["current_event_id"], MEASURE_ACTIVE),
            ).fetchall()
            item["active_interim"] = [{
                "measure_id": m["measure_id"], "kind": m["kind"], "description": m["description"],
                "expires_at": m["expires_at"], "provider_organization_id": m["provider_organization_id"],
                "valid": m["effective_at"] <= now < m["expires_at"],
            } for m in active]
            item["has_valid_alternative"] = any(
                m["effective_at"] <= now < m["expires_at"] for m in active
            )
            blocked.append(item)
        return {"site_id": site_id, "as_of": now, "in_service": usable, "out_of_service": blocked}

    def expiring_commitments(self, site_id: str, within_hours: int = 24) -> dict[str, Any]:
        """列出即将到期（默认 24 小时内）且尚未交接/失效的替代承诺。"""

        now_dt = self.clock.now()
        horizon = self._iso(now_dt + _dt.timedelta(hours=within_hours))
        now = self._iso(now_dt)
        connection = self.database.connection
        self._site(connection, site_id)
        items = []
        rows = connection.execute(
            "SELECT m.* FROM interim_measures m JOIN defect_events e ON e.event_id=m.event_id "
            "JOIN accessibility_facilities f ON f.facility_id=e.facility_id "
            "WHERE f.site_id=? AND m.status=? AND m.expires_at>? AND m.expires_at<=? "
            "AND e.status!=? ORDER BY m.expires_at",
            (site_id, MEASURE_ACTIVE, now, horizon, EVENT_CLOSED),
        ).fetchall()
        for m in rows:
            items.append({"measure_id": m["measure_id"], "chain_id": m["chain_id"],
                          "event_id": m["event_id"], "kind": m["kind"], "description": m["description"],
                          "expires_at": m["expires_at"],
                          "provider_organization_id": m["provider_organization_id"]})
        return {"site_id": site_id, "as_of": now, "within_hours": within_hours, "items": items}

    def overdue_board(self, site_id: str) -> dict[str, Any]:
        """列出每个未关闭事件链上的逾期环节：响应、修复、过期承诺。"""

        now = self._now()
        connection = self.database.connection
        self._site(connection, site_id)
        events = connection.execute(
            "SELECT * FROM defect_events e WHERE e.status!=? AND e.facility_id IN "
            "(SELECT facility_id FROM accessibility_facilities WHERE site_id=?) "
            "ORDER BY e.opened_at",
            (EVENT_CLOSED, site_id),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for event in events:
            breaches: list[dict[str, Any]] = []
            for wo in connection.execute("SELECT * FROM work_orders WHERE event_id=? ORDER BY seq",
                                         (event["event_id"],)):
                open_ended = wo["status"] in (WO_OPEN, WO_IN_PROGRESS, WO_TRANSFERRED)
                # 旧工单即使已移交，到期仍未响应/完工的，逾期记在旧责任方头上不被抹掉。
                if wo["responded_at"] is None and wo["due_response_at"] < now and open_ended:
                    breaches.append({"stage": "response", "work_order_id": wo["work_order_id"],
                                     "status": wo["status"],
                                     "provider_organization_id": wo["provider_organization_id"],
                                     "due_at": wo["due_response_at"],
                                     "stamped_at": wo["response_breached_at"]})
                if wo["completed_at"] is None and wo["due_repair_at"] < now and open_ended:
                    breaches.append({"stage": "repair", "work_order_id": wo["work_order_id"],
                                     "status": wo["status"],
                                     "provider_organization_id": wo["provider_organization_id"],
                                     "due_at": wo["due_repair_at"],
                                     "stamped_at": wo["repair_breached_at"]})
            for m in connection.execute(
                "SELECT * FROM interim_measures WHERE event_id=? AND status=?",
                (event["event_id"], MEASURE_EXPIRED),
            ):
                breaches.append({"stage": "interim_expired", "measure_id": m["measure_id"],
                                 "provider_organization_id": m["provider_organization_id"],
                                 "due_at": m["expires_at"], "stamped_at": m["handed_over_at"]})
            if breaches:
                current = connection.execute(
                    "SELECT provider_organization_id FROM work_orders WHERE event_id=? "
                    "ORDER BY seq DESC LIMIT 1", (event["event_id"],)
                ).fetchone()
                items.append({"event_id": event["event_id"], "chain_id": event["chain_id"],
                              "facility_id": event["facility_id"], "risk_level": event["risk_level"],
                              "frozen_provider_org_id": event["frozen_provider_org_id"],
                              "current_provider_org_id": current["provider_organization_id"] if current else None,
                              "breaches": breaches})
        return {"site_id": site_id, "as_of": now, "items": items}

    # ---- 审计还原 --------------------------------------------------------

    def chain_view(self, chain_id: str) -> dict[str, Any]:
        """还原一条事件链的全部环节、工单、临时措施与复检证据。"""

        connection = self.database.connection
        events = connection.execute(
            "SELECT * FROM defect_events WHERE chain_id=? ORDER BY chain_seq", (chain_id,)
        ).fetchall()
        if not events:
            raise NotFoundError("事件链不存在")
        result = []
        for event in events:
            work_orders = [self._wo_view(row) for row in connection.execute(
                "SELECT * FROM work_orders WHERE event_id=? ORDER BY seq", (event["event_id"],))]
            measures = [dict(self._measure_view(row)) for row in connection.execute(
                "SELECT * FROM interim_measures WHERE event_id=? ORDER BY effective_at", (event["event_id"],))]
            evidence = [self._evidence_view(row) for row in connection.execute(
                "SELECT * FROM reinspection_evidence WHERE event_id=? ORDER BY inspected_at",
                (event["event_id"],))]
            result.append({
                "event_id": event["event_id"], "chain_seq": event["chain_seq"],
                "report_type": event["report_type"], "parent_event_id": event["parent_event_id"],
                "facility_id": event["facility_id"], "description": event["description"],
                "impact_groups": json.loads(event["impact_groups_json"]),
                "risk_level": event["risk_level"], "reported_risk_level": event["reported_risk_level"],
                "escalated": bool(event["escalated"]), "status": event["status"],
                "repeat_count": event["repeat_count"],
                "frozen_contract_id": event["frozen_contract_id"],
                "frozen_provider_org_id": event["frozen_provider_org_id"],
                "current_provider_org_id": work_orders[-1]["provider_organization_id"] if work_orders else None,
                "frozen_sla": json.loads(event["frozen_sla_json"]),
                "opened_at": event["opened_at"], "closed_at": event["closed_at"],
                "closed_evidence_id": event["closed_evidence_id"],
                "work_orders": work_orders, "interim_measures": measures, "evidence": evidence,
            })
        return {"chain_id": chain_id, "events": result}

    def facility_as_of(self, facility_id: str, at: str) -> dict[str, Any]:
        """按任意时刻还原设施状态、通知对象、验收依据与复发责任。"""

        moment = parse_iso(at, "at")
        at_text = self._iso(moment)
        connection = self.database.connection
        facility = self._facility(connection, facility_id)
        if facility["created_at"] > at_text:
            raise NotFoundError("该时刻设施尚未登记")

        history = connection.execute(
            "SELECT * FROM facility_status_history WHERE facility_id=? AND changed_at<=? "
            "ORDER BY changed_at DESC, history_id DESC LIMIT 1",
            (facility_id, at_text),
        ).fetchone()
        status = history["status"] if history else FACILITY_STATUS_IN_SERVICE

        event_rows = connection.execute(
            "SELECT * FROM defect_events WHERE facility_id=? AND opened_at<=? ORDER BY chain_seq",
            (facility_id, at_text),
        ).fetchall()
        chain_snapshots = []
        open_event = None
        for event in event_rows:
            closed_by_then = event["closed_at"] is not None and event["closed_at"] <= at_text
            evidence_rows = connection.execute(
                "SELECT * FROM reinspection_evidence WHERE event_id=? AND created_at<=? ORDER BY inspected_at",
                (event["event_id"], at_text),
            ).fetchall()
            evidence_snapshot = []
            for ev in evidence_rows:
                withdrawn_by_then = ev["withdrawn_at"] is not None and ev["withdrawn_at"] <= at_text
                evidence_snapshot.append({
                    "evidence_id": ev["evidence_id"], "result": ev["result"],
                    "inspector_actor_id": ev["inspector_actor_id"],
                    "inspector_organization_id": ev["inspector_organization_id"],
                    "evidence_ref": ev["evidence_ref"], "evidence_hash": ev["evidence_hash"],
                    "inspected_at": ev["inspected_at"],
                    "state_at": EVIDENCE_WITHDRAWN if withdrawn_by_then else EVIDENCE_VALID,
                    "was_acceptance_basis": closed_by_then and event["closed_evidence_id"] == ev["evidence_id"]
                                             and not withdrawn_by_then,
                })
            measures = []
            for m in connection.execute(
                "SELECT * FROM interim_measures WHERE event_id=? AND effective_at<=? ORDER BY effective_at",
                (event["event_id"], at_text),
            ):
                handed_by_then = m["handed_over_at"] is not None and m["handed_over_at"] <= at_text
                active_then = not handed_by_then and m["expires_at"] > at_text
                measures.append({"measure_id": m["measure_id"], "kind": m["kind"],
                                 "description": m["description"], "expires_at": m["expires_at"],
                                 "provider_organization_id": m["provider_organization_id"],
                                 "state_at": ("active" if active_then else
                                              "handed_over" if handed_by_then else "expired")})
            work_orders = []
            for wo in connection.execute(
                "SELECT * FROM work_orders WHERE event_id=? AND created_at<=? ORDER BY seq",
                (event["event_id"], at_text),
            ):
                work_orders.append({"work_order_id": wo["work_order_id"], "seq": wo["seq"],
                                    "provider_organization_id": wo["provider_organization_id"],
                                    "status": wo["status"], "due_response_at": wo["due_response_at"],
                                    "due_repair_at": wo["due_repair_at"],
                                    "response_breached": bool(wo["response_breached_at"]
                                                              and wo["response_breached_at"] <= at_text),
                                    "repair_breached": bool(wo["repair_breached_at"]
                                                            and wo["repair_breached_at"] <= at_text)})
            # 该时刻可见的最新工单即当时的当前责任方；旧工单保留各自责任方留痕。
            current_provider = work_orders[-1]["provider_organization_id"] if work_orders else None
            snapshot = {
                "event_id": event["event_id"], "chain_id": event["chain_id"],
                "chain_seq": event["chain_seq"], "report_type": event["report_type"],
                "risk_level": event["risk_level"], "escalated": bool(event["escalated"]),
                "repeat_count": event["repeat_count"],
                "frozen_provider_org_id": event["frozen_provider_org_id"],
                "frozen_contract_id": event["frozen_contract_id"],
                "current_provider_org_id": current_provider,
                "opened_at": event["opened_at"],
                "was_open": not closed_by_then,
                "closed_at": event["closed_at"] if closed_by_then else None,
                "work_orders": work_orders, "interim_measures": measures,
                "evidence": evidence_snapshot,
                "recurrence_responsibility": current_provider if event["report_type"]
                                             in (EVENT_RECURRENCE, EVENT_EVIDENCE_WITHDRAWN) else None,
            }
            chain_snapshots.append(snapshot)
            if not closed_by_then:
                open_event = snapshot

        notifications = []
        for note in connection.execute(
            "SELECT * FROM notifications WHERE facility_id=? AND created_at<=? ORDER BY created_at",
            (facility_id, at_text),
        ):
            notifications.append({"reason": note["reason"], "channel": note["channel"],
                                  "target_organization_id": note["target_organization_id"],
                                  "target_role": note["target_role"], "target_actor_id": note["target_actor_id"],
                                  "chain_id": note["chain_id"], "created_at": note["created_at"]})

        acceptance = None
        if open_event is None:
            for snapshot in reversed(chain_snapshots):
                basis = [e for e in snapshot["evidence"] if e["was_acceptance_basis"]]
                if basis:
                    acceptance = {"event_id": snapshot["event_id"], **basis[0]}
                    break

        return {
            "facility_id": facility_id, "as_of": at_text, "status": status,
            "site_id": facility["site_id"], "location_desc": facility["location_desc"],
            "facility_type": facility["facility_type"],
            "open_event": open_event and {"event_id": open_event["event_id"],
                                          "chain_id": open_event["chain_id"],
                                          "risk_level": open_event["risk_level"],
                                          "frozen_provider_org_id": open_event["frozen_provider_org_id"],
                                          "current_provider_org_id": open_event["current_provider_org_id"]},
            "acceptance_basis": acceptance,
            "notifications": notifications,
            "chains": chain_snapshots,
        }

    # ---- 内部工具 --------------------------------------------------------

    def _spawn_withdrawn_event(self, connection, *, parent, facility, withdrawn_evidence,
                               reason, actor_id, now) -> tuple[str, str]:
        chain_seq = connection.execute(
            "SELECT MAX(chain_seq) AS m FROM defect_events WHERE chain_id=?", (parent["chain_id"],)
        ).fetchone()["m"] + 1
        event_id = uuid.uuid4().hex
        opened_dt = parse_iso(now, "now")
        # 撤回时的责任方以父事件最新工单为准（可能已经跨单位移交），并在新环节重新冻结。
        latest_wo = connection.execute(
            "SELECT * FROM work_orders WHERE event_id=? ORDER BY seq DESC LIMIT 1",
            (parent["event_id"],),
        ).fetchone()
        provider = latest_wo["provider_organization_id"]
        contract_id = latest_wo["contract_id"]
        sla = self._sla_for(connection, contract_id, json.loads(parent["frozen_sla_json"]))
        level = parent["risk_level"]
        connection.execute(
            "INSERT INTO defect_events(event_id,chain_id,chain_seq,facility_id,report_type,parent_event_id,"
            "description,impact_groups_json,risk_level,reported_risk_level,escalated,status,"
            "frozen_contract_id,frozen_provider_org_id,frozen_sla_json,repeat_count,reported_by,"
            "opened_at,closed_at,closed_evidence_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, parent["chain_id"], chain_seq, parent["facility_id"], EVENT_EVIDENCE_WITHDRAWN,
             parent["event_id"], f"复开依据撤回：{reason}",
             parent["impact_groups_json"], level, level, int(parent["escalated"]), EVENT_OPEN,
             contract_id, provider, canonical_json(sla),
             parent["repeat_count"], actor_id, now, None, None),
        )
        work_order_id = self._open_work_order(
            connection, event=event_id, chain_id=parent["chain_id"], seq=1,
            provider=provider, contract_id=contract_id,
            sla=sla, level=level, opened_at=opened_dt, created_by=actor_id,
        )
        connection.execute(
            "UPDATE accessibility_facilities SET status=?, current_event_id=?, version=version+1 "
            "WHERE facility_id=?",
            (FACILITY_STATUS_OUT_OF_SERVICE, event_id, parent["facility_id"]),
        )
        connection.execute(
            "INSERT INTO facility_status_history(facility_id,status,reason,event_id,changed_by,changed_at) "
            "VALUES(?,?,?,?,?,?)",
            (parent["facility_id"], FACILITY_STATUS_OUT_OF_SERVICE, "evidence_withdrawn",
             event_id, actor_id, now),
        )
        return event_id, work_order_id

    def _open_work_order(self, connection, *, event, chain_id, seq, provider, contract_id,
                         sla, level, opened_at, created_by) -> str:
        work_order_id = uuid.uuid4().hex
        due_response = opened_at + _dt.timedelta(hours=sla[level]["response"])
        due_repair = opened_at + _dt.timedelta(hours=sla[level]["repair"])
        connection.execute(
            "INSERT INTO work_orders(work_order_id,chain_id,event_id,seq,provider_organization_id,"
            "contract_id,status,due_response_at,due_repair_at,responded_at,completed_at,"
            "response_breached_at,repair_breached_at,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (work_order_id, chain_id, event, seq, provider, contract_id, WO_OPEN,
             self._iso(due_response), self._iso(due_repair), None, None, None, None,
             created_by, self._iso(opened_at)),
        )
        return work_order_id

    def _notify(self, connection, *, chain_id, event_id, facility_id, organization_id,
                role=None, actor_id=None, reason=None, now, channel="in_system") -> None:
        connection.execute(
            "INSERT INTO notifications(notification_id,chain_id,event_id,facility_id,target_actor_id,"
            "target_organization_id,target_role,channel,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, chain_id, event_id, facility_id, actor_id, organization_id,
             role, channel, reason, now),
        )

    @staticmethod
    def _iso(value) -> str:
        return value.astimezone().isoformat().replace("+00:00", "Z")

    @staticmethod
    def _groups(values) -> set[str]:
        if not isinstance(values, list) or not values:
            raise ValidationError("affected_groups 必须是非空数组")
        groups = set(values)
        if not groups <= AFFECTED_GROUPS:
            raise ValidationError("affected_groups 含不合法的人群标识")
        return groups

    @staticmethod
    def _site(connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    @staticmethod
    def _facility(connection, facility_id: str):
        row = connection.execute(
            "SELECT * FROM accessibility_facilities WHERE facility_id=?", (facility_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("设施不存在")
        return row

    @staticmethod
    def _event(connection, event_id: str):
        row = connection.execute("SELECT * FROM defect_events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise NotFoundError("缺陷事件不存在")
        return row

    @staticmethod
    def _work_order(connection, work_order_id: str):
        row = connection.execute(
            "SELECT * FROM work_orders WHERE work_order_id=?", (work_order_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("工单不存在")
        return row

    @staticmethod
    def _measure(connection, measure_id: str):
        row = connection.execute(
            "SELECT * FROM interim_measures WHERE measure_id=?", (measure_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("临时措施不存在")
        return row

    @staticmethod
    def _require_open(event) -> None:
        if event["status"] == EVENT_CLOSED:
            raise ConflictError("事件已经关闭")

    def _require_provider_actor(self, actor, provider_org_id: str) -> None:
        self._require(actor, *PROVIDER_ROLES)
        if actor.role != "admin" and actor.organization_id != provider_org_id:
            raise PermissionDenied("操作者不属于当前冻结责任单位")

    def _current_provider(self, connection, event_id: str):
        """返回事件链上最新工单对应的当前责任方（工单不可变，移交只新增工单）。"""

        row = connection.execute(
            "SELECT provider_organization_id, contract_id FROM work_orders "
            "WHERE event_id=? ORDER BY seq DESC LIMIT 1", (event_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("事件尚无工单")
        return row["provider_organization_id"], row["contract_id"]

    def _sla_for(self, connection, contract_id, fallback):
        if contract_id is None:
            return fallback
        row = connection.execute("SELECT sla_json FROM maintenance_contracts WHERE contract_id=?",
                                 (contract_id,)).fetchone()
        return json.loads(row["sla_json"]) if row is not None else fallback

    def _active_contract(self, connection, facility, now_dt):
        now_text = self._iso(now_dt)
        return connection.execute(
            "SELECT * FROM maintenance_contracts WHERE site_id=? AND active=1 "
            "AND valid_from<=? AND valid_to>? AND "
            "(facility_id=? OR (facility_id IS NULL AND (facility_type=? OR facility_type IS NULL))) "
            "ORDER BY (facility_id IS NOT NULL) DESC, valid_from DESC LIMIT 1",
            (facility["site_id"], now_text, now_text, facility["facility_id"], facility["facility_type"]),
        ).fetchone()

    def _facility_view(self, facility) -> dict[str, Any]:
        return {"facility_id": facility["facility_id"], "site_id": facility["site_id"],
                "facility_type": facility["facility_type"], "location_desc": facility["location_desc"],
                "affected_groups": json.loads(facility["affected_groups_json"]),
                "status": facility["status"], "current_event_id": facility["current_event_id"]}

    def _wo_view(self, row) -> dict[str, Any]:
        return {"work_order_id": row["work_order_id"], "seq": row["seq"], "status": row["status"],
                "provider_organization_id": row["provider_organization_id"],
                "contract_id": row["contract_id"], "due_response_at": row["due_response_at"],
                "due_repair_at": row["due_repair_at"], "responded_at": row["responded_at"],
                "completed_at": row["completed_at"],
                "response_breached_at": row["response_breached_at"],
                "repair_breached_at": row["repair_breached_at"], "created_at": row["created_at"]}

    @staticmethod
    def _measure_view(row) -> dict[str, Any]:
        return {"measure_id": row["measure_id"], "kind": row["kind"], "description": row["description"],
                "provider_organization_id": row["provider_organization_id"],
                "linked_from_measure_id": row["linked_from_measure_id"],
                "effective_at": row["effective_at"], "expires_at": row["expires_at"],
                "status": row["status"], "handed_to_org_id": row["handed_to_org_id"],
                "handed_over_at": row["handed_over_at"]}

    @staticmethod
    def _evidence_view(row) -> dict[str, Any]:
        return {"evidence_id": row["evidence_id"], "work_order_id": row["work_order_id"],
                "result": row["result"], "summary": row["summary"], "evidence_ref": row["evidence_ref"],
                "evidence_hash": row["evidence_hash"], "status": row["status"],
                "inspector_actor_id": row["inspector_actor_id"],
                "inspector_organization_id": row["inspector_organization_id"],
                "inspected_at": row["inspected_at"], "withdrawn_at": row["withdrawn_at"],
                "withdrawn_by": row["withdrawn_by"], "withdraw_reason": row["withdraw_reason"]}


def facility_org(connection, facility) -> str:
    return connection.execute("SELECT organization_id FROM sites WHERE site_id=?",
                              (facility["site_id"],)).fetchone()["organization_id"]


def facility_org_id(connection, event) -> str:
    return connection.execute(
        "SELECT s.organization_id FROM sites s JOIN accessibility_facilities f ON f.site_id=s.site_id "
        "WHERE f.facility_id=?", (event["facility_id"],)).fetchone()["organization_id"]
