package rules

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"unicode/utf8"
)

const maxJSONDepth = 32

var errBadJSON = errors.New("bad json")

// StrictObject parses body as exactly one JSON object and returns its
// top-level fields keyed by lower-cased name. It rejects invalid UTF-8,
// trailing data, nesting deeper than maxJSONDepth, and duplicate keys at any
// level, where keys differing only in case count as duplicates: several
// upstreams (ASP.NET Core in the Servarr apps and Ombi) bind JSON properties
// case-insensitively, so {"name":"A","Name":"B"} must never reach them.
func StrictObject(body []byte) (map[string]json.RawMessage, error) {
	if !utf8.Valid(body) {
		return nil, errBadJSON
	}
	trimmed := bytes.TrimSpace(body)
	if len(trimmed) == 0 || trimmed[0] != '{' {
		return nil, errBadJSON
	}
	dec := json.NewDecoder(bytes.NewReader(trimmed))
	dec.UseNumber()
	if err := walk(dec, 0); err != nil {
		return nil, err
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, errBadJSON
	}
	var top map[string]json.RawMessage
	if err := json.Unmarshal(trimmed, &top); err != nil {
		return nil, errBadJSON
	}
	out := make(map[string]json.RawMessage, len(top))
	for k, v := range top {
		out[strings.ToLower(k)] = v
	}
	return out, nil
}

// walk consumes one JSON value from dec, checking keys and depth.
func walk(dec *json.Decoder, depth int) error {
	if depth > maxJSONDepth {
		return errBadJSON
	}
	tok, err := dec.Token()
	if err != nil {
		return errBadJSON
	}
	switch t := tok.(type) {
	case json.Delim:
		switch t {
		case '{':
			seen := map[string]bool{}
			for dec.More() {
				kt, err := dec.Token()
				if err != nil {
					return errBadJSON
				}
				k, ok := kt.(string)
				if !ok {
					return errBadJSON
				}
				lk := strings.ToLower(k)
				if seen[lk] {
					return errBadJSON
				}
				seen[lk] = true
				if err := walk(dec, depth+1); err != nil {
					return err
				}
			}
			if end, err := dec.Token(); err != nil || end != json.Delim('}') {
				return errBadJSON
			}
		case '[':
			for dec.More() {
				if err := walk(dec, depth+1); err != nil {
					return err
				}
			}
			if end, err := dec.Token(); err != nil || end != json.Delim(']') {
				return errBadJSON
			}
		default:
			return errBadJSON
		}
	}
	return nil
}

// RPCCall is a parsed single JSON-RPC call.
type RPCCall struct {
	Method string
	Params []json.RawMessage
}

// ParseRPC parses a single JSON-RPC request object. Batches (arrays) are
// refused by StrictObject.
func ParseRPC(body []byte) (*RPCCall, error) {
	obj, err := StrictObject(body)
	if err != nil {
		return nil, err
	}
	rawMethod, ok := obj["method"]
	if !ok {
		return nil, errBadJSON
	}
	var m string
	if err := json.Unmarshal(rawMethod, &m); err != nil {
		return nil, errBadJSON
	}
	call := &RPCCall{Method: m}
	if rawParams, ok := obj["params"]; ok {
		p := bytes.TrimSpace(rawParams)
		if string(p) != "null" {
			if len(p) == 0 || p[0] != '[' {
				return nil, errBadJSON
			}
			if err := json.Unmarshal(p, &call.Params); err != nil {
				return nil, errBadJSON
			}
		}
	}
	return call, nil
}
