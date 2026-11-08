"""`/maint` from Telegram (doc 13.1), parsed here, not by the agent.

Noah types the command to OpenClaw's Telegram bot; OpenClaw passes the text
through verbatim (POST /maint/text) and relays the reply. The grammar is
strict, so a model in between can only forward what this parser accepts:

  /maint <node>[,<node>...] <duration> <reason>    open a quiet window
  /maint list                                      open windows
  /maint close <maint-id>                          close one (recovery check runs)

  node      a node of inventory/dependencies.yml (not `site`: rack days are
            declared on the router, scripts/event-router)
  duration  <n>m or <n>h, at most 12h
  reason    3-200 printable characters
"""

import re

from .util import parse_duration

MAX_TEXT = 300
_OPEN = re.compile(r"^/maint\s+([a-z0-9_.,-]{1,200})\s+([0-9]{1,3}[mh])\s+(\S.{1,198}\S)\s*$")
_CLOSE = re.compile(r"^/maint\s+close\s+(maint-[0-9a-f]{8})\s*$")
_LIST = re.compile(r"^/maint\s+list\s*$")
USAGE = ("Usage: /maint <node>[,<node>...] <duration> <reason>  (duration like 90m or 2h, at most 12h)\n"
         "       /maint list | /maint close <maint-id>")


class BadCommand(ValueError):
    pass


def parse(text, graph, max_window):
    """('open', scope, seconds, reason) | ('list',) | ('close', id); raises BadCommand."""
    if not isinstance(text, str) or len(text) > MAX_TEXT or not text.isprintable():
        raise BadCommand(USAGE)
    text = text.strip()
    if _LIST.match(text):
        return ("list",)
    m = _CLOSE.match(text)
    if m:
        return ("close", m.group(1))
    m = _OPEN.match(text)
    if not m:
        raise BadCommand(USAGE)
    scope = [s for s in m.group(1).split(",") if s]
    if not scope or len(scope) > 10:
        raise BadCommand("Give 1-10 nodes. " + USAGE)
    unknown = [s for s in scope if s not in graph.nodes]
    if unknown:
        raise BadCommand(f"Unknown node(s): {', '.join(unknown)}. Nodes are the names in "
                         f"inventory/dependencies.yml (e.g. zigbee2mqtt, mqtt, homeassistant).")
    seconds = parse_duration(m.group(2))
    if seconds <= 0 or seconds > max_window:
        raise BadCommand(f"Duration must be between 1m and {max_window // 3600}h.")
    return ("open", scope, seconds, m.group(3))
