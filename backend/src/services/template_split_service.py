"""模板按采集站拆分：影响范围预览 → 一次性执行 → 快照回滚。

背景：网关（gateway-svc）与支付（pay-svc）两个采集站发出相同日志骨架，Drain 按模板串
归并成同一个模板，两站事件混在一条模板里，桶计数与告警判定都是混合口径。本服务把混合
模板拆成「每站一个子模板」，并保证：

1. 先给影响范围（每站事件量 / error 数 / 桶数 / 模板上规则与告警数），确认后一次性迁移；
2. 事件按 log_event.source_id 归到各站子模板；模板级规则按站复制，原规则留在混合模板上
   （已无事件，扫描不会再命中），子规则只扫本站子模板，不会扫到另一站；
3. 历史告警按「告警桶内主导采集站」归到对应子模板并切换到该站规则，原始归属写入回滚快照；
4. 全程单事务，任何一步失败整体回滚；rollback_snapshot 支持事后按快照恢复；
5. 幂等：同一模板存在 completed 作业时重复执行直接 409，不会二次生效，更不会把已拆记录
   挪回去；回滚后允许重新拆分（复用同一作业行，重算确定性签名与快照）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from src.constants import error_codes as codes
from src.constants.enums import EventLevel, SplitJobStatus, TemplateStatus
from src.models.alert_event import AlertEvent
from src.models.alert_rule import AlertRule
from src.models.base import now_local
from src.models.log_event import LogEvent
from src.models.log_source import LogSource
from src.models.log_template import LogTemplate
from src.models.template_split_job import TemplateSplitJob
from src.utils.errors import AppError
from src.utils.text import sha1_text
from src.utils.logger import get_logger

logger = get_logger("logmine.split")

ERROR_LEVELS = (EventLevel.ERROR.value, EventLevel.FATAL.value)
JOB_TYPE_BY_SOURCE = "by_source"
PREVIEW_BUCKET_LIMIT = 48


# ---------------------------------------------------------------- 序列化
def _iso(value: datetime | None) -> str | None:
    return value.isoformat(sep=" ") if value else None


def _child_signature(parent_id: int, source_id: int, template_str: str) -> str:
    """子模板确定性签名：同 (parent, source) 重算结果一致，回滚后重拆也不会产生新签名。"""
    return sha1_text(f"split:{parent_id}:{source_id}:{template_str}")


def serialize_job(job: TemplateSplitJob) -> dict:
    return {
        "id": job.id,
        "template_id": job.template_id,
        "job_type": job.job_type,
        "status": job.status,
        "impact_snapshot": job.impact_snapshot or {},
        "result_snapshot": job.result_snapshot or {},
        "child_template_ids": list(job.child_template_ids or []),
        "child_rule_ids": list(job.child_rule_ids or []),
        "moved_events": job.moved_events,
        "moved_alerts": job.moved_alerts,
        "duplicated_rules": job.duplicated_rules,
        "created_by": job.created_by,
        "rolled_back_by": job.rolled_back_by,
        "executed_at": _iso(job.executed_at),
        "rolled_back_at": _iso(job.rolled_back_at),
    }


def get_job(db: Session, template_id: int) -> TemplateSplitJob | None:
    return db.execute(
        select(TemplateSplitJob).where(
            TemplateSplitJob.template_id == template_id,
            TemplateSplitJob.job_type == JOB_TYPE_BY_SOURCE,
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------- 影响范围
def _per_source_stats(db: Session, template_id: int) -> list[dict]:
    rows = db.execute(
        select(
            LogEvent.source_id,
            LogSource.name,
            LogSource.service,
            func.count(LogEvent.id),
            func.sum(case((LogEvent.level.in_(ERROR_LEVELS), 1), else_=0)),
            func.min(LogEvent.event_time),
            func.max(LogEvent.event_time),
            func.count(func.distinct(LogEvent.bucket_start)),
        )
        .join(LogSource, LogSource.id == LogEvent.source_id)
        .where(LogEvent.template_id == template_id)
        .group_by(LogEvent.source_id, LogSource.name, LogSource.service)
        .order_by(func.count(LogEvent.id).desc())
    ).all()
    return [
        {
            "source_id": int(row[0]),
            "source_name": row[1],
            "service": row[2],
            "event_count": int(row[3] or 0),
            "error_count": int(row[4] or 0),
            "first_seen_at": _iso(row[5]),
            "last_seen_at": _iso(row[6]),
            "bucket_count": int(row[7] or 0),
        }
        for row in rows
    ]


def _per_source_buckets(db: Session, template_id: int) -> list[dict]:
    """最近 N 个桶 × 采集站的计数，给前端展示拆分前后口径变化。"""
    latest_bucket = db.execute(
        select(func.max(LogEvent.bucket_start)).where(LogEvent.template_id == template_id)
    ).scalar_one_or_none()
    if latest_bucket is None:
        return []
    rows = db.execute(
        select(
            LogEvent.bucket_start,
            LogEvent.source_id,
            func.count(LogEvent.id),
            func.sum(case((LogEvent.level.in_(ERROR_LEVELS), 1), else_=0)),
        )
        .where(LogEvent.template_id == template_id, LogEvent.bucket_start.is_not(None))
        .group_by(LogEvent.bucket_start, LogEvent.source_id)
        .order_by(LogEvent.bucket_start.desc())
        .limit(PREVIEW_BUCKET_LIMIT * 4)
    ).all()
    out = [
        {
            "bucket_start": _iso(bucket_at),
            "source_id": int(source_id),
            "count": int(count or 0),
            "error_count": int(errors or 0),
        }
        for bucket_at, source_id, count, errors in rows
    ]
    out.reverse()
    return out[-PREVIEW_BUCKET_LIMIT * 2 :]


def build_impact_preview(db: Session, template_id: int) -> dict:
    """拆分前影响范围；若已拆分则同时回显上次拆分结果（前端展示前后对比）。"""
    template = db.get(LogTemplate, template_id)
    if template is None:
        raise AppError(codes.TEMPLATE_NOT_FOUND, "模板不存在", f"template_id={template_id}")

    job = get_job(db, template_id)
    per_source = _per_source_stats(db, template_id)
    total_events = sum(item["event_count"] for item in per_source)

    parent_rules = db.execute(
        select(AlertRule).where(AlertRule.template_id == template_id).order_by(AlertRule.id)
    ).scalars().all()
    parent_alerts = int(
        db.execute(
            select(func.count()).select_from(AlertEvent).where(AlertEvent.template_id == template_id)
        ).scalar_one()
    )

    payload = {
        "template_id": template_id,
        "template_str": template.template_str,
        "template_status": template.status,
        "is_noise": bool(template.is_noise),
        "splittable": len(per_source) >= 2 and not template.is_noise,
        "reason": "" if len(per_source) >= 2 else "模板事件只来自一个采集站，无需拆分",
        "total_events": total_events,
        "sources": per_source,
        "buckets": _per_source_buckets(db, template_id),
        "rules_on_template": [
            {"rule_id": rule.id, "name": rule.name, "metric": rule.metric, "enabled": bool(rule.enabled)}
            for rule in parent_rules
        ],
        "alerts_on_template": parent_alerts,
        "global_rules_note": "全局规则（不绑定模板）拆分后自动按各站子模板独立计数，无需复制。",
    }

    if job is not None:
        payload["job"] = serialize_job(job)
        payload["already_split"] = job.status == SplitJobStatus.COMPLETED.value
        if job.status == SplitJobStatus.COMPLETED.value:
            payload["reason"] = "该模板已完成拆分，重复执行不会再次生效"
            child_rows = db.execute(
                select(LogTemplate.id, LogTemplate.source_id, LogTemplate.frequency)
                .where(LogTemplate.id.in_(job.child_template_ids or [0]))
            ).all()
            source_names = dict(db.execute(select(LogSource.id, LogSource.name)).all())
            rule_counts = dict(
                db.execute(
                    select(AlertRule.template_id, func.count(AlertRule.id))
                    .where(AlertRule.template_id.in_(job.child_template_ids or [0]))
                    .group_by(AlertRule.template_id)
                ).all()
            )
            alert_counts = dict(
                db.execute(
                    select(AlertEvent.template_id, func.count(AlertEvent.id))
                    .where(AlertEvent.template_id.in_(job.child_template_ids or [0]))
                    .group_by(AlertEvent.template_id)
                ).all()
            )
            payload["children_now"] = [
                {
                    "template_id": int(row[0]),
                    "source_id": row[1],
                    "source_name": source_names.get(row[1], ""),
                    "event_count": int(row[2]),
                    "rule_count": int(rule_counts.get(row[0], 0)),
                    "alert_count": int(alert_counts.get(row[0], 0)),
                }
                for row in child_rows
            ]
    else:
        payload["job"] = None
        payload["already_split"] = False
    return payload


# ---------------------------------------------------------------- 执行拆分
def _get_or_create_child(
    db: Session,
    *,
    parent: LogTemplate,
    source: LogSource,
    signatures_seen: dict[str, LogTemplate],
) -> LogTemplate:
    signature = _child_signature(parent.id, source.id, parent.template_str)
    if signature in signatures_seen:
        return signatures_seen[signature]
    existing = db.execute(
        select(LogTemplate).where(LogTemplate.signature == signature)
    ).scalar_one_or_none()
    if existing is not None:
        signatures_seen[signature] = existing
        return existing
    child = LogTemplate(
        signature=signature,
        template_str=parent.template_str,
        token_count=parent.token_count,
        slot_types=list(parent.slot_types or []),
        slot_names=list(parent.slot_names or []),
        source_id=source.id,
        service=source.service,
        sample_line=parent.sample_line,
        level_hint=parent.level_hint,
        status=TemplateStatus.MINED.value,
        is_noise=False,
        frequency=0,
        first_seen_at=None,
        last_seen_at=None,
        origin="split",
        confirmed_by="",
    )
    db.add(child)
    db.flush()
    signatures_seen[signature] = child
    return child


def _refresh_template_stats(db: Session, template_id: int) -> None:
    stats = db.execute(
        select(
            func.count(),
            func.min(LogEvent.event_time),
            func.max(LogEvent.event_time),
        ).where(LogEvent.template_id == template_id)
    ).first()
    template = db.get(LogTemplate, template_id)
    if template is None:
        return
    template.frequency = int(stats[0] or 0)
    template.first_seen_at = stats[1]
    template.last_seen_at = stats[2]
    template.updated_at = now_local()


def _bucket_source_attribution(db: Session, template_id: int) -> dict[datetime, int]:
    """每个告警桶内的主导采集站（事件数最多；并列取 source_id 小的，保证确定性）。"""
    rows = db.execute(
        select(
            LogEvent.bucket_start,
            LogEvent.source_id,
            func.count(LogEvent.id),
        )
        .where(LogEvent.template_id == template_id, LogEvent.bucket_start.is_not(None))
        .group_by(LogEvent.bucket_start, LogEvent.source_id)
    ).all()
    winners: dict[datetime, tuple[int, int]] = {}
    for bucket_at, source_id, count in rows:
        current = winners.get(bucket_at)
        if current is None or int(count) > current[1] or (int(count) == current[1] and int(source_id) < current[0]):
            winners[bucket_at] = (int(source_id), int(count))
    return {bucket_at: value[0] for bucket_at, value in winners.items()}


def execute_split(db: Session, template_id: int, actor: str) -> dict:
    """按采集站拆分混合模板。单事务；调用方（controller）负责 commit 与审计。"""
    parent = db.get(LogTemplate, template_id)
    if parent is None:
        raise AppError(codes.TEMPLATE_NOT_FOUND, "模板不存在", f"template_id={template_id}")

    job = get_job(db, template_id)
    if job is not None and job.status == SplitJobStatus.COMPLETED.value:
        raise AppError(
            codes.TEMPLATE_SPLIT_CONFLICT,
            "该模板已拆分，重复执行不会再次生效",
            f"template_id={template_id} job_id={job.id}",
        )
    if parent.is_noise:
        raise AppError(codes.TEMPLATE_SINGLE_SOURCE, "噪声模板不支持拆分", f"template_id={template_id}")

    per_source = _per_source_stats(db, template_id)
    if len(per_source) < 2:
        raise AppError(
            codes.TEMPLATE_SINGLE_SOURCE,
            "模板事件只来自一个采集站，无需拆分",
            f"template_id={template_id} sources={len(per_source)}",
        )

    started_at = now_local()
    sources = {
        row.id: row
        for row in db.execute(select(LogSource).where(LogSource.id.in_([item["source_id"] for item in per_source])))
        .scalars()
        .all()
    }

    # 1) 建/取各站子模板
    signatures_seen: dict[str, LogTemplate] = {}
    children: dict[int, LogTemplate] = {}
    for item in per_source:
        source = sources[item["source_id"]]
        children[source.id] = _get_or_create_child(db, parent=parent, source=source, signatures_seen=signatures_seen)

    # 2) 模板级规则按站复制（原规则保留在混合模板上，已无事件，扫描不会再命中）
    parent_rules = db.execute(
        select(AlertRule).where(AlertRule.template_id == parent.id).order_by(AlertRule.id)
    ).scalars().all()
    rule_copies: dict[int, dict[int, AlertRule]] = {}  # {origin_rule_id: {source_id: copy}}
    duplicated_rule_rows: list[dict] = []
    for rule in parent_rules:
        rule_copies[rule.id] = {}
        for source_id, child in children.items():
            copy_name = f"{rule.name} · {sources[source_id].name}（模板#{parent.id}）"
            copy = db.execute(select(AlertRule).where(AlertRule.name == copy_name)).scalar_one_or_none()
            if copy is None:
                copy = AlertRule(
                    name=copy_name,
                    template_id=child.id,
                    metric=rule.metric,
                    window_minutes=rule.window_minutes,
                    baseline_buckets=rule.baseline_buckets,
                    z_threshold=rule.z_threshold,
                    error_ratio_low=rule.error_ratio_low,
                    error_ratio_high=rule.error_ratio_high,
                    silence_minutes=rule.silence_minutes,
                    enabled=bool(rule.enabled),
                    description=f"[模板拆分] 源自规则「{rule.name}」，仅作用于 {sources[source_id].name} 子模板。",
                    created_by=actor,
                )
                db.add(copy)
                db.flush()
            else:
                copy.template_id = child.id
                copy.enabled = bool(rule.enabled)
                db.flush()
            rule_copies[rule.id][source_id] = copy
            duplicated_rule_rows.append(
                {
                    "new_rule_id": copy.id,
                    "source_id": source_id,
                    "origin_rule_id": rule.id,
                    "origin_name": rule.name,
                }
            )

    # 3) 历史告警：按桶内主导采集站归属（在事件迁移前算，原始口径还在 parent 上）
    attribution = _bucket_source_attribution(db, parent.id)
    fallback_source_id = per_source[0]["source_id"]
    parent_alerts = db.execute(
        select(AlertEvent).where(AlertEvent.template_id == parent.id).order_by(AlertEvent.id)
    ).scalars().all()
    alert_snapshot: list[dict] = []
    for alert in parent_alerts:
        source_id = attribution.get(alert.bucket_start, fallback_source_id)
        child = children[source_id]
        new_rule = rule_copies.get(alert.rule_id, {}).get(source_id)
        alert_snapshot.append(
            {
                "alert_id": alert.id,
                "template_id_before": parent.id,
                "rule_id_before": alert.rule_id,
                "assigned_source_id": source_id,
                "child_template_id": child.id,
                "new_rule_id": new_rule.id if new_rule else alert.rule_id,
            }
        )
        alert.template_id = child.id
        if new_rule is not None:
            alert.rule_id = new_rule.id
    db.flush()

    # 4) 事件一次性归到各站子模板（只动当前仍挂在 parent 上的事件，天然不会碰已拆记录）
    moved_events: list[dict] = []
    for source_id, child in children.items():
        moved = int(
            db.execute(
                select(func.count())
                .select_from(LogEvent)
                .where(LogEvent.template_id == parent.id, LogEvent.source_id == source_id)
            ).scalar_one()
        )
        db.execute(
            update(LogEvent)
            .where(LogEvent.template_id == parent.id, LogEvent.source_id == source_id)
            .values(template_id=child.id)
        )
        moved_events.append({"source_id": source_id, "child_template_id": child.id, "count": moved})
    db.flush()

    # 5) 频次 / 首末时间按真实计数刷新；parent 保留为拆分痕迹（status=split，0 事件）
    for child in children.values():
        _refresh_template_stats(db, child.id)
    parent_status_before = parent.status
    parent_source_before = parent.source_id
    parent_frequency_before = parent.frequency
    parent_first_seen_before = parent.first_seen_at
    parent_last_seen_before = parent.last_seen_at
    parent.status = TemplateStatus.SPLIT.value
    parent.source_id = None
    parent.frequency = 0
    parent.updated_at = now_local()
    db.flush()

    impact_snapshot = {
        "template_id": parent.id,
        "template_str": parent.template_str,
        "parent_status_before": parent_status_before,
        "parent_source_id_before": parent_source_before,
        "parent_frequency_before": parent_frequency_before,
        "total_events": sum(item["event_count"] for item in per_source),
        "sources": per_source,
        "rules_on_parent": [{"rule_id": rule.id, "name": rule.name} for rule in parent_rules],
        "alerts_on_parent": len(parent_alerts),
    }
    rollback_snapshot = {
        "parent": {
            "id": parent.id,
            "status_before": parent_status_before,
            "source_id_before": parent_source_before,
            "frequency_before": parent_frequency_before,
            "first_seen_before": _iso(parent_first_seen_before),
            "last_seen_before": _iso(parent_last_seen_before),
        },
        "children": [
            {
                "template_id": child.id,
                "source_id": source_id,
                "signature": child.signature,
            }
            for source_id, child in children.items()
        ],
        "moved_events": moved_events,
        "rules": {"duplicated": duplicated_rule_rows},
        "alerts": alert_snapshot,
    }
    result_snapshot = {
        "after": [
            {
                "source_id": source_id,
                "source_name": sources[source_id].name,
                "template_id": child.id,
                "event_count": next(
                    (item["count"] for item in moved_events if item["source_id"] == source_id), 0
                ),
                "rule_ids": [row["new_rule_id"] for row in duplicated_rule_rows if row["source_id"] == source_id],
                "alert_count": sum(1 for row in alert_snapshot if row["assigned_source_id"] == source_id),
            }
            for source_id, child in children.items()
        ],
        "moved_events_total": sum(item["count"] for item in moved_events),
        "moved_alerts_total": len(alert_snapshot),
        "duplicated_rules_total": len(duplicated_rule_rows),
    }

    # 6) 作业行：复用历史行（回滚后重拆）或新建；唯一约束保证同模板只有一条
    if job is None:
        job = TemplateSplitJob(
            template_id=parent.id,
            job_type=JOB_TYPE_BY_SOURCE,
            status=SplitJobStatus.COMPLETED.value,
            created_by=actor,
        )
        db.add(job)
    else:
        job.status = SplitJobStatus.COMPLETED.value
        job.rolled_back_by = ""
        job.rolled_back_at = None
        job.created_by = actor
    job.impact_snapshot = impact_snapshot
    job.rollback_snapshot = rollback_snapshot
    job.result_snapshot = result_snapshot
    job.child_template_ids = [child.id for child in children.values()]
    job.child_rule_ids = [row["new_rule_id"] for row in duplicated_rule_rows]
    job.moved_events = result_snapshot["moved_events_total"]
    job.moved_alerts = result_snapshot["moved_alerts_total"]
    job.duplicated_rules = result_snapshot["duplicated_rules_total"]
    job.executed_at = started_at
    db.flush()

    logger.warning(
        "template.split executed template_id=%s children=%s events=%s alerts=%s rules=%s actor=%s",
        parent.id,
        job.child_template_ids,
        job.moved_events,
        job.moved_alerts,
        job.duplicated_rules,
        actor,
    )
    return {
        "job": serialize_job(job),
        "impact": impact_snapshot,
        "result": result_snapshot,
        "message": "ok",
    }


# ---------------------------------------------------------------- 回滚
def rollback_split(db: Session, template_id: int, actor: str) -> dict:
    """按 rollback_snapshot 把一次拆分完整恢复。单事务；controller 负责 commit 与审计。"""
    parent = db.get(LogTemplate, template_id)
    if parent is None:
        raise AppError(codes.TEMPLATE_NOT_FOUND, "模板不存在", f"template_id={template_id}")
    job = get_job(db, template_id)
    if job is None:
        raise AppError(codes.TEMPLATE_NOT_SPLIT, "该模板没有拆分记录，无法回滚", f"template_id={template_id}")
    if job.status != SplitJobStatus.COMPLETED.value:
        raise AppError(
            codes.TEMPLATE_ROLLBACK_CONFLICT,
            "拆分已回滚，不能重复回滚",
            f"template_id={template_id} status={job.status}",
        )

    snapshot = job.rollback_snapshot or {}
    parent_snap = snapshot.get("parent", {})
    children_snap = snapshot.get("children", [])
    events_snap = snapshot.get("moved_events", [])
    rules_snap = snapshot.get("rules", {}).get("duplicated", [])
    alerts_snap = snapshot.get("alerts", [])

    # 1) 事件从子模板迁回 parent（按 source 精确匹配，记录漂移量但不改写快照口径）
    restored_events = 0
    drift: list[dict] = []
    for item in events_snap:
        child_id = item["child_template_id"]
        source_id = item["source_id"]
        expected = int(item.get("count", 0))
        actual = int(
            db.execute(
                select(func.count())
                .select_from(LogEvent)
                .where(LogEvent.template_id == child_id, LogEvent.source_id == source_id)
            ).scalar_one()
        )
        db.execute(
            update(LogEvent)
            .where(LogEvent.template_id == child_id, LogEvent.source_id == source_id)
            .values(template_id=parent.id)
        )
        restored_events += actual
        if actual != expected:
            drift.append({"source_id": source_id, "expected": expected, "actual": actual})

    # 2) 告警恢复原始 template_id / rule_id
    restored_alerts = 0
    for item in alerts_snap:
        alert = db.get(AlertEvent, item["alert_id"])
        if alert is None:
            continue
        alert.template_id = item["template_id_before"]
        alert.rule_id = item["rule_id_before"]
        restored_alerts += 1
    db.flush()

    # 3) 删除拆分时复制的规则
    removed_rules = 0
    for item in rules_snap:
        rule = db.get(AlertRule, item["new_rule_id"])
        if rule is not None and rule.template_id in {row["template_id"] for row in children_snap}:
            db.delete(rule)
            removed_rules += 1
    db.flush()

    # 4) 删除子模板（事件已全部迁走，FK ondelete=SET NULL 兜底）
    removed_children = 0
    for item in children_snap:
        child = db.get(LogTemplate, item["template_id"])
        if child is not None:
            db.delete(child)
            removed_children += 1
    db.flush()

    # 5) parent 恢复拆分前状态与频次
    parent.status = parent_snap.get("status_before", TemplateStatus.MINED.value)
    parent.source_id = parent_snap.get("source_id_before")
    parent.frequency = int(parent_snap.get("frequency_before", 0))
    first_seen = parent_snap.get("first_seen_before")
    last_seen = parent_snap.get("last_seen_before")
    parent.first_seen_at = (
        datetime.fromisoformat(first_seen) if isinstance(first_seen, str) and first_seen else first_seen
    )
    parent.last_seen_at = (
        datetime.fromisoformat(last_seen) if isinstance(last_seen, str) and last_seen else last_seen
    )
    parent.updated_at = now_local()

    job.status = SplitJobStatus.ROLLED_BACK.value
    job.rolled_back_by = actor
    job.rolled_back_at = now_local()
    job.child_template_ids = []
    job.child_rule_ids = []
    job.moved_events = 0
    job.moved_alerts = 0
    job.duplicated_rules = 0
    db.flush()

    logger.warning(
        "template.split rollback template_id=%s events=%s alerts=%s rules=%s children=%s drift=%s actor=%s",
        parent.id,
        restored_events,
        restored_alerts,
        removed_rules,
        removed_children,
        drift,
        actor,
    )
    return {
        "job": serialize_job(job),
        "restored_events": restored_events,
        "restored_alerts": restored_alerts,
        "removed_rules": removed_rules,
        "removed_children": removed_children,
        "drift": drift,
    }


def list_jobs(db: Session, *, limit: int = 100) -> list[dict]:
    rows = db.execute(
        select(TemplateSplitJob).order_by(TemplateSplitJob.id.desc()).limit(limit)
    ).scalars().all()
    return [serialize_job(row) for row in rows]


def resolve_import_target(db: Session, template_row: LogTemplate, source_id: int | None) -> LogTemplate:
    """拆分后新导入同骨架日志的归属路由。

    命中的是 status=split 的父模板时，按事件采集站找到对应子模板；该站没有子模板
    （例如第三个新采集站也发了同骨架）则仍落到父模板，不静默丢数据。普通模板原样返回。
    """
    if template_row is None or source_id is None or template_row.status != TemplateStatus.SPLIT.value:
        return template_row
    child = db.execute(
        select(LogTemplate).where(
            LogTemplate.origin == "split",
            LogTemplate.source_id == source_id,
            LogTemplate.template_str == template_row.template_str,
        )
    ).scalars().first()
    return child or template_row
