# Imagen de los routers R1..R4: Ubuntu 22.04 + FRR (Free Range Routing)
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive TZ=Europe/Madrid
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        frr iproute2 iputils-ping traceroute tcpdump curl procps tzdata \
 && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
