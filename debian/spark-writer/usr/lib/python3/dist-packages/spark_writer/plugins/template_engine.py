"""Template rendering for SparkPlug manifests.

Supports Jinja-style control flow and variable substitution while preserving
compatibility with hyphenated field identifiers used by manifest config IDs.
"""

import re
from typing import Any, Dict

from jinja2 import StrictUndefined
from jinja2.exceptions import TemplateError, TemplateSyntaxError
from jinja2.sandbox import SandboxedEnvironment


class SparkTemplateEngine:
    """Jinja-based rendering for user-controlled template content.
    
    Design philosophy:
    - Users provide complete scripts/content
    - Variables are their own data (authkeys, hostnames, etc.)
    - No shell escaping needed - user controls the context
    - Fails fast on undefined variables
    """

    _JINJA_BLOCK_PATTERN = re.compile(r"(\{\{.*?\}\}|\{%.*?%\})", re.DOTALL)

    def __init__(self) -> None:
        # Keep rendering deterministic and fail fast for missing variables.
        self._env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)

    def validate_template(self, template_string: str) -> bool:
        """Validate template syntax."""
        try:
            self._env.parse(template_string)
            return True
        except TemplateSyntaxError:
            return False
        except Exception:
            return False

    def _build_aliases(self, values: Dict[str, Any]) -> Dict[str, str]:
        """Map hyphenated keys to Jinja-safe aliases."""
        aliases: Dict[str, str] = {}
        for key in values:
            if '-' in key:
                aliases[key] = key.replace('-', '_')
        return aliases

    def _replace_identifiers(self, text: str, aliases: Dict[str, str]) -> str:
        """Replace aliased identifiers in expressions, skipping quoted literals."""
        if not aliases:
            return text

        keys = sorted(aliases.keys(), key=len, reverse=True)
        out: list[str] = []
        i = 0
        in_single = False
        in_double = False

        while i < len(text):
            ch = text[i]
            if ch == "'" and not in_double:
                in_single = not in_single
                out.append(ch)
                i += 1
                continue
            if ch == '"' and not in_single:
                in_double = not in_double
                out.append(ch)
                i += 1
                continue

            if in_single or in_double:
                out.append(ch)
                i += 1
                continue

            replaced = False
            for key in keys:
                if not text.startswith(key, i):
                    continue

                prev_char = text[i - 1] if i > 0 else ''
                next_idx = i + len(key)
                next_char = text[next_idx] if next_idx < len(text) else ''

                if (prev_char and (prev_char.isalnum() or prev_char == '_')):
                    continue
                if (next_char and (next_char.isalnum() or next_char == '_')):
                    continue

                out.append(aliases[key])
                i += len(key)
                replaced = True
                break

            if replaced:
                continue

            out.append(ch)
            i += 1

        return ''.join(out)

    def _normalize_template(self, template_string: str, aliases: Dict[str, str]) -> str:
        """Apply alias replacement inside Jinja expression/control blocks only."""

        def repl(match: re.Match[str]) -> str:
            block = match.group(0)
            if block.startswith('{{'):
                inner = block[2:-2]
                return '{{' + self._replace_identifiers(inner, aliases) + '}}'
            inner = block[2:-2]
            return '{%' + self._replace_identifiers(inner, aliases) + '%}'

        return self._JINJA_BLOCK_PATTERN.sub(repl, template_string)

    def render(self, template_string: str, values: Dict[str, Any]) -> str:
        """Render a template with user-provided values.
        
        Args:
            template_string: User's script/content with {{variable}} placeholders
            values: User-provided configuration values
            
        Returns:
            String with variables replaced
            
        Raises:
            ValueError: If template is invalid or references undefined variables
        """
        aliases = self._build_aliases(values)
        context: Dict[str, Any] = dict(values)
        for original, alias in aliases.items():
            context[alias] = values[original]

        normalized = self._normalize_template(template_string, aliases)
        try:
            template = self._env.from_string(normalized)
            return template.render(**context)
        except TemplateError as exc:
            raise ValueError(str(exc)) from exc

