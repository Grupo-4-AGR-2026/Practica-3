#!/bin/bash
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
