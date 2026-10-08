package cigovernance

import "testing"

func TestRenewalV3PolicyRole(t *testing.T) {
	p := Policy{Version: Contract, Role: "HOLDOUT", Profile: ProfileV3, GTMapping: GTMappingV3, Split: SplitVersionV3, Frozen: true}
	if p.Validate() != nil || CheckIntent(p.Role, "RELEASE_EVALUATION") != nil {
		t.Fatal("v3 holdout contract rejected")
	}
	for _, role := range []string{"DEVELOPMENT", "CALIBRATION"} {
		p.Role = role
		if p.Validate() == nil {
			t.Fatal("v3 profile accepted outside HOLDOUT", role)
		}
	}
	p.Role, p.GTMapping = "HOLDOUT", GTMappingV2
	if p.Validate() == nil {
		t.Fatal("mixed generation and mapping versions accepted")
	}
}
