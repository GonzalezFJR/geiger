Geiger Web (Raspberry Pi)
=========================

Web app en FastAPI + Uvicorn para leer un contador Geiger conectado a una Raspberry Pi mediante pulsos en GPIO y visualizar:

- Conteo en vivo con parpadeo (y sonido opcional en el navegador).
- Serie temporal integrada por segundo + media acumulada.
- Estimación de "actividad efectiva observada" en Bq ± error (Poisson).
- Histograma de Δt entre detecciones.

El backend de adquisición de pulsos usa RPi.GPIO con add_event_detect, que es el método más compatible con muchos setups reales en Raspberry Pi OS.


Requisitos
----------

- Raspberry Pi OS
- Python 3
- Un módulo/contador Geiger con salida de pulsos digitales
- Conexión de la señal a un GPIO de entrada

IMPORTANTE:
Los GPIO de la Raspberry Pi son de 3.3V.
Si tu módulo entrega pulsos a 5V, usa un divisor resistivo o conversor de nivel.

Ejemplo divisor recomendado:
- Señal -> 10k -> nodo -> GPIO
- Nodo -> 20k -> GND


Estructura del proyecto
-----------------------

geiger-web/
  main.py
  geiger.py
  geiger_test.py
  .env
  templates/
    index.html
  static/
    styles.css
    script.js


Instalación
-----------

1) Dependencias del sistema

  sudo apt update
  sudo apt install -y python3-rpi.gpio

2) Crear entorno virtual

  cd ~/geiger-web
  python3 -m venv .venv --system-site-packages
  source .venv/bin/activate

3) Dependencias Python

  pip install fastapi uvicorn[standard] jinja2 python-dotenv


Configuración .env
------------------

Crea un archivo .env en la raíz del proyecto:

  # GPIO en modo BCM (no pin físico)
  GEIGER_PIN=18

  # Logs de debug en consola
  GEIGER_VERBOSE=1

  # Tamaños de buffers en memoria
  GEIGER_MAX_DELTAS=2000
  GEIGER_MAX_SERIES=3600

  # --- Modo simulación sin hardware (opcional) ---
  # GEIGER_MOCK=1
  # GEIGER_MOCK_RATE=5

Notas:
- GEIGER_PIN usa numeración BCM (por ejemplo GPIO18).
- Asegúrate de que coincide con tu cableado real.


Prueba rápida del backend
-------------------------

Este script usa exactamente las mismas funciones que la app web:

  source .venv/bin/activate
  python geiger_test.py

Deberías ver:
- Mensajes de arranque del lector
- Incremento de total
- Resúmenes cada 5 segundos


Ejecutar la web app
-------------------

  source .venv/bin/activate
  uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1

Abre en el navegador:

  http://<IP_DE_TU_PI>:8000

Recomendación:
- Usa 1 worker para evitar conflictos de acceso al GPIO.
- Evita usar --reload en una demo estable con hardware.


Endpoints útiles
----------------

- UI:
  /

- Estado actual (útil para debug):
  /api/snapshot

- Reset global:
  POST /api/reset

- WebSocket (usado por la UI):
  /ws


Notas de diagnóstico
--------------------

Si un script simple de GPIO funciona y la web no:

1) Revisa que GEIGER_PIN en .env coincide con tu cableado.
2) Ejecuta primero:
     python geiger_test.py
3) Lanza Uvicorn con un solo worker:
     uvicorn main:app --workers 1


Licencia
--------

Uso libre para proyectos educativos y de divulgación.

