package postgres

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"time"

	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	ob "agentevalops/go-backend/internal/observation"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Online struct{ Pool *pgxpool.Pool }

func (k Online) transaction(ctx context.Context, s ob.Scope, admission bool, fn func(pgx.Tx) (ev.Reply, error)) (ev.Reply, error) {
	return (Evaluation{Pool: k.Pool}).transact(ctx, ev.Scope{Scope: s.Scope, Epoch: s.Epoch}, "", admission, func(tx pgx.Tx, _ ev.Run, _ string) (ev.Reply, error) { return fn(tx) })
}
func (k Online) IngestStrict(ctx context.Context, s ob.Scope, source string, r io.Reader) (ev.Reply, error) {
	if !s.Ingest || !s.AuthenticatedSource || source != "LOCALAGENT_TRACE_V1" {
		return ev.Reply{Code: ev.Rejected, Reason: "AUTHENTICATED_SOURCE_REQUIRED"}, nil
	}
	d, err := ob.ReadTrace(r)
	if err != nil {
		return ev.Reply{Code: ev.Rejected, Reason: err.Error()}, nil
	}
	t := d.Trace
	result, err := k.transaction(ctx, s, true, func(tx pgx.Tx) (ev.Reply, error) {
		traceUUID, spanUUID := asset.NewID(), asset.NewID()
		_, e := tx.Exec(ctx, `INSERT INTO localagent_external_trace_identity(external_trace_id,project_id,internal_trace_uuid,run_id) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING`, t.TraceID, s.ProjectID, traceUUID, t.RunID)
		if e != nil {
			return ev.Reply{}, e
		}
		var p, run string
		e = tx.QueryRow(ctx, `SELECT project_id::text,run_id,internal_trace_uuid::text FROM localagent_external_trace_identity WHERE external_trace_id=$1 FOR UPDATE`, t.TraceID).Scan(&p, &run, &traceUUID)
		if e != nil {
			return ev.Reply{}, e
		}
		if p != s.ProjectID || run != t.RunID {
			return ev.Reply{}, stop(ev.Conflict, "TRACE_IDENTITY_CONFLICT")
		}
		var parentUUID *string
		if t.Parent != nil {
			var parentProject, parentTrace, uuid string
			e = tx.QueryRow(ctx, `SELECT project_id::text,external_trace_id,internal_span_uuid::text FROM localagent_external_span_identity WHERE external_span_id=$1`, *t.Parent).Scan(&parentProject, &parentTrace, &uuid)
			if e != nil && !errors.Is(e, pgx.ErrNoRows) {
				return ev.Reply{}, e
			}
			if e == nil {
				if parentProject != s.ProjectID || parentTrace != t.TraceID {
					return ev.Reply{}, stop(ev.Conflict, "PARENT_IDENTITY_CONFLICT")
				}
				parentUUID = &uuid
			}
		}
		if t.Parent != nil && *t.Parent == t.SpanID {
			return ev.Reply{}, stop(ev.Rejected, "SELF_PARENT")
		}
		_, e = tx.Exec(ctx, `INSERT INTO localagent_external_span_identity(external_span_id,project_id,internal_span_uuid,external_trace_id) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING`, t.SpanID, s.ProjectID, spanUUID, t.TraceID)
		if e != nil {
			return ev.Reply{}, e
		}
		var spanTrace string
		e = tx.QueryRow(ctx, `SELECT project_id::text,external_trace_id,internal_span_uuid::text FROM localagent_external_span_identity WHERE external_span_id=$1`, t.SpanID).Scan(&p, &spanTrace, &spanUUID)
		if e != nil {
			return ev.Reply{}, e
		}
		if p != s.ProjectID || spanTrace != t.TraceID {
			return ev.Reply{}, stop(ev.Conflict, "SPAN_IDENTITY_CONFLICT")
		}
		receiptID := asset.NewID()
		var old string
		e = tx.QueryRow(ctx, `SELECT envelope_id::text,canonical_payload_digest FROM localagent_trace_envelope_sidecars WHERE external_span_id=$1`, t.SpanID).Scan(&receiptID, &old)
		duplicate := e == nil
		if e != nil && !errors.Is(e, pgx.ErrNoRows) {
			return ev.Reply{}, e
		}
		if duplicate && old != d.Digest {
			return ev.Reply{}, stop(ev.Conflict, "TRACE_DIGEST_CONFLICT")
		}
		if !duplicate {
			attrs, _ := json.Marshal(t.Attributes)
			_, e = tx.Exec(ctx, `INSERT INTO traces(trace_id,project_id,name,status,metadata,started_at,ended_at,tags,created_at) VALUES($1,$2,'localagent.trace','COMPLETED','{}',$3,$4,'{}',clock_timestamp()) ON CONFLICT DO NOTHING`, traceUUID, s.ProjectID, t.Started, t.Completed)
			if e != nil {
				return ev.Reply{}, e
			}
			legacyStatus := "ERROR"
			if t.Status == "OK" {
				legacyStatus = "OK"
			}
			_, e = tx.Exec(ctx, `INSERT INTO spans(span_id,trace_id,parent_span_id,name,kind,status,metadata,started_at,ended_at) VALUES($1,$2,$3,$4,'OTHER',$5,'{}',$6,$7)`, spanUUID, traceUUID, parentUUID, "localagent:"+t.Operation, legacyStatus, t.Started, t.Completed)
			if e != nil {
				return ev.Reply{}, e
			}
			_, e = tx.Exec(ctx, `INSERT INTO localagent_trace_envelope_sidecars(envelope_id,project_id,external_run_id,external_trace_id,external_span_id,external_parent_span_id,step_id,operation,component,started_at,completed_at,duration_ms,status,error_code,attributes,contract_identity,contract_version,contract_fingerprint,canonical_payload_digest,internal_trace_uuid,internal_span_uuid) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::numeric,$13,$14,$15,$16,1,$17,$18,$19,$20)`, receiptID, s.ProjectID, t.RunID, t.TraceID, t.SpanID, t.Parent, t.Step, t.Operation, t.Component, t.Started, t.Completed, d.Duration, t.Status, t.Error, attrs, t.ContractIdentity, t.Fingerprint, d.Digest, traceUUID, spanUUID)
			if e != nil {
				return ev.Reply{}, e
			}
		}
		// 已有历史 strict receipt 可被 Go 明确接收并补 Observation，不改历史 digest。
		o := ob.Observation{Ref: ob.Ref{ProjectID: s.ProjectID, ID: asset.NewID(), Digest: d.Digest, Schema: "trace-v1", Kind: ob.SpanKind}, TraceID: t.TraceID, RuntimeID: t.RunID, SpanID: &t.SpanID, Parent: t.Parent, StepID: t.Step, Source: source, Principal: s.Principal, Trust: "STRICT_AUTHENTICATED", Operation: t.Operation, Status: t.Status, Started: t.Started, Completed: t.Completed, Canonical: d.Canonical, BodyAvailability: "SAFE_METADATA_ONLY", Retention: "SAFE_METADATA", Evidence: []ob.Evidence{}}
		switch t.Operation {
		case "runtime.run":
			o.Ref.Kind = ob.TraceKind
		case "runtime.planning":
			o.Ref.Kind = ob.Plan
		case "runtime.step":
			o.Ref.Kind = ob.Step
		}
		o.Envelope, _ = asset.ParseJSON(d.Raw)
		o.Metadata, _ = asset.Freeze(map[string]any{"run_mode": t.Attributes["runtime_mode"]})
		o.Evidence = []ob.Evidence{{Kind: "observation", Schema: "trace-v1", Availability: "AVAILABLE", Digest: o.Envelope.Digest(), Source: "strict-receipt:" + receiptID, Body: o.Envelope, Retention: "SAFE_METADATA"}}
		_, e = tx.Exec(ctx, `INSERT INTO evaluation_observations(id,project_id,kind,source,source_principal,trust,source_identity,trace_id,runtime_id,schema_version,digest,canonical_bytes,envelope_bytes,observation_bytes,receipt_id,completed_at) VALUES($1,$2,$3,'LOCALAGENT_TRACE_V1',$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) ON CONFLICT(source,source_identity) DO NOTHING`, o.Ref.ID, s.ProjectID, o.Ref.Kind, s.Principal, o.Trust, t.SpanID, t.TraceID, t.RunID, o.Ref.Schema, d.Digest, d.Canonical, d.Raw, encode(o), receiptID, t.Completed)
		if e != nil {
			return ev.Reply{}, e
		}
		var id, digest string
		e = tx.QueryRow(ctx, `SELECT id::text,digest FROM evaluation_observations WHERE source='LOCALAGENT_TRACE_V1' AND source_identity=$1 AND project_id=$2`, t.SpanID, s.ProjectID).Scan(&id, &digest)
		if e != nil {
			return ev.Reply{}, e
		}
		if digest != d.Digest {
			return ev.Reply{}, stop(ev.Conflict, "OBSERVATION_DIGEST_CONFLICT")
		}
		code := ev.Applied
		if duplicate {
			code = ev.AlreadyApplied
		}
		return ev.Reply{Code: code, ID: id}, nil
	})
	event := "trace_ingested"
	if result.Code == ev.AlreadyApplied {
		event = "trace_duplicate"
	}
	if result.Code == ev.Conflict {
		event = "trace_conflict"
	}
	slog.Info(event, "event", event, "project_id", s.ProjectID)
	return result, err
}

func readObservation(ctx context.Context, q queryer, s asset.Scope, id string) (ob.Observation, error) {
	var o ob.Observation
	var raw []byte
	var integrity string
	e := q.QueryRow(ctx, `SELECT o.observation_bytes,o.created_at,
 CASE WHEN sc.external_parent_span_id IS NULL THEN 'NOT_APPLICABLE' WHEN parent.external_span_id IS NULL THEN 'PENDING'
 WHEN parent.project_id=o.project_id AND parent.external_trace_id=o.trace_id THEN 'MATCHED' ELSE 'PARENT_IDENTITY_CONFLICT' END
 FROM evaluation_observations o JOIN projects p ON p.id=o.project_id
 LEFT JOIN localagent_trace_envelope_sidecars sc ON sc.envelope_id=o.receipt_id
 LEFT JOIN localagent_external_span_identity parent ON parent.external_span_id=sc.external_parent_span_id
 WHERE o.project_id=$1 AND p.org_id=$2 AND o.id=$3`, s.ProjectID, s.OrganizationID, id).Scan(&raw, &o.Created, &integrity)
	if e != nil {
		return o, dbError(e)
	}
	created := o.Created
	e = json.Unmarshal(raw, &o)
	o.Created = created
	o.ParentIntegrity = integrity
	return o, e
}
func (k Online) GetObservation(ctx context.Context, s asset.Scope, id string) (ob.Observation, error) {
	if s.Validate(false) != nil || !asset.ValidID(id) {
		return ob.Observation{}, asset.ErrInvalid
	}
	return readObservation(ctx, k.Pool, s, id)
}
func (k Online) ListObservations(ctx context.Context, s asset.Scope, c ob.Cursor, limit int) ([]ob.Observation, error) {
	if s.Validate(false) != nil || limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	rows, e := k.Pool.Query(ctx, `SELECT o.id::text FROM evaluation_observations o JOIN projects p ON p.id=o.project_id WHERE o.project_id=$1 AND p.org_id=$2 AND ($3::timestamptz IS NULL OR (o.created_at,o.id)>($3,$4::uuid)) ORDER BY o.created_at,o.id LIMIT $5`, s.ProjectID, s.OrganizationID, nullableTime(c.Created), nullableID(c.ID), limit)
	if e != nil {
		return nil, e
	}
	ids := []string{}
	for rows.Next() {
		var id string
		if e = rows.Scan(&id); e != nil {
			rows.Close()
			return nil, e
		}
		ids = append(ids, id)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return nil, e
	}
	result := []ob.Observation{}
	for _, id := range ids {
		o, e := k.GetObservation(ctx, s, id)
		if e != nil {
			return nil, e
		}
		result = append(result, o)
	}
	return result, nil
}
func nullableTime(t time.Time) any {
	if t.IsZero() {
		return nil
	}
	return t
}
func nullableID(id string) any {
	if id == "" {
		return nil
	}
	return id
}

// 普通观测的显式正文策略不使其获得 strict/process trust。
func (k Online) StoreObservation(ctx context.Context, s ob.Scope, o ob.Observation) (ev.Reply, error) {
	if !s.Ingest || o.Trust != "NORMAL_OBSERVATION" || o.Ref.ProjectID != s.ProjectID || !asset.ValidID(o.Ref.ID) || !asset.Text(o.Source) || o.Source == "LOCALAGENT_TRACE_V1" || o.TraceID == "" || o.Completed.IsZero() || o.Retention != "EXPLICIT_EVALUATION_RETENTION" || o.Envelope.String() == "null" || len(encode(o)) > 1024*1024 {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	switch o.Ref.Kind {
	case ob.TraceKind, ob.SpanKind, ob.Plan, ob.Step, ob.ToolCall, ob.Retrieval:
	default:
		return ev.Reply{Code: ev.Rejected}, nil
	}
	for _, e := range o.Evidence {
		if e.Availability == "AVAILABLE" && (e.Body.String() == "null" || e.Digest != e.Body.Digest() || e.Retention != "EXPLICIT_EVALUATION_RETENTION") {
			return ev.Reply{Code: ev.Rejected, Reason: "EXPLICIT_BODY_POLICY_REQUIRED"}, nil
		}
	}
	o.Ref.Digest = ""
	o.Canonical = nil
	o.Created = time.Time{}
	o.ParentIntegrity = ""
	o.Principal = s.Principal
	j, e := asset.Freeze(o)
	if e != nil {
		return ev.Reply{}, e
	}
	o.Canonical = j.Bytes()
	o.Ref.Digest = j.Digest()
	o.Principal = s.Principal
	return k.transaction(ctx, s, true, func(tx pgx.Tx) (ev.Reply, error) {
		_, e := tx.Exec(ctx, `INSERT INTO evaluation_observations(id,project_id,kind,source,source_principal,trust,source_identity,trace_id,runtime_id,schema_version,digest,canonical_bytes,envelope_bytes,observation_bytes,completed_at) VALUES($1::uuid,$2,$3,$4,$5,'NORMAL_OBSERVATION',($1::uuid)::text,$6,$7,$8,$9,$10,$10,$11,$12) ON CONFLICT DO NOTHING`, o.Ref.ID, s.ProjectID, o.Ref.Kind, o.Source, s.Principal, o.TraceID, o.RuntimeID, o.Ref.Schema, o.Ref.Digest, o.Canonical, encode(o), o.Completed)
		if e != nil {
			return ev.Reply{}, e
		}
		saved, e := readObservation(ctx, tx, s.Scope, o.Ref.ID)
		if e != nil {
			return ev.Reply{}, e
		}
		a := saved
		a.Created = time.Time{}
		a.ParentIntegrity = ""
		o.Created = time.Time{}
		if !bytes.Equal(encode(a), encode(o)) {
			return ev.Reply{}, stop(ev.Conflict, "OBSERVATION_INTENT_CONFLICT")
		}
		return ev.Reply{Code: ev.Applied, ID: o.Ref.ID}, nil
	})
}
