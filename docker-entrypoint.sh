#!/bin/sh
set -e

echo "==> Ejecutando migraciones de base de datos y sincronizando permisos..."
python -m app.cli migrate

echo "==> Iniciando servicio..."
exec "$@"
