// Package evaluation 拥有冻结的 Run 输入与 durable evaluation 合同。
package evaluation

import (
	"context"
	"fmt"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/metric"
)

const SnapshotContract = "stage12.run-input.v1"

type Origin string

const (
	PublishedCatalog Origin = "PUBLISHED_CATALOG"
	LegacySnapshot   Origin = "LEGACY_SNAPSHOT"
)

type AssetIdentity struct {
	Ref              asset.Ref  `json:"ref"`
	ProjectID        string     `json:"project_id"`
	Algorithm        string     `json:"algorithm_ref"`
	ContentDigest    string     `json:"content_digest"`
	SemanticDigest   string     `json:"semantic_digest"`
	CanonicalContent asset.JSON `json:"canonical_content"`
}

func identity[T any](v asset.Version[T]) (AssetIdentity, error) {
	j, err := asset.ParseJSON(v.Bytes())
	return AssetIdentity{v.Ref(), v.ProjectID(), v.Algorithm(), v.ContentDigest(), v.SemanticDigest(), j}, err
}

type CaseInput struct {
	Identity AssetIdentity       `json:"identity"`
	Case     catalog.CaseContent `json:"case"`
}
type EvaluatorSpec struct {
	Identity      AssetIdentity              `json:"identity"`
	Definition    metric.EvaluatorDefinition `json:"definition"`
	Metrics       []asset.Ref                `json:"metric_bindings"`
	Required      bool                       `json:"required"`
	Applicability asset.Applicability        `json:"applicability"`
}
type MetricInput struct {
	Identity   AssetIdentity     `json:"identity"`
	Definition metric.Definition `json:"definition"`
}
type LegacyCaseInput struct {
	CaseID          string     `json:"case_id"`
	CaseVersion     string     `json:"case_version"`
	AttemptID       string     `json:"attempt_id"`
	AttemptNo       int        `json:"attempt_no"`
	RequestSnapshot asset.JSON `json:"request_snapshot"`
}
type LegacyInput struct {
	ProjectID          string            `json:"project_id"`
	SourceRunID        string            `json:"source_run_id"`
	DatasetSnapshot    asset.JSON        `json:"dataset_snapshot"`
	SuiteSnapshot      asset.JSON        `json:"suite_snapshot"`
	TargetSnapshot     asset.JSON        `json:"target_snapshot"`
	SubjectSnapshot    asset.JSON        `json:"subject_snapshot"`
	Attempts           []LegacyCaseInput `json:"attempt_snapshots"`
	DigestVerification string            `json:"digest_verification"`
}
type SnapshotInput struct {
	Contract   string           `json:"contract_version"`
	ProjectID  string           `json:"project_id"`
	Origin     Origin           `json:"origin"`
	Suite      *AssetIdentity   `json:"suite_version"`
	Dataset    *AssetIdentity   `json:"dataset_version"`
	Manifest   []CaseInput      `json:"ordered_manifest"`
	Evaluators []EvaluatorSpec  `json:"evaluator_specs"`
	Metrics    []MetricInput    `json:"metric_bindings"`
	Policy     *asset.PolicyRef `json:"policy_version"`
	Legacy     *LegacyInput     `json:"legacy"`
}

// Snapshot 拥有完整 canonical bytes；G2 持久化它，不从当前配置补齐来源。
type Snapshot struct{ input asset.JSON }

func (s Snapshot) Bytes() []byte     { return s.input.Bytes() }
func (s Snapshot) Digest() string    { return s.input.Digest() }
func (s Snapshot) Algorithm() string { return asset.CatalogAlgorithm }
func (s Snapshot) Input() SnapshotInput {
	var v SnapshotInput
	if err := s.input.Decode(&v); err != nil {
		panic(err)
	}
	return v
}
func (s Snapshot) MarshalJSON() ([]byte, error) { return s.Bytes(), nil }

type PublishedReader interface {
	catalog.SuiteReferenceReader
	GetSuiteVersion(context.Context, asset.Scope, asset.Ref) (catalog.SuiteVersion, error)
}
type LegacyReader interface {
	ReadLegacyRunInput(context.Context, asset.Scope, string) (LegacyInput, error)
}
type Builder struct {
	Assets PublishedReader
	Legacy LegacyReader
}
type BuildRunSnapshot struct {
	Suite         *asset.Ref
	Dataset       *asset.Ref
	SelectedCases []asset.Ref
	// 仅 Dataset 路径可指定 bindings；Suite 路径使用已发布的完整 manifest。
	Evaluators []catalog.EvaluatorBinding
}

func scoped[T any](v asset.Version[T], scope asset.Scope, ref asset.Ref) error {
	if v.ProjectID() != scope.ProjectID || v.Ref() != ref {
		return asset.ErrNotFound
	}
	return nil
}
func (b Builder) BuildRunSnapshot(ctx context.Context, scope asset.Scope, cmd BuildRunSnapshot) (Snapshot, error) {
	if err := scope.Validate(false); err != nil {
		return Snapshot{}, err
	}
	if (cmd.Suite == nil) == (cmd.Dataset == nil) {
		return Snapshot{}, asset.ErrInvalid
	}
	input := SnapshotInput{Contract: SnapshotContract, ProjectID: scope.ProjectID, Origin: PublishedCatalog}
	var cases []asset.Ref
	bindings := cmd.Evaluators
	if cmd.Suite != nil {
		if len(cmd.Evaluators) > 0 {
			return Snapshot{}, asset.ErrInvalid
		}
		suite, err := b.Assets.GetSuiteVersion(ctx, scope, *cmd.Suite)
		if err != nil {
			return Snapshot{}, err
		}
		if err = scoped(suite, scope, *cmd.Suite); err != nil {
			return Snapshot{}, err
		}
		id, err := identity(suite)
		if err != nil {
			return Snapshot{}, err
		}
		input.Suite = &id
		body := suite.Content().Body
		cases = body.Cases
		bindings = body.Evaluators
		input.Policy = body.Policy
		if body.Dataset != nil {
			cmd.Dataset = body.Dataset
		}
	}
	if cmd.Dataset != nil {
		dataset, err := b.Assets.GetDatasetVersion(ctx, scope, *cmd.Dataset)
		if err != nil {
			return Snapshot{}, err
		}
		if err = scoped(dataset, scope, *cmd.Dataset); err != nil {
			return Snapshot{}, err
		}
		id, err := identity(dataset)
		if err != nil {
			return Snapshot{}, err
		}
		input.Dataset = &id
		cases = dataset.Content().Body.Cases
	}
	if len(cases) == 0 || len(bindings) == 0 {
		return Snapshot{}, asset.ErrInvalid
	}
	selected := map[asset.Ref]bool{}
	for _, r := range cmd.SelectedCases {
		if r.Validate() != nil || selected[r] {
			return Snapshot{}, asset.ErrInvalid
		}
		selected[r] = true
	}
	available := map[asset.Ref]bool{}
	for _, r := range cases {
		available[r] = true
	}
	for r := range selected {
		if !available[r] {
			return Snapshot{}, asset.ErrInvalid
		}
	}
	for _, r := range cases {
		if len(selected) > 0 && !selected[r] {
			continue
		}
		v, err := b.Assets.GetCaseVersion(ctx, scope, r)
		if err != nil {
			return Snapshot{}, err
		}
		if err = scoped(v, scope, r); err != nil {
			return Snapshot{}, err
		}
		id, err := identity(v)
		if err != nil {
			return Snapshot{}, err
		}
		input.Manifest = append(input.Manifest, CaseInput{id, v.Content().Body})
	}
	seenEvaluators := map[asset.Ref]bool{}
	seenMetrics := map[asset.Ref]bool{}
	for _, binding := range bindings {
		if binding.Evaluator.Validate() != nil || binding.Applicability.Validate() != nil || seenEvaluators[binding.Evaluator] {
			return Snapshot{}, asset.ErrInvalid
		}
		seenEvaluators[binding.Evaluator] = true
		v, err := b.Assets.GetEvaluatorDefinitionVersion(ctx, scope, binding.Evaluator)
		if err != nil {
			return Snapshot{}, err
		}
		if err = scoped(v, scope, binding.Evaluator); err != nil {
			return Snapshot{}, err
		}
		body := v.Content().Body
		if err = catalog.ValidateMetricBinding(binding, body.OutputMetrics); err != nil {
			return Snapshot{}, err
		}
		id, err := identity(v)
		if err != nil {
			return Snapshot{}, err
		}
		input.Evaluators = append(input.Evaluators, EvaluatorSpec{id, body, binding.Metrics, binding.Required, binding.Applicability})
		for _, r := range binding.Metrics {
			if seenMetrics[r] {
				continue
			}
			seenMetrics[r] = true
			m, err := b.Assets.GetMetricDefinitionVersion(ctx, scope, r)
			if err != nil {
				return Snapshot{}, err
			}
			if err = scoped(m, scope, r); err != nil {
				return Snapshot{}, err
			}
			id, err := identity(m)
			if err != nil {
				return Snapshot{}, err
			}
			input.Metrics = append(input.Metrics, MetricInput{id, m.Content().Body})
		}
	}
	frozen, err := asset.Freeze(input)
	if err != nil {
		return Snapshot{}, err
	}
	return Snapshot{frozen}, nil
}
func (b Builder) BuildLegacyRunSnapshot(ctx context.Context, scope asset.Scope, runID string) (Snapshot, error) {
	if err := scope.Validate(false); err != nil {
		return Snapshot{}, err
	}
	if !asset.ValidID(runID) {
		return Snapshot{}, asset.ErrInvalid
	}
	legacy, err := b.Legacy.ReadLegacyRunInput(ctx, scope, runID)
	if err != nil {
		return Snapshot{}, err
	}
	if legacy.ProjectID != scope.ProjectID || legacy.SourceRunID != runID {
		return Snapshot{}, asset.ErrNotFound
	}
	// 旧 JSONB 不能证明原 float 类型信息；保留历史 digest，不重新生成旧 Case/Spec hash。
	legacy.DigestVerification = "DIGEST_UNVERIFIABLE"
	frozen, err := asset.Freeze(SnapshotInput{Contract: SnapshotContract, ProjectID: scope.ProjectID, Origin: LegacySnapshot, Legacy: &legacy})
	if err != nil {
		return Snapshot{}, fmt.Errorf("legacy frozen input: %w", err)
	}
	return Snapshot{frozen}, nil
}
