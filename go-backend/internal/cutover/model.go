// Package cutover 定义离线切换证据，不授予生产写入权限。
package cutover

import (
	"agentevalops/go-backend/internal/asset"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"time"
)

const Schema = "c12a00800001"

type State struct {
	Scope     string `json:"scope"`
	Mode      string `json:"mode"`
	Writer    string `json:"active_writer"`
	Epoch     int64  `json:"writer_epoch"`
	CutoverID string `json:"cutover_id"`
	Schema    string `json:"schema_head"`
}

type Consumer struct {
	Name       string `json:"consumer"`
	Owner      string `json:"owner"`
	Current    string `json:"current_endpoint"`
	Target     string `json:"target_endpoint"`
	Status     string `json:"cutover_status"`
	Rollback   string `json:"rollback_requirement"`
	VerifiedAt string `json:"verified_at"`
	Kind       string `json:"verification_kind"`
}

type Evidence struct {
	CutoverID      string            `json:"cutover_id"`
	Epoch          int64             `json:"expected_epoch"`
	Operator       string            `json:"operator"`
	LegacyRoles    []string          `json:"legacy_roles"`
	Consumers      []Consumer        `json:"consumers"`
	Checks         map[string]string `json:"checks"`
	Drain          map[string]*int64 `json:"drain"`
	BackupRef      string            `json:"backup_evidence_ref"`
	BackupDigest   string            `json:"backup_sha256"`
	ReleaseDigest  string            `json:"release_manifest_sha256"`
	ConsumerDigest string            `json:"consumer_checklist_sha256"`
}

var DrainMetrics = []string{"active_python_requests", "active_celery_canonical_tasks", "queued_canonical_tasks", "scheduled_canonical_next_occurrences", "inflight_transactions", "legacy_mutations_since_drain"}
var MandatoryChecks = []string{"admission_stopped", "beat_stopped", "legacy_inventory", "migration_state", "canonical_integrity", "immutable_digests", "project_isolation", "go_roles", "go_api_artifact_ready", "go_worker_artifact_ready", "trace_ingress", "backup_restore_validated", "old_sessions_closed"}

type Check struct {
	Name   string `json:"name"`
	Status string `json:"status"`
}
type Report struct {
	State      State   `json:"state"`
	Status     string  `json:"status"`
	Checks     []Check `json:"checks"`
	Production string  `json:"production"`
}

func Digest(raw []byte) string           { d := sha256.Sum256(raw); return hex.EncodeToString(d[:]) }
func ConsumerDigest(c []Consumer) string { b, _ := json.Marshal(c); return Digest(b) }

func Next(s State, command string) (State, error) {
	n := s
	switch command {
	case "enter-drain":
		if s.Mode != "PYTHON_ACTIVE" && s.Mode != "GO_ACTIVE" {
			return s, errors.New("INVALID_WRITER_TRANSITION")
		}
		if s.Mode == "PYTHON_ACTIVE" {
			n.Mode = "PYTHON_DRAINING"
		} else {
			n.Mode = "GO_DRAINING"
		}
	case "enter-barrier":
		if s.Mode != "PYTHON_DRAINING" && s.Mode != "GO_DRAINING" {
			return s, errors.New("INVALID_WRITER_TRANSITION")
		}
		n.Mode, n.Writer, n.Epoch = "BARRIER", "NONE", s.Epoch+1
	case "activate-go":
		if s.Mode != "BARRIER" {
			return s, errors.New("INVALID_WRITER_TRANSITION")
		}
		n.Mode, n.Writer, n.Epoch = "GO_ACTIVE", "GO", s.Epoch+1
	case "abort-before-go-write":
		return s, errors.New("ROLLBACK_REQUIRES_RESTORE_OR_FORWARD_FIX")
	default:
		return s, errors.New("INVALID_COMMAND")
	}
	return n, nil
}

// Evaluate 不把静态声明或缺失统计提升为验证通过。
func Evaluate(s State, e Evidence, controlled bool) Report {
	r := Report{State: s, Status: "PASS", Production: "PRODUCTION_CUTOVER_BLOCKED", Checks: []Check{}}
	add := func(name string, ok bool, missing bool) {
		status := "PASS"
		if !ok {
			status = "BLOCKED"
			if missing {
				status = "NOT_VERIFIED"
			}
			r.Status = "BLOCKED"
		}
		r.Checks = append(r.Checks, Check{name, status})
	}
	add("schema", s.Schema == Schema, false)
	validState := (s.Mode == "PYTHON_ACTIVE" || s.Mode == "PYTHON_DRAINING") && s.Writer == "PYTHON" || (s.Mode == "GO_ACTIVE" || s.Mode == "GO_DRAINING") && s.Writer == "GO" || s.Mode == "BARRIER" && s.Writer == "NONE"
	add("writer_state", validState, false)
	add("epoch", s.Epoch > 0 && s.Epoch == e.Epoch, false)
	add("operator", e.Operator != "", e.Operator == "")
	add("cutover_id", asset.ValidID(e.CutoverID), e.CutoverID == "")
	add("legacy_roles_inventory", len(e.LegacyRoles) > 0, len(e.LegacyRoles) == 0)
	add("consumer_manifest_binding", e.ConsumerDigest == ConsumerDigest(e.Consumers), e.ConsumerDigest == "")
	add("known_consumers", len(e.Consumers) > 0, len(e.Consumers) == 0)
	seen := map[string]bool{}
	for _, c := range e.Consumers {
		_, err := time.Parse(time.RFC3339, c.VerifiedAt)
		valid := c.Name != "" && c.Name != "UNKNOWN_EXTERNAL_CONSUMER" && !seen[c.Name] && c.Owner != "" && c.Target != "" && c.Rollback != "" && c.Status == "VERIFIED" && err == nil && (c.Kind == "REAL_WORLD" || controlled && c.Kind == "CONTROLLED")
		add("consumer:"+c.Name, valid, c.Status == "DECLARED" || c.Name == "UNKNOWN_EXTERNAL_CONSUMER")
		seen[c.Name] = true
	}
	for _, key := range MandatoryChecks {
		add(key, e.Checks[key] == "VERIFIED", e.Checks[key] == "" || e.Checks[key] == "DECLARED")
	}
	for _, key := range DrainMetrics {
		v := e.Drain[key]
		add(key, v != nil && *v == 0, v == nil)
	}
	decoded, err := hex.DecodeString(e.BackupDigest)
	add("backup", e.BackupRef != "" && err == nil && len(decoded) == 32, e.BackupRef == "")
	return r
}
