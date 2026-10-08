package cigovernance

import (
	"testing"

	"agentevalops/go-backend/internal/asset"
)

func TestWP12BSourceExposedHoldoutRetirement(t *testing.T) {
	if !WP12BRetired("another-project", asset.Ref{EntityID: "0f94fa11-8194-5afd-8c66-30be45cfec79", Version: "repackaged-version"}) {
		t.Fatal("source-exposed dataset regained independent release through version change")
	}
	if !WP12BRetired("de00633e-36db-5a50-a85c-b65ccb0724f0", asset.Ref{}) {
		t.Fatal("source-exposed project accepted an explicit-case release")
	}
	if WP12BRetired("independent-project", asset.Ref{EntityID: asset.NewID(), Version: "golden-v3"}) {
		t.Fatal("version name alone retired an unrelated holdout")
	}
}
