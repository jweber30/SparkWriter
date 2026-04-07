"""Trust evaluation for SparkPlug manifest sources."""

from typing import Optional, Tuple
from urllib.parse import urlparse


# Domains we trust implicitly for plugin distribution
TRUSTED_HOSTS = frozenset({
    'github.io',
    'raw.githubusercontent.com',
    'gitlab.com',
    'gist.githubusercontent.com',
})


def evaluate_trust(url: str, allow_insecure: bool = False) -> Tuple[bool, Optional[str]]:
    """Evaluate whether a plugin manifest URL should be trusted.
    
    Args:
        url: Plugin manifest URL to evaluate
        allow_insecure: Whether to allow HTTP sources (default: False)
        
    Returns:
        Tuple of (allowed, prompt_message):
        - (True, None): Auto-trusted, no prompt needed
        - (True, "message"): Allowed but show confirmation prompt
        - (False, "message"): Blocked with reason
    """
    parsed = urlparse(url)
    
    # Local files are always trusted (user's own manifests)
    if parsed.scheme == 'file' or not parsed.scheme:
        return True, None
    
    # Allow localhost for development/local backend
    if parsed.hostname == 'localhost':
        return True, f"Loading plugin from local: {parsed.netloc}"

    # Block HTTP unless explicitly enabled
    if parsed.scheme == 'http':
        if allow_insecure:
            return True, f"Loading plugin over insecure HTTP from {parsed.netloc}"
        return False, "HTTP sources are blocked. Enable 'Allow Insecure Plugins' in Preferences."
    
    # Only support HTTPS beyond this point
    if parsed.scheme != 'https':
        return False, f"Unsupported protocol: {parsed.scheme}. Only HTTPS is supported."
    
    # Check trusted hosts (including subdomains)
    host = parsed.netloc.lower()
    for trusted in TRUSTED_HOSTS:
        if host == trusted or host.endswith('.' + trusted):
            return True, None
    
    # Unknown HTTPS host - require user confirmation
    return True, f"Install plugin from {parsed.netloc}?"


def is_trusted_host(url: str) -> bool:
    """Check if URL is from a pre-trusted host without user confirmation.
    
    Args:
        url: URL to check
        
    Returns:
        True if from trusted host, False otherwise
    """
    allowed, prompt = evaluate_trust(url, allow_insecure=False)
    return allowed and prompt is None
