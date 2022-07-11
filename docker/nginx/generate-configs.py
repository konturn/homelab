import json
import argparse
import yaml
import copy
import os
import shutil
import time
def generate_stream_config(port_mappings, path):
    with open(path + '/stream-master-template.conf', 'r') as stream:
        result = json.load(stream)
    with open(path + '/stream-entry-template.conf', 'r') as stream:
        template = json.load(stream)
    for service_name, port in port_mappings.items():
        template_instance = copy.deepcopy(template)
        host_port = port
        container_port = port
        port_split = port.split(":")
        if len(port_split) == 2:
            host_port = port_split[0]
            container_port = port_split[1]
        template_instance['block'][0]['args'].append(host_port)
        template_instance['block'][1]['args'].append(service_name + ":" + container_port)
        result['config'][0]['parsed'][0]['block'].append(template_instance)
    with open(path + "/stream-output.conf", "w") as output:
        output.writelines(json.dumps(result))
    cmd = "crossplane build -f -d " + path + " " + path + "/stream-output.conf"
    os.system(cmd)

def generate_http_config(port_mappings, path, domain_name):
    with open(path + '/http-master-template.conf', 'r') as http:
        result = json.load(http)
    with open(path + '/http-entry-template.conf', 'r') as http:
        template = json.load(http)
    if domain_name == 'nkontur.com':
        with open(path + '/http-external-drop-in.conf', 'r') as drop_in:
            drop_in_template = json.load(drop_in)
            result['config'][0]['parsed'][0]['block'].append(drop_in_template['config'][0]['parsed'][0]['block'][0])
    for service_name, port in port_mappings.items():
        template_instance = copy.deepcopy(template)
        host_port = port
        container_port = port
        port_split = port.split(":")
        if len(port_split) == 2:
            host_port = port_split[0]
            container_port = port_split[1]
        template_instance['block'][0]['args'].append(host_port)
        template_instance['block'][1]['args'].append(service_name + "." + domain_name)
        template_instance['block'][3]['block'][0]['args'].append("http://" + service_name + ":" + container_port)
        result['config'][0]['parsed'][0]['block'].append(template_instance)
    with open(path + "/http-output.conf", "w") as output:
        output.writelines(json.dumps(result))
    cmd = "crossplane build -f -d " + path + " " + path + "/http-output.conf"
    os.system(cmd)

def generate_port_mappings(services, network):
    stream_port_mappings = {}
    http_port_mappings = {}
    for service_name, service in services.items():
        if 'networks' in service:
            if network in service['networks']:
                if 'ports' in service:
                    for port in service['ports']:
                        if port.endswith('/tcp'):
                            stream_port_mappings[service_name] = port.split('/')[0]
                        else:
                            http_port_mappings[service_name] = port
    return stream_port_mappings, http_port_mappings

def main():
    parser = argparse.ArgumentParser(description='Take in Ansible variables.')
    parser.add_argument('--workspace-path', type=str,
                        help='The path which the script should use as a workspace',
                        required=True)
    parser.add_argument('--network', type=str,
                        help='The name of the docker network which the nginx instance serves for',
                        required=True)
    args = parser.parse_args()

    with open(args.workspace_path + "/docker-compose.yml", "r") as stream:
        services = yaml.safe_load(stream)['services']

    if args.network == "internal":
        domain_name = "lab.nkontur.com"
    elif args.network == "external":
        domain_name = "nkontur.com"

    stream_port_mappings, http_port_mappings = generate_port_mappings(services, args.network)
    generate_http_config(http_port_mappings, args.workspace_path, domain_name)
    generate_stream_config(stream_port_mappings, args.workspace_path)


if __name__ == "__main__":
    main()
