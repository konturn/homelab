FROM python:3.13-alpine3.22@sha256:e81548ac35b07a3bd4805f275107592ef458b1e893c0e04d45aedaa19416cca5

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
