# Short-lived helper for network faults and network observation. It is started with
# --network container:<target> so tc / iptables / ss / nstat act on the target's own network
# namespace, without installing anything into the shop's containers.
FROM alpine:3.20
RUN apk add --no-cache iproute2 iproute2-ss iproute2-tc iptables
LABEL faultline.tool=nettools
