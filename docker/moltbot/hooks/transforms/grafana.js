// Grafana webhook → OpenClaw wake event transform
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

  var text;
  if (lines.length > 0) {
    text = '🚨 Grafana (' + status + '): ' + lines.join(' | ');
  } else {
    text = '🚨 Grafana (' + status + '): no alert details';
  }

  return {
    text: text,
    mode: 'now'
  };
};
