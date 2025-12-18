#!/bin/bash
ip=$(/sbin/ip -f inet addr show {{ inet_interface_name }}|sed -En -e 's/.*inet ([0-9.]+).*/\1/p')
/sbin/ipset flush wan_ip
/sbin/ipset add wan_ip $ip
