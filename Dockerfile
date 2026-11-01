FROM python:3.12-alpine

WORKDIR /app
COPY app.py .

USER nobody
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null || exit 1

CMD ["python", "-u", "app.py"]
