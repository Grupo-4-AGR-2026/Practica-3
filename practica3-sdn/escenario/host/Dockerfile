# Imagen de los PCs de RED1..RED4: Ubuntu 22.04 + utilidades de red
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive TZ=Europe/Madrid
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        iproute2 iputils-ping traceroute curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
