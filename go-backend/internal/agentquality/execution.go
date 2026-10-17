package agentquality

import (
	"agentevalops/go-backend/internal/asset"
	"fmt"
)

// AgentExecution 是 WP5 既有 v1 投影的严格读取；不修改它的写入合同。
type AgentExecution struct {
	Schema   string   `json:"schema_version"`
	Policy   string   `json:"execution_policy"`
	Coverage string   `json:"coverage"`
	Gaps     []string `json:"gaps"`
	Run      string   `json:"run_ref"`
	Outcome  string   `json:"outcome"`
	Frames   []struct {
		Ref        string  `json:"ref"`
		Role       string  `json:"role"`
		Agent      string  `json:"agent_ref"`
		Parent     *string `json:"parent_ref"`
		Seed       string  `json:"seed_digest"`
		Config     string  `json:"config_digest"`
		Generation int64   `json:"generation"`
		Fence      int64   `json:"fence"`
		Delivered  bool    `json:"final_delivered"`
	} `json:"frames"`
	Decisions []struct {
		Ref         string  `json:"ref"`
		Frame       string  `json:"frame_ref"`
		Iteration   int     `json:"iteration"`
		Kind        string  `json:"kind"`
		Digest      string  `json:"decision_digest"`
		Model       string  `json:"model_ref"`
		Snapshot    string  `json:"snapshot_ref"`
		Observation *string `json:"observation_digest"`
	} `json:"decisions"`
	Actions []struct {
		Ref        string  `json:"ref"`
		Frame      string  `json:"frame_ref"`
		Iteration  int     `json:"iteration"`
		Ordinal    int     `json:"ordinal"`
		Call       string  `json:"call_ref"`
		Invocation *string `json:"invocation_ref"`
		Recipe     string  `json:"recipe_digest"`
		State      string  `json:"state"`
		Source     *string `json:"source_digest"`
		Projection *string `json:"projection_digest"`
	} `json:"actions"`
	Children []struct {
		Ref        string  `json:"ref"`
		Parent     string  `json:"parent_ref"`
		Child      string  `json:"child_ref"`
		Permission string  `json:"permission_digest"`
		Frozen     string  `json:"frozen_digest"`
		Result     *string `json:"result_digest"`
		Accepted   bool    `json:"parent_accepted"`
		Envelope   string  `json:"envelope_ref"`
		Conserved  bool    `json:"conserved"`
	} `json:"delegations"`
	Snapshots []struct {
		Ref       string  `json:"ref"`
		Frame     string  `json:"frame_ref"`
		Version   int     `json:"version"`
		Digest    string  `json:"digest"`
		Source    *string `json:"source_digest"`
		Mandatory *string `json:"mandatory_digest"`
		Validator string  `json:"validator"`
	} `json:"snapshots"`
	Memory *struct {
		Mode       string `json:"mode"`
		Status     string `json:"status"`
		Manifest   string `json:"manifest_digest"`
		Admissions string `json:"admissions_digest"`
	} `json:"memory_binding"`
	Budget struct {
		Consumed map[string]int64 `json:"consumed"`
		Reserved map[string]int64 `json:"reserved"`
	} `json:"budget"`
	Final *string `json:"final_receipt_digest"`
}

func executionIntegrity(j asset.JSON, b QualityBundle, o QualityObservations) (string, string) {
	var e AgentExecution
	if StrictQualityShape(j.Bytes(), &e) != nil || e.Schema != "agent-execution-evidence.v1" || e.Policy != b.Policy || e.Outcome != b.Terminal.Status {
		return "ERROR", "EXECUTION_COMPONENT_INVALID"
	}
	if e.Coverage != "COMPLETE" || len(e.Gaps) > 0 {
		return "INCONCLUSIVE", "EXECUTION_COMPONENT_INCOMPLETE"
	}
	roots, children, delivered := 0, 0, 0
	frames := map[string]string{}
	root := ""
	for _, f := range e.Frames {
		if frames[f.Ref] != "" || !loopDigest.MatchString(f.Ref) || !loopDigest.MatchString(f.Seed) || !loopDigest.MatchString(f.Config) || f.Generation < 0 || f.Fence < 0 {
			return "FAIL", "FRAME_IDENTITY_INVALID"
		}
		frames[f.Ref] = f.Role
		if f.Role == "ROOT" {
			roots++
			root = f.Ref
			if f.Parent != nil {
				return "FAIL", "ROOT_PARENT_INVALID"
			}
		} else if f.Role == "CHILD" {
			children++
			if f.Parent == nil || f.Delivered {
				return "FAIL", "CHILD_AUTHORITY_INVALID"
			}
		} else {
			return "FAIL", "FRAME_ROLE_INVALID"
		}
		if f.Delivered {
			delivered++
		}
	}
	if roots != 1 || children > 2 || delivered != 1 || e.Final == nil {
		return "FAIL", "ROOT_FINAL_AUTHORITY_INVALID"
	}
	for _, f := range e.Frames {
		if f.Role == "CHILD" && *f.Parent != root {
			return "FAIL", "FRAME_RECURSION_INVALID"
		}
	}
	models := map[string]QualityModel{}
	for _, m := range o.Models {
		models[m.Source] = m
	}
	snapshots := map[string]string{}
	for _, s := range e.Snapshots {
		if s.Validator != "context-validator.v2" || snapshots[s.Ref] != "" || frames[s.Frame] == "" {
			return "FAIL", "SNAPSHOT_IDENTITY_INVALID"
		}
		snapshots[s.Ref] = s.Frame
	}
	decisions := map[string]string{}
	finals := map[string]int{}
	kinds := map[string]string{}
	for _, d := range e.Decisions {
		key := fmt.Sprintf("%s:%d", d.Frame, d.Iteration)
		m, ok := models["model:"+d.Model]
		if decisions[d.Ref] != "" || kinds[key] != "" || frames[d.Frame] == "" || !oneOf(d.Kind, "FINAL", "REFUSAL", "TOOL_BATCH", "HANDOFF") || snapshots[d.Snapshot] != d.Frame {
			return "FAIL", "DECISION_IDENTITY_INVALID"
		}
		if !ok || m.Purpose != "DECISION" || m.State != "COMPLETED" || m.Frame == nil || *m.Frame != d.Frame || m.Iteration == nil || *m.Iteration != d.Iteration || m.Snapshot == nil || *m.Snapshot != d.Snapshot {
			return "INCONCLUSIVE", "DECISION_MODEL_SOURCE_UNPROVEN"
		}
		decisions[d.Ref] = key
		kinds[key] = d.Kind
		if oneOf(d.Kind, "FINAL", "REFUSAL") {
			finals[d.Frame]++
		}
	}
	if finals[root] != 1 {
		return "FAIL", "ROOT_FINAL_DECISION_INVALID"
	}
	ordinals := map[string]map[int]bool{}
	calls := map[string]bool{}
	for _, a := range e.Actions {
		key := fmt.Sprintf("%s:%d", a.Frame, a.Iteration)
		if kinds[key] != "TOOL_BATCH" || calls[a.Call] || a.Ordinal < 0 || a.Ordinal >= 4 {
			return "FAIL", "BATCH_PROTOCOL_INVALID"
		}
		calls[a.Call] = true
		if ordinals[key] == nil {
			ordinals[key] = map[int]bool{}
		}
		if ordinals[key][a.Ordinal] {
			return "FAIL", "ACTION_ORDINAL_DUPLICATED"
		}
		ordinals[key][a.Ordinal] = true
		if a.State == "COMMITTED" {
			if a.Invocation == nil || a.Source == nil || a.Projection == nil {
				return "INCONCLUSIVE", "ACTION_RESULT_UNPROVEN"
			}
		} else if a.State != "NOT_EXECUTED" {
			return "INCONCLUSIVE", "ACTION_OUTCOME_UNKNOWN"
		}
	}
	for _, group := range ordinals {
		for i := 0; i < len(group); i++ {
			if !group[i] {
				return "FAIL", "ACTION_ORDINAL_GAP"
			}
		}
	}
	delegated := map[string]bool{}
	for _, d := range e.Children {
		if d.Parent != root || frames[d.Child] != "CHILD" || delegated[d.Child] || !d.Accepted || d.Result == nil || !d.Conserved {
			return "INCONCLUSIVE", "CHILD_RESULT_UNPROVEN"
		}
		delegated[d.Child] = true
	}
	if len(delegated) != children {
		return "FAIL", "DELEGATION_TREE_INVALID"
	}
	return "PASS", "EXECUTION_TREE_CONFIRMED"
}

func budgetIntegrity(b *QualityBudget) (string, string) {
	if b == nil {
		return "INCONCLUSIVE", "BUDGET_SOURCE_MISSING"
	}
	consumed := map[string]int64{}
	seen := map[string]bool{}
	for _, op := range b.Operations {
		if seen[op.Ref] {
			return "FAIL", "BUDGET_OPERATION_DUPLICATED"
		}
		seen[op.Ref] = true
		for _, n := range op.Reserved {
			if n < 0 {
				return "FAIL", "NEGATIVE_BUDGET"
			}
		}
		if op.State == "RESERVED" {
			return "INCONCLUSIVE", "BUDGET_OPERATION_UNSETTLED"
		}
		if op.Kind == "DELEGATION_ALLOCATION" {
			if op.State != "RELEASED" || op.Unspent == nil || op.Released == nil || op.Delegation == nil {
				return "INCONCLUSIVE", "DELEGATION_BUDGET_UNSETTLED"
			}
			for dim, allocated := range op.Reserved {
				used := int64(0)
				for _, child := range b.Operations {
					if child.Kind == "DELEGATION_ALLOCATION" || child.Delegation == nil || *child.Delegation != *op.Delegation {
						continue
					}
					if child.State != "SETTLED" || child.Actual == nil {
						return "INCONCLUSIVE", "CHILD_BUDGET_UNSETTLED"
					}
					n := (*child.Actual)[dim]
					if n < 0 || n > 1<<50 || used > 1<<50-n {
						return "FAIL", "CHILD_BUDGET_INVALID"
					}
					used += n
				}
				unspent, released := (*op.Unspent)[dim], (*op.Released)[dim]
				if unspent < 0 || released < 0 || allocated != used+unspent+released {
					return "FAIL", "DELEGATION_BUDGET_NOT_CONSERVED"
				}
			}
			continue
		}
		if op.State != "SETTLED" || op.Actual == nil || op.Source == nil {
			return "INCONCLUSIVE", "BUDGET_SETTLEMENT_UNPROVEN"
		}
		for dim, n := range *op.Actual {
			if n < 0 || n > 1<<50 || consumed[dim] > 1<<50-n {
				return "FAIL", "BUDGET_VALUE_INVALID"
			}
			consumed[dim] += n
		}
	}
	for dim, n := range b.Consumed {
		if n < 0 || consumed[dim] != n {
			return "FAIL", "BUDGET_CONSUMPTION_MISMATCH"
		}
	}
	for _, n := range b.Reserved {
		if n != 0 {
			return "INCONCLUSIVE", "BUDGET_REMAINS_RESERVED"
		}
	}
	return "PASS", "BUDGET_CONSUMPTION_CONFIRMED"
}
