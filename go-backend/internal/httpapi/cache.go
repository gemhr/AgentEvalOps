package httpapi

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"strings"
)

func immutableRoute(e route) bool {
	if e.Method != "GET" {
		return false
	}
	p := strings.TrimPrefix(e.Path, "/api/v1/projects/{project_id}")
	return strings.HasSuffix(p, "/versions/{version}") || p == "/gates/{id}" || p == "/golden/{id}" || p == "/calibrations/{id}"
}

// 认证/实时授权先执行；private revalidation 防止 revoke 被长期缓存绕过。
func projectionETag(pepper []byte, principal, project, path string, raw []byte) string {
	h := hmac.New(sha256.New, pepper)
	for _, v := range []string{principal, project, path} {
		h.Write([]byte(v))
		h.Write([]byte{0})
	}
	h.Write(raw)
	return `"` + hex.EncodeToString(h.Sum(nil)) + `"`
}
func etagMatches(header, tag string) bool {
	for _, v := range strings.Split(header, ",") {
		v = strings.TrimSpace(v)
		if v == "*" || strings.TrimPrefix(v, "W/") == tag {
			return true
		}
	}
	return false
}
func conditional(w http.ResponseWriter, r *http.Request, tag string) bool {
	w.Header().Set("ETag", tag)
	w.Header().Set("Cache-Control", "private, no-cache")
	w.Header().Add("Vary", "Authorization")
	w.Header().Add("Vary", "X-API-Key")
	if etagMatches(r.Header.Get("If-None-Match"), tag) {
		w.WriteHeader(http.StatusNotModified)
		return true
	}
	return false
}
