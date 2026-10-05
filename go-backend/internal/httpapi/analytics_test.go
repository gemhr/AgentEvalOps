package httpapi

import (
	"agentevalops/go-backend/internal/analytics"
	"net/url"
	"testing"
	"time"
)

func TestG9QueryAndDenominatorContracts(t *testing.T) {
	now := time.Date(2026, 10, 5, 0, 0, 0, 0, time.UTC)
	for _, raw := range []string{"window=invalid", "window=custom&from=2026-01-01T00%3A00%3A00Z&until=2026-10-01T00%3A00%3A00Z", "bucket=week", "limit=10000", "window=7d&window=24h", "source_kind=ONLINE", "rule=other", "from=garbage"} {
		values, _ := url.ParseQuery(raw)
		if _, e := analyticsQuery(values, "quality", now); e == nil {
			t.Errorf("accepted %s", raw)
		}
	}
	q, e := analyticsQuery(url.Values{"window": {"24h"}}, "quality", now)
	if e != nil || q.Bucket != "hour" || q.Until.Sub(q.From) != 24*time.Hour {
		t.Fatal(q, e)
	}
	if analytics.Ratio(0, 0) != nil || *analytics.Ratio(0, 10) != 0 {
		t.Fatal("zero denominator or zero numerator collapsed")
	}
}

func TestG9ETagPrincipalProjectionIsolation(t *testing.T) {
	pepper := []byte("controlled-only-pepper")
	raw := []byte(`{"decision":"PASS"}`)
	a := projectionETag(pepper, "a", "p", "/gate", raw)
	for _, v := range []string{projectionETag(pepper, "b", "p", "/gate", raw), projectionETag(pepper, "a", "other", "/gate", raw), projectionETag(pepper, "a", "p", "/other", raw), projectionETag(pepper, "a", "p", "/gate", []byte(`{"decision":"FAIL"}`))} {
		if a == v {
			t.Fatal("ETag identity not bound")
		}
	}
	if !etagMatches("W/"+a, a) || etagMatches(`"unrelated"`, a) {
		t.Fatal("conditional comparison")
	}
}
