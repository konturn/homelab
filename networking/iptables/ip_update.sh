#!/bin/bash
ip=$(/sbin/ip -f inet addr show enp2s0f0|sed -En -e 's/.*inet ([0-9.]+).*/\1/p')
/sbin/ipset flush wan_ip
/sbin/ipset add wan_ip $ip
