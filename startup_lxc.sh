#!/bin/bash
# Crea y arranca el contenedor LXC u1, clona SSR-master-server y lanza el servidor Node.js.

NOMBRE="u1"
DIST="ubuntu"
RELEASE="jammy"
ARCH="amd64"
REPO="https://github.com/gisai/SSR-master-server.git"
DESTINO="/home/SSR-master-server"
PUERTO=3000   # puerto en el que escucha el servidor

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

# 4. Instalar git, Node.js y npm si faltan, y clonar el proyecto en /home
if ! sudo lxc-attach -n "$NOMBRE" -- which git node npm &>/dev/null; then
    echo "Instalando git, nodejs y npm..."
    sudo lxc-attach -n "$NOMBRE" -- apt-get update
    sudo lxc-attach -n "$NOMBRE" -- env DEBIAN_FRONTEND=noninteractive apt-get install -y git nodejs npm || exit 1
fi

if sudo lxc-attach -n "$NOMBRE" -- test -d "$DESTINO"; then
    echo "El proyecto ya está clonado en $DESTINO."
else
    echo "Clonando $REPO en $DESTINO..."
    sudo lxc-attach -n "$NOMBRE" -- git clone "$REPO" "$DESTINO"
fi

# 5. Instalar las dependencias del proyecto (solo la primera vez)
if ! sudo lxc-attach -n "$NOMBRE" -- test -d "$DESTINO/node_modules"; then
    echo "Instalando dependencias con npm install..."
    sudo lxc-attach -n "$NOMBRE" -- bash -c "cd $DESTINO && npm install" || exit 1
fi

# 6. Arrancar el servidor en segundo plano si no está ya en marcha
if sudo lxc-attach -n "$NOMBRE" -- pgrep -f "node ./bin/www" &>/dev/null; then
    echo "El servidor ya está en marcha."
else
    echo "Arrancando el servidor..."
    sudo lxc-attach -n "$NOMBRE" -- bash -c \
        "cd $DESTINO && setsid nohup node ./bin/www > /var/log/ssr-server.log 2>&1 < /dev/null &"
    sleep 2
fi

# 7. Mostrar su estado e IP
sudo lxc-info -n "$NOMBRE"
IP=$(sudo lxc-info -n "$NOMBRE" -iH | head -n 1)
echo
echo "Servidor disponible en: http://$IP:$PUERTO"
echo "Log del servidor (dentro del contenedor): /var/log/ssr-server.log"
