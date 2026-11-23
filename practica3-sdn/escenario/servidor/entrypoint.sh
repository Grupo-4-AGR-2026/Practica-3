#!/bin/bash
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
#    para clonar el repositorio); las redes de la practica se alcanzan via R1.
ip route replace "$RED_INTERNA" via "$ROUTER"
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
