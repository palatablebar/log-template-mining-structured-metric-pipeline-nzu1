from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, TimestampMixin, now_local


class TemplateSplitJob(Base, TimestampMixin):
    """模板按采集站拆分作业：一个混合模板一条（自然键 = template_id + job_type，保证重复执行只生效一次）。

    生命周期：
    - completed：拆分已执行，parent 模板状态置为 split，事件/规则/告警已归到各采集站子模板；
    - rolled_back：执行过回滚，事件/规则/告警快照已恢复，子模板已删除，parent 恢复原状态。
    impact_snapshot 存拆分前影响范围；rollback_snapshot 存回滚所需的全部原始归属，
    因此整条事务失败时由数据库回滚保证原子性，事后还能按快照恢复。
    """

    __tablename__ = "template_split_job"
    __table_args__ = (
        UniqueConstraint("template_id", "job_type", name="uq_split_job_template_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("log_template.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_type: Mapped[str] = mapped_column(String(32), nullable=False, default="by_source")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="completed", index=True)
    impact_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    rollback_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    child_template_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    child_rule_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    moved_events: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    moved_alerts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicated_rules: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    rolled_back_by: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_local, onupdate=now_local, nullable=False)
