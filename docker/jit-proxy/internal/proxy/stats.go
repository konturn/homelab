package proxy

import (
	"fmt"
	"sync"
	"time"

	"github.com/nkontur/jit-proxy/internal/logger"
)

// Stats counts calls since the proxy started, for GET /summary (the daily
// digest reads it; the audit log in Loki is the full record).
type Stats struct {
	mu         sync.Mutex
	since      time.Time
	calls      map[string]int // resource/template/decision
	denials    map[string]int // reason
	redactions map[string]int // resource
	lifecycle  map[string]int // grant_registered, grant_revoked, ...
}

// NewStats returns empty counters.
func NewStats() *Stats {
	return &Stats{since: time.Now(), calls: map[string]int{}, denials: map[string]int{},
		redactions: map[string]int{}, lifecycle: map[string]int{}}
}

// Record counts one audit line.
func (s *Stats) Record(f logger.Fields) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	key := fmt.Sprintf("%v/%v/%v", f["resource"], f["template"], f["decision"])
	s.calls[key]++
	if f["decision"] != "allow" {
		s.denials[fmt.Sprint(f["reason"])]++
	}
	if n, ok := f["redactions"].(int); ok && n > 0 {
		s.redactions[fmt.Sprint(f["resource"])] += n
	}
}

// Lifecycle counts a grant lifecycle event.
func (s *Stats) Lifecycle(event string) {
	if s == nil {
		return
	}
	s.mu.Lock()
	s.lifecycle[event]++
	s.mu.Unlock()
}

// Summary returns the counters (no secrets, no paths).
func (s *Stats) Summary(active map[string]int) map[string]interface{} {
	s.mu.Lock()
	defer s.mu.Unlock()
	cp := func(m map[string]int) map[string]int {
		out := make(map[string]int, len(m))
		for k, v := range m {
			out[k] = v
		}
		return out
	}
	return map[string]interface{}{
		"since":         s.since.UTC().Format(time.RFC3339),
		"calls":         cp(s.calls),
		"denials":       cp(s.denials),
		"redactions":    cp(s.redactions),
		"grants":        cp(s.lifecycle),
		"active_grants": active,
	}
}
