// stage13-probe 通过正式 provider 执行一次冻结请求，用于独立进程协议验收。
package main

import (
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/provider"
	"context"
	"encoding/json"
	"fmt"
	"os"
)

func main() {
	if len(os.Args) == 3 && os.Args[1] == "--policy" {
		raw, err := os.ReadFile(os.Args[2])
		if err != nil {
			panic(err)
		}
		frozen, err := asset.ParseJSON(raw)
		if err != nil {
			panic(err)
		}
		var document provider.Stage13PolicyDocument
		if err = frozen.Decode(&document); err != nil {
			panic(err)
		}
		_ = json.NewEncoder(os.Stdout).Encode(provider.CompareStage13Models(document))
		return
	}
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "request file required")
		os.Exit(2)
	}
	raw, err := os.ReadFile(os.Args[1])
	if err != nil {
		panic(err)
	}
	var document struct {
		Config  provider.Stage13Config `json:"config"`
		Request ev.Request             `json:"request"`
	}
	frozen, err := asset.ParseJSON(raw)
	if err != nil {
		panic(err)
	}
	if err = frozen.Decode(&document); err != nil {
		panic(err)
	}
	target, err := provider.NewStage13Target(document.Config)
	if err != nil {
		panic(err)
	}
	defer target.Close()
	out, err := target.Execute(context.Background(), ev.Scope{Scope: asset.Scope{ProjectID: document.Request.Case.Identity.ProjectID}}, document.Request)
	if err != nil {
		panic(err)
	}
	_ = json.NewEncoder(os.Stdout).Encode(out)
}
