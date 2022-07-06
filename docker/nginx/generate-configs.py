import json
import argparse
import yaml
import copy
import subprocess
import shutil
def generate_stream_config(port_mappings, path):
    with open('stream.conf', 'r') as stream:
        result = json.load(stream)
    with open('stream-template.conf', 'r') as stream:
        template = json.load(stream)
    line_counter = 2
    for service_name, port in port_mappings.items():
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
    with open(path + "/stream_template.conf", "w") as output:
        output.writelines(json.dumps(result))
    cmd = "crossplane build -f -d " + path + " " + path + "/stream_template.conf"
    subprocess.call(cmd, shell=True)

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
    parser.add_argument('--stream-config-path', type=str,
                        help='The path of the directory where the stream config file resides',
                        required=True)
#    parser.add_argument('--http-config-path', type=str,
#                        help='The path of the directory where the http config file resides',
#                        required=True)
    parser.add_argument('--network', type=str,
                        help='The name of the docker network which the nginx instance serves for',
                        required=True)
    args = parser.parse_args()

    with open(args.workspace_path + "/docker-compose.yml", "r") as stream:
        services = yaml.safe_load(stream)['services']

    stream_port_mappings, http_port_mappings = generate_port_mappings(services, args.network)
    generate_stream_config(stream_port_mappings, args.workspace_path)
    shutil.move(args.workspace_path + "/stream.conf", args.stream_config_path + "/stream.conf")


if __name__ == "__main__":
    main()
