package rules

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"unicode"

	"gopkg.in/yaml.v3"
)

// ValueSpec constrains one query parameter, JSON field or JSON-RPC param.
//
// In YAML it is either a type name ("any", "int", "bool", "date", "hex"),
// a list of allowed values (an enum), or a map {type, min, max, enum}.
type ValueSpec struct {
	Type string   // any, int, bool, date, hex, enum
	Enum []string // for enum: allowed values
	// Literal marks enum values written as YAML booleans or integers; in a
	// JSON body they match only the JSON literal, never a string ("false"
	// is truthy to most upstreams).
	Literal []bool
	Min     *int64
	Max     *int64
}

var (
	dateRe = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d{1,9})?)?(Z|[+-]\d{2}:\d{2})?)?$`)
	hexRe  = regexp.MustCompile(`^[0-9a-fA-F]{1,128}$`)
	intRe  = regexp.MustCompile(`^-?[0-9]{1,18}$`)
)

// maxAnyLen caps free-text values (search terms and the like).
const maxAnyLen = 512

// UnmarshalYAML implements yaml.Unmarshaler.
func (v *ValueSpec) UnmarshalYAML(n *yaml.Node) error {
	switch n.Kind {
	case yaml.ScalarNode:
		v.Type = n.Value
	case yaml.SequenceNode:
		v.Type = "enum"
		for _, c := range n.Content {
			if c.Kind != yaml.ScalarNode {
				return fmt.Errorf("line %d: enum values must be scalars", c.Line)
			}
			v.Enum = append(v.Enum, c.Value)
			v.Literal = append(v.Literal, c.Tag == "!!bool" || c.Tag == "!!int")
		}
	case yaml.MappingNode:
		var raw struct {
			Type string   `yaml:"type"`
			Min  *int64   `yaml:"min"`
			Max  *int64   `yaml:"max"`
			Enum []string `yaml:"enum"`
		}
		if err := n.Decode(&raw); err != nil {
			return err
		}
		v.Type, v.Min, v.Max, v.Enum = raw.Type, raw.Min, raw.Max, raw.Enum
		v.Literal = make([]bool, len(v.Enum))
		if len(v.Enum) > 0 && v.Type == "" {
			v.Type = "enum"
		}
	default:
		return fmt.Errorf("line %d: invalid value spec", n.Line)
	}
	return v.Validate()
}

// Validate checks the spec itself.
func (v *ValueSpec) Validate() error {
	switch v.Type {
	case "any", "bool", "date", "hex":
	case "int":
	case "enum":
		if len(v.Enum) == 0 {
			return fmt.Errorf("enum without values")
		}
	default:
		return fmt.Errorf("unknown value type %q", v.Type)
	}
	if (v.Min != nil || v.Max != nil) && v.Type != "int" {
		return fmt.Errorf("min/max only apply to int")
	}
	return nil
}

func (v ValueSpec) checkInt(s string) bool {
	if !intRe.MatchString(s) {
		return false
	}
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return false
	}
	if v.Min != nil && n < *v.Min {
		return false
	}
	if v.Max != nil && n > *v.Max {
		return false
	}
	return true
}

// Check validates a query-string value.
func (v ValueSpec) Check(s string) bool {
	switch v.Type {
	case "any":
		if len(s) > maxAnyLen {
			return false
		}
		for _, r := range s {
			if r != ' ' && !unicode.IsPrint(r) {
				return false
			}
		}
		return true
	case "int":
		return v.checkInt(s)
	case "bool":
		return s == "true" || s == "false"
	case "date":
		return dateRe.MatchString(s)
	case "hex":
		return hexRe.MatchString(s)
	case "enum":
		for _, e := range v.Enum {
			if s == e {
				return true
			}
		}
	}
	return false
}

func (v ValueSpec) isLiteral(i int) bool { return i < len(v.Literal) && v.Literal[i] }

// CheckJSON validates a raw JSON value (already parsed strictly).
func (v ValueSpec) CheckJSON(raw json.RawMessage) bool {
	s := strings.TrimSpace(string(raw))
	if s == "" {
		return false
	}
	switch v.Type {
	case "int":
		return v.checkInt(s)
	case "bool":
		return s == "true" || s == "false"
	case "enum":
		// Strings match by their decoded text; true/false/null/numbers by
		// their literal. A string "false" does not match the literal false.
		var str string
		if s[0] == '"' {
			if err := json.Unmarshal(raw, &str); err != nil {
				return false
			}
			for i, e := range v.Enum {
				if str == e && !v.isLiteral(i) {
					return true
				}
			}
			return false
		}
		for i, e := range v.Enum {
			if s == e && v.isLiteral(i) {
				return true
			}
		}
		return false
	case "any", "date", "hex":
		if s[0] != '"' {
			return v.Type == "any" && intRe.MatchString(s)
		}
		var str string
		if err := json.Unmarshal(raw, &str); err != nil {
			return false
		}
		return v.Check(str)
	}
	return false
}
