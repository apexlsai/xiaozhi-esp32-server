ARG NODE_REGISTRY=docker.io/library
FROM ${NODE_REGISTRY}/node:22-bookworm AS build
WORKDIR /app
ADD --checksum=sha256:8984a0ccd790470234c34a98f6497e5924255299bbb2f6c331dcf6e38d1f9ab3 https://codeload.github.com/xinnan-tech/xiaozhi-mqtt-gateway/tar.gz/93f026ad3a9a5572c9cf6ce651dd8fcf46fd2419 /tmp/gateway.tar.gz
RUN tar -xzf /tmp/gateway.tar.gz --strip-components=1 -C /app
COPY package.json package-lock.json ksz.patch ./
RUN git apply --check ksz.patch && git apply ksz.patch \
    && npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    && rm ksz.patch
COPY start.cjs healthcheck.cjs verify-ota.cjs guide-control.cjs ./

FROM ${NODE_REGISTRY}/node:22-bookworm-slim
WORKDIR /app
COPY --from=build --chown=node:node /app /app
USER node
EXPOSE 1883/tcp 8884/udp 8007/tcp
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 CMD ["node", "healthcheck.cjs"]
CMD ["node", "start.cjs"]
