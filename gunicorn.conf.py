"""Production worker configuration. Mail features require explicit configuration."""
import os

bind = '0.0.0.0:' + os.environ.get('PORT', '10000')
workers = 1
worker_class = 'gthread'
threads = 4
timeout = 120
graceful_timeout = 30
keepalive = 5
limit_request_line = 4094
limit_request_fields = 100
limit_request_field_size = 8190
# Do not add body/header/query logging to credential-bearing requests.
accesslog = None
capture_output = False
