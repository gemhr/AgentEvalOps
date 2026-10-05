package cutover

import (
	"agentevalops/go-backend/internal/buildinfo"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

type Artifact struct {
	SHA     string `json:"sha256"`
	Version struct {
		SHA       string            `json:"git_sha"`
		Dirty     string            `json:"dirty"`
		Schema    string            `json:"schema_head"`
		Binary    string            `json:"binary"`
		Contracts map[string]string `json:"contracts"`
	} `json:"version"`
}
type Manifest struct {
	SHA      string              `json:"git_sha"`
	Dirty    string              `json:"dirty"`
	Schema   string              `json:"schema_head"`
	Frontend string              `json:"frontend_build_id"`
	Binaries map[string]Artifact `json:"binaries"`
	Sources  map[string]string   `json:"source_sha256"`
}

// VerifyRelease 比较实际文件；不执行 manifest 指定的程序或命令。
func VerifyRelease(path, root, expected string, controlled bool) []Check {
	out := []Check{}
	add := func(name string, ok bool) {
		status := "PASS"
		if !ok {
			status = "BLOCKED"
		}
		out = append(out, Check{name, status})
	}
	raw, err := os.ReadFile(path)
	add("release_manifest_binding", err == nil && Digest(raw) == expected)
	var m Manifest
	if err != nil || json.Unmarshal(raw, &m) != nil {
		return append(out, Check{"release_manifest", "NOT_VERIFIED"})
	}
	add("release_identity", len(m.SHA) == 40 && m.Schema == Schema && m.Frontend != "")
	add("artifact_cleanliness", m.Dirty == "false" || controlled && m.Dirty == "true")
	git := func(args ...string) (string, error) {
		cmd := exec.Command("git", args...)
		cmd.Dir = root
		b, e := cmd.Output()
		return strings.TrimSpace(string(b)), e
	}
	head, headErr := git("rev-parse", "HEAD")
	add("current_revision", headErr == nil && head == m.SHA)
	status, statusErr := git("status", "--porcelain")
	add("current_cleanliness", statusErr == nil && (controlled || status == "" && m.Dirty == "false"))
	files, filesErr := git("ls-files", "--cached", "--others", "--exclude-standard", "--", "go-backend", "frontend", "backend/migrations", "scripts/release-g10a.ps1", "scripts/g10a-inventory.py", "docker-compose.go-rehearsal.yml", ".dockerignore", ".github/workflows/go-product-release.yml")
	complete := filesErr == nil
	for _, p := range strings.Split(files, "\n") {
		if p != "" {
			if _, ok := m.Sources[p]; !ok {
				complete = false
			}
		}
	}
	add("source_inventory_complete", complete)
	contracts := buildinfo.Version("")["contracts"].(map[string]string)
	for _, name := range []string{"api", "worker", "evalgate", "cutoverctl"} {
		a, ok := m.Binaries[name]
		p := filepath.Join(filepath.Dir(path), name)
		if _, e := os.Stat(p); e != nil {
			p += ".exe"
		}
		b, e := os.ReadFile(p)
		bound := len(a.Version.Contracts) == len(contracts)
		for k, v := range contracts {
			bound = bound && a.Version.Contracts[k] == v
		}
		add("artifact:"+name, ok && e == nil && Digest(b) == a.SHA && a.Version.SHA == m.SHA && a.Version.Dirty == m.Dirty && a.Version.Schema == m.Schema && a.Version.Binary == name && bound)
	}
	add("source_inventory", len(m.Sources) > 0)
	for p, d := range m.Sources {
		clean := filepath.Clean(filepath.FromSlash(p))
		safe := !filepath.IsAbs(clean) && clean != ".." && !strings.HasPrefix(clean, ".."+string(filepath.Separator))
		if !safe {
			add("source_binding", false)
			break
		}
		b, e := os.ReadFile(filepath.Join(root, clean))
		if e != nil || Digest(b) != d {
			add("source_binding", false)
			break
		}
	}
	b, e := os.ReadFile(filepath.Join(root, "frontend", ".next", "BUILD_ID"))
	add("frontend_build", e == nil && strings.TrimSpace(string(b)) == m.Frontend)
	return out
}
