FROM python:3.12-alpine

# Dashboard
WORKDIR /app
COPY server/dashboard.py .
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD wget -q -O /dev/null http://localhost:8080/ || exit 1
ENTRYPOINT ["python3", "dashboard.py"]
