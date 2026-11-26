FROM python:3.12-slim
WORKDIR /app
COPY sendfile.py server.py watcher.py settings.py ui.html ./
# /data holds only settings.json (saved from the web UI). Screenshots are never written anywhere.
RUN mkdir /data && chown nobody /data
ENV PYTHONUNBUFFERED=1 PORT=8989 SETTINGS_FILE=/data/settings.json
USER nobody
EXPOSE 8989
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8989/health', timeout=4)"
CMD ["python", "server.py"]
