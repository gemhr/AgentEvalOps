package httpapi

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	gov "agentevalops/go-backend/internal/cigovernance"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/provider"
	"encoding/json"
	"strings"
	"testing"
)

func TestDevelopmentEvidenceNeverExportsFrozenGroundTruth(t *testing.T) {
	state := ev.RunState{}
	state.Run.ID = asset.NewID()
	state.Run.Snapshot.Target.ID = provider.Stage13TargetID
	for _, role := range []string{"HOLDOUT", "CALIBRATION", "DEVELOPMENT"} {
		metadata := gov.Metadata(gov.Policy{Version: gov.Contract, Role: role, GTMapping: gov.GTMapping, Split: gov.SplitVersion, Profile: gov.Profile, Frozen: true})
		raw, _ := asset.Freeze(asset.Content[catalog.DatasetContent]{Body: catalog.DatasetContent{Metadata: metadata}})
		state.Run.Snapshot.Input.Dataset = &ev.AssetIdentity{CanonicalContent: raw}
		if role != "DEVELOPMENT" {
			if _, e := projectTriageDevelopment(state); e != asset.ErrForbidden {
				t.Fatal(role, e)
			}
		}
	}
	hidden, _ := asset.Freeze(map[string]any{"ExpectedFailureCategory": "hidden-answer", "RelevantRootCauses": []string{"hidden-root"}})
	state.Run.Snapshot.Input.Manifest = []ev.CaseInput{{Case: catalog.CaseContent{GroundTruth: hidden}}}
	value, e := projectTriageDevelopment(state)
	if e != nil {
		t.Fatal(e)
	}
	raw, _ := json.Marshal(value)
	if strings.Contains(string(raw), "hidden-answer") || strings.Contains(string(raw), "hidden-root") || strings.Contains(string(raw), "ground_truth") {
		t.Fatal("GT escaped projection", string(raw))
	}
}
