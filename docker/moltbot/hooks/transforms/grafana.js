// Grafana webhook → OpenClaw wake event transform
//
// Receives Grafana's unified alerting webhook payload and converts it
// to an OpenClaw wake event ({ text, mode }).
//
// Deployed to: /home/node/.openclaw/hooks/transforms/grafana.js
// Triggered via: POST /hooks/grafana on the gateway
module.exports = function transform(payload) {
  const alerts = payload.alerts || [];
  const status = payload.status || 'unknown';
  const lines = alerts.map(function(a) {
    const name = (a.labels && a.labels.alertname) || 'Unknown';
    const severity = (a.labels && a.labels.severity) || '';
    const summary = (a.annotations && a.annotations.summary) || '';
    const state = a.status || '';
    return name + (severity ? ' (' + severity + ')' : '') + ' [' + state + ']: ' + summary;
  });
  return {
    text: '🚨 Grafana Alert (' + status + '): ' + lines.join(' | '),
    mode: 'now'
  };
};
