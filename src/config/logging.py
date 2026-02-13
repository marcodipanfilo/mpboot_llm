import logging
from typing import Optional

def setup_logging(level: int = logging.INFO, name: Optional[str] = None) -> logging.Logger:
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    return logging.getLogger(name or "mpboot_llm")
