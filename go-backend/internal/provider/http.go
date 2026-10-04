// Package provider 只拥有外部 HTTP observation，不直接修改数据库。
package provider

import (
	"bytes"
	"crypto/tls"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"time"

	"agentevalops/go-backend/internal/asset"
)

type ErrorKind string

const (
	TransportError     ErrorKind = "TRANSPORT"
	ProtocolError      ErrorKind = "PROTOCOL"
	IdentityMismatch   ErrorKind = "IDENTITY_MISMATCH"
	SchemaMismatch     ErrorKind = "SCHEMA_MISMATCH"
	RemoteTerminal     ErrorKind = "REMOTE_TERMINAL"
	Unavailable        ErrorKind = "PROVIDER_UNAVAILABLE"
	RateLimit          ErrorKind = "PROVIDER_RATE_LIMIT"
	ProviderTimeout    ErrorKind = "PROVIDER_TIMEOUT"
	BudgetExhausted    ErrorKind = "BUDGET_EXHAUSTED"
	InvalidJudgeOutput ErrorKind = "INVALID_JUDGE_OUTPUT"
)

// Error 不包含 URL、credential 或远程响应正文。
type Error struct{ Kind ErrorKind }

func (e *Error) Error() string { return string(e.Kind) }

type HTTPConfig struct {
	MaxIdleConns        int   `json:"max_idle_conns"`
	MaxIdleConnsPerHost int   `json:"max_idle_conns_per_host"`
	IdleMilliseconds    int64 `json:"idle_milliseconds"`
	HeaderMilliseconds  int64 `json:"header_milliseconds"`
	DialMilliseconds    int64 `json:"dial_milliseconds"`
	TLSMilliseconds     int64 `json:"tls_milliseconds"`
}

func DefaultHTTPConfig() HTTPConfig { return HTTPConfig{32, 8, 90000, 30000, 5000, 5000} }
func (c HTTPConfig) Validate() error {
	if c.MaxIdleConns < 1 || c.MaxIdleConnsPerHost < 1 || c.MaxIdleConnsPerHost > c.MaxIdleConns || c.IdleMilliseconds <= 0 || c.HeaderMilliseconds <= 0 || c.DialMilliseconds <= 0 || c.TLSMilliseconds <= 0 {
		return asset.ErrInvalid
	}
	return nil
}
func newClient(c HTTPConfig) (*http.Client, error) {
	if err := c.Validate(); err != nil {
		return nil, err
	}
	t := &http.Transport{Proxy: nil, DialContext: (&net.Dialer{Timeout: time.Duration(c.DialMilliseconds) * time.Millisecond, KeepAlive: 30 * time.Second}).DialContext,
		MaxIdleConns: c.MaxIdleConns, MaxIdleConnsPerHost: c.MaxIdleConnsPerHost, IdleConnTimeout: time.Duration(c.IdleMilliseconds) * time.Millisecond,
		ResponseHeaderTimeout: time.Duration(c.HeaderMilliseconds) * time.Millisecond, TLSHandshakeTimeout: time.Duration(c.TLSMilliseconds) * time.Millisecond,
		TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12}, TLSNextProto: map[string]func(string, *tls.Conn) http.RoundTripper{}, ForceAttemptHTTP2: false}
	return &http.Client{Transport: t, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, nil
}
func endpoint(s string) error {
	u, e := url.Parse(s)
	if e != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return asset.ErrInvalid
	}
	return nil
}
func boundedBody(r io.Reader, limit int) ([]byte, error) {
	if limit < 1 {
		return nil, &Error{Kind: ProtocolError}
	}
	b, e := io.ReadAll(io.LimitReader(r, int64(limit)+1))
	if e != nil {
		return nil, e
	}
	if len(b) > limit {
		return nil, &Error{Kind: ProtocolError}
	}
	return b, nil
}
func strict(raw []byte, dst any) error {
	j, e := asset.ParseJSON(raw)
	if e != nil {
		return &Error{Kind: ProtocolError}
	}
	if e = j.Decode(dst); e != nil {
		return &Error{Kind: ProtocolError}
	}
	return nil
}
func required(raw []byte, keys ...string) error {
	var m map[string]asset.JSON
	if strict(raw, &m) != nil || m == nil {
		return &Error{Kind: ProtocolError}
	}
	for _, k := range keys {
		if _, ok := m[k]; !ok {
			return &Error{Kind: ProtocolError}
		}
	}
	return nil
}
func post(ctxReq *http.Request, body []byte) { // 禁止 stdlib 在失效连接上重放可重建 body。
	ctxReq.Body = io.NopCloser(bytes.NewReader(body))
	ctxReq.ContentLength = int64(len(body))
	ctxReq.GetBody = nil
	ctxReq.Header.Set("Content-Type", "application/json")
}
func safeCommand(code, reason string) error {
	return fmt.Errorf("provider durable command: %s/%s", code, reason)
}
