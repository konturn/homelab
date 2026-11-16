// jit-proxy: the JIT credential proxy (docs/design/jit-credential-proxy.md).
//
// The broker (jit-approval-svc) issues short-lived, scoped grants for the
// services that have no native short-lived credential and registers them
// here over the control listener. The agent calls /r/<resource>/<path> on
// the data listener with its grant; the proxy checks the grant and the
// resource's rules, injects the real credential (read from Vault with the
// proxy's own AppRole) and scrubs the response.
package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"sort"
	"strings"
	"syscall"
	"time"

	"github.com/nkontur/jit-proxy/internal/ctl"
	"github.com/nkontur/jit-proxy/internal/grants"
	"github.com/nkontur/jit-proxy/internal/logger"
	"github.com/nkontur/jit-proxy/internal/matrix"
	"github.com/nkontur/jit-proxy/internal/proxy"
	"github.com/nkontur/jit-proxy/internal/redact"
	"github.com/nkontur/jit-proxy/internal/secrets"
	"github.com/nkontur/jit-proxy/internal/templates"
	"github.com/nkontur/jit-proxy/internal/upstream"
)

//go:embed templates/*.yml
var templateFS embed.FS

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func main() {
	healthcheck := flag.Bool("healthcheck", false, "probe the data listener's /health and exit 0/1 (Docker healthcheck)")
	flag.Parse()

	dataAddr := env("DATA_LISTEN", "127.0.0.1:8090")
	ctlAddr := env("CTL_LISTEN", "127.0.0.1:8091")

	if *healthcheck {
		c := &http.Client{Timeout: 3 * time.Second}
		resp, err := c.Get("http://" + dataAddr + "/health")
		if err != nil || resp.StatusCode != http.StatusOK {
			os.Exit(1)
		}
		os.Exit(0)
	}

	scrub := redact.New()
	logger.SetScrubber(scrub.KnownOnly)

	set, err := templates.Load(templateFS, "templates")
	if err != nil {
		logger.Fatal("templates_invalid", logger.Fields{"error": err.Error()})
	}

	ctlKey := os.Getenv("CTL_KEY")
	if len(ctlKey) < 32 {
		logger.Fatal("config_invalid", logger.Fields{"error": "CTL_KEY must be at least 32 characters"})
	}
	roleID, secretID := os.Getenv("VAULT_ROLE_ID"), os.Getenv("VAULT_SECRET_ID")
	if roleID == "" || secretID == "" {
		logger.Fatal("config_invalid", logger.Fields{"error": "VAULT_ROLE_ID and VAULT_SECRET_ID are required"})
	}
	// The proxy's own credentials are never logged either.
	scrub.Set("self", ctlKey, secretID)

	m := matrix.Default()
	if s := os.Getenv("JIT_APPROVAL_MATRIX"); s != "" {
		if m, err = matrix.Parse(s); err != nil {
			logger.Fatal("config_invalid", logger.Fields{"error": err.Error()})
		}
	}

	var clientNet *net.IPNet
	if s := os.Getenv("CLIENT_CIDR"); s != "" {
		if _, clientNet, err = net.ParseCIDR(s); err != nil {
			logger.Fatal("config_invalid", logger.Fields{"error": "CLIENT_CIDR: " + err.Error()})
		}
	}

	client := proxy.NewClient()
	vault := &secrets.Vault{
		Addr:   strings.TrimRight(env("VAULT_ADDR", "https://vault.lab.nkontur.com:8200"), "/"),
		RoleID: roleID, SecretID: secretID,
		Client: &http.Client{Timeout: 10 * time.Second},
	}
	specs := map[string]secrets.Spec{}
	for name, r := range set {
		specs[name] = secrets.Spec{VaultPath: r.Auth.VaultPath, Field: r.Auth.Field, UsernameField: r.Auth.UsernameField}
	}
	cache := secrets.NewCache(vault, specs, func(res string, c secrets.Credential) {
		if len(c.Secret) < redact.MinSecretLen {
			logger.Warn("secret_short", logger.Fields{"resource": res})
		}
		scrub.Set(res, c.Secret)
		logger.Info("credential_loaded", logger.Fields{"resource": res})
	})

	injectors := map[string]upstream.Injector{}
	for name, r := range set {
		name := name
		inj, err := upstream.New(r, func() (secrets.Credential, bool) { return cache.Get(name) }, client,
			func(owner string, v ...string) { scrub.Set(owner, v...) })
		if err != nil {
			logger.Fatal("config_invalid", logger.Fields{"error": err.Error()})
		}
		injectors[name] = inj
	}

	gcfg := grants.DefaultConfig()
	gcfg.Matrix = m
	store := grants.New(gcfg, set)
	stats := proxy.NewStats()

	bootID := newBootID()
	health := func() (bool, map[string]interface{}) {
		loaded := cache.Loaded()
		sort.Strings(loaded)
		var missing []string
		for _, n := range set.Names() {
			if _, ok := cache.Get(n); !ok {
				missing = append(missing, n)
			}
		}
		status := "ok"
		if len(missing) > 0 {
			status = "degraded"
		}
		// Unhealthy (fail closed) until at least one credential is loaded,
		// i.e. while Vault is unreachable. A missing path only degrades.
		return len(loaded) > 0, map[string]interface{}{
			"status": status, "boot_id": bootID, "credentials_loaded": loaded, "credentials_missing": missing,
		}
	}

	px := &proxy.Proxy{
		Set: set, Grants: store, Injectors: injectors, Creds: cache, Scrub: scrub,
		Client: client, ClientNet: clientNet, Health: health, Stats: stats,
		Inflight: make(chan struct{}, 16),
	}
	ch := &ctl.Handler{
		KeyHash: sha256.Sum256([]byte(ctlKey)), Grants: store, Set: set, BootID: bootID,
		Health: health, OnEvent: stats.Lifecycle,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	// Credentials: load at start (retrying until Vault answers; nothing is
	// served for a resource without its credential), refresh every 15
	// minutes and on SIGHUP.
	go func() {
		backoff := 5 * time.Second
		for {
			if err := cache.LoadAll(ctx); err != nil {
				logger.Error("credential_load_failed", logger.Fields{"error": scrub.KnownOnly(err.Error())})
			} else {
				break
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(backoff):
			}
			if backoff < time.Minute {
				backoff *= 2
			}
		}
	}()
	hup := make(chan os.Signal, 1)
	signal.Notify(hup, syscall.SIGHUP)
	go func() {
		tick := time.NewTicker(15 * time.Minute)
		defer tick.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-tick.C:
			case <-hup:
				logger.Info("credential_reload_requested", nil)
			}
			if err := cache.LoadAll(ctx); err != nil {
				logger.Error("credential_load_failed", logger.Fields{"error": scrub.KnownOnly(err.Error())})
			}
		}
	}()
	go func() {
		tick := time.NewTicker(30 * time.Second)
		defer tick.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-tick.C:
				for _, g := range store.Sweep() {
					logger.Info("grant_expired", logger.Fields{"grant_id": g.ID, "resource": g.Resource})
					stats.Lifecycle("grant_expired")
				}
			}
		}
	}()

	dataSrv := &http.Server{
		Addr: dataAddr, Handler: px,
		ReadHeaderTimeout: 10 * time.Second, ReadTimeout: 60 * time.Second,
		WriteTimeout: 150 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 32 << 10,
	}
	ctlSrv := &http.Server{
		Addr: ctlAddr, Handler: ch,
		ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second,
		WriteTimeout: 10 * time.Second, MaxHeaderBytes: 16 << 10,
	}
	// Both listeners are plain HTTP/1.1: net/http never enables cleartext
	// HTTP/2 (h2c) unless asked to, and the data plane refuses ProtoMajor != 1.

	logger.Info("starting", logger.Fields{
		"data_listen": dataAddr, "ctl_listen": ctlAddr, "boot_id": bootID,
		"resources": set.Names(), "client_cidr": os.Getenv("CLIENT_CIDR"),
	})
	errc := make(chan error, 2)
	go func() { errc <- dataSrv.ListenAndServe() }()
	go func() { errc <- ctlSrv.ListenAndServe() }()
	select {
	case err := <-errc:
		logger.Fatal("listen_failed", logger.Fields{"error": err.Error()})
	case <-ctx.Done():
	}
	shut, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = dataSrv.Shutdown(shut)
	_ = ctlSrv.Shutdown(shut)
	fmt.Fprintln(os.Stderr, "stopped")
}

func newBootID() string {
	b := make([]byte, 8)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}
