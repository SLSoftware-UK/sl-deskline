FROM python:3.12-slim

WORKDIR /app

# No apt-get build toolchain here on purpose: every package in
# requirements.txt, including psycopg2-binary (which bundles its own libpq
# rather than linking the system one), ships a prebuilt wheel for this
# image's platform, so nothing needs to be compiled from source at install
# time. Verified by installing requirements.txt into a bare python:3.12-slim
# container with no gcc/libpq-dev present.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN SECRET_KEY=build-time-placeholder python manage.py collectstatic --no-input

EXPOSE 8000

SHELL ["/bin/bash", "-c"]
CMD python manage.py migrate && echo "Migrations done" && exec gunicorn support_core.wsgi --bind "0.0.0.0:${PORT:-8000}" --workers 2 --timeout 120
