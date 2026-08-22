"""Write ``openapi.yaml`` from the live application.

Generated from the same Pydantic models the code uses, so the published contract
cannot drift from the implementation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "../openapi.yaml")

    from app.main import create_app

    schema = create_app().openapi()

    try:
        import yaml

        body = yaml.safe_dump(schema, sort_keys=False, allow_unicode=True)
    except ModuleNotFoundError:
        # PyYAML is not a runtime dependency; JSON is a valid OpenAPI document.
        target = target.with_suffix(".json")
        body = json.dumps(schema, indent=2, ensure_ascii=False)

    target.write_text(body, encoding="utf-8")
    print(f"Wrote {target} ({len(schema['paths'])} paths)")


if __name__ == "__main__":
    main()
