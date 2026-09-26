"""Extract structured data from policy PDFs."""

from policy_extract.extractor import extract_policy
from policy_extract.version import SERVICE_VERSION

__version__ = SERVICE_VERSION
__all__ = ["extract_policy", "__version__"]
