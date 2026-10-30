package main

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/postgres"
	"context"
	"strings"
	"testing"
)

func TestEmptyWriterIdentityFailsClosed(t *testing.T) {
	i := postgres.EmptyWriterIdentity{TaskID: strings.Repeat("a", 32), OperationID: asset.NewID(), Host: "127.0.0.1", Port: 55435,
		Database: "ci_evaluation_" + strings.Repeat("a", 32), DatabaseOID: 1, Owner: "postgres", SystemIdentifier: "1234567890123456789",
		ContainerID: strings.Repeat("b", 64), Volume: "ci-e2e-eopg-" + strings.Repeat("a", 32) + "-data", MigrationDigest: strings.Repeat("c", 64), CodeDigest: strings.Repeat("d", 64)}
	if i.Validate() != nil {
		t.Fatal("valid exact identity rejected")
	}
	for _, change := range []func(*postgres.EmptyWriterIdentity){func(v *postgres.EmptyWriterIdentity) { v.TaskID = "*" }, func(v *postgres.EmptyWriterIdentity) { v.Database = "postgres" }, func(v *postgres.EmptyWriterIdentity) { v.Host = "192.0.2.1" }, func(v *postgres.EmptyWriterIdentity) { v.Volume = "old-volume" }} {
		bad := i
		change(&bad)
		if bad.Validate() == nil {
			t.Fatal("invalid identity accepted")
		}
	}
	if verifyEmptyDocker(context.Background(), i) {
		t.Fatal("invented Docker identity accepted")
	}
	if initializeEmptyWriter(false, true, "", "", "") == 0 {
		t.Fatal("uncontrolled command accepted")
	}
}
