package postgres

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/decision"
	"agentevalops/go-backend/internal/delivery"
	ev "agentevalops/go-backend/internal/evaluation"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"sort"
)

// Stage13DeliveryBinding 只读 Gate/Snapshot/Result；不重算、不更新 Evaluation Truth。
func (k Decisions) Stage13DeliveryBinding(ctx context.Context, s asset.Scope, gateID, anchor string) (delivery.GateBinding, delivery.OutcomeBinding, []string, error) {
	var g delivery.GateBinding
	var o delivery.OutcomeBinding
	if s.Validate(false) != nil || !asset.ValidID(gateID) || !asset.ValidID(anchor) {
		return g, o, nil, asset.ErrInvalid
	}
	var raw, snapshot []byte
	var receiptDigest, snapshotDigest, comparisonDigest, storedDecision string
	err := k.Pool.QueryRow(ctx, `SELECT r.receipt_bytes,r.receipt_digest,r.decision,c.snapshot_bytes,c.snapshot_digest,c.intent_digest FROM evaluation_gate_receipts r JOIN evaluation_comparisons c ON c.project_id=r.project_id AND c.id=r.gate_id JOIN projects p ON p.id=r.project_id WHERE r.project_id=$1 AND p.org_id=$2 AND r.gate_id=$3`, s.ProjectID, s.OrganizationID, gateID).Scan(&raw, &receiptDigest, &storedDecision, &snapshot, &snapshotDigest, &comparisonDigest)
	if err != nil {
		return g, o, nil, dbError(err)
	}
	rj, e := asset.ParseJSON(raw)
	if e != nil || rj.Digest() != receiptDigest {
		return g, o, nil, asset.ErrInvalid
	}
	sj, e := asset.ParseJSON(snapshot)
	if e != nil || sj.Digest() != snapshotDigest {
		return g, o, nil, asset.ErrInvalid
	}
	var r decision.Receipt
	var snap decision.Snapshot
	if json.Unmarshal(raw, &r) != nil || json.Unmarshal(snapshot, &snap) != nil || r.GateID != gateID || r.ProjectID != s.ProjectID || snap.Command.ID != gateID || snap.ProjectID != s.ProjectID || r.SnapshotDigest != snapshotDigest || string(r.Decision) != storedDecision || r.PolicyDigest != snap.PolicyDigest || r.PolicyRef != snap.Command.Policy {
		return g, o, nil, asset.ErrInvalid
	}
	unitFound := false
	var u decision.Unit
	for _, v := range snap.Candidate.Units {
		if v.AttemptID == anchor {
			if unitFound {
				return g, o, nil, asset.ErrInvalid
			}
			u = v
			unitFound = true
		}
	}
	if !unitFound {
		return g, o, nil, asset.ErrNotFound
	}
	states := map[string]ev.RunState{}
	read := func(id string) (ev.RunState, error) {
		if v, ok := states[id]; ok {
			return v, nil
		}
		v, e := (Evaluation{Pool: k.Pool}).ReadRunState(ctx, s, id)
		states[id] = v
		return v, e
	}
	subject := func(src decision.Source) (string, string, error) {
		var manifest, dataset string
		for _, v := range src.Units {
			st, e := read(v.RunID)
			if e != nil {
				return "", "", e
			}
			var cfg map[string]asset.JSON
			_ = st.Run.Snapshot.Target.Config.Decode(&cfg)
			var m map[string]asset.JSON
			_ = cfg["expected_subject_manifest"].Decode(&m)
			var d string
			_ = m["subject_manifest_digest"].Decode(&d)
			ds := st.Run.Snapshot.Input.Dataset.Ref.Version
			if len(d) != 64 || ds == "" || manifest != "" && (manifest != d || dataset != ds) {
				return "", "", asset.ErrInvalid
			}
			manifest, dataset = d, ds
		}
		return manifest, dataset, nil
	}
	base, ds, e := subject(snap.Baseline)
	if e != nil {
		return g, o, nil, e
	}
	cand, cds, e := subject(snap.Candidate)
	if e != nil || cds != ds {
		return g, o, nil, asset.ErrInvalid
	}
	g = delivery.GateBinding{GateID: gateID, Decision: r.Decision, ReceiptDigest: receiptDigest, DatasetVersion: ds, BaselineSubject: base, CandidateSubject: cand, ComparisonDigest: comparisonDigest, SnapshotDigest: snapshotDigest, PolicyVersion: r.PolicyRef.Version, PolicyDigest: r.PolicyDigest}
	st, e := read(u.RunID)
	if e != nil {
		return g, o, nil, e
	}
	var a ev.Attempt
	for _, v := range st.Attempts {
		if v.ID == anchor {
			a = v
		}
	}
	if a.ID == "" || a.Status != "TERMINAL" || a.Outcome != ev.Success || a.Metadata.Observation.Artifact == nil {
		return g, o, nil, asset.ErrInvalid
	}
	var answer string
	if a.Metadata.Observation.Artifact.Body.Decode(&answer) != nil {
		return g, o, nil, asset.ErrInvalid
	}
	for _, v := range a.Metadata.Observation.Evidence {
		if v.Schema != "stage13-subject-receipt.v1" {
			continue
		}
		var wire map[string]asset.JSON
		_ = v.Body.Decode(&wire)
		var selected string
		_ = wire["selected_final_run_id"].Decode(&selected)
		receipts := []asset.JSON{wire["actual_subject_receipt"]}
		var children []asset.JSON
		_ = wire["child_subject_receipts"].Decode(&children)
		receipts = append(receipts, children...)
		for _, receipt := range receipts {
			var m map[string]asset.JSON
			_ = receipt.Decode(&m)
			text := func(key string) string { var x string; _ = m[key].Decode(&x); return x }
			if text("run_id") != selected {
				continue
			}
			rd := text("receipt_digest")
			delete(m, "receipt_digest")
			j, _ := asset.Freeze(m)
			var manifest map[string]asset.JSON
			_ = m["actual_subject_manifest"].Decode(&manifest)
			var subject string
			_ = manifest["subject_manifest_digest"].Decode(&subject)
			ad := fmt.Sprintf("%x", sha256.Sum256([]byte(answer)))
			if rd != j.Digest() || subject != cand || text("final_answer_digest") != ad || text("evaluation_attempt_id") != anchor {
				return g, o, nil, asset.ErrInvalid
			}
			o = delivery.OutcomeBinding{CaseID: u.CaseID, CaseVersion: u.CaseVersion, EvaluationRunID: u.RunID, AnchorRunID: anchor, SelectedRunID: selected, SubjectDigest: cand, AnswerDigest: ad, ReceiptDigest: rd}
		}
	}
	if o.SelectedRunID == "" {
		return g, o, nil, asset.ErrInvalid
	}
	results := []string{}
	for _, m := range u.Metrics {
		if m.ResultID != "" {
			results = append(results, m.ResultID)
		}
	}
	sort.Strings(results)
	return g, o, results, nil
}

func (k Decisions) AuthorizeStage13Delivery(ctx context.Context, s asset.Scope, q delivery.Request) (delivery.Authorization, error) {
	g, o, results, e := k.Stage13DeliveryBinding(ctx, s, q.Gate.GateID, q.Outcome.AnchorRunID)
	if e != nil {
		return delivery.Authorization{}, e
	}
	r, e := k.GetGateReceipt(ctx, s, q.Gate.GateID)
	if e != nil {
		return delivery.Authorization{}, e
	}
	return delivery.Project(s.ProjectID, r.CreatedAt, q, g, o, results), nil
}
