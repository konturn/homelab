package logger

import (
	"encoding/json"
	"fmt"
	"os"
	"sync"
	"time"
)

// Fields is a map of key-value pairs for structured logging.
type Fields map[string]interface{}

var (
	mu  sync.Mutex
	out = os.Stdout
)

// log writes a structured JSON log line to stdout.
func log(level, event string, fields Fields) {
	entry := make(map[string]interface{}, len(fields)+3)
	entry["ts"] = time.Now().UTC().Format(time.RFC3339Nano)
	entry["level"] = level
	entry["event"] = event

	for k, v := range fields {
		entry[k] = v
	}

	data, err := json.Marshal(entry)
	if err != nil {
		// Fallback: write the error itself
		fmt.Fprintf(os.Stderr, `{"ts":"%s","level":"error","event":"log_marshal_error","error":"%s"}`+"\n",
			time.Now().UTC().Format(time.RFC3339Nano), err.Error())
		return
	}

	mu.Lock()
	defer mu.Unlock()
	fmt.Fprintln(out, string(data))
}

// Info logs an info-level event.
func Info(event string, fields Fields) {
	log("info", event, fields)
}

// Warn logs a warn-level event.
func Warn(event string, fields Fields) {
	log("warn", event, fields)
}

// Error logs an error-level event.
func Error(event string, fields Fields) {
	log("error", event, fields)
}

// Fatal logs a fatal-level event and exits.
func Fatal(event string, fields Fields) {
	log("fatal", event, fields)
	os.Exit(1)
}
