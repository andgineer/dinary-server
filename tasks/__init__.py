"""Invoke task package — re-exports all public tasks so ``inv --list`` works."""

import sys

from invoke import Collection

from .analytics import analytics
from .backups.analytics_backup import (
    backup_analytics,
    backup_analytics_yadisk,
    restore_analytics,
    restore_analytics_yadisk,
)
from .backups.backups_replica import (
    replica_reset_trust,
    replica_resync,
    restore_replica,
    setup_replica,
)
from .backups.backups_restore import restore_from_yadisk
from .backups.backups_status import backup_status
from .backups.backups_yandex import setup_yadisk
from .db import (
    migrate,
    restore_primary,
    restore_yoyo,
    seed_categories,
    verify_db,
)
from .deploy import deploy
from .devtools.build_docs import ALLOWED_DOC_LANGUAGES, build_docs, docs_task_factory
from .devtools.constants import ALLOWED_VERSION_TYPES
from .devtools.dev import build_static, dev, pre, reqs, test, uv, ver_task_factory, version
from .dinary_ai import install_dinary_ai, setup_dinary_ai, uninstall_dinary_ai
from .healthcheck import healthcheck
from .receipt import classify_receipt, reclassify_receipts
from .reports.report_tasks import report_expenses, report_income, sql_query
from .server import logs, restart_server, ssh, ssh_replica, status
from .setup import setup_server

__all__ = [
    "analytics",
    "backup_analytics",
    "backup_analytics_yadisk",
    "restore_analytics",
    "restore_analytics_yadisk",
    "backup_status",
    "classify_receipt",
    "reclassify_receipts",
    "build_docs",
    "build_static",
    "deploy",
    "dev",
    "docs_task_factory",
    "healthcheck",
    "install_dinary_ai",
    "logs",
    "migrate",
    "pre",
    "reqs",
    "report_expenses",
    "report_income",
    "restart_server",
    "replica_reset_trust",
    "replica_resync",
    "restore_from_yadisk",
    "restore_primary",
    "restore_yoyo",
    "restore_replica",
    "seed_categories",
    "setup_dinary_ai",
    "setup_replica",
    "setup_server",
    "setup_yadisk",
    "sql_query",
    "ssh",
    "ssh_replica",
    "status",
    "test",
    "uninstall_dinary_ai",
    "uv",
    "ver_task_factory",
    "verify_db",
    "version",
]

namespace = Collection.from_module(sys.modules[__name__])
for name in ALLOWED_VERSION_TYPES:
    namespace.add_task(ver_task_factory(name), name=f"ver-{name}")  # type: ignore[bad-argument-type]
for name in ALLOWED_DOC_LANGUAGES:
    namespace.add_task(docs_task_factory(name), name=f"docs-{name}")  # type: ignore[bad-argument-type]
