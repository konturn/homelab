docker stop pihole2
docker container rm pihole2
docker stop pihole3
docker container rm pihole3
docker network rm docker_test
#ip link del bond0.4
ip link del vlan4-shim
