FROM python:3.13-slim

WORKDIR /app

COPY requirements-remote.txt .
RUN pip install --no-cache-dir -r requirements-remote.txt

COPY mcp-remote-server.py .

EXPOSE 8888

CMD ["python", "mcp-remote-server.py"]
