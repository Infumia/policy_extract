"""Policy PDF'lerinden structured veri çıkaran küçük paket."""

from policy_extract.extractor import extract_policy
from policy_extract.service import SERVICE_VERSION

__version__ = SERVICE_VERSION
__all__ = ["extract_policy", "__version__"]
