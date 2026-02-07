package main

import (
	"context"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/nkontur/jit-approval-svc/internal/backend"
	"github.com/nkontur/jit-approval-svc/internal/config"
	"github.com/nkontur/jit-approval-svc/internal/handler"
	"github.com/nkontur/jit-approval-svc/internal/logger"
	"github.com/nkontur/jit-approval-svc/internal/store"
	"github.com/nkontur/jit-approval-svc/internal/telegram"
	"github.com/nkontur/jit-approval-svc/internal/vault"
)

func main() {
	// Load configuration
	cfg, err := config.Load()
	if err != nil {
		logger.Fatal("config_load_failed", logger.Fields{
			"error": err.Error(),
		})
	}

	logger.Info("starting", logger.Fields{
		"listen_addr":        cfg.ListenAddr,
		"vault_addr":         cfg.VaultAddr,
		"request_timeout":    cfg.RequestTimeout.String(),
		"allowed_requesters": cfg.AllowedRequesters,
		"ha_url":             cfg.HAURL,
		"grafana_url":        cfg.GrafanaURL,
		"plex_url":           cfg.PlexURL,
		"influxdb_url":       cfg.InfluxDBURL,
	})

	// Initialize request store
	reqStore := store.New()

	// Initialize Vault client
	vaultClient, err := vault.New(cfg.VaultAddr, cfg.VaultRoleID, cfg.VaultSecretID)
	if err != nil {
		logger.Fatal("vault_init_failed", logger.Fields{
			"error": err.Error(),
		})
	}

	// Initialize Telegram client
	tgClient := telegram.New(cfg.TelegramBotToken, cfg.TelegramChatID)

	// Initialize backend registry (dynamic backends + static fallback)
	backends := backend.NewRegistry(
		vaultClient,
		vaultClient,
		cfg.HAURL,
		cfg.GrafanaURL,
		cfg.PlexURL,
		cfg.InfluxDBURL,
	)

	// Initialize handler
	h := handler.New(cfg, reqStore, vaultClient, tgClient, backends)

	// Setup HTTP routes
	mux := http.NewServeMux()
	mux.HandleFunc("/request", h.HandleRequest)
	mux.HandleFunc("/status/", h.HandleStatus)
	mux.HandleFunc("/health", h.HandleHealth)
	mux.HandleFunc("/telegram/webhook", h.HandleTelegramWebhook)

	server := &http.Server{
		Addr:         cfg.ListenAddr,
		Handler:      loggingMiddleware(mux),
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 10 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	// Start background cleanup goroutine
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go cleanupLoop(ctx, reqStore)

	// Graceful shutdown
	go func() {
		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
		sig := <-sigCh
		logger.Info("shutdown_signal", logger.Fields{
			"signal": sig.String(),
		})
		cancel()
		shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer shutdownCancel()
		if err := server.Shutdown(shutdownCtx); err != nil {
			logger.Error("shutdown_error", logger.Fields{
				"error": err.Error(),
			})
		}
	}()

	logger.Info("server_started", logger.Fields{
		"addr": cfg.ListenAddr,
	})

	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		logger.Fatal("server_error", logger.Fields{
			"error": err.Error(),
		})
	}

	logger.Info("server_stopped", nil)
}

// loggingMiddleware logs every HTTP request.
func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rw := &responseWriter{ResponseWriter: w, statusCode: http.StatusOK}

		next.ServeHTTP(rw, r)

		logger.Info("http_request", logger.Fields{
			"method":      r.Method,
			"path":        r.URL.Path,
			"status":      rw.statusCode,
			"duration_ms": time.Since(start).Milliseconds(),
			"remote_addr": r.RemoteAddr,
		})
	})
}

type responseWriter struct {
	http.ResponseWriter
	statusCode int
}

func (rw *responseWriter) WriteHeader(code int) {
	rw.statusCode = code
	rw.ResponseWriter.WriteHeader(code)
}

// cleanupLoop periodically removes old resolved requests from the store.
func cleanupLoop(ctx context.Context, s *store.Store) {
	ticker := time.NewTicker(5 * time.Minute)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			removed := s.Cleanup(1 * time.Hour)
			if removed > 0 {
				logger.Info("store_cleanup", logger.Fields{
					"removed":   removed,
					"remaining": s.Count(),
				})
			}
		}
	}
}
