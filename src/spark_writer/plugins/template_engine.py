"""Simple string interpolation for SparkPlug manifests.

No complex templating - just basic {{variable}} replacement for user scripts.
Users are responsible for their own script security.
"""

import re
from typing import Any, Dict


class SparkTemplateEngine:
    """Simple {{variable}} substitution for user-controlled content.
    
    Design philosophy:
    - Users provide complete scripts/content
    - Variables are their own data (authkeys, hostnames, etc.)
    - No shell escaping needed - user controls the context
    - Fails fast on undefined variables
    """

    VAR_PATTERN = re.compile(r'\{\{\s*(\w+)\s*\}\}')

    def validate_template(self, template_string: str) -> bool:
        """Validate template syntax by ensuring all variables are well-formed."""
        try:
            # Attempt a dry-run render with placeholder values for discovered vars.
            vars_found = {match.group(1): '' for match in self.VAR_PATTERN.finditer(template_string)}
            self.render(template_string, vars_found)
            return True
        except ValueError:
            return False
        except Exception:
            return False

    def render(self, template_string: str, values: Dict[str, Any]) -> str:
        """Replace {{variable}} placeholders with values.
        
        Args:
            template_string: User's script/content with {{variable}} placeholders
            values: User-provided configuration values
            
        Returns:
            String with variables replaced
            
        Raises:
            ValueError: If template references undefined variables
        """
        def replacer(match):
            var_name = match.group(1).strip()
            if var_name not in values:
                raise ValueError(f"Undefined variable: {var_name}")
            return str(values[var_name])
        
        return self.VAR_PATTERN.sub(replacer, template_string)

