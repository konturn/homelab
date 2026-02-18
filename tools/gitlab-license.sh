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

# Cleanup temp files in container
docker exec "$GITLAB_CONTAINER" rm -f /tmp/license_key /tmp/license_key.pub /tmp/homelab.gitlab-license
echo "Cleaned up temp files."
