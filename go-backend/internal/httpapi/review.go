package httpapi

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/decision"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/postgres"
	rv "agentevalops/go-backend/internal/review"
	"time"
)

type reviewSourceRequest struct {
	Case            *asset.Ref `json:"case,omitempty"`
	Type            string     `json:"type"`
	RunID           string     `json:"run_id"`
	ResultID        string     `json:"result_id"`
	ObservationID   string     `json:"observation_id"`
	GateID          string     `json:"gate_id"`
	CandidateSource string     `json:"candidate_source"`
	Classification  string     `json:"classification"`
}

func (d reviewSourceRequest) domain() rv.SourceRef {
	return rv.SourceRef{Type: d.Type, RunID: d.RunID, ResultID: d.ResultID, ObservationID: d.ObservationID, GateID: d.GateID, CandidateSource: d.CandidateSource, Classification: d.Classification, Case: d.Case}
}

type enqueueRequest struct {
	Source      reviewSourceRequest `json:"source"`
	Schema      asset.Ref           `json:"schema"`
	Kind        rv.DecisionKind     `json:"kind"`
	Policy      rv.Policy           `json:"policy"`
	Reason      string              `json:"reason"`
	Priority    string              `json:"priority"`
	Criticality string              `json:"criticality"`
}
type leaseRequest struct {
	LeaseSeconds int `json:"lease_seconds"`
}
type ownedRequest struct {
	Slot         int    `json:"slot"`
	Token        string `json:"token"`
	LeaseSeconds int    `json:"lease_seconds"`
}
type judgmentRequest struct {
	Kind         rv.DecisionKind `json:"kind"`
	Value        string          `json:"value"`
	Reason       string          `json:"reason"`
	EvidenceRefs []string        `json:"evidence_refs"`
	Confidence   *float64        `json:"confidence"`
}

func (d judgmentRequest) domain() rv.Judgment {
	return rv.Judgment{Kind: d.Kind, Value: d.Value, Reason: d.Reason, EvidenceRefs: d.EvidenceRefs, Confidence: d.Confidence}
}

type submitRequest struct {
	Slot     int             `json:"slot"`
	Token    string          `json:"token"`
	Decision judgmentRequest `json:"decision"`
}
type adjudicateRequest struct {
	Decision   judgmentRequest `json:"decision"`
	Reason     string          `json:"reason"`
	Supersedes *string         `json:"supersedes"`
}
type calibrationRequest struct {
	Dataset       asset.Ref              `json:"dataset"`
	Evaluator     asset.Ref              `json:"evaluator"`
	Schema        asset.Ref              `json:"schema"`
	Samples       []rv.CalibrationSample `json:"samples"`
	Protocol      rv.Protocol            `json:"protocol"`
	Sampling      rv.Sampling            `json:"sampling"`
	PositiveClass string                 `json:"positive_class"`
}
type calibrationCompareRequest struct {
	Baseline  string `json:"baseline"`
	Candidate string `json:"candidate"`
}
type draftRequest struct {
	ItemID               string              `json:"item_id"`
	GoldenID             *string             `json:"golden_id"`
	CaseVersion          string              `json:"case_version"`
	Body                 catalog.CaseContent `json:"body"`
	Sanitization         string              `json:"sanitization"`
	Policy               string              `json:"policy"`
	Reason               string              `json:"reason"`
	HumanSupplement      bool                `json:"human_supplement"`
	UseGoldenGroundTruth bool                `json:"use_golden_ground_truth"`
	Supersedes           *string             `json:"supersedes"`
}
type datasetFeedbackRequest struct {
	Base     asset.Ref `json:"base"`
	Target   asset.Ref `json:"target"`
	DraftIDs []string  `json:"draft_ids"`
}
type exceptionRequest struct {
	ItemID          string    `json:"item_id"`
	GateID          string    `json:"gate_id"`
	Code            string    `json:"code"`
	Scope           string    `json:"scope"`
	RequestedReason string    `json:"requested_reason"`
	Decision        string    `json:"decision"`
	Reason          string    `json:"reason"`
	Expiry          time.Time `json:"expiry"`
}
type policyProofRequest struct {
	Version           string          `json:"version"`
	Body              decision.Policy `json:"body"`
	ExceptionProofIDs []string        `json:"exception_proof_ids"`
}

func (s *Server) reviewRoutes() {
	reviews := postgres.Reviews{Pool: s.Pool, DefaultLease: 30 * time.Minute, MaxLease: 24 * time.Hour}
	s.add("POST", "/reviews", identity.Review, true, enqueueRequest{}, func(r *request) (any, error) {
		var d enqueueRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		v, e := reviews.EnqueueReview(r.ctx(), r.reviewScope(), rv.Enqueue{ID: r.CommandID, Source: d.Source.domain(), Schema: d.Schema, Kind: d.Kind, Policy: d.Policy, Reason: d.Reason, Priority: d.Priority, Criticality: d.Criticality})
		return map[string]any{"id": v.ID, "status": v.Status}, e
	})
	s.add("GET", "/reviews", identity.Review, false, nil, func(r *request) (any, error) {
		key, n, e := r.pagination("reviews")
		if e != nil {
			return nil, e
		}
		items, e := reviews.ListReviewQueue(r.ctx(), r.reviewScope(), key, n+1)
		if e != nil {
			return nil, e
		}
		out := page{}
		if len(items) > n {
			out.NextCursor = s.encodeCursor(r.scope().ProjectID, "reviews", items[n-1].ID)
			items = items[:n]
		}
		safe := []reviewResponse{}
		for _, item := range items {
			safe = append(safe, reviewProjection(rv.ReadModel{Item: item}, r.Access.Principal, false))
		}
		out.Items = safe
		return out, nil
	})
	s.add("GET", "/reviews/{id}", identity.Review, false, nil, func(r *request) (any, error) {
		v, e := reviews.GetReview(r.ctx(), r.reviewScope(), r.HTTP.PathValue("id"))
		return reviewProjection(v, r.Access.Principal, false), e
	})
	s.add("GET", "/reviews/{id}/adjudication-view", identity.Adjudicate, false, nil, func(r *request) (any, error) {
		v, e := reviews.GetAdjudicationReview(r.ctx(), r.reviewScope(), r.HTTP.PathValue("id"))
		if e != nil {
			return nil, e
		}
		if v.Item.Status != "ADJUDICATION_REQUIRED" && v.Item.Status != "COMPLETED" {
			return nil, asset.ErrUnsupported
		}
		return reviewProjection(v, r.Access.Principal, true), nil
	})
	s.add("POST", "/reviews/{id}/claim", identity.Review, false, leaseRequest{}, func(r *request) (any, error) {
		var d leaseRequest
		if e := r.decode(&d); e != nil || d.LeaseSeconds < 0 {
			return nil, asset.ErrInvalid
		}
		v, e := reviews.ClaimReview(r.ctx(), r.reviewScope(), r.HTTP.PathValue("id"), time.Duration(d.LeaseSeconds)*time.Second)
		if e == nil {
			e = s.Identity.Audit(r.ctx(), r.Access, r.ID, "review_claimed", v.ItemID)
		}
		return claimProjection(v), e
	})
	s.add("POST", "/reviews/{id}/renew", identity.Review, false, ownedRequest{}, func(r *request) (any, error) {
		var d ownedRequest
		if e := r.decode(&d); e != nil || d.LeaseSeconds < 0 {
			return nil, asset.ErrInvalid
		}
		v, e := reviews.RenewReview(r.ctx(), r.reviewScope(), rv.Claim{ItemID: r.HTTP.PathValue("id"), Slot: d.Slot, Token: d.Token}, time.Duration(d.LeaseSeconds)*time.Second)
		return claimProjection(v), e
	})
	s.add("POST", "/reviews/{id}/release", identity.Review, false, ownedRequest{}, func(r *request) (any, error) {
		var d ownedRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		e := reviews.ReleaseReview(r.ctx(), r.reviewScope(), rv.Claim{ItemID: r.HTTP.PathValue("id"), Slot: d.Slot, Token: d.Token})
		return map[string]bool{"released": e == nil}, e
	})
	s.add("POST", "/reviews/{id}/annotations", identity.Review, true, submitRequest{}, func(r *request) (any, error) {
		var d submitRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		item, e := reviews.GetReview(r.ctx(), r.reviewScope(), r.HTTP.PathValue("id"))
		if e != nil {
			return nil, e
		}
		v, e := reviews.SubmitAnnotation(r.ctx(), r.reviewScope(), rv.Submit{ID: r.CommandID, ItemID: item.Item.ID, Token: d.Token, Slot: d.Slot, SourceDigest: item.Item.Source.Digest, SchemaDigest: item.Item.SchemaDigest, Decision: d.Decision.domain()})
		if e == nil {
			e = s.Identity.Audit(r.ctx(), r.Access, r.ID, "review_submitted", v.ID)
		}
		return map[string]any{"id": v.ID, "item_id": v.ItemID, "decision": v.Decision, "submitted_at": v.SubmittedAt}, e
	})
	s.add("POST", "/reviews/{id}/adjudications", identity.Adjudicate, true, adjudicateRequest{}, func(r *request) (any, error) {
		var d adjudicateRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		v, e := reviews.AdjudicateReview(r.ctx(), r.reviewScope(), r.CommandID, r.HTTP.PathValue("id"), d.Decision.domain(), d.Reason, d.Supersedes)
		return adjudicationProjection(v), e
	})
	s.add("POST", "/reviews/{id}/golden", identity.PublishGolden, true, struct{}{}, func(r *request) (any, error) {
		if e := r.decode(&struct{}{}); e != nil {
			return nil, e
		}
		v, e := reviews.PublishGoldenLabel(r.ctx(), r.reviewScope(), r.CommandID, r.HTTP.PathValue("id"))
		if e == nil {
			e = s.Identity.Audit(r.ctx(), r.Access, r.ID, "golden_published", v.ID)
		}
		return goldenProjection(v), e
	})
	s.add("GET", "/reviews/coverage", identity.Review, false, nil, func(r *request) (any, error) { return reviews.GetHumanCoverage(r.ctx(), r.reviewScope()) })
	s.add("GET", "/golden/{id}", identity.PublishGolden, false, nil, func(r *request) (any, error) {
		v, e := reviews.GetGoldenLabel(r.ctx(), r.reviewScope(), r.HTTP.PathValue("id"))
		return goldenProjection(v), e
	})
	s.add("POST", "/calibrations", identity.ManagePolicy, true, calibrationRequest{}, func(r *request) (any, error) {
		var d calibrationRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		_, e := reviews.PrepareCalibration(r.ctx(), r.reviewScope(), rv.CalibrationCommand{ID: r.CommandID, Dataset: d.Dataset, Evaluator: d.Evaluator, Schema: d.Schema, Samples: d.Samples, Protocol: d.Protocol, Sampling: d.Sampling, PositiveClass: d.PositiveClass})
		if e != nil {
			return nil, e
		}
		v, e := reviews.CompleteCalibration(r.ctx(), r.reviewScope(), r.CommandID)
		return calibrationProjection(v), e
	})
	s.add("GET", "/calibrations/{id}", identity.ManagePolicy, false, nil, func(r *request) (any, error) {
		v, e := reviews.GetCalibrationReport(r.ctx(), r.reviewScope(), r.HTTP.PathValue("id"))
		return calibrationProjection(v), e
	})
	s.add("POST", "/calibrations/compare", identity.ManagePolicy, false, calibrationCompareRequest{}, func(r *request) (any, error) {
		var d calibrationCompareRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		return reviews.CompareEvaluatorCalibrations(r.ctx(), r.reviewScope(), d.Baseline, d.Candidate)
	})
	s.add("POST", "/drafts", identity.PublishDataset, true, draftRequest{}, func(r *request) (any, error) {
		var d draftRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		caseRef := asset.Ref{EntityID: identity.CommandID(r.Access.Principal.ID, r.scope().ProjectID, "reviewed-case:"+r.CommandID, "case"), Version: d.CaseVersion}
		v, e := reviews.CreateReviewedCaseDraft(r.ctx(), r.reviewScope(), rv.Draft{ID: r.CommandID, ItemID: d.ItemID, GoldenID: d.GoldenID, Case: caseRef, Body: d.Body, Sanitization: d.Sanitization, Policy: d.Policy, Reason: d.Reason, HumanSupplement: d.HumanSupplement, UseGoldenGroundTruth: d.UseGoldenGroundTruth, Supersedes: d.Supersedes})
		return draftProjection(v), e
	})
	s.add("GET", "/drafts/{id}", identity.PublishDataset, false, nil, func(r *request) (any, error) {
		v, e := reviews.GetReviewedCaseDraft(r.ctx(), r.reviewScope(), r.HTTP.PathValue("id"))
		return draftProjection(v), e
	})
	s.add("POST", "/drafts/{id}/publish", identity.PublishDataset, true, createLogicalRequest{}, func(r *request) (any, error) {
		var d createLogicalRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		v, e := reviews.PublishReviewedCase(r.ctx(), r.reviewScope(), r.HTTP.PathValue("id"), d.Name)
		if e == nil {
			e = s.Identity.Audit(r.ctx(), r.Access, r.ID, "reviewed_case_published", v.Ref().EntityID)
		}
		return v, e
	})
	s.add("POST", "/dataset-feedback", identity.PublishDataset, true, datasetFeedbackRequest{}, func(r *request) (any, error) {
		var d datasetFeedbackRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		v, e := reviews.BuildDatasetRevisionFromReviewedCases(r.ctx(), r.reviewScope(), d.Base, d.Target, d.DraftIDs)
		if e == nil {
			e = s.Identity.Audit(r.ctx(), r.Access, r.ID, "dataset_published", v.Ref().EntityID)
		}
		return v, e
	})
	s.add("POST", "/gate-exceptions", identity.ApproveException, true, exceptionRequest{}, func(r *request) (any, error) {
		var d exceptionRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		v, e := reviews.ReviewGateException(r.ctx(), r.reviewScope(), rv.GateException{ID: r.CommandID, ItemID: d.ItemID, GateID: d.GateID, Code: d.Code, Scope: d.Scope, RequestedReason: d.RequestedReason, Decision: d.Decision, Reason: d.Reason, Expiry: d.Expiry})
		if e == nil {
			e = s.Identity.Audit(r.ctx(), r.Access, r.ID, "exception_reviewed", v.ID)
		}
		return v, e
	})
	s.add("GET", "/gate-exceptions/{id}/proof", identity.ApproveException, false, nil, func(r *request) (any, error) {
		v, e := reviews.AcceptedExceptionProof(r.ctx(), r.reviewScope(), r.HTTP.PathValue("id"))
		return map[string]any{"proof": v, "next_step": "显式发布新 PolicyVersion 并创建新 Gate；旧 Gate 保持不变"}, e
	})
	s.add("POST", "/policies/{id}/versions-with-exceptions", identity.ManagePolicy, true, policyProofRequest{}, func(r *request) (any, error) {
		var d policyProofRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		if !r.Access.Principal.Has(identity.ApproveException) || len(d.ExceptionProofIDs) == 0 || len(d.Body.Exceptions) > 0 || len(d.Body.AcceptedDifferences) > 0 {
			return nil, asset.ErrForbidden
		}
		for _, id := range d.ExceptionProofIDs {
			v, e := reviews.AcceptedExceptionProof(r.ctx(), r.reviewScope(), id)
			if e != nil {
				return nil, e
			}
			d.Body.Exceptions = append(d.Body.Exceptions, v)
		}
		v, e := (postgres.Decisions{Pool: s.Pool}).PublishPolicyVersion(r.ctx(), r.scope(), asset.Publish[decision.Policy]{Ref: asset.Ref{EntityID: r.HTTP.PathValue("id"), Version: d.Version}, Body: d.Body, Source: source(r)})
		return v, e
	})
}
func claimProjection(c rv.Claim) any {
	return map[string]any{"item_id": c.ItemID, "slot": c.Slot, "token": c.Token, "lease": c.Lease}
}
