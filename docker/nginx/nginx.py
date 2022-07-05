import json
import argparse
import yaml
import copy
template={}
result={}
parser = argparse.ArgumentParser(description='Take in Ansible variables.')
parser.add_argument('--workspace-path', type=str,
                    help='The path which the script should use as a workspace',
                    required=True)
args = parser.parse_args()
with open('stream.conf', 'r') as stream:
    result = json.load(stream)
with open('stream-template.conf', 'r') as stream:
    template = json.load(stream)
#json['block'][0]['args'].append(port)
#json['block'][1]['args'].append(service + ":" + port)
#print(json)
with open(args.workspace_path + "/docker-compose.yml", "r") as stream:
    services = yaml.safe_load(stream)['services']
    line_counter = 2
    for service_name, service in services.items():
        stream_port_mappings = []
        http_port_mappings = []
        if 'ports' in service:
            for port in service['ports']:
                if port.endswith('/tcp'):
                    stream_port_mappings.append(port.split('/')[0])
                else:
                    http_port_mappings.append(port.split('/')[0])
            for port in stream_port_mappings:
                template_instance = copy.deepcopy(template)
                template_instance['line'] = line_counter
                host_port = port
                container_port = port
                port_split = port.split(":")
                if len(port_split) == 2:
                    host_port = port_split[0]
                    container_port = port_split[1]
                template_instance['block'][0]['args'].append(host_port)
                template_instance['block'][1]['args'].append(service_name + ":" + container_port)
                template_instance['block'][0]['line'] = line_counter + 1
                template_instance['block'][1]['line'] = line_counter + 2
                line_counter += 4
                result['config'][0]['parsed'][0]['block'].append(template_instance)
print(json.dumps(result))
