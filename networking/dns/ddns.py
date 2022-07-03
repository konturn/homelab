import requests
import xml.etree.ElementTree as ET
import sys
import getopt
def usage():
    print('python3 ddns.py -d DOMAIN -k KEY -r RRHOST [-h]')
try:
    opts, args = getopt.getopt(sys.argv[1:], 'd:k:rh', ['domain=', 'key=', 'rrhost=', 'help'])
except getopt.GetoptError:
    usage()
    sys.exit(2)

domain = ''
key = ''
rrhost = ''
if not opts:
    usage()
    sys.exit(2)
for opt, arg in opts:
    if opt in ('-h', '--help'):
        usage()
        sys.exit(2)
    elif opt in ('-d', '--domain'):
        domain = arg
    elif opt in ('-k', '--key'):
        key = arg
    elif opt in ('-r', '--rrhost'):
        rrhost = arg
    else:
        usage()
        sys.exit(2)

my_ip = requests.get('https://ifconfig.me').text
list_request = 'https://www.namesilo.com/api/dnsListRecords?version=1&type=xml&key=' + key + '&domain=' + domain
response = requests.get(list_request);
xml = response.text
root = ET.fromstring(xml)
for record in root.iter('resource_record'):
   record_type = record.find('type').text
   if record_type == 'A':
       record_host = record.find('host').text
       if record_host == rrhost + domain:
           rrid = record.find('record_id').text
           update_request = 'https://www.namesilo.com/api/dnsUpdateRecord?version=1&type=xml&key=' + key + '&domain=' + domain + '&rrid=' + rrid + '&rrhost=' + rrhost + '&rrvalue=' + my_ip + '&rrttl=7207'
           response = requests.get(update_request)
           code = response.status_code
           if code!=200:
               print('Update failed--Namesilo returned status code ' + str(code))
           else:
               print(response.content)

