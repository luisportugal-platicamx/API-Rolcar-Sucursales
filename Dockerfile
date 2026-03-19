# Usamos una imagen base oficial de Python ligera
FROM python:3.11-slim

# Establecemos el directorio de trabajo dentro del contenedor
WORKDIR /app

# Actualizamos el sistema e instalamos dependencias básicas que podría necesitar Playwright
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Copiamos primero el archivo de requerimientos para aprovechar el caché de Docker
COPY requirements.txt .

# Instalamos las dependencias de Python
RUN pip install --no-cache-dir -r requirements.txt

# ¡Paso Crítico para Playwright! 
# Instalamos solo el binario de Chromium (que es el que usas en el código) 
# y le pedimos a Playwright que instale todas las dependencias del SO necesarias (--with-deps)
RUN playwright install --with-deps chromium

# Copiamos el resto de los archivos de tu proyecto (incluyendo sucursales2.py)
COPY . .

# Render suele asignar el puerto dinámicamente a través de la variable de entorno $PORT,
# pero exponemos el 10000 como convención estándar para Web Services ahí.
EXPOSE 10000

# Comando para arrancar el servidor FastAPI con Uvicorn.
# Usamos ${PORT:-10000} para que tome el puerto de Render, o el 10000 por defecto si lo corres local.
CMD ["sh", "-c", "uvicorn sucursales2:app --host 0.0.0.0 --port ${PORT:-10000}"]
