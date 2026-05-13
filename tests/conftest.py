"""Fixtures partagées pour les tests cloe-care."""
import os
import sys
from pathlib import Path

# Permet aux tests d'importer les modules du repo sans installation
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Variables d'environnement minimales pour que les modules s'importent sans crash
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-prod-just-import-shim")
os.environ.setdefault("SERVICE_SECRET", "test-service-secret")
os.environ.setdefault("OPERATOR_OPENROUTER_KEY", "test-key")
