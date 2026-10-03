FROM python:3.12.12-alpine3.22
ARG BUILD_VERSION
ARG BUILD_ARCH
LABEL io.hass.type="addon" io.hass.version="${BUILD_VERSION}" io.hass.arch="${BUILD_ARCH}"
RUN apk add --no-cache git ca-certificates
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY manager /app/manager
ENV PYTHONUNBUFFERED=1
EXPOSE 8099
CMD ["python", "-m", "manager.web"]
