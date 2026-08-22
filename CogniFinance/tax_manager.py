import os
import json
import logging

logger = logging.getLogger (__name__)

class TaxTemplateManager:
    def __init__(self, templates_dir="tax_templates"):
        if templates_dir is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            templates_dir = os.path.join(base_dir, "tax_templates")
        self.templates_dir = templates_dir
        os.makedirs(self.templates_dir, exist_ok=True)

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
