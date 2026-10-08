package cigovernance

import "agentevalops/go-backend/internal/asset"

const HoldoutConsumption = "stage13.holdout-consumption.v1"

type ReleaseIdentity struct {
	ID      string `json:"subject_id"`
	Version string `json:"subject_version"`
	Digest  string `json:"subject_manifest_digest"`
}

// ReleaseSubject 只校验消费身份的自摘要；完整执行回执继续由 Stage13Target 核验。
func ReleaseSubject(config asset.JSON) (ReleaseIdentity, error) {
	var cfg map[string]asset.JSON
	var out ReleaseIdentity
	if config.Decode(&cfg) != nil {
		return out, asset.ErrInvalid
	}
	manifest := cfg["expected_subject_manifest"]
	var fields map[string]asset.JSON
	if manifest.Decode(&fields) != nil {
		return out, asset.ErrInvalid
	}
	if fields["subject_id"].Decode(&out.ID) != nil || fields["subject_version"].Decode(&out.Version) != nil || fields["subject_manifest_digest"].Decode(&out.Digest) != nil || !asset.Text(out.ID) || !asset.Text(out.Version) {
		return out, asset.ErrInvalid
	}
	delete(fields, "subject_manifest_digest")
	j, e := asset.Freeze(fields)
	if e != nil || out.Digest != j.Digest() {
		return out, asset.ErrInvalid
	}
	return out, nil
}
