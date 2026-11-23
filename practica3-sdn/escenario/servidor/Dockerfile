# Imagen del servidor: Ubuntu 22.04 + Node.js + npm + git.
# El codigo del servidor web NO va dentro de la imagen: el contenedor lo clona
# de GitHub cada vez que arranca (ver entrypoint.sh).
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive TZ=Europe/Madrid
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        nodejs npm git ca-certificates iproute2 iputils-ping traceroute curl tzdata \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
EXPOSE 3000
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
