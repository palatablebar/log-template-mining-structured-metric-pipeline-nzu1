"""模板库查询 / 确认 / 噪声标记 / 按采集站拆分。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.constants import error_codes as codes
from src.constants.messages import (
    MSG_TEMPLATE_CONFIRMED,
    MSG_TEMPLATE_IGNORED,
    MSG_TEMPLATE_ROLLBACK_DONE,
    MSG_TEMPLATE_SPLIT_DONE,
)
from src.middlewares.auth import current_ip
from src.models.user import User
from src.services import audit_service, template_service, template_split_service
from src.utils.errors import AppError


def list_templates(
    db: Session,
    *,
    status: str | None = None,
    source_id: int | None = None,
    search: str | None = None,
    include_noise: bool = False,
    limit: int = 200,
) -> dict:
    items = template_service.list_templates(
        db, status=status, source_id=source_id, search=search, include_noise=include_noise, limit=limit
    )
    return {"items": items, "total": len(items)}


def get_template(db: Session, template_id: int) -> dict:
    return template_service.get_template(db, template_id)


def confirm(db: Session, template_id: int, user: User, request: Request) -> dict:
    payload = template_service.confirm_template(db, template_id, user.email)
    audit_service.record(
        db,
        user=user,
        action="templates.confirm",
        entity="log_template",
        entity_id=str(template_id),
        detail={"status": payload["status"]},
        client_ip=current_ip(request),
    )
    db.commit()
    return {"id": template_id, "status": payload["status"], "message": MSG_TEMPLATE_CONFIRMED}


def mark_noise(db: Session, template_id: int, user: User, request: Request) -> dict:
    payload = template_service.mark_noise(db, template_id, user.email)
    audit_service.record(
        db,
        user=user,
        action="templates.mark_noise",
        entity="log_template",
        entity_id=str(template_id),
        detail={"status": payload["status"]},
        client_ip=current_ip(request),
    )
    db.commit()
    return {"id": template_id, "status": payload["status"], "message": MSG_TEMPLATE_IGNORED}


def split_preview(db: Session, template_id: int) -> dict:
    """拆分前影响范围（含已拆分模板的上次结果回显），只读不落库。"""
    return template_split_service.build_impact_preview(db, template_id)


def split_execute(db: Session, template_id: int, user: User, request: Request) -> dict:
    """按采集站一次性拆分：事件归站、规则按站复制、告警按桶归属，全部在单事务内。"""
    try:
        result = template_split_service.execute_split(db, template_id, user.email)
    except IntegrityError as exc:
        # 并发拆分竞态：(template_id, job_type) 唯一约束或子模板签名唯一约束兜底
        db.rollback()
        raise AppError(
            codes.TEMPLATE_SPLIT_CONFLICT,
            "模板已拆分或正在被其他操作处理，重复执行不会再次生效",
            f"template_id={template_id} integrity={exc.orig}",
        ) from exc
    audit_service.record(
        db,
        user=user,
        action="templates.split",
        entity="log_template",
        entity_id=str(template_id),
        detail={
            "job_id": result["job"]["id"],
            "moved_events": result["job"]["moved_events"],
            "moved_alerts": result["job"]["moved_alerts"],
            "duplicated_rules": result["job"]["duplicated_rules"],
            "child_template_ids": result["job"]["child_template_ids"],
        },
        client_ip=current_ip(request),
    )
    db.commit()
    result["message"] = MSG_TEMPLATE_SPLIT_DONE
    return result


def split_rollback(db: Session, template_id: int, user: User, request: Request) -> dict:
    """拆分失败/误拆后的恢复入口：按回滚快照把事件、规则、告警、子模板恢复到拆分前。"""
    result = template_split_service.rollback_split(db, template_id, user.email)
    audit_service.record(
        db,
        user=user,
        action="templates.split_rollback",
        entity="log_template",
        entity_id=str(template_id),
        detail={
            "job_id": result["job"]["id"],
            "restored_events": result["restored_events"],
            "restored_alerts": result["restored_alerts"],
            "removed_rules": result["removed_rules"],
            "removed_children": result["removed_children"],
            "drift": result["drift"],
        },
        client_ip=current_ip(request),
    )
    db.commit()
    result["message"] = MSG_TEMPLATE_ROLLBACK_DONE
    return result


def split_jobs(db: Session) -> dict:
    items = template_split_service.list_jobs(db)
    return {"items": items, "total": len(items)}
