# GitLab API Reference

Complete API reference for GitLab MR lifecycle management.

**Base URL:** `https://gitlab.lab.nkontur.com/api/v4`  
**Project ID:** 4  
**Authentication:** `PRIVATE-TOKEN: $GITLAB_TOKEN` header

## Merge Requests

### Create MR
```bash
POST /projects/4/merge_requests
```

**Payload:**
```json
{
  "source_branch": "feature/branch-name",
  "target_branch": "main",
  "title": "Brief descriptive title",
  "description": "Detailed description of changes and motivation",
  "remove_source_branch": true,
  "squash": false
}
```

**Response:** Returns MR object with `iid` (internal ID) and `web_url`

### Get MR Details
```bash
GET /projects/4/merge_requests/{mr_iid}
```

**Key response fields:**
- `state`: "opened", "merged", "closed"
- `merge_status`: "can_be_merged", "cannot_be_merged"
- `user_notes_count`: Number of comments
- `upvotes`: Number of thumbs up
- `downvotes`: Number of thumbs down

### List MR Comments
```bash
GET /projects/4/merge_requests/{mr_iid}/notes
```

**Query parameters:**
- `sort=asc` - Chronological order
- `order_by=created_at` - Sort by creation time

**Key response fields per note:**
- `id`: Note ID
- `created_at`: ISO timestamp
- `updated_at`: ISO timestamp  
- `body`: Comment text
- `author.username`: Comment author
- `system`: Boolean (true for system notes like "approved", false for user comments)

### Create Standalone Comment
```bash
POST /projects/4/merge_requests/{mr_iid}/notes
```

**Payload:**
```json
{
  "body": "Response text (not threaded)"
}
```

**Note:** This creates a top-level comment, NOT a threaded reply. Use discussions for threading.

## Discussions (Threaded Comments)

### List Discussions
```bash
GET /projects/4/merge_requests/{mr_iid}/discussions
```

Returns array of discussions. Each discussion has:
- `id`: Discussion ID (use this for replies)
- `notes`: Array of notes in the thread
  - `id`: Note ID
  - `body`: Comment text
  - `author.username`: Author

### Create New Discussion
```bash
POST /projects/4/merge_requests/{mr_iid}/discussions
```

**Payload:**
```json
{
  "body": "Starting a new discussion thread"
}
```

### Reply to Existing Discussion (THREADED)
```bash
POST /projects/4/merge_requests/{mr_iid}/discussions/{discussion_id}/notes
```

**Payload:**
```json
{
  "body": "This reply appears in the thread"
}
```

**This is the correct way to reply to comments.** Always use the discussion_id from the original comment's discussion.

### Update MR Description
```bash
PUT /projects/4/merge_requests/{mr_iid}
```

**Payload:**
```json
{
  "description": "Updated description with changelog"
}
```

## Branch Operations

### List Branches
```bash
GET /projects/4/repository/branches
```

### Create Branch
```bash
POST /projects/4/repository/branches
```

**Payload:**
```json
{
  "branch": "feature/branch-name",
  "ref": "main"
}
```

### Delete Branch
```bash
DELETE /projects/4/repository/branches/{branch_name}
```

## Useful Queries

### Get Latest Comments Since Timestamp
```bash
GET /projects/4/merge_requests/{mr_iid}/notes?sort=asc&order_by=created_at
```

Filter response by `created_at > last_check_time` to find new comments.

### Check MR Status
```bash
GET /projects/4/merge_requests/{mr_iid}
```

Look for:
- `state == "merged"` (completed)
- `state == "closed"` (rejected) 
- `upvotes > 0` (approved)
- `user_notes_count` increased (new comments)

## Error Handling

**401 Unauthorized** - Check `$GITLAB_TOKEN` validity  
**403 Forbidden** - Token lacks required permissions  
**404 Not Found** - MR/branch/project doesn't exist  
**409 Conflict** - Branch already exists or merge conflict  
**422 Unprocessable** - Invalid request data

## Rate Limiting

GitLab.com default: 2000 requests/minute per user  
Self-hosted instances may have different limits  
Use exponential backoff on 429 responses

## Authentication Test

Verify token and permissions:
```bash
curl -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
     "https://gitlab.lab.nkontur.com/api/v4/projects/4"
```

Should return project details if token has access.