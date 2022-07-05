#!/usr/bin/env python

import yaml
import argparse

def read_file(path):
    contents=""
    try:
        with open(path, "r") as f:
            contents = f.readlines()
    except FileNotFoundError:
        print("Pihole configuration file does not exist: creating")
    return contents


def main():
    parser = argparse.ArgumentParser(description='Take in Ansible variables.')
    parser.add_argument('--lab-nginx-ip', type=str,
                        help='The IP of the homelab-facing nginx container',
                        required=True)
    parser.add_argument('--nginx-ip', type=str,
                        help='The IP of the internet-facing nginx container',
                        required=True)
    parser.add_argument('--workspace-path', type=str,
                        help='The path which the script should use as a workspace',
                        required=True)
    parser.add_argument('--pihole-path', type=str,
                        help='The path where the script should output the pihole configuration',
                        required=True)
    args = parser.parse_args()
    
    initial = read_file(args.pihole_path)
    
    
    with open(args.pihole_path, "w") as output:
        with open(args.workspace_path + "/base-custom.list") as base:
            outputText = base.readlines()
        base_domains = [base_record.split(" ")[1][:-1] for base_record in outputText]
        output.writelines(outputText)
        
        
        internal_services = []
        external_services = []
        with open(args.workspace_path + "/docker-compose.yml", "r") as stream:
            services = yaml.safe_load(stream)['services']
            internal_services = []
            external_services = []
            for service_name, service in services.items():
                record_address = []
                service_type = [] 
                root_domain = []
                if 'internal' in service['networks']:
                    record_address.append(args.lab_nginx_ip)
                    service_type.append('internal')
                    root_domain.append(".lab.nkontur.com")
                if 'external' in service['networks']:
                    record_address.append(args.nginx_ip)
                    service_type.append('external')
                    root_domain.append(".nkontur.com")
                for i in range(len(record_address)):
                    fqdn = service_name + root_domain[i]
                    if fqdn in base_domains:
                        continue
                    record_string = record_address[i] + " " + service_name + root_domain[i] + "\n"
                    if service_type[i] == 'external':
                        external_services.append(record_string)
                    else:
                        internal_services.append(record_string)
            output.writelines(external_services)
            output.writelines(internal_services)
    current = read_file(args.pihole_path)
    if initial != current:
        print("Pihole config file changed")

if __name__ == "__main__":
    main()

