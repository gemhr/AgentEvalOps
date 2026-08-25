"""Feature Risk Review demo/evaluation layer 的最小 typed contracts。"""

# ruff: noqa: D101,D415

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StrictStr, model_validator


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class CoverageState(StrEnum):
    """测试覆盖状态；空 TestCase[] 不等于零覆盖。"""

    NO_TEST_DATA = "NO_TEST_DATA"
    PLAN_ONLY = "PLAN_ONLY"
    PARTIAL_COVERAGE = "PARTIAL_COVERAGE"
    COVERED = "COVERED"


class Priority(StrEnum):
    """报告处理次序；不是 P0/P1/P2 bug severity 映射。"""

    COMPLETE_REVIEW = "COMPLETE_REVIEW"
    ACT_NOW = "ACT_NOW"
    SCHEDULE_REVIEW = "SCHEDULE_REVIEW"
    MONITOR = "MONITOR"


class ReportCompleteness(StrEnum):
    """报告完整性；PARTIAL_* 表示有分支缺失，不表示风险已覆盖。"""

    FULL = "FULL"
    PARTIAL_RISK_UNAVAILABLE = "PARTIAL_RISK_UNAVAILABLE"
    PARTIAL_TEST_UNAVAILABLE = "PARTIAL_TEST_UNAVAILABLE"


class ReportUncertainty(_Contract):
    """带来源标签的确定性 uncertainty 条目；不含 numeric confidence。"""

    branch: StrictStr = Field(min_length=1)
    message: StrictStr = Field(min_length=1)


class AnnotationStatus(StrEnum):
    PENDING = "PENDING"
    HUMAN_REVIEWED = "HUMAN_REVIEWED"


class EvidenceRef(_Contract):
    """指向一个离线快照中的真实或人工标注来源。"""

    evidence_id: StrictStr = Field(min_length=1)
    source_type: StrictStr = Field(min_length=1)
    source_id: StrictStr = Field(min_length=1)
    source_path: StrictStr = Field(min_length=1)
    source_url: HttpUrl
    section: StrictStr | None = None


class FeatureDocument(_Contract):
    """仅包含可交给后续 DocumentAnalysisAgent 的 feature 内容。"""

    case_id: StrictStr = Field(min_length=1)
    feature_id: StrictStr = Field(min_length=1)
    title: StrictStr = Field(min_length=1)
    agent_visible_content: StrictStr = Field(min_length=1)
    source: EvidenceRef


class FeatureChangePoint(_Contract):
    description: StrictStr = Field(min_length=1)
    affected_components: list[StrictStr] = Field(min_length=1)
    change_type: StrictStr | None = None
    potential_risk_areas: list[StrictStr] = Field(default_factory=list)


class HistoricalIssue(_Contract):
    issue_id: StrictStr = Field(min_length=1)
    title: StrictStr = Field(min_length=1)
    description: StrictStr
    component: StrictStr = Field(min_length=1)
    labels: list[StrictStr] = Field(default_factory=list)
    state: StrictStr = Field(min_length=1)
    severity: StrictStr | None = None
    curated_severity: RiskLevel | None = None
    annotation_source: StrictStr | None = None
    evidence_ref: EvidenceRef

    @model_validator(mode="after")
    def _curation_is_labeled(self) -> "HistoricalIssue":
        if self.curated_severity is not None and not self.annotation_source:
            raise ValueError("curated_severity requires annotation_source")
        return self


class TestPlan(_Contract):
    plan_id: StrictStr = Field(min_length=1)
    case_id: StrictStr = Field(min_length=1)
    content: StrictStr = Field(min_length=1)
    evidence_ref: EvidenceRef


class TestCase(_Contract):
    test_case_id: StrictStr = Field(min_length=1)
    description: StrictStr = Field(min_length=1)
    evidence_ref: EvidenceRef


class RiskFinding(_Contract):
    description: StrictStr = Field(min_length=1)
    affected_components: list[StrictStr] = Field(default_factory=list)
    risk_area: StrictStr = Field(min_length=1)
    historical_issue_refs: list[StrictStr] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(min_length=1)
    uncertainty: StrictStr | None = None


class FeatureRiskReviewReport(_Contract):
    case_id: StrictStr = Field(min_length=1)
    feature_summary: StrictStr = Field(min_length=1)
    change_points: list[FeatureChangePoint] = Field(default_factory=list)
    high_risk_scenarios: list[RiskFinding] = Field(default_factory=list)
    historical_issues: list[HistoricalIssue] = Field(default_factory=list)
    existing_coverage: list[TestPlan] = Field(default_factory=list)
    existing_test_cases: list[TestCase] = Field(default_factory=list)
    coverage_state: CoverageState | None = None
    coverage_assessment: StrictStr | None = None
    potential_gaps: list[StrictStr] = Field(default_factory=list)
    missing_cases: list[StrictStr] = Field(default_factory=list)
    risk_level: RiskLevel | None = None
    priority: Priority | None = None
    completeness: ReportCompleteness = ReportCompleteness.FULL
    unavailable_sections: list[StrictStr] = Field(default_factory=list)
    uncertainties: list[ReportUncertainty] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    uncertainty: StrictStr | None = None


class EvaluationAnnotation(_Contract):
    """评价侧人工标注；不由正常 FeatureRiskReviewCase loader 返回。"""

    case_id: StrictStr = Field(min_length=1)
    annotation_status: AnnotationStatus
    expected_change_points: list[StrictStr] = Field(default_factory=list)
    expected_components: list[StrictStr] = Field(default_factory=list)
    expected_risk_areas: list[StrictStr] = Field(default_factory=list)
    expected_historical_issue_ids: list[StrictStr] = Field(default_factory=list)
    expected_coverage_gaps: list[StrictStr] = Field(default_factory=list)
    expected_risk_level: RiskLevel | None = None
    annotation_source: StrictStr = Field(min_length=1)


class FeatureRiskReviewCase(_Contract):
    """后续 demo path 可加载的业务投影；不携带 evaluation annotation。"""

    feature_document: FeatureDocument
    historical_issues: list[HistoricalIssue] = Field(min_length=1)
    test_plans: list[TestPlan] = Field(min_length=1)
    test_cases: list[TestCase] = Field(default_factory=list)
