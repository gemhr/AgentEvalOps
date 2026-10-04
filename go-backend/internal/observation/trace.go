// Package observation 拥有生产观测和在线判分；观测永不授予 execution 写权。
package observation

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"math/big"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf16"

	"agentevalops/go-backend/internal/asset"
)

const TraceAlgorithm = "trace-v1"
const TraceIdentity = "localagent.runtime.trace_export"
const TraceFingerprint = "6fc033bb4310c7671541d7dc9e7297fdf0d0bb32605651b840ccd0fd173390ab"
const MaxTraceBytes = 16384

var safeID = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)
var digestID = regexp.MustCompile(`^[0-9a-f]{64}$`)

type Trace struct {
	ContractIdentity string         `json:"contract_identity"`
	ContractVersion  int            `json:"contract_version"`
	Fingerprint      string         `json:"contract_fingerprint"`
	RunID            string         `json:"run_id"`
	TraceID          string         `json:"trace_id"`
	SpanID           string         `json:"span_id"`
	Parent           *string        `json:"parent_span_id"`
	Step             *string        `json:"step_id"`
	Operation        string         `json:"operation"`
	Component        string         `json:"component"`
	Started          time.Time      `json:"started_at"`
	Completed        time.Time      `json:"completed_at"`
	Duration         json.Number    `json:"duration_ms"`
	Status           string         `json:"status"`
	Error            *string        `json:"error_code"`
	Attributes       map[string]any `json:"attributes"`
}
type DecodedTrace struct {
	Trace            Trace
	Canonical        []byte
	Digest, Duration string
	Raw              []byte
}

// ReadTrace 在读取与 JSON 解析前执行冻结的 body 上限。
func ReadTrace(r io.Reader) (DecodedTrace, error) {
	raw, err := io.ReadAll(io.LimitReader(r, MaxTraceBytes+1))
	if err != nil {
		return DecodedTrace{}, err
	}
	if len(raw) > MaxTraceBytes {
		return DecodedTrace{}, fmt.Errorf("TRACE_BODY_TOO_LARGE")
	}
	return DecodeTrace(raw)
}
func DecodeTrace(raw []byte) (DecodedTrace, error) {
	fail := func() (DecodedTrace, error) { return DecodedTrace{}, fmt.Errorf("LOCALAGENT_ENVELOPE_INVALID") }
	if len(raw) > MaxTraceBytes {
		return DecodedTrace{}, fmt.Errorf("TRACE_BODY_TOO_LARGE")
	}
	j, err := asset.ParseJSON(raw)
	if err != nil {
		return fail()
	}
	var keys map[string]json.RawMessage
	if json.Unmarshal(raw, &keys) != nil || keys == nil {
		return fail()
	}
	fields := []string{"contract_identity", "contract_version", "contract_fingerprint", "run_id", "trace_id", "span_id", "parent_span_id", "step_id", "operation", "component", "started_at", "completed_at", "duration_ms", "status", "error_code"}
	for _, key := range fields {
		if _, ok := keys[key]; !ok {
			return fail()
		}
	}
	var t Trace
	if j.Decode(&t) != nil {
		return fail()
	}
	if t.Attributes == nil {
		if _, ok := keys["attributes"]; ok {
			return fail()
		}
		t.Attributes = map[string]any{}
	}
	// 数字类型从严格 token decoder 保留；不能经 float64 转换整数。
	for _, key := range []string{"contract_version", "duration_ms"} {
		if bytes.Equal(keys[key], []byte("null")) || len(keys[key]) == 0 || keys[key][0] == '"' {
			return fail()
		}
	}
	if t.ContractIdentity != TraceIdentity || t.ContractVersion != 1 || t.Fingerprint != TraceFingerprint {
		return fail()
	}
	for _, id := range []string{t.RunID, t.TraceID, t.SpanID, t.Component} {
		if !safeID.MatchString(id) {
			return fail()
		}
	}
	for _, id := range []*string{t.Parent, t.Step, t.Error} {
		if id != nil && !safeID.MatchString(*id) {
			return fail()
		}
	}
	category, ok := traceCategories[t.Operation]
	if !ok || ((category == "step" || category == "synthesis" || category == "delivery" || category == "memory") != (t.Step != nil)) {
		return fail()
	}
	_, so := t.Started.Zone()
	_, co := t.Completed.Zone()
	if so != 0 || co != 0 || t.Completed.Before(t.Started) || t.Started.Year() < 1 || t.Completed.Year() > 9999 {
		return fail()
	}
	// Python datetime 的 microsecond 精度。
	t.Started = t.Started.Truncate(time.Microsecond)
	t.Completed = t.Completed.Truncate(time.Microsecond)
	duration, err := exactNumber(string(t.Duration))
	if err != nil || strings.HasPrefix(duration, "-") {
		return fail()
	}
	if !strings.ContainsAny(string(t.Duration), ".eE") {
		n, _ := new(big.Int).SetString(duration, 10)
		limit := new(big.Int).Sub(new(big.Int).Lsh(big.NewInt(1), 1024), new(big.Int).Lsh(big.NewInt(1), 970))
		limit.Sub(limit, big.NewInt(1))
		if n.Cmp(limit) > 0 {
			return fail()
		}
	}
	if !has(t.Status, "OK", "ERROR", "CANCELLED", "TIMED_OUT") || (t.Status == "OK") != (t.Error == nil) {
		return fail()
	}
	for key, value := range t.Attributes {
		typ, ok := traceAttributes[category][key]
		if !ok || !validAttribute(typ, value) {
			return fail()
		}
		if domain, ok := traceDomains[key]; ok {
			v, _ := value.(string)
			if !has(v, domain...) {
				return fail()
			}
		}
		if key == "publish_attempt_count" {
			n, _ := new(big.Int).SetString(fmt.Sprint(value), 10)
			if n.Cmp(big.NewInt(1)) > 0 {
				return fail()
			}
		}
	}
	payload := map[string]any{"contract_identity": t.ContractIdentity, "contract_version": json.Number("1"), "contract_fingerprint": t.Fingerprint, "run_id": t.RunID, "trace_id": t.TraceID, "span_id": t.SpanID, "parent_span_id": pointerValue(t.Parent), "step_id": pointerValue(t.Step), "operation": t.Operation, "component": t.Component, "started_at": traceTime(t.Started), "completed_at": traceTime(t.Completed), "duration_ms": duration, "status": t.Status, "error_code": pointerValue(t.Error), "attributes": t.Attributes}
	canonical, err := TraceCanonical(payload)
	if err != nil {
		return fail()
	}
	return DecodedTrace{t, canonical, fmt.Sprintf("%x", sha256.Sum256(canonical)), duration, append([]byte(nil), raw...)}, nil
}
func pointerValue(p *string) any {
	if p == nil {
		return nil
	}
	return *p
}
func traceTime(t time.Time) string {
	if t.Nanosecond() == 0 {
		return t.UTC().Format("2006-01-02T15:04:05+00:00")
	}
	return t.UTC().Format("2006-01-02T15:04:05.000000+00:00")
}
func has(s string, values ...string) bool {
	for _, v := range values {
		if s == v {
			return true
		}
	}
	return false
}
func validAttribute(typ string, v any) bool {
	switch typ {
	case "b":
		_, ok := v.(bool)
		return ok
	case "i":
		n, ok := v.(json.Number)
		if !ok || strings.ContainsAny(string(n), ".eE") {
			return false
		}
		x, ok := new(big.Int).SetString(string(n), 10)
		return ok && x.Sign() >= 0
	case "s":
		s, ok := v.(string)
		return ok && safeID.MatchString(s)
	case "d":
		s, ok := v.(string)
		return ok && digestID.MatchString(s)
	}
	return false
}

var traceCategories = map[string]string{"runtime.run": "run", "runtime.planning": "planning", "runtime.step": "step", "runtime.synthesis": "synthesis", "runtime.output_delivery": "delivery", "runtime.final_memory_commit": "memory"}
var traceAttributes = map[string]map[string]string{
	"run":       {"plan_id": "s", "plan_version": "i", "plan_fingerprint": "d", "planning_source": "s", "step_count": "i", "selected_entry_agent_id": "s", "runtime_mode": "s", "final_status": "s", "stop_reason": "s", "shape": "s"},
	"planning":  {"planning_source": "s", "schema_version": "i", "planner_model_invoked": "b", "compiled_shape": "s", "specialist_count": "i", "synthesis_required": "b"},
	"step":      {"preferred_agent": "s", "execution_kind": "s", "output_policy": "s", "dependency_count": "i", "state": "s", "result_char_count": "i"},
	"synthesis": {"state": "s", "execution_kind": "s"},
	"delivery":  {"final_step_id": "s", "output_policy": "s", "delivery_status": "s", "gate_terminal_state": "s", "publish_attempt_count": "i", "partially_persisted": "b", "output_char_count": "i"},
	"memory":    {"persist_enabled": "b", "entry_agent_id": "s", "memory_scope": "s", "delivery_status": "s", "user_write_status": "s", "assistant_write_status": "s", "transaction_used": "b"},
}
var traceDomains = map[string][]string{
	"planning_source": {"deterministic", "legacy_adapter", "model_generated", "unknown"}, "compiled_shape": {"0", "1", "2", "3", "unknown"}, "shape": {"0", "1", "2", "3", "unknown"},
	"final_status": {"CREATED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"}, "stop_reason": {"COMPLETED", "UNHANDLED_ERROR", "DEADLINE_EXCEEDED", "USER_CANCELLED", "CLIENT_DISCONNECTED", "SYSTEM_SHUTDOWN", "MAX_STEPS_REACHED", "NO_ACTION", "REPEATED_ACTION", "BUDGET_EXHAUSTED", "PLANNING_FAILED"},
	"execution_kind": {"AGENT", "SYNTHESIS"}, "output_policy": {"INTERNAL", "FINAL_PASSTHROUGH", "FINAL_SYNTHESIS"}, "delivery_status": {"DELIVERED", "FAILED", "OUTCOME_UNKNOWN"}, "gate_terminal_state": {"PUBLISHED", "FAILED", "OUTCOME_UNKNOWN"}, "memory_scope": {"direct"}, "user_write_status": {"NOT_ATTEMPTED", "WRITTEN", "FAILED"}, "assistant_write_status": {"NOT_ATTEMPTED", "WRITTEN", "FAILED"},
}

// exactNumber 按 binary64 的精确有理数展开 fixed point，不能用最短 float repr。
func exactNumber(s string) (string, error) {
	if !strings.ContainsAny(s, ".eE") {
		n, ok := new(big.Int).SetString(s, 10)
		if !ok {
			return "", asset.ErrInvalid
		}
		return n.String(), nil
	}
	f, e := strconv.ParseFloat(s, 64)
	if e != nil || math.IsInf(f, 0) || math.IsNaN(f) {
		return "", asset.ErrInvalid
	}
	if f == 0 {
		return "0", nil
	}
	r := new(big.Rat).SetFloat64(f)
	scale := r.Denom().BitLen() - 1
	if scale == 0 {
		return r.FloatString(0), nil
	}
	return strings.TrimRight(strings.TrimRight(r.FloatString(scale), "0"), "."), nil
}
func asciiString(s string) string {
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"', '\\':
			b.WriteByte('\\')
			b.WriteRune(r)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		default:
			if r < 32 || r >= 127 {
				if r > 0xffff {
					h, l := utf16.EncodeRune(r)
					fmt.Fprintf(&b, `\u%04x\u%04x`, h, l)
				} else {
					fmt.Fprintf(&b, `\u%04x`, r)
				}
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
	return b.String()
}
func TraceCanonical(v any) ([]byte, error) {
	var b strings.Builder
	var write func(any) error
	write = func(v any) error {
		switch x := v.(type) {
		case nil:
			b.WriteString("null")
		case string:
			b.WriteString(asciiString(x))
		case bool:
			b.WriteString(strconv.FormatBool(x))
		case json.Number:
			n, e := exactNumber(string(x))
			if e != nil {
				return e
			}
			b.WriteString(n)
		case map[string]any:
			keys := make([]string, 0, len(x))
			for k := range x {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			b.WriteByte('{')
			for i, k := range keys {
				if i > 0 {
					b.WriteByte(',')
				}
				b.WriteString(asciiString(k))
				b.WriteByte(':')
				if e := write(x[k]); e != nil {
					return e
				}
			}
			b.WriteByte('}')
		default:
			return asset.ErrInvalid
		}
		return nil
	}
	e := write(v)
	return []byte(b.String()), e
}
