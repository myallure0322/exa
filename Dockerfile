FROM python3.11-slim

WORKDIR app
COPY . app

RUN pip install --no-cache-dir 
  mcp[cli]=1.25.0,2 
  httpx[socks]=0.27.0 
  uvicorn=0.30.0 
  starlette=0.37.0

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD [python, exa_pool_mcp.py]