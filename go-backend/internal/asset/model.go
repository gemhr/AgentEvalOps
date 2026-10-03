package asset

import (
	"crypto/rand"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

var (
	ErrInvalid     = errors.New("非法目录命令")
	ErrForbidden   = errors.New("缺少目录权限")
	ErrNotFound    = errors.New("项目内资源不存在")
	ErrConflict    = errors.New("目录身份或版本冲突")
	ErrUnsupported = errors.New("UNSUPPORTED")
)

var uuidPattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

func ValidID(id string) bool { return uuidPattern.MatchString(id) }
func NewID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	b[6] = (b[6] & 15) | 64
	b[8] = (b[8] & 63) | 128
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[:4], b[4:6], b[6:8], b[8:10], b[10:])
}
func Text(s string) bool        { return ContentText(s) && len(s) <= 255 }
func ContentText(s string) bool { return utf8.ValidString(s) && strings.TrimSpace(s) != "" }

// Scope 只能由已认证边界或受控 system/test principal 构建；不能直接信任 HTTP body。
type Scope struct {
	ProjectID, OrganizationID, Principal string
	CanPublish                           bool
}

func (s Scope) Validate(write bool) error {
	if !ValidID(s.ProjectID) || !ValidID(s.OrganizationID) || !Text(s.Principal) {
		return ErrInvalid
	}
	if write && !s.CanPublish {
		return ErrForbidden
	}
	return nil
}

type Ref struct {
	EntityID string `json:"entity_id"`
	Version  string `json:"version"`
}

func (r Ref) Validate() error {
	if !ValidID(r.EntityID) || !Text(r.Version) {
		return ErrInvalid
	}
	return nil
}

type Logical struct {
	ID, ProjectID, Name, CreatedBy string
	CreatedAt                      time.Time
}
type Create struct{ ID, Name string }

func (c Create) Validate() error {
	if !ValidID(c.ID) || !Text(c.Name) {
		return ErrInvalid
	}
	return nil
}

type Source struct {
	Kind      string `json:"kind"`
	Ref       string `json:"ref"`
	Principal string `json:"principal"`
	Metadata  JSON   `json:"metadata"`
}

func (s Source) Validate() error {
	if !Text(s.Kind) || !Text(s.Ref) || !Text(s.Principal) {
		return ErrInvalid
	}
	return nil
}

type Publish[T any] struct {
	Ref    Ref
	Body   T
	Source Source
}
type Content[T any] struct {
	Body   T      `json:"body"`
	Source Source `json:"source"`
}

// Version 仅提供读取方法；Body/Bytes 每次返回独立副本。
type Version[T any] struct {
	projectID      string
	ref            Ref
	content        JSON
	semanticDigest string
	publishedBy    string
	publishedAt    time.Time
}

func NewVersion[T any](project string, ref Ref, body T, source Source, actor string, at time.Time) (Version[T], error) {
	if !ValidID(project) || ref.Validate() != nil || source.Validate() != nil || !Text(actor) {
		return Version[T]{}, ErrInvalid
	}
	semantic, err := Freeze(body)
	if err != nil {
		return Version[T]{}, fmt.Errorf("%w: %v", ErrInvalid, err)
	}
	content, err := Freeze(Content[T]{body, source})
	if err != nil {
		return Version[T]{}, err
	}
	return Version[T]{project, ref, content, semantic.Digest(), actor, at}, nil
}
func Restore[T any](project string, ref Ref, raw []byte, contentDigest, semanticDigest, actor string, at time.Time) (Version[T], error) {
	var c Content[T]
	content, err := ParseJSON(raw)
	if err != nil {
		return Version[T]{}, err
	}
	if err = content.Decode(&c); err != nil {
		return Version[T]{}, err
	}
	v, err := NewVersion(project, ref, c.Body, c.Source, actor, at)
	if err != nil {
		return v, err
	}
	if v.content.String() != string(raw) || v.ContentDigest() != contentDigest || v.SemanticDigest() != semanticDigest {
		return Version[T]{}, fmt.Errorf("目录 canonical bytes/digest 损坏")
	}
	return v, nil
}
func (v Version[T]) Ref() Ref               { return v.ref }
func (v Version[T]) ProjectID() string      { return v.projectID }
func (v Version[T]) Algorithm() string      { return CatalogAlgorithm }
func (v Version[T]) ContentDigest() string  { return v.content.Digest() }
func (v Version[T]) SemanticDigest() string { return v.semanticDigest }
func (v Version[T]) PublishedBy() string    { return v.publishedBy }
func (v Version[T]) PublishedAt() time.Time { return v.publishedAt }
func (v Version[T]) Bytes() []byte          { return v.content.Bytes() }
func (v Version[T]) Content() Content[T] {
	var c Content[T]
	if err := v.content.Decode(&c); err != nil {
		panic(err)
	}
	return c
}
func (v Version[T]) MarshalJSON() ([]byte, error) {
	return FreezeBytes(struct {
		ProjectID      string     `json:"project_id"`
		Ref            Ref        `json:"ref"`
		Algorithm      string     `json:"algorithm_ref"`
		ContentDigest  string     `json:"content_digest"`
		SemanticDigest string     `json:"semantic_digest"`
		Content        Content[T] `json:"content"`
		PublishedBy    string     `json:"published_by"`
		PublishedAt    time.Time  `json:"published_at"`
	}{v.projectID, v.ref, v.Algorithm(), v.ContentDigest(), v.semanticDigest, v.Content(), v.publishedBy, v.publishedAt})
}
func FreezeBytes(v any) ([]byte, error) {
	j, err := Freeze(v)
	if err != nil {
		return nil, err
	}
	return j.Bytes(), nil
}

type Availability string

const (
	Unsupported  Availability = "UNSUPPORTED"
	ContractOnly Availability = "CONTRACT_ONLY"
)

type EvidenceRequirement struct {
	Kind         string `json:"kind"`
	Schema       string `json:"schema_version"`
	BodyRequired bool   `json:"body_required"`
}
type Applicability struct {
	CaseTypes        []string              `json:"case_types"`
	RequiredEvidence []EvidenceRequirement `json:"required_evidence"`
	RuleRef          string                `json:"rule_ref"`
}
type Evidence struct {
	Kind, Schema  string
	BodyAvailable bool
}
type ApplicabilityState string

const (
	Applicable          ApplicabilityState = "APPLICABLE"
	NotApplicable       ApplicabilityState = "NOT_APPLICABLE"
	MissingEvidence     ApplicabilityState = "MISSING_EVIDENCE"
	UnsupportedEvidence ApplicabilityState = "UNSUPPORTED"
)

func (a Applicability) Validate() error {
	if !Text(a.RuleRef) {
		return ErrInvalid
	}
	seen := map[string]bool{}
	for _, r := range a.RequiredEvidence {
		if !Text(r.Kind) || !Text(r.Schema) || seen[r.Kind+"/"+r.Schema] {
			return ErrInvalid
		}
		seen[r.Kind+"/"+r.Schema] = true
	}
	return nil
}

// CheckApplicability 只判断证据适用性，不执行 evaluator 或把缺证据转成分数。
func CheckApplicability(a Applicability, availability Availability, caseType string, evidence []Evidence) ApplicabilityState {
	if len(a.CaseTypes) > 0 {
		found := false
		for _, t := range a.CaseTypes {
			found = found || t == caseType
		}
		if !found {
			return NotApplicable
		}
	}
	if availability == Unsupported {
		return UnsupportedEvidence
	}
	for _, r := range a.RequiredEvidence {
		found := false
		for _, e := range evidence {
			found = found || (e.Kind == r.Kind && e.Schema == r.Schema && (!r.BodyRequired || e.BodyAvailable))
		}
		if !found {
			return MissingEvidence
		}
	}
	return Applicable
}

// PolicyRef / ExperimentRef 仅冻结身份边界，G1 不提供持久化和决策引擎。
type PolicyRef struct {
	PolicyID  string `json:"policy_id"`
	Version   string `json:"version"`
	Digest    string `json:"digest"`
	Algorithm string `json:"algorithm_ref"`
}

func (p PolicyRef) Validate() error {
	if !Text(p.PolicyID) || !Text(p.Version) || !Text(p.Algorithm) || len(p.Digest) != 64 {
		return ErrInvalid
	}
	return nil
}

type ExperimentRef struct {
	ProjectID    string `json:"project_id"`
	ExperimentID string `json:"experiment_id"`
}
