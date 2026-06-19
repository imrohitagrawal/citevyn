"""Ingestion worker package.

See :mod:`app.worker.cli` for the console-script entry point. The
modules in this package implement the fetch → parse → chunk → embed →
index pipeline that the admin route enqueues jobs for.
"""
