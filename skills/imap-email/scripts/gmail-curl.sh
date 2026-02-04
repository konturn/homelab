#!/bin/bash
# Gmail IMAP via curl - lightweight wrapper
# Usage: gmail-curl.sh <command> [options]
#
# Commands:
#   search <query>       - IMAP SEARCH (e.g., "FROM fidelity", "UNSEEN", "SINCE 01-Jan-2026")
#   fetch <num> [part]   - Fetch message by MAILINDEX (part: HEADER, TEXT, or empty for full)
#   headers <num>        - Fetch key headers (From, Subject, Date, To)
#   count               - Count total and unseen messages
#   folders             - List mailbox folders
#   batch-headers <nums> - Fetch headers for multiple messages (comma-separated)

set -euo pipefail

GMAIL_USER="${GMAIL_EMAIL:?GMAIL_EMAIL not set}"
GMAIL_PASS="${GMAIL_APP_PASSWORD:?GMAIL_APP_PASSWORD not set}"
IMAP_URL="imaps://imap.gmail.com:993"
MAILBOX="${GMAIL_MAILBOX:-INBOX}"

curl_imap() {
  curl -s --max-time 30 --url "$1" --user "$GMAIL_USER:$GMAIL_PASS" "${@:2}" 2>/dev/null
}

case "${1:-help}" in
  search)
    shift
    QUERY="$*"
    curl_imap "$IMAP_URL/$MAILBOX" -X "SEARCH $QUERY"
    ;;

  fetch)
    NUM="${2:?Usage: fetch <mailindex> [HEADER|TEXT]}"
    SECTION="${3:-}"
    if [ -n "$SECTION" ]; then
      curl_imap "$IMAP_URL/$MAILBOX;MAILINDEX=$NUM;SECTION=$SECTION"
    else
      curl_imap "$IMAP_URL/$MAILBOX;MAILINDEX=$NUM"
    fi
    ;;

  headers)
    NUM="${2:?Usage: headers <mailindex>}"
    curl_imap "$IMAP_URL/$MAILBOX;MAILINDEX=$NUM;SECTION=HEADER.FIELDS%20(FROM%20TO%20SUBJECT%20DATE%20CC)"
    ;;

  count)
    curl_imap "$IMAP_URL/$MAILBOX" -X "STATUS \"$MAILBOX\" (MESSAGES UNSEEN RECENT)"
    ;;

  folders)
    curl_imap "$IMAP_URL/" -X "LIST \"\" *"
    ;;

  batch-headers)
    NUMS="${2:?Usage: batch-headers <num1,num2,num3,...>}"
    IFS=',' read -ra INDICES <<< "$NUMS"
    for idx in "${INDICES[@]}"; do
      echo "=== MESSAGE $idx ==="
      curl_imap "$IMAP_URL/$MAILBOX;MAILINDEX=$idx;SECTION=HEADER.FIELDS%20(FROM%20TO%20SUBJECT%20DATE)" || echo "(fetch failed)"
      echo ""
    done
    ;;

  help|*)
    echo "Gmail IMAP CLI (curl-based)"
    echo ""
    echo "Commands:"
    echo "  search <query>        IMAP search (FROM x, UNSEEN, SINCE dd-Mon-yyyy, SUBJECT x)"
    echo "  fetch <num> [part]    Fetch message (part: HEADER, TEXT, or omit for full)"
    echo "  headers <num>         Fetch key headers for message"
    echo "  count                 Count total/unseen messages"
    echo "  folders               List mailbox folders"
    echo "  batch-headers <n,n>   Headers for multiple messages"
    echo ""
    echo "Search examples:"
    echo "  search FROM john@example.com"
    echo "  search UNSEEN SINCE 01-Jan-2026"
    echo "  search SUBJECT \"meeting\" FROM boss"
    echo "  search OR FROM alice FROM bob"
    echo ""
    echo "Env: GMAIL_EMAIL, GMAIL_APP_PASSWORD, GMAIL_MAILBOX (default: INBOX)"
    ;;
esac
