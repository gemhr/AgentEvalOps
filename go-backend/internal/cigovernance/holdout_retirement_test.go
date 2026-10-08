package cigovernance

import (
	"testing"

	"agentevalops/go-backend/internal/asset"
)

func TestWP11RetiredIdentities(t *testing.T) {
	if !WP11Retired(asset.Ref{EntityID: "e7a70b04-0712-58bc-a0c8-655eac0aa463", Version: "new-version"}, nil) {
		t.Fatal("retired dataset recovered through version change")
	}
	for _, id := range []string{"93703f50-d29f-5fa5-9c42-f5652027383d", "fd5d2343-1d3c-5c9f-8067-627e4ee1cbeb", "4a46786a-2e67-597e-bc72-efc90526d51e", "df831bc1-6b59-5004-90fe-dc4e795908a1", "8624c585-6f3c-5749-a2b5-7a475af42135", "03bc75f8-bcdd-5c9d-89a3-6a1b3ed12caa"} {
		if !WP11Retired(asset.Ref{EntityID: asset.NewID(), Version: "repackaged"}, []asset.Ref{{EntityID: id, Version: "new-version"}}) {
			t.Fatal("retired case repackaging accepted")
		}
	}
	if WP11Retired(asset.Ref{EntityID: "0f94fa11-8194-5afd-8c66-30be45cfec79", Version: "golden-v3"}, nil) {
		t.Fatal("new independent holdout retired")
	}
}
