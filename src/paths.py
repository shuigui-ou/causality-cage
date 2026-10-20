# -*- coding: utf-8 -*-
"""项目路径解析。单一出处，避免各模块各自拼路径。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"

SQLITE_PATH = DATA_DIR / "state.db"
SIGNALS_RAW_DIR = DATA_DIR / "signals_raw"
REJECTION_CASES_DIR = DATA_DIR / "rejection_cases"
BASELINE_SERIES_DIR = DATA_DIR / "baseline_series"

THRESHOLDS_JSON = CONFIG_DIR / "thresholds.json"
SOURCES_JSON = CONFIG_DIR / "sources.json"
COLLECTION_PARAMS_JSON = CONFIG_DIR / "collection_params.json"
