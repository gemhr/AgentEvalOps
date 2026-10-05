// Package buildinfo 提供可核对的发布身份，不读取运行时 secret。
package buildinfo

import (
	"agentevalops/go-backend/internal/analytics"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/decision"
	"agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/observation"
	"encoding/json"
	"os"
	"runtime"
	"runtime/debug"
)

var Revision = "unknown"
var Dirty = "unknown"
var BuiltAt = "unknown"
var Schema = "c12a00800001"

func Version(binary string) map[string]any {
	revision, dirty := Revision, Dirty
	if b, ok := debug.ReadBuildInfo(); ok {
		for _, s := range b.Settings {
			if s.Key == "vcs.revision" && revision == "unknown" {
				revision = s.Value
			}
			if s.Key == "vcs.modified" && dirty == "unknown" {
				dirty = s.Value
			}
		}
	}
	return map[string]any{"binary": binary, "git_sha": revision, "dirty": dirty, "built_at": BuiltAt, "go_version": runtime.Version(), "goos": runtime.GOOS, "goarch": runtime.GOARCH, "schema_head": Schema, "contracts": map[string]string{"kernel": evaluation.DurableContract, "run_input": evaluation.SnapshotContract, "gate": decision.Contract, "online": observation.Contract, "trace_identity": observation.TraceIdentity, "trace_fingerprint": observation.TraceFingerprint, "catalog": asset.CatalogAlgorithm, "analytics": analytics.Version}}
}
func Requested(binary string) bool {
	if len(os.Args) == 2 && (os.Args[1] == "--version" || os.Args[1] == "-version") {
		_ = json.NewEncoder(os.Stdout).Encode(Version(binary))
		return true
	}
	return false
}
