#!/usr/bin/env python

import yaml
import argparse
import jinja2

def read_file():
    with open("/persistent_data/application/pihole/conf/custom.list", "r") as f:
        SMRF1 = f.readlines()
    return SMRF1

parser = argparse.ArgumentParser(description='Take in Ansible variables.')
parser.add_argument('--lab-nginx-ip', type=str,
                    help='The IP of the homelab-facing nginx container',
                    required=True)
parser.add_argument('--nginx-ip', type=str,
                    help='The IP of the internet-facing nginx container',
                    required=True)
args = parser.parse_args()

initial_file = read_file()


with open("output.txt", "w") as output:
    templateLoader = jinja2.FileSystemLoader(searchpath="./")
    templateEnv = jinja2.Environment(loader=templateLoader)
    TEMPLATE_FILE = "pihole/base-custom.list"
    template = templateEnv.get_template(TEMPLATE_FILE)
    outputText = template.render(nginx_ip=args.nginx_ip)  # this is where to put args to the template renderer
    base_domains = [base_record.split(" ")[1] for base_record in outputText.split("\n")]
    output.writelines(outputText)
    output.write("\n")
    
    
    internal_services = []
    external_services = []
    with open("/persistent_data/application/docker-compose.yml", "r") as stream:
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
                    break
                record_string = record_address[i] + " " + service_name + root_domain[i] + "\n"
                if service_type[i] == 'external':
                    external_services.append(record_string)
                else:
                    internal_services.append(record_string)
        output.writelines(external_services)
        output.writelines(internal_services)
