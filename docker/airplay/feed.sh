#!/bin/sh
{# Rendered once per airplay_receivers entry, like shairport-sync.conf. #}
# {{ item.receiver.name }}: shairport-sync's play hooks (docs/AUDIO.md).
#   start  connect the audio pipe to snapserver's {{ item.receiver.stream }} source
#          (tcp {{ snapserver_address }}:{{ item.receiver.port }}) for one play session
#   stop   close that connection, so the port is free for other senders
# Rendered by Ansible from docker/airplay/feed.sh.
FIFO=/tmp/shairport-sync-audio
PIDFILE=/tmp/feed.pid

close() {
	if [ -f "$PIDFILE" ]; then
		kill "$(cat "$PIDFILE")" 2>/dev/null
		rm -f "$PIDFILE"
	fi
}

case "${1:-}" in
start)
	close
	nc {{ snapserver_address }} {{ item.receiver.port }} < "$FIFO" > /dev/null 2>&1 &
	echo $! > "$PIDFILE"
	;;
stop)
	close
	# Drop what is left in the pipe, so the next session does not start with
	# the tail of this one.
	timeout 1 cat "$FIFO" > /dev/null 2>&1
	exit 0
	;;
*)
	echo "usage: $0 start|stop" >&2
	exit 2
	;;
esac
