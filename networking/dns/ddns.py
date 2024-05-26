import requests
import sys
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('-d', '--domain', type=str,
                    help='The domain to update DNS records for',
                    required=True)
parser.add_argument('-k', '--key', type=str,
                    help='The value of the Cloudflare API key',
                    required=True)
parser.add_argument('-e', '--email', type=str,
                    help='The email associated with your Cloudflare account',
                    required=True)
parser.add_argument('-z', '--zone_id', type=str,
                    help='The Cloudflare zone ID for your domain',
                    required=True)
parser.add_argument('-r', '--rrhost', type=str,
                    help='The name of the rrhost',
                    nargs='+',
                    required=True)
args = parser.parse_args()

domain = args.domain
key = args.key
email = args.email
zone_id = args.zone_id
rrhost = args.rrhost

my_ip = requests.get('https://ifconfig.me').text

# Get DNS records from Cloudflare
headers = {
    'Authorization': "Bearer " +  key,
    'Content-Type': 'application/json'
}

list_request = f'https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records'
response = requests.get(list_request, headers=headers)
if response.status_code != 200:
    print('Failed to list DNS records--Cloudflare returned status code ' + str(response.content))
    sys.exit(1)

dns_records = response.json()['result']
for host in rrhost:
    for record in dns_records:
        record_type = record['type']
        if record_type == 'A':
            record_name = record['name']
            if record_name == host + "." + domain or record_name == host + domain:
                record_id = record['id']
                update_request = f'https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}'
                if host == '':
                    computed_host = domain
                else:
                    computed_host = host
                data = {
                    'type': 'A',
                    'name': computed_host,
                    'content': my_ip,
                    'ttl': 7200
                }
                response = requests.put(update_request, headers=headers, json=data)
                if response.status_code != 200:
                    print('Update failed--Cloudflare returned status code ' + str(response.content))
                else:
                    print(response.json())

