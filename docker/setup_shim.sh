#ip link add bond0.4 link bond0 type vlan id 4
#ip link set bond0.4 up
ip link add vlan4-shim link bond0.4 type macvlan mode bridge
ip link set vlan4-shim up
ip route add 10.4.0.0/18 dev vlan4-shim
#ip route add 10.4.0.0/16 dev bond0.4
#ip route add 192.168.4.0/24 dev bond0.4
#ip addr add 10.4.0.1 dev bond0.4
