"""Kill switch (section 7.3, decision 13).

Three sources, read on every decision:
  repo     `mode` in config.yml (the default for deploys)
  host     /etc/event-router/override/mode on the router (root writes it; wins
           over the repo value, either direction)
  runtime  set by the router itself on an automation storm (7.2): it can
           only lower the mode, never raise it; the Telegram "Resume" button
           (event-router-control) clears it

The Telegram buttons ("Stop automation" / "Resume") write and remove the host
file through event-router-control, so they work while this container is down.

The executor computes the same thing independently (executor/event-router-executor).
"""

MODES = ("notify-only", "shadow", "active")       # least to most privileged
RUNTIME_MODES = ("notify-only", "shadow")


def valid(mode):
    return mode in MODES


def lower(a, b):
    return a if MODES.index(a) <= MODES.index(b) else b


def effective_mode(repo, host=None, runtime=None):
    """Unknown or empty values fail closed to notify-only."""
    base = host if host else repo
    if not valid(base):
        return "notify-only"
    if runtime:
        if runtime not in RUNTIME_MODES:
            return "notify-only"
        base = lower(base, runtime)
    return base


def read_host_override(path):
    """Contents of the host override file, or None. Unreadable or invalid
    content fails closed (notify-only)."""
    try:
        with open(path) as f:
            value = f.read().strip()
    except FileNotFoundError:
        return None
    except OSError:
        return "notify-only"
    if not value:
        return None
    return value if valid(value) else "notify-only"


def playbook_mode(global_mode, pb_mode):
    """A playbook acts only when both it and the router are active."""
    if global_mode == "notify-only":
        return "notify-only"
    if global_mode == "active" and pb_mode == "active":
        return "active"
    return "shadow"
