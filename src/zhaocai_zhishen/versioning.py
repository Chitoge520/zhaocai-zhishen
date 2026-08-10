"""集中管理比赛版本、数据契约和算法版本标识。"""

from __future__ import annotations

PACKAGE_VERSION = "0.3.0"
COMPETITION_RELEASE = "competition-m0-2026.08"
BASELINE_SCHEMA_VERSION = "bid-audit-baseline/v1"
AUDIT_SCHEMA_VERSION = "bid-audit/v1"
EVIDENCE_SCHEMA_VERSION = "bid-audit-evidence/v1"
ALGORITHM_VERSION = "bid-audit-unsupervised/v1"
MODEL_SCHEMA_VERSION = "bid-anomaly-model/v1"


def version_payload() -> dict[str, str]:
    """返回可写入产物和接口的统一版本字段。"""

    return {
        "package_version": PACKAGE_VERSION,
        "competition_release": COMPETITION_RELEASE,
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "model_schema_version": MODEL_SCHEMA_VERSION,
    }