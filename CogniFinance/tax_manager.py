# tax_manager.py ver 0.1
# For CogniFinance ver 1.8

import os
import json
import logging
import shutil
from pathlib import Path
from config import TAX_TEMPLATES_DIR

logger = logging.getLogger (__name__)

class TaxTemplateManager:
    def __init__(self, templates_dir="tax_templates"):
        # The user-writable directory in ~/CogniSuite/
        if templates_dir is None:
            self.templates_dir = str(TAX_TEMPLATES_DIR)
        else:
            self.templates_dir = str(templates_dir)

        os.makedirs(self.templates_dir, exist_ok=True)

        # Run the bootstrap to ensure defaults are present
        self._bootstrap_default_templates()

    def _bootstrap_default_templates(self):
        """
        Copies default templates from the app directory to the user directory.
        Only copies files that DO NOT already exist in the user directory,
        ensuring we never overwrite a user's customized templates.
        """
        # Path(__file__).parent is the CogniFinance app code directory
        app_template_dir = Path(__file__).parent / "tax_templates"

        if not app_template_dir.exists():
            return

        for file_path in app_template_dir.glob("*.json"):
            dest_path = Path(self.templates_dir) / file_path.name
            if not dest_path.exists():
                try:
                    shutil.copy2(file_path, dest_path)
                    logger.info(f"Bootstrapped default tax template: {file_path.name}")
                except Exception as e:
                    logger.warning(f"Failed to bootstrap template {file_path.name}: {e}")


    def load_available_templates(self) -> dict:
        """Scans the template directory and loads all valid JSON tax templates."""
        templates = {}
        if not os.path.exists(self.templates_dir):
            return templates

        for file_name in os.listdir(self.templates_dir):
            if file_name.endswith(".json"):
                path = os.path.join(self.templates_dir, file_name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if "template_id" in data and "title" in data:
                            templates[data["template_id"]] = data
                except Exception as e:
                    logger.warning(f"Failed to load tax template '{file_name}': {e}")
                    print(f"Failed to load tax template {file_name}: {e}")
        return templates
