#!/bin/bash
ip=$(ip -f inet addr show {{ inet_interface_name }}|sed -En -e 's/.*inet ([0-9.]+).*/\1/p')
ipset flush wan_ip
ipset add wan_ip $ip
