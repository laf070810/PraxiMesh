"""Application process defaults. No IP, query string, referrer, cookies or code in access logs."""

bind = "0.0.0.0:8000"
workers = 2
threads = 2
timeout = 30
worker_tmp_dir = "/tmp"
accesslog = "-"
access_log_format = "%(m)s %(U)s %(s)s %(L)s"
# Scheme trust is handled once, by Django's explicit peer/protocol policy.
# Do not also infer HTTPS from X-Forwarded-SSL or another Gunicorn header.
forwarded_allow_ips = ""
