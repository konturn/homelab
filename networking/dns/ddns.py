import os
import sys
import argparse
import logging

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ddns")

parser = argparse.ArgumentParser(description="Update Cloudflare DNS A records to current public IP")
parser.add_argument('-d', '--domain', type=str, required=True,
                    help='The domain to update DNS records for')
parser.add_argument('-k', '--key', type=str, default=None,
                    help='Cloudflare API token (prefer CLOUDFLARE_API_KEY env var)')
parser.add_argument('-e', '--email', type=str, default=None,
                    help='Cloudflare account email (prefer CLOUDFLARE_EMAIL env var)')
parser.add_argument('-z', '--zone_id', type=str, default=None,
                    help='Cloudflare zone ID (prefer CLOUDFLARE_ZONE_ID env var)')
parser.add_argument('-r', '--rrhost', type=str, nargs='+', required=True,
                    help='Host prefixes to update (use "" for bare domain, "*" for wildcard)')
parser.add_argument('-v', '--verbose', action='store_true',
                    help='Enable debug logging')
args = parser.parse_args()

if args.verbose:
    log.setLevel(logging.DEBUG)

domain = args.domain
key = args.key or os.environ.get('CLOUDFLARE_API_KEY')
email = args.email or os.environ.get('CLOUDFLARE_EMAIL')
zone_id = args.zone_id or os.environ.get('CLOUDFLARE_ZONE_ID')

if not key:
    log.error("Cloudflare API key not provided. Set CLOUDFLARE_API_KEY env var or use -k flag.")
    sys.exit(1)
if not email:
    log.error("Cloudflare email not provided. Set CLOUDFLARE_EMAIL env var or use -e flag.")
    sys.exit(1)
if not zone_id:
    log.error("Cloudflare zone ID not provided. Set CLOUDFLARE_ZONE_ID env var or use -z flag.")
    sys.exit(1)

rrhost = args.rrhost
log.info("Hosts to update: %s (domain: %s)", rrhost, domain)

# Get current public IP
try:
    my_ip = requests.get('https://ifconfig.me', timeout=10).text.strip()
except requests.RequestException as e:
    log.error("Failed to get public IP: %s", e)
    sys.exit(1)

log.info("Current public IP: %s", my_ip)

# Get DNS records from Cloudflare
headers = {
    'Authorization': "Bearer " + key,
    'Content-Type': 'application/json',
}

list_url = f'https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records?type=A&per_page=100'
response = requests.get(list_url, headers=headers, timeout=15)
if response.status_code != 200:
    log.error("Failed to list DNS records (HTTP %d): %s", response.status_code, response.text)
    sys.exit(1)

dns_records = response.json()['result']
log.info("Found %d A records in zone", len(dns_records))
log.debug("A records: %s", [r['name'] for r in dns_records])

updated = 0
skipped = 0
errors = 0

for host in rrhost:
    # Build the FQDN we're looking for
    if host == '':
        target_fqdn = domain
    else:
        target_fqdn = f"{host}.{domain}"

    matched = False
    for record in dns_records:
        if record['name'] == target_fqdn:
            matched = True
            old_ip = record['content']

            if old_ip == my_ip:
                log.info("SKIP %s — already %s", target_fqdn, my_ip)
                skipped += 1
                continue

            log.info("UPDATE %s — %s -> %s", target_fqdn, old_ip, my_ip)
            update_url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record['id']}"
            data = {
                'type': 'A',
                'name': target_fqdn,
                'content': my_ip,
                'ttl': 7200,
            }
            resp = requests.put(update_url, headers=headers, json=data, timeout=15)
            if resp.status_code != 200:
                log.error("FAIL %s (HTTP %d): %s", target_fqdn, resp.status_code, resp.text)
                errors += 1
            else:
                log.info("OK %s updated", target_fqdn)
                updated += 1

    if not matched:
        log.warning("NO MATCH for host=%r (expected A record: %s)", host, target_fqdn)

log.info("Done: %d updated, %d skipped (already current), %d errors", updated, skipped, errors)

# If any records were updated (IP changed), refresh the JIT webhook
if updated > 0:
    jit_api_key = os.environ.get('JIT_API_KEY', '')
    jit_url = os.environ.get('JIT_URL', 'http://jit-approval-svc:8080')
    if jit_api_key:
        log.info("IP changed — refreshing JIT Telegram webhook...")
        try:
            resp = requests.post(
                f"{jit_url}/webhook/refresh",
                headers={"X-JIT-API-Key": jit_api_key},
                timeout=15,
            )
            if resp.status_code == 200:
                log.info("Webhook refresh OK")
            elif resp.status_code == 429:
                log.warning("Webhook refresh rate limited (already refreshed recently)")
            else:
                log.error("Webhook refresh failed (HTTP %d): %s", resp.status_code, resp.text)
        except requests.RequestException as e:
            log.error("Webhook refresh request failed: %s", e)
    else:
        log.debug("JIT_API_KEY not set, skipping webhook refresh")

if errors:
    sys.exit(1)
