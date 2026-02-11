package ratelimit

import (
	"testing"
	"time"
)

func TestAllow(t *testing.T) {
	l := New(3, 1*time.Second)

	// First 3 should be allowed
	for i := 0; i < 3; i++ {
		ok, _ := l.Allow("grafana", "prometheus")
		if !ok {
			t.Fatalf("request %d should be allowed", i+1)
		}
	}

	// 4th should be denied
	ok, retryAfter := l.Allow("grafana", "prometheus")
	if ok {
		t.Fatal("4th request should be rate limited")
	}
	if retryAfter <= 0 || retryAfter > 1*time.Second {
		t.Fatalf("retryAfter should be between 0 and 1s, got %v", retryAfter)
	}

	// Different resource should be allowed
	ok, _ = l.Allow("radarr", "prometheus")
	if !ok {
		t.Fatal("different resource should be allowed")
	}

	// Different requester should be allowed
	ok, _ = l.Allow("grafana", "other-agent")
	if !ok {
		t.Fatal("different requester should be allowed")
	}
}

func TestWindowExpiry(t *testing.T) {
	l := New(2, 50*time.Millisecond)

	l.Allow("res", "req")
	l.Allow("res", "req")

	ok, _ := l.Allow("res", "req")
	if ok {
		t.Fatal("should be rate limited")
	}

	time.Sleep(60 * time.Millisecond)

	ok, _ = l.Allow("res", "req")
	if !ok {
		t.Fatal("should be allowed after window expires")
	}
}

func TestMessage(t *testing.T) {
	msg := Message("grafana", "prometheus", 30*time.Second)
	if msg == "" {
		t.Fatal("message should not be empty")
	}
}
