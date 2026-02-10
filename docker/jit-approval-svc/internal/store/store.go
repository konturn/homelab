package store

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"sync"
	"time"
)

// Status represents the lifecycle state of an access request.
type Status string

const (
	StatusPending  Status = "pending"
	StatusApproved Status = "approved"
	StatusDenied   Status = "denied"
	StatusTimeout  Status = "timeout"
	StatusClaimed  Status = "claimed"
)

// VaultPathRequest represents a requested Vault path with capabilities.
type VaultPathRequest struct {
	Path         string   `json:"path"`
	Capabilities []string `json:"capabilities"`
}

// Request represents a JIT access request.
type Request struct {
	ID        string    `json:"request_id"`
	Requester string    `json:"requester"`
	Resource  string    `json:"resource"`
	Tier      int       `json:"tier"`
	Reason    string    `json:"reason"`
	Scopes    []string  `json:"scopes,omitempty"`
	Status    Status    `json:"status"`
	CreatedAt time.Time `json:"created_at"`

	// Dynamic Vault backend: requested paths and capabilities
	VaultPaths []VaultPathRequest `json:"vault_paths,omitempty"`

	// Set on approval
	ApprovedAt *time.Time `json:"approved_at,omitempty"`
	TTL        time.Duration `json:"-"`

	// Credential data (only returned once via claim)
	Credential *Credential `json:"-"`

	// SSH host (for display/audit, not enforced by certificate)
	SSHHost string `json:"ssh_host,omitempty"`

	// Telegram tracking
	TelegramMessageID int `json:"-"`
}

// Credential holds the minted credential data.
type Credential struct {
	Token    string            `json:"token,omitempty"`
	LeaseTTL time.Duration    `json:"lease_ttl,omitempty"`
	LeaseID  string            `json:"lease_id,omitempty"`
	Policies []string          `json:"policies,omitempty"`
	Metadata map[string]string `json:"metadata,omitempty"`
}

// Store is an in-memory, thread-safe request store.
type Store struct {
	mu       sync.RWMutex
	requests map[string]*Request
}

// New creates a new request store.
func New() *Store {
	return &Store{
		requests: make(map[string]*Request),
	}
}

// GenerateID creates a unique request ID.
func GenerateID() string {
	b := make([]byte, 6)
	if _, err := rand.Read(b); err != nil {
		// Fallback to timestamp-based ID
		return fmt.Sprintf("req-%d", time.Now().UnixNano())
	}
	return "req-" + hex.EncodeToString(b)
}

// Create stores a new request and returns it.
func (s *Store) Create(requester, resource string, tier int, reason string, scopes []string) *Request {
	req := &Request{
		ID:        GenerateID(),
		Requester: requester,
		Resource:  resource,
		Tier:      tier,
		Reason:    reason,
		Scopes:    scopes,
		Status:    StatusPending,
		CreatedAt: time.Now(),
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	s.requests[req.ID] = req
	return req
}

// Get retrieves a request by ID. Returns nil if not found.
func (s *Store) Get(id string) *Request {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.requests[id]
}

// Approve transitions a request to approved status and attaches a credential.
func (s *Store) Approve(id string, cred *Credential, ttl time.Duration) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	req, ok := s.requests[id]
	if !ok {
		return fmt.Errorf("request not found: %s", id)
	}
	if req.Status != StatusPending {
		return fmt.Errorf("request %s is not pending (status: %s)", id, req.Status)
	}

	now := time.Now()
	req.Status = StatusApproved
	req.ApprovedAt = &now
	req.Credential = cred
	req.TTL = ttl
	return nil
}

// Deny transitions a request to denied status.
func (s *Store) Deny(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	req, ok := s.requests[id]
	if !ok {
		return fmt.Errorf("request not found: %s", id)
	}
	if req.Status != StatusPending {
		return fmt.Errorf("request %s is not pending (status: %s)", id, req.Status)
	}

	req.Status = StatusDenied
	return nil
}

// Claim transitions an approved request to claimed and returns the credential.
// The credential is only returned once.
func (s *Store) Claim(id string) (*Credential, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	req, ok := s.requests[id]
	if !ok {
		return nil, fmt.Errorf("request not found: %s", id)
	}
	if req.Status != StatusApproved {
		return nil, nil // Not ready to claim
	}

	cred := req.Credential
	req.Status = StatusClaimed
	req.Credential = nil // Clear after claim
	return cred, nil
}

// Timeout transitions a request to timeout status.
func (s *Store) Timeout(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	req, ok := s.requests[id]
	if !ok {
		return fmt.Errorf("request not found: %s", id)
	}
	if req.Status != StatusPending {
		return nil // Already resolved, no-op
	}

	req.Status = StatusTimeout
	return nil
}

// SetTelegramMessageID records the Telegram message ID for a request.
func (s *Store) SetTelegramMessageID(id string, msgID int) {
	s.mu.Lock()
	defer s.mu.Unlock()

	if req, ok := s.requests[id]; ok {
		req.TelegramMessageID = msgID
	}
}

// PendingRequests returns all requests in pending status.
func (s *Store) PendingRequests() []*Request {
	s.mu.RLock()
	defer s.mu.RUnlock()

	var pending []*Request
	for _, req := range s.requests {
		if req.Status == StatusPending {
			pending = append(pending, req)
		}
	}
	return pending
}

// Cleanup removes requests older than the given duration.
func (s *Store) Cleanup(maxAge time.Duration) int {
	s.mu.Lock()
	defer s.mu.Unlock()

	cutoff := time.Now().Add(-maxAge)
	removed := 0
	for id, req := range s.requests {
		if req.CreatedAt.Before(cutoff) && req.Status != StatusPending {
			delete(s.requests, id)
			removed++
		}
	}
	return removed
}

// Count returns the total number of requests in the store.
func (s *Store) Count() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.requests)
}
