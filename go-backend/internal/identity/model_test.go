package identity

import (
	"agentevalops/go-backend/internal/asset"
	"context"
	"strings"
	"testing"
	"time"
)

func TestIdentityAndControlledAuth(t *testing.T) {
	a, _ := NewKey()
	b, _ := NewKey()
	pepper := []byte(strings.Repeat("p", 32))
	if len(a) != 47 || a == b || len(Digest(pepper, a)) != 64 || Digest(pepper, a) == Digest([]byte(strings.Repeat("q", 32)), a) {
		t.Fatal("entropy/digest boundary")
	}
	if !ValidCapabilities(All) || ValidCapabilities([]Capability{Read, Read}) || ValidCapabilities([]Capability{"ADMIN"}) {
		t.Fatal("capability validation")
	}
	project := asset.NewID()
	id := CommandID("a", project, "POST /runs", "k")
	if !asset.ValidID(id) || id != CommandID("a", project, "POST /runs", "k") || id == CommandID("b", project, "POST /runs", "k") {
		t.Fatal("command identity")
	}
	auth := DevAuth{Environment: "test", PrincipalID: asset.NewID(), OrganizationID: asset.NewID(), Password: strings.Repeat("d", 20), SigningKey: pepper, Lifetime: time.Minute}
	token, err := auth.Login(auth.Password)
	if err != nil {
		t.Fatal(err)
	}
	p, err := auth.AuthenticateBearer(context.Background(), token)
	if err != nil || p.ID != auth.PrincipalID || len(p.Capabilities) != 0 {
		t.Fatal("principal must get permissions from DB")
	}
	if _, err = auth.AuthenticateBearer(context.Background(), token+"x"); err == nil {
		t.Fatal("tampered bearer")
	}
	auth.Environment = "production"
	if auth.Validate() == nil {
		t.Fatal("production dev auth")
	}
	access := Access{Principal: Principal{ID: "reviewer", Type: "HUMAN", AuthMethod: "TEST", Capabilities: []Capability{Review}}, Scope: asset.Scope{ProjectID: project, OrganizationID: asset.NewID(), Principal: "reviewer"}}
	if access.Review(1).Adjudicate || access.Evaluation(1).Execute || access.Evaluation(1).Create {
		t.Fatal("capability escalation")
	}
}
