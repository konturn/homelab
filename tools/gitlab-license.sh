#!/bin/bash
# GitLab EE License Generator
# Run this on the router host (where GitLab container runs)
#
# Usage: bash gitlab-license.sh
#
# What it does:
# 1. Generates a new RSA keypair
# 2. Creates an Ultimate license signed with the private key
# 3. Replaces GitLab's public key with ours
# 4. Imports the license via Rails runner
# 5. Restarts GitLab
#
# The license will be valid for 1000 years with unlimited users.

set -euo pipefail

GITLAB_CONTAINER="gitlab"
PERSISTENT_KEY_PATH="/persistent_data/application/gitlab/license_encryption_key.pub"
CONTAINER_KEY_PATH="/opt/gitlab/embedded/service/gitlab-rails/.license_encryption_key.pub"

echo "=== GitLab EE License Generator ==="
echo ""

# Step 1: Generate everything inside the container using Rails runner
echo "[1/4] Generating keypair and license inside GitLab container..."

docker exec "$GITLAB_CONTAINER" gitlab-rails runner '
require "openssl"

# Generate keypair
key_pair = OpenSSL::PKey::RSA.generate(2048)
File.open("/tmp/license_key", "w") { |f| f.write(key_pair.to_pem) }
File.open("/tmp/license_key.pub", "w") { |f| f.write(key_pair.public_key.to_pem) }

# Set the encryption key to our private key
Gitlab::License.encryption_key = key_pair

# Build license
license = Gitlab::License.new
license.licensee = {
  "Name"    => "Admin",
  "Company" => "Homelab",
  "Email"   => "admin@localhost"
}
license.starts_at = Date.today
license.expires_at = Date.today + (365 * 1000)
license.restrictions = {
  plan: "ultimate",
  active_user_count: 10000
}

# Export (encrypts and encodes)
data = license.export
File.open("/tmp/homelab.gitlab-license", "w") { |f| f.write(data) }

puts "License generated successfully"
puts "Starts: #{license.starts_at}"
puts "Expires: #{license.expires_at}"
'

echo "[1/4] Done."
echo ""

# Step 2: Copy public key to host persistent path
echo "[2/4] Replacing license encryption public key..."
docker cp "$GITLAB_CONTAINER:/tmp/license_key.pub" "$PERSISTENT_KEY_PATH"
echo "  Written to: $PERSISTENT_KEY_PATH"
echo ""

# Step 3: Restart GitLab so it picks up the new public key
echo "[3/4] Restarting GitLab to load new public key..."
docker restart "$GITLAB_CONTAINER"
echo "  Waiting 60s for GitLab to start..."
sleep 60
echo ""

# Step 4: Import the license
echo "[4/4] Importing license..."
docker exec "$GITLAB_CONTAINER" gitlab-rails runner '
license_data = File.read("/tmp/homelab.gitlab-license")
new_license = License.new(data: license_data)
if new_license.save
  puts "License imported successfully!"
  puts "Plan: #{new_license.plan}"
  puts "Starts: #{new_license.starts_at}"
  puts "Expires: #{new_license.expires_at}"
  puts "Licensee: #{new_license.licensee}"
else
  puts "ERROR: #{new_license.errors.full_messages.join(", ")}"
  exit 1
end
'

echo ""
echo "=== Done! ==="
echo "GitLab should now have an Ultimate license."
echo "Verify at: https://gitlab.lab.nkontur.com/admin/license"

# Step 5: Push updated public key to homelab repo via GitLab API
echo ""
echo "[5/5] Pushing new public key to homelab repo..."

GITLAB_HOST="https://gitlab.lab.nkontur.com"
GITLAB_PROJECT_ID=4
PUBKEY_CONTENT=$(docker cp "$GITLAB_CONTAINER:/tmp/license_key.pub" /dev/stdout 2>/dev/null || docker exec "$GITLAB_CONTAINER" cat /tmp/license_key.pub)
PUBKEY_BASE64=$(echo "$PUBKEY_CONTENT" | base64 -w0)

# Need a token — try GITLAB_TOKEN env, or prompt
if [ -z "${GITLAB_TOKEN:-}" ]; then
  echo "  GITLAB_TOKEN not set. Trying to extract from git remote..."
  # The homelab repo clone on the runner has the token embedded in the remote URL
  GITLAB_TOKEN=$(git -C /root/homelab config --get remote.origin.url 2>/dev/null \
    | grep -oP '(?<=:)[^@]+(?=@)' || true)
  if [ -z "${GITLAB_TOKEN:-}" ]; then
    echo "  Trying Vault..."
    if command -v vault &>/dev/null; then
      GITLAB_TOKEN=$(vault kv get -mount=homelab -field=token agents/gitlab-token 2>/dev/null || true)
    fi
  fi
fi

if [ -n "${GITLAB_TOKEN:-}" ]; then
  # Get current file SHA (needed for update)
  FILE_PATH="docker/gitlab/license_encryption_key.pub"
  CURRENT_SHA=$(curl -sf \
    -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
    "$GITLAB_HOST/api/v4/projects/$GITLAB_PROJECT_ID/repository/files/$(echo "$FILE_PATH" | sed 's|/|%2F|g')?ref=main" \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('last_commit_id',''))" 2>/dev/null || echo "")

  # Push the file
  HTTP_CODE=$(curl -sf -w '%{http_code}' -o /tmp/gitlab-push-resp.json \
    -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
    -H "Content-Type: application/json" \
    -X PUT \
    -d "{
      \"branch\": \"main\",
      \"content\": $(echo "$PUBKEY_CONTENT" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))'),
      \"commit_message\": \"chore: update license encryption public key\",
      \"last_commit_id\": \"$CURRENT_SHA\"
    }" \
    "$GITLAB_HOST/api/v4/projects/$GITLAB_PROJECT_ID/repository/files/$(echo "$FILE_PATH" | sed 's|/|%2F|g')" 2>/dev/null || echo "000")

  if [ "$HTTP_CODE" = "200" ]; then
    echo "  Public key pushed to repo successfully."
  else
    echo "  WARNING: Push failed (HTTP $HTTP_CODE). Response:"
    cat /tmp/gitlab-push-resp.json 2>/dev/null || true
    echo ""
    echo "  Manually commit docker/gitlab/license_encryption_key.pub to the homelab repo"
    echo "  or the next deploy will overwrite it and break the license."
  fi
  rm -f /tmp/gitlab-push-resp.json
else
  echo "  WARNING: No GITLAB_TOKEN available. You must manually commit the public key."
  echo "  To extract it: docker exec gitlab cat /tmp/license_key.pub"
  echo "  Then commit to: docker/gitlab/license_encryption_key.pub in homelab repo"
fi
echo ""

# Cleanup temp files in container
docker exec "$GITLAB_CONTAINER" rm -f /tmp/license_key /tmp/license_key.pub /tmp/homelab.gitlab-license
echo "Cleaned up temp files."
