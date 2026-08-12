"""Shared pytest fixtures, including a local SparkSession.

The Glue jobs' feature logic is the part most likely to be wrong and the most expensive
to debug in the cloud, so it is tested here against small fixture DataFrames. A local
Spark session starts in a few seconds; a Glue run takes minutes and costs money.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

# PySpark converts timestamps to Python datetimes using the *driver's* local timezone on
# collect(), so a machine in New York and a machine in London disagree about what a
# fixture means. Pinning the process to UTC before the JVM starts makes timestamp
# assertions portable — and matches how the Glue workers actually run.
os.environ["TZ"] = "UTC"
time.tzset()

# PySpark needs a JVM. Homebrew's openjdk is keg-only, so it is not on PATH and
# JAVA_HOME usually is not set — resolve it here rather than making every developer
# edit their shell profile to run the test suite.
_JDK_CANDIDATES = [
    "/opt/homebrew/opt/openjdk@17",
    "/opt/homebrew/opt/openjdk@11",
    "/usr/local/opt/openjdk@17",
    "/Library/Java/JavaVirtualMachines/temurin-17.jdk/Contents/Home",
]


def _resolve_java_home() -> str | None:
    if os.environ.get("JAVA_HOME"):
        return os.environ["JAVA_HOME"]
    for candidate in _JDK_CANDIDATES:
        if Path(candidate, "bin", "java").exists():
            return candidate
    if shutil.which("java"):
        return ""  # java is on PATH already; Spark will find it
    return None


@pytest.fixture(scope="session")
def spark():
    java_home = _resolve_java_home()
    if java_home is None:
        pytest.skip("no JVM found — install a JDK 17 (`brew install openjdk@17`) to run Spark tests")
    if java_home:
        os.environ["JAVA_HOME"] = java_home

    pyspark = pytest.importorskip("pyspark", reason="pyspark not installed")
    from pyspark.sql import SparkSession

    session = (
        SparkSession.builder.appName("fraud-lake-tests")
        .master("local[2]")
        # Small, deterministic, and fast: the default 200 shuffle partitions turn a
        # 12-row fixture into 200 empty tasks.
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.default.parallelism", "2")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.session.timeZone", "UTC")
        # Spark on Java 17 needs these opens for its own serialisation internals.
        .config(
            "spark.driver.extraJavaOptions",
            "--add-opens=java.base/java.nio=ALL-UNNAMED "
            "--add-opens=java.base/java.lang=ALL-UNNAMED "
            "--add-opens=java.base/java.util=ALL-UNNAMED "
            "--add-opens=java.base/sun.nio.ch=ALL-UNNAMED",
        )
        .config(
            "spark.executor.extraJavaOptions",
            "--add-opens=java.base/java.nio=ALL-UNNAMED "
            "--add-opens=java.base/java.lang=ALL-UNNAMED "
            "--add-opens=java.base/java.util=ALL-UNNAMED "
            "--add-opens=java.base/sun.nio.ch=ALL-UNNAMED",
        )
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    assert pyspark.__version__
    yield session
    session.stop()
