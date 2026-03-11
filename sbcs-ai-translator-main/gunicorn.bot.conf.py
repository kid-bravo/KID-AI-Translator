import multiprocessing, os
bind = "0.0.0.0:8000"
workers = int(os.getenv("WEB_CONCURRENCY", max(2, multiprocessing.cpu_count())))
worker_class = "uvicorn.workers.UvicornWorker"
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
graceful_timeout = timeout
keepalive = 30
accesslog = "-"
errorlog = "-"
