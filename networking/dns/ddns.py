import requests
import xml.etree.ElementTree as ET
import sys
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('-d', '--domain', type=str,
        help='The domain to update DNS records for',
        required=True)
parser.add_argument('-k', '--key', type=str,
        help='The value of the Namesilo API key',
        )
parser.add_argument('-r', '--rrhost', type=str,
                    help='The name of the rrhost',
                    nargs='+',
                    required=False)
args = parser.parse_args()
domain=args.domain
key = args.key
rrhost = args.rrhost

my_ip = requests.get('https://ifconfig.me').text
list_request = 'https://www.namesilo.com/api/dnsListRecords?version=1&type=xml&key=' + key + '&domain=' + "nkontur.com"
response = requests.get(list_request);
xml = response.text
root = ET.fromstring(xml)
for host in rrhost:
    for record in root.iter('resource_record'):
       record_type = record.find('type').text
       if record_type == 'A':
           record_host = record.find('host').text
           if record_host == host + "." + domain or record_host == host + domain:
               rrid = record.find('record_id').text
               update_request = 'https://www.namesilo.com/api/dnsUpdateRecord?version=1&type=xml&key=' + key + '&domain=' + domain + '&rrid=' + rrid + '&rrhost=' + host + '&rrvalue=' + my_ip + '&rrttl=7207'
               response = requests.get(update_request)
               code = response.status_code
               if code!=200:
                   print('Update failed--Namesilo returned status code ' + str(code))
               else:
                   print(response.content)
    
