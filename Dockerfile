# Dockerfile para ejecutar tu Flask app con yt-dlp + ffmpeg en Render
FROM python:3.11-slim

# Variables de entorno para comportamiento no interactivo y buffering
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=5001

# Instalar ffmpeg y dependencias necesarias para paquetes compilados
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
     ffmpeg \
     gcc \
     build-essential \
     libssl-dev \
     libffi-dev \
     git \
     curl \
     unzip \
  && curl -fsSL https://deno.land/x/install/install.sh | sh \
  && mv /root/.deno/bin/deno /usr/local/bin/deno \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

# Directorio de trabajo
WORKDIR /app

# Copiar requirements e instalar
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
  && pip install -r /app/requirements.txt

# Copiar el resto del código
COPY . /app

# Exponer puerto (Render pasa $PORT al contenedor)
EXPOSE 5001

# Ejecutar con gunicorn (server:app es el módulo Flask que expones)
CMD exec gunicorn server:app --bind 0.0.0.0:$PORT --workers 4 --threads 2 --timeout 120
