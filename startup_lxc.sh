#!/bin/bash
# Crea el contenedor LXC si no existe y lo arranca si no está encendido.

NOMBRE="u1"
DIST="ubuntu"
RELEASE="jammy"
ARCH="amd64"
REPO="https://github.com/gisai/SSR-master-server.git"
DESTINO="/home/SSR-master-server"

# 1. Crear el contenedor solo si no existe
if sudo lxc-info -n "$NOMBRE" &>/dev/null; then
    echo "El contenedor $NOMBRE ya existe."
else
    echo "Creando el contenedor $NOMBRE ($DIST $RELEASE $ARCH)..."
    sudo lxc-create -t download -n "$NOMBRE" -- --dist "$DIST" --release "$RELEASE" --arch "$ARCH" || exit 1
fi

# 2. Arrancarlo si no está en marcha
if sudo lxc-info -n "$NOMBRE" -s | grep -q RUNNING; then
    echo "El contenedor $NOMBRE ya está encendido."
else
    echo "Arrancando $NOMBRE..."
    sudo lxc-start -n "$NOMBRE" || exit 1
    sudo lxc-wait -n "$NOMBRE" -s RUNNING
fi

# 3. Esperar a que el contenedor tenga red (resuelve github.com)
echo "Esperando a que el contenedor tenga red..."
for i in $(seq 1 30); do
    sudo lxc-attach -n "$NOMBRE" -- getent hosts github.com &>/dev/null && break
    sleep 1
done

# 4. Instalar git si no está y clonar el proyecto en /home
if ! sudo lxc-attach -n "$NOMBRE" -- which git &>/dev/null; then
    echo "Instalando git..."
    sudo lxc-attach -n "$NOMBRE" -- apt-get update
    sudo lxc-attach -n "$NOMBRE" -- apt-get install -y git
fi

if sudo lxc-attach -n "$NOMBRE" -- test -d "$DESTINO"; then
    echo "El proyecto ya está clonado en $DESTINO."
else
    echo "Clonando $REPO en $DESTINO..."
    sudo lxc-attach -n "$NOMBRE" -- git clone "$REPO" "$DESTINO"
fi

# 5. Mostrar su estado e IP
sudo lxc-info -n "$NOMBRE"