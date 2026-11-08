FROM python:3.14-alpine3.22@sha256:6b91e66ab2a880ce9ca5a1b91c70f45963ff71ff68268df056336e1a657d5efd

RUN apk add --no-cache tzdata

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY detector ./detector
COPY tests ./tests
# Defaults baked in; the deployed copies are mounted over /config.
COPY config.yml suppressions.yml /config/

RUN adduser -D -u 1000 svc
USER svc

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080

ENTRYPOINT ["python", "-m", "detector"]
CMD ["serve"]
