// Package catalog 拥有项目内 Case/Dataset/Suite 的发布身份。
package catalog

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/cigovernance"
	"agentevalops/go-backend/internal/citriage"
)

type CaseType string

const (
	AgentTask  CaseType = "AGENT_TASK"
	Golden     CaseType = "GOLDEN"
	Regression CaseType = "REGRESSION"
	Security   CaseType = "SECURITY"
	RAG        CaseType = "RAG"
)

type Criticality string

const (
	Normal   Criticality = "NORMAL"
	Critical Criticality = "CRITICAL"
)

type BodyPolicy string

const (
	Retained      BodyPolicy = "RETAINED"
	Redacted      BodyPolicy = "REDACTED"
	ReferenceOnly BodyPolicy = "REFERENCE_ONLY"
)

type Assertion struct {
	ID       string     `json:"assertion_id"`
	Kind     string     `json:"kind"`
	Config   asset.JSON `json:"config"`
	Required bool       `json:"required"`
}
type CaseContent struct {
	Input              asset.JSON          `json:"input"`
	TaskGoal           string              `json:"task_goal"`
	AcceptanceCriteria []string            `json:"acceptance_criteria"`
	ExpectedOutput     asset.JSON          `json:"expected_output"`
	Assertions         []Assertion         `json:"assertions"`
	GroundTruth        asset.JSON          `json:"ground_truth"`
	Applicability      asset.Applicability `json:"applicability"`
	Type               CaseType            `json:"case_type"`
	Capability         string              `json:"capability"`
	Criticality        Criticality         `json:"criticality"`
	Tags               []string            `json:"tags"`
	Metadata           asset.JSON          `json:"metadata"`
	BodyPolicy         BodyPolicy          `json:"body_policy"`
}

func (c CaseContent) Validate() error {
	if _, err := cigovernance.ReadPolicy(c.Metadata); err != nil {
		return err
	}
	if c.Capability == "CI_FAILURE_TRIAGE" {
		gt, err := citriage.ReadGroundTruth(c.GroundTruth)
		if err != nil || gt.Criticality != string(c.Criticality) || citriage.ValidateInput(c.Input) != nil || c.ExpectedOutput.String() != "null" {
			return asset.ErrInvalid
		}
	}
	switch c.Type {
	case AgentTask, Golden, Regression, Security, RAG:
	default:
		return asset.ErrInvalid
	}
	switch c.BodyPolicy {
	case Retained, Redacted, ReferenceOnly:
	default:
		return asset.ErrInvalid
	}
	if c.Criticality != Normal && c.Criticality != Critical {
		return asset.ErrInvalid
	}
	if !asset.ContentText(c.TaskGoal) || !asset.Text(c.Capability) || len(c.AcceptanceCriteria) == 0 {
		return asset.ErrInvalid
	}
	for _, s := range c.AcceptanceCriteria {
		if !asset.ContentText(s) {
			return asset.ErrInvalid
		}
	}
	seen := map[string]bool{}
	for _, a := range c.Assertions {
		if !asset.Text(a.ID) || !asset.Text(a.Kind) || seen[a.ID] {
			return asset.ErrInvalid
		}
		seen[a.ID] = true
	}
	return c.Applicability.Validate()
}

type DatasetContent struct {
	Cases    []asset.Ref `json:"ordered_cases"`
	Metadata asset.JSON  `json:"metadata"`
}

func validateCases(refs []asset.Ref) error {
	if len(refs) == 0 {
		return asset.ErrInvalid
	}
	seen := map[string]bool{}
	for _, r := range refs {
		if r.Validate() != nil || seen[r.EntityID] {
			return asset.ErrInvalid
		}
		seen[r.EntityID] = true
	}
	return nil
}
func (d DatasetContent) Validate() error {
	if _, err := cigovernance.ReadPolicy(d.Metadata); err != nil {
		return err
	}
	return validateCases(d.Cases)
}

type EvaluatorBinding struct {
	Evaluator asset.Ref   `json:"evaluator"`
	Metrics   []asset.Ref `json:"metrics"`
	// Required 仅表达 Gate criticality；列入 manifest 的 optional evaluator 仍须完成。
	Required      bool                `json:"required"`
	Applicability asset.Applicability `json:"applicability"`
}
type SuiteContent struct {
	Dataset       *asset.Ref          `json:"dataset_version"`
	Cases         []asset.Ref         `json:"explicit_cases"`
	Evaluators    []EvaluatorBinding  `json:"evaluator_bindings"`
	Metrics       []asset.Ref         `json:"metric_versions"`
	Policy        *asset.PolicyRef    `json:"policy_version"`
	Applicability asset.Applicability `json:"applicability"`
	Metadata      asset.JSON          `json:"metadata"`
}

func (s SuiteContent) Validate() error {
	if (s.Dataset == nil) == (len(s.Cases) == 0) {
		return asset.ErrInvalid
	}
	if s.Dataset != nil {
		if err := s.Dataset.Validate(); err != nil {
			return err
		}
	} else if err := validateCases(s.Cases); err != nil {
		return err
	}
	if len(s.Evaluators) == 0 || len(s.Metrics) == 0 {
		return asset.ErrInvalid
	}
	metrics := map[asset.Ref]bool{}
	for _, r := range s.Metrics {
		if r.Validate() != nil || metrics[r] {
			return asset.ErrInvalid
		}
		metrics[r] = true
	}
	evaluators := map[asset.Ref]bool{}
	boundMetrics := map[asset.Ref]bool{}
	for _, b := range s.Evaluators {
		if b.Evaluator.Validate() != nil || evaluators[b.Evaluator] || len(b.Metrics) == 0 || b.Applicability.Validate() != nil {
			return asset.ErrInvalid
		}
		evaluators[b.Evaluator] = true
		seen := map[asset.Ref]bool{}
		for _, r := range b.Metrics {
			if !metrics[r] || seen[r] {
				return asset.ErrInvalid
			}
			seen[r] = true
			boundMetrics[r] = true
		}
	}
	if len(boundMetrics) != len(metrics) {
		return asset.ErrInvalid
	}
	if s.Policy != nil {
		if err := s.Policy.Validate(); err != nil {
			return err
		}
	}
	return s.Applicability.Validate()
}

type CaseVersion = asset.Version[CaseContent]
type DatasetVersion = asset.Version[DatasetContent]
type SuiteVersion = asset.Version[SuiteContent]
