FROM python:3.12-slim
WORKDIR /app
COPY sendfile.py server.py ./
ENV PYTHONUNBUFFERED=1 PORT=8989
USER nobody
EXPOSE 8989
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8989/health', timeout=4)"
CMD ["python", "server.py"]
