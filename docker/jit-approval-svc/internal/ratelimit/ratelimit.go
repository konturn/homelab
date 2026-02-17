package ratelimit

import (
	"fmt"
	"os"
	"strconv"
	"sync"
	"time"
)

// Limiter tracks request counts per resource+requester within a sliding window.
type Limiter struct {
	mu       sync.Mutex
	window   time.Duration
	maxReqs  int
	requests map[string][]time.Time
}

// NewFromEnv creates a Limiter from environment variables:
//   - JIT_RATE_LIMIT_MAX (default 50)
//   - JIT_RATE_LIMIT_WINDOW_MIN (default 15)
func NewFromEnv() *Limiter {
	maxReqs := 50
	windowMin := 15
	if v := os.Getenv("JIT_RATE_LIMIT_MAX"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			maxReqs = n
		}
	}
	if v := os.Getenv("JIT_RATE_LIMIT_WINDOW_MIN"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			windowMin = n
		}
	}
	return New(maxReqs, time.Duration(windowMin)*time.Minute)
}

// New creates a Limiter that allows maxReqs requests per window per key.
func New(maxReqs int, window time.Duration) *Limiter {
	return &Limiter{
		window:   window,
		maxReqs:  maxReqs,
		requests: make(map[string][]time.Time),
	}
}

// Allow checks if a request for the given resource+requester is allowed.
// If allowed, it records the request and returns true.
// If rate limited, it returns false and the duration until the next slot opens.
func (l *Limiter) Allow(resource, requester string) (bool, time.Duration) {
	l.mu.Lock()
	defer l.mu.Unlock()

	key := resource + ":" + requester
	now := time.Now()
	cutoff := now.Add(-l.window)

	// Prune expired entries
	timestamps := l.requests[key]
	valid := timestamps[:0]
	for _, t := range timestamps {
		if t.After(cutoff) {
			valid = append(valid, t)
		}
	}

	if len(valid) >= l.maxReqs {
		// Return time until oldest entry expires
		retryAfter := valid[0].Add(l.window).Sub(now)
		l.requests[key] = valid
		return false, retryAfter
	}

	l.requests[key] = append(valid, now)
	return true, 0
}

// Message returns a human-readable rate limit error message.
func Message(resource, requester string, retryAfter time.Duration) string {
	secs := int(retryAfter.Seconds()) + 1
	return fmt.Sprintf("rate limited: too many requests for resource %q by %q, retry in %ds", resource, requester, secs)
}
