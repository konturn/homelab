package config

import (
	"encoding/hex"
	"strings"
	"testing"
)

func hashHex(key string) string {
	h := HashKey(key)
	return hex.EncodeToString(h[:])
}

func TestDefaultApprovalMatrix(t *testing.T) {
	m := DefaultApprovalMatrix()
	if err := ValidateApprovalMatrix(m); err != nil {
		t.Fatal(err)
	}
	want := map[string][MaxTier]Decision{
		"openclaw-noah": {DecisionAuto, DecisionAutoFYI, DecisionApprove},
		"openclaw-ops":  {DecisionAuto, DecisionApprove, DecisionDeny},
	}
	for name, cells := range want {
		for i, d := range cells {
			if got := m[name][i+1]; got != d {
				t.Errorf("%s tier %d = %q, want %q", name, i+1, got, d)
			}
		}
	}
}

func TestParseApprovalMatrix(t *testing.T) {
	m, err := ParseApprovalMatrix(" openclaw-noah=auto,auto-fyi,approve ; openclaw-ops=auto,approve,deny ")
	if err != nil {
		t.Fatal(err)
	}
	if m["openclaw-noah"][2] != DecisionAutoFYI || m["openclaw-ops"][3] != DecisionDeny {
		t.Fatalf("parsed %+v", m)
	}
	for _, bad := range []string{
		"", ";", "openclaw-noah", "openclaw-noah=auto,auto", "openclaw-noah=auto,auto,deny,deny",
		"openclaw-noah=auto,yes,deny", "Openclaw-Noah=auto,auto,deny", "a=auto,auto,deny;a=auto,auto,deny",
		"=auto,auto,deny",
	} {
		if _, err := ParseApprovalMatrix(bad); err == nil {
			t.Errorf("ParseApprovalMatrix(%q): expected an error", bad)
		}
	}
}

// FuzzApprovalMatrix: whatever JIT_APPROVAL_MATRIX says, a matrix that
// passes validation never mints tier 3 without approval, and DecisionFor
// never does either, even for a hand-built matrix that skipped validation.
func FuzzApprovalMatrix(f *testing.F) {
	f.Add("openclaw-noah=auto,auto-fyi,approve;openclaw-ops=auto,approve,deny", "openclaw-ops", 3)
	f.Add("x=auto,auto,auto", "x", 3)
	f.Add("x=auto,auto,auto-fyi", "x", 3)
	f.Add("x=deny,deny,deny", "y", 1)
	f.Fuzz(func(t *testing.T, spec, requester string, tier int) {
		m, err := ParseApprovalMatrix(spec)
		if err != nil {
			return
		}
		valid := ValidateApprovalMatrix(m) == nil
		if valid && m[requester] != nil && m[requester][MaxTier].Mints() {
			t.Fatalf("validated matrix %q mints tier %d for %s", spec, MaxTier, requester)
		}
		cfg := &Config{ApprovalMatrix: m}
		d := cfg.DecisionFor(requester, tier)
		if tier >= MaxTier && d.Mints() {
			t.Fatalf("DecisionFor(%q, %d) = %q", requester, tier, d)
		}
		if _, ok := m[requester]; !ok && d != DecisionDeny {
			t.Fatalf("unknown requester %q got %q", requester, d)
		}
		if tier < 1 || tier > MaxTier {
			if d != DecisionDeny {
				t.Fatalf("unknown tier %d got %q", tier, d)
			}
		}
	})
}

func TestDecisionForUnknownIsDeny(t *testing.T) {
	cfg := &Config{ApprovalMatrix: DefaultApprovalMatrix()}
	for _, r := range []string{"", "unknown", "openclaw-noah ", "OPENCLAW-NOAH", "openclaw"} {
		for tier := -1; tier <= 5; tier++ {
			if d := cfg.DecisionFor(r, tier); d != DecisionDeny {
				t.Errorf("DecisionFor(%q, %d) = %q, want deny", r, tier, d)
			}
		}
	}
}

func TestParseRequesterKeys(t *testing.T) {
	keys, err := ParseRequesterKeys("openclaw-noah:" + hashHex("a") + ", openclaw-ops:" + strings.ToUpper(hashHex("b")))
	if err != nil {
		t.Fatal(err)
	}
	if len(keys) != 2 || keys[0].Name != "openclaw-noah" || keys[1].Hash != HashKey("b") {
		t.Fatalf("parsed %+v", keys)
	}
	if keys, err := ParseRequesterKeys(""); err != nil || len(keys) != 0 {
		t.Fatalf("empty: %v %v", keys, err)
	}
	for _, bad := range []string{
		"openclaw-noah", "openclaw-noah:abc", "openclaw-noah:" + hashHex("a") + "00",
		"Noah:" + hashHex("a"), ":" + hashHex("a"),
		"a:" + hashHex("x") + ",a:" + hashHex("y"),
		"a:" + hashHex("x") + ",b:" + hashHex("x"),
	} {
		if _, err := ParseRequesterKeys(bad); err == nil {
			t.Errorf("ParseRequesterKeys(%q): expected an error", bad)
		} else if strings.Contains(err.Error(), hashHex("a")) || strings.Contains(err.Error(), hashHex("x")) {
			t.Errorf("error repeats the hash: %v", err)
		}
	}
}

func TestAuthenticate(t *testing.T) {
	cfg := &Config{
		RequesterKeys: []RequesterKey{
			{Name: "openclaw-noah", Hash: HashKey("noah-key")},
			{Name: "openclaw-ops", Hash: HashKey("ops-key")},
		},
	}
	for key, want := range map[string]string{
		"noah-key": "openclaw-noah",
		"ops-key":  "openclaw-ops",
	} {
		if got, ok := cfg.Authenticate(key); !ok || got != want {
			t.Errorf("Authenticate(%q) = %q,%v want %q", key, got, ok, want)
		}
	}
	for _, key := range []string{"", "noah-key ", "NOAH-KEY", "ops", "x", hashHex("noah-key"), "shared-key"} {
		if got, ok := cfg.Authenticate(key); ok {
			t.Errorf("Authenticate(%q) = %q, want failure", key, got)
		}
	}
}
