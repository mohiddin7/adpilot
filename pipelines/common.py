"""Shared config for pipeline scripts. Real names come from .env; defaults are neutral."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

PROJECT       = os.getenv("BQ_PROJECT_ID", "adpilot-lakehouse")
BRONZE_DS     = os.getenv("BQ_BRONZE_DATASET", "adpilot_bronze")
STAGING_DS    = os.getenv("BQ_STAGING_DATASET", "adpilot_staging")
PRODUCTION_DS = os.getenv("BQ_PRODUCTION_DATASET", "adpilot_production")
