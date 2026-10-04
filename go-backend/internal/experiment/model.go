// Package experiment 拥有冻结实验意图和 Run 引用，不拥有第二套 Run lifecycle。
package experiment

import (
	"bytes"
	"context"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
)

type Baseline struct{ RunID, ExperimentID string }
type Create struct {
	ID, Name  string
	Candidate asset.JSON
	Baseline  *Baseline
	Repeat    int
	Run       ev.CreateRun
}
type Intent struct {
	Name      string
	Candidate asset.JSON
	Baseline  *Baseline
	Repeat    int
	Snapshot  ev.RunSnapshot
}
type Slot struct {
	Repeat    int
	CommandID string
	RunID     *string
}
type Experiment struct {
	ID, ProjectID, CreatedBy, Digest string
	CreatedAt                        time.Time
	Intent                           Intent
	Slots                            []Slot
}
type View struct {
	Experiment Experiment
	Status     string
	Runs       []ev.RunState
}
type Store interface {
	CreateExperiment(context.Context, ev.Scope, Experiment) (Experiment, error)
	GetExperiment(context.Context, asset.Scope, string) (Experiment, error)
	ListExperiments(context.Context, asset.Scope, int) ([]Experiment, error)
	AttachRun(context.Context, ev.Scope, string, int, string) error
}
type Service struct {
	Store        Store
	Kernel       ev.Persistence
	Reader       ev.Reader
	Builder      ev.Builder
	Capabilities ev.EvaluatorExecutionCapability
}

func Freeze(c Create, scope ev.Scope, caps ev.EvaluatorExecutionCapability) (Experiment, error) {
	if scope.Validate(false) != nil || !scope.Create || !asset.ValidID(c.ID) || !asset.Text(c.Name) || c.Repeat < 1 || c.Repeat > 100 || c.Candidate.String() == "null" {
		return Experiment{}, asset.ErrInvalid
	}
	if c.Baseline != nil && ((c.Baseline.RunID == "") == (c.Baseline.ExperimentID == "")) {
		return Experiment{}, asset.ErrInvalid
	}
	if c.Baseline != nil && ((c.Baseline.RunID != "" && !asset.ValidID(c.Baseline.RunID)) || (c.Baseline.ExperimentID != "" && !asset.ValidID(c.Baseline.ExperimentID))) {
		return Experiment{}, asset.ErrInvalid
	}
	snapshot, err := ev.FreezeRun(c.Run, scope.ProjectID, caps)
	if err != nil {
		return Experiment{}, err
	}
	intent := Intent{Name: c.Name, Candidate: c.Candidate, Baseline: c.Baseline, Repeat: c.Repeat, Snapshot: snapshot}
	digest, err := ev.Intent(struct {
		Intent Intent
		Actor  string
	}{intent, scope.Principal})
	if err != nil {
		return Experiment{}, err
	}
	exp := Experiment{ID: c.ID, ProjectID: scope.ProjectID, CreatedBy: scope.Principal, Digest: digest, Intent: intent}
	for n := 1; n <= c.Repeat; n++ {
		exp.Slots = append(exp.Slots, Slot{Repeat: n, CommandID: asset.NewID()})
	}
	return exp, nil
}
func (s Service) Create(ctx context.Context, scope ev.Scope, c Create) (Experiment, error) {
	exp, err := Freeze(c, scope, s.Capabilities)
	if err != nil {
		return Experiment{}, err
	}
	return s.Store.CreateExperiment(ctx, scope, exp)
}

// slots/command IDs 已 durable。CreateRun 后 crash、Attach 前 crash 都可重交同一 command 恢复。
func (s Service) Materialize(ctx context.Context, scope ev.Scope, id string) (Experiment, error) {
	exp, err := s.Store.GetExperiment(ctx, scope.Scope, id)
	if err != nil {
		return Experiment{}, err
	}
	input := exp.Intent.Snapshot.Input
	build := ev.BuildRunSnapshot{}
	if input.Suite != nil {
		ref := input.Suite.Ref
		build.Suite = &ref
	} else if input.Dataset != nil {
		ref := input.Dataset.Ref
		build.Dataset = &ref
		for _, spec := range input.Evaluators {
			build.Evaluators = append(build.Evaluators, catalog.EvaluatorBinding{Evaluator: spec.Identity.Ref, Metrics: spec.Metrics, Required: spec.Required, Applicability: spec.Applicability})
		}
	} else {
		return Experiment{}, asset.ErrInvalid
	}
	for _, c := range input.Manifest {
		build.SelectedCases = append(build.SelectedCases, c.Identity.Ref)
	}
	snapshot, err := s.Builder.BuildRunSnapshot(ctx, scope.Scope, build)
	if err != nil {
		return Experiment{}, err
	}
	if !bytes.Equal(snapshot.Bytes(), exp.Intent.Snapshot.InputBytes) {
		return Experiment{}, asset.ErrConflict
	}
	// actor 冻结为创建者；由已授权 application 调用，不改变已保存 command intent。
	creator := scope
	creator.Principal = exp.CreatedBy
	for _, slot := range exp.Slots {
		if slot.RunID != nil {
			continue
		}
		cmd := ev.CreateRun{CommandID: slot.CommandID, Snapshot: snapshot, Target: exp.Intent.Snapshot.Target, Subject: exp.Intent.Snapshot.Subject, Retry: exp.Intent.Snapshot.Retry}
		reply, e := s.Kernel.CreateRun(ctx, creator, cmd)
		if e != nil {
			return Experiment{}, e
		}
		if reply.Code != ev.Applied && reply.Code != ev.AlreadyApplied {
			return Experiment{}, asset.ErrConflict
		}
		if err = s.Store.AttachRun(ctx, scope, id, slot.Repeat, reply.ID); err != nil {
			return Experiment{}, err
		}
	}
	return s.Store.GetExperiment(ctx, scope.Scope, id)
}
func (s Service) Get(ctx context.Context, scope asset.Scope, id string) (View, error) {
	exp, err := s.Store.GetExperiment(ctx, scope, id)
	if err != nil {
		return View{}, err
	}
	view := View{Experiment: exp, Status: "PENDING"}
	complete := len(exp.Slots) > 0
	active := false
	for _, slot := range exp.Slots {
		if slot.RunID == nil {
			complete = false
			continue
		}
		state, e := s.Reader.ReadRunState(ctx, scope, *slot.RunID)
		if e != nil {
			return View{}, e
		}
		view.Runs = append(view.Runs, state)
		if state.Run.Status != ev.RunPending {
			active = true
		}
		if !state.Run.Status.Terminal() {
			complete = false
		}
	}
	if active {
		view.Status = "RUNNING"
	}
	if complete {
		view.Status = "FINISHED"
	}
	return view, nil
}
func (s Service) List(ctx context.Context, scope asset.Scope, limit int) ([]Experiment, error) {
	return s.Store.ListExperiments(ctx, scope, limit)
}
