#!/bin/bash
ip=$(curl --silent https://ifconfig.me)
ipset flush wan_ip
ipset add wan_ip $ip
