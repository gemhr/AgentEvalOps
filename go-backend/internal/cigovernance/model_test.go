package cigovernance_test

import (
	"slices"
	"strings"
	"testing"

	"agentevalops/go-backend/internal/asset"
	gov "agentevalops/go-backend/internal/cigovernance"
	"agentevalops/go-backend/internal/citriage"
)

func TestIndependentGenerationAndStableManifest(t *testing.T) {
	a := gov.Generate("wp07-test", "seed-1")
	b := gov.Generate("wp07-test", "seed-1")
	if a.Digest != b.Digest || len(a.Families) != 18 || gov.AuditIsolation(a.Families) != nil {
		t.Fatal("generation not deterministic or independent")
	}
	counts := map[string]int{}
	mechanisms := map[string]bool{}
	for _, f := range a.Families {
		g, err := citriage.ReadGroundTruth(f.GT)
		if err != nil || citriage.ValidateInput(f.Input) != nil {
			t.Fatal("invalid controlled asset", err)
		}
		counts[f.Role]++
		mechanisms[f.RootMechanism] = true
		if !g.EvidencePolicy.Decidable || f.Control == "" {
			t.Fatal("missing intervention or sufficiency")
		}
		for _, secret := range []string{"hidden-eval-root-", f.ID, "ExpectedFailureCategory", "ExpectedTicketDecision", "AcceptableActions", "GroundTruth", "Criticality"} {
			if strings.Contains(f.Input.String(), secret) {
				t.Fatal("hidden truth in runtime input", secret)
			}
		}
	}
	if len(mechanisms) != 18 {
		t.Fatal("mechanism padding")
	}
	for _, role := range gov.Roles {
		if counts[role] != 6 {
			t.Fatal("role coverage", counts)
		}
	}
	// namespace 变化仅产生实例，不能通过改 ID 宣称新 family。
	c := gov.Generate("wp07-new-namespace", "seed-1")
	for i := range a.Families {
		if a.Families[i].RootMechanism != c.Families[i].RootMechanism || a.Families[i].GTDigest != c.Families[i].GTDigest {
			t.Fatal("namespace altered truth")
		}
	}
}

func TestLeakageRejectedAcrossAllFrozenDimensions(t *testing.T) {
	base := gov.Generate("wp07-test", "seed-1").Families
	for _, dimension := range []string{"mechanism", "template", "signature", "mapping"} {
		t.Run(dimension, func(t *testing.T) {
			families := slices.Clone(base)
			a, b := 0, 1
			families[a].Role = "DEVELOPMENT"
			families[b].Role = "HOLDOUT"
			switch dimension {
			case "mechanism":
				families[b].RootMechanism = families[a].RootMechanism
			case "template":
				families[b].Template = families[a].Template
			case "signature":
				families[b].SignatureComponent = families[a].SignatureComponent
			case "mapping":
				families[b].GT = families[a].GT
				families[b].GTDigest = families[a].GTDigest
			}
			if gov.AuditIsolation(families) == nil {
				t.Fatal("leakage accepted")
			}
		})
	}
}

func TestReviewAuthorityAndEvaluationIntent(t *testing.T) {
	if !gov.HardGolden("CONFIRMED") || !gov.HardGolden("CORRECTED") {
		t.Fatal("golden authority")
	}
	for _, state := range []string{"AMBIGUOUS", "INSUFFICIENT_EVIDENCE", "REJECTED", "PENDING_REVIEW"} {
		if gov.HardGolden(state) {
			t.Fatal("unreviewed/ambiguous hard denominator")
		}
	}
	if gov.CheckIntent("HOLDOUT", "DEV_EVALUATION") == nil || gov.CheckIntent("CALIBRATION", "RELEASE_EVALUATION") == nil || gov.CheckIntent("DEVELOPMENT", "RELEASE_EVALUATION") == nil {
		t.Fatal("intent boundary")
	}
	if gov.CheckIntent("HOLDOUT", "RELEASE_EVALUATION") != nil {
		t.Fatal("release intent rejected")
	}
	if _, err := gov.ReadPolicy(gov.Freeze(map[string]any{"stage13_governance": map[string]string{"role": "HOLDOUT"}})); err == nil {
		t.Fatal("incomplete policy accepted")
	}
	if p, e := gov.ReadPolicy(asset.JSON{}); e != nil || p != nil {
		t.Fatal("legacy compatibility")
	}
}
