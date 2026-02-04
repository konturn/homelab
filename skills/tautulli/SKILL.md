---
name: tautulli
description: Monitor Plex activity and stats via Tautulli API. Check who's watching, view history, get library stats.
---

# Tautulli

Monitor Plex Media Server activity via Tautulli API.

## Configuration

Environment variables (need to configure):
- `TAUTULLI_URL`: Tautulli instance URL (e.g., `http://tautulli:8181`)
- `TAUTULLI_API_KEY`: Settings → Web Interface → API Key

## API Reference

All calls use: `$TAUTULLI_URL/api/v2?apikey=$TAUTULLI_API_KEY&cmd=<command>`

### Current Activity (Who's Watching)

```bash
curl -s "$TAUTULLI_URL/api/v2?apikey=$TAUTULLI_API_KEY&cmd=get_activity" | \
  jq '.response.data.sessions[] | {user: .user, title: .full_title, progress_percent, quality_profile, player}'
```

### Watch History

```bash
# Last 10 items
curl -s "$TAUTULLI_URL/api/v2?apikey=$TAUTULLI_API_KEY&cmd=get_history&length=10" | \
  jq '.response.data.data[] | {user, title: .full_title, date, duration}'
```

### Library Stats

```bash
curl -s "$TAUTULLI_URL/api/v2?apikey=$TAUTULLI_API_KEY&cmd=get_libraries" | \
  jq '.response.data[] | {section_name, count, parent_count, child_count}'
```

### Recently Added

```bash
curl -s "$TAUTULLI_URL/api/v2?apikey=$TAUTULLI_API_KEY&cmd=get_recently_added&count=10" | \
  jq '.response.data.recently_added[] | {title: .full_title, added_at, library_name}'
```

### User Stats

```bash
curl -s "$TAUTULLI_URL/api/v2?apikey=$TAUTULLI_API_KEY&cmd=get_users" | \
  jq '.response.data[] | {friendly_name, total_plays, last_seen}'
```

### Server Info

```bash
curl -s "$TAUTULLI_URL/api/v2?apikey=$TAUTULLI_API_KEY&cmd=get_server_info" | \
  jq '.response.data | {pms_name, pms_version, pms_platform}'
```

### Most Watched

```bash
# Top 10 movies
curl -s "$TAUTULLI_URL/api/v2?apikey=$TAUTULLI_API_KEY&cmd=get_home_stats&stat_id=top_movies&stats_count=10" | \
  jq '.response.data[0].rows[] | {title, total_plays, total_duration}'

# Top 10 TV shows
curl -s "$TAUTULLI_URL/api/v2?apikey=$TAUTULLI_API_KEY&cmd=get_home_stats&stat_id=top_tv&stats_count=10" | \
  jq '.response.data[0].rows[] | {title, total_plays, total_duration}'
```

### Playback History for Specific User

```bash
curl -s "$TAUTULLI_URL/api/v2?apikey=$TAUTULLI_API_KEY&cmd=get_history&user=USERNAME&length=20" | \
  jq '.response.data.data[] | {title: .full_title, date, watched_status}'
```

## Common API Commands

| Command | Description |
|---------|-------------|
| `get_activity` | Current active streams |
| `get_history` | Watch history (params: user, length, start) |
| `get_libraries` | Library stats |
| `get_recently_added` | Recently added media |
| `get_users` | User list with stats |
| `get_server_info` | Plex server details |
| `get_home_stats` | Top media, most active users, etc. |
| `get_plays_by_date` | Historical play counts |

## Notes

- Requires Tautulli running alongside Plex
- API key is per-installation, not per-user
- Historical data depends on how long Tautulli has been running
