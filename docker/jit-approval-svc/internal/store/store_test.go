package store

import (
	"testing"
	"time"
)

func TestGenerateID(t *testing.T) {
	id1 := GenerateID()
	id2 := GenerateID()

	if id1 == id2 {
		t.Errorf("expected unique IDs, got %s and %s", id1, id2)
	}

	if len(id1) < 8 {
		t.Errorf("expected ID length >= 8, got %d: %s", len(id1), id1)
	}

	if id1[:4] != "req-" {
		t.Errorf("expected ID prefix 'req-', got %s", id1[:4])
	}
}

func TestCreateAndGet(t *testing.T) {
	s := New()

	req := s.Create("prometheus", "homeassistant", 1, "Check sensors")
	if req.ID == "" {
		t.Fatal("expected non-empty ID")
	}
	if req.Status != StatusPending {
		t.Errorf("expected status pending, got %s", req.Status)
	}
	if req.Requester != "prometheus" {
		t.Errorf("expected requester prometheus, got %s", req.Requester)
	}

	got := s.Get(req.ID)
	if got == nil {
		t.Fatal("expected to find request")
	}
	if got.ID != req.ID {
		t.Errorf("expected ID %s, got %s", req.ID, got.ID)
	}

	missing := s.Get("req-nonexistent")
	if missing != nil {
		t.Error("expected nil for missing request")
	}
}

func TestApproveAndClaim(t *testing.T) {
	s := New()
	req := s.Create("prometheus", "gitlab", 2, "MR review")

	cred := &Credential{
		Token:    "hvs.test-token",
		LeaseTTL: 30 * time.Minute,
	}

	err := s.Approve(req.ID, cred, 30*time.Minute)
	if err != nil {
		t.Fatalf("approve failed: %v", err)
	}

	got := s.Get(req.ID)
	if got.Status != StatusApproved {
		t.Errorf("expected approved, got %s", got.Status)
	}
	if got.ApprovedAt == nil {
		t.Error("expected ApprovedAt to be set")
	}

	// Claim should return the credential and transition to claimed
	claimed, err := s.Claim(req.ID)
	if err != nil {
		t.Fatalf("claim failed: %v", err)
	}
	if claimed == nil {
		t.Fatal("expected credential from claim")
	}
	if claimed.Token != "hvs.test-token" {
		t.Errorf("expected token hvs.test-token, got %s", claimed.Token)
	}

	got = s.Get(req.ID)
	if got.Status != StatusClaimed {
		t.Errorf("expected claimed, got %s", got.Status)
	}

	// Second claim should return nil (already claimed)
	claimed2, err := s.Claim(req.ID)
	if err != nil {
		t.Fatalf("second claim failed: %v", err)
	}
	if claimed2 != nil {
		t.Error("expected nil on second claim")
	}
}

func TestDeny(t *testing.T) {
	s := New()
	req := s.Create("prometheus", "ssh", 3, "Router access")

	err := s.Deny(req.ID)
	if err != nil {
		t.Fatalf("deny failed: %v", err)
	}

	got := s.Get(req.ID)
	if got.Status != StatusDenied {
		t.Errorf("expected denied, got %s", got.Status)
	}

	// Can't deny again
	err = s.Deny(req.ID)
	if err == nil {
		t.Error("expected error on double deny")
	}
}

func TestTimeout(t *testing.T) {
	s := New()
	req := s.Create("prometheus", "docker", 2, "Check containers")

	err := s.Timeout(req.ID)
	if err != nil {
		t.Fatalf("timeout failed: %v", err)
	}

	got := s.Get(req.ID)
	if got.Status != StatusTimeout {
		t.Errorf("expected timeout, got %s", got.Status)
	}

	// Timeout on already-resolved is no-op
	err = s.Timeout(req.ID)
	if err != nil {
		t.Errorf("expected no-op on second timeout, got: %v", err)
	}
}

func TestApproveNonPending(t *testing.T) {
	s := New()
	req := s.Create("prometheus", "gitlab", 2, "test")
	_ = s.Deny(req.ID)

	err := s.Approve(req.ID, &Credential{Token: "x"}, time.Minute)
	if err == nil {
		t.Error("expected error approving denied request")
	}
}

func TestPendingRequests(t *testing.T) {
	s := New()
	s.Create("prometheus", "res1", 1, "test1")
	req2 := s.Create("prometheus", "res2", 2, "test2")
	s.Create("prometheus", "res3", 1, "test3")

	_ = s.Deny(req2.ID)

	pending := s.PendingRequests()
	if len(pending) != 2 {
		t.Errorf("expected 2 pending, got %d", len(pending))
	}
}

func TestCleanup(t *testing.T) {
	s := New()

	// Create and resolve a request, backdate it
	req := s.Create("prometheus", "test", 1, "test")
	_ = s.Deny(req.ID)

	s.mu.Lock()
	s.requests[req.ID].CreatedAt = time.Now().Add(-2 * time.Hour)
	s.mu.Unlock()

	removed := s.Cleanup(1 * time.Hour)
	if removed != 1 {
		t.Errorf("expected 1 removed, got %d", removed)
	}
	if s.Count() != 0 {
		t.Errorf("expected 0 remaining, got %d", s.Count())
	}
}

func TestCleanupPreservesPending(t *testing.T) {
	s := New()

	req := s.Create("prometheus", "test", 1, "test")
	s.mu.Lock()
	s.requests[req.ID].CreatedAt = time.Now().Add(-2 * time.Hour)
	s.mu.Unlock()

	removed := s.Cleanup(1 * time.Hour)
	if removed != 0 {
		t.Errorf("expected 0 removed (pending preserved), got %d", removed)
	}
}

func TestCount(t *testing.T) {
	s := New()
	if s.Count() != 0 {
		t.Errorf("expected 0, got %d", s.Count())
	}

	s.Create("prometheus", "a", 1, "a")
	s.Create("prometheus", "b", 1, "b")

	if s.Count() != 2 {
		t.Errorf("expected 2, got %d", s.Count())
	}
}
