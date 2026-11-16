// Package logger writes one JSON object per line to stdout, the format the
// rest of the homelab ships to Loki. Every line passes through a scrubber
// (SetScrubber) before it is written, so a held secret never reaches the log
// even if a caller puts one in a field by mistake.
package logger

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sync"
	"time"
)

// Fields is a map of key-value pairs for structured logging.
type Fields map[string]interface{}

var (
	mu      sync.Mutex
	out     io.Writer = os.Stdout
	scrub   func(string) string
	nowFunc = time.Now
)

// SetOutput redirects log output (tests).
func SetOutput(w io.Writer) {
	mu.Lock()
	defer mu.Unlock()
	out = w
}

// SetScrubber installs a function applied to every serialised line.
func SetScrubber(f func(string) string) {
	mu.Lock()
	defer mu.Unlock()
	scrub = f
}

func log(level, event string, fields Fields) {
	entry := make(map[string]interface{}, len(fields)+3)
	for k, v := range fields {
		entry[k] = v
	}
	entry["ts"] = nowFunc().UTC().Format(time.RFC3339Nano)
	entry["level"] = level
	entry["event"] = event

	data, err := json.Marshal(entry)
	if err != nil {
		data = []byte(fmt.Sprintf(`{"ts":%q,"level":"error","event":"log_marshal_error"}`,
			nowFunc().UTC().Format(time.RFC3339Nano)))
	}
	line := string(data)

	mu.Lock()
	defer mu.Unlock()
	if scrub != nil {
		line = scrub(line)
	}
	fmt.Fprintln(out, line)
}

// Info logs an info-level event.
func Info(event string, fields Fields) { log("info", event, fields) }

// Warn logs a warn-level event.
func Warn(event string, fields Fields) { log("warn", event, fields) }

// Error logs an error-level event.
func Error(event string, fields Fields) { log("error", event, fields) }

// Fatal logs a fatal-level event and exits.
func Fatal(event string, fields Fields) {
	log("fatal", event, fields)
	os.Exit(1)
}
