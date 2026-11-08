// Grafana webhook → OpenClaw agent event transform
// Gateway wraps the HTTP body inside { payload, headers, url, path }
//
// Phase 3 (docs/design/event-routing.md 15): the event router is the
// catch-all. It hands OpenClaw what it could not handle through
// /hooks/event-router (event-router.js). This route stays as the fallback
// (property L4): for each alert it asks the router whether it has it
// (GET /ack); alerts the router acknowledged are dropped here, the rest (the
// router is down, never received the alert, or is not forwarding) reach the
// agent as before, marked as a fallback. No secret is needed: /ack answers
// only this gateway's address.
'use strict';

var ROUTER = 'http://event-router:8080';
var ACK_WAIT_MS = 8000;      // the router's copy normally arrives ~20s earlier (group_wait 10s vs 30s)

async function acked(fp, status) {
  if (!/^[0-9a-f]{1,32}$/.test(String(fp || ''))) return false;
  try {
    var r = await fetch(ROUTER + '/ack?fp=' + fp + '&status=' + (status === 'resolved' ? 'resolved' : 'firing'),
                        {signal: AbortSignal.timeout(2000)});
    if (!r.ok) return false;
    var body = await r.json();
    return body && body.accepted === true;
  } catch (e) {
    return false;            // router down or unreachable: fallback
  }
}

// The alerts the router does not own, after waiting briefly for it to catch up.
async function unowned(alerts) {
  var deadline = Date.now() + ACK_WAIT_MS;
  var left = alerts.slice();
  var reachable = true;
  while (left.length > 0) {
    var next = [];
    for (var i = 0; i < left.length; i++) {
      if (!(await acked(left[i].fingerprint, left[i].status))) next.push(left[i]);
    }
    left = next;
    if (left.length === 0 || Date.now() >= deadline) break;
    try {
      var h = await fetch(ROUTER + '/healthz', {signal: AbortSignal.timeout(2000)});
      reachable = h.ok;
    } catch (e) {
      reachable = false;
    }
    if (!reachable) break;   // no point waiting for a router that is down
    await new Promise(function (res) { setTimeout(res, 2000); });
  }
  return left;
}

module.exports = async function transform(raw) {
  var payload = raw.payload || raw;
  var allAlerts = payload.alerts || [];
  var alerts = await unowned(allAlerts);
  if (allAlerts.length > 0 && alerts.length === 0) {
    return null;             // the router has every alert in this notification
  }
  var status = payload.status || 'unknown';
  var fallback = allAlerts.length > 0;

  var lines = alerts.map(function(a) {
    var name = (a.labels && a.labels.alertname) || 'Unknown';
    var severity = (a.labels && a.labels.severity) || '';
    var summary = (a.annotations && a.annotations.summary) ||
                  (a.annotations && a.annotations.description) || '';
    var state = a.status || '';
    var parts = [name];
    if (severity) parts[0] += ' (' + severity + ')';
    if (state) parts[0] += ' [' + state + ']';
    if (summary) parts.push(summary);
    return parts.join(': ');
  });

  var alertText;
  if (lines.length > 0) {
    alertText = lines.join('\n');
  } else {
    alertText = 'No alert details in payload';
  }

  // Alerts from the anomaly detector (label source=anomaly-detector) follow
  // their own protocol: gather context, message Noah only when actionable.
  var anomaly = alerts.length > 0 && alerts.every(function(a) {
    return a.labels && a.labels.source === 'anomaly-detector';
  });
  // A resolved anomaly needs no message (the firing one was the message, or
  // triage decided it was not actionable). Without this the agent re-reported
  // the [TEST] alert on resolve (2026-10-21).
  var allResolved = alerts.every(function(a) { return a.status === 'resolved'; });
  if (anomaly && allResolved) {
    return {
      message: '📈 Anomaly alert resolved:\n' + alertText + '\n\n' +
        'No action and no message: your entire reply is NO_REPLY.',
      mode: 'now'
    };
  }
  if (anomaly) {
    return {
      message: '📈 Anomaly alert (' + status + '):\n' + alertText + '\n\n' +
        'Follow skills/anomaly-triage/SKILL.md (not ALERTS.md): check /status, recent deploys and /context, ' +
        'then message Noah only if it is actionable (one line: why + suggested action). ' +
        'If it is not actionable, your entire reply is NO_REPLY. ' +
        'If the summary starts with [TEST] (a firing test alert), send exactly one message prefixed [TEST].',
      mode: 'now'
    };
  }

  // Test notifications (summary starting with [TEST]) that reach the agent
  // through this fallback: confirm the path, deliver nothing.
  var tests = alerts.length > 0 && alerts.every(function (a) {
    return a.annotations && String(a.annotations.summary || '').indexOf('[TEST]') === 0;
  });
  if (fallback && tests && !anomaly) {
    return {
      message: '🚨 Grafana Alert (' + status + '), FALLBACK path [TEST]:\n' + alertText + '\n\n' +
        'This is a test of the fallback route (the event router did not take this alert). Nothing is broken; ' +
        'do nothing. Your reply is not delivered: reply with exactly TEST-OK FALLBACK.',
      deliver: false,
      mode: 'now'
    };
  }
  var note = fallback
    ? '\n\nFALLBACK: the event router did not take this alert (it may be down: check `event-router-down` and ' +
      '`docker ps --filter name=event-router`). No playbook ran for it. Handle it yourself.'
    : '';
  return {
    message: '🚨 Grafana Alert (' + status + '):\n' + alertText + note + '\n\nRead ALERTS.md for response protocol. Investigate using Loki logs and Grafana dashboards. Fix if possible. Notify Noah via Telegram with what happened and what you did.',
    mode: 'now'
  };
};
