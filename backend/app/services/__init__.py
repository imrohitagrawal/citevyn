"""Shared service helpers used by both the HTTP routes and the worker.

The service modules wrap DB access patterns that the route and worker
both need (audit writes, ingestion-job enqueueing, index promotion,
exact-lookup projection) so neither side has to duplicate the SQL or
the audit-event shape.
"""
