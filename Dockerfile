FROM eclipse-temurin:21-jre-jammy AS java21

FROM eclipse-temurin:25-jre-jammy

ENV WEB_PORT=8080 \
    MC_PORT=25565 \
    MINESERVE_DATA=/data \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/mineserve
COPY docker/panel.py /opt/mineserve/panel.py
COPY linux/docker_manager.py /opt/mineserve/docker_manager.py
COPY linux/webpanel.html /opt/mineserve/webpanel.html
COPY --from=java21 /opt/java/openjdk /opt/java/openjdk-21

RUN mkdir -p /data/server \
    && useradd --system --uid 10001 --create-home --home-dir /home/mineserve mineserve \
    && chown -R mineserve:mineserve /opt/mineserve /data

USER mineserve
EXPOSE 8080/tcp 25565/tcp 25565/udp
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/', timeout=2)" || exit 1

CMD ["python", "/opt/mineserve/panel.py"]
