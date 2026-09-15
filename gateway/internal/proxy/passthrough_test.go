package proxy

import (
	"path/filepath"
	"testing"

	"piso/gateway/internal/store"
)

// passthroughHandler builds a Handler with a real store so hosts can be
// classified by the configured rules/domains/exceptions (CCT-style
// selective interception decision).
func passthroughHandler(t *testing.T) (*Handler, *store.Store) {
	t.Helper()
	dir := t.TempDir()
	st, err := store.New(filepath.Join(dir, "state.json"), filepath.Join(dir, "log.jsonl"), filepath.Join(dir, "patterns.json"), filepath.Join(dir, "activities.db"), 20)
	if err != nil {
		t.Fatal(err)
	}
	return &Handler{Store: st}, st
}

func TestHostNeedsInterceptionEmptyStore(t *testing.T) {
	h, _ := passthroughHandler(t)
	if h.hostNeedsInterception("code.example.com") {
		t.Fatal("empty store must not require interception")
	}
}

func TestHostNeedsInterceptionByRuleHost(t *testing.T) {
	h, st := passthroughHandler(t)
	if err := st.AddRule(store.RuleRec{
		ID: "r1", SecretID: "s1", Host: "code.example.com", Placeholder: "piso_gh_x",
	}); err != nil {
		t.Fatal(err)
	}
	if !h.hostNeedsInterception("code.example.com") {
		t.Fatal("host with a rule must be intercepted")
	}
	if h.hostNeedsInterception("npmjs.org") {
		t.Fatal("unruled host must pass through")
	}
}

func TestHostNeedsInterceptionWildcardRule(t *testing.T) {
	h, st := passthroughHandler(t)
	if err := st.AddRule(store.RuleRec{
		ID: "r1", SecretID: "s1", Host: "*", Placeholder: "piso_gh_x",
	}); err != nil {
		t.Fatal(err)
	}
	if !h.hostNeedsInterception("anything.example.com") {
		t.Fatal("wildcard rule must intercept every host")
	}
}

func TestHostNeedsInterceptionByDomainPolicy(t *testing.T) {
	h, st := passthroughHandler(t)
	if err := st.UpsertDomain(store.DomainRec{Host: "denied.example.com", Deny: true}); err != nil {
		t.Fatal(err)
	}
	if !h.hostNeedsInterception("denied.example.com") {
		t.Fatal("domain policy host must be intercepted (so it can be blocked)")
	}
	if h.hostNeedsInterception("other.example.com") {
		t.Fatal("host without domain policy must pass through")
	}
}

func TestHostNeedsInterceptionByException(t *testing.T) {
	h, st := passthroughHandler(t)
	if err := st.AddException(store.ExceptionRec{
		ID: "e1", Enabled: true, HostRegex: `^api\.anthropic\.com$`, PatternID: "basic-auth",
	}); err != nil {
		t.Fatal(err)
	}
	if !h.hostNeedsInterception("api.anthropic.com") {
		t.Fatal("host matching an exception HostRegex must be intercepted")
	}
	if h.hostNeedsInterception("api.openai.com") {
		t.Fatal("non-matching host must pass through")
	}
}

func TestHostNeedsInterceptionHostWideException(t *testing.T) {
	h, st := passthroughHandler(t)
	if err := st.AddException(store.ExceptionRec{
		ID: "e1", Enabled: true, PatternID: "jwt",
	}); err != nil {
		t.Fatal(err)
	}
	if !h.hostNeedsInterception("anywhere.example.com") {
		t.Fatal("host-wide exception must intercept every host")
	}
}

func TestHostNeedsInterceptionDisabledExceptionIgnored(t *testing.T) {
	h, st := passthroughHandler(t)
	if err := st.AddException(store.ExceptionRec{
		ID: "e1", Enabled: false, HostRegex: `^api\.anthropic\.com$`, PatternID: "basic-auth",
	}); err != nil {
		t.Fatal(err)
	}
	if h.hostNeedsInterception("api.anthropic.com") {
		t.Fatal("disabled exception must not force interception")
	}
}
