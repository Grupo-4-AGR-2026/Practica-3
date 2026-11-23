#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
 Administración y Gestión de Redes
 Práctica: Despliegue de una red definida por software (SDN) usando Docker
===============================================================================

Este script GENERA el fichero docker-compose.yml y todos los ficheros de
configuración necesarios para desplegar la red del laboratorio 3103 y, si se
le pide, la DESPLIEGA y ejecuta el PLAN DE PRUEBAS:

                         [ servidor ]   SSR-master-server (Node.js)
                              |
                             R1         router del servidor
                              |
                             R2         router de núcleo
                     _________|_________
                    |                   |
                   R3                  R4       routers de acceso
                 __|__               __|__
                |     |             |     |
             SW-V10 SW-V20       SW-V30 SW-V40  switches con VLAN
                |     |             |     |
              RED 1 RED 2         RED 3 RED 4

  Direccionamiento (el mismo que la práctica 2; tabla PLAN_REDES más abajo):
    servidor-R1 10.0.0.0/24 · R1-R2 10.0.1.0/24 · R2-R3 10.0.2.0/24 · R2-R4 10.0.3.0/24
    RED 1..4    192.168.1.0/24 ... 192.168.4.0/24
    El router tiene siempre la .1 de cada LAN; el servidor la .2 y los PCs la .10.

  * Routers (R1..R4): Ubuntu 22.04 + FRR (Free Range Routing) con rutas
    estáticas (o OSPF con --ospf). FRR escribe su log en un volumen compartido.
  * Servidor: al arrancar clona https://github.com/gisai/SSR-master-server,
    instala las dependencias con npm y lo ejecuta con Node.js (puerto 3000).
  * PCs de RED1..RED4: su puerta de enlace es su router de acceso.
  * fluentd: recoge el log de FRR de cada router y la salida de los
    contenedores (driver de logging "fluentd" de Docker).

Uso:
  python3 desplegar_red.py [desplegar]  genera los ficheros, levanta el escenario y lo prueba
  python3 desplegar_red.py generar      solo genera los ficheros (carpeta ./escenario)
  python3 desplegar_red.py probar       ejecuta el plan de pruebas sobre el escenario levantado
  python3 desplegar_red.py estado       muestra el estado de los contenedores
  python3 desplegar_red.py parar        detiene y elimina contenedores y redes

Opciones de 'generar' y 'desplegar':
  --hosts N        número de PCs en cada RED (por defecto 1)
  --ospf           usar OSPF (enrutamiento dinámico) en vez de rutas estáticas
  --puerto-web P   puerto del anfitrión para acceder al servidor web (por defecto 3000)
  --repo URL       repositorio Git del servidor web (por defecto SSR-master-server)
  --docker-antiguo generar para Docker Engine < 28 (sin gateway_mode_ipv4; el
                   script lo detecta solo, esta opción fuerza ese modo)
  --forzar         (desplegar) no comprobar si las subredes chocan con las
                   redes del anfitrión

Solo usa la biblioteca estándar de Python (3.7 o superior).
"""

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# -----------------------------------------------------------------------------
# Parámetros generales
# -----------------------------------------------------------------------------
DIR_BASE = Path(__file__).resolve().parent
DIR_ESC = DIR_BASE / "escenario"          # aquí se genera todo el escenario
PROYECTO = "p3sdn"                         # nombre del proyecto de Docker Compose
IMAGEN_UBUNTU = "ubuntu:22.04"             # FRR 8.1 y Node.js 12 desde los repositorios
IMAGEN_FLUENTD = "fluent/fluentd:v1.20-debian-1"
REPO_SSR = "https://github.com/gisai/SSR-master-server.git"
PUERTO_APP = 3000                          # puerto en el que escucha SSR-master-server
REDES_PC = ["vlan10_red1", "vlan20_red2", "vlan30_red3", "vlan40_red4"]

# -----------------------------------------------------------------------------
# PLAN DE DIRECCIONAMIENTO (topología de la práctica). Para cambiar las IPs del
# escenario basta con tocar esta tabla y volver a ejecutar el script.
#   red Docker: (subred, descripción, etiqueta, tipo, VLAN, bridge en el anfitrión)
# Los bridges de los enlaces NO se llaman br-r1-r2... a propósito: así no chocan
# con los bridges de libvirt de la práctica 2 si siguen creados en la máquina.
# -----------------------------------------------------------------------------
PLAN_REDES = {
    "vlan100_servidor": ("10.0.0.0/24", "LAN del servidor (servidor-R1)", "LAN-servidor-VLAN100", "lan", 100, "br-vlan100"),
    "enlace_r1_r2": ("10.0.1.0/24", "Enlace R1-R2", "enlace-R1-R2", "enlace", None, "br-enlace12"),
    "enlace_r2_r3": ("10.0.2.0/24", "Enlace R2-R3", "enlace-R2-R3", "enlace", None, "br-enlace23"),
    "enlace_r2_r4": ("10.0.3.0/24", "Enlace R2-R4", "enlace-R2-R4", "enlace", None, "br-enlace24"),
    "vlan10_red1": ("192.168.1.0/24", "RED 1", "RED1-VLAN10", "lan", 10, "br-vlan10"),
    "vlan20_red2": ("192.168.2.0/24", "RED 2", "RED2-VLAN20", "lan", 20, "br-vlan20"),
    "vlan30_red3": ("192.168.3.0/24", "RED 3", "RED3-VLAN30", "lan", 30, "br-vlan30"),
    "vlan40_red4": ("192.168.4.0/24", "RED 4", "RED4-VLAN40", "lan", 40, "br-vlan40"),
    "gestion": ("10.0.200.0/24", "Red de gestion (fluentd), no enrutada", "gestion", "gestion", None, "br-gestion"),
}
HOST_SERVIDOR = 2      # el servidor es la .2 de su LAN (R1 es la .1)
PRIMER_PC = 10         # los PCs de cada RED empiezan en la .10
HOST_FLUENTD = 10      # fluentd es la .10 de la red de gestión


# =============================================================================
# 1. TOPOLOGÍA Y PLAN DE NUMERACIÓN
# =============================================================================
def construir_topologia(hosts_por_red=1, ospf=False, puerto_web=3000,
                        repo=REPO_SSR, modo_gateway="nat-unprotected"):
    """Devuelve un diccionario con redes, routers, servidor, PCs y fluentd.

    Plan de numeración (tabla PLAN_REDES):
      - servidor-R1 10.0.0.0/24, enlaces R1-R2/R2-R3/R2-R4 10.0.1-3.0/24.
      - RED 1..4: 192.168.1.0/24 ... 192.168.4.0/24 (una por switch con VLAN).
      - El router tiene la .1 de cada LAN; en los enlaces, el router de arriba
        la .1 y el de abajo la .2. La última IP útil (.254) la usa el anfitrión
        Docker como pasarela del bridge.
    """
    redes = {}
    for nombre, (prefijo, descripcion, etiqueta, tipo, vlan, bridge) in PLAN_REDES.items():
        net = ipaddress.ip_network(prefijo)
        redes[nombre] = {"prefijo": str(net), "descripcion": descripcion,
                         "etiqueta": etiqueta, "tipo": tipo, "vlan": vlan,
                         "bridge": bridge,
                         "gateway_docker": str(net.broadcast_address - 1)}

    def ip(nombre_red, n):
        return str(ipaddress.ip_network(redes[nombre_red]["prefijo"]).network_address + n)

    # router -> [(red a la que se conecta, nº de host dentro de esa subred)]
    conexiones = {
        "R1": [("vlan100_servidor", 1), ("enlace_r1_r2", 1)],
        "R2": [("enlace_r1_r2", 2), ("enlace_r2_r3", 1), ("enlace_r2_r4", 1)],
        "R3": [("enlace_r2_r3", 2), ("vlan10_red1", 1), ("vlan20_red2", 1)],
        "R4": [("enlace_r2_r4", 2), ("vlan30_red3", 1), ("vlan40_red4", 1)],
    }
    roles = {"R1": "router del servidor", "R2": "router de nucleo",
             "R3": "router de acceso de RED 1 y RED 2",
             "R4": "router de acceso de RED 3 y RED 4"}

    routers = {}
    for nombre, lista in conexiones.items():
        num = nombre[1:]
        routers[nombre] = {"rol": roles[nombre],
                           "router_id": ".".join([num] * 4),
                           "interfaces": {r: ip(r, n) for r, n in lista},
                           "rutas": []}

    # El servidor llega a cada una de las demás redes de la práctica a través de R1
    # (no hay un prefijo que las agrupe a todas: 10.0.x.0 y 192.168.x.0).
    servidor = {"nombre": "servidor", "red": "vlan100_servidor",
                "ip": ip("vlan100_servidor", HOST_SERVIDOR), "router": "R1",
                "gateway": routers["R1"]["interfaces"]["vlan100_servidor"],
                "rutas": [d["prefijo"] for n, d in redes.items()
                          if d["tipo"] != "gestion" and n != "vlan100_servidor"]}

    hosts = []
    for i, nombre_red in enumerate(REDES_PC, start=1):
        router = next(r for r, d in routers.items() if nombre_red in d["interfaces"])
        for j in range(1, hosts_por_red + 1):
            hosts.append({"nombre": "red%d-pc%d" % (i, j), "red": nombre_red,
                          "lan": "RED %d" % i, "ip": ip(nombre_red, PRIMER_PC + j - 1),
                          "router": router,
                          "gateway": routers[router]["interfaces"][nombre_red]})

    topo = {"ospf": ospf, "puerto_web": puerto_web, "repo": repo,
            "hosts_por_red": hosts_por_red, "modo_gateway": modo_gateway,
            "redes": redes, "routers": routers,
            "servidor": servidor, "hosts": hosts,
            "fluentd": {"nombre": "fluentd", "red": "gestion",
                        "ip": ip("gestion", HOST_FLUENTD)}}
    calcular_rutas(topo)
    return topo


def calcular_rutas(topo):
    """Calcula las rutas estáticas de cada router.

    Para cada red que no está directamente conectada, busca (en anchura) el
    router más cercano que sí lo está y usa como siguiente salto la IP del
    router vecino por el que se llega a él.
    """
    routers, redes = topo["routers"], topo["redes"]
    enrutables = [n for n, r in redes.items() if r["tipo"] != "gestion"]

    vecinos = {r: [] for r in routers}
    for a in routers:
        for b in routers:
            if a == b:
                continue
            for n in routers[a]["interfaces"]:
                if n in routers[b]["interfaces"] and n in enrutables:
                    vecinos[a].append((b, n))

    for origen in routers:
        dist, salto = {origen: 0}, {origen: None}
        cola = [origen]
        while cola:
            actual = cola.pop(0)
            for vecino, n in vecinos[actual]:
                if vecino not in dist:
                    dist[vecino] = dist[actual] + 1
                    salto[vecino] = (vecino, n) if actual == origen else salto[actual]
                    cola.append(vecino)
        rutas = []
        for n in enrutables:
            if n in routers[origen]["interfaces"]:
                continue
            candidatos = [r for r in routers if n in routers[r]["interfaces"] and r in dist]
            if not candidatos:
                continue
            destino = min(candidatos, key=lambda r: dist[r])
            vecino, enlace = salto[destino]
            rutas.append({"prefijo": redes[n]["prefijo"],
                          "siguiente_salto": routers[vecino]["interfaces"][enlace],
                          "via": vecino, "descripcion": redes[n]["descripcion"]})
        rutas.sort(key=lambda x: ipaddress.ip_network(x["prefijo"]))
        routers[origen]["rutas"] = rutas


def camino_esperado(topo, host):
    """IPs que debe mostrar un traceroute desde 'host' hasta el servidor."""
    routers = topo["routers"]
    actual, camino, previo = host["router"], [host["gateway"]], None
    destino = topo["servidor"]["router"]
    while actual != destino:
        ruta = next(r for r in routers[actual]["rutas"]
                    if r["prefijo"] == topo["redes"][topo["servidor"]["red"]]["prefijo"])
        previo, actual = actual, ruta["via"]
        # el router responde con la IP de la interfaz por la que vuelve al origen
        enlace = next(n for n in routers[actual]["interfaces"] if n in routers[previo]["interfaces"])
        camino.append(routers[actual]["interfaces"][enlace])
    camino.append(topo["servidor"]["ip"])
    return camino


def esquema_ascii(topo):
    red, R = topo["redes"], topo["routers"]

    def ult(router, n):  # último octeto de la IP de un router en una red
        return "." + R[router]["interfaces"][n].split(".")[-1]

    lans = [red[n]["prefijo"].split("/")[0] for n in REDES_PC]
    return "\n".join([
        "                        [ servidor ]  %s" % topo["servidor"]["ip"],
        "                              |          VLAN100   %s" % red["vlan100_servidor"]["prefijo"],
        "                          %s (R1)" % ult("R1", "vlan100_servidor"),
        "                              | %-8s enlace    %s" % (ult("R1", "enlace_r1_r2"), red["enlace_r1_r2"]["prefijo"]),
        "                          %s (R2)" % ult("R2", "enlace_r1_r2"),
        "                   %3s +-------+-------+ %s" % (ult("R2", "enlace_r2_r3"), ult("R2", "enlace_r2_r4")),
        "                       |               |  enlaces   %s (R2-R3)" % red["enlace_r2_r3"]["prefijo"],
        "                   %3s |               | %-10s %s (R2-R4)" % (ult("R3", "enlace_r2_r3"), ult("R4", "enlace_r2_r4"), red["enlace_r2_r4"]["prefijo"]),
        "                    (R3)            (R4)",
        "                 .1 /   \\ .1     .1 /   \\ .1",
        "          [SW VLAN10] [SW VLAN20] [SW VLAN30] [SW VLAN40]",
        "             RED 1       RED 2       RED 3       RED 4",
        "          %-12s%-12s%-12s%s" % tuple(lans),
        "          (todas /24; router .1, PCs desde la .%d)" % PRIMER_PC,
    ])


# =============================================================================
# 2. FICHEROS FIJOS: Dockerfiles y scripts de arranque (entrypoints)
# =============================================================================
DOCKERFILE_ROUTER = r"""# Imagen de los routers R1..R4: Ubuntu 22.04 + FRR (Free Range Routing)
FROM {imagen}
ENV DEBIAN_FRONTEND=noninteractive TZ=Europe/Madrid
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        frr iproute2 iputils-ping traceroute tcpdump curl procps tzdata \
 && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
"""

ENTRYPOINT_ROUTER = r"""#!/bin/bash
# =============================================================================
#  Arranque de los routers (R1..R4) - generado por desplegar_red.py
#   1) Activa el reenvio IP
#   2) Quita la ruta por defecto que pone Docker
#   3) Prepara el fichero de log de FRR en el volumen compartido con fluentd
#   4) Copia /config/frr.conf a /etc/frr/frr.conf y arranca FRR
#      (zebra + staticd, y ospfd si OSPF=yes)
#   5) Pone descripciones a las interfaces y muestra la tabla de rutas
# =============================================================================
set -u
ROUTER="${ROUTER_NAME:-$(hostname)}"
LOG_DIR=/shared-volume/frr
LOG_FILE="$LOG_DIR/$ROUTER.log"

echo "[$ROUTER] Interfaces:"
ip -br -4 addr show

# 1) Reenvio IP (tambien se fija con 'sysctls' en el docker-compose.yml)
sysctl -qw net.ipv4.ip_forward=1 || echo "[$ROUTER] aviso: no se pudo fijar net.ipv4.ip_forward"

# 2) Docker anade una ruta por defecto hacia el anfitrion (.254, .6, ...). El router
#    no la necesita (todo esta instalado en la imagen) y, si se dejara, un error en
#    las tablas de rutas quedaria oculto porque el anfitrion reenviaria el trafico.
#    Se elimina para que solo cuenten las rutas configuradas en FRR.
ip route del default 2>/dev/null && echo "[$ROUTER] Eliminada la ruta por defecto que pone Docker"

# 3) FRR se ejecuta como usuario 'frr': el fichero de log debe existir y ser suyo
mkdir -p "$LOG_DIR"
touch "$LOG_FILE"
chown frr:frr "$LOG_FILE" 2>/dev/null || true
chmod 666 "$LOG_FILE" 2>/dev/null || true

# 4) Configuracion y arranque de FRR
install -o frr -g frr -m 640 /config/frr.conf /etc/frr/frr.conf
if [ "${OSPF:-no}" = "yes" ]; then
    sed -i 's/^ospfd=no/ospfd=yes/' /etc/frr/daemons
fi
/usr/lib/frr/frrinit.sh start || service frr start

# Esperar a que FRR haya cargado frr.conf (aparece 'log file' en la configuracion)
for _ in $(seq 1 30); do
    vtysh -c "show running-config" 2>/dev/null | grep -q "^log file" && break
    sleep 1
done

# 5) Descripcion de cada interfaz (el orden ethX lo decide Docker, asi que se
#    busca la interfaz por su IP). Formato: "IP=descripcion IP=descripcion ..."
echo "[$ROUTER] Interfaces de FRR:"
for par in ${DESCRIPCIONES:-}; do
    ip_if="${par%%=*}"
    desc="${par#*=}"
    ifname=$(ip -o -4 addr show | awk -v ip="$ip_if" '{split($4, a, "/"); if (a[1] == ip) {sub(/@.*/, "", $2); print $2}}')
    if [ -n "$ifname" ]; then
        vtysh -c "configure terminal" -c "interface $ifname" -c "description $desc" >/dev/null 2>&1
        printf '   %-8s %-15s %s\n' "$ifname" "$ip_if" "$desc"
    fi
done

echo "[$ROUTER] Tabla de rutas:"
vtysh -c "show ip route"

# Mantener el contenedor en marcha (con 'init: true' se para limpiamente)
exec sleep infinity
"""

DOCKERFILE_HOST = r"""# Imagen de los PCs de RED1..RED4: Ubuntu 22.04 + utilidades de red
FROM {imagen}
ENV DEBIAN_FRONTEND=noninteractive TZ=Europe/Madrid
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        iproute2 iputils-ping traceroute curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
"""

ENTRYPOINT_HOST = r"""#!/bin/bash
# =============================================================================
#  Arranque de los PCs de RED1..RED4 - generado por desplegar_red.py
#  Su puerta de enlace por defecto pasa a ser su router de acceso (R3 o R4)
#  en lugar del anfitrion Docker.
# =============================================================================
set -u
echo "[$(hostname)] Interfaces:"
ip -br -4 addr show
ip route replace default via "$GATEWAY"
echo "[$(hostname)] Tabla de rutas:"
ip route
exec sleep infinity
"""

DOCKERFILE_SERVIDOR = r"""# Imagen del servidor: Ubuntu 22.04 + Node.js + npm + git.
# El codigo del servidor web NO va dentro de la imagen: el contenedor lo clona
# de GitHub cada vez que arranca (ver entrypoint.sh).
FROM {imagen}
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
"""

ENTRYPOINT_SERVIDOR = r"""#!/bin/bash
# =============================================================================
#  Arranque del servidor - generado por desplegar_red.py
#   1) Ruta hacia las redes de la practica a traves de R1
#   2) Clona (o actualiza) SSR-master-server desde GitHub
#   3) Instala las dependencias con npm
#   4) Ejecuta el servidor con Node.js (puerto 3000)
# =============================================================================
set -u
REPO_URL="${REPO_URL:-https://github.com/gisai/SSR-master-server.git}"
APP_DIR=/srv/SSR-master-server

echo "[servidor] Interfaces:"
ip -br -4 addr show

# 1) La ruta por defecto sigue apuntando al anfitrion Docker (salida a Internet
#    para clonar el repositorio); el resto de redes de la practica (RUTAS) se
#    alcanzan a traves de R1.
for red in ${RUTAS:-}; do
    ip route replace "$red" via "$ROUTER"
done
echo "[servidor] Tabla de rutas:"
ip route

# 2) Clonar el repositorio (o actualizarlo si el contenedor se reinicia)
if [ -d "$APP_DIR/.git" ]; then
    echo "[servidor] Actualizando $APP_DIR (git pull)"
    git -C "$APP_DIR" pull --ff-only || echo "[servidor] aviso: no se pudo actualizar; uso la copia local"
else
    for intento in 1 2 3 4 5; do
        echo "[servidor] Clonando $REPO_URL (intento $intento)"
        git clone --depth 1 "$REPO_URL" "$APP_DIR" && break
        rm -rf "$APP_DIR"
        sleep 5
    done
fi
if [ ! -f "$APP_DIR/package.json" ]; then
    echo "[servidor] ERROR: no se pudo clonar $REPO_URL (sin acceso a Internet?)"
    exit 1
fi
cd "$APP_DIR"
echo "[servidor] Version: $(git log -1 --format='%h %s')"

# 3) Dependencias (express, ejs, morgan, ...)
for intento in 1 2 3; do
    npm install --no-audit --no-fund && break
    echo "[servidor] npm install fallo (intento $intento); reintento..."
    sleep 5
done
mkdir -p public/logs    # aqui guarda /record los CSV con los datos recibidos

# 4) Ejecutar. OJO: 'npm start' (nodemon ./bin/www) falla en este repositorio
#    con "Port 3000 is already in use": app.js ya hace app.listen(3000) y
#    bin/www vuelve a escuchar en el 3000. Por eso se arranca app.js.
echo "[servidor] Arrancando SSR-master-server: node app.js (puerto 3000)"
exec node app.js
"""


# =============================================================================
# 3. FICHEROS GENERADOS A PARTIR DE LA TOPOLOGÍA
# =============================================================================
def generar_frr_conf(topo, nombre):
    r = topo["routers"][nombre]
    redes = topo["redes"]
    L = ["! " + "=" * 70,
         "! FRR (Free Range Routing) - %s: %s" % (nombre, r["rol"]),
         "! Generado por desplegar_red.py. Se copia a /etc/frr/frr.conf al arrancar.",
         "! " + "=" * 70,
         "frr defaults traditional",
         "hostname %s" % nombre,
         "!",
         "! Log en el volumen compartido con fluentd, con milisegundos",
         "log file /shared-volume/frr/%s.log debugging" % nombre,
         "log timestamp precision 3",
         "!",
         "banner motd line %s - %s - Practica SDN con Docker (AGR)" % (nombre, r["rol"]),
         "!",
         "! Depuracion de zebra: deja en el log las altas y bajas de rutas",
         "debug zebra events",
         "debug zebra rib",
         "!",
         "ip forwarding",
         "!",
         "! Redes directamente conectadas:"]
    for n, ip in r["interfaces"].items():
        L.append("!   %-18s %-15s %s" % (redes[n]["prefijo"], ip, redes[n]["descripcion"]))
    L.append("!")
    if topo["ospf"]:
        L += ["! Enrutamiento dinamico OSPF: todas las redes en el area 0",
              "router ospf",
              " ospf router-id %s" % r["router_id"],
              " log-adjacency-changes detail"]
        for n in r["interfaces"]:
            L.append(" network %s area 0" % redes[n]["prefijo"])
        L.append("!")
    else:
        L.append("! Rutas estaticas:  ip route <red destino> <siguiente salto>")
        for ruta in r["rutas"]:
            L.append("! -> %s (por %s)" % (ruta["descripcion"], ruta["via"]))
            L.append("ip route %s %s" % (ruta["prefijo"], ruta["siguiente_salto"]))
        L.append("!")
    L.append("line vty")
    L.append("!")
    return "\n".join(L) + "\n"


def generar_fluent_conf(topo):
    L = ["# " + "=" * 70,
         "#  fluentd - recogida de logs del escenario SDN",
         "#  Generado por desplegar_red.py",
         "#  Resultados (en el anfitrion): escenario/fluentd/output/",
         "#     frr/<router>.<fecha>.log      log de FRR de cada router",
         "#     docker/<contenedor>.<fecha>.log  salida de los contenedores",
         "#     fluentd/fluentd.<fecha>.log   log interno de fluentd",
         "# " + "=" * 70,
         "",
         "# --- Entrada 1: driver de logging 'fluentd' de Docker (routers y servidor)",
         "<source>",
         "  @type forward",
         "  port 24224",
         "  bind 0.0.0.0",
         "</source>",
         ""]
    for nombre in topo["routers"]:
        L += ["# --- Entrada: log de FRR de %s (fichero en el volumen compartido)" % nombre,
              "<source>",
              "  @type tail",
              "  path /shared-volume/frr/%s.log" % nombre,
              "  pos_file /shared-volume/frr/fluentd-%s.pos" % nombre,
              "  tag frr.%s" % nombre,
              "  read_from_head true",
              "  refresh_interval 5",
              "  <parse>",
              "    @type none",
              "  </parse>",
              "</source>",
              ""]

    def salida(patron, ruta, buffer, chunk_keys="tag,time", sangria=""):
        s = sangria
        return [s + "<match %s>" % patron,
                s + "  @type file",
                s + "  path %s" % ruta,
                s + "  append true",
                s + "  <buffer %s>" % chunk_keys,
                s + "    @type file",
                s + "    path /fluentd/buffer/%s" % buffer,
                s + "    timekey 1d",
                s + "    timekey_wait 0",
                s + "    flush_mode interval",
                s + "    flush_interval 5s",
                s + "    flush_at_shutdown true",
                s + "  </buffer>",
                s + "</match>"]

    L += ["# --- Salida 1: logs de FRR -> /fluentd/output/frr/<router>.<fecha>.log"]
    L += salida("frr.**", "/fluentd/output/frr/${tag[1]}", "frr")
    L += ["", "# --- Salida 2: salida de los contenedores -> /fluentd/output/docker/<contenedor>.<fecha>.log"]
    L += salida("docker.**", "/fluentd/output/docker/${tag[1]}", "docker")
    L += ["", "# --- Log interno de fluentd -> /fluentd/output/fluentd/fluentd.<fecha>.log",
          "<label @FLUENT_LOG>"]
    L += salida("fluent.**", "/fluentd/output/fluentd/fluentd", "fluent", "time", "  ")
    L += ["</label>"]
    return "\n".join(L) + "\n"


def generar_compose(topo):
    redes, routers = topo["redes"], topo["routers"]
    srv, flu = topo["servidor"], topo["fluentd"]
    L = []
    a = L.append

    def logging(tag):
        return ["    logging:                        # salida del contenedor -> fluentd",
                "      driver: fluentd",
                "      options:",
                '        fluentd-address: "127.0.0.1:24224"',
                '        fluentd-async: "true"',
                '        tag: "docker.%s"' % tag]

    def redes_servicio(pares):
        out = ["    networks:"]
        for n, ip in pares:
            out += ["      %s:" % n, "        ipv4_address: %s" % ip]
        return out

    a("# " + "=" * 76)
    a("#  docker-compose.yml  -  GENERADO por desplegar_red.py")
    a("#  (no editar a mano: cambia el script y ejecuta 'python3 desplegar_red.py generar')")
    a("#")
    for linea in esquema_ascii(topo).splitlines():
        a("#  " + linea)
    a("#")
    a("#  Enrutamiento: %s con FRR (Free Range Routing)"
      % ("OSPF (dinamico)" if topo["ospf"] else "rutas estaticas"))
    a("#  Logs: fluentd recoge el log de FRR de cada router y la salida de los contenedores")
    a("# " + "=" * 76)
    a('version: "2.4"   # solo por compatibilidad con docker-compose v1; Compose v2 lo ignora')
    a("")
    a("services:")
    a("")

    # ---- fluentd ---------------------------------------------------------
    a("  # " + "-" * 72)
    a("  # fluentd: servicio de soporte (no forma parte del mapa de red).")
    a("  # Lee el log de FRR de cada router (volumen compartido) y recibe la salida")
    a("  # de los contenedores a traves del driver de logging de Docker (puerto 24224).")
    a("  fluentd:")
    a("    image: %s" % IMAGEN_FLUENTD)
    a("    container_name: fluentd")
    a("    hostname: fluentd")
    a("    user: root                       # para leer los logs de FRR y escribir en ./fluentd/output")
    a("    volumes:")
    a("      - ./fluentd/conf:/fluentd/etc:ro            # fluent.conf")
    a("      - ./fluentd/shared-volume:/shared-volume    # logs de FRR de los routers")
    a("      - ./fluentd/output:/fluentd/output          # datos recogidos")
    a("    ports:")
    a('      - "127.0.0.1:24224:24224"     # aqui envia Docker la salida de los contenedores')
    L += redes_servicio([(flu["red"], flu["ip"])])
    a("    restart: unless-stopped")
    a("")

    # ---- routers ---------------------------------------------------------
    for nombre, r in routers.items():
        a("  # " + "-" * 72)
        a("  # %s - %s" % (nombre, r["rol"]))
        for n, ip in r["interfaces"].items():
            a("  #   %-15s %-18s %s" % (ip, redes[n]["prefijo"], redes[n]["descripcion"]))
        a("  %s:" % nombre.lower())
        a("    build: ./router")
        a("    container_name: %s" % nombre)
        a("    hostname: %s" % nombre)
        a("    privileged: true                 # zebra (FRR) instala rutas en el kernel")
        a("    init: true")
        a("    sysctls:")
        a("      net.ipv4.ip_forward: 1         # el contenedor reenvia paquetes: es un router")
        a("    environment:")
        a("      ROUTER_NAME: %s" % nombre)
        a('      OSPF: "%s"' % ("yes" if topo["ospf"] else "no"))
        desc = " ".join("%s=%s" % (ip, redes[n]["etiqueta"]) for n, ip in r["interfaces"].items())
        a('      DESCRIPCIONES: "%s"' % desc)
        a("    volumes:")
        a("      - ./config/%s:/config:ro                  # frr.conf de este router" % nombre)
        a("      - ./fluentd/shared-volume:/shared-volume  # FRR escribe aqui su log (lo lee fluentd)")
        a("    depends_on:")
        a("      - fluentd")
        L += logging(nombre)
        L += redes_servicio(list(r["interfaces"].items()))
        a("    restart: unless-stopped")
        a("")

    # ---- servidor --------------------------------------------------------
    a("  # " + "-" * 72)
    a("  # Servidor web: al arrancar clona SSR-master-server de GitHub, instala sus")
    a("  # dependencias con npm y lo ejecuta con Node.js en el puerto %d." % PUERTO_APP)
    a("  servidor:")
    a("    build: ./servidor")
    a("    container_name: servidor")
    a("    hostname: servidor")
    a("    init: true")
    a("    cap_add:")
    a("      - NET_ADMIN                    # para anadir la ruta hacia las redes de la practica")
    a("    environment:")
    a('      REPO_URL: "%s"' % topo["repo"])
    a("      # el resto de redes de la practica se alcanzan a traves de R1")
    a('      RUTAS: "%s"' % " ".join(srv["rutas"]))
    a('      ROUTER: "%s"' % srv["gateway"])
    a("    ports:")
    a('      - "%d:%d"                    # acceso desde el anfitrion: http://localhost:%d'
      % (topo["puerto_web"], PUERTO_APP, topo["puerto_web"]))
    a("    depends_on:")
    a("      - fluentd")
    a("      - r1")
    L += logging("servidor")
    a("    healthcheck:                     # 'healthy' cuando responde la web")
    a('      test: ["CMD", "curl", "-fsS", "-o", "/dev/null", "http://localhost:%d/"]' % PUERTO_APP)
    a("      interval: 10s")
    a("      timeout: 5s")
    a("      retries: 30")
    a("      start_period: 60s")
    L += redes_servicio([(srv["red"], srv["ip"])])
    a("    restart: unless-stopped")
    a("")

    # ---- PCs de las RED 1..4 --------------------------------------------
    for h in topo["hosts"]:
        a("  # " + "-" * 72)
        a("  # %s - PC de %s (switch VLAN%d), puerta de enlace %s (%s)"
          % (h["nombre"], h["lan"], redes[h["red"]]["vlan"], h["gateway"], h["router"]))
        a("  %s:" % h["nombre"])
        a("    build: ./host")
        a("    container_name: %s" % h["nombre"])
        a("    hostname: %s" % h["nombre"])
        a("    init: true")
        a("    cap_add:")
        a("      - NET_ADMIN                    # para cambiar la ruta por defecto")
        a("    environment:")
        a('      GATEWAY: "%s"' % h["gateway"])
        a("    extra_hosts:")
        a('      - "servidor:%s"        # resolucion del nombre del servidor' % srv["ip"])
        a("    depends_on:")
        a("      - %s" % h["router"].lower())
        L += redes_servicio([(h["red"], h["ip"])])
        a("    restart: unless-stopped")
        a("")

    # ---- redes -----------------------------------------------------------
    a("# " + "=" * 76)
    a("# Redes. Cada switch con VLAN del dibujo es una red bridge de Docker")
    a("# independiente (un dominio de difusion aislado = una VLAN); su bridge en el")
    a("# anfitrion se llama br-vlanXX. La ultima IP util de cada subred es la del")
    a("# anfitrion Docker (gateway), para dejar la .1 libre para el router.")
    if topo.get("modo_gateway"):
        a("#")
        a("# gateway_mode_ipv4=%s: desde Docker 28, Docker descarta los paquetes" % topo["modo_gateway"])
        a("# dirigidos a la IP de un contenedor que entran por un bridge distinto del suyo")
        a("# (reglas 'raw' DIRECT ACCESS FILTERING). Eso rompe el trafico que encaminan los")
        a("# routers de la practica. Este modo mantiene el NAT pero no crea esas reglas.")
        a("# (Docker 27 no acepta este valor: regenera con --docker-antiguo.)")
    a("# " + "=" * 76)
    a("networks:")
    for n, d in redes.items():
        extra = (" (VLAN %d)" % d["vlan"]) if d["vlan"] else ""
        a("  %s:                     # %s%s" % (n, d["descripcion"], extra))
        a("    driver: bridge")
        a("    driver_opts:")
        a("      com.docker.network.bridge.name: %s" % d["bridge"])
        if topo.get("modo_gateway"):
            a("      com.docker.network.bridge.gateway_mode_ipv4: %s" % topo["modo_gateway"])
        a("    ipam:")
        a("      config:")
        a("        - subnet: %s" % d["prefijo"])
        a("          gateway: %s" % d["gateway_docker"])
    return "\n".join(L) + "\n"


def escribir(ruta, contenido, ejecutable=False):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "w", encoding="utf-8", newline="\n") as f:
        f.write(contenido)
    if ejecutable:
        try:
            os.chmod(ruta, 0o755)
        except OSError:
            pass


def crear_directorio_datos(ruta):
    """Directorios compartidos con los contenedores (deben poder escribir en ellos)."""
    ruta.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(ruta, 0o777)
    except OSError:
        pass


def generar(topo):
    """Escribe todo el escenario en ./escenario."""
    img = {"imagen": IMAGEN_UBUNTU}
    escribir(DIR_ESC / "docker-compose.yml", generar_compose(topo))
    escribir(DIR_ESC / ".env", "COMPOSE_PROJECT_NAME=%s\n" % PROYECTO)
    escribir(DIR_ESC / "router" / "Dockerfile", DOCKERFILE_ROUTER.format(**img))
    escribir(DIR_ESC / "router" / "entrypoint.sh", ENTRYPOINT_ROUTER, ejecutable=True)
    escribir(DIR_ESC / "host" / "Dockerfile", DOCKERFILE_HOST.format(**img))
    escribir(DIR_ESC / "host" / "entrypoint.sh", ENTRYPOINT_HOST, ejecutable=True)
    escribir(DIR_ESC / "servidor" / "Dockerfile", DOCKERFILE_SERVIDOR.format(**img))
    escribir(DIR_ESC / "servidor" / "entrypoint.sh", ENTRYPOINT_SERVIDOR, ejecutable=True)
    for nombre in topo["routers"]:
        escribir(DIR_ESC / "config" / nombre / "frr.conf", generar_frr_conf(topo, nombre))
    escribir(DIR_ESC / "fluentd" / "conf" / "fluent.conf", generar_fluent_conf(topo))
    for sub in ("shared-volume", "shared-volume/frr", "output", "output/frr",
                "output/docker", "output/fluentd"):
        crear_directorio_datos(DIR_ESC / "fluentd" / sub)
    escribir(DIR_ESC / "topologia.json", json.dumps(topo, indent=2, ensure_ascii=False))


def imprimir_plan(topo):
    redes, routers = topo["redes"], topo["routers"]
    print()
    print(esquema_ascii(topo))
    print()
    print("PLAN DE NUMERACION")
    print("  %-18s %-18s %-5s %-16s %s" % ("Red Docker", "Subred", "VLAN", "Anfitrion(gw)", "Equipos"))
    for n, d in redes.items():
        equipos = ["%s %s" % (r, ip) for r, x in routers.items()
                   for m, ip in x["interfaces"].items() if m == n]
        if topo["servidor"]["red"] == n:
            equipos.append("servidor %s" % topo["servidor"]["ip"])
        equipos += ["%s %s" % (h["nombre"], h["ip"]) for h in topo["hosts"] if h["red"] == n]
        if topo["fluentd"]["red"] == n:
            equipos.append("fluentd %s" % topo["fluentd"]["ip"])
        print("  %-18s %-18s %-5s %-16s %s" % (n, d["prefijo"], d["vlan"] or "-",
                                               d["gateway_docker"], ", ".join(equipos)))
    print()
    if topo["ospf"]:
        print("ENRUTAMIENTO: OSPF area 0 (las rutas se aprenden dinamicamente)")
    else:
        print("RUTAS ESTATICAS (FRR)")
        for nombre, r in routers.items():
            for ruta in r["rutas"]:
                print("  %-3s ip route %-17s %-14s ! %s (por %s)"
                      % (nombre, ruta["prefijo"], ruta["siguiente_salto"],
                         ruta["descripcion"], ruta["via"]))
    srv = topo["servidor"]
    for prefijo in srv["rutas"]:
        print("  servidor: ip route %-17s via %s (R1)" % (prefijo, srv["gateway"]))
    print("  servidor: default via anfitrion Docker (salida a Internet)")
    for h in topo["hosts"]:
        print("  %s: default via %s (%s)" % (h["nombre"], h["gateway"], h["router"]))
    print()


# =============================================================================
# 4. DOCKER: despliegue, estado, parada
# =============================================================================
def comando_compose():
    if shutil.which("docker") is None:
        sys.exit("ERROR: no se encuentra 'docker'. Instala Docker Engine o Docker Desktop.")
    r = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
    if r.returncode == 0:
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    sys.exit("ERROR: no se encuentra Docker Compose ('docker compose' ni 'docker-compose').")


def compose(*args, comprobar=True):
    cmd = comando_compose() + list(args)
    print("$ " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(DIR_ESC))
    if comprobar and r.returncode != 0:
        sys.exit("ERROR: fallo al ejecutar: " + " ".join(cmd))
    return r.returncode


def estado_contenedor(nombre):
    r = subprocess.run(["docker", "inspect", "-f",
                        "{{.State.Status}}{{if .State.Health}} {{.State.Health.Status}}{{end}}",
                        nombre], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else "no existe"


def esperar_servidor(limite=600):
    """Espera a que el servidor clone, instale y responda (healthcheck)."""
    print("\nEsperando a que el servidor clone SSR-master-server, instale dependencias y arranque...")
    inicio, anterior = time.time(), None
    while time.time() - inicio < limite:
        estado = estado_contenedor("servidor")
        if estado in ("running healthy", "running"):   # "running" = sin healthcheck
            print("  servidor: %s (%.0f s)" % (estado, time.time() - inicio))
            return True
        log = subprocess.run(["docker", "logs", "--tail", "1", "servidor"],
                             capture_output=True, text=True)
        ultima = (log.stdout + log.stderr).strip().splitlines()
        linea = "  servidor: %s | %s" % (estado, ultima[-1][:90] if ultima else "")
        if linea != anterior:
            print(linea)
            anterior = linea
        time.sleep(5)
    print("  AVISO: el servidor no ha respondido en %d s (mira 'docker logs servidor')." % limite)
    return False


def cargar_topologia():
    fichero = DIR_ESC / "topologia.json"
    if not fichero.exists():
        sys.exit("ERROR: no hay escenario generado. Ejecuta primero: python3 desplegar_red.py")
    with open(fichero, encoding="utf-8") as f:
        return json.load(f)


def version_docker():
    """Versión mayor del Docker Engine (None si no se puede consultar)."""
    try:
        r = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.match(r"(\d+)\.", r.stdout.strip())
    return int(m.group(1)) if (r.returncode == 0 and m) else None


def modo_gateway(args):
    """Docker >= 28 necesita gateway_mode_ipv4=nat-unprotected en las redes para
    que los contenedores puedan hacer de router entre ellas; Docker 27 no acepta
    ese valor y las versiones anteriores no lo necesitan."""
    if args.docker_antiguo:
        print("Docker < 28 indicado (--docker-antiguo): redes sin gateway_mode_ipv4.")
        return None
    v = version_docker()
    if v is not None and v < 28:
        print("Docker Engine %d detectado: redes sin gateway_mode_ipv4." % v)
        return None
    print("Docker Engine %s: redes con gateway_mode_ipv4=nat-unprotected."
          % (v if v is not None else "no detectado (se supone >= 28)"))
    return "nat-unprotected"


def redes_del_anfitrion():
    """Redes IPv4 de la tabla de rutas del anfitrión (solo Linux): [(interfaz, red)]."""
    try:
        with open("/proc/net/route") as f:
            lineas = f.read().splitlines()[1:]
    except OSError:
        return []          # Windows / macOS: Docker va dentro de una VM, no hay choque
    redes = []
    for linea in lineas:
        campos = linea.split()
        if len(campos) < 8:
            continue
        destino = ipaddress.IPv4Address(int(campos[1], 16).to_bytes(4, sys.byteorder))
        mascara = ipaddress.IPv4Address(int(campos[7], 16).to_bytes(4, sys.byteorder))
        if int(mascara) == 0:
            continue       # ruta por defecto
        redes.append((campos[0], ipaddress.ip_network("%s/%s" % (destino, mascara), strict=False)))
    return redes


def comprobar_solapes(topo):
    """Subredes del escenario que coinciden con redes que ya usa el anfitrión
    (p. ej. una LAN 192.168.1.0/24 en casa). Se ignoran los bridges del propio
    escenario, que existen si ya estaba desplegado."""
    propios = {d["bridge"] for d in topo["redes"].values()}
    choques = []
    for interfaz, red_anfitrion in redes_del_anfitrion():
        if interfaz in propios:
            continue
        for nombre, d in topo["redes"].items():
            if red_anfitrion.overlaps(ipaddress.ip_network(d["prefijo"])):
                choques.append((d["prefijo"], nombre, str(red_anfitrion), interfaz))
    return choques


def cmd_generar(args):
    topo = construir_topologia(args.hosts, args.ospf, args.puerto_web,
                               args.repo, modo_gateway(args))
    generar(topo)
    print("Escenario generado en %s" % DIR_ESC)
    imprimir_plan(topo)
    return topo


def cmd_desplegar(args):
    topo = cmd_generar(args)
    choques = [] if args.forzar else comprobar_solapes(topo)
    if choques:
        print("ERROR: estas subredes del escenario ya se usan en el anfitrion:")
        for prefijo, nombre, red_anfitrion, interfaz in choques:
            print("  %-16s (%s)  choca con  %s en la interfaz %s" % (prefijo, nombre, red_anfitrion, interfaz))
        print("Si se crea el bridge de Docker, el anfitrion puede perder la conexion con esa red.")
        print("Cambia esa subred en PLAN_REDES (al principio del script) o, si sabes lo que")
        print("haces, despliega con --forzar.")
        return 1
    print("Construyendo imagenes y levantando el escenario (la primera vez tarda unos minutos)...")
    compose("up", "-d", "--build", "--remove-orphans")
    esperar_servidor()
    print("\nEscenario desplegado:")
    print("  - Web del servidor desde el anfitrion: http://localhost:%d" % topo["puerto_web"])
    print("  - Consola de un router:   docker exec -it R1 vtysh   (show ip route)")
    print("  - Consola de un PC:       docker exec -it red1-pc1 bash")
    print("  - Logs recogidos por fluentd: %s" % (DIR_ESC / "fluentd" / "output"))
    if not args.sin_pruebas:
        time.sleep(6)  # fluentd vuelca a disco cada 5 s
        return cmd_probar(args)
    return 0


def cmd_estado(args):
    compose("ps", comprobar=False)
    return 0


def cmd_parar(args):
    compose("down", "--remove-orphans", comprobar=False)
    if args.borrar_datos:
        for sub in ("shared-volume", "output"):
            shutil.rmtree(DIR_ESC / "fluentd" / sub, ignore_errors=True)
        print("Datos de fluentd borrados.")
    return 0


# =============================================================================
# 5. PLAN DE PRUEBAS
# =============================================================================
def cmd_probar(args):
    topo = cargar_topologia()
    routers, redes, srv = topo["routers"], topo["redes"], topo["servidor"]
    resultados, informe = [], []

    def dexec(contenedor, *cmd, timeout=60):
        try:
            r = subprocess.run(["docker", "exec", contenedor] + list(cmd),
                               capture_output=True, text=True, timeout=timeout)
            return r.returncode, (r.stdout + r.stderr)
        except subprocess.TimeoutExpired:
            return 124, "timeout"

    def prueba(descripcion, ok, mostrar=True):
        resultados.append((descripcion, ok))
        informe.append(("[OK]    " if ok else "[FALLO] ") + descripcion)
        if mostrar or not ok:
            print(("  [OK]    " if ok else "  [FALLO] ") + descripcion)

    def titulo(texto):
        print("\n== %s ==" % texto)
        informe.append("\n== %s ==" % texto)

    # 1. Contenedores ------------------------------------------------------
    titulo("1. Contenedores en ejecucion")
    nombres = ["fluentd"] + list(routers) + ["servidor"] + [h["nombre"] for h in topo["hosts"]]
    for n in nombres:
        estado = estado_contenedor(n)
        prueba("%-10s %s" % (n, estado), estado.startswith("running"))
    if estado_contenedor("servidor") == "running starting":
        esperar_servidor(limite=300)    # todavía clonando / instalando dependencias

    # 2. Tablas de rutas de los routers -----------------------------------
    titulo("2. Tablas de encaminamiento de los routers (FRR)")
    codigo = "O" if topo["ospf"] else "S"
    for nombre, r in routers.items():
        _, salida = dexec(nombre, "vtysh", "-c", "show ip route")
        informe.append("--- %s# show ip route\n%s" % (nombre, salida.rstrip()))
        for ruta in r["rutas"]:
            patron = re.compile(r"^%s>\*\s*%s\s.*via %s\b" % (codigo, re.escape(ruta["prefijo"]),
                                                             re.escape(ruta["siguiente_salto"])), re.M)
            prueba("%s: %s %s via %s" % (nombre, codigo, ruta["prefijo"], ruta["siguiente_salto"]),
                   bool(patron.search(salida)))

    # 3. Rutas de PCs y servidor ------------------------------------------
    titulo("3. Rutas de los PCs y del servidor")
    _, salida = dexec("servidor", "ip", "route")
    informe.append("--- servidor$ ip route\n" + salida.rstrip())
    for prefijo in srv["rutas"]:
        patron = re.compile(r"^%s via %s\s" % (re.escape(prefijo), re.escape(srv["gateway"])), re.M)
        prueba("servidor: %s via %s" % (prefijo, srv["gateway"]), bool(patron.search(salida)))
    for h in topo["hosts"]:
        _, salida = dexec(h["nombre"], "ip", "route")
        informe.append("--- %s$ ip route\n%s" % (h["nombre"], salida.rstrip()))
        prueba("%s: default via %s" % (h["nombre"], h["gateway"]),
               bool(re.search(r"^default via %s\s" % re.escape(h["gateway"]), salida, re.M)))

    # 4. Conectividad global (ping) ---------------------------------------
    titulo("4. Conectividad global (ping desde cada PC a servidor, routers y resto de PCs)")
    destinos = [("servidor", srv["ip"])]
    for nombre, r in routers.items():
        destinos += [("%s(%s)" % (nombre, redes[n]["etiqueta"]), ip) for n, ip in r["interfaces"].items()]
    destinos += [(h["nombre"], h["ip"]) for h in topo["hosts"]]
    for h in topo["hosts"]:
        lista = [(n, ip) for n, ip in destinos if ip != h["ip"]]
        script = "; ".join("ping -c 1 -W 2 %s >/dev/null 2>&1 && echo OK %s || echo FALLO %s"
                           % (ip, ip, ip) for _, ip in lista)
        _, salida = dexec(h["nombre"], "sh", "-c", script, timeout=120)
        ok_ips = set(re.findall(r"^OK (\S+)$", salida, re.M))
        for n, ip in lista:
            prueba("%s -> %s %s" % (h["nombre"], n, ip), ip in ok_ips, mostrar=False)
        print("  %-9s %2d/%d destinos alcanzables" % (h["nombre"], len(ok_ips), len(lista)))

    # 5. Camino de los paquetes (traceroute) ------------------------------
    titulo("5. Traceroute desde cada RED hasta el servidor")
    vistos = set()
    for h in topo["hosts"]:
        if h["red"] in vistos:
            continue
        vistos.add(h["red"])
        _, salida = dexec(h["nombre"], "traceroute", "-n", "-q", "1", "-w", "2", srv["ip"])
        informe.append("--- %s$ traceroute -n %s\n%s" % (h["nombre"], srv["ip"], salida.rstrip()))
        saltos = re.findall(r"^\s*\d+\s+(\d+\.\d+\.\d+\.\d+)", salida, re.M)
        esperado = camino_esperado(topo, h)
        prueba("%s: %s" % (h["nombre"], " -> ".join(saltos) or "sin respuesta"), saltos == esperado)

    # 6. Servicio web desde los PCs ---------------------------------------
    titulo("6. Servicio web SSR-master-server desde cada PC")
    for h in topo["hosts"]:
        _, codigo_http = dexec(h["nombre"], "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                               "--max-time", "5", "http://servidor:%d/" % PUERTO_APP)
        prueba("%s: GET http://servidor:%d/ -> HTTP %s" % (h["nombre"], PUERTO_APP, codigo_http.strip()),
               codigo_http.strip() == "200")
        url = ("http://servidor:%d/record?id_nodo=%s&temperatura=21.5&humedad=45.2&co2=400&volatiles=12"
               % (PUERTO_APP, h["nombre"]))
        _, respuesta = dexec(h["nombre"], "curl", "-s", "--max-time", "5", url)
        prueba("%s: GET /record (el servidor guarda la medida)" % h["nombre"], "Saving" in respuesta)

    # 7. Acceso desde el anfitrión ----------------------------------------
    titulo("7. Acceso al servidor desde la maquina anfitriona")
    url = "http://localhost:%d/" % topo["puerto_web"]
    try:
        sin_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with sin_proxy.open(url, timeout=5) as resp:
            prueba("anfitrion: GET %s -> HTTP %d" % (url, resp.status), resp.status == 200)
    except Exception as e:  # noqa: BLE001
        prueba("anfitrion: GET %s -> %s" % (url, e), False)

    # 8. fluentd -----------------------------------------------------------
    titulo("8. Recogida de logs con fluentd")
    dir_frr = DIR_ESC / "fluentd" / "shared-volume" / "frr"
    dir_out = DIR_ESC / "fluentd" / "output"
    time.sleep(1)
    for nombre in routers:
        log = dir_frr / ("%s.log" % nombre)
        prueba("FRR de %s escribe su log (%s)" % (nombre, log.relative_to(DIR_BASE)),
               log.exists() and log.stat().st_size > 0)
        recogidos = [f for f in (dir_out / "frr").glob("%s.*.log" % nombre) if f.stat().st_size > 0]
        prueba("fluentd ha recogido el log de FRR de %s (%s)" % (
            nombre, ", ".join(str(f.relative_to(DIR_BASE)) for f in recogidos) or "nada"),
            bool(recogidos))
    recogidos = [f for f in (dir_out / "docker").glob("*.log") if f.stat().st_size > 0]
    prueba("fluentd recibe la salida de los contenedores (%d ficheros en output/docker)" % len(recogidos),
           bool(recogidos))

    # 9. Prueba negativa -----------------------------------------------------
    # Demuestra que el tráfico depende de las rutas estáticas de FRR: se quita en
    # R2 la ruta hacia la RED del primer PC, se comprueba que deja de llegar al
    # servidor y se restaura.
    h = topo["hosts"][0] if topo["hosts"] else None
    ruta = None
    if h and not topo["ospf"] and "R2" in routers:
        ruta = next((x for x in routers["R2"]["rutas"]
                     if x["prefijo"] == redes[h["red"]]["prefijo"]), None)
    if ruta:
        titulo("9. Prueba negativa: sin la ruta estatica de R2 hacia %s no hay conectividad" % h["lan"])
        orden = "ip route %s %s" % (ruta["prefijo"], ruta["siguiente_salto"])
        try:
            dexec("R2", "vtysh", "-c", "configure terminal", "-c", "no " + orden)
            time.sleep(1)
            rc, _ = dexec(h["nombre"], "ping", "-c", "2", "-W", "2", srv["ip"])
            prueba("R2 sin '%s': %s NO alcanza al servidor" % (orden, h["nombre"]), rc != 0)
        finally:
            dexec("R2", "vtysh", "-c", "configure terminal", "-c", orden)
        time.sleep(1)
        rc, _ = dexec(h["nombre"], "ping", "-c", "2", "-W", "2", srv["ip"])
        prueba("ruta restaurada en R2: %s vuelve a alcanzar al servidor" % h["nombre"], rc == 0)

    # Resumen ---------------------------------------------------------------
    total, fallos = len(resultados), [d for d, ok in resultados if not ok]
    resumen = "RESULTADO: %d/%d pruebas superadas" % (total - len(fallos), total)
    print("\n" + resumen)
    for d in fallos:
        print("  FALLO: " + d)
    informe.insert(0, "Plan de pruebas - %s\n%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), resumen))
    escribir(DIR_ESC / "resultados_pruebas.txt", "\n".join(informe) + "\n")
    print("Informe completo: %s" % (DIR_ESC / "resultados_pruebas.txt"))
    return 0 if not fallos else 1


# =============================================================================
# 6. LÍNEA DE COMANDOS
# =============================================================================
def main():
    p = argparse.ArgumentParser(description="Genera, despliega y prueba la red SDN con Docker Compose.")
    sub = p.add_subparsers(dest="orden")

    def opciones_topologia(sp):
        sp.add_argument("--hosts", type=int, default=1, choices=range(1, 21), metavar="N",
                        help="PCs por cada RED (1-20, por defecto 1)")
        sp.add_argument("--ospf", action="store_true",
                        help="usar OSPF en lugar de rutas estaticas")
        sp.add_argument("--puerto-web", type=int, default=3000,
                        help="puerto del anfitrion para la web (por defecto 3000)")
        sp.add_argument("--repo", default=REPO_SSR, help="repositorio Git del servidor web")
        sp.add_argument("--docker-antiguo", action="store_true",
                        help="generar para Docker Engine < 28 (sin gateway_mode_ipv4)")

    sp = sub.add_parser("desplegar", help="genera, levanta el escenario y lo prueba (por defecto)")
    opciones_topologia(sp)
    sp.add_argument("--sin-pruebas", action="store_true", help="no ejecutar el plan de pruebas al final")
    sp.add_argument("--forzar", action="store_true",
                    help="no comprobar si las subredes chocan con las redes del anfitrion")
    sp = sub.add_parser("generar", help="solo genera los ficheros en ./escenario")
    opciones_topologia(sp)
    sub.add_parser("probar", help="ejecuta el plan de pruebas")
    sub.add_parser("estado", help="estado de los contenedores")
    sp = sub.add_parser("parar", help="detiene y elimina el escenario")
    sp.add_argument("--borrar-datos", action="store_true", help="borra tambien los logs recogidos")

    argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        argv = ["desplegar"] + argv
    args = p.parse_args(argv)
    acciones = {"desplegar": cmd_desplegar, "generar": lambda a: (cmd_generar(a), 0)[1],
                "probar": cmd_probar, "estado": cmd_estado, "parar": cmd_parar}
    sys.exit(acciones[args.orden](args))


if __name__ == "__main__":
    main()
