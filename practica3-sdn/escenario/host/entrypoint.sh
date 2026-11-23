#!/bin/bash
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
