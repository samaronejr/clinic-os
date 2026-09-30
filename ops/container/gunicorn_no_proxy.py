forwarded_allow_ips = ""
secure_scheme_headers = {}
# ADR-014: access logs carry method + status + duration only; the request
# line, query, remote address and headers are never logged (PHI boundary).
access_log_format = "%(m)s %(s)s %(D)s"
