from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional, Dict
import json
import uuid


class ReleaseType(Enum):
    NORMAL = "NORMAL"
    HOTFIX = "HOTFIX"


class ReleaseStatus(Enum):
    PENDING = "pending"
    PRE_CHECKING = "pre_checking"
    PRE_CHECK_FAILED = "pre_check_failed"
    PRE_CHECK_PASSED = "pre_check_passed"
    PENDING_APPROVAL = "pending_approval"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_APPROVED = "approval_approved"
    GRAYSCALE_DEPLOYING = "grayscale_deploying"
    FULL_DEPLOYING = "full_deploying"
    DEPLOYED = "deployed"
    CIRCUIT_BREAKER_TRIGGERED = "circuit_breaker_triggered"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    CANCELLED = "cancelled"


class ApprovalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    POST_SIGN = "post_sign"


class CheckResultStatus(Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    ERROR = "error"


class CircuitBreakerAction(Enum):
    CIRCUIT_BREAK = "circuit_break"
    WARNING = "warning"
    IGNORE = "ignore"


class DrillStatus(Enum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class PreCheckItem:
    metric_key: str
    metric_name: str
    threshold: float
    actual_value: float
    unit: str
    status: CheckResultStatus
    critical: bool = True
    fix_suggestion: str = ""
    checked_at: str = field(default_factory=lambda: datetime.now().isoformat())
    sample_size: Optional[int] = None
    period: Optional[str] = None
    trend: Optional[str] = None
    extra_info: Optional[Dict] = None
    raw_data: Optional[Dict] = None

    @property
    def is_pass(self) -> bool:
        return self.status == CheckResultStatus.PASS

    @property
    def passed(self) -> bool:
        return self.status == CheckResultStatus.PASS

    @property
    def current_value(self) -> float:
        return self.actual_value

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["passed"] = self.passed
        d["current_value"] = self.actual_value
        return d


@dataclass
class PreCheckReport:
    release_id: str
    items: list = field(default_factory=list)
    all_passed: bool = False
    checked_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "release_id": self.release_id,
            "items": [item.to_dict() for item in self.items],
            "all_passed": self.all_passed,
            "checked_at": self.checked_at,
        }


@dataclass
class ApprovalRecord:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    release_id: str = ""
    level: int = 1
    role: str = ""
    approver_id: str = ""
    approver_name: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    comment: str = ""
    approved_at: Optional[str] = None
    timeout_minutes: int = 60
    is_post_sign: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class MonitorMetric:
    metric_key: str
    metric_name: str
    value: float
    threshold: float
    unit: str
    is_breach: bool
    collected_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MonitorSnapshot:
    release_id: str
    phase_name: str
    round_number: int
    metrics: list = field(default_factory=list)
    has_breach: bool = False
    collected_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "release_id": self.release_id,
            "phase_name": self.phase_name,
            "round_number": self.round_number,
            "metrics": [m.to_dict() for m in self.metrics],
            "has_breach": self.has_breach,
            "collected_at": self.collected_at,
        }


@dataclass
class CircuitBreakerEvent:
    release_id: str
    trigger_metric: str
    trigger_value: float
    threshold: float
    affected_parks: list = field(default_factory=list)
    affected_zones: list = field(default_factory=list)
    trigger_time: str = field(default_factory=lambda: datetime.now().isoformat())
    rollback_started_at: Optional[str] = None
    rollback_completed_at: Optional[str] = None
    report_generated: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RollbackReport:
    release_id: str
    reason: str
    trigger_metric: str
    trigger_value: float
    threshold: float
    affected_parks: list = field(default_factory=list)
    affected_zones: list = field(default_factory=list)
    rollback_from_version: str = ""
    rollback_to_version: str = ""
    rollback_started_at: Optional[str] = None
    rollback_completed_at: Optional[str] = None
    duration_seconds: float = 0.0
    monitor_restarted: bool = False
    notification_sent: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReleaseRecord:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    version: str = ""
    previous_version: str = ""
    release_type: ReleaseType = ReleaseType.NORMAL
    status: ReleaseStatus = ReleaseStatus.PENDING
    branch: str = ""
    labels: list = field(default_factory=list)
    applicant: str = ""
    applicant_id: str = ""
    description: str = ""
    hotfix_reason: str = ""
    target_parks: list = field(default_factory=list)
    grayscale_strategy: str = "by_zone"
    current_phase_index: int = 0
    pre_check_report: Optional[dict] = None
    approval_records: list = field(default_factory=list)
    monitor_snapshots: list = field(default_factory=list)
    rollback_report: Optional[dict] = None
    circuit_breaker_event: Optional[dict] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    deployed_at: Optional[str] = None
    rolled_back_at: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["release_type"] = self.release_type.value
        d["status"] = self.status.value
        return d


@dataclass
class DrillRecord:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    scheduled_at: str = ""
    executed_at: Optional[str] = None
    completed_at: Optional[str] = None
    status: DrillStatus = DrillStatus.SCHEDULED
    target_parks: list = field(default_factory=list)
    rollback_duration_seconds: float = 0.0
    circuit_breaker_response_seconds: float = 0.0
    result_detail: str = ""
    issues_found: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class AuditLogEntry:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    release_id: str = ""
    action: str = ""
    actor: str = ""
    actor_role: str = ""
    detail: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    checksum: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
