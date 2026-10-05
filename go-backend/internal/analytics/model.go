// Package analytics 定义只读投影口径；权威事实仍属于 G1–G8。
package analytics

import (
	"agentevalops/go-backend/internal/asset"
	"time"
)

const Version = "stage12.analytics.v1"

type Query struct {
	From        time.Time `json:"from"`
	Until       time.Time `json:"until"`
	Bucket      string    `json:"bucket"`
	Source      string    `json:"source_kind"`
	Subject     string    `json:"subject"`
	Environment string    `json:"environment"`
	RunMode     string    `json:"run_mode"`
	Metric      string    `json:"metric"`
	Evaluator   string    `json:"evaluator"`
	Rule        string    `json:"rule"`
}

func (q Query) Validate() error {
	if q.From.IsZero() || !q.Until.After(q.From) || q.Until.Sub(q.From) > 90*24*time.Hour || (q.Bucket != "hour" && q.Bucket != "day") {
		return asset.ErrInvalid
	}
	for _, v := range []string{q.Subject, q.Environment, q.RunMode, q.Metric, q.Evaluator, q.Rule} {
		if len(v) > 255 {
			return asset.ErrInvalid
		}
	}
	return nil
}

type Response struct {
	ProjectionVersion string           `json:"projection_version"`
	ProjectID         string           `json:"project_id"`
	AsOf              time.Time        `json:"as_of"`
	Timezone          string           `json:"timezone"`
	Query             Query            `json:"query"`
	Unit              string           `json:"unit"`
	Denominator       string           `json:"denominator"`
	Consistency       string           `json:"consistency"`
	Rows              []map[string]any `json:"rows"`
}

func Ratio(n, d int) *float64 {
	if d == 0 {
		return nil
	}
	v := float64(n) / float64(d)
	return &v
}
