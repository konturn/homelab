FROM python:3.13-alpine3.22@sha256:e81548ac35b07a3bd4805f275107592ef458b1e893c0e04d45aedaa19416cca5

RUN apk add --no-cache tzdata

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code, configuration and playbooks ship together: a playbook change is an
# image change, deployed like any other (README.md).
COPY router ./router
COPY config.yml ./config.yml
COPY playbooks ./playbooks
COPY suppressions.yml ./suppressions.yml

# The uid the executor accepts (SO_PEERCRED); compose sets the same user and
# the executor socket's group. See ansible/roles/event-router.
USER 2470:2470

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080

ENTRYPOINT ["python", "-m", "router"]
CMD ["serve"]
