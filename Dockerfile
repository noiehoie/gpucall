FROM python:3.12-slim

WORKDIR /app
ARG GPUCALL_GIT_COMMIT=""
RUN printf "%s" "$GPUCALL_GIT_COMMIT" > /app/BUILD_COMMIT
COPY pyproject.toml ./
COPY gpucall ./gpucall
RUN pip install --no-cache-dir ".[providers]"
RUN mkdir -p /app/.flash && chown -R 1000:1000 /app
USER 1000:1000
EXPOSE 8080
CMD ["gpucall", "serve", "--config-dir", "/config", "--host", "0.0.0.0", "--port", "8080"]
