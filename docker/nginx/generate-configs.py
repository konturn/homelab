import json
import argparse
import yaml
import copy
import os
import shutil
import time
import re
def generate_stream_config(port_mappings, path, output_prefix):
    with open(path.rsplit('/',1)[0] + '/stream-master-template.conf', 'r') as stream:
        result = json.load(stream)
    with open(path.rsplit('/',1)[0] + '/stream-entry-template.conf', 'r') as stream:
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
        template_instance['block'][2]['args'].append(service_name + ":" + container_port)
        result['config'][0]['parsed'][0]['block'].append(template_instance)
    result['config'][0]['file'] = output_prefix + "stream.conf"
    output_path = path + "/stream-output.conf"
    with open(output_path, "w") as output:
        output.writelines(json.dumps(result))
    cmd = "crossplane build -f -d " + path + " " + output_path
    os.system(cmd)

def generate_http_config(port_mappings, path, domain_name, output_prefix):
    with open(path.rsplit('/',1)[0] + '/http-master-template.conf', 'r') as http:
        result = json.load(http)
    with open(path.rsplit('/',1)[0] + '/http-entry-template.conf', 'r') as http:
        template = json.load(http)
    if domain_name == 'nkontur.com' or domain_name == 'lab.nkontur.com':
        drop_in_file = ''
        if domain_name == 'nkontur.com':
            drop_in_file = 'http-external-drop-in.conf'
        else:
            drop_in_file = 'http-internal-drop-in.conf'
        with open(path.rsplit('/',1)[0] + '/' + drop_in_file, 'r') as drop_in:
            drop_in_template = json.load(drop_in)
            # Handle both single object and array of server blocks
            if isinstance(drop_in_template, list):
                result['config'][0]['parsed'][0]['block'].extend(drop_in_template)
            else:
                result['config'][0]['parsed'][0]['block'].append(drop_in_template)
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
        template_instance['block'][3]['block'][1]['args'].append("http://" + service_name + ":" + container_port)
        result['config'][0]['parsed'][0]['block'].append(template_instance)
    result['config'][0]['file'] = output_prefix + "http.conf"
    output_path = path + "/http-output.conf"
    with open(output_path, "w") as output:
        output.writelines(json.dumps(result))
    cmd = "crossplane build -f -d " + path + " " + output_path
    os.system(cmd)

def generate_port_mappings(services, network):
    stream_port_mappings = {}
    http_port_mappings = {}
    for service_name, service in services.items():
        if 'networks' in service:
            if network in service['networks']:
                if 'ports' in service:
                    for port in service['ports']:
                        # Skip localhost-only bindings (not externally accessible)
                        if port.startswith('127.0.0.1:'):
                            continue
                        if port.endswith('/tcp'):
                            stream_port_mappings[service_name] = port.split('/')[0]
                        else:
                            http_port_mappings[service_name] = "443:" + port.split(':')[1]
    return stream_port_mappings, http_port_mappings

def main():
    parser = argparse.ArgumentParser(description='Take in Ansible variables.')
    parser.add_argument('--workspace-path', type=str,
                        help='The path which the script should use as a workspace',
                        required=True)
    parser.add_argument('--output-prefix', type=str,
                        help='A prefix applied to the names of the output files',
                        required=False,
                        default='')
    parser.add_argument('--network', type=str,
                        help='The name of the docker network which the nginx instance serves for',
                        required=True)
    parser.add_argument('--domain-name', type=str,
                        help='The name of the domain the server lives under',
                        required=False,
                        default='')
    parser.add_argument('--output-subdir', type=str,
                        help='The subdirectory, relative to the workspace path, to place the output files.',
                        required=False,
                        default='/')
    args = parser.parse_args()

    services = {}
    full_path = args.workspace_path + '/' + args.output_subdir
    for filename in os.listdir(full_path):
        if re.match("docker-compose*", filename):
            with open(os.path.join(full_path, filename), 'r') as stream:
                temp = yaml.safe_load(stream)['services']
                services.update(temp)

    if args.domain_name == '':
        if args.network == "internal":
            domain_name = "lab.nkontur.com"
        elif args.network == "external":
            domain_name = "nkontur.com"
        elif args.network == "iot":
            domain_name = "iot.lab.nkontur.com"
    else:
        domain_name = args.domain_name

    stream_port_mappings, http_port_mappings = generate_port_mappings(services, args.network)
    generate_http_config(http_port_mappings, full_path, domain_name, args.output_prefix)
    generate_stream_config(stream_port_mappings, full_path, args.output_prefix)


if __name__ == "__main__":
    main()
