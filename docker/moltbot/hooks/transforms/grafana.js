// Grafana webhook → OpenClaw wake event transform
//
// Receives Grafana's unified alerting webhook payload and converts it
// to an OpenClaw wake event ({ text, mode }).
//
// Grafana sends: { receiver, status, alerts: [{ labels, annotations, status, ... }], ... }
//
// Deployed to: /home/node/.openclaw/hooks/transforms/grafana.js
// Triggered via: POST /hooks/grafana on the gateway
module.exports = function transform(payload) {
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

  var text;
  if (lines.length > 0) {
    text = '🚨 Grafana Alert (' + status + '): ' + lines.join(' | ');
  } else {
    // No alerts array — dump top-level keys for debugging
    text = '🚨 Grafana Alert: payload had no alerts array. Keys: ' +
           Object.keys(payload || {}).join(', ');
  }

  return {
    text: text,
    mode: 'now'
  };
};
