// Package asset 提供目录共享的身份和不可变字节值，不拥有业务工作流。
package asset

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"math/big"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

const CatalogAlgorithm = "catalog-json-v1"
const LegacyAlgorithm = "legacy-python-json-v1"

// JSON 保存 canonical bytes；调用方不能通过 map/slice 修改已冻结值。
type JSON struct{ canonical string }

func ParseJSON(raw []byte) (JSON, error) {
	if !utf8.Valid(raw) {
		return JSON{}, fmt.Errorf("JSON 必须是 UTF-8")
	}
	// encoding/json 会替换孤立 surrogate；显式拒绝，避免 silently changing identity。
	if err := validateSurrogates(raw); err != nil {
		return JSON{}, err
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	v, err := readJSON(d)
	if err != nil {
		return JSON{}, err
	}
	if _, err = d.Token(); err != io.EOF {
		return JSON{}, fmt.Errorf("JSON 尾部存在多余内容")
	}
	var b strings.Builder
	if err = writeJSON(&b, v); err != nil {
		return JSON{}, err
	}
	return JSON{b.String()}, nil
}

func Freeze(v any) (JSON, error) {
	raw, err := json.Marshal(v)
	if err != nil {
		return JSON{}, err
	}
	return ParseJSON(raw)
}
func (v JSON) Bytes() []byte { return []byte(v.String()) }
func (v JSON) String() string {
	if v.canonical == "" {
		return "null"
	}
	return v.canonical
}
func (v JSON) Digest() string               { sum := sha256.Sum256(v.Bytes()); return hex.EncodeToString(sum[:]) }
func (v JSON) MarshalJSON() ([]byte, error) { return v.Bytes(), nil }
func (v *JSON) UnmarshalJSON(raw []byte) error {
	next, err := ParseJSON(raw)
	if err == nil {
		*v = next
	}
	return err
}
func (v JSON) Decode(dst any) error {
	d := json.NewDecoder(bytes.NewReader(v.Bytes()))
	d.UseNumber()
	d.DisallowUnknownFields()
	return d.Decode(dst)
}

func readJSON(d *json.Decoder) (any, error) {
	t, err := d.Token()
	if err != nil {
		return nil, err
	}
	delim, ok := t.(json.Delim)
	if !ok {
		return t, nil
	}
	switch delim {
	case '{':
		m := map[string]any{}
		for d.More() {
			key, err := d.Token()
			if err != nil {
				return nil, err
			}
			k, ok := key.(string)
			if !ok {
				return nil, fmt.Errorf("非法 object key")
			}
			if _, exists := m[k]; exists {
				return nil, fmt.Errorf("重复 JSON key: %s", k)
			}
			value, err := readJSON(d)
			if err != nil {
				return nil, err
			}
			m[k] = value
		}
		if _, err := d.Token(); err != nil {
			return nil, err
		}
		return m, nil
	case '[':
		a := []any{}
		for d.More() {
			v, err := readJSON(d)
			if err != nil {
				return nil, err
			}
			a = append(a, v)
		}
		if _, err := d.Token(); err != nil {
			return nil, err
		}
		return a, nil
	default:
		return nil, fmt.Errorf("非法 JSON delimiter")
	}
}

func writeJSON(b *strings.Builder, v any) error {
	switch x := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		b.WriteString(strconv.FormatBool(x))
	case string:
		writeString(b, x)
	case json.Number:
		n, err := canonicalNumber(string(x))
		if err != nil {
			return err
		}
		b.WriteString(n)
	case []any:
		b.WriteByte('[')
		for i, item := range x {
			if i > 0 {
				b.WriteByte(',')
			}
			if err := writeJSON(b, item); err != nil {
				return err
			}
		}
		b.WriteByte(']')
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
			writeString(b, k)
			b.WriteByte(':')
			if err := writeJSON(b, x[k]); err != nil {
				return err
			}
		}
		b.WriteByte('}')
	default:
		return fmt.Errorf("不支持的 JSON 类型 %T", v)
	}
	return nil
}

func writeString(b *strings.Builder, s string) {
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
			if r < 32 {
				fmt.Fprintf(b, `\u%04x`, r)
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
}

func canonicalNumber(s string) (string, error) {
	if !strings.ContainsAny(s, ".eE") {
		n, ok := new(big.Int).SetString(s, 10)
		if !ok {
			return "", fmt.Errorf("非法整数")
		}
		return n.String(), nil
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil || math.IsInf(f, 0) || math.IsNaN(f) {
		return "", fmt.Errorf("非 finite binary64")
	}
	if f == 0 {
		if math.Signbit(f) {
			return "-0.0", nil
		}
		return "0.0", nil
	}
	scientific := strconv.FormatFloat(f, 'e', -1, 64)
	split := strings.Split(scientific, "e")
	exp, _ := strconv.Atoi(split[1])
	if exp >= -4 && exp < 16 {
		fixed := strconv.FormatFloat(f, 'f', -1, 64)
		if !strings.Contains(fixed, ".") {
			fixed += ".0"
		}
		return fixed, nil
	}
	return fmt.Sprintf("%se%+03d", split[0], exp), nil
}

func validateSurrogates(raw []byte) error {
	quoted := false
	for i := 0; i < len(raw); i++ {
		if raw[i] == '"' {
			quoted = !quoted
			continue
		}
		if !quoted || raw[i] != '\\' {
			continue
		}
		i++
		if i >= len(raw) {
			break
		}
		if raw[i] != 'u' || i+4 >= len(raw) {
			continue
		}
		n, err := strconv.ParseUint(string(raw[i+1:i+5]), 16, 16)
		if err != nil {
			continue
		}
		i += 4
		if n >= 0xdc00 && n <= 0xdfff {
			return fmt.Errorf("孤立 low surrogate")
		}
		if n < 0xd800 || n > 0xdbff {
			continue
		}
		if i+6 >= len(raw) || raw[i+1] != '\\' || raw[i+2] != 'u' {
			return fmt.Errorf("孤立 high surrogate")
		}
		low, err := strconv.ParseUint(string(raw[i+3:i+7]), 16, 16)
		if err != nil || low < 0xdc00 || low > 0xdfff {
			return fmt.Errorf("非法 surrogate pair")
		}
		i += 6
	}
	return nil
}
