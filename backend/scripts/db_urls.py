"""Connection strings used by the setup scripts (migrate, seed, wait).

The app itself connects as ``app_user`` (DATABASE_URL), which is subject to row-level security.
Only these setup scripts use the admin URL, because they create roles, tables and seed data.
"""
import os

ADMIN_DATABASE_URL = os.getenv("ADMIN_DATABASE_URL", "postgresql://postgres:postgres@db:5432/hrchat")
