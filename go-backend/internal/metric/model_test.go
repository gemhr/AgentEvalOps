package metric_test

import (
	"testing"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/metric"
)

func TestBuiltinSemanticsAndEvidenceApplicability(t *testing.T) {
	definitions := metric.Builtins()
	found := map[string]bool{}
	for _, b := range definitions {
		if err := b.Definition.Validate(); err != nil {
			t.Fatalf("%s: %v", b.Key, err)
		}
		found[b.Key] = true
		d := b.Definition
		if d.Availability != asset.ContractOnly && d.Availability != asset.Unsupported {
			t.Fatal("G1 claims executable producer")
		}
		switch b.Key {
		case "plan_quality", "plan_adherence", "tool_correctness", "tool_argument_correctness", "step_efficiency":
			if d.Availability != asset.Unsupported || asset.CheckApplicability(d.Applicability, d.Availability, "AGENT_TASK", nil) != asset.UnsupportedEvidence {
				t.Fatal("strict trace unsupported evidence labeled supported")
			}
		case "task_success":
			if d.ValueType != metric.Enum || d.Labels[0] != "SUCCESS" {
				t.Fatal("Task Success collapsed into execution or metric pass")
			}
			if asset.CheckApplicability(d.Applicability, d.Availability, "AGENT_TASK", nil) != asset.MissingEvidence {
				t.Fatal("missing evidence defaulted to pass")
			}
		}
	}
	for _, key := range []string{"task_success", "execution_success", "evaluation_coverage", "recall@k", "mrr", "ndcg", "answer_correctness", "answer_groundedness"} {
		if !found[key] {
			t.Fatalf("missing P0 %s", key)
		}
	}
	app := asset.Applicability{CaseTypes: []string{"RAG"}, RequiredEvidence: []asset.EvidenceRequirement{{Kind: "context", Schema: "v1", BodyRequired: true}}, RuleRef: "rag.v1"}
	if asset.CheckApplicability(app, asset.ContractOnly, "AGENT_TASK", nil) != asset.NotApplicable {
		t.Fatal("N/A collapsed into missing")
	}
	if asset.CheckApplicability(app, asset.ContractOnly, "RAG", []asset.Evidence{{Kind: "context", Schema: "v1"}}) != asset.MissingEvidence {
		t.Fatal("opaque evidence treated as available body")
	}
	if asset.CheckApplicability(app, asset.ContractOnly, "RAG", []asset.Evidence{{Kind: "context", Schema: "v1", BodyAvailable: true}}) != asset.Applicable {
		t.Fatal("valid evidence not applicable")
	}
}
