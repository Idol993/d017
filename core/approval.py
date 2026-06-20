import logging
import re
from datetime import datetime
from typing import Dict, List, Optional

import yaml

from models.schemas import ApprovalRecord, ApprovalStatus, ReleaseType
from utils.audit_log import write_audit_log

logger = logging.getLogger(__name__)

_APPROVAL_MATRIX = {}


def load_approval_matrix(config_path: str = "config/approval_matrix.yaml") -> dict:
    global _APPROVAL_MATRIX
    with open(config_path, "r", encoding="utf-8") as f:
        _APPROVAL_MATRIX = yaml.safe_load(f)
    return _APPROVAL_MATRIX


def get_approval_matrix() -> dict:
    if not _APPROVAL_MATRIX:
        return load_approval_matrix()
    return _APPROVAL_MATRIX


class ReleaseTypeDetector:
    def __init__(self, matrix_config: Optional[dict] = None):
        self.matrix_config = matrix_config or get_approval_matrix()

    def detect(self, branch: str = "", labels: Optional[List[str]] = None) -> ReleaseType:
        labels = labels or []
        detection_rules = self.matrix_config.get("type_detection", {})

        branch_rules = detection_rules.get("branch_rules", {})
        for rtype, patterns in branch_rules.items():
            for pattern in patterns:
                regex = pattern.replace("*", ".*")
                if re.match(regex, branch):
                    logger.info("根据分支名 [%s] 识别为: %s", branch, rtype)
                    return ReleaseType(rtype.upper())

        label_rules = detection_rules.get("label_rules", {})
        for rtype, label_list in label_rules.items():
            if any(label in labels for label in label_list):
                logger.info("根据标签 %s 识别为: %s", labels, rtype)
                return ReleaseType(rtype.upper())

        logger.info("未匹配特殊规则，默认识别为: NORMAL")
        return ReleaseType.NORMAL


class ApprovalEngine:
    def __init__(self, matrix_config: Optional[dict] = None):
        self.matrix_config = matrix_config or get_approval_matrix()
        self.type_detector = ReleaseTypeDetector(self.matrix_config)

    def detect_release_type(self, branch: str = "", labels: Optional[List[str]] = None) -> ReleaseType:
        return self.type_detector.detect(branch, labels)

    def create_approval_flow(self, release_id: str, release_type: ReleaseType,
                              hotfix_reason: str = "") -> List[ApprovalRecord]:
        type_key = "normal" if release_type == ReleaseType.NORMAL else "hotfix"
        type_config = self.matrix_config.get("release_types", {}).get(type_key, {})
        flow_config = type_config.get("flow", [])
        approvers_config = self.matrix_config.get("approvers", {})

        records = []
        for step in flow_config:
            role = step["role"]
            level = step["level"]
            timeout = step.get("timeout_minutes", 60)
            role_approvers = approvers_config.get(role, [])

            for approver in role_approvers:
                record = ApprovalRecord(
                    release_id=release_id,
                    level=level,
                    role=role,
                    approver_id=approver["id"],
                    approver_name=approver["name"],
                    status=ApprovalStatus.PENDING,
                    timeout_minutes=timeout,
                    is_post_sign=False,
                )
                records.append(record)

        write_audit_log(
            release_id=release_id,
            action="approval_flow_created",
            actor="system",
            actor_role="automated",
            detail=f"创建审批流: 类型={type_key}, 审批步骤数={len(flow_config)}, "
                   f"审批人总数={len(records)}, 紧急原因={hotfix_reason or '无'}",
        )

        logger.info(
            "创建审批流: release_id=%s, type=%s, 审批记录数=%d",
            release_id, type_key, len(records),
        )
        return records

    def process_approval(self, release_id: str, records: List[ApprovalRecord],
                          approver_id: str, approved: bool,
                          comment: str = "",
                          release_type: Optional[ReleaseType] = None) -> Dict:
        target_record = None
        for record in records:
            if record.approver_id == approver_id and record.status == ApprovalStatus.PENDING:
                target_record = record
                break

        if target_record is None:
            return {
                "success": False,
                "message": f"未找到审批人 {approver_id} 的待审批记录",
            }

        if release_type == ReleaseType.NORMAL:
            levels = sorted(set(r.level for r in records))
            target_level = target_record.level

            for level in levels:
                if level >= target_level:
                    continue

                level_records = [r for r in records if r.level == level]
                level_pending = [r for r in level_records if r.status == ApprovalStatus.PENDING]
                level_rejected = [r for r in level_records if r.status == ApprovalStatus.REJECTED]

                if level_rejected:
                    return {
                        "success": False,
                        "message": f"串行审批模式下，级别 {level} 已被 {level_rejected[0].approver_name} 驳回，"
                                   f"不能继续审批级别 {target_level}",
                    }

                if level_pending:
                    level_approved_count = len([r for r in level_records if r.status == ApprovalStatus.APPROVED])
                    return {
                        "success": False,
                        "message": f"串行审批模式下，级别 {level} 还有 {len(level_pending)} 人待审批"
                                   f" (已通过 {level_approved_count}/{len(level_records)} 人)，"
                                   f"必须等该层所有审批人全部处理完成后，才能审批级别 {target_level}",
                    }

            same_level_records = [r for r in records if r.level == target_level]
            same_level_approved = [r for r in same_level_records if r.status == ApprovalStatus.APPROVED]
            if same_level_approved and len(same_level_records) > 1:
                logger.debug(
                    "级别 %d 审批进行中: 已通过 %d/%d, 当前审批人: %s",
                    target_level,
                    len(same_level_approved),
                    len(same_level_records),
                    target_record.approver_name,
                )

        if approved:
            target_record.status = ApprovalStatus.APPROVED
            target_record.comment = comment
            target_record.approved_at = datetime.now().isoformat()
        else:
            target_record.status = ApprovalStatus.REJECTED
            target_record.comment = comment
            target_record.approved_at = datetime.now().isoformat()

        write_audit_log(
            release_id=release_id,
            action="approval_decision",
            actor=target_record.approver_name,
            actor_role=target_record.role,
            detail=f"审批{'通过' if approved else '驳回'}: 角色={target_record.role}, "
                   f"级别={target_record.level}, 意见={comment or '无'}",
        )

        flow_result = self._evaluate_approval_flow(release_id, records, release_type)
        return {
            "success": True,
            "record": target_record.to_dict(),
            "flow_result": flow_result,
        }

    def _evaluate_approval_flow(self, release_id: str,
                                 records: List[ApprovalRecord],
                                 release_type: Optional[ReleaseType] = None) -> Dict:
        pending_records = [r for r in records if r.status == ApprovalStatus.PENDING]
        rejected_records = [r for r in records if r.status == ApprovalStatus.REJECTED]
        approved_records = [r for r in records if r.status == ApprovalStatus.APPROVED]

        if rejected_records:
            return {
                "status": "rejected",
                "message": f"审批被驳回: {rejected_records[0].approver_name} ({rejected_records[0].role})",
                "rejected_by": rejected_records[0].to_dict(),
            }

        if not pending_records and all(r.status == ApprovalStatus.APPROVED for r in records):
            return {
                "status": "approved",
                "message": "所有审批已通过",
            }

        if release_type is not None:
            type_key = "normal" if release_type == ReleaseType.NORMAL else "hotfix"
        else:
            levels = set(r.level for r in records)
            type_key = "hotfix" if any(r.level == 1 for r in records) and len(levels) == 1 else "normal"
        type_config = self.matrix_config.get("release_types", {}).get(type_key, {})
        approval_mode = type_config.get("approval_mode", "serial")

        if approval_mode == "serial":
            current_level = min(r.level for r in pending_records)
            current_level_pending = [r for r in pending_records if r.level == current_level]
            current_level_approved = [r for r in approved_records if r.level == current_level]

            if current_level_approved and not current_level_pending:
                return {
                    "status": "in_progress",
                    "message": f"当前级别 {current_level} 已通过，等待下一级别审批",
                    "current_level": current_level,
                }

            return {
                "status": "in_progress",
                "message": f"等待级别 {current_level} 审批中",
                "current_level": current_level,
                "pending_approvers": [r.to_dict() for r in current_level_pending],
            }
        else:
            return {
                "status": "in_progress",
                "message": "并行审批中",
                "pending_approvers": [r.to_dict() for r in pending_records],
            }

    def process_auto_approval_for_hotfix(self, release_id: str,
                                          records: List[ApprovalRecord],
                                          hotfix_reason: str) -> List[ApprovalRecord]:
        for record in records:
            record.status = ApprovalStatus.POST_SIGN
            record.approved_at = datetime.now().isoformat()
            record.comment = f"紧急热修复自动放行，事后补签。原因: {hotfix_reason}"
            record.is_post_sign = True

        write_audit_log(
            release_id=release_id,
            action="hotfix_auto_approved",
            actor="system",
            actor_role="automated",
            detail=f"紧急热修复自动放行，事后补签。原因: {hotfix_reason}",
        )

        logger.info("紧急热修复自动放行: release_id=%s, 原因=%s", release_id, hotfix_reason)
        return records

    def get_pending_approvals(self, records: List[ApprovalRecord]) -> List[Dict]:
        pending = [r for r in records if r.status == ApprovalStatus.PENDING]
        return [r.to_dict() for r in pending]
