#!/usr/bin/env python

import yaml
import argparse

def read_file():
    with open("/persistent_data/application/pihole/conf/custom.list", "r") as f:
        SMRF1 = f.readlines()
    f.close()
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

internal_services = []
external_services = []
with open("/persistent_data/application/docker-compose.yml", "r") as stream:
    services = yaml.safe_load(stream)['services']
    f = open("output.txt", "w")
    internal_services = []
    external_services = []
    for service_name, service in services.items():
        if 'internal' in service['networks']:
            internal_services.append(service_name + ".lab.nkontur.com " + args.lab_nginx_ip + "\n")
        if 'external' in service['networks']:
            external_services.append(service_name + ".nkontur.com " + args.nginx_ip + "\n")
    f.writelines(internal_services)
    f.writelines(external_services)
    f.close()
