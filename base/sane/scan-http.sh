#!/bin/sh
# CGI wrapper for shell2http (-cgi): runs scan.sh and returns HTTP 500 when it
# fails, so the caller (Home Assistant shell_command.scan_doc, curl -f) sees
# the error instead of a 200.
if out=$(/root/scan.sh 2>&1); then
  printf 'Status: 200 OK\r\nContent-Type: text/plain\r\n\r\n%s\n' "$out"
else
  rc=$?
  logger -t scan-http -p user.err "scan failed (exit $rc)"
  printf 'Status: 500 Internal Server Error\r\nContent-Type: text/plain\r\n\r\nscan failed (exit %s)\n%s\n' "$rc" "$out"
fi
