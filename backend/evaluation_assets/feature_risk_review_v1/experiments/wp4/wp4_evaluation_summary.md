# WP4 Evaluation Summary

- Ground truth state: GROUND_TRUTH_READY
- Total cases: 5
- E2E workflow success: 0.800
- E2E status counts: SUCCESS=4 PARTIAL=0 FAILED=1 ENVIRONMENT_FAILURE=0
- Report generation success: 0.800
- Change point: status=EVALUATED TP=16 FP=8 FN=0 P=0.667 R=1.000 F1=0.800
- Component: status=EVALUATED TP=14 FP=21 FN=9 P=0.400 R=0.609 F1=0.483
- Risk area: status=EVALUATED TP=12 FP=29 FN=4 P=0.293 R=0.750 F1=0.421
- Historical evidence @5: status=EVALUATED TP=2 FP=0 FN=10 P=1.000 R=0.167 F1=0.286
- Historical issue finding: status=EVALUATED TP=6 FP=0 FN=6 P=1.000 R=0.500 F1=0.667
- Coverage gap: status=EVALUATED TP=6 FP=40 FN=9 P=0.130 R=0.400 F1=0.197
- Risk level accuracy: status=EVALUATED correct=2/4 value=0.500
- Citation correctness: status=EVALUATED numerator=20 denominator=35 value=0.571 unverifiable=0 rubric={'supported': 20, 'partially_supported': 11, 'unsupported': 4, 'unverifiable': 0}
- Priority correctness: NOT_EVALUATED
- Citation completeness: NOT_EVALUATED
- Token usage: NOT_EVALUATED
- Cost: NOT_EVALUATED
- Top5 source-type composition: {"kubernetes_enhancement_proposal": 18, "kubernetes_issue_snapshot": 2}

## k8s_541

- execution_status: SUCCESS
- execution_classification: BUSINESS_RESULT
- retry_eligible: no
- report_generation: status=EVALUATED numerator=1 denominator=1
- report_completeness: FULL
- change_point: status=EVALUATED TP=4 FP=2 FN=0 P=0.667 R=1.000 F1=0.800
- component: status=EVALUATED TP=2 FP=3 FN=1 P=0.400 R=0.667 F1=0.500
- risk_area: status=EVALUATED TP=2 FP=4 FN=2 P=0.333 R=0.500 F1=0.400
- historical_evidence_at_5: status=EVALUATED TP=0 FP=0 FN=3 P=None R=0.000 F1=0.000
- historical_issue_finding: status=EVALUATED TP=1 FP=0 FN=2 P=1.000 R=0.333 F1=0.500
- coverage_gap: status=EVALUATED TP=0 FP=10 FN=3 P=0.000 R=0.000 F1=0.000
- risk_level: status=EVALUATED predicted=HIGH expected=MEDIUM correct=False
- citation_correctness: status=EVALUATED numerator=5 denominator=11 value=0.455 unverifiable=0
- top5_source_type_composition: {"kubernetes_enhancement_proposal": 5}

## k8s_753

- execution_status: SUCCESS
- execution_classification: BUSINESS_RESULT
- retry_eligible: no
- report_generation: status=EVALUATED numerator=1 denominator=1
- report_completeness: FULL
- change_point: status=EVALUATED TP=4 FP=5 FN=0 P=0.444 R=1.000 F1=0.615
- component: status=EVALUATED TP=3 FP=6 FN=1 P=0.333 R=0.750 F1=0.462
- risk_area: status=EVALUATED TP=4 FP=19 FN=0 P=0.174 R=1.000 F1=0.296
- historical_evidence_at_5: status=EVALUATED TP=0 FP=0 FN=3 P=None R=0.000 F1=0.000
- historical_issue_finding: status=EVALUATED TP=1 FP=0 FN=2 P=1.000 R=0.333 F1=0.500
- coverage_gap: status=EVALUATED TP=2 FP=18 FN=2 P=0.100 R=0.500 F1=0.167
- risk_level: status=EVALUATED predicted=HIGH expected=HIGH correct=True
- citation_correctness: status=EVALUATED numerator=7 denominator=9 value=0.778 unverifiable=0
- top5_source_type_composition: {"kubernetes_enhancement_proposal": 5}

## k8s_1287

- execution_status: FAILED
- execution_classification: BUSINESS_RESULT
- retry_eligible: no
- report_generation: status=EVALUATED numerator=0 denominator=1
- change_point: status=NOT_EVALUATED TP=0 FP=0 FN=0 P=None R=None F1=None
- component: status=EVALUATED TP=1 FP=7 FN=5 P=0.125 R=0.167 F1=0.143
- risk_area: status=NOT_EVALUATED TP=0 FP=0 FN=0 P=None R=None F1=None
- historical_evidence_at_5: status=EXECUTION_FAILED TP=0 FP=0 FN=0 P=None R=None F1=None
- historical_issue_finding: status=EXECUTION_FAILED TP=0 FP=0 FN=0 P=None R=None F1=None
- coverage_gap: status=EXECUTION_FAILED TP=0 FP=0 FN=0 P=None R=None F1=None
- risk_level: status=EXECUTION_FAILED predicted=None expected=None correct=None
- citation_correctness: status=EXECUTION_FAILED numerator=0 denominator=0 value=None unverifiable=0

## k8s_1472

- execution_status: SUCCESS
- execution_classification: BUSINESS_RESULT
- retry_eligible: no
- report_generation: status=EVALUATED numerator=1 denominator=1
- report_completeness: FULL
- change_point: status=EVALUATED TP=4 FP=0 FN=0 P=1.000 R=1.000 F1=1.000
- component: status=EVALUATED TP=3 FP=4 FN=1 P=0.429 R=0.750 F1=0.545
- risk_area: status=EVALUATED TP=3 FP=4 FN=1 P=0.429 R=0.750 F1=0.545
- historical_evidence_at_5: status=EVALUATED TP=2 FP=0 FN=1 P=1.000 R=0.667 F1=0.800
- historical_issue_finding: status=EVALUATED TP=3 FP=0 FN=0 P=1.000 R=1.000 F1=1.000
- coverage_gap: status=EVALUATED TP=2 FP=5 FN=2 P=0.286 R=0.500 F1=0.364
- risk_level: status=EVALUATED predicted=HIGH expected=MEDIUM correct=False
- citation_correctness: status=EVALUATED numerator=5 denominator=7 value=0.714 unverifiable=0
- top5_source_type_composition: {"kubernetes_enhancement_proposal": 3, "kubernetes_issue_snapshot": 2}

## k8s_1602

- execution_status: SUCCESS
- execution_classification: BUSINESS_RESULT
- retry_eligible: no
- report_generation: status=EVALUATED numerator=1 denominator=1
- report_completeness: FULL
- change_point: status=EVALUATED TP=4 FP=1 FN=0 P=0.800 R=1.000 F1=0.889
- component: status=EVALUATED TP=5 FP=1 FN=1 P=0.833 R=0.833 F1=0.833
- risk_area: status=EVALUATED TP=3 FP=2 FN=1 P=0.600 R=0.750 F1=0.667
- historical_evidence_at_5: status=EVALUATED TP=0 FP=0 FN=3 P=None R=0.000 F1=0.000
- historical_issue_finding: status=EVALUATED TP=1 FP=0 FN=2 P=1.000 R=0.333 F1=0.500
- coverage_gap: status=EVALUATED TP=2 FP=7 FN=2 P=0.222 R=0.500 F1=0.308
- risk_level: status=EVALUATED predicted=HIGH expected=HIGH correct=True
- citation_correctness: status=EVALUATED numerator=3 denominator=8 value=0.375 unverifiable=0
- top5_source_type_composition: {"kubernetes_enhancement_proposal": 5}

