FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Mexico_City

WORKDIR /app

# Instalar tzdata para soporte de zonas horarias (ZoneInfo)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Instalar dependencias de Python aprovechando la caché de capas
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copiar el código de la aplicación y el entrypoint
COPY app ./app
COPY .env.example ./.env.example
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

# Crear directorio para almacenamiento local de media y usuario no-root
RUN chmod +x /app/docker-entrypoint.sh && \
    mkdir -p /app/media && \
    adduser --disabled-password --gecos "" appuser && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
