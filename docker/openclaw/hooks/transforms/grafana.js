// Grafana webhook → OpenClaw agent event transform
// Gateway wraps the HTTP body inside { payload, headers, url, path }
module.exports = function transform(raw) {
  var payload = raw.payload || raw;
  var alerts = payload.alerts || [];
  var status = payload.status || 'unknown';

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

  return {
    message: '🚨 Grafana Alert (' + status + '):\n' + alertText + '\n\nRead ALERTS.md for response protocol. Investigate using Loki logs and Grafana dashboards. Fix if possible. Notify Noah via Telegram with what happened and what you did.',
    mode: 'now'
  };
};
