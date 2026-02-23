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

  return {
    message: '🚨 Grafana Alert (' + status + '):\n' + alertText + '\n\nRead ALERTS.md for response protocol. Investigate using Loki logs and Grafana dashboards. Fix if possible. Notify Noah via Telegram with what happened and what you did.',
    mode: 'now'
  };
};
