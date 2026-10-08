package catalog

import (
	"context"
	"encoding/json"
	"slices"

	"agentevalops/go-backend/internal/asset"
	gov "agentevalops/go-backend/internal/cigovernance"
	"agentevalops/go-backend/internal/citriage"
)

// HoldoutVerification 是 operator 的完整输出白名单；不包含 Case 引用或正文。
type HoldoutVerification struct {
	Dataset         asset.Ref `json:"dataset_version"`
	CaseCount       int       `json:"case_count"`
	ManifestDigest  string    `json:"manifest_digest"`
	Role            string    `json:"role"`
	CriticalCount   int       `json:"critical_count"`
	NormalCount     int       `json:"normal_count"`
	IntegrityStatus string    `json:"integrity_status"`
}

// VerifyHoldoutMetadata 在服务端读取并校验冻结资产，只返回元数据。
func (s DatasetService) VerifyHoldoutMetadata(ctx context.Context, scope asset.Scope, ref asset.Ref) (HoldoutVerification, error) {
	ds, err := s.GetDatasetVersion(ctx, scope, ref)
	if err != nil {
		return HoldoutVerification{}, err
	}
	p, err := gov.ReadPolicy(ds.Content().Body.Metadata)
	if err != nil || p == nil || p.Role != "HOLDOUT" {
		return HoldoutVerification{}, asset.ErrForbidden
	}
	out := HoldoutVerification{Dataset: ref, CaseCount: len(ds.Content().Body.Cases), ManifestDigest: ds.ContentDigest(), Role: p.Role, IntegrityStatus: "PASS"}
	families := []string{}
	for _, r := range ds.Content().Body.Cases {
		c, err := s.Cases.GetCaseVersion(ctx, scope, r)
		if err != nil {
			return HoldoutVerification{}, err
		}
		body := c.Content().Body
		cp, err := gov.ReadPolicy(body.Metadata)
		if err != nil || cp == nil || cp.Role != p.Role || cp.Profile != p.Profile || cp.GTMapping != p.GTMapping || cp.Split != p.Split || !gov.HardGolden(cp.State) || cp.ReviewID == "" || slices.Contains(families, cp.Family) {
			return HoldoutVerification{}, asset.ErrInvalid
		}
		gt, err := citriage.ReadGroundTruth(body.GroundTruth)
		if err != nil {
			return HoldoutVerification{}, err
		}
		var in struct {
			Evidence []struct {
				Type         string     `json:"type"`
				Availability string     `json:"availability"`
				Digest       string     `json:"digest"`
				Content      asset.JSON `json:"content"`
			} `json:"visible_evidence"`
		}
		if citriage.ValidateInput(body.Input) != nil || json.Unmarshal(body.Input.Bytes(), &in) != nil || string(body.Criticality) != gt.Criticality {
			return HoldoutVerification{}, asset.ErrInvalid
		}
		for _, evidence := range in.Evidence {
			if evidence.Availability == "AVAILABLE" && evidence.Content.Digest() != evidence.Digest {
				return HoldoutVerification{}, asset.ErrInvalid
			}
		}
		for _, kind := range gt.EvidencePolicy.Types {
			found := false
			for _, evidence := range in.Evidence {
				found = found || evidence.Type == kind && evidence.Availability == "AVAILABLE"
			}
			if !found {
				return HoldoutVerification{}, asset.ErrInvalid
			}
		}
		families = append(families, cp.Family)
		if gt.Criticality == "CRITICAL" {
			out.CriticalCount++
		} else {
			out.NormalCount++
		}
	}
	slices.Sort(families)
	expected := slices.Clone(p.Families)
	slices.Sort(expected)
	if len(families) == 0 || !slices.Equal(families, expected) {
		return HoldoutVerification{}, asset.ErrInvalid
	}
	return out, nil
}
